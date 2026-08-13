#include "crystalizer.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#include <speex/speex_resampler.h>

#define CRYSTALIZER_BANDS 13U
#define BLOCK_SIZE 2048U
#define FFT_SIZE (2U * BLOCK_SIZE)
#define PI 3.14159265358979323846

typedef struct { float re, im; } complex_value;

struct fx_crystalizer {
    unsigned rate, *reverse;
    size_t kernel_size, partition_count, block_pos, output_pos, output_count, spectrum_pos;
    SpeexResamplerState *upsampler, *downsampler;
    complex_value *roots, *ir, *spectra, *work;
    float *band_data[CRYSTALIZER_BANDS];
    float *band_previous_data[CRYSTALIZER_BANDS];
    float *overlap[CRYSTALIZER_BANDS];
    float input_block[BLOCK_SIZE], processed_block[BLOCK_SIZE], output_block[BLOCK_SIZE];
    float global_previous_data[BLOCK_SIZE], band_previous[CRYSTALIZER_BANDS];
    float env_kurtosis[CRYSTALIZER_BANDS], env_crest[CRYSTALIZER_BANDS], env_flux[CRYSTALIZER_BANDS];
    float adaptive_intensity[CRYSTALIZER_BANDS], base_intensity[CRYSTALIZER_BANDS];
    float global_kurtosis, global_crest, global_flux, global_previous;
    int first_block;
};

static float edges[CRYSTALIZER_BANDS + 1U];
static int edges_ready;

static void initialize_edges(void) {
    if (edges_ready) return;
    for (unsigned i = 0; i <= CRYSTALIZER_BANDS; i++) {
        float t = (float)i / (float)CRYSTALIZER_BANDS;
        edges[i] = 20.0F * powf(1000.0F, powf(t, 0.413F));
    }
    edges_ready = 1;
}

static void fft(const fx_crystalizer *c, complex_value *values, int inverse) {
    for (size_t i = 0; i < FFT_SIZE; i++) if (c->reverse[i] > i) {
        complex_value swap = values[i]; values[i] = values[c->reverse[i]]; values[c->reverse[i]] = swap;
    }
    for (size_t width = 2; width <= FFT_SIZE; width *= 2U) {
        size_t step = FFT_SIZE / width;
        for (size_t base = 0; base < FFT_SIZE; base += width) for (size_t j = 0; j < width / 2U; j++) {
            complex_value root = c->roots[j * step], right = values[base + j + width / 2U], product;
            if (inverse) root.im = -root.im;
            product.re = right.re * root.re - right.im * root.im;
            product.im = right.re * root.im + right.im * root.re;
            right = values[base + j];
            values[base + j].re = right.re + product.re; values[base + j].im = right.im + product.im;
            values[base + j + width / 2U].re = right.re - product.re;
            values[base + j + width / 2U].im = right.im - product.im;
        }
    }
    if (inverse) for (size_t i = 0; i < FFT_SIZE; i++) {
        values[i].re /= FFT_SIZE; values[i].im /= FFT_SIZE;
    }
}

static void filter_block(fx_crystalizer *c) {
    c->spectrum_pos = (c->spectrum_pos + 1U) % c->partition_count;
    complex_value *current = c->spectra + c->spectrum_pos * FFT_SIZE;
    memset(current, 0, FFT_SIZE * sizeof *current);
    for (size_t i = 0; i < BLOCK_SIZE; i++) current[i].re = c->input_block[i];
    fft(c, current, 0);
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        memset(c->work, 0, FFT_SIZE * sizeof *c->work);
        for (size_t p = 0; p < c->partition_count; p++) {
            size_t index = (c->spectrum_pos + c->partition_count - p) % c->partition_count;
            complex_value *x = c->spectra + index * FFT_SIZE;
            complex_value *h = c->ir + (band * c->partition_count + p) * FFT_SIZE;
            for (size_t i = 0; i < FFT_SIZE; i++) {
                c->work[i].re += x[i].re * h[i].re - x[i].im * h[i].im;
                c->work[i].im += x[i].re * h[i].im + x[i].im * h[i].re;
            }
        }
        fft(c, c->work, 1);
        for (size_t i = 0; i < BLOCK_SIZE; i++) {
            c->band_data[band][i] = c->work[i].re + c->overlap[band][i];
            c->overlap[band][i] = c->work[i + BLOCK_SIZE].re;
        }
    }
}

