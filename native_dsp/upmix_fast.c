/* SPDX-License-Identifier: AGPL-3.0-only */
#include "upmix_fast.h"
#include "upmix_common.h"

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PI FX_UPMIX_PI
#define SQRT_HALF FX_UPMIX_SQRT_HALF

/* Circular history; must exceed twice the Hilbert half length. */
#define HISTORY 1024U
#define HISTORY_MASK (HISTORY - 1U)
#define HILBERT_MAX_HALF 500U
/* Half length of the 90 degree phase FIR at 48 kHz (1.33 ms); scaled with the
 * rate so the low-frequency corner stays near 500 Hz. */
#define HILBERT_HALF_48K 64U
/* Steering targets are recomputed every CONTROL_FRAMES frames. */
#define CONTROL_FRAMES 32U

/* Detector side chain: the analysis ignores bass (always centred) and the top
 * octave (little energy, diffuse), so neither skews the steering. */
#define SIDECHAIN_HIGHPASS_HZ 150.0
#define SIDECHAIN_LOWPASS_HZ 6000.0
#define SIDECHAIN_Q 0.70710678118654752440

/* Time constants in seconds; the multichannel detection is shared with HQ. */
#define STATS_SECONDS 0.040
#define ATTACK_SECONDS 0.030
#define RELEASE_SECONDS 0.250

/* Level gate on the side-chain power sum (-80 dB closed, -70 dB open). */
#define LEVEL_GATE_LOG10_FLOOR 8.0
/* Share of the side component taken out of L/R at full Surround steering. */
#define SIDE_RELIEF 0.5

typedef struct { double b0, b1, b2, a1, a2, z1, z2; } biquad;

struct fx_upmix_fast {
    unsigned rate, center_out, side_out, rear_out;
    unsigned half, taps;
    double hilbert[HILBERT_MAX_HALF / 2U];
    biquad sidechain[2][2];
    double power_left, power_right, cross;
    double center_target, surround_target, center_mix, surround_mix;
    double center_scale, center_exponent, surround_scale, surround_exponent;
    double stats_coeff, attack_coeff, release_coeff;
    fx_upmix_detector detector;
    unsigned position, control_phase;
    float history[FX_UPMIX_FAST_CHANNELS][HISTORY];
    float side_history[HISTORY];
};

static void design_sidechain(biquad *highpass, biquad *lowpass, unsigned rate) {
    double high_w = 2.0 * PI * SIDECHAIN_HIGHPASS_HZ / rate, low_w = 2.0 * PI * SIDECHAIN_LOWPASS_HZ / rate;
    double high_cos = cos(high_w), low_cos = cos(low_w);
    double high_alpha = sin(high_w) / (2.0 * SIDECHAIN_Q), low_alpha = sin(low_w) / (2.0 * SIDECHAIN_Q);
    double high_a0 = 1.0 + high_alpha, low_a0 = 1.0 + low_alpha;
    memset(highpass, 0, sizeof *highpass); memset(lowpass, 0, sizeof *lowpass);
    highpass->b0 = (1.0 + high_cos) / 2.0 / high_a0; highpass->b1 = -(1.0 + high_cos) / high_a0;
    highpass->b2 = highpass->b0; highpass->a1 = -2.0 * high_cos / high_a0; highpass->a2 = (1.0 - high_alpha) / high_a0;
    lowpass->b0 = (1.0 - low_cos) / 2.0 / low_a0; lowpass->b1 = (1.0 - low_cos) / low_a0;
    lowpass->b2 = lowpass->b0; lowpass->a1 = -2.0 * low_cos / low_a0; lowpass->a2 = (1.0 - low_alpha) / low_a0;
}

static double run_biquad(biquad *filter, double value) {
    double output = value * filter->b0 + filter->z1;
    filter->z1 = value * filter->b1 - filter->a1 * output + filter->z2;
    filter->z2 = value * filter->b2 - filter->a2 * output;
    return output;
}

static void set_state_defaults(fx_upmix_fast *u) {
    memset(u->history, 0, sizeof u->history);
    memset(u->side_history, 0, sizeof u->side_history);
    for (unsigned channel = 0; channel < 2U; channel++)
        for (unsigned stage = 0; stage < 2U; stage++) u->sidechain[channel][stage].z1 = u->sidechain[channel][stage].z2 = 0.0;
    u->power_left = u->power_right = u->cross = 0.0;
    u->center_target = u->surround_target = u->center_mix = u->surround_mix = 0.0;
    fx_upmix_detector_reset(&u->detector);
    u->position = u->control_phase = 0;
}

