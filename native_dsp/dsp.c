#define _POSIX_C_SOURCE 200809L
#include "dsp.h"
#include "autogain.h"
#include "crystalizer.h"
#include "lv2_host.h"

#include <errno.h>
#include <math.h>
#include <limits.h>
#include <samplerate.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_ROUTES 1024
#define MAX_BIQUADS 32
#define MAX_LINE 4096
#define CONV_BLOCK 256
#define PROCESS_BLOCK 1024
#define MAX_STAGES 128
#define MAX_CONTROLS 268
#define PI 3.14159265358979323846

typedef struct { unsigned in, out; float gain; } route;
typedef struct { float b0, b1, b2, a1, a2, z1, z2; } biquad;
typedef struct { float re, im; } complex_value;
typedef struct {
    size_t tap_count, head_count, head_pos, partition_count, input_pos, spectrum_pos;
    float *head, *head_history, *input_block, *tail_output, *overlap;
    complex_value *ir_spectra, *input_spectra, *work;
} convolution;
typedef struct {
    float gain, polarity;
    size_t delay, delay_pos, delay_size;
    float *delay_line;
    biquad filters[MAX_BIQUADS];
    unsigned filter_count;
    float peak, square_sum;
    uint64_t meter_frames;
} output_state;

typedef enum { STAGE_CONVOLVER, STAGE_DELAY, STAGE_HEADROOM, STAGE_AUTOGAIN,
               STAGE_CRYSTALIZER, STAGE_LV2 } stage_kind;
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
} dsp_stage;

struct fxdsp {
    unsigned rate, inputs, outputs;
    route routes[MAX_ROUTES];
    unsigned route_count;
    output_state out[FXDSP_MAX_CHANNELS];
    dsp_stage stages[MAX_STAGES];
    unsigned stage_count;
    int bypass;
    float *scratch[2][FXDSP_MAX_CHANNELS];
    unsigned *fft_reverse;
    complex_value *fft_roots;
    _Atomic uint32_t mute_mask;
    _Atomic uint32_t peak_bits[FXDSP_MAX_CHANNELS];
};

static uint32_t float_bits(float value) { uint32_t bits; memcpy(&bits,&value,sizeof bits); return bits; }
static float bits_float(uint32_t bits) { float value; memcpy(&value,&bits,sizeof value); return value; }

static void update_peak(_Atomic uint32_t *peak, float value) {
    uint32_t wanted=float_bits(value),current=atomic_load_explicit(peak,memory_order_relaxed);
    while(bits_float(current)<value&&!atomic_compare_exchange_weak_explicit(peak,&current,wanted,memory_order_relaxed,memory_order_relaxed)) {}
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
    c->tap_count = count; c->head_count = count < CONV_BLOCK ? count : CONV_BLOCK;
    c->partition_count = count > CONV_BLOCK ? (count - CONV_BLOCK + CONV_BLOCK - 1) / CONV_BLOCK : 0;
    c->head = calloc(c->head_count, sizeof *c->head); c->head_history = calloc(c->head_count, sizeof *c->head_history);
    if (c->partition_count) {
        c->input_block = calloc(CONV_BLOCK, sizeof *c->input_block); c->tail_output = calloc(CONV_BLOCK, sizeof *c->tail_output);
        c->overlap = calloc(CONV_BLOCK, sizeof *c->overlap); c->work = calloc(fft_size, sizeof *c->work);
        c->ir_spectra = calloc(c->partition_count * fft_size, sizeof *c->ir_spectra);
        c->input_spectra = calloc(c->partition_count * fft_size, sizeof *c->input_spectra);
    }
    if (!c->head || !c->head_history || (c->partition_count && (!c->input_block || !c->tail_output || !c->overlap || !c->work || !c->ir_spectra || !c->input_spectra))) return -1;
    memcpy(c->head, taps, c->head_count * sizeof *taps);
    for (size_t p = 0; p < c->partition_count; p++) {
        complex_value *spectrum = c->ir_spectra + p * fft_size;
        size_t offset = CONV_BLOCK + p * CONV_BLOCK;
        size_t length = count - offset < CONV_BLOCK ? count - offset : CONV_BLOCK;
        for (size_t i = 0; i < length; i++) spectrum[i].re = taps[offset + i];
        fft(d, spectrum, 0);
    }
    return 0;
}

