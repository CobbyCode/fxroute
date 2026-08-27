#include "crystalizer.h"

#include <math.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>

#include <speex/speex_resampler.h>

#if defined(__aarch64__) || defined(__ARM_NEON)
#include <arm_neon.h>
#define FXROUTE_NEON 1
#else
#define FXROUTE_NEON 0
#endif

/* Compile-time escape hatch for verifying the scalar reference path. */
#if defined(FXROUTE_FORCE_SCALAR) && FXROUTE_NEON
#undef FXROUTE_NEON
#define FXROUTE_NEON 0
#endif

#define CRYSTALIZER_BANDS 13U
#define CRYSTALIZER_PAIRS ((CRYSTALIZER_BANDS + 1U) / 2U)
#define BLOCK_SIZE 2048U
#define FFT_SIZE (2U * BLOCK_SIZE)
#define FFT_STAGES 12U
#define PI 3.14159265358979323846

/* Forward/inverse transforms of real band filters are evaluated pairwise in
 * one complex inverse FFT using linearity: if Sa/Sb are spectra of real
 * signals, IDFT(Sa + i*Sb).re == sa[n] and .im == sb[n]. Pairs therefore
 * share a joint spectrum K = H_even + i*H_odd and one inverse transform
 * produces both band outputs, cutting 13 inverse FFTs down to 7 and halving
 * the frequency-domain multiply-accumulate traffic. */

/* Frequency-domain pipeline uses permutations-free conventions:
 *
 * - Forward transform: decimation-in-frequency takes natural-order input and
 *   yields a bit-reversed-index spectrum.
 * - Inverse transform: decimation-in-time consumes bit-reversed-index input
 *   and yields natural-order output.
 * - The 1/N normalization of the inverse is folded once into the frequency
 *   kernels at initialization.
 *
 * All frequency-domain mixing between spectra is elementwise, so operating
 * entirely in the bit-reversed domain is exact and skips every permutation.
 */

