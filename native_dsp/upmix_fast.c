/* SPDX-License-Identifier: AGPL-3.0-only */
#include "upmix_fast.h"

#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define PI 3.14159265358979323846
#define SQRT_HALF 0.70710678118654752440

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

/* Time constants in seconds. */
#define STATS_SECONDS 0.040
#define ATTACK_SECONDS 0.030
#define RELEASE_SECONDS 0.250
#define ENGAGE_SECONDS 1.000
#define DISENGAGE_SECONDS 0.005

/* Real multichannel detection on the power envelope of FC/LFE/SL/SR/BL/BR,
 * compared as the peak level of an equivalent sine. Above -66 dBFS the source
 * counts as multichannel and the matrix disengages; while it does, the level
 * only has to stay above -72 dBFS (hysteresis) to keep refreshing the hold,
 * and the matrix re-engages HOLD_SECONDS after it fell below that. The
 * envelope ignores lone glitches and noise whose peaks only touch the
 * threshold. Content that interrupts a re-engagement within STABLE_SECONDS
 * doubles the hold (up to HOLD_SCALE_MAX times), so sparse multichannel
 * content does not make the matrix come and go; an engagement that lasted
 * STABLE_SECONDS restores the normal hold. */
#define ACTIVITY_SECONDS 0.020
#define MULTICHANNEL_ON_POWER 1.25e-7
#define MULTICHANNEL_OFF_POWER 3.125e-8
#define HOLD_SECONDS 30.0
#define HOLD_SCALE_MAX 4U
#define STABLE_SECONDS 600.0

/* Level gate on the side-chain power sum (-80 dB closed, -70 dB open). */
#define LEVEL_GATE_LOG10_FLOOR 8.0
/* Share of the side component taken out of L/R at full Surround steering. */
#define SIDE_RELIEF 0.5
/* Strength 1..10 maps to a steering scale and a response exponent 1.80..0.70:
 * stronger settings extract more and also react to material that is only
 * partly correlated. Center may take the whole mid component; Surround stops
 * at 0.8 of the side component so the rears do not outweigh the fronts on
 * wide material. Strength 0 switches that share off: scale 0 down to 1 is
 * linear into the weakest steering. */
#define SCALE_MIN 0.10
#define CENTER_SCALE_MAX 1.00
#define SURROUND_SCALE_MAX 0.80
#define EXPONENT_WEAK 1.80
#define EXPONENT_STRONG 0.70
#define STEERING_STRENGTH_FIRST 1.0

typedef struct { double b0, b1, b2, a1, a2, z1, z2; } biquad;

struct fx_upmix_fast {
    unsigned rate, center_out, side_out, rear_out;
    unsigned half, taps;
    double hilbert[HILBERT_MAX_HALF / 2U];
    biquad sidechain[2][2];
    double power_left, power_right, cross;
    double center_target, surround_target, center_mix, surround_mix, engage;
    double center_scale, center_exponent, surround_scale, surround_exponent;
    double stats_coeff, attack_coeff, release_coeff, engage_coeff, disengage_coeff, activity_coeff;
    double activity;
    size_t hold_frames, hold_left, stable_frames, engaged_run;
    unsigned hold_scale;
    unsigned position, control_phase;
    float history[FX_UPMIX_FAST_CHANNELS][HISTORY];
    float side_history[HISTORY];
};

static double clamp_unit(double value) { return value < 0.0 ? 0.0 : value > 1.0 ? 1.0 : value; }

/* 0 at or below low, 1 at or above high, smooth in between. */
static double ramp(double value, double low, double high) {
    double t = clamp_unit((value - low) / (high - low));
    return t * t * (3.0 - 2.0 * t);
}

static double time_coeff(double seconds, unsigned rate) { return 1.0 - exp(-1.0 / (seconds * rate)); }

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

static void flush_tiny(double *value) { if (fabs(*value) < 1e-30) *value = 0.0; }

static void set_state_defaults(fx_upmix_fast *u) {
    memset(u->history, 0, sizeof u->history);
    memset(u->side_history, 0, sizeof u->side_history);
    for (unsigned channel = 0; channel < 2U; channel++)
        for (unsigned stage = 0; stage < 2U; stage++) u->sidechain[channel][stage].z1 = u->sidechain[channel][stage].z2 = 0.0;
    u->power_left = u->power_right = u->cross = 0.0;
    u->center_target = u->surround_target = u->center_mix = u->surround_mix = 0.0;
    u->engage = 1.0;
    u->activity = 0.0;
    u->hold_left = 0;
    u->hold_scale = 1U;
    /* A fresh state has no interrupted engagement behind it. */
    u->engaged_run = u->stable_frames;
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
    u->stats_coeff = time_coeff(STATS_SECONDS, rate);
    u->attack_coeff = time_coeff(ATTACK_SECONDS, rate);
    u->release_coeff = time_coeff(RELEASE_SECONDS, rate);
    u->engage_coeff = time_coeff(ENGAGE_SECONDS, rate);
    u->disengage_coeff = time_coeff(DISENGAGE_SECONDS, rate);
    u->activity_coeff = time_coeff(ACTIVITY_SECONDS, rate);
    u->hold_frames = (size_t)(HOLD_SECONDS * rate);
    u->stable_frames = (size_t)(STABLE_SECONDS * rate);
    set_state_defaults(u);
    fx_upmix_fast_set_strength(u, 6.0F, 6.0F);
    return u;
}