static float convolve(fxdsp *d, convolution *c, float input) {
    double direct = 0;
    c->head_history[c->head_pos] = input;
    size_t history = c->head_pos;
    for (size_t i = 0; i < c->head_count; i++) { direct += c->head[i] * c->head_history[history]; history = history ? history - 1 : c->head_count - 1; }
    c->head_pos = (c->head_pos + 1) % c->head_count;
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
            for (size_t i = 0; i < fft_size; i++) { c->work[i].re += x[i].re*h[i].re-x[i].im*h[i].im; c->work[i].im += x[i].re*h[i].im+x[i].im*h[i].re; }
        }
        fft(d, c->work, 1);
        for (size_t i = 0; i < CONV_BLOCK; i++) { c->tail_output[i] = c->work[i].re + c->overlap[i]; c->overlap[i] = c->work[i + CONV_BLOCK].re; }
        c->input_pos = 0;
    }
    return (float)direct + tail;
}

static int design(biquad *b, const char *type, float rate, float frequency, float q, float gain_db) {
    if (!(frequency > 0 && frequency < rate * .5f && q > 0)) return -1;
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
    b->b0=b0/a0; b->b1=b1/a0; b->b2=b2/a0; b->a1=a1/a0; b->a2=a2/a0; b->z1=b->z2=0; return 0;
}

static float run_biquad(biquad *b, float value) {
    float output = value * b->b0 + b->z1;
    b->z1 = value * b->b1 - b->a1 * output + b->z2;
    b->z2 = value * b->b2 - b->a2 * output;
    return output;
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

static int parse_float_value(char *text, float *result) {
    char *end;
    float value = strtof(text, &end);
    while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') end++;
    if (!*text || *end || !isfinite(value)) return -1;
    *result = value;
    return 0;
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
        if (parse_float_value(value, &number) || number < 0.0f || number > 1000.0f) return -1;
        if (!strcmp(key, "left_ms")) stage->left_ms = number;
        else if (!strcmp(key, "right_ms")) stage->right_ms = number;
        else return -1;
        return 0;
    }
    if (stage->kind == STAGE_HEADROOM) {
        if (strcmp(key, "gain_db") || parse_float_value(value, &number)) return -1;
        stage->gain = powf(10.0f, number / 20.0f); return 0;
    }
    if (stage->kind == STAGE_CRYSTALIZER) {
        if (strcmp(key, "intensity_band2_db") || parse_float_value(value, &number)) return -1;
        stage->intensity_db = number; return 0;
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
        else if (!strcmp(key, "maximum_history_seconds") && number >= 1.0f && number <= 3600.0f && number == floorf(number)) stage->history_seconds = (unsigned)number;
        else return -1;
        return 0;
    }
    return -1;
}

static int initialize_stage(fxdsp *d, dsp_stage *stage, char *error, size_t error_size) {
    unsigned channel;
    if (stage->kind == STAGE_CONVOLVER) {
        if (!stage->argument[0]) { fail(error, error_size, "convolver stage requires path"); return -1; }
        for (channel = 0; channel < d->outputs; channel++) {
            float *taps = NULL; size_t count = 0;
            if (load_wav(stage->argument, channel & 1U, &taps, &count, d->rate) &&
                load_wav(stage->argument, 0, &taps, &count, d->rate)) {
                fail(error, error_size, "cannot load convolver path"); return -1;
            }
            if (prepare_convolution(d, &stage->conv[channel], taps, count)) {
                free(taps); fail(error, error_size, "out of memory"); return -1;
            }
            free(taps);
        }
    } else if (stage->kind == STAGE_DELAY) {
        for (channel = 0; channel < d->outputs; channel++) {
            float milliseconds = channel & 1U ? stage->right_ms : stage->left_ms;
            stage->delay[channel] = (size_t)llround(milliseconds * d->rate / 1000.0f);
            stage->delay_size[channel] = stage->delay[channel] + 1U;
            stage->delay_line[channel] = calloc(stage->delay_size[channel], sizeof(float));
            if (!stage->delay_line[channel]) { fail(error, error_size, "out of memory"); return -1; }
        }
    } else if (stage->kind == STAGE_AUTOGAIN) {
        fx_autogain_config config = fx_autogain_default_config();
        config.reference = stage->reference; config.target_lufs = stage->target_db;
        config.silence_threshold_lufs = stage->silence_db;
        config.maximum_history_seconds = stage->history_seconds;
        if (d->outputs & 1U) { fail(error, error_size, "autogain requires an even stereo output count"); return -1; }
        for (channel = 0; channel < d->outputs / 2U; channel++) {
            stage->autogain[channel] = fx_autogain_init(d->rate, PROCESS_BLOCK, &config);
            if (!stage->autogain[channel]) { fail(error, error_size, "cannot initialize autogain"); return -1; }
        }
    } else if (stage->kind == STAGE_CRYSTALIZER) {
        for (channel = 0; channel < d->outputs; channel++) {
            stage->crystalizer[channel] = fx_crystalizer_create(d->rate);
            if (!stage->crystalizer[channel]) { fail(error, error_size, "cannot initialize crystalizer"); return -1; }
            fx_crystalizer_set_band_intensity_db(stage->crystalizer[channel], 2U,
                                                  stage->intensity_db);
        }
    } else if (stage->kind == STAGE_LV2) {
        if (d->outputs & 1U) { fail(error, error_size, "LV2 stages require an even stereo output count"); return -1; }
        for (channel = 0; channel < d->outputs / 2U; channel++) {
            stage->lv2[channel] = fx_lv2_host_new(stage->argument, d->rate, PROCESS_BLOCK, error, error_size);
            if (!stage->lv2[channel]) return -1;
            for (unsigned control = 0; control < stage->control_count; control++)
                if (!fx_lv2_host_set_control(stage->lv2[channel], stage->controls[control].symbol,
                                             stage->controls[control].value)) {
                    fail(error, error_size, "unknown LV2 control symbol"); return -1;
                }
            fx_lv2_host_activate(stage->lv2[channel]);
        }
    }
    return 0;
}