static float *lowpass(unsigned rate, float cutoff, size_t count) {
    float *kernel = calloc(count, sizeof *kernel); size_t m = count - 1U; double sum = 0.0, fc = cutoff / rate;
    if (!kernel) return NULL;
    for (size_t n = 0; n < count; n++) {
        long distance = (long)n - (long)(m / 2U);
        double value = distance ? sin(2.0 * PI * fc * distance) / distance : 2.0 * PI * fc;
        double window = 0.42 - 0.5 * cos(2.0 * PI * n / m) + 0.08 * cos(4.0 * PI * n / m);
        kernel[n] = (float)(value * window); sum += kernel[n];
    }
    for (size_t n = 0; n < count; n++) kernel[n] = (float)(kernel[n] / sum);
    return kernel;
}

static float compute_kurtosis(const float *data) {
    float mean = 0.0F, m2 = 0.0F, m4 = 0.0F;
    for (size_t i = 0; i < BLOCK_SIZE; i++) mean += data[i];
    mean /= BLOCK_SIZE;
    for (size_t i = 0; i < BLOCK_SIZE; i++) { float d = data[i] - mean, d2 = d * d; m2 += d2; m4 += d2 * d2; }
    m2 /= BLOCK_SIZE; m4 /= BLOCK_SIZE; return m2 > 1e-6F ? m4 / (m2 * m2) : 3.0F;
}

static float compute_crest(const float *data) {
    float square = 0.0F, peak = 0.0F;
    for (size_t i = 0; i < BLOCK_SIZE; i++) { float v = fabsf(data[i]); square += v * v; if (v > peak) peak = v; }
    float value = sqrtf(square / BLOCK_SIZE); return value > 1e-6F ? peak / value : 1.0F;
}

static float compute_flux(const float *data, float *previous) {
    float flux = 0.0F;
    for (size_t i = 0; i < BLOCK_SIZE; i++) { flux += fabsf(data[i] - previous[i]); previous[i] = data[i]; }
    flux /= BLOCK_SIZE; return flux > 1e-6F ? flux : 1.0F;
}

static float envelope(float old, float value, float attack, float release) {
    float alpha = value > old ? attack : release; return alpha * old + (1.0F - alpha) * value;
}

static void process_block(fx_crystalizer *c) {
    float global_d2[BLOCK_SIZE];
    float global_next = 2.0F * c->input_block[BLOCK_SIZE - 1U] - c->input_block[BLOCK_SIZE - 2U];
    if (c->first_block) c->global_previous = c->input_block[0];
    global_d2[0] = c->input_block[1] - 2.0F * c->input_block[0] + c->global_previous;
    for (size_t i = 1; i + 1U < BLOCK_SIZE; i++)
        global_d2[i] = c->input_block[i + 1U] - 2.0F * c->input_block[i] + c->input_block[i - 1U];
    global_d2[BLOCK_SIZE - 1U] = global_next - 2.0F * c->input_block[BLOCK_SIZE - 1U] + c->input_block[BLOCK_SIZE - 2U];
    c->global_previous = c->input_block[BLOCK_SIZE - 1U];
    float block_time = (float)BLOCK_SIZE / (2.0F * c->rate);
    float attack = expf(-block_time / 0.4F), release = expf(-block_time / 3.0F);
    c->global_crest = envelope(c->global_crest, compute_crest(global_d2), attack, release);
    c->global_kurtosis = envelope(c->global_kurtosis, compute_kurtosis(global_d2), attack, release);
    c->global_flux = envelope(c->global_flux, compute_flux(global_d2, c->global_previous_data), attack, release);
    memset(c->processed_block, 0, sizeof c->processed_block);
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        float *data = c->band_data[band], *d2 = global_d2;
        float next = 2.0F * data[BLOCK_SIZE - 1U] - data[BLOCK_SIZE - 2U];
        if (c->first_block) c->band_previous[band] = data[0];
        d2[0] = data[1] - 2.0F * data[0] + c->band_previous[band];
        for (size_t i = 1; i + 1U < BLOCK_SIZE; i++) d2[i] = data[i + 1U] - 2.0F * data[i] + data[i - 1U];
        d2[BLOCK_SIZE - 1U] = next - 2.0F * data[BLOCK_SIZE - 1U] + data[BLOCK_SIZE - 2U];
        c->band_previous[band] = data[BLOCK_SIZE - 1U];
        c->env_crest[band] = envelope(c->env_crest[band], compute_crest(d2), attack, release);
        c->env_kurtosis[band] = envelope(c->env_kurtosis[band], compute_kurtosis(d2), attack, release);
        c->env_flux[band] = envelope(c->env_flux[band], compute_flux(d2, c->band_previous_data[band]), attack, release);
        float base = c->base_intensity[band];
        c->adaptive_intensity[band] = base * cbrtf((c->global_crest / c->env_crest[band]) *
            (c->global_kurtosis / c->env_kurtosis[band]) * (c->global_flux / c->env_flux[band]));
        for (size_t i = 0; i < BLOCK_SIZE; i++)
            c->processed_block[i] += data[i] - c->adaptive_intensity[band] * d2[i];
    }
    spx_uint32_t input_count = BLOCK_SIZE, output_count = BLOCK_SIZE;
    if (speex_resampler_process_float(c->downsampler, 0U, c->processed_block, &input_count,
                                      c->output_block, &output_count) != RESAMPLER_ERR_SUCCESS ||
        input_count != BLOCK_SIZE) output_count = 0U;
    c->output_pos = 0U; c->output_count = output_count; c->first_block = 0;
}