struct fx_crystalizer {
    unsigned rate;
    size_t kernel_size, partition_count, block_pos, output_pos, output_count, spectrum_pos;
    SpeexResamplerState *upsampler, *downsampler;
    /* Per-stage contiguous twiddle pools; DIF stages read them front to back,
     * DIT stages in reverse order. */
    float *tw_cos, *tw_sin_fwd, *tw_sin_inv;
    /* Split-complex (SoA) spectra; slot-major layout. */
    float *spectra_re, *spectra_im;
    float *pair_ir_re, *pair_ir_im;
    float *work_re, *work_im;
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

static void initialize_twiddles(fx_crystalizer *c) {
    size_t offset = 0;
    for (unsigned stage = 0; stage < FFT_STAGES; stage++) {
        size_t half = (size_t)1U << stage;
        double denominator = (double)half * 2.0;
        for (size_t j = 0; j < half; j++) {
            double angle = 2.0 * PI * (double)j / denominator;
            c->tw_cos[offset + j] = (float)cos(angle);
            c->tw_sin_fwd[offset + j] = (float)-sin(angle);
            c->tw_sin_inv[offset + j] = (float)sin(angle);
        }
        offset += half;
    }
}

/* Radix-2 DIF butterflies for one stage: groups of `width` samples, lower
 * halves stay unmultiplied, upper halves get decimated by the twiddle. */
static void dif_stage(size_t half, const float *tw_cos, const float *tw_sin,
                      float *re, float *im) {
    const size_t width = half * 2U;
    for (size_t base = 0; base < FFT_SIZE; base += width) {
        float *restrict lower_re = re + base, *restrict lower_im = im + base;
        float *restrict upper_re = lower_re + half, *restrict upper_im = lower_im + half;
#if FXROUTE_NEON
        size_t j = 0;
        for (; j + 4U <= half; j += 4U) {
            float32x4_t lr = vld1q_f32(lower_re + j), li = vld1q_f32(lower_im + j);
            float32x4_t ur = vld1q_f32(upper_re + j), ui = vld1q_f32(upper_im + j);
            float32x4_t kr = vld1q_f32(tw_cos + j), ki = vld1q_f32(tw_sin + j);
            vst1q_f32(lower_re + j, vaddq_f32(lr, ur));
            vst1q_f32(lower_im + j, vaddq_f32(li, ui));
            float32x4_t dr = vsubq_f32(lr, ur), di = vsubq_f32(li, ui);
            /* product = d * w with w = kr + i*ki. */
            float32x4_t pr = vmlsq_f32(vmulq_f32(dr, kr), di, ki);
            float32x4_t pi = vfmaq_f32(vmulq_f32(dr, ki), di, kr);
            vst1q_f32(upper_re + j, pr);
            vst1q_f32(upper_im + j, pi);
        }
        for (; j < half; j++)
#else
        for (size_t j = 0; j < half; j++)
#endif
        {
            const float lr = lower_re[j], li = lower_im[j];
            const float yr = upper_re[j], yi = upper_im[j];
            lower_re[j] = lr + yr; lower_im[j] = li + yi;
            const float dr = lr - yr, di = li - yi;
            upper_re[j] = dr * tw_cos[j] - di * tw_sin[j];
            upper_im[j] = dr * tw_sin[j] + di * tw_cos[j];
        }
    }
}

/* Radix-2 DIT butterflies for one stage: groups of `width` samples, lower and
 * upper halves combined in place with per-stage contiguous twiddles. */
static void dit_stage(size_t half, const float *tw_cos, const float *tw_sin,
                      float *re, float *im) {
    const size_t width = half * 2U;
    for (size_t base = 0; base < FFT_SIZE; base += width) {
        float *restrict lower_re = re + base, *restrict lower_im = im + base;
        float *restrict upper_re = lower_re + half, *restrict upper_im = lower_im + half;
#if FXROUTE_NEON
        size_t j = 0;
        for (; j + 4U <= half; j += 4U) {
            float32x4_t lr = vld1q_f32(lower_re + j), li = vld1q_f32(lower_im + j);
            float32x4_t ur = vld1q_f32(upper_re + j), ui = vld1q_f32(upper_im + j);
            float32x4_t kr = vld1q_f32(tw_cos + j), ki = vld1q_f32(tw_sin + j);
            float32x4_t pr = vmlsq_f32(vmulq_f32(ur, kr), ui, ki);
            float32x4_t pi = vfmaq_f32(vmulq_f32(ur, ki), ui, kr);
            vst1q_f32(lower_re + j, vaddq_f32(lr, pr));
            vst1q_f32(lower_im + j, vaddq_f32(li, pi));
            vst1q_f32(upper_re + j, vsubq_f32(lr, pr));
            vst1q_f32(upper_im + j, vsubq_f32(li, pi));
        }
        for (; j < half; j++)
#else
        for (size_t j = 0; j < half; j++)
#endif
        {
            const float lr = lower_re[j], li = lower_im[j];
            const float yr = upper_re[j], yi = upper_im[j];
            const float pr = yr * tw_cos[j] - yi * tw_sin[j];
            const float pi = yr * tw_sin[j] + yi * tw_cos[j];
            lower_re[j] = lr + pr; lower_im[j] = li + pi;
            upper_re[j] = lr - pr; upper_im[j] = li - pi;
        }
    }
}

/* Natural order -> bit-reversed spectrum. */
static void fx_fft_forward(const fx_crystalizer *c, float *re, float *im) {
    /* Stage widths descend N, N/2, ..., 2. The pool stores twiddle families
     * ascending, so the family for width 2*M sits at offset M/2 - 1. */
    size_t width = FFT_SIZE;
    while (width >= 2U) {
        const size_t half = width / 2U;
        dif_stage(half, c->tw_cos + (half - 1U), c->tw_sin_fwd + (half - 1U), re, im);
        width = half;
    }
}

/* Bit-reversed spectrum -> natural-order time signal (already normalized). */
static void fx_fft_inverse(const fx_crystalizer *c, float *re, float *im) {
    /* Stage widths ascend 2, 4, ..., N; pool offsets sit at (1<<s)-1. */
    size_t offset = 0;
    for (unsigned stage = 0; stage < FFT_STAGES; stage++) {
        dit_stage((size_t)1U << stage, c->tw_cos + offset,
                  c->tw_sin_inv + offset, re, im);
        offset += (size_t)1U << stage;
    }
}

/* Frequency-domain convolution accumulator: z += x * h over all bins. */
static void accumulate_spectrum(const float *restrict x_re, const float *restrict x_im,
                                const float *restrict h_re, const float *restrict h_im,
                                float *restrict z_re, float *restrict z_im, size_t count) {
#if FXROUTE_NEON
    size_t i = 0;
    for (; i + 4U <= count; i += 4U) {
        float32x4_t xr = vld1q_f32(x_re + i), xi = vld1q_f32(x_im + i);
        float32x4_t hr = vld1q_f32(h_re + i), hi = vld1q_f32(h_im + i);
        float32x4_t zr = vld1q_f32(z_re + i), zi = vld1q_f32(z_im + i);
        zr = vaddq_f32(zr, vmlsq_f32(vmulq_f32(xr, hr), xi, hi));
        zi = vaddq_f32(zi, vfmaq_f32(vmulq_f32(xr, hi), xi, hr));
        vst1q_f32(z_re + i, zr);
        vst1q_f32(z_im + i, zi);
    }
    for (; i < count; i++)
#else
    for (size_t i = 0; i < count; i++)
#endif
    {
        z_re[i] += x_re[i] * h_re[i] - x_im[i] * h_im[i];
        z_im[i] += x_re[i] * h_im[i] + x_im[i] * h_re[i];
    }
}

static void filter_block(fx_crystalizer *c) {
    c->spectrum_pos = (c->spectrum_pos + 1U) % c->partition_count;
    float *current_re = c->spectra_re + c->spectrum_pos * FFT_SIZE;
    float *current_im = c->spectra_im + c->spectrum_pos * FFT_SIZE;
    memset(current_re, 0, FFT_SIZE * sizeof *current_re);
    memset(current_im, 0, FFT_SIZE * sizeof *current_im);
    memcpy(current_re, c->input_block, BLOCK_SIZE * sizeof *current_re);
    fx_fft_forward(c, current_re, current_im);
    for (unsigned pair = 0; pair < CRYSTALIZER_PAIRS; pair++) {
        memset(c->work_re, 0, FFT_SIZE * sizeof *c->work_re);
        memset(c->work_im, 0, FFT_SIZE * sizeof *c->work_im);
        for (size_t p = 0; p < c->partition_count; p++) {
            size_t index = (c->spectrum_pos + c->partition_count - p) % c->partition_count;
            accumulate_spectrum(c->spectra_re + index * FFT_SIZE, c->spectra_im + index * FFT_SIZE,
                                c->pair_ir_re + (pair * c->partition_count + p) * FFT_SIZE,
                                c->pair_ir_im + (pair * c->partition_count + p) * FFT_SIZE,
                                c->work_re, c->work_im, FFT_SIZE);
        }
        /* work = IDFT(S_even + i*S_odd): real part feeds the even band,
         * imaginary part the odd band (row CRYSTALIZER_BANDS stays unused). */
        fx_fft_inverse(c, c->work_re, c->work_im);
        const unsigned even = pair * 2U, odd = even + 1U;
        const float *time_even = c->work_re, *time_odd = c->work_im;
        for (size_t i = 0; i < BLOCK_SIZE; i++) {
            if (even < CRYSTALIZER_BANDS) {
                c->band_data[even][i] = time_even[i] + c->overlap[even][i];
                c->overlap[even][i] = time_even[i + BLOCK_SIZE];
            }
            if (odd < CRYSTALIZER_BANDS) {
                c->band_data[odd][i] = time_odd[i] + c->overlap[odd][i];
                c->overlap[odd][i] = time_odd[i + BLOCK_SIZE];
            }
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
    c->tw_cos = calloc(FFT_SIZE - 1U, sizeof *c->tw_cos);
    c->tw_sin_fwd = calloc(FFT_SIZE - 1U, sizeof *c->tw_sin_fwd);
    c->tw_sin_inv = calloc(FFT_SIZE - 1U, sizeof *c->tw_sin_inv);
    c->spectra_re = calloc(c->partition_count * FFT_SIZE, sizeof *c->spectra_re);
    c->spectra_im = calloc(c->partition_count * FFT_SIZE, sizeof *c->spectra_im);
    c->pair_ir_re = calloc(CRYSTALIZER_PAIRS * c->partition_count * FFT_SIZE, sizeof *c->pair_ir_re);
    c->pair_ir_im = calloc(CRYSTALIZER_PAIRS * c->partition_count * FFT_SIZE, sizeof *c->pair_ir_im);
    c->work_re = calloc(FFT_SIZE, sizeof *c->work_re);
    c->work_im = calloc(FFT_SIZE, sizeof *c->work_im);
    if (!c->tw_cos || !c->tw_sin_fwd || !c->tw_sin_inv || !c->spectra_re ||
        !c->spectra_im || !c->pair_ir_re || !c->pair_ir_im || !c->work_re || !c->work_im) {
        fx_crystalizer_destroy(c); return NULL;
    }
    initialize_twiddles(c);
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        c->band_data[band] = calloc(BLOCK_SIZE, sizeof(float));
        c->band_previous_data[band] = calloc(BLOCK_SIZE, sizeof(float));
        c->overlap[band] = calloc(BLOCK_SIZE, sizeof(float));
        if (!c->band_data[band] || !c->band_previous_data[band] || !c->overlap[band]) {
            fx_crystalizer_destroy(c); return NULL;
        }
    }
    float *scratch_re = calloc(FFT_SIZE, sizeof *scratch_re);
    float *scratch_im = calloc(FFT_SIZE, sizeof *scratch_im);
    if (!scratch_re || !scratch_im) { free(scratch_re); free(scratch_im); fx_crystalizer_destroy(c); return NULL; }
    for (unsigned pair = 0; pair < CRYSTALIZER_PAIRS; pair++) {
        float *pair_re = c->pair_ir_re + pair * c->partition_count * FFT_SIZE;
        float *pair_im = c->pair_ir_im + pair * c->partition_count * FFT_SIZE;
        for (unsigned member = 0; member < 2U; member++) {
            unsigned band = pair * 2U + member;
            if (band >= CRYSTALIZER_BANDS) break; /* trailing pair partner stays zero */
            float *upper = lowpass(2U * rate, edges[band + 1U], c->kernel_size);
            float *lower = lowpass(2U * rate, edges[band], c->kernel_size);
            float *kernel = calloc(c->kernel_size, sizeof *kernel);
            if (!upper || !lower || !kernel) {
                free(upper); free(lower); free(kernel);
                free(scratch_re); free(scratch_im); fx_crystalizer_destroy(c); return NULL;
            }
            for (size_t i = 0; i < c->kernel_size; i++) kernel[i] = lower[i] - upper[i];
            for (size_t p = 0; p < c->partition_count; p++) {
                memset(scratch_re, 0, FFT_SIZE * sizeof *scratch_re);
                memset(scratch_im, 0, FFT_SIZE * sizeof *scratch_im);
                size_t offset = p * BLOCK_SIZE;
                size_t length = c->kernel_size - offset < BLOCK_SIZE ? c->kernel_size - offset : BLOCK_SIZE;
                /* Fold the 1/N inverse normalization into the kernels once. */
                const float scale = 1.0F / (float)FFT_SIZE;
                for (size_t i = 0; i < length; i++) scratch_re[i] = scale * kernel[offset + i];
                fx_fft_forward(c, scratch_re, scratch_im);
                float *slot_re = pair_re + p * FFT_SIZE, *slot_im = pair_im + p * FFT_SIZE;
                if (member == 0U) {
                    memcpy(slot_re, scratch_re, FFT_SIZE * sizeof *slot_re);
                    memcpy(slot_im, scratch_im, FFT_SIZE * sizeof *slot_im);
                } else {
                    /* Joint pair spectrum K = H_even + i*H_odd. */
                    for (size_t i = 0; i < FFT_SIZE; i++) {
                        float even_re = slot_re[i], even_im = slot_im[i];
                        slot_re[i] = even_re - scratch_im[i];
                        slot_im[i] = even_im + scratch_re[i];
                    }
                }
            }
            free(upper); free(lower); free(kernel);
        }
    }
    free(scratch_re); free(scratch_im);
    fx_crystalizer_reset(c); return c;
}

void fx_crystalizer_destroy(fx_crystalizer *c) {
    if (!c) return;
    for (unsigned band = 0; band < CRYSTALIZER_BANDS; band++) {
        free(c->band_data[band]); free(c->band_previous_data[band]); free(c->overlap[band]);
    }
    speex_resampler_destroy(c->upsampler); speex_resampler_destroy(c->downsampler);
    free(c->tw_cos); free(c->tw_sin_fwd); free(c->tw_sin_inv);
    free(c->spectra_re); free(c->spectra_im);
    free(c->pair_ir_re); free(c->pair_ir_im);
    free(c->work_re); free(c->work_im);
    free(c);
}

void fx_crystalizer_reset(fx_crystalizer *c) {
    if (!c) return;
    c->block_pos = c->output_pos = c->output_count = 0U; c->global_previous = 0.0F;
    speex_resampler_reset_mem(c->upsampler); speex_resampler_reset_mem(c->downsampler);
    c->global_crest = c->global_kurtosis = c->global_flux = 1.0F; c->first_block = 1;
    memset(c->input_block, 0, sizeof c->input_block); memset(c->output_block, 0, sizeof c->output_block);
    memset(c->global_previous_data, 0, sizeof c->global_previous_data); memset(c->band_previous, 0, sizeof c->band_previous);
    memset(c->spectra_re, 0, c->partition_count * FFT_SIZE * sizeof *c->spectra_re);
    memset(c->spectra_im, 0, c->partition_count * FFT_SIZE * sizeof *c->spectra_im);
    c->spectrum_pos = 0U;
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
    /* Batched resampler with per-sample read/feed semantics: slices end
     * exactly on 2048-upsample block completions (one completion per 1024
     * inputs at the 2x oversampling ratio), so block results become visible
     * to the output ring at the same input positions as the per-sample loop
     * for every quantum and call pattern. */
    float staging[2048];
    size_t done = 0;
    while (done < frames) {
        size_t to_completion = (BLOCK_SIZE - c->block_pos + 1U) / 2U;
        size_t slice = frames - done;
        if (slice > to_completion) slice = to_completion;
        if (slice > 1024U) slice = 1024U;
        for (size_t i = 0; i < slice; i++) {
            output[done + i] = c->output_count ? c->output_block[c->output_pos++] : 0.0F;
            if (c->output_count) c->output_count--;
        }
        spx_uint32_t input_count = (spx_uint32_t)slice, output_count = (spx_uint32_t)(2U * slice);
        if (speex_resampler_process_float(c->upsampler, 0U, input + done, &input_count,
                                          staging, &output_count) != RESAMPLER_ERR_SUCCESS) output_count = 0U;
        for (spx_uint32_t phase = 0; phase < output_count; phase++) {
            c->input_block[c->block_pos] = staging[phase];
            if (++c->block_pos == BLOCK_SIZE) { filter_block(c); process_block(c); c->block_pos = 0U; }
        }
        done += slice;
    }
}

/* Stereo channels complete their heavy frequency-domain blocks on the same
 * audio-clock phase and would coincide in every other callback. Running both
 * channel instances concurrently keeps per-callback wall time near the
 * single-channel level; outputs stay bitwise identical to sequential calls. */
struct fx_crystalizer_worker {
    pthread_mutex_t lock;
    pthread_cond_t kick, done;
    int running, shutdown, started_ok;
    fx_crystalizer *instance;
    const float *in;
    float *out;
    size_t frames;
};

static struct fx_crystalizer_worker pair_worker;
static pthread_once_t pair_worker_once = PTHREAD_ONCE_INIT;

static void *pair_worker_main(void *arg) {
    struct fx_crystalizer_worker *w = arg;
    pthread_mutex_lock(&w->lock);
    while (!w->shutdown) {
        while (!w->running && !w->shutdown)
            pthread_cond_wait(&w->kick, &w->lock);
        if (w->shutdown) break;
        fx_crystalizer_process(w->instance, w->in, w->out, w->frames);
        w->running = 0;
        pthread_cond_signal(&w->done);
    }
    pthread_mutex_unlock(&w->lock);
    return NULL;
}

static void pair_worker_init(void) {
    memset(&pair_worker, 0, sizeof pair_worker);
    pthread_mutex_init(&pair_worker.lock, NULL);
    pthread_cond_init(&pair_worker.kick, NULL);
    pthread_cond_init(&pair_worker.done, NULL);
    pthread_t thread;
    if (pthread_create(&thread, NULL, pair_worker_main, &pair_worker) == 0)
        pair_worker.started_ok = 1;
}

void fx_crystalizer_process_pair(fx_crystalizer *left, fx_crystalizer *right,
                                 const float *input_l, const float *input_r,
                                 float *output_l, float *output_r, size_t frames) {
    if (!left || !right || !input_l || !input_r || !output_l || !output_r) return;
    pthread_once(&pair_worker_once, pair_worker_init);
    if (!pair_worker.started_ok) {
        /* No worker available: fall back to plain sequential processing. */
        fx_crystalizer_process(left, input_l, output_l, frames);
        fx_crystalizer_process(right, input_r, output_r, frames);
        return;
    }
    pthread_mutex_lock(&pair_worker.lock);
    pair_worker.instance = right;
    pair_worker.in = input_r;
    pair_worker.out = output_r;
    pair_worker.frames = frames;
    pair_worker.running = 1;
    pthread_cond_signal(&pair_worker.kick);
    pthread_mutex_unlock(&pair_worker.lock);
    fx_crystalizer_process(left, input_l, output_l, frames);
    pthread_mutex_lock(&pair_worker.lock);
    while (pair_worker.running)
        pthread_cond_wait(&pair_worker.done, &pair_worker.lock);
    pthread_mutex_unlock(&pair_worker.lock);
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