fxdsp *fxdsp_load(const char *path, char *error, size_t error_size) {
    FILE *file = fopen(path, "r");
    fxdsp *d = calloc(1, sizeof *d);
    char line[MAX_LINE], arg[256], backend[1024], type[32], extra;
    unsigned line_no = 0, out, in;
    dsp_stage *current = NULL;
    int stage_section = 0, post_section = 0;
    if (!file || !d) { fail(error,error_size,"cannot open config"); if(file)fclose(file); free(d); return NULL; }
    d->rate=48000;
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
            if (!strcmp(type, "lv2")) { current->kind = STAGE_LV2; snprintf(current->argument, sizeof current->argument, "%s", kind); }
            else if (!strcmp(type, "native") && !strcmp(kind, "convolver")) current->kind = STAGE_CONVOLVER;
            else if (!strcmp(type, "native") && !strcmp(kind, "delay")) current->kind = STAGE_DELAY;
            else if (!strcmp(type, "native") && !strcmp(kind, "headroom")) current->kind = STAGE_HEADROOM;
            else if (!strcmp(type, "native") && !strcmp(kind, "autogain")) current->kind = STAGE_AUTOGAIN;
            else if (!strcmp(type, "native") && !strcmp(kind, "crystalizer")) current->kind = STAGE_CRYSTALIZER;
            else goto invalid;
        } else { float x,y,z,route_gain; int enabled;
            if (!stage_section && !post_section && sscanf(p,"rate %u %c",&d->rate,&extra)==1) {}
            else if (!stage_section && !post_section && sscanf(p,"inputs %u %c",&d->inputs,&extra)==1) {}
            else if (!stage_section && !post_section && sscanf(p,"outputs %u %c",&d->outputs,&extra)==1) {}
            else if (!stage_section && !post_section && sscanf(p,"matrix %u %u %f %c",&out,&in,&route_gain,&extra)==3 && d->route_count<MAX_ROUTES) { d->routes[d->route_count].out=out; d->routes[d->route_count].in=in; d->routes[d->route_count++].gain=route_gain; }
            else if (!stage_section && !post_section && sscanf(p,"peq %u %31s %f %f %f %c",&out,type,&x,&y,&z,&extra)==5 && out<FXDSP_MAX_CHANNELS && d->out[out].filter_count<MAX_BIQUADS && !design(&d->out[out].filters[d->out[out].filter_count],type,d->rate,x,y,z)) d->out[out].filter_count++;
            else if (sscanf(p,"output %u %f %f %31s %c",&out,&x,&y,arg,&extra)==4 && out<FXDSP_MAX_CHANNELS && y>=0.0f && (!strcmp(arg,"normal") || !strcmp(arg,"invert"))) { post_section=1; d->out[out].gain=powf(10,x/20); d->out[out].delay=(size_t)llround(y*d->rate/1000); d->out[out].polarity=!strcmp(arg,"invert")?-1:1; }
            else if (post_section && sscanf(p,"bypass %d %c",&enabled,&extra)==1) d->bypass=!!enabled;
            else { invalid: snprintf(line,sizeof line,"invalid config line %u",line_no); fail(error,error_size,line); goto bad; }
        }
    }
    if (current) goto invalid;
    fclose(file); file=NULL;
    if (!d->rate || d->inputs<1 || d->inputs>32 || d->outputs<1 || d->outputs>32) { fail(error,error_size,"inputs/outputs must be 1..32"); goto bad; }
    for(unsigned r=0;r<d->route_count;r++) if(d->routes[r].in>=d->inputs||d->routes[r].out>=d->outputs){fail(error,error_size,"matrix index out of range");goto bad;}
    d->fft_reverse=calloc(CONV_BLOCK*2,sizeof *d->fft_reverse);d->fft_roots=calloc(CONV_BLOCK,sizeof *d->fft_roots);
    if(!d->fft_reverse||!d->fft_roots){fail(error,error_size,"out of memory");goto bad;}
    for(unsigned i=0;i<CONV_BLOCK*2;i++){unsigned value=i,reversed=0;for(unsigned bit=0;bit<9;bit++){reversed=(reversed<<1)|(value&1);value>>=1;}d->fft_reverse[i]=reversed;}
    for(unsigned i=0;i<CONV_BLOCK;i++){double angle=-2*PI*i/(CONV_BLOCK*2);d->fft_roots[i].re=cos(angle);d->fft_roots[i].im=sin(angle);}
    for(out=0;out<d->outputs;out++) { output_state *s=&d->out[out]; s->delay_size=s->delay+1; s->delay_line=calloc(s->delay_size,sizeof(float)); if(!s->delay_line){fail(error,error_size,"out of memory");goto bad;} for(unsigned b=0;b<2;b++){d->scratch[b][out]=calloc(PROCESS_BLOCK,sizeof(float));if(!d->scratch[b][out]){fail(error,error_size,"out of memory");goto bad;}} }
    for (unsigned stage = 0; stage < d->stage_count; stage++) if (initialize_stage(d, &d->stages[stage], error, error_size)) goto bad;
    return d;