fx_crystalizer *fx_crystalizer_create(unsigned rate) {
    if (rate < 40040U) return NULL;
    fx_crystalizer *c = calloc(1, sizeof *c); if (!c) return NULL;
    c->rate = rate; initialize_edges();
    int error = RESAMPLER_ERR_SUCCESS;
    c->upsampler = speex_resampler_init(1U, rate, 2U * rate, SPEEX_RESAMPLER_QUALITY_DESKTOP, &error);
    if (!c->upsampler || error != RESAMPLER_ERR_SUCCESS) { fx_crystalizer_destroy(c); return NULL; }
    c->downsampler = speex_resampler_init(1U, 2U * rate, rate, SPEEX_RESAMPLER_QUALITY_DESKTOP, &error);
    if (!c->downsampler || error != RESAMPLER_ERR_SUCCESS) { fx_crystalizer_destroy(c); return NULL; }
    size_t m = (size_t)ceil(4.0 / (120.0 / (2.0 * rate))); if (m & 1U) m++;
    c->kernel_size = m + 1U;
    c->partition_count = (c->kernel_size + BLOCK_SIZE - 1U) / BLOCK_SIZE;
    c->reverse = calloc(FFT_SIZE, sizeof *c->reverse);
    c->roots = calloc(BLOCK_SIZE, sizeof *c->roots);
    c->ir = calloc(CRYSTALIZER_BANDS * c->partition_count * FFT_SIZE, sizeof *c->ir);
    c->spectra = calloc(c->partition_count * FFT_SIZE, sizeof *c->spectra);
    c->work = calloc(FFT_SIZE, sizeof *c->work);
    if (!c->reverse || !c->roots || !c->ir || !c->spectra || !c->work) {
        fx_crystalizer_destroy(c); return NULL;
    }
    for (unsigned i = 0; i < FFT_SIZE; i++) {
        unsigned value = i, reversed = 0;
        for (unsigned bit = 0; bit < 12U; bit++) { reversed = (reversed << 1U) | (value & 1U); value >>= 1U; }
        c->reverse[i] = reversed;
    }
    for (unsigned i = 0; i < BLOCK_SIZE; i++) {
        double angle = -2.0 * PI * i / FFT_SIZE; c->roots[i].re = (float)cos(angle); c->roots[i].im = (float)sin(angle);
    }
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        float *upper = lowpass(2U * rate, edges[band + 1U], c->kernel_size);
        float *lower = lowpass(2U * rate, edges[band], c->kernel_size);
        float *kernel = calloc(c->kernel_size, sizeof *kernel);
        c->band_data[band] = calloc(BLOCK_SIZE, sizeof(float));
        c->band_previous_data[band] = calloc(BLOCK_SIZE, sizeof(float));
        c->overlap[band] = calloc(BLOCK_SIZE, sizeof(float));
        if (!upper || !lower || !kernel || !c->band_data[band] || !c->band_previous_data[band] || !c->overlap[band]) {
            free(upper); free(lower); free(kernel); fx_crystalizer_destroy(c); return NULL;
        }
        for (size_t i = 0; i < c->kernel_size; i++) kernel[i] = lower[i] - upper[i];
        for (size_t p = 0; p < c->partition_count; p++) {
            complex_value *spectrum = c->ir + (band * c->partition_count + p) * FFT_SIZE;
            size_t offset = p * BLOCK_SIZE;
            size_t length = c->kernel_size - offset < BLOCK_SIZE ? c->kernel_size - offset : BLOCK_SIZE;
            for (size_t i = 0; i < length; i++) spectrum[i].re = kernel[offset + i];
            fft(c, spectrum, 0);
        }
        free(upper); free(lower); free(kernel);
    }
    fx_crystalizer_reset(c); return c;
}