fx_upmix_fast *fx_upmix_fast_create(unsigned rate, unsigned center_out, unsigned side_out, unsigned rear_out) {
    fx_upmix_fast *u;
    unsigned half;
    if (rate < 8000U || rate > 768000U) return NULL;
    u = calloc(1, sizeof *u);
    if (!u) return NULL;
    u->rate = rate;
    u->center_out = center_out ? 1U : 0U; u->side_out = side_out ? 1U : 0U; u->rear_out = rear_out ? 1U : 0U;
    half = 2U * (unsigned)llround((double)HILBERT_HALF_48K / 2.0 * rate / 48000.0);
    if (half < 8U) half = 8U;
    if (half > HILBERT_MAX_HALF) half = HILBERT_MAX_HALF;
    u->half = half; u->taps = half / 2U;
    /* Blackman-windowed Hilbert FIR: odd taps only, h[-m] = -h[m]. */
    for (unsigned tap = 0; tap < u->taps; tap++) {
        double m = 2.0 * tap + 1.0, angle = PI * m / (half + 1.0);
        double window = 0.42 + 0.5 * cos(angle) + 0.08 * cos(2.0 * angle);
        u->hilbert[tap] = 2.0 / (PI * m) * window;
    }
    for (unsigned channel = 0; channel < 2U; channel++) design_sidechain(&u->sidechain[channel][0], &u->sidechain[channel][1], rate);
    u->stats_coeff = fx_upmix_time_coeff(STATS_SECONDS, rate);
    u->attack_coeff = fx_upmix_time_coeff(ATTACK_SECONDS, rate);
    u->release_coeff = fx_upmix_time_coeff(RELEASE_SECONDS, rate);
    fx_upmix_detector_init(&u->detector, rate);
    set_state_defaults(u);
    fx_upmix_fast_set_strength(u, 6.0F, 6.0F);
    return u;
}

void fx_upmix_fast_destroy(fx_upmix_fast *upmixer) { free(upmixer); }

void fx_upmix_fast_reset(fx_upmix_fast *upmixer) { if (upmixer) set_state_defaults(upmixer); }

int fx_upmix_fast_strength_valid(float strength) {
    return fx_upmix_strength_valid(strength);
}

void fx_upmix_fast_set_strength(fx_upmix_fast *upmixer, float center, float surround) {
    if (!upmixer) return;
    fx_upmix_strength_response(center, FX_UPMIX_CENTER_SCALE_MAX, &upmixer->center_scale, &upmixer->center_exponent);
    fx_upmix_strength_response(surround, FX_UPMIX_SURROUND_SCALE_MAX, &upmixer->surround_scale, &upmixer->surround_exponent);
}

size_t fx_upmix_fast_latency(const fx_upmix_fast *upmixer) { return upmixer ? upmixer->half : 0; }

float fx_upmix_fast_engagement(const fx_upmix_fast *upmixer) { return upmixer ? (float)upmixer->detector.engage : 0.0F; }

/* Steering targets from the smoothed band-limited L/R statistics. */
static void update_targets(fx_upmix_fast *u) {
    double energy = u->power_left + u->power_right, product = u->power_left * u->power_right;
    double level = fx_upmix_clamp_unit(log10(energy + 1e-30) + LEVEL_GATE_LOG10_FLOOR);
    double lateral = fabs((u->power_left - u->power_right) / (energy + 1e-30));
    double correlation = product > 1e-30 ? u->cross / sqrt(product) : 0.0;
    double center_raw = 0.0, surround_raw = 0.0;
    if (correlation > 1.0) correlation = 1.0; else if (correlation < -1.0) correlation = -1.0;
    /* Centre: strongly correlated and close to balanced. */
    if (u->center_out) center_raw = fx_upmix_ramp(correlation, 0.15, 0.85) * (1.0 - fx_upmix_ramp(lateral, 0.15, 0.70)) * level;
    /* Surround: open for anything that is not tied to one front channel. The
     * side component itself scales with 1 - correlation, so a further gate on
     * the correlation would only suppress ambience twice; it closes near mono. */
    if (u->side_out || u->rear_out) surround_raw = (1.0 - fx_upmix_ramp(correlation, 0.90, 0.995)) * (1.0 - fx_upmix_ramp(lateral, 0.35, 0.90)) * level;
    u->center_target = u->center_scale * pow(center_raw, u->center_exponent);
    u->surround_target = u->surround_scale * pow(surround_raw, u->surround_exponent);
}

