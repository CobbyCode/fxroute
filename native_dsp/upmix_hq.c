/* SPDX-License-Identifier: AGPL-3.0-only */
#include "upmix_hq.h"

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PI FX_UPMIX_PI
#define SQRT_HALF FX_UPMIX_SQRT_HALF

/* Hann analysis and synthesis windows at 75 % overlap sum to 1.5 for every sample. */
#define OVERLAP 4U
#define OVERLAP_GAIN 1.5

/* Output signals synthesised per hop: front deltas, Center and the surround pairs. */
enum { SIG_FRONT_LEFT, SIG_FRONT_RIGHT, SIG_CENTER, SIG_SIDE_LEFT, SIG_SIDE_RIGHT, SIG_REAR_LEFT, SIG_REAR_RIGHT, SIGNALS };
static const unsigned SIGNAL_CHANNEL[SIGNALS] = {0U, 1U, 2U, 4U, 5U, 6U, 7U};

/* Time constants in seconds; the multichannel detection is shared with Fast. */
#define STATS_SECONDS 0.060
#define ATTACK_SECONDS 0.030
#define RELEASE_SECONDS 0.250
/* The statistics of a bin are averaged over a band around it: half width of at
 * least BAND_MIN_HZ, growing with the bin number (about a sixth of an octave). */
#define BAND_MIN_HZ 40.0
#define BAND_FRACTION 0.06

/* Bass stays in FL/FR: below BASS_LOW_HZ nothing is extracted, fully open from
 * BASS_LOW_HZ + BASS_RAMP_HZ. Centred bass is not a Center signal, and leaving it
 * alone keeps the level the existing bass management receives. The corner is
 * never closer than two bins, the reach of the window's main lobe. */
#define BASS_LOW_HZ 70.0
#define BASS_RAMP_HZ 80.0
#define BASS_MAIN_LOBE_BINS 2.0

/* Level gate on the smoothed mean square of FL+FR (-80 dB closed, -70 dB open). */
#define LEVEL_GATE_LOG10_FLOOR 8.0
/* Share of the ambience taken out of FL/FR at full Surround steering. */
#define AMBIENCE_RELIEF 0.5
/* Where the front direction is dominant and close enough to in phase to steer
 * the ambience null at it; elsewhere the null falls back to the centred side
 * axis, so a transient is never mistaken for ambience because of stale
 * statistics and out-of-phase content reaches the surround. */
#define DOMINANCE_LOW 0.30
#define DOMINANCE_HIGH 0.80
#define IN_PHASE_LOW 0.30
#define IN_PHASE_HIGH 0.70
/* Share of a bin's power along the null at which the surround opens. */
#define AMBIENCE_OPEN_LOW 0.002
#define AMBIENCE_OPEN_HIGH 0.040
/* Side/rear split angle: sides take more of lateral ambience, rears of diffuse. */
#define SPLIT_ANGLE_COHERENT (PI / 6.0)
#define SPLIT_ANGLE_DIFFUSE (PI / 3.0)

struct fx_upmix_hq {
    unsigned rate, size, hop, bins, mask;
    unsigned center_out, side_out, rear_out;
    unsigned position, phase;
    double center_scale, center_exponent, surround_scale, surround_exponent;
    double stats_alpha, attack_alpha, release_alpha, norm;
    double energy;
    fx_upmix_detector detector;
    /* FFT tables and work space. */
    unsigned *bit_reverse;
    double *twiddle_cos, *twiddle_sin, *window, *synthesis;
    double *work_re, *work_im;
    /* Per-bin statistics, band limits and gains. */
    unsigned *band_low, *band_high;
    double *band_inverse, *bass_gate;
    double *left_re, *left_im, *right_re, *right_im;
    double *power_left, *power_right, *cross_re, *cross_im;
    double *smooth[4], *cumulative;
    double *center_raw, *surround_raw, *diffuse, *null_left_re, *null_left_im, *null_right;
    double *center_mix, *surround_mix;
    double *spectrum_re[SIGNALS], *spectrum_im[SIGNALS];
    double *accumulator[SIGNALS];
    int signal_active[SIGNALS];
    float *history[FX_UPMIX_HQ_CHANNELS];
};

int fx_upmix_hq_fft_size_valid(unsigned fft_size) { return fft_size == 1024U || fft_size == 2048U || fft_size == 4096U || fft_size == 8192U; }