bad:
    if (file) fclose(file);
    fxdsp_free(d);
    return NULL;
}

static void free_convolution(convolution *c) {
    free(c->head); free(c->head_history); free(c->input_block); free(c->tail_output);
    free(c->overlap); free(c->ir_spectra); free(c->input_spectra); free(c->work);
}

void fxdsp_free(fxdsp *d) {
    if(!d)return;
    for(unsigned i=0;i<FXDSP_MAX_CHANNELS;i++) {
        free(d->out[i].delay_line); free(d->scratch[0][i]); free(d->scratch[1][i]);
    }
    for(unsigned index=0;index<d->stage_count;index++) {
        dsp_stage *stage=&d->stages[index];
        for(unsigned channel=0;channel<FXDSP_MAX_CHANNELS;channel++) {
            free_convolution(&stage->conv[channel]); free(stage->delay_line[channel]);
            fx_crystalizer_destroy(stage->crystalizer[channel]);
        }
        for(unsigned pair=0;pair<FXDSP_MAX_CHANNELS/2;pair++) {
            fx_autogain_free(stage->autogain[pair]); fx_lv2_host_free(stage->lv2[pair]);
        }
    }
    free(d->fft_reverse);free(d->fft_roots);free(d);
}
unsigned fxdsp_inputs(const fxdsp*d){return d->inputs;} unsigned fxdsp_outputs(const fxdsp*d){return d->outputs;} unsigned fxdsp_rate(const fxdsp*d){return d->rate;}