void fx_upmix_fast_process(fx_upmix_fast *u, const float *const *input, float *const *output, size_t frames) {
    double side_gain = u->side_out && u->rear_out ? SQRT_HALF : u->side_out ? 1.0 : 0.0;
    double rear_gain = u->side_out && u->rear_out ? SQRT_HALF : u->rear_out ? 1.0 : 0.0;
    for (size_t n = 0; n < frames; n++) {
        unsigned position = u->position, aligned = (position - u->half) & HISTORY_MASK;
        float sample[FX_UPMIX_FAST_CHANNELS];
        double extra_power = 0.0;
        for (unsigned channel = 0; channel < FX_UPMIX_FAST_CHANNELS; channel++) {
            float value = input[channel] ? input[channel][n] : 0.0F;
            if (!isfinite(value)) value = 0.0F;
            sample[channel] = value;
            u->history[channel][position] = value;
            if (channel >= 2U) extra_power += (double)value * value;
        }
        fx_upmix_detector_step(&u->detector, extra_power);
        {
            double left = run_biquad(&u->sidechain[0][1], run_biquad(&u->sidechain[0][0], sample[0]));
            double right = run_biquad(&u->sidechain[1][1], run_biquad(&u->sidechain[1][0], sample[1]));
            u->power_left += u->stats_coeff * (left * left - u->power_left);
            u->power_right += u->stats_coeff * (right * right - u->power_right);
            u->cross += u->stats_coeff * (left * right - u->cross);
        }
        u->side_history[position] = (float)((sample[0] - sample[1]) * SQRT_HALF);
        if (++u->control_phase >= CONTROL_FRAMES) {
            u->control_phase = 0;
            update_targets(u);
            fx_upmix_flush_tiny(&u->power_left); fx_upmix_flush_tiny(&u->power_right); fx_upmix_flush_tiny(&u->cross);
            fx_upmix_flush_tiny(&u->center_mix); fx_upmix_flush_tiny(&u->surround_mix); fx_upmix_detector_flush(&u->detector);
            for (unsigned channel = 0; channel < 2U; channel++)
                for (unsigned stage = 0; stage < 2U; stage++) {
                    fx_upmix_flush_tiny(&u->sidechain[channel][stage].z1); fx_upmix_flush_tiny(&u->sidechain[channel][stage].z2);
                }
        }
        u->center_mix += (u->center_target > u->center_mix ? u->attack_coeff : u->release_coeff) * (u->center_target - u->center_mix);
        u->surround_mix += (u->surround_target > u->surround_mix ? u->attack_coeff : u->release_coeff) * (u->surround_target - u->surround_mix);
        fx_upmix_detector_engage(&u->detector);
        {
            double side_hilbert = 0.0;
            for (unsigned tap = 0; tap < u->taps; tap++) {
                unsigned offset = 2U * tap + 1U;
                side_hilbert += u->hilbert[tap] * ((double)u->side_history[(aligned - offset) & HISTORY_MASK]
                                                   - (double)u->side_history[(aligned + offset) & HISTORY_MASK]);
            }
            double left = u->history[0][aligned], right = u->history[1][aligned];
            double mid = (left + right) * SQRT_HALF, side = (left - right) * SQRT_HALF;
            double phase = u->center_mix * (PI / 2.0), keep = cos(phase), send = sin(phase);
            double take_mid = (1.0 - keep) * mid * SQRT_HALF;
            double take_side = SIDE_RELIEF * u->surround_mix * side * SQRT_HALF;
            double surround_left = u->surround_mix * (side + side_hilbert) * SQRT_HALF;
            double surround_right = u->surround_mix * (side_hilbert - side) * SQRT_HALF;
            double engage = u->detector.engage;
            double result[FX_UPMIX_FAST_CHANNELS];
            for (unsigned channel = 0; channel < FX_UPMIX_FAST_CHANNELS; channel++) result[channel] = u->history[channel][aligned];
            result[0] += engage * -(take_mid + take_side);
            result[1] += engage * (take_side - take_mid);
            if (u->center_out) result[2] += engage * send * mid;
            result[4] += engage * side_gain * surround_left; result[5] += engage * side_gain * surround_right;
            result[6] += engage * rear_gain * surround_left; result[7] += engage * rear_gain * surround_right;
            for (unsigned channel = 0; channel < FX_UPMIX_FAST_CHANNELS; channel++)
                output[channel][n] = isfinite(result[channel]) ? (float)result[channel] : 0.0F;
        }
        u->position = (position + 1U) & HISTORY_MASK;
    }
}
