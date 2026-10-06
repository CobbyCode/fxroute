/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FXROUTE_UPMIX_COMMON_H
#define FXROUTE_UPMIX_COMMON_H

/* Pieces shared by the Fast and HQ stereo-to-Surround upmixers: the strength
 * scale, the real-multichannel detection with its engage/disengage smoothing
 * and a few numeric helpers. Header-only so both kernels stay libm-only. */
#include <math.h>
#include <stddef.h>

#define FX_UPMIX_PI 3.14159265358979323846
#define FX_UPMIX_SQRT_HALF 0.70710678118654752440

#define FX_UPMIX_STRENGTH_MIN 0.0F
#define FX_UPMIX_STRENGTH_MAX 10.0F

/* Time constants in seconds. */
#define FX_UPMIX_ENGAGE_SECONDS 1.000
#define FX_UPMIX_DISENGAGE_SECONDS 0.005

/* Real multichannel detection on the power envelope of FC/LFE/SL/SR/BL/BR,
 * compared as the peak level of an equivalent sine. Above -66 dBFS the source
 * counts as multichannel and the matrix disengages; while it does, the level
 * only has to stay above -72 dBFS (hysteresis) to keep refreshing the hold,
 * and the matrix re-engages HOLD_SECONDS after it fell below that. The
 * envelope ignores lone glitches and noise whose peaks only touch the
 * threshold. */
#define FX_UPMIX_ACTIVITY_SECONDS 0.020
#define FX_UPMIX_MULTICHANNEL_ON_POWER 1.25e-7
#define FX_UPMIX_MULTICHANNEL_OFF_POWER 3.125e-8
#define FX_UPMIX_HOLD_SECONDS 10.0

/* Strength 1..10 maps to a steering scale and a response exponent 1.80..0.70:
 * stronger settings extract more and also react to material that is only
 * partly correlated. Center may take the whole mid component; Surround stops
 * at 0.8 of the side component so the rears do not outweigh the fronts on
 * wide material. Strength 0 switches that share off: scale 0 down to 1 is
 * linear into the weakest steering. */
#define FX_UPMIX_SCALE_MIN 0.10
#define FX_UPMIX_CENTER_SCALE_MAX 1.00
#define FX_UPMIX_SURROUND_SCALE_MAX 0.80
#define FX_UPMIX_EXPONENT_WEAK 1.80
#define FX_UPMIX_EXPONENT_STRONG 0.70
#define FX_UPMIX_STEERING_STRENGTH_FIRST 1.0

static inline double fx_upmix_clamp_unit(double value) { return value < 0.0 ? 0.0 : value > 1.0 ? 1.0 : value; }

/* 0 at or below low, 1 at or above high, smooth in between. */
static inline double fx_upmix_ramp(double value, double low, double high) {
    double t = fx_upmix_clamp_unit((value - low) / (high - low));
    return t * t * (3.0 - 2.0 * t);
}

static inline double fx_upmix_time_coeff(double seconds, unsigned rate) { return 1.0 - exp(-1.0 / (seconds * rate)); }

/* Recursive states decay into denormals while idle; flush them. */
static inline void fx_upmix_flush_tiny(double *value) { if (fabs(*value) < 1e-30) *value = 0.0; }

static inline int fx_upmix_strength_valid(float strength) {
    return isfinite(strength) && strength >= FX_UPMIX_STRENGTH_MIN && strength <= FX_UPMIX_STRENGTH_MAX;
}

static inline void fx_upmix_strength_response(float strength, double scale_max, double *scale, double *exponent) {
    double clamped = !isfinite(strength) ? FX_UPMIX_STRENGTH_MIN :
        strength < FX_UPMIX_STRENGTH_MIN ? FX_UPMIX_STRENGTH_MIN :
        strength > FX_UPMIX_STRENGTH_MAX ? FX_UPMIX_STRENGTH_MAX : strength;
    if (clamped < FX_UPMIX_STEERING_STRENGTH_FIRST) {
        *scale = FX_UPMIX_SCALE_MIN * clamped;
        *exponent = FX_UPMIX_EXPONENT_WEAK;
        return;
    }
    double t = (clamped - FX_UPMIX_STEERING_STRENGTH_FIRST) / (FX_UPMIX_STRENGTH_MAX - FX_UPMIX_STEERING_STRENGTH_FIRST);
    *scale = FX_UPMIX_SCALE_MIN + (scale_max - FX_UPMIX_SCALE_MIN) * t;
    *exponent = FX_UPMIX_EXPONENT_WEAK + (FX_UPMIX_EXPONENT_STRONG - FX_UPMIX_EXPONENT_WEAK) * t;
}

/* Multichannel detector and the engagement it drives. engage is 1 while the
 * source is stereo and falls to 0 within milliseconds once real multichannel
 * content shows up; it returns slowly after the hold has run out. */
typedef struct {
    double activity, engage;
    double activity_coeff, engage_coeff, disengage_coeff;
    size_t hold_frames, hold_left;
} fx_upmix_detector;

static inline void fx_upmix_detector_init(fx_upmix_detector *d, unsigned rate) {
    d->activity_coeff = fx_upmix_time_coeff(FX_UPMIX_ACTIVITY_SECONDS, rate);
    d->engage_coeff = fx_upmix_time_coeff(FX_UPMIX_ENGAGE_SECONDS, rate);
    d->disengage_coeff = fx_upmix_time_coeff(FX_UPMIX_DISENGAGE_SECONDS, rate);
    d->hold_frames = (size_t)(FX_UPMIX_HOLD_SECONDS * rate);
}

/* A cleared detector starts engaged: stereo after a long idle must upmix at once. */
static inline void fx_upmix_detector_reset(fx_upmix_detector *d) {
    d->engage = 1.0;
    d->activity = 0.0;
    d->hold_left = 0;
}

/* One frame: extra_power is the summed square of the channels above Left/Right. */
static inline void fx_upmix_detector_step(fx_upmix_detector *d, double extra_power) {
    d->activity += d->activity_coeff * (extra_power - d->activity);
    if (d->activity > (d->hold_left ? FX_UPMIX_MULTICHANNEL_OFF_POWER : FX_UPMIX_MULTICHANNEL_ON_POWER)) d->hold_left = d->hold_frames;
    else if (d->hold_left) d->hold_left--;
}

/* Smooth the engagement towards its target; call once per frame after step. */
static inline void fx_upmix_detector_engage(fx_upmix_detector *d) {
    d->engage += (d->hold_left ? d->disengage_coeff : d->engage_coeff) * ((d->hold_left ? 0.0 : 1.0) - d->engage);
}

static inline void fx_upmix_detector_flush(fx_upmix_detector *d) {
    fx_upmix_flush_tiny(&d->engage);
    fx_upmix_flush_tiny(&d->activity);
}

#endif
