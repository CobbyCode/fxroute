#define _GNU_SOURCE
#include "dsp.h"
#include "autogain.h"
#include "crystalizer.h"
#include "lv2_host.h"
#include "upmix_fast.h"
#include "upmix_hq.h"

#include <errno.h>
#include <math.h>
#include <limits.h>
#include <samplerate.h>
#include <stdint.h>
#include <stdatomic.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MAX_ROUTES 1024
#define MAX_BIQUADS 32
/* Separately low-passed pre-crossover buses per output (Surround speaker
 * groups); group 0 carries matrix/xsos lines, groups 1.. gmatrix/gxsos. */
#define FXDSP_XO_GROUPS 4U
#define MAX_LINE 4096
#define CONV_BLOCK 256
#define PROCESS_BLOCK 1024
#define MAX_STAGES 128
#define MAX_CONTROLS 268
#define MAX_LIVE_UPDATES 1024
#define LIVE_COMMIT_TIMEOUT_NS 250000000L
#define PI 3.14159265358979323846
/* PEQ gain range in dB, mirroring the Python validation (-24..24) so
 * extreme finite values cannot overflow the biquad math into Inf/NaN. */
#define PEQ_GAIN_DB_MIN -24.0f
#define PEQ_GAIN_DB_MAX 24.0f
/* Delay buffer capacities in ms: ordinary stages allocate 500ms, stages
 * whose id ends in "-delay" allocate 10000ms, outputs allocate 500ms.
 * Config and live paths enforce these same limits. */
#define STAGE_DELAY_MAX_MS 500.0f
#define PEQ_STAGE_DELAY_MAX_MS 10000.0f
#define OUTPUT_DELAY_MAX_MS 500.0f

typedef struct { unsigned in, out; float gain; int post_crossover; unsigned group; } route;
typedef struct { float b0, b1, b2, a1, a2, z1, z2; } biquad;
typedef struct { float re, im; } complex_value;
typedef struct {
    size_t tap_count, head_count, head_pos, partition_count, input_pos, spectrum_pos;
    float *head, *head_fwd, *head_history, *win, *input_block, *tail_output, *overlap;
    complex_value *ir_spectra, *input_spectra, *work;
} convolution;
typedef struct {
    float gain, polarity;
    size_t delay, delay_pos, delay_size;
    float *delay_line;
    biquad filters[MAX_BIQUADS];
    unsigned filter_count;
    biquad crossover_filters[MAX_BIQUADS];
    unsigned char crossover_group[MAX_BIQUADS];
    unsigned crossover_filter_count, crossover_groups;
    /* Per-output correction bank: raw SOS biquads share filters[] in file
     * order (live_peq addresses array positions); at most one convolver. */
    int has_conv;
    convolution conv;
    char conv_path[1024];
    unsigned conv_channel;
    float conv_wet, conv_dry, conv_in_gain, conv_out_gain;
    float peak, square_sum;
    uint64_t meter_frames;
} output_state;

typedef enum { STAGE_CONVOLVER, STAGE_DELAY, STAGE_HEADROOM, STAGE_AUTOGAIN,
               STAGE_CRYSTALIZER, STAGE_LV2, STAGE_MASTER_GAIN, STAGE_UPMIXER } stage_kind;
typedef struct { char symbol[128]; float value; } stage_control;
typedef struct {
    stage_kind kind;
    char id[256], argument[1024];
    stage_control controls[MAX_CONTROLS];
    unsigned control_count;
    float wet, dry, input_gain, output_gain, gain;
    float left_ms, right_ms, target_db, silence_db, intensity_db;
    unsigned history_seconds;
    fx_autogain_reference reference;
    convolution conv[FXDSP_MAX_CHANNELS];
    float *delay_line[FXDSP_MAX_CHANNELS];
    size_t delay[FXDSP_MAX_CHANNELS], delay_size[FXDSP_MAX_CHANNELS], delay_pos[FXDSP_MAX_CHANNELS];
    fx_autogain *autogain[FXDSP_MAX_CHANNELS / 2];
    fx_crystalizer *crystalizer[FXDSP_MAX_CHANNELS];
    fx_lv2_host *lv2[FXDSP_MAX_CHANNELS / 2];
    /* Surround upmixer: engine (Fast matrix or HQ spectral), steering strengths
     * 0..10, HQ FFT size and the destinations that exist. */
    fx_upmix_fast *upmixer;
    fx_upmix_hq *upmixer_hq;
    unsigned upmix_hq, upmix_fft_size;
    float upmix_center, upmix_surround;
    unsigned upmix_center_out, upmix_side_out, upmix_rear_out;
    /* Matched-latency loudness compensation (post-LSP output trim). */
    size_t lv2_frames_fed;
    unsigned lv2_frame_size, lv2_buf_size;
    int lv2_comp_transition;
    float lv2_comp_old_g, lv2_comp_new_g;
    size_t lv2_comp_boundary_l, lv2_comp_boundary_r;
} dsp_stage;

typedef enum { LIVE_CONTROL, LIVE_PARAM, LIVE_MATRIX, LIVE_PEQ, LIVE_OUTPUT } live_kind;
typedef struct {
    live_kind kind;
    unsigned first, second;
    float values[8];
    int invert;
    char stage_id[256], symbol[128], type[32];
} live_update;

struct fxdsp {
    unsigned rate, inputs, outputs;
    unsigned surround;
    route routes[MAX_ROUTES];
    unsigned route_count;
    /* Routes grouped by output channel (CSR) so the mixing loop skips the
     * per-sample output-match scan: routes[] indices for output o live in
     * route_order[route_start[o] .. route_start[o + 1]).  Live matrix updates
     * only rewrite gains, never the route set, so this stays valid at runtime. */
    unsigned route_order[MAX_ROUTES];
    unsigned route_start[FXDSP_MAX_CHANNELS + 1];
    output_state out[FXDSP_MAX_CHANNELS];
    dsp_stage stages[MAX_STAGES];
    unsigned stage_count;
    _Atomic int effect_bypass;
    /* Audio thread only: the stage chain was skipped since the upmixers last ran. */
    int effects_were_bypassed;
    _Atomic uint32_t output_gain_bits;
    float *scratch[2][FXDSP_MAX_CHANNELS];
    unsigned *fft_reverse;
    complex_value *fft_roots;
    _Atomic uint32_t mute_mask;
    _Atomic uint32_t peak_bits[FXDSP_MAX_CHANNELS];
    live_update live[MAX_LIVE_UPDATES];
    _Atomic unsigned live_count;
    _Atomic int live_commit;
    /* Borrowed: the owner of the pool is whoever called fx_workers_create. */
    fx_workers *workers;
    fx_workers *owned_workers;
    unsigned worker_demand;
    int parallel_outputs;
    int measure;
    fx_timing timing[FXDSP_TIMING_COUNT];
    _Atomic int timing_reset;
};

static unsigned effect_pairs(const fxdsp *d) { return d->surround ? 5U : d->inputs / 2U; }
static unsigned pair_left(const fxdsp *d, unsigned pair) {
    static const unsigned positions[] = {0U, 2U, 3U, 4U, 6U};
    return d->surround ? positions[pair] : pair * 2U;
}
static unsigned pair_right(const fxdsp *d, unsigned pair) {
    static const unsigned positions[] = {1U, 2U, 3U, 5U, 7U};
    return d->surround ? positions[pair] : pair * 2U + 1U;
}
static unsigned channel_side(const fxdsp *d, unsigned channel) {
    return d->surround && (channel == 2U || channel == 3U) ? 0U : channel & 1U;
}

static uint32_t float_bits(float value) { uint32_t bits; memcpy(&bits,&value,sizeof bits); return bits; }
static float bits_float(uint32_t bits) { float value; memcpy(&value,&bits,sizeof value); return value; }

static void update_peak(_Atomic uint32_t *peak, float value) {
    uint32_t wanted=float_bits(value),current=atomic_load_explicit(peak,memory_order_relaxed);
    while(bits_float(current)<value&&!atomic_compare_exchange_weak_explicit(peak,&current,wanted,memory_order_relaxed,memory_order_relaxed)) {}
}

static void write_post_effect_taps(float *const *post_effect, unsigned inputs,
                                   float *const *source_buffers,
                                   size_t offset, size_t count) {
    for(unsigned channel=0;channel<2U;channel++) {
        if(channel<inputs) memcpy(post_effect[channel]+offset,source_buffers[channel],count*sizeof(float));
        else memset(post_effect[channel]+offset,0,count*sizeof(float));
    }
}

static void fail(char *error, size_t size, const char *message) {
    if (error && size) snprintf(error, size, "%s", message);
}