void fx_upmix_fast_destroy(fx_upmix_fast *upmixer) { free(upmixer); }

void fx_upmix_fast_reset(fx_upmix_fast *upmixer) { if (upmixer) set_state_defaults(upmixer); }

int fx_upmix_fast_strength_valid(float strength) {
    return isfinite(strength) && strength >= FX_UPMIX_FAST_STRENGTH_MIN && strength <= FX_UPMIX_FAST_STRENGTH_MAX;
}

static void strength_response(float strength, double scale_max, double *scale, double *exponent) {
    double clamped = !isfinite(strength) ? FX_UPMIX_FAST_STRENGTH_MIN :
        strength < FX_UPMIX_FAST_STRENGTH_MIN ? FX_UPMIX_FAST_STRENGTH_MIN :
        strength > FX_UPMIX_FAST_STRENGTH_MAX ? FX_UPMIX_FAST_STRENGTH_MAX : strength;
    if (clamped < STEERING_STRENGTH_FIRST) {
        *scale = SCALE_MIN * clamped;
        *exponent = EXPONENT_WEAK;
        return;
    }
    double t = (clamped - STEERING_STRENGTH_FIRST) / (FX_UPMIX_FAST_STRENGTH_MAX - STEERING_STRENGTH_FIRST);
    *scale = SCALE_MIN + (scale_max - SCALE_MIN) * t;
    *exponent = EXPONENT_WEAK + (EXPONENT_STRONG - EXPONENT_WEAK) * t;
}

void fx_upmix_fast_set_strength(fx_upmix_fast *upmixer, float center, float surround) {
    if (!upmixer) return;
    strength_response(center, CENTER_SCALE_MAX, &upmixer->center_scale, &upmixer->center_exponent);
    strength_response(surround, SURROUND_SCALE_MAX, &upmixer->surround_scale, &upmixer->surround_exponent);
}

size_t fx_upmix_fast_latency(const fx_upmix_fast *upmixer) { return upmixer ? upmixer->half : 0; }

float fx_upmix_fast_engagement(const fx_upmix_fast *upmixer) { return upmixer ? (float)upmixer->engage : 0.0F; }

/* Steering targets from the smoothed band-limited L/R statistics. */
static void update_targets(fx_upmix_fast *u) {
    double energy = u->power_left + u->power_right, product = u->power_left * u->power_right;
    double level = clamp_unit(log10(energy + 1e-30) + LEVEL_GATE_LOG10_FLOOR);
    double lateral = fabs((u->power_left - u->power_right) / (energy + 1e-30));
    double correlation = product > 1e-30 ? u->cross / sqrt(product) : 0.0;
    double center_raw = 0.0, surround_raw = 0.0;
    if (correlation > 1.0) correlation = 1.0; else if (correlation < -1.0) correlation = -1.0;
    /* Centre: strongly correlated and close to balanced. */
    if (u->center_out) center_raw = ramp(correlation, 0.15, 0.85) * (1.0 - ramp(lateral, 0.15, 0.70)) * level;
    /* Surround: open for anything that is not tied to one front channel. The
     * side component itself scales with 1 - correlation, so a further gate on
     * the correlation would only suppress ambience twice; it closes near mono. */
    if (u->side_out || u->rear_out) surround_raw = (1.0 - ramp(correlation, 0.90, 0.995)) * (1.0 - ramp(lateral, 0.35, 0.90)) * level;
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
        u->activity += u->activity_coeff * (extra_power - u->activity);
        if (u->activity > (u->hold_left ? MULTICHANNEL_OFF_POWER : MULTICHANNEL_ON_POWER)) {
            if (!u->hold_left) {
                if (u->engaged_run >= u->stable_frames) u->hold_scale = 1U;
                else if (u->hold_scale < HOLD_SCALE_MAX) u->hold_scale *= 2U;
                u->engaged_run = 0;
            }
            u->hold_left = u->hold_frames * u->hold_scale;
        } else if (u->hold_left) u->hold_left--;
        else if (u->engaged_run < u->stable_frames) u->engaged_run++;
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
            flush_tiny(&u->power_left); flush_tiny(&u->power_right); flush_tiny(&u->cross);
            flush_tiny(&u->center_mix); flush_tiny(&u->surround_mix); flush_tiny(&u->engage); flush_tiny(&u->activity);
            for (unsigned channel = 0; channel < 2U; channel++)
                for (unsigned stage = 0; stage < 2U; stage++) {
                    flush_tiny(&u->sidechain[channel][stage].z1); flush_tiny(&u->sidechain[channel][stage].z2);
                }
        }
        u->center_mix += (u->center_target > u->center_mix ? u->attack_coeff : u->release_coeff) * (u->center_target - u->center_mix);
        u->surround_mix += (u->surround_target > u->surround_mix ? u->attack_coeff : u->release_coeff) * (u->surround_target - u->surround_mix);
        u->engage += (u->hold_left ? u->disengage_coeff : u->engage_coeff) * ((u->hold_left ? 0.0 : 1.0) - u->engage);
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
            double engage = u->engage;
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