void fxdsp_process(fxdsp *d, const float *const *input, float *const *output, size_t frames) {
    for(size_t offset=0;offset<frames;offset+=PROCESS_BLOCK) {
        size_t count=frames-offset<PROCESS_BLOCK?frames-offset:PROCESS_BLOCK;
        uint32_t mute_mask=atomic_load_explicit(&d->mute_mask,memory_order_relaxed);
        if(d->bypass) {
            for(unsigned channel=0;channel<d->outputs;channel++) for(size_t n=0;n<count;n++)
                d->scratch[0][channel][n]=channel<d->inputs?input[channel][offset+n]:0.0f;
        } else {
            for(unsigned channel=0;channel<d->outputs;channel++) for(size_t n=0;n<count;n++) {
                float value=0.0f;
                for(unsigned route_index=0;route_index<d->route_count;route_index++)
                    if(d->routes[route_index].out==channel)value+=input[d->routes[route_index].in][offset+n]*d->routes[route_index].gain;
                for(unsigned filter=0;filter<d->out[channel].filter_count;filter++) value=run_biquad(&d->out[channel].filters[filter],value);
                d->scratch[0][channel][n]=isfinite(value)?value:0.0f;
            }
            unsigned source=0;
            for(unsigned index=0;index<d->stage_count;index++) {
                dsp_stage *stage=&d->stages[index]; unsigned target=1U-source;
                if(stage->kind==STAGE_AUTOGAIN || stage->kind==STAGE_LV2) {
                    for(unsigned pair=0;pair<d->outputs/2U;pair++) {
                        unsigned left=pair*2U,right=left+1U;
                        if(stage->kind==STAGE_AUTOGAIN)
                            (void)fx_autogain_process(stage->autogain[pair],d->scratch[source][left],d->scratch[source][right],d->scratch[target][left],d->scratch[target][right],count);
                        else
                            (void)fx_lv2_host_run(stage->lv2[pair],d->scratch[source][left],d->scratch[source][right],d->scratch[target][left],d->scratch[target][right],(uint32_t)count,NULL,0);
                        if(stage->kind==STAGE_LV2 && stage->output_gain!=1.0f) for(size_t n=0;n<count;n++) {
                            d->scratch[target][left][n]*=stage->output_gain;
                            d->scratch[target][right][n]*=stage->output_gain;
                        }
                    }
                } else for(unsigned channel=0;channel<d->outputs;channel++) {
                    float *src=d->scratch[source][channel],*dst=d->scratch[target][channel];
                    if(stage->kind==STAGE_CRYSTALIZER) fx_crystalizer_process(stage->crystalizer[channel],src,dst,count);
                    else for(size_t n=0;n<count;n++) {
                        float value=src[n];
                        if(stage->kind==STAGE_HEADROOM) value*=stage->gain;
                        else if(stage->kind==STAGE_CONVOLVER) { float driven=value*stage->input_gain; value=stage->output_gain*(stage->dry*driven+stage->wet*convolve(d,&stage->conv[channel],driven)); }
                        else if(stage->kind==STAGE_DELAY) {
                            stage->delay_line[channel][stage->delay_pos[channel]]=value;
                            size_t read=(stage->delay_pos[channel]+stage->delay_size[channel]-stage->delay[channel])%stage->delay_size[channel];
                            value=stage->delay_line[channel][read]; stage->delay_pos[channel]=(stage->delay_pos[channel]+1U)%stage->delay_size[channel];
                        }
                        dst[n]=isfinite(value)?value:0.0f;
                    }
                }
                source=target;
            }
            if(source) for(unsigned channel=0;channel<d->outputs;channel++)
                memcpy(d->scratch[0][channel],d->scratch[source][channel],count*sizeof(float));
        }
        for(unsigned channel=0;channel<d->outputs;channel++) for(size_t n=0;n<count;n++) {
            output_state *state=&d->out[channel]; float value=d->scratch[0][channel][n];
            if(!d->bypass) {
                state->delay_line[state->delay_pos]=value;
                size_t read=(state->delay_pos+state->delay_size-state->delay)%state->delay_size;
                value=state->delay_line[read]*state->gain*state->polarity;
                state->delay_pos=(state->delay_pos+1U)%state->delay_size;
            }
            if(!isfinite(value)||(mute_mask&(UINT32_C(1)<<channel)))value=0.0f;
            output[channel][offset+n]=value;float magnitude=fabsf(value);if(magnitude>state->peak)state->peak=magnitude;
            update_peak(&d->peak_bits[channel],magnitude);state->square_sum+=value*value;state->meter_frames++;
        }
    }
}
void fxdsp_meter(const fxdsp*d,unsigned o,float*p,float*r){if(o>=d->outputs){*p=*r=0;return;}*p=d->out[o].peak;*r=d->out[o].meter_frames?sqrtf(d->out[o].square_sum/d->out[o].meter_frames):0;}
void fxdsp_set_mute(fxdsp*d,uint32_t mask,int muted){if(muted)atomic_fetch_or_explicit(&d->mute_mask,mask,memory_order_relaxed);else atomic_fetch_and_explicit(&d->mute_mask,~mask,memory_order_relaxed);}
unsigned fxdsp_peaks(const fxdsp*d,float*peaks,unsigned count){unsigned total=d->outputs,limit=count<total?count:total;for(unsigned o=0;o<limit;o++)peaks[o]=bits_float(atomic_load_explicit(&d->peak_bits[o],memory_order_relaxed));return total;}
void fxdsp_reset_peaks(fxdsp*d){for(unsigned o=0;o<d->outputs;o++)atomic_store_explicit(&d->peak_bits[o],0,memory_order_relaxed);}