/* In-place forward complex FFT, radix-2 with the first two stages fused (their
 * twiddles are 1 and -j). Twiddles are stored per stage, so every stage reads
 * them contiguously: the stage of butterfly span `length` starts at index
 * length / 2 - 1. The inverse is the same transform with the real and
 * imaginary arrays swapped. */
static void fft(const fx_upmix_hq *u, double *re, double *im) {
    unsigned size = u->size;
    for (unsigned i = 0; i < size; i++) {
        unsigned j = u->bit_reverse[i];
        if (j > i) {
            double t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (unsigned i = 0; i < size; i += 4U) {
        double sum_re = re[i] + re[i + 1U], sum_im = im[i] + im[i + 1U], diff_re = re[i] - re[i + 1U], diff_im = im[i] - im[i + 1U];
        double sum2_re = re[i + 2U] + re[i + 3U], sum2_im = im[i + 2U] + im[i + 3U];
        double rot_re = im[i + 2U] - im[i + 3U], rot_im = re[i + 3U] - re[i + 2U];       /* (x2 - x3) * -j */
        re[i] = sum_re + sum2_re; im[i] = sum_im + sum2_im;
        re[i + 2U] = sum_re - sum2_re; im[i + 2U] = sum_im - sum2_im;
        re[i + 1U] = diff_re + rot_re; im[i + 1U] = diff_im + rot_im;
        re[i + 3U] = diff_re - rot_re; im[i + 3U] = diff_im - rot_im;
    }
    for (unsigned length = 8U; length <= size; length <<= 1) {
        unsigned half = length >> 1;
        const double *cosines = u->twiddle_cos + (half - 1U), *sines = u->twiddle_sin + (half - 1U);
        for (unsigned start = 0; start < size; start += length)
            for (unsigned k = 0; k < half; k++) {
                double wr = cosines[k], wi = sines[k];
                unsigned a = start + k, b = a + half;
                double tr = re[b] * wr - im[b] * wi, ti = re[b] * wi + im[b] * wr;
                re[b] = re[a] - tr; im[b] = im[a] - ti;
                re[a] += tr; im[a] += ti;
            }
    }
}

static void free_all(fx_upmix_hq *u) {
    if (!u) return;
    free(u->bit_reverse); free(u->twiddle_cos); free(u->twiddle_sin); free(u->window); free(u->synthesis);
    free(u->work_re); free(u->work_im);
    free(u->band_low); free(u->band_high); free(u->band_inverse); free(u->bass_gate);
    free(u->left_re); free(u->left_im); free(u->right_re); free(u->right_im);
    free(u->power_left); free(u->power_right); free(u->cross_re); free(u->cross_im);
    for (unsigned i = 0; i < 4U; i++) free(u->smooth[i]);
    free(u->cumulative);
    free(u->center_raw); free(u->surround_raw); free(u->diffuse);
    free(u->null_left_re); free(u->null_left_im); free(u->null_right);
    free(u->center_mix); free(u->surround_mix);
    for (unsigned i = 0; i < SIGNALS; i++) { free(u->spectrum_re[i]); free(u->spectrum_im[i]); free(u->accumulator[i]); }
    for (unsigned i = 0; i < FX_UPMIX_HQ_CHANNELS; i++) free(u->history[i]);
    free(u);
}

static void set_state_defaults(fx_upmix_hq *u) {
    size_t bins = u->bins;
    for (unsigned c = 0; c < FX_UPMIX_HQ_CHANNELS; c++) memset(u->history[c], 0, sizeof(float) * u->size);
    for (unsigned i = 0; i < SIGNALS; i++) memset(u->accumulator[i], 0, sizeof(double) * u->size);
    memset(u->power_left, 0, sizeof(double) * bins); memset(u->power_right, 0, sizeof(double) * bins);
    memset(u->cross_re, 0, sizeof(double) * bins); memset(u->cross_im, 0, sizeof(double) * bins);
    memset(u->center_mix, 0, sizeof(double) * bins); memset(u->surround_mix, 0, sizeof(double) * bins);
    u->energy = 0.0;
    fx_upmix_detector_reset(&u->detector);
    u->position = u->phase = 0;
}

fx_upmix_hq *fx_upmix_hq_create(unsigned rate, unsigned fft_size, unsigned center_out, unsigned side_out, unsigned rear_out) {
    fx_upmix_hq *u;
    unsigned size, bins;
    if (rate < 8000U || rate > 768000U || !fx_upmix_hq_fft_size_valid(fft_size)) return NULL;
    u = calloc(1, sizeof *u);
    if (!u) return NULL;
    size = fft_size; bins = size / 2U + 1U;
    u->rate = rate; u->size = size; u->bins = bins; u->mask = size - 1U; u->hop = size / OVERLAP;
    u->center_out = center_out ? 1U : 0U; u->side_out = side_out ? 1U : 0U; u->rear_out = rear_out ? 1U : 0U;
#define ALLOC(field, count, type) do { u->field = calloc((count), sizeof(type)); if (!u->field) { free_all(u); return NULL; } } while (0)
    ALLOC(bit_reverse, size, unsigned); ALLOC(twiddle_cos, size, double); ALLOC(twiddle_sin, size, double);
    ALLOC(window, size, double); ALLOC(synthesis, size, double); ALLOC(work_re, size, double); ALLOC(work_im, size, double);
    ALLOC(band_low, bins, unsigned); ALLOC(band_high, bins, unsigned); ALLOC(band_inverse, bins, double); ALLOC(bass_gate, bins, double);
    ALLOC(left_re, bins, double); ALLOC(left_im, bins, double); ALLOC(right_re, bins, double); ALLOC(right_im, bins, double);
    ALLOC(power_left, bins, double); ALLOC(power_right, bins, double); ALLOC(cross_re, bins, double); ALLOC(cross_im, bins, double);
    for (unsigned i = 0; i < 4U; i++) ALLOC(smooth[i], bins, double);
    ALLOC(cumulative, bins + 1U, double);
    ALLOC(center_raw, bins, double); ALLOC(surround_raw, bins, double); ALLOC(diffuse, bins, double);
    ALLOC(null_left_re, bins, double); ALLOC(null_left_im, bins, double); ALLOC(null_right, bins, double);
    ALLOC(center_mix, bins, double); ALLOC(surround_mix, bins, double);
    for (unsigned i = 0; i < SIGNALS; i++) { ALLOC(spectrum_re[i], bins, double); ALLOC(spectrum_im[i], bins, double); ALLOC(accumulator[i], size, double); }
    for (unsigned i = 0; i < FX_UPMIX_HQ_CHANNELS; i++) ALLOC(history[i], size, float);
#undef ALLOC
    {
        unsigned log2_size = 0;
        while ((1U << log2_size) < size) log2_size++;
        for (unsigned i = 0; i < size; i++) {
            unsigned reversed = 0;
            for (unsigned bit = 0; bit < log2_size; bit++) if (i & (1U << bit)) reversed |= 1U << (log2_size - 1U - bit);
            u->bit_reverse[i] = reversed;
        }
    }
    for (unsigned length = 2U; length <= size; length <<= 1)
        for (unsigned k = 0; k < length / 2U; k++) {
            u->twiddle_cos[length / 2U - 1U + k] = cos(2.0 * PI * k / length);
            u->twiddle_sin[length / 2U - 1U + k] = -sin(2.0 * PI * k / length);
        }
    {
        double window_energy = 0.0;
        for (unsigned n = 0; n < size; n++) {
            u->window[n] = 0.5 - 0.5 * cos(2.0 * PI * n / size);
            window_energy += u->window[n] * u->window[n];
            u->synthesis[n] = u->window[n] / (OVERLAP_GAIN * size);
        }
        u->norm = 1.0 / window_energy;
    }
    {
        double lobe_hz = BASS_MAIN_LOBE_BINS * rate / size, bass_low = lobe_hz > BASS_LOW_HZ ? lobe_hz : BASS_LOW_HZ;
        for (unsigned k = 0; k < bins; k++) {
            double half_hz = BAND_MIN_HZ * size / rate, half = half_hz > k * BAND_FRACTION ? half_hz : k * BAND_FRACTION;
            unsigned width = (unsigned)llround(half);
            if (width < 1U) width = 1U;
            u->band_low[k] = k > width ? k - width : 0U;
            u->band_high[k] = k + width < bins ? k + width : bins - 1U;
            u->band_inverse[k] = 1.0 / (u->band_high[k] - u->band_low[k] + 1U);
            u->bass_gate[k] = fx_upmix_ramp((double)k * rate / size, bass_low, bass_low + BASS_RAMP_HZ);
        }
    }
    u->stats_alpha = 1.0 - exp(-(double)u->hop / (STATS_SECONDS * rate));
    u->attack_alpha = 1.0 - exp(-(double)u->hop / (ATTACK_SECONDS * rate));
    u->release_alpha = 1.0 - exp(-(double)u->hop / (RELEASE_SECONDS * rate));
    fx_upmix_detector_init(&u->detector, rate);
    set_state_defaults(u);
    fx_upmix_hq_set_strength(u, 6.0F, 6.0F);
    return u;
}

void fx_upmix_hq_destroy(fx_upmix_hq *upmixer) { free_all(upmixer); }

void fx_upmix_hq_reset(fx_upmix_hq *upmixer) { if (upmixer) set_state_defaults(upmixer); }

void fx_upmix_hq_set_strength(fx_upmix_hq *upmixer, float center, float surround) {
    if (!upmixer) return;
    fx_upmix_strength_response(center, FX_UPMIX_CENTER_SCALE_MAX, &upmixer->center_scale, &upmixer->center_exponent);
    fx_upmix_strength_response(surround, FX_UPMIX_SURROUND_SCALE_MAX, &upmixer->surround_scale, &upmixer->surround_exponent);
}

size_t fx_upmix_hq_latency(const fx_upmix_hq *upmixer) { return upmixer ? upmixer->size - 1U : 0; }

float fx_upmix_hq_engagement(const fx_upmix_hq *upmixer) { return upmixer ? (float)upmixer->detector.engage : 0.0F; }

/* Mean of the four smoothed-in-time statistics over the band around every bin. */
static void smooth_bands(fx_upmix_hq *u) {
    const double *source[4] = {u->power_left, u->power_right, u->cross_re, u->cross_im};
    for (unsigned s = 0; s < 4U; s++) {
        u->cumulative[0] = 0.0;
        for (unsigned k = 0; k < u->bins; k++) u->cumulative[k + 1U] = u->cumulative[k] + source[s][k];
        for (unsigned k = 0; k < u->bins; k++)
            u->smooth[s][k] = (u->cumulative[u->band_high[k] + 1U] - u->cumulative[u->band_low[k]]) * u->band_inverse[k];
    }
}

/* Steering gates, the ambience null and the side/rear diffuseness of every bin. */
static void steer_bins(fx_upmix_hq *u) {
    double level = fx_upmix_clamp_unit(log10(u->energy + 1e-30) + LEVEL_GATE_LOG10_FLOOR);
    int surround_present = u->side_out || u->rear_out;
    for (unsigned k = 0; k < u->bins; k++) {
        double a = u->smooth[0][k], b = u->smooth[1][k], cre = u->smooth[2][k], cim = u->smooth[3][k];
        double total = a + b, product = a * b, cross = sqrt(cre * cre + cim * cim);
        double half_difference = 0.5 * (a - b), spread = sqrt(half_difference * half_difference + cross * cross);
        double correlation = product > 1e-30 ? cre / sqrt(product) : 0.0;
        double lateral = fabs(a - b) / (total + 1e-30);
        double dominance = spread * 2.0 / (total + 1e-30);
        double in_phase = cross > 1e-30 ? cre / cross : 0.0;
        /* The phase between the channels only matters while both carry the source. */
        double both_channels = spread > 1e-30 ? cross / spread : 0.0;
        double weight = fx_upmix_ramp(dominance, DOMINANCE_LOW, DOMINANCE_HIGH) *
                        (1.0 - both_channels * (1.0 - fx_upmix_ramp(in_phase, IN_PHASE_LOW, IN_PHASE_HIGH)));
        double null_left_re = SQRT_HALF, null_left_im = 0.0, null_right = -SQRT_HALF, norm, ambient_power;
        if (correlation > 1.0) correlation = 1.0; else if (correlation < -1.0) correlation = -1.0;
        u->center_raw[k] = u->center_out ?
            fx_upmix_ramp(correlation, 0.15, 0.85) * (1.0 - fx_upmix_ramp(lateral, 0.15, 0.70)) * level * u->bass_gate[k] : 0.0;
        /* Null direction: [sin(a) e^(j phase), -cos(a)] for the dominant in-phase
         * direction (cos 2a = (a - b) / 2 spread, sin 2a = cross / spread), blended
         * with the centred side axis by how dominant that direction is. A coherent
         * source that is out of phase between the channels is no front source: it
         * stays on the side axis and so reaches the surround. */
        if (weight > 0.0 && spread > 1e-30) {
            double cos_double = half_difference / spread;
            double cos_a, sin_a, dir_left_re, dir_left_im;
            if (cos_double > 1.0) cos_double = 1.0; else if (cos_double < -1.0) cos_double = -1.0;
            cos_a = sqrt(0.5 * (1.0 + cos_double)); sin_a = sqrt(0.5 * (1.0 - cos_double));
            /* The phase is undefined without cross power; it only scales sin(a), which is then 0 or irrelevant. */
            dir_left_re = cross > 1e-30 ? sin_a * cre / cross : sin_a; dir_left_im = cross > 1e-30 ? sin_a * cim / cross : 0.0;
            null_left_re = (1.0 - weight) * null_left_re + weight * dir_left_re;
            null_left_im = weight * dir_left_im;
            null_right = (1.0 - weight) * null_right - weight * cos_a;
        }
        norm = sqrt(null_left_re * null_left_re + null_left_im * null_left_im + null_right * null_right);
        null_left_re /= norm; null_left_im /= norm; null_right /= norm;
        u->null_left_re[k] = null_left_re; u->null_left_im[k] = null_left_im; u->null_right[k] = null_right;
        /* Power along the null direction: what is left of the bin once the front direction is gone. */
        ambient_power = (null_left_re * null_left_re + null_left_im * null_left_im) * a + null_right * null_right * b
                        + 2.0 * (null_left_re * cre + null_left_im * cim) * null_right;
        u->surround_raw[k] = surround_present ? fx_upmix_ramp(ambient_power / (total + 1e-30), AMBIENCE_OPEN_LOW, AMBIENCE_OPEN_HIGH) * level * u->bass_gate[k] : 0.0;
        u->diffuse[k] = 1.0 - fx_upmix_ramp(correlation, 0.0, 0.9);
    }
    /* One 1-2-1 pass over the gates keeps single bins from flickering. */
    for (unsigned pass = 0; pass < 2U; pass++) {
        double *gate = pass ? u->surround_raw : u->center_raw, previous = gate[0];
        for (unsigned k = 1; k + 1U < u->bins; k++) {
            double current = gate[k];
            gate[k] = 0.25 * previous + 0.5 * current + 0.25 * gate[k + 1U];
            previous = current;
        }
    }
}

/* Spectra of the signals added to the delayed input, from the current frame. */
static void synthesise_bins(fx_upmix_hq *u) {
    for (unsigned s = 0; s < SIGNALS; s++) { memset(u->spectrum_re[s], 0, sizeof(double) * u->bins); memset(u->spectrum_im[s], 0, sizeof(double) * u->bins); }
    for (unsigned k = 1; k + 1U < u->bins; k++) {
        double center_target = u->center_out ? u->center_scale * pow(u->center_raw[k], u->center_exponent) : 0.0;
        double surround_target = u->surround_scale * pow(u->surround_raw[k], u->surround_exponent);
        double lr = u->left_re[k], li = u->left_im[k], rr = u->right_re[k], ri = u->right_im[k];
        double mid_re = (lr + rr) * SQRT_HALF, mid_im = (li + ri) * SQRT_HALF;
        double phase, keep, send, take, ambient_re, ambient_im, side_gain, rear_gain;
        double relief, feed_re, feed_im;
        u->center_mix[k] += (center_target > u->center_mix[k] ? u->attack_alpha : u->release_alpha) * (center_target - u->center_mix[k]);
        u->surround_mix[k] += (surround_target > u->surround_mix[k] ? u->attack_alpha : u->release_alpha) * (surround_target - u->surround_mix[k]);
        fx_upmix_flush_tiny(&u->center_mix[k]); fx_upmix_flush_tiny(&u->surround_mix[k]);
        phase = u->center_mix[k] * (PI / 2.0); keep = cos(phase); send = sin(phase);
        take = (1.0 - keep) * SQRT_HALF;
        /* Ambient scalar: the front pair projected on the null direction. */
        ambient_re = u->null_left_re[k] * lr + u->null_left_im[k] * li + u->null_right[k] * rr;
        ambient_im = u->null_left_re[k] * li - u->null_left_im[k] * lr + u->null_right[k] * ri;
        relief = AMBIENCE_RELIEF * u->surround_mix[k];
        /* Front deltas: the mid share given to Center and the ambience share given up. */
        u->spectrum_re[SIG_FRONT_LEFT][k] = -take * mid_re - relief * (u->null_left_re[k] * ambient_re - u->null_left_im[k] * ambient_im);
        u->spectrum_im[SIG_FRONT_LEFT][k] = -take * mid_im - relief * (u->null_left_re[k] * ambient_im + u->null_left_im[k] * ambient_re);
        u->spectrum_re[SIG_FRONT_RIGHT][k] = -take * mid_re - relief * u->null_right[k] * ambient_re;
        u->spectrum_im[SIG_FRONT_RIGHT][k] = -take * mid_im - relief * u->null_right[k] * ambient_im;
        if (u->center_out) { u->spectrum_re[SIG_CENTER][k] = send * mid_re; u->spectrum_im[SIG_CENTER][k] = send * mid_im; }
        if (u->side_out && u->rear_out) {
            double angle = SPLIT_ANGLE_COHERENT + (SPLIT_ANGLE_DIFFUSE - SPLIT_ANGLE_COHERENT) * u->diffuse[k];
            side_gain = cos(angle); rear_gain = sin(angle);
        } else {
            side_gain = u->side_out ? 1.0 : 0.0; rear_gain = u->rear_out ? 1.0 : 0.0;
        }
        /* Two feeds 90 degrees apart: (1 - j) and -(1 + j) over sqrt 2. */
        feed_re = u->surround_mix[k] * ambient_re; feed_im = u->surround_mix[k] * ambient_im;
        {
            double left_re = (feed_re + feed_im) * SQRT_HALF, left_im = (feed_im - feed_re) * SQRT_HALF;
            double right_re = (-feed_re + feed_im) * SQRT_HALF, right_im = (-feed_im - feed_re) * SQRT_HALF;
            u->spectrum_re[SIG_SIDE_LEFT][k] = side_gain * left_re; u->spectrum_im[SIG_SIDE_LEFT][k] = side_gain * left_im;
            u->spectrum_re[SIG_SIDE_RIGHT][k] = side_gain * right_re; u->spectrum_im[SIG_SIDE_RIGHT][k] = side_gain * right_im;
            u->spectrum_re[SIG_REAR_LEFT][k] = rear_gain * left_re; u->spectrum_im[SIG_REAR_LEFT][k] = rear_gain * left_im;
            u->spectrum_re[SIG_REAR_RIGHT][k] = rear_gain * right_re; u->spectrum_im[SIG_REAR_RIGHT][k] = rear_gain * right_im;
        }
    }
    for (unsigned s = 0; s < SIGNALS; s++) {
        u->signal_active[s] = 0;
        for (unsigned k = 1; k + 1U < u->bins && !u->signal_active[s]; k++)
            u->signal_active[s] = u->spectrum_re[s][k] != 0.0 || u->spectrum_im[s][k] != 0.0;
    }
}

/* Inverse transform of two real signals at once and windowed overlap-add. */
static void overlap_add(fx_upmix_hq *u, unsigned first, int second) {
    unsigned size = u->size, bins = u->bins;
    const double *a_re = u->spectrum_re[first], *a_im = u->spectrum_im[first];
    const double *b_re = second >= 0 ? u->spectrum_re[second] : NULL, *b_im = second >= 0 ? u->spectrum_im[second] : NULL;
    for (unsigned k = 0; k < bins; k++) {
        double br = b_re ? b_re[k] : 0.0, bi = b_im ? b_im[k] : 0.0;
        u->work_re[k] = a_re[k] - bi; u->work_im[k] = a_im[k] + br;
        if (k > 0U && k + 1U < bins) { u->work_re[size - k] = a_re[k] + bi; u->work_im[size - k] = br - a_im[k]; }
    }
    /* Inverse: swap the arrays; the real signal ends up in the second one. */
    fft(u, u->work_im, u->work_re);
    for (unsigned n = 0; n < size; n++) {
        unsigned slot = (u->position + n) & u->mask;
        u->accumulator[first][slot] += u->synthesis[n] * u->work_re[n];
        if (second >= 0) u->accumulator[second][slot] += u->synthesis[n] * u->work_im[n];
    }
}

static void run_frame(fx_upmix_hq *u) {
    unsigned size = u->size, bins = u->bins;
    double frame_energy = 0.0;
    for (unsigned n = 0; n < size; n++) {
        unsigned slot = (u->position + n) & u->mask;
        u->work_re[n] = u->window[n] * u->history[0][slot];
        u->work_im[n] = u->window[n] * u->history[1][slot];
    }
    fft(u, u->work_re, u->work_im);
    for (unsigned k = 0; k < bins; k++) {
        unsigned mirror = (size - k) & u->mask;
        double lr = 0.5 * (u->work_re[k] + u->work_re[mirror]), li = 0.5 * (u->work_im[k] - u->work_im[mirror]);
        double rr = 0.5 * (u->work_im[k] + u->work_im[mirror]), ri = -0.5 * (u->work_re[k] - u->work_re[mirror]);
        double power_left = (lr * lr + li * li) * u->norm, power_right = (rr * rr + ri * ri) * u->norm;
        double weight = k == 0U || k + 1U == bins ? 1.0 : 2.0;
        u->left_re[k] = lr; u->left_im[k] = li; u->right_re[k] = rr; u->right_im[k] = ri;
        u->power_left[k] += u->stats_alpha * (power_left - u->power_left[k]);
        u->power_right[k] += u->stats_alpha * (power_right - u->power_right[k]);
        u->cross_re[k] += u->stats_alpha * ((lr * rr + li * ri) * u->norm - u->cross_re[k]);
        u->cross_im[k] += u->stats_alpha * ((li * rr - lr * ri) * u->norm - u->cross_im[k]);
        frame_energy += weight * (power_left + power_right);
    }
    u->energy += u->stats_alpha * (frame_energy / size - u->energy);
    fx_upmix_flush_tiny(&u->energy);
    for (unsigned k = 0; k < bins; k++) {
        fx_upmix_flush_tiny(&u->power_left[k]); fx_upmix_flush_tiny(&u->power_right[k]);
        fx_upmix_flush_tiny(&u->cross_re[k]); fx_upmix_flush_tiny(&u->cross_im[k]);
    }
    smooth_bands(u);
    steer_bins(u);
    synthesise_bins(u);
    /* A signal whose spectrum is all zero adds nothing and is not transformed, so a
     * share that is off (or fully released) stays exactly silent: packing two real
     * signals into one transform would leak rounding noise from one into the other. */
    {
        int pending = -1;
        for (unsigned s = 0; s < SIGNALS; s++) {
            if (!u->signal_active[s]) continue;
            if (pending < 0) { pending = (int)s; continue; }
            overlap_add(u, (unsigned)pending, (int)s);
            pending = -1;
        }
        if (pending >= 0) overlap_add(u, (unsigned)pending, -1);
    }
}

void fx_upmix_hq_process(fx_upmix_hq *u, const float *const *input, float *const *output, size_t frames) {
    for (size_t n = 0; n < frames; n++) {
        double extra_power = 0.0, engage;
        unsigned slot = u->position;
        for (unsigned channel = 0; channel < FX_UPMIX_HQ_CHANNELS; channel++) {
            float value = input[channel] ? input[channel][n] : 0.0F;
            if (!isfinite(value)) value = 0.0F;
            u->history[channel][slot] = value;
            if (channel >= 2U) extra_power += (double)value * value;
        }
        u->position = (slot + 1U) & u->mask;
        if (++u->phase >= u->hop) { u->phase = 0; run_frame(u); fx_upmix_detector_flush(&u->detector); }
        fx_upmix_detector_step(&u->detector, extra_power);
        fx_upmix_detector_engage(&u->detector);
        engage = u->detector.engage;
        for (unsigned channel = 0; channel < FX_UPMIX_HQ_CHANNELS; channel++) output[channel][n] = u->history[channel][u->position];
        for (unsigned s = 0; s < SIGNALS; s++) {
            double *accumulated = &u->accumulator[s][u->position];
            double result = (double)output[SIGNAL_CHANNEL[s]][n] + engage * *accumulated;
            output[SIGNAL_CHANNEL[s]][n] = isfinite(result) ? (float)result : 0.0F;
            *accumulated = 0.0;
        }
    }
}