static uint16_t u16le(const unsigned char *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t u32le(const unsigned char *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }

static int load_wav(const char *path, unsigned wanted_channel, float **samples, size_t *count, unsigned wanted_rate) {
    FILE *file = fopen(path, "rb");
    unsigned char header[12], chunk[8], fmt[40];
    uint16_t format = 0, channels = 0, bits = 0;
    uint32_t rate = 0, data_size = 0;
    long data_offset = 0;
    if (!file || fread(header, 1, 12, file) != 12 || memcmp(header, "RIFF", 4) || memcmp(header + 8, "WAVE", 4)) goto bad;
    while (fread(chunk, 1, 8, file) == 8) {
        uint32_t size = u32le(chunk + 4);
        if (!memcmp(chunk, "fmt ", 4)) {
            if (size < 16 || size > sizeof fmt || fread(fmt, 1, size, file) != size) goto bad;
            format = u16le(fmt); channels = u16le(fmt + 2); rate = u32le(fmt + 4); bits = u16le(fmt + 14);
        } else if (!memcmp(chunk, "data", 4)) {
            data_offset = ftell(file); data_size = size;
            if (fseek(file, size + (size & 1), SEEK_CUR)) goto bad;
        } else if (fseek(file, size + (size & 1), SEEK_CUR)) goto bad;
    }
    if (!data_offset || !channels || !rate || !wanted_rate || wanted_channel >= channels || !((format == 1 && (bits == 16 || bits == 24 || bits == 32)) || (format == 3 && bits == 32))) goto bad;
    size_t bytes = bits / 8, frames = data_size / (bytes * channels);
    if (!data_size || !frames) goto bad;
    float *result = calloc(frames, sizeof *result);
    unsigned char raw[4];
    if (!result || fseek(file, data_offset, SEEK_SET)) { free(result); goto bad; }
    for (size_t i = 0; i < frames; i++) {
        for (unsigned c = 0; c < channels; c++) {
            if (fread(raw, 1, bytes, file) != bytes) { free(result); goto bad; }
            if (c != wanted_channel) continue;
            if (format == 3) memcpy(&result[i], raw, 4);
            else if (bits == 16) result[i] = (int16_t)u16le(raw) / 32768.0f;
            else if (bits == 24) { int32_t value = raw[0] | raw[1] << 8 | raw[2] << 16; if (value & 0x800000) value |= ~0xffffff; result[i] = value / 8388608.0f; }
            else result[i] = (int32_t)u32le(raw) / 2147483648.0f;
        }
    }
    fclose(file); file = NULL;
    if (rate != wanted_rate) {
        double ratio = (double)wanted_rate / rate;
        double converted_frames = ceil(frames * ratio);
        size_t output_frames;
        float *resampled;
        SRC_DATA conversion = {0};
        if (frames > LONG_MAX || !isfinite(converted_frames) || converted_frames < 1.0 ||
            converted_frames >= (double)LONG_MAX || converted_frames >= (double)(SIZE_MAX / sizeof *resampled)) {
            free(result); goto bad;
        }
        output_frames = (size_t)converted_frames + 1U;
        resampled = calloc(output_frames, sizeof *resampled);
        if (!resampled) { free(result); goto bad; }
        conversion.data_in = result; conversion.input_frames = (long)frames;
        conversion.data_out = resampled; conversion.output_frames = (long)output_frames;
        conversion.src_ratio = ratio; conversion.end_of_input = 1;
        if (src_simple(&conversion, SRC_SINC_BEST_QUALITY, 1) || conversion.output_frames_gen <= 0) {
            free(resampled); free(result); goto bad;
        }
        free(result); result = resampled; frames = (size_t)conversion.output_frames_gen;
        /* libsamplerate's SINC converters scale the resampled signal by
         * approximately the conversion ratio (measured: |H(997 Hz)| x1.99 at
         * 96 kHz, x3.98 at 192 kHz, x0.916 at 44.1 kHz against a 48 kHz
         * source).  Without compensation the same convolver configuration
         * changes level by about +6 dB per rate doubling when the playback
         * rate changes.  The pre-native-DSP engine compensated the same
         * libsamplerate behavior with hidden output-gain anchors; the native
         * engine fixes the cause by normalizing the resampled IR by the
         * ratio, making the convolution level-stable across rates.
         * (test_native_dsp_convolver_sr_level.py verifies this.) */
        for (size_t i = 0; i < frames; i++) result[i] = (float)(result[i] / ratio);
    }
    *samples = result; *count = frames; return 0;
bad:
    if (file) fclose(file);
    return -1;
}

static void fft(const fxdsp *d, complex_value *values, int inverse) {
    const size_t size = CONV_BLOCK * 2;
    for (size_t i = 0; i < size; i++) if (d->fft_reverse[i] > i) {
        complex_value swap = values[i]; values[i] = values[d->fft_reverse[i]]; values[d->fft_reverse[i]] = swap;
    }
    for (size_t width = 2; width <= size; width *= 2) {
        size_t step = size / width;
        for (size_t base = 0; base < size; base += width) for (size_t j = 0; j < width / 2; j++) {
            complex_value root = d->fft_roots[j * step];
            if (inverse) root.im = -root.im;
            complex_value right = values[base + j + width / 2];
            complex_value product = {right.re * root.re - right.im * root.im, right.re * root.im + right.im * root.re};
            complex_value left = values[base + j];
            values[base + j].re = left.re + product.re; values[base + j].im = left.im + product.im;
            values[base + j + width / 2].re = left.re - product.re; values[base + j + width / 2].im = left.im - product.im;
        }
    }
    if (inverse) for (size_t i = 0; i < size; i++) { values[i].re /= size; values[i].im /= size; }
}

static int prepare_convolution(fxdsp *d, convolution *c, float *taps, size_t count) {
    const size_t fft_size = CONV_BLOCK * 2;
    if (!taps || !count) return -1;
    c->tap_count = count; c->head_count = count < CONV_BLOCK ? count : CONV_BLOCK;
    c->partition_count = count > CONV_BLOCK ? (count - CONV_BLOCK + CONV_BLOCK - 1) / CONV_BLOCK : 0;
    c->head = calloc(c->head_count, sizeof *c->head); c->head_history = calloc(c->head_count, sizeof *c->head_history);
    c->head_fwd = calloc(c->head_count, sizeof *c->head_fwd); c->win = calloc(2 * c->head_count, sizeof *c->win);
    if (c->partition_count) {
        c->input_block = calloc(CONV_BLOCK, sizeof *c->input_block); c->tail_output = calloc(CONV_BLOCK, sizeof *c->tail_output);
        c->overlap = calloc(CONV_BLOCK, sizeof *c->overlap); c->work = calloc(fft_size, sizeof *c->work);
        c->ir_spectra = calloc(c->partition_count * fft_size, sizeof *c->ir_spectra);
        c->input_spectra = calloc(c->partition_count * fft_size, sizeof *c->input_spectra);
    }
    if (!c->head || !c->head_history || !c->head_fwd || !c->win ||
        (c->partition_count && (!c->input_block || !c->tail_output || !c->overlap || !c->work || !c->ir_spectra || !c->input_spectra))) return -1;
    memcpy(c->head, taps, c->head_count * sizeof *taps);
    /* head_fwd is the time-reversed head so the per-sample window sum walks
     * both buffers in ascending order (vectorizer-friendly dot product). */
    for (size_t i = 0; i < c->head_count; i++) c->head_fwd[i] = c->head[c->head_count - 1U - i];
    for (size_t p = 0; p < c->partition_count; p++) {
        complex_value *spectrum = c->ir_spectra + p * fft_size;
        size_t offset = CONV_BLOCK + p * CONV_BLOCK;
        size_t length = count - offset < CONV_BLOCK ? count - offset : CONV_BLOCK;
        for (size_t i = 0; i < length; i++) spectrum[i].re = taps[offset + i];
        fft(d, spectrum, 0);
    }
    return 0;
}

/* Independent bins, disjoint spectra: expose aliasing guarantees to the
 * vectorizer without changing partition order or float rounding. */
static void accumulate_convolution_spectrum(const complex_value *restrict x,
                                             const complex_value *restrict h,
                                             complex_value *restrict work, size_t count) {
    for (size_t i = 0; i < count; i++) {
        work[i].re += x[i].re * h[i].re - x[i].im * h[i].im;
        work[i].im += x[i].re * h[i].im + x[i].im * h[i].re;
    }
}

static float convolve(fxdsp *d, convolution *c, float input) {
    const size_t h = c->head_count;
    if (!h || !c->win || !c->head_fwd) return 0.0f;
    /* Two-copy linear window: the newest sample is stored at head_pos and
     * head_pos + h, so after the index wrap the last h inputs are
     * win[head_pos .. head_pos + h - 1] in ascending order.  The window sum
     * uses four independent accumulators (the serial single-accumulator
     * double chain dominated the ARM64 profile) and walks both buffers
     * ascending, which GCC can vectorize; products are rounded to float
     * exactly like the original loop, only the summation order differs. */
    c->win[c->head_pos] = input;
    c->win[c->head_pos + h] = input;
    c->head_pos = (c->head_pos + 1) % h;
    const float *restrict w = c->win + c->head_pos;
    const float *restrict taps = c->head_fwd;
    double a0 = 0.0, a1 = 0.0, a2 = 0.0, a3 = 0.0;
    size_t m = 0;
    for (; m + 4 <= h; m += 4) {
        a0 += (double)(taps[m] * w[m]);
        a1 += (double)(taps[m + 1] * w[m + 1]);
        a2 += (double)(taps[m + 2] * w[m + 2]);
        a3 += (double)(taps[m + 3] * w[m + 3]);
    }
    double direct = (a0 + a1) + (a2 + a3);
    for (; m < h; m++) direct += (double)(taps[m] * w[m]);
    if (!c->partition_count) return (float)direct;
    float tail = c->tail_output[c->input_pos];
    c->input_block[c->input_pos++] = input;
    if (c->input_pos == CONV_BLOCK) {
        const size_t fft_size = CONV_BLOCK * 2;
        c->spectrum_pos = (c->spectrum_pos + 1) % c->partition_count;
        complex_value *current = c->input_spectra + c->spectrum_pos * fft_size;
        memset(current, 0, fft_size * sizeof *current);
        for (size_t i = 0; i < CONV_BLOCK; i++) current[i].re = c->input_block[i];
        fft(d, current, 0); memset(c->work, 0, fft_size * sizeof *c->work);
        for (size_t p = 0; p < c->partition_count; p++) {
            size_t x_index = (c->spectrum_pos + c->partition_count - p) % c->partition_count;
            complex_value *x = c->input_spectra + x_index * fft_size, *h = c->ir_spectra + p * fft_size;
            accumulate_convolution_spectrum(x, h, c->work, fft_size);
        }
        fft(d, c->work, 1);
        for (size_t i = 0; i < CONV_BLOCK; i++) { c->tail_output[i] = c->work[i].re + c->overlap[i]; c->overlap[i] = c->work[i + CONV_BLOCK].re; }
        c->input_pos = 0;
    }
    return (float)direct + tail;
}

static int design(biquad *b, const char *type, float rate, float frequency, float q, float gain_db) {
    if (!(frequency > 0 && frequency < rate * .5f && q > 0)) return -1;
    if (!isfinite(gain_db) || gain_db < PEQ_GAIN_DB_MIN || gain_db > PEQ_GAIN_DB_MAX) return -1;
    double a = pow(10.0, gain_db / 40.0), w = 2.0 * PI * frequency / rate;
    double cs = cos(w), sn = sin(w), alpha = sn / (2.0 * q), beta = 2.0 * sqrt(a) * alpha;
    double b0, b1, b2, a0, a1, a2;
    if (!strcmp(type, "bell")) { b0=1+alpha*a; b1=-2*cs; b2=1-alpha*a; a0=1+alpha/a; a1=-2*cs; a2=1-alpha/a; }
    else if (!strcmp(type, "notch")) { b0=1; b1=-2*cs; b2=1; a0=1+alpha; a1=-2*cs; a2=1-alpha; }
    else if (!strcmp(type, "lowpass")) { b0=(1-cs)/2; b1=1-cs; b2=(1-cs)/2; a0=1+alpha; a1=-2*cs; a2=1-alpha; }
    else if (!strcmp(type, "highpass")) { b0=(1+cs)/2; b1=-(1+cs); b2=(1+cs)/2; a0=1+alpha; a1=-2*cs; a2=1-alpha; }
    else if (!strcmp(type, "lowshelf")) { b0=a*((a+1)-(a-1)*cs+beta); b1=2*a*((a-1)-(a+1)*cs); b2=a*((a+1)-(a-1)*cs-beta); a0=(a+1)+(a-1)*cs+beta; a1=-2*((a-1)+(a+1)*cs); a2=(a+1)+(a-1)*cs-beta; }
    else if (!strcmp(type, "highshelf")) { b0=a*((a+1)+(a-1)*cs+beta); b1=-2*a*((a-1)+(a+1)*cs); b2=a*((a+1)+(a-1)*cs-beta); a0=(a+1)-(a-1)*cs+beta; a1=2*((a-1)-(a+1)*cs); a2=(a+1)-(a-1)*cs-beta; }
    else return -1;
    if (!isfinite(b0) || !isfinite(b1) || !isfinite(b2) || !isfinite(a0) || !isfinite(a1) || !isfinite(a2) || a0 == 0.0) return -1;
    b->b0=b0/a0; b->b1=b1/a0; b->b2=b2/a0; b->a1=a1/a0; b->a2=a2/a0; b->z1=b->z2=0; return 0;
}

static float run_biquad(biquad *b, float value) {
    float output = value * b->b0 + b->z1;
    b->z1 = value * b->b1 - b->a1 * output + b->z2;
    b->z2 = value * b->b2 - b->a2 * output;
    /* Idle states stall in denormals, which slows every sample on x86. */
    if (fabsf(b->z1) < 1e-30f && fabsf(b->z2) < 1e-30f) b->z1 = b->z2 = 0.0f;
    return output;
}

static int sos_stable(float a1, float a2) {
    /* Poles of 1 + a1 z^-1 + a2 z^-2 strictly inside the unit circle. */
    return fabsf(a2) < 1.0f && fabsf(a1) < 1.0f + a2;
}

static char *trim_value(char *value) {
    char *end;
    while (*value == ' ' || *value == '\t') value++;
    end = value + strlen(value);
    while (end > value && (end[-1] == ' ' || end[-1] == '\t' || end[-1] == '\r' || end[-1] == '\n')) end--;
    *end = '\0';
    if (*value == '"' && end > value + 1 && end[-1] == '"') { value++; end[-1] = '\0'; }
    return value;
}

_Static_assert(FX_UPMIX_FAST_CHANNELS == FX_UPMIX_HQ_CHANNELS, "both upmixer engines take the same eight positions");

static void upmixer_apply_strength(const dsp_stage *stage) {
    if (stage->upmixer_hq) fx_upmix_hq_set_strength(stage->upmixer_hq, stage->upmix_center, stage->upmix_surround);
    else fx_upmix_fast_set_strength(stage->upmixer, stage->upmix_center, stage->upmix_surround);
}

/* A bypassed chain is not fed. The upmixers hold up to a transform of past input, so releasing the
 * bypass must start them empty instead of replaying what they held when it began. */
static void upmixers_reset(fxdsp *d) {
    for (unsigned index = 0; index < d->stage_count; index++) {
        dsp_stage *stage = &d->stages[index];
        if (stage->kind != STAGE_UPMIXER) continue;
        if (stage->upmixer_hq) fx_upmix_hq_reset(stage->upmixer_hq);
        else fx_upmix_fast_reset(stage->upmixer);
    }
}

static int parse_float_value(char *text, float *result) {
    char *end;
    float value = strtof(text, &end);
    while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') end++;
    if (!*text || *end || !isfinite(value)) return -1;
    *result = value;
    return 0;
}

static int stage_delay_is_peq_delay(const dsp_stage *stage) {
    size_t id_length = strlen(stage->id);
    return id_length >= 6U && !strcmp(stage->id + id_length - 6U, "-delay");
}

static float stage_delay_limit(const dsp_stage *stage) {
    return stage_delay_is_peq_delay(stage) ? PEQ_STAGE_DELAY_MAX_MS : STAGE_DELAY_MAX_MS;
}

static int set_native_param(dsp_stage *stage, const char *key, char *raw) {
    char *value = trim_value(raw);
    float number;
    if (stage->kind == STAGE_CONVOLVER) {
        if (!strcmp(key, "path") && *value) { snprintf(stage->argument, sizeof stage->argument, "%s", value); return 0; }
        if (parse_float_value(value, &number)) return -1;
        if (!strcmp(key, "wet_db")) stage->wet = powf(10.0f, number / 20.0f);
        else if (!strcmp(key, "dry_db")) stage->dry = powf(10.0f, number / 20.0f);
        else if (!strcmp(key, "input_gain_db")) stage->input_gain = powf(10.0f, number / 20.0f);
        else if (!strcmp(key, "output_gain_db")) stage->output_gain = powf(10.0f, number / 20.0f);
        else return -1;
        return 0;
    }
    if (stage->kind == STAGE_DELAY) {
        if (parse_float_value(value, &number) || number < 0.0f ||
            number > stage_delay_limit(stage)) return -1;
        if (!strcmp(key, "left_ms")) stage->left_ms = number;
        else if (!strcmp(key, "right_ms")) stage->right_ms = number;
        else return -1;
        return 0;
    }
    if (stage->kind == STAGE_HEADROOM) {
        if (strcmp(key, "gain_db") || parse_float_value(value, &number)) return -1;
        stage->gain = powf(10.0f, number / 20.0f); return 0;
    }
    if (stage->kind == STAGE_MASTER_GAIN) {
        if (strcmp(key, "gain_db") || parse_float_value(value, &number)) return -1;
        stage->gain = powf(10.0f, number / 20.0f); return 0;
    }
    if (stage->kind == STAGE_CRYSTALIZER) {
        if (strcmp(key, "intensity_band2_db") || parse_float_value(value, &number) ||
            !fx_crystalizer_intensity_db_valid(number)) return -1;
        stage->intensity_db = number; return 0;
    }
    if (stage->kind == STAGE_UPMIXER) {
        if (!strcmp(key, "mode")) {
            if (!strcmp(value, "fast")) stage->upmix_hq = 0U;
            else if (!strcmp(value, "hq")) stage->upmix_hq = 1U;
            else return -1;
            return 0;
        }
        if (parse_float_value(value, &number)) return -1;
        if (!strcmp(key, "center") && fx_upmix_strength_valid(number)) stage->upmix_center = number;
        else if (!strcmp(key, "surround") && fx_upmix_strength_valid(number)) stage->upmix_surround = number;
        else if (!strcmp(key, "fft_size") && number == floorf(number) && fx_upmix_hq_fft_size_valid((unsigned)number)) stage->upmix_fft_size = (unsigned)number;
        else if (!strcmp(key, "center_out") && (number == 0.0f || number == 1.0f)) stage->upmix_center_out = (unsigned)number;
        else if (!strcmp(key, "side_out") && (number == 0.0f || number == 1.0f)) stage->upmix_side_out = (unsigned)number;
        else if (!strcmp(key, "rear_out") && (number == 0.0f || number == 1.0f)) stage->upmix_rear_out = (unsigned)number;
        else return -1;
        return 0;
    }
    if (stage->kind == STAGE_AUTOGAIN) {
        if (!strcmp(key, "reference")) {
            if (!strcmp(value, "Momentary")) stage->reference = FX_AUTOGAIN_MOMENTARY;
            else if (!strcmp(value, "Shortterm")) stage->reference = FX_AUTOGAIN_SHORTTERM;
            else if (!strcmp(value, "Integrated")) stage->reference = FX_AUTOGAIN_INTEGRATED;
            else if (!strcmp(value, "Geometric Mean (MSI)")) stage->reference = FX_AUTOGAIN_GEOMETRIC_MEAN_MSI;
            else if (!strcmp(value, "Geometric Mean (MS)")) stage->reference = FX_AUTOGAIN_GEOMETRIC_MEAN_MS;
            else if (!strcmp(value, "Geometric Mean (MI)")) stage->reference = FX_AUTOGAIN_GEOMETRIC_MEAN_MI;
            else if (!strcmp(value, "Geometric Mean (SI)")) stage->reference = FX_AUTOGAIN_GEOMETRIC_MEAN_SI;
            else return -1;
            return 0;
        }
        if (parse_float_value(value, &number)) return -1;
        if (!strcmp(key, "target_db")) stage->target_db = number;
        else if (!strcmp(key, "silence_threshold_db")) stage->silence_db = number;
        else if (!strcmp(key, "maximum_history_seconds") && number >= 6.0f && number <= 3600.0f && number == floorf(number)) stage->history_seconds = (unsigned)number;
        else return -1;
        return 0;
    }
    return -1;
}

static int initialize_stage(fxdsp *d, dsp_stage *stage, char *error, size_t error_size) {
    unsigned channel;
    if (stage->kind == STAGE_CONVOLVER) {
        if (!stage->argument[0]) { fail(error, error_size, "convolver stage requires path"); return -1; }
        for (channel = 0; channel < d->inputs; channel++) {
            float *taps = NULL; size_t count = 0;
            if (load_wav(stage->argument, channel_side(d, channel), &taps, &count, d->rate) &&
                load_wav(stage->argument, 0, &taps, &count, d->rate)) {
                fail(error, error_size, "cannot load convolver path"); return -1;
            }
            if (prepare_convolution(d, &stage->conv[channel], taps, count)) {
                free(taps); fail(error, error_size, "out of memory"); return -1;
            }
            free(taps);
        }
    } else if (stage->kind == STAGE_DELAY) {
        float capacity_ms = stage_delay_limit(stage);
        for (channel = 0; channel < d->inputs; channel++) {
            float milliseconds = channel_side(d, channel) ? stage->right_ms : stage->left_ms;
            stage->delay[channel] = (size_t)llround(milliseconds * d->rate / 1000.0f);
            stage->delay_size[channel] = (size_t)llround(capacity_ms * d->rate / 1000.0f) + 1U;
            stage->delay_line[channel] = calloc(stage->delay_size[channel], sizeof(float));
            if (!stage->delay_line[channel]) { fail(error, error_size, "out of memory"); return -1; }
        }
    } else if (stage->kind == STAGE_AUTOGAIN) {
        fx_autogain_config config = fx_autogain_default_config();
        config.reference = stage->reference; config.target_lufs = stage->target_db;
        config.silence_threshold_lufs = stage->silence_db;
        config.maximum_history_seconds = stage->history_seconds;
        if (d->inputs & 1U) { fail(error, error_size, "autogain requires an even stereo input count"); return -1; }
        for (channel = 0; channel < effect_pairs(d); channel++) {
            stage->autogain[channel] = fx_autogain_init(d->rate, PROCESS_BLOCK, &config);
            if (!stage->autogain[channel]) { fail(error, error_size, "cannot initialize autogain"); return -1; }
        }
    } else if (stage->kind == STAGE_CRYSTALIZER) {
        for (channel = 0; channel < d->inputs; channel++) {
            stage->crystalizer[channel] = fx_crystalizer_create(d->rate);
            if (!stage->crystalizer[channel]) { fail(error, error_size, "cannot initialize crystalizer"); return -1; }
            fx_crystalizer_set_band_intensity_db(stage->crystalizer[channel], 2U,
                                                  stage->intensity_db);
        }
    } else if (stage->kind == STAGE_UPMIXER) {
        if (!d->surround) { fail(error, error_size, "upmixer requires Surround"); return -1; }
        if (stage->upmix_hq) {
            stage->upmixer_hq = fx_upmix_hq_create(d->rate, stage->upmix_fft_size, stage->upmix_center_out, stage->upmix_side_out, stage->upmix_rear_out);
            if (!stage->upmixer_hq) { fail(error, error_size, "cannot initialize upmixer"); return -1; }
        } else {
            stage->upmixer = fx_upmix_fast_create(d->rate, stage->upmix_center_out, stage->upmix_side_out, stage->upmix_rear_out);
            if (!stage->upmixer) { fail(error, error_size, "cannot initialize upmixer"); return -1; }
        }
        upmixer_apply_strength(stage);
    } else if (stage->kind == STAGE_LV2) {
        if (d->inputs & 1U) { fail(error, error_size, "LV2 stages require an even stereo input count"); return -1; }
        for (channel = 0; channel < effect_pairs(d); channel++) {
            stage->lv2[channel] = fx_lv2_host_new(stage->argument, d->rate, PROCESS_BLOCK, error, error_size);
            if (!stage->lv2[channel]) return -1;
            for (unsigned control = 0; control < stage->control_count; control++) {
                const char *symbol = stage->controls[control].symbol;
                float value = stage->controls[control].value;
                if (!fx_lv2_host_control_value_declared(stage->lv2[channel], symbol, value)) {
                    /* A plugin clamps an undeclared enumeration value to another
                     * setting (e.g. a filter mode an older LSP lacks): refuse. */
                    if (error && error_size)
                        snprintf(error, error_size, "LV2 plugin %s does not declare value %g for control %s "
                                 "(installed plugin too old or incompatible)", stage->argument, (double)value, symbol);
                    return -1;
                }
                if (!fx_lv2_host_set_control(stage->lv2[channel], symbol, value)) {
                    fail(error, error_size, "unknown LV2 control symbol"); return -1;
                }
            }
            fx_lv2_host_activate(stage->lv2[channel]);
        }
        /* Derive the FFT overlap timing for the matched-latency loudness
         * compensation from the plugin rank (fft 0..6 -> rank 8..14 -> FFT
         * size 256..16384).  The spectral processor uses a Hann (cosine)
         * window with 50% overlap, so the curve change reaches the output
         * at the next frame boundary and crossfades over half the window. */
        unsigned fft_index = 4U;
        for (unsigned control = 0; control < stage->control_count; control++)
            if (!strcmp(stage->controls[control].symbol, "fft"))
                fft_index = (unsigned)stage->controls[control].value;
        if (fft_index > 6U) fft_index = 4U;
        unsigned rank = 8U + fft_index;
        stage->lv2_frame_size = 1U << (rank - 1U);
        stage->lv2_buf_size = 1U << rank;
        stage->lv2_frames_fed = 0;
        stage->lv2_comp_transition = 0;
    }
    return 0;
}

fxdsp *fxdsp_load(const char *path, char *error, size_t error_size) {
    FILE *file = fopen(path, "r");
    fxdsp *d = calloc(1, sizeof *d);
    char line[MAX_LINE], arg[256], backend[1024], type[32], extra;
    unsigned line_no = 0, out, in, group;
    dsp_stage *current = NULL;
    int stage_section = 0, post_section = 0;
    if (!file || !d) { fail(error,error_size,"cannot open config"); if(file)fclose(file); free(d); return NULL; }
    d->rate=48000;
    atomic_store_explicit(&d->output_gain_bits,float_bits(1.0f),memory_order_relaxed);
    for (unsigned i=0;i<FXDSP_MAX_CHANNELS;i++) { d->out[i].gain=1; d->out[i].polarity=1; }
    while (fgets(line,sizeof line,file)) {
        line_no++; char *p=line; while(*p==' '||*p=='\t')p++; if(*p=='#'||*p=='\n'||!*p)continue;
        if (current) {
            char key[128], raw[MAX_LINE]; float value;
            if (sscanf(p, "stage_end %c", &extra) == EOF) current = NULL;
            else if (current->kind == STAGE_LV2 && sscanf(p, "control %127s %f %c", key, &value, &extra) == 2 &&
                     current->control_count < MAX_CONTROLS && isfinite(value)) {
                stage_control *control = &current->controls[current->control_count++];
                snprintf(control->symbol, sizeof control->symbol, "%s", key); control->value = value;
            } else if (current->kind == STAGE_LV2 && sscanf(p, "param %127s %4095[^\n]", key, raw) == 2 &&
                       !strcmp(key, "output_gain_db") && !parse_float_value(trim_value(raw), &value)) {
                current->output_gain = powf(10.0f, value / 20.0f);
            } else if (current->kind != STAGE_LV2 && sscanf(p, "param %127s %4095[^\n]", key, raw) == 2 &&
                       !set_native_param(current, key, raw)) {}
            else goto invalid;
            continue;
        }
        if (!post_section && sscanf(p, "stage_begin %u %255s %31s %1023s %c", &out, arg, type,
                                    backend, &extra) == 4) {
            char *kind = backend;
            if (out != d->stage_count || d->stage_count >= MAX_STAGES) goto invalid;
            stage_section = 1; current = &d->stages[d->stage_count++];
            snprintf(current->id, sizeof current->id, "%s", arg);
            current->wet = current->input_gain = current->output_gain = current->gain = 1.0f;
            current->dry = powf(10.0f, -5.0f); current->target_db = -12.0f;
            current->silence_db = -70.0f; current->history_seconds = 15U;
            current->reference = FX_AUTOGAIN_GEOMETRIC_MEAN_MSI; current->intensity_db = -2.0f;
            current->upmix_center = current->upmix_surround = 6.0f; current->upmix_fft_size = 2048U;
            if (!strcmp(type, "lv2")) { current->kind = STAGE_LV2; snprintf(current->argument, sizeof current->argument, "%s", kind); }
            else if (!strcmp(type, "native") && !strcmp(kind, "convolver")) current->kind = STAGE_CONVOLVER;
            else if (!strcmp(type, "native") && !strcmp(kind, "delay")) current->kind = STAGE_DELAY;
            else if (!strcmp(type, "native") && !strcmp(kind, "headroom")) current->kind = STAGE_HEADROOM;
            else if (!strcmp(type, "native") && !strcmp(kind, "master_gain")) current->kind = STAGE_MASTER_GAIN;
            else if (!strcmp(type, "native") && !strcmp(kind, "autogain")) current->kind = STAGE_AUTOGAIN;
            else if (!strcmp(type, "native") && !strcmp(kind, "crystalizer")) current->kind = STAGE_CRYSTALIZER;
            else if (!strcmp(type, "native") && !strcmp(kind, "upmixer")) current->kind = STAGE_UPMIXER;
            else goto invalid;
        } else { float x,y,z,route_gain; int enabled; float b0,b1,b2,a1,a2; int consumed;
            if (!stage_section && !post_section && sscanf(p,"rate %u %c",&d->rate,&extra)==1) {}
            else if (!stage_section && !post_section && sscanf(p,"inputs %u %c",&d->inputs,&extra)==1) {}
            else if (!stage_section && !post_section && sscanf(p,"surround %u %c",&d->surround,&extra)==1 && d->surround==1U) {}
            else if (!stage_section && !post_section && sscanf(p,"outputs %u %c",&d->outputs,&extra)==1) {}
            else if (!stage_section && !post_section && sscanf(p,"matrix %u %u %f %c",&out,&in,&route_gain,&extra)==3 && d->route_count<MAX_ROUTES) { d->routes[d->route_count].out=out; d->routes[d->route_count].in=in; d->routes[d->route_count++].gain=route_gain; }
            else if (!stage_section && !post_section && sscanf(p,"postmatrix %u %u %f %c",&out,&in,&route_gain,&extra)==3 && d->route_count<MAX_ROUTES && isfinite(route_gain)) { route *r=&d->routes[d->route_count++]; r->out=out; r->in=in; r->gain=route_gain; r->post_crossover=1; }
            else if (!stage_section && !post_section && sscanf(p,"xsos %u %f %f %f %f %f %c",&out,&b0,&b1,&b2,&a1,&a2,&extra)==6 && out<FXDSP_MAX_CHANNELS && d->out[out].crossover_filter_count<MAX_BIQUADS && isfinite(b0) && isfinite(b1) && isfinite(b2) && isfinite(a1) && isfinite(a2) && sos_stable(a1,a2)) { d->out[out].crossover_group[d->out[out].crossover_filter_count]=0; biquad *target=&d->out[out].crossover_filters[d->out[out].crossover_filter_count++]; target->b0=b0; target->b1=b1; target->b2=b2; target->a1=a1; target->a2=a2; }
            else if (!stage_section && !post_section && sscanf(p,"gmatrix %u %u %u %f %c",&out,&group,&in,&route_gain,&extra)==4 && group>0U && group<FXDSP_XO_GROUPS && out<FXDSP_MAX_CHANNELS && d->route_count<MAX_ROUTES && isfinite(route_gain)) { route *r=&d->routes[d->route_count++]; r->out=out; r->in=in; r->gain=route_gain; r->group=group; if (d->out[out].crossover_groups<=group) d->out[out].crossover_groups=group+1U; }
            else if (!stage_section && !post_section && sscanf(p,"gxsos %u %u %f %f %f %f %f %c",&out,&group,&b0,&b1,&b2,&a1,&a2,&extra)==7 && group>0U && group<FXDSP_XO_GROUPS && out<FXDSP_MAX_CHANNELS && d->out[out].crossover_filter_count<MAX_BIQUADS && isfinite(b0) && isfinite(b1) && isfinite(b2) && isfinite(a1) && isfinite(a2) && sos_stable(a1,a2)) { d->out[out].crossover_group[d->out[out].crossover_filter_count]=(unsigned char)group; biquad *target=&d->out[out].crossover_filters[d->out[out].crossover_filter_count++]; target->b0=b0; target->b1=b1; target->b2=b2; target->a1=a1; target->a2=a2; if (d->out[out].crossover_groups<=group) d->out[out].crossover_groups=group+1U; }
            else if (!stage_section && !post_section && sscanf(p,"peq %u %31s %f %f %f %c",&out,type,&x,&y,&z,&extra)==5 && out<FXDSP_MAX_CHANNELS && d->out[out].filter_count<MAX_BIQUADS && !design(&d->out[out].filters[d->out[out].filter_count],type,d->rate,x,y,z)) d->out[out].filter_count++;
            else if (!stage_section && !post_section && sscanf(p,"sos %u %f %f %f %f %f %c",&out,&b0,&b1,&b2,&a1,&a2,&extra)==6 && out<FXDSP_MAX_CHANNELS && d->out[out].filter_count<MAX_BIQUADS && isfinite(b0) && isfinite(b1) && isfinite(b2) && isfinite(a1) && isfinite(a2) && sos_stable(a1,a2)) { biquad *target=&d->out[out].filters[d->out[out].filter_count++]; target->b0=b0; target->b1=b1; target->b2=b2; target->a1=a1; target->a2=a2; target->z1=target->z2=0.0f; }
            else if (!stage_section && !post_section && sscanf(p,"oconv %u %u %f %f %f %f %n",&out,&in,&x,&y,&z,&route_gain,&consumed)==6 && out<FXDSP_MAX_CHANNELS && !d->out[out].has_conv) {
                char *conv_path = trim_value(p + consumed);
                output_state *conv_state = &d->out[out];
                if (!*conv_path || !isfinite(x) || !isfinite(y) || !isfinite(z) || !isfinite(route_gain) || strlen(conv_path) >= sizeof conv_state->conv_path) goto invalid;
                snprintf(conv_state->conv_path, sizeof conv_state->conv_path, "%s", conv_path);
                conv_state->conv_channel = in;
                conv_state->conv_wet = powf(10.0f, x / 20.0f); conv_state->conv_dry = powf(10.0f, y / 20.0f);
                conv_state->conv_in_gain = powf(10.0f, z / 20.0f); conv_state->conv_out_gain = powf(10.0f, route_gain / 20.0f);
                conv_state->has_conv = 1;
            }
            else if (sscanf(p,"output %u %f %f %31s %c",&out,&x,&y,arg,&extra)==4 && out<FXDSP_MAX_CHANNELS && isfinite(x) && isfinite(y) && y>=0.0f && y<=OUTPUT_DELAY_MAX_MS && (!strcmp(arg,"normal") || !strcmp(arg,"invert"))) { post_section=1; d->out[out].gain=powf(10,x/20); d->out[out].delay=(size_t)llround(y*d->rate/1000); d->out[out].polarity=!strcmp(arg,"invert")?-1:1; }
            else if (post_section && sscanf(p,"bypass %d %c",&enabled,&extra)==1) atomic_store_explicit(&d->effect_bypass,!!enabled,memory_order_relaxed);
            else { invalid: snprintf(line,sizeof line,"invalid config line %u",line_no); fail(error,error_size,line); goto bad; }
        }
    }
    if (current) goto invalid;
    fclose(file); file=NULL;
    if (!d->rate || d->inputs<1 || d->inputs>32 || d->outputs<1 || d->outputs>32) { fail(error,error_size,"inputs/outputs must be 1..32"); goto bad; }
    if (d->surround && d->inputs != 8U) { fail(error,error_size,"Surround requires eight positioned inputs"); goto bad; }
    for (unsigned o=0;o<d->outputs;o++) if(d->out[o].filter_count+d->out[o].crossover_filter_count>MAX_BIQUADS) { fail(error,error_size,"output exceeds biquad budget"); goto bad; }
    for(unsigned r=0;r<d->route_count;r++) if(d->routes[r].in>=d->inputs||d->routes[r].out>=d->outputs){fail(error,error_size,"matrix index out of range");goto bad;}
    {
        unsigned tally[FXDSP_MAX_CHANNELS] = {0};
        unsigned cursor[FXDSP_MAX_CHANNELS];
        for (unsigned r = 0; r < d->route_count; r++) tally[d->routes[r].out]++;
        d->route_start[0] = 0;
        for (unsigned o = 0; o < d->outputs; o++) d->route_start[o + 1] = d->route_start[o] + tally[o];
        for (unsigned o = 0; o < d->outputs; o++) cursor[o] = d->route_start[o];
        for (unsigned r = 0; r < d->route_count; r++) {
            unsigned o = d->routes[r].out;
            d->route_order[cursor[o]++] = r;
        }
    }
    d->fft_reverse=calloc(CONV_BLOCK*2,sizeof *d->fft_reverse);d->fft_roots=calloc(CONV_BLOCK,sizeof *d->fft_roots);
    if(!d->fft_reverse||!d->fft_roots){fail(error,error_size,"out of memory");goto bad;}
    for(unsigned i=0;i<CONV_BLOCK*2;i++){unsigned value=i,reversed=0;for(unsigned bit=0;bit<9;bit++){reversed=(reversed<<1)|(value&1);value>>=1;}d->fft_reverse[i]=reversed;}
    for(unsigned i=0;i<CONV_BLOCK;i++){double angle=-2*PI*i/(CONV_BLOCK*2);d->fft_roots[i].re=cos(angle);d->fft_roots[i].im=sin(angle);}
    for(out=0;out<d->outputs;out++) { output_state *s=&d->out[out]; s->delay_size=(size_t)llround((double)OUTPUT_DELAY_MAX_MS*d->rate/1000.0)+1U; s->delay_line=calloc(s->delay_size,sizeof(float)); if(!s->delay_line){fail(error,error_size,"out of memory");goto bad;} }
    for(out=0;out<d->outputs;out++) {
        output_state *s=&d->out[out];
        if(!s->has_conv) continue;
        float *taps=NULL; size_t count=0;
        if(load_wav(s->conv_path,s->conv_channel,&taps,&count,d->rate)||prepare_convolution(d,&s->conv,taps,count)) {
            free(taps); fail(error,error_size,"cannot load output convolver path"); goto bad;
        }
        free(taps);
    }
    for(out=0;out<(d->inputs>d->outputs?d->inputs:d->outputs);out++) for(unsigned b=0;b<2;b++){d->scratch[b][out]=calloc(PROCESS_BLOCK,sizeof(float));if(!d->scratch[b][out]){fail(error,error_size,"out of memory");goto bad;}}
    for (unsigned stage = 0; stage < d->stage_count; stage++) if (initialize_stage(d, &d->stages[stage], error, error_size)) goto bad;
    return d;
bad:
    if (file) fclose(file);
    fxdsp_free(d);
    return NULL;
}

static void free_convolution(convolution *c) {
    free(c->head); free(c->head_history); free(c->head_fwd); free(c->win);
    free(c->input_block); free(c->tail_output);
    free(c->overlap); free(c->ir_spectra); free(c->input_spectra); free(c->work);
}

void fxdsp_free(fxdsp *d) {
    if(!d)return;
    fx_workers_destroy(d->owned_workers);
    for(unsigned i=0;i<FXDSP_MAX_CHANNELS;i++) {
        free(d->out[i].delay_line); free(d->scratch[0][i]); free(d->scratch[1][i]);
        free_convolution(&d->out[i].conv);
    }
    for(unsigned index=0;index<d->stage_count;index++) {
        dsp_stage *stage=&d->stages[index];
        for(unsigned channel=0;channel<FXDSP_MAX_CHANNELS;channel++) {
            free_convolution(&stage->conv[channel]); free(stage->delay_line[channel]);
            fx_crystalizer_destroy(stage->crystalizer[channel]);
        }
        fx_upmix_fast_destroy(stage->upmixer);
        fx_upmix_hq_destroy(stage->upmixer_hq);
        for(unsigned pair=0;pair<FXDSP_MAX_CHANNELS/2;pair++) {
            fx_autogain_free(stage->autogain[pair]); fx_lv2_host_free(stage->lv2[pair]);
        }
    }
    free(d->fft_reverse);free(d->fft_roots);free(d);
}
unsigned fxdsp_inputs(const fxdsp*d){return d->inputs;} unsigned fxdsp_outputs(const fxdsp*d){return d->outputs;} unsigned fxdsp_rate(const fxdsp*d){return d->rate;}

/* Graph side: how far one dispatch spreads over jobs.  Published with the
 * per-graph parallel-output selection, which the callback reads, so this runs
 * on a stopped graph only. */
static unsigned plan_workers(fxdsp *d) {
    unsigned jobs = 1;
    d->parallel_outputs = 0;
    for (unsigned s = 0; s < d->stage_count; s++) {
        stage_kind kind = d->stages[s].kind;
        unsigned count = kind == STAGE_CONVOLVER || kind == STAGE_CRYSTALIZER ? d->inputs : 1;
        if (count > jobs) jobs = count;
    }
    for (unsigned o = 0; o < d->outputs; o++)
        if (d->out[o].has_conv || d->out[o].filter_count) d->parallel_outputs = 1;
    if (d->parallel_outputs && d->outputs > jobs) jobs = d->outputs;
    d->worker_demand = jobs;
    return jobs;
}

unsigned fxdsp_worker_demand(const fxdsp *d) { return d->worker_demand; }

/* FXROUTE_DSP_WORKERS caps the helper count.  Read by whoever builds the
 * pool; the live engine reads it once per process, not per graph load. */
static unsigned worker_env_limit(void) {
    const char *override = getenv("FXROUTE_DSP_WORKERS");
    if (!override) return FX_WORKER_LIMIT;
    char *end; unsigned long value = strtoul(override, &end, 10);
    if (!*override || *end || value > FX_WORKER_LIMIT) return 0;
    return (unsigned)value;
}

/* Owner side: helper capacity is a machine property, not a graph property.
 * Kept independent of the graph so one pool can serve every graph of a fixed
 * channel layout; a serial graph then leaves helpers idle instead of churning
 * create/admission/join work on every swap. */
static unsigned worker_capacity(unsigned jobs, unsigned limit) {
    if (!jobs || !limit) return 0;
    long cpus = sysconf(_SC_NPROCESSORS_ONLN);
#if defined(__linux__)
    cpu_set_t available;
    if (!sched_getaffinity(0, sizeof available, &available)) cpus = CPU_COUNT(&available);
#endif
    unsigned allowed = cpus > 1 ? (unsigned)(cpus - 1) : 0;
    if (allowed > limit) allowed = limit;
    if (allowed > FX_WORKER_LIMIT) allowed = FX_WORKER_LIMIT;
    return allowed < jobs - 1 ? allowed : jobs - 1;
}

static unsigned prepare_worker_limit(unsigned jobs, const fx_worker_policy *requested) {
    unsigned count = requested ? requested->count : FX_WORKER_LIMIT;
    unsigned limit = worker_env_limit();
    if (limit < count) count = limit;
    return worker_capacity(jobs, count);
}

static fx_workers *prepare_worker_pool(unsigned jobs, const fx_worker_policy *requested) {
    fx_worker_policy policy = {.count = FX_WORKER_LIMIT};
    if (requested) policy = *requested;
    policy.count = prepare_worker_limit(jobs, requested);
    return fx_workers_create(&policy);
}

unsigned fxdsp_prepare_workers(fxdsp *d, const fx_worker_policy *requested) {
    fx_workers_destroy(d->owned_workers);
    d->owned_workers = prepare_worker_pool(plan_workers(d), requested);
    d->workers = d->owned_workers;
    return fx_workers_count(d->workers);
}

fx_workers *fxdsp_create_engine_workers(const fxdsp *d, const fx_worker_policy *policy) {
    unsigned jobs = d->inputs > d->outputs ? d->inputs : d->outputs;
    return prepare_worker_pool(jobs, policy);
}

unsigned fxdsp_engine_worker_limit(const fxdsp *d, const fx_worker_policy *policy) {
    unsigned jobs = d->inputs > d->outputs ? d->inputs : d->outputs;
    return prepare_worker_limit(jobs, policy);
}

void fxdsp_borrow_workers(fxdsp *d, fx_workers *pool) {
    fx_workers_destroy(d->owned_workers); d->owned_workers = NULL;
    (void)plan_workers(d);
    d->workers = pool;
}

unsigned fxdsp_worker_count(const fxdsp *d) { return fx_workers_count(d->workers); }
int fxdsp_worker_scheduler(const fxdsp *d, int *priority) { return fx_workers_scheduler(d->workers, priority); }
const char *fxdsp_timing_name(unsigned metric) {
    static const char *const names[] = {
        [FXDSP_TIMING_CALLBACK] = "callback",
        [FXDSP_TIMING_BARRIER] = "barrier",
        [FXDSP_TIMING_WAKE] = "wake",
    };
    _Static_assert(sizeof names / sizeof names[0] == FXDSP_TIMING_COUNT, "timing name count");
    return metric < FXDSP_TIMING_COUNT ? names[metric] : NULL;
}
int fxdsp_enable_timing(fxdsp *d, int enabled) {
    d->measure = enabled && fx_timing_available(&d->timing[0]);
    return d->measure;
}
int fxdsp_timing_enabled(const fxdsp *d) { return d->measure; }
void fxdsp_begin_callback(fxdsp *d) {
    if (!atomic_load_explicit(&d->timing_reset, memory_order_acquire)) return;
    for (unsigned i = 0; i < FXDSP_TIMING_COUNT; i++) fx_timing_reset(&d->timing[i]);
    atomic_store_explicit(&d->timing_reset, 0, memory_order_release);
}
void fxdsp_record_callback(fxdsp *d, uint64_t ns) {
    if (d->measure) fx_timing_record(&d->timing[FXDSP_TIMING_CALLBACK], ns);
}
fx_timing_summary fxdsp_read_timing(const fxdsp *d, unsigned metric) {
    fx_timing_summary empty = {0};
    return metric < FXDSP_TIMING_COUNT ? fx_timing_read(&d->timing[metric]) : empty;
}
int fxdsp_reset_timing(fxdsp *d) {
    if (!d->measure) return 1;
    atomic_store_explicit(&d->timing_reset, 1, memory_order_release);
    uint64_t deadline = fx_workers_now_ns() + UINT64_C(250000000);
    while (atomic_load_explicit(&d->timing_reset, memory_order_acquire)) {
        if (fx_workers_now_ns() >= deadline) return 0;
        struct timespec pause = {.tv_nsec=1000000}; nanosleep(&pause, NULL);
    }
    return 1;
}

static void run_jobs(fxdsp *d, fx_workers *pool, unsigned jobs, void (*function)(void *, unsigned), void *context) {
    fx_worker_sample sample = fx_workers_run(pool, jobs, function, context, d->measure);
    if (!d->measure || !sample.wakes) return;
    fx_timing_record(&d->timing[FXDSP_TIMING_BARRIER], sample.barrier_ns);
    for (unsigned i = 0; i < sample.wakes; i++) fx_timing_record(&d->timing[FXDSP_TIMING_WAKE], sample.wake_ns[i]);
}

static dsp_stage *find_stage(fxdsp *d, const char *id) {
    for (unsigned i = 0; i < d->stage_count; i++)
        if (!strcmp(d->stages[i].id, id)) return &d->stages[i];
    return NULL;
}

static int live_add(fxdsp *d, const live_update *update) {
    unsigned count = atomic_load_explicit(&d->live_count, memory_order_relaxed);
    if (count >= MAX_LIVE_UPDATES || atomic_load_explicit(&d->live_commit, memory_order_acquire)) return 0;
    d->live[count] = *update;
    atomic_store_explicit(&d->live_count, count + 1U, memory_order_release);
    return 1;
}

int fxdsp_live_begin(fxdsp *d) {
    if (!d || atomic_load_explicit(&d->live_commit, memory_order_acquire)) return 0;
    atomic_store_explicit(&d->live_count, 0, memory_order_release);
    return 1;
}

int fxdsp_live_abort(fxdsp *d) {
    if (!d || atomic_load_explicit(&d->live_commit, memory_order_acquire)) return 0;
    atomic_store_explicit(&d->live_count, 0, memory_order_release);
    return 1;
}

int fxdsp_live_control(fxdsp *d, const char *stage_id, const char *symbol, float value) {
    dsp_stage *stage = d ? find_stage(d, stage_id) : NULL;
    live_update update = {.kind = LIVE_CONTROL, .values = {value}};
    if (!stage || stage->kind != STAGE_LV2 || !symbol || !isfinite(value)) return 0;
    int known = 0;
    for (unsigned i = 0; i < stage->control_count; i++)
        if (!strcmp(stage->controls[i].symbol, symbol)) known = 1;
    if (!known) return 0;
    snprintf(update.stage_id, sizeof update.stage_id, "%s", stage_id);
    snprintf(update.symbol, sizeof update.symbol, "%s", symbol);
    return live_add(d, &update);
}

int fxdsp_live_param(fxdsp *d, const char *stage_id, const char *key, float value) {
    dsp_stage *stage = d ? find_stage(d, stage_id) : NULL;
    live_update update = {.kind = LIVE_PARAM, .values = {value}};
    int known = 0;
    if (!stage || !key || !isfinite(value)) return 0;
    if (stage->kind == STAGE_LV2) known = !strcmp(key, "output_gain_db");
    else if (stage->kind == STAGE_CONVOLVER)
        known = !strcmp(key, "wet_db") || !strcmp(key, "dry_db") || !strcmp(key, "input_gain_db") || !strcmp(key, "output_gain_db");
    else if (stage->kind == STAGE_DELAY)
        known = (!strcmp(key, "left_ms") || !strcmp(key, "right_ms")) &&
            value >= 0.0f && value <= stage_delay_limit(stage);
    else if (stage->kind == STAGE_HEADROOM || stage->kind == STAGE_MASTER_GAIN) known = !strcmp(key, "gain_db");
    else if (stage->kind == STAGE_CRYSTALIZER)
        known = !strcmp(key, "intensity_band2_db") && fx_crystalizer_intensity_db_valid(value);
    else if (stage->kind == STAGE_AUTOGAIN) known = !strcmp(key, "target_db") || !strcmp(key, "silence_threshold_db");
    else if (stage->kind == STAGE_UPMIXER)
        known = (!strcmp(key, "center") || !strcmp(key, "surround")) && fx_upmix_strength_valid(value);
    if (!known) return 0;
    snprintf(update.stage_id, sizeof update.stage_id, "%s", stage_id);
    snprintf(update.symbol, sizeof update.symbol, "%s", key);
    if (stage->kind == STAGE_CONVOLVER || stage->kind == STAGE_LV2 ||
        stage->kind == STAGE_HEADROOM || stage->kind == STAGE_MASTER_GAIN) update.values[1] = powf(10.0f, value / 20.0f);
    if (stage->kind == STAGE_DELAY) update.values[1] = (float)llround(value * d->rate / 1000.0f);
    return live_add(d, &update);
}

int fxdsp_live_matrix(fxdsp *d, unsigned output, unsigned input, float gain) {
    live_update update = {.kind = LIVE_MATRIX, .first = output, .second = input, .values = {gain}};
    if (!d || output >= d->outputs || input >= d->inputs || !isfinite(gain)) return 0;
    for (unsigned i = 0; i < d->route_count; i++)
        if (d->routes[i].out == output && d->routes[i].in == input) return live_add(d, &update);
    return 0;
}

int fxdsp_live_peq(fxdsp *d, unsigned output, unsigned filter, const char *type,
                   float frequency, float q, float gain_db) {
    live_update update = {.kind = LIVE_PEQ, .first = output, .second = filter};
    biquad prepared = {0};
    if (!d || output >= d->outputs || filter >= d->out[output].filter_count || !type ||
        !isfinite(frequency) || !isfinite(q) || !isfinite(gain_db) ||
        (strcmp(type, "bell") && strcmp(type, "notch") && strcmp(type, "lowpass") &&
         strcmp(type, "highpass") && strcmp(type, "lowshelf") && strcmp(type, "highshelf"))) return 0;
    if (design(&prepared, type, (float)d->rate, frequency, q, gain_db)) return 0;
    update.values[0] = prepared.b0;
    update.values[1] = prepared.b1;
    update.values[2] = prepared.b2;
    update.values[3] = prepared.a1;
    update.values[4] = prepared.a2;
    return live_add(d, &update);
}

int fxdsp_live_output(fxdsp *d, unsigned output, float gain_db, float delay_ms, int invert) {
    live_update update = {.kind = LIVE_OUTPUT, .first = output, .values = {gain_db, delay_ms}, .invert = invert};
    if (!d || output >= d->outputs || !isfinite(gain_db) || !isfinite(delay_ms) || delay_ms < 0.0f || delay_ms > OUTPUT_DELAY_MAX_MS || (invert != 0 && invert != 1)) return 0;
    update.values[2] = powf(10.0f, gain_db / 20.0f);
    update.values[3] = (float)llround(delay_ms * d->rate / 1000.0f);
    return live_add(d, &update);
}

/* Inverse trim for one output sample of a matched-latency loudness
 * transition.  The plugin crossfades the work-point curve (its flat 1 kHz
 * component g) from g_old to g_new over one frame with the squared cosine
 * overlap weight sin^2(pi*d/buf_size); the compensation applies the
 * reciprocal so the Loudness+trim pair stays level-neutral at the
 * post-limiter meter tap on every sample of the transition (except for
 * genuine limiter capping, which is the visible post-limiter level). */
static float lv2_comp_gain(const dsp_stage *stage, size_t sample, unsigned channel) {
    size_t boundary = channel ? stage->lv2_comp_boundary_r : stage->lv2_comp_boundary_l;
    if (sample < boundary) return 1.0f / stage->lv2_comp_old_g;
    if (sample >= boundary + stage->lv2_frame_size) return 1.0f / stage->lv2_comp_new_g;
    float weight = sinf((float)PI * (float)(sample - boundary) / (float)stage->lv2_buf_size);
    weight *= weight;
    float curve = stage->lv2_comp_old_g * (1.0f - weight) + stage->lv2_comp_new_g * weight;
    return 1.0f / curve;
}

static void apply_live_updates(fxdsp *d) {
    if (!atomic_load_explicit(&d->live_commit, memory_order_acquire)) return;
    unsigned count = atomic_load_explicit(&d->live_count, memory_order_acquire);
    for (unsigned i = 0; i < count; i++) {
        live_update *update = &d->live[i];
        if (update->kind == LIVE_MATRIX) {
            for (unsigned route = 0; route < d->route_count; route++)
                if (d->routes[route].out == update->first && d->routes[route].in == update->second) d->routes[route].gain = update->values[0];
        } else if (update->kind == LIVE_OUTPUT) {
            output_state *state = &d->out[update->first];
            state->gain = update->values[2];
            state->delay = (size_t)update->values[3];
            if (state->delay >= state->delay_size) state->delay = state->delay_size - 1U;
            state->polarity = update->invert ? -1.0f : 1.0f;
        } else if (update->kind == LIVE_PEQ) {
            biquad *filter = &d->out[update->first].filters[update->second];
            float z1 = filter->z1, z2 = filter->z2;
            filter->b0 = update->values[0]; filter->b1 = update->values[1]; filter->b2 = update->values[2];
            filter->a1 = update->values[3]; filter->a2 = update->values[4];
            filter->z1 = z1; filter->z2 = z2;
        } else {
            dsp_stage *stage = find_stage(d, update->stage_id);
            if (!stage) continue;
            if (update->kind == LIVE_CONTROL) {
                for (unsigned pair = 0; pair < effect_pairs(d); pair++)
                    fx_lv2_host_set_control(stage->lv2[pair], update->symbol, update->values[0]);
            } else if (stage->kind == STAGE_LV2) {
                float gain = update->values[1];
                if (stage->lv2_frame_size && gain != stage->output_gain) {
                    /* Schedule the inverse trim to switch in lock-step with
                     * the work-point curve: keep the previous compensation
                     * until the new curve reaches the plugin output, then
                     * crossfade over the same Hann overlap the plugin uses. */
                    stage->lv2_comp_old_g = 1.0f / stage->output_gain;
                    stage->lv2_comp_new_g = 1.0f / gain;
                    size_t fed = stage->lv2_frames_fed, frame = stage->lv2_frame_size;
                    stage->lv2_comp_boundary_l = frame * ((fed + frame - 1U) / frame);
                    stage->lv2_comp_boundary_r = frame * ((fed + frame / 2U + frame - 1U) / frame) - frame / 2U;
                    stage->lv2_comp_transition = 1;
                }
                stage->output_gain = gain;
            } else if (stage->kind == STAGE_CONVOLVER) {
                float gain = update->values[1];
                if (!strcmp(update->symbol, "wet_db")) stage->wet = gain;
                else if (!strcmp(update->symbol, "dry_db")) stage->dry = gain;
                else if (!strcmp(update->symbol, "input_gain_db")) stage->input_gain = gain;
                else if (!strcmp(update->symbol, "output_gain_db")) stage->output_gain = gain;
            } else if (stage->kind == STAGE_DELAY) {
                float *milliseconds = !strcmp(update->symbol, "left_ms") ? &stage->left_ms : &stage->right_ms;
                *milliseconds = update->values[0];
                for (unsigned channel = 0; channel < d->inputs; channel++)
                    if ((!strcmp(update->symbol, "left_ms") && !channel_side(d, channel)) ||
                        (!strcmp(update->symbol, "right_ms") && channel_side(d, channel))) {
                        stage->delay[channel] = (size_t)update->values[1];
                        if (stage->delay[channel] >= stage->delay_size[channel]) stage->delay[channel] = stage->delay_size[channel] - 1U;
                    }
            } else if (stage->kind == STAGE_HEADROOM || stage->kind == STAGE_MASTER_GAIN) stage->gain = update->values[1];
            else if (stage->kind == STAGE_UPMIXER) {
                if (!strcmp(update->symbol, "center")) stage->upmix_center = update->values[0];
                else stage->upmix_surround = update->values[0];
                upmixer_apply_strength(stage);
            } else if (stage->kind == STAGE_CRYSTALIZER) {
                stage->intensity_db = update->values[0];
                for (unsigned channel = 0; channel < d->inputs; channel++) fx_crystalizer_set_band_intensity_db(stage->crystalizer[channel], 2U, stage->intensity_db);
            } else if (stage->kind == STAGE_AUTOGAIN) {
                if (!strcmp(update->symbol, "target_db")) {
                    stage->target_db = update->values[0];
                    for (unsigned pair = 0; pair < effect_pairs(d); pair++) fx_autogain_set_target(stage->autogain[pair], update->values[0]);
                } else {
                    stage->silence_db = update->values[0];
                    for (unsigned pair = 0; pair < effect_pairs(d); pair++) fx_autogain_set_silence_threshold(stage->autogain[pair], update->values[0]);
                }
            }
        }
    }
    atomic_store_explicit(&d->live_count, 0, memory_order_release);
    atomic_store_explicit(&d->live_commit, 0, memory_order_release);
}

int fxdsp_live_commit(fxdsp *d) {
    if (!d || atomic_load_explicit(&d->live_commit, memory_order_acquire)) return 0;
    struct timespec start, now;
    if (clock_gettime(CLOCK_MONOTONIC, &start)) return 0;
    atomic_store_explicit(&d->live_commit, 1, memory_order_release);
    while (atomic_load_explicit(&d->live_commit, memory_order_acquire)) {
        if (clock_gettime(CLOCK_MONOTONIC, &now) ||
            (int64_t)(now.tv_sec - start.tv_sec) * INT64_C(1000000000) + now.tv_nsec - start.tv_nsec >= LIVE_COMMIT_TIMEOUT_NS)
            return 0;
        sched_yield();
    }
    return 1;
}

typedef struct {
    fxdsp *d;
    dsp_stage *stage;
    unsigned source, target;
    size_t count, offset;
    uint32_t mute_mask;
    float *const *output;
} process_job;

static void process_channel(void *context, unsigned channel) {
    process_job *j = context;
    fxdsp *d = j->d; dsp_stage *stage = j->stage;
    float *src = d->scratch[j->source][channel], *dst = d->scratch[j->target][channel];
    if (stage->kind == STAGE_CRYSTALIZER) {
        fx_crystalizer_process(stage->crystalizer[channel], src, dst, j->count);
    } else for (size_t n = 0; n < j->count; n++) {
        float driven = src[n] * stage->input_gain;
        float value = stage->output_gain * (stage->dry * driven + stage->wet * convolve(d, &stage->conv[channel], driven));
        dst[n] = isfinite(value) ? value : 0.0f;
    }
}

static void process_pair(void *context, unsigned pair) {
    process_job *j = context;
    fxdsp *d = j->d; dsp_stage *stage = j->stage;
    unsigned left = pair_left(d, pair), right = pair_right(d, pair);
    float ignored[PROCESS_BLOCK];
    float *right_output = left == right ? ignored : d->scratch[j->target][right];
    if (stage->kind == STAGE_AUTOGAIN) {
        (void)fx_autogain_process(stage->autogain[pair], d->scratch[j->source][left], d->scratch[j->source][right],
                                 d->scratch[j->target][left], right_output, j->count);
    } else {
        (void)fx_lv2_host_run(stage->lv2[pair], d->scratch[j->source][left], d->scratch[j->source][right],
                            d->scratch[j->target][left], right_output, (uint32_t)j->count, NULL, 0);
        /* Shared compensation timing is read-only until the stage barrier. */
        if (stage->lv2_comp_transition) {
            for (size_t n = 0; n < j->count; n++) {
                size_t s = stage->lv2_frames_fed + n;
                d->scratch[j->target][left][n] *= lv2_comp_gain(stage, s, 0U);
                right_output[n] *= lv2_comp_gain(stage, s, 1U);
            }
        } else if (stage->output_gain != 1.0f) {
            for (size_t n = 0; n < j->count; n++) {
                d->scratch[j->target][left][n] *= stage->output_gain;
                right_output[n] *= stage->output_gain;
            }
        }
    }
}

static void process_output_bank(void *context, unsigned channel) {
    process_job *j = context;
    fxdsp *d = j->d; output_state *state = &d->out[channel];
    for (size_t n = 0; n < j->count; n++) {
        float bus[FXDSP_XO_GROUPS] = {0.0f};
        for (unsigned order = d->route_start[channel]; order < d->route_start[channel + 1]; order++) {
            const route *r = &d->routes[d->route_order[order]];
            if (!r->post_crossover) bus[r->group] += d->scratch[j->source][r->in][n] * r->gain;
        }
        for (unsigned filter = 0; filter < state->crossover_filter_count; filter++) {
            unsigned group = state->crossover_group[filter];
            bus[group] = run_biquad(&state->crossover_filters[filter], bus[group]);
        }
        float value = bus[0];
        for (unsigned group = 1; group < state->crossover_groups; group++) value += bus[group];
        for (unsigned order = d->route_start[channel]; order < d->route_start[channel + 1]; order++) {
            const route *r = &d->routes[d->route_order[order]];
            if (r->post_crossover) value += d->scratch[j->source][r->in][n] * r->gain;
        }
        for (unsigned filter = 0; filter < state->filter_count; filter++) value = run_biquad(&state->filters[filter], value);
        if (state->has_conv) {
            float driven = value * state->conv_in_gain;
            value = state->conv_out_gain * (state->conv_dry * driven + state->conv_wet * convolve(d, &state->conv, driven));
        }
        d->scratch[j->target][channel][n] = isfinite(value) ? value : 0.0f;
    }
    if (!j->output[channel]) return; /* Preserve unlinked bank, but not delay/meter, histories. */
    for (size_t n = 0; n < j->count; n++) {
        float value = d->scratch[j->target][channel][n];
        state->delay_line[state->delay_pos] = value;
        size_t read = (state->delay_pos + state->delay_size - state->delay) % state->delay_size;
        value = state->delay_line[read] * state->gain * state->polarity;
        state->delay_pos = (state->delay_pos + 1U) % state->delay_size;
        value *= bits_float(atomic_load_explicit(&d->output_gain_bits, memory_order_relaxed));
        if (!isfinite(value) || (j->mute_mask & (UINT32_C(1) << channel))) value = 0.0f;
        j->output[channel][j->offset + n] = value;
        float magnitude = fabsf(value); if (magnitude > state->peak) state->peak = magnitude;
        update_peak(&d->peak_bits[channel], magnitude); state->square_sum += value * value; state->meter_frames++;
    }
}

void fxdsp_process_tapped(fxdsp *d, const float *const *input, float *const *output,
                          float *const *post_effect, size_t frames) {
    for(size_t offset=0;offset<frames;offset+=PROCESS_BLOCK) {
        size_t count=frames-offset<PROCESS_BLOCK?frames-offset:PROCESS_BLOCK;
        apply_live_updates(d);
        uint32_t mute_mask=atomic_load_explicit(&d->mute_mask,memory_order_relaxed);
        for(unsigned channel=0;channel<d->inputs;channel++) {
            if (input[channel]) memcpy(d->scratch[0][channel],input[channel]+offset,count*sizeof(float));
            else memset(d->scratch[0][channel],0,count*sizeof(float));
        }
        unsigned source=0;
        const int bypassed=atomic_load_explicit(&d->effect_bypass,memory_order_relaxed);
        if(bypassed) d->effects_were_bypassed=1;
        else if(d->effects_were_bypassed) { upmixers_reset(d); d->effects_were_bypassed=0; }
        if(!bypassed) {
            for(unsigned index=0;index<d->stage_count;index++) {
                dsp_stage *stage=&d->stages[index]; unsigned target=1U-source;
                process_job job = {.d=d, .stage=stage, .source=source, .target=target, .count=count};
                if(stage->kind==STAGE_CONVOLVER || stage->kind==STAGE_CRYSTALIZER) {
                    run_jobs(d, d->workers, d->inputs, process_channel, &job);
                } else if(stage->kind==STAGE_UPMIXER) {
                    /* Fixed 8 -> 8 matrix on the callback; no per-channel jobs. */
                    const float *upmix_in[FX_UPMIX_FAST_CHANNELS]; float *upmix_out[FX_UPMIX_FAST_CHANNELS];
                    for(unsigned channel=0;channel<FX_UPMIX_FAST_CHANNELS;channel++) {
                        upmix_in[channel]=d->scratch[source][channel]; upmix_out[channel]=d->scratch[target][channel];
                    }
                    if(stage->upmixer_hq) fx_upmix_hq_process(stage->upmixer_hq,upmix_in,upmix_out,count);
                    else fx_upmix_fast_process(stage->upmixer,upmix_in,upmix_out,count);
                } else if(stage->kind==STAGE_AUTOGAIN || stage->kind==STAGE_LV2) {
                    /* Stereo instances stay indivisible on the callback;
                     * this pool only moves native channel/bank kernels. */
                    run_jobs(d, NULL, effect_pairs(d), process_pair, &job);
                    if(stage->kind==STAGE_LV2) {
                        stage->lv2_frames_fed += count;
                        if(stage->lv2_comp_transition) {
                            size_t latest=(stage->lv2_comp_boundary_l>stage->lv2_comp_boundary_r)?stage->lv2_comp_boundary_l:stage->lv2_comp_boundary_r;
                            if(stage->lv2_frames_fed >= latest + stage->lv2_frame_size)
                                stage->lv2_comp_transition = 0;
                        }
                    }
                } else {
                    for(unsigned channel=0;channel<d->inputs;channel++) {
                    float *src=d->scratch[source][channel],*dst=d->scratch[target][channel];
                    for(size_t n=0;n<count;n++) {
                        float value=src[n];
                        if(stage->kind==STAGE_HEADROOM || stage->kind==STAGE_MASTER_GAIN) value*=stage->gain;
                        else if(stage->kind==STAGE_DELAY) {
                            stage->delay_line[channel][stage->delay_pos[channel]]=value;
                            size_t read=(stage->delay_pos[channel]+stage->delay_size[channel]-stage->delay[channel])%stage->delay_size[channel];
                            value=stage->delay_line[channel][read]; stage->delay_pos[channel]=(stage->delay_pos[channel]+1U)%stage->delay_size[channel];
                        }
                        dst[n]=isfinite(value)?value:0.0f;
                    }
                    }
                }
                source=target;
            }
        }
        /* Visible UI meter tap: post-all-stages (behind the final
         * protection limiter), pre-matrix full-band stereo.  Both VU and
         * Peak are derived from this post-limiter signal, so limited peaks
         * are shown as limited.  The tap stays independent of the system
         * master (applied after the whole chain) and of the per-output
         * crossover/trim (applied after the matrix). */
        if(post_effect) {
            write_post_effect_taps(post_effect,d->inputs,d->scratch[source],offset,count);
        }
        unsigned routed=1U-source;
        process_job bank = {.d=d, .source=source, .target=routed, .count=count,
                            .offset=offset, .mute_mask=mute_mask, .output=output};
        run_jobs(d, d->parallel_outputs ? d->workers : NULL, d->outputs, process_output_bank, &bank);
    }
}
void fxdsp_process(fxdsp*d,const float*const*input,float*const*output,size_t frames){fxdsp_process_tapped(d,input,output,NULL,frames);}
void fxdsp_meter(const fxdsp*d,unsigned o,float*p,float*r){if(o>=d->outputs){*p=*r=0;return;}*p=d->out[o].peak;*r=d->out[o].meter_frames?sqrtf(d->out[o].square_sum/d->out[o].meter_frames):0;}
void fxdsp_set_mute(fxdsp*d,uint32_t mask,int muted){if(muted)atomic_fetch_or_explicit(&d->mute_mask,mask,memory_order_relaxed);else atomic_fetch_and_explicit(&d->mute_mask,~mask,memory_order_relaxed);}
void fxdsp_set_effect_bypass(fxdsp*d,int bypassed){atomic_store_explicit(&d->effect_bypass,!!bypassed,memory_order_relaxed);}
int fxdsp_effect_bypass(const fxdsp*d){return atomic_load_explicit(&d->effect_bypass,memory_order_relaxed);}
void fxdsp_set_output_gain_db(fxdsp*d,float gain_db){float gain=isfinite(gain_db)?powf(10.0f,gain_db/20.0f):0.0f;atomic_store_explicit(&d->output_gain_bits,float_bits(gain),memory_order_relaxed);}
float fxdsp_output_gain_db(const fxdsp*d){float gain=bits_float(atomic_load_explicit(&d->output_gain_bits,memory_order_relaxed));return gain>0.0f?20.0f*log10f(gain):-INFINITY;}
unsigned fxdsp_peaks(const fxdsp*d,float*peaks,unsigned count){unsigned total=d->outputs,limit=count<total?count:total;for(unsigned o=0;o<limit;o++)peaks[o]=bits_float(atomic_load_explicit(&d->peak_bits[o],memory_order_relaxed));return total;}
void fxdsp_reset_peaks(fxdsp*d){for(unsigned o=0;o<d->outputs;o++)atomic_store_explicit(&d->peak_bits[o],0,memory_order_relaxed);}
int fxdsp_compatible(const fxdsp *a, const fxdsp *b){return a&&b&&a->rate==b->rate&&a->inputs==b->inputs&&a->outputs==b->outputs&&a->surround==b->surround;}