void fx_crystalizer_destroy(fx_crystalizer *c) {
    if (!c) return;
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        free(c->band_data[band]); free(c->band_previous_data[band]); free(c->overlap[band]);
    }
    speex_resampler_destroy(c->upsampler); speex_resampler_destroy(c->downsampler);
    free(c->reverse); free(c->roots); free(c->ir); free(c->spectra); free(c->work);
    free(c);
}

void fx_crystalizer_reset(fx_crystalizer *c) {
    if (!c) return;
    c->block_pos = c->output_pos = c->output_count = 0U; c->global_previous = 0.0F;
    speex_resampler_reset_mem(c->upsampler); speex_resampler_reset_mem(c->downsampler);
    c->global_crest = c->global_kurtosis = c->global_flux = 1.0F; c->first_block = 1;
    memset(c->input_block, 0, sizeof c->input_block); memset(c->output_block, 0, sizeof c->output_block);
    memset(c->global_previous_data, 0, sizeof c->global_previous_data); memset(c->band_previous, 0, sizeof c->band_previous);
    memset(c->spectra, 0, c->partition_count * FFT_SIZE * sizeof *c->spectra); c->spectrum_pos = 0U;
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        memset(c->band_data[band], 0, BLOCK_SIZE * sizeof(float));
        memset(c->band_previous_data[band], 0, BLOCK_SIZE * sizeof(float));
        memset(c->overlap[band], 0, BLOCK_SIZE * sizeof(float));
        c->env_kurtosis[band] = 3.0F; c->env_crest[band] = c->env_flux[band] = 1.0F;
        if (c->base_intensity[band] == 0.0F)
            c->base_intensity[band] = band == 2U ? powf(10.0F, -2.0F / 20.0F) : 1.0F;
        c->adaptive_intensity[band] = c->base_intensity[band];
    }
}

void fx_crystalizer_set_band_intensity_db(fx_crystalizer *c, size_t band, float db) {
    if (!c || band >= CRYSTALIZER_BANDS || !isfinite(db)) return;
    c->base_intensity[band] = powf(10.0F, db / 20.0F);
    c->adaptive_intensity[band] = c->base_intensity[band];
}

void fx_crystalizer_process(fx_crystalizer *c, const float *input, float *output, size_t frames) {
    if (!c || !input || !output) return;
    for (size_t i = 0; i < frames; i++) {
        output[i] = c->output_count ? c->output_block[c->output_pos++] : 0.0F;
        if (c->output_count) c->output_count--;
        float up[4];
        spx_uint32_t input_count = 1U, output_count = 4U;
        if (speex_resampler_process_float(c->upsampler, 0U, input + i, &input_count,
                                          up, &output_count) != RESAMPLER_ERR_SUCCESS) output_count = 0U;
        for (spx_uint32_t phase = 0; phase < output_count; phase++) {
            c->input_block[c->block_pos] = up[phase];
            if (++c->block_pos == BLOCK_SIZE) { filter_block(c); process_block(c); c->block_pos = 0U; }
        }
    }
}

size_t fx_crystalizer_latency(const fx_crystalizer *c) {
    if (!c) return 0U;
    return BLOCK_SIZE / 2U + (c->kernel_size - 1U) / 4U +
        speex_resampler_get_output_latency(c->upsampler) / 2U +
        speex_resampler_get_output_latency(c->downsampler);
}
size_t fx_crystalizer_band_count(void) { return CRYSTALIZER_BANDS; }
float fx_crystalizer_band_edge(size_t index) { initialize_edges(); return index <= CRYSTALIZER_BANDS ? edges[index] : 0.0F; }
float fx_crystalizer_base_intensity(const fx_crystalizer *c, size_t index) {
    return c && index < CRYSTALIZER_BANDS ? c->base_intensity[index] : 0.0F;
}
float fx_crystalizer_adaptive_intensity(const fx_crystalizer *c, size_t index) {
    return c && index < CRYSTALIZER_BANDS ? c->adaptive_intensity[index] : 0.0F;
}
unsigned fx_crystalizer_oversampling_quality(const fx_crystalizer *c) {
    int quality = 0;
    if (c) speex_resampler_get_quality(c->upsampler, &quality);
    return (unsigned)quality;
}
float fx_crystalizer_transition_band(const fx_crystalizer *c) { return c ? 120.0F : 0.0F; }
