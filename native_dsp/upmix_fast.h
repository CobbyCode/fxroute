/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FXROUTE_UPMIX_FAST_H
#define FXROUTE_UPMIX_FAST_H

#include <stddef.h>

/* Stereo to Surround upmixer, low-latency adaptive matrix ("Fast").
 *
 * Works on the eight Surround program positions FL FR FC LFE SL SR BL BR.
 * FL/FR carry the stereo programme; Center and the Side/Rear pairs are
 * derived from them. LFE is never touched. Every channel passes through the
 * same fixed delay, so inter-channel timing is identical with the upmixer
 * engaged, disengaged or bypassed.
 *
 * The matrix steers from the band-limited inter-channel correlation and
 * balance of L/R: correlated, centred material feeds Center and is taken out
 * of L/R by an energy-preserving rotation; decorrelated or out-of-phase
 * material feeds the surround pair(s) as two mutually decorrelated feeds
 * (90 degree phase pair). Hard-panned material stays in its front channel.
 *
 * Output presence flags select the destinations that exist in the routing:
 * without a Center output no Center extraction happens (L/R keep the centre
 * image); without Side and Rear outputs no Surround extraction happens. With
 * both pairs present each gets half the power.
 *
 * A source that already carries real multichannel content (a level in
 * FC/LFE/SL/SR/BL/BR above the detection threshold) disengages the matrix
 * and keeps it disengaged until those channels have stayed quiet for a while.
 * Detection uses a smoothed level with hysteresis, so single glitches and
 * levels around the threshold do not switch it; the matrix returns after a
 * fixed 10 seconds without such content.
 *
 * All state is allocated at creation; process performs no allocation,
 * locking or I/O. */
#define FX_UPMIX_FAST_CHANNELS 8U
#define FX_UPMIX_FAST_STRENGTH_MIN 0.0F
#define FX_UPMIX_FAST_STRENGTH_MAX 10.0F

typedef struct fx_upmix_fast fx_upmix_fast;

fx_upmix_fast *fx_upmix_fast_create(unsigned rate, unsigned center_out,
                                    unsigned side_out, unsigned rear_out);
void fx_upmix_fast_destroy(fx_upmix_fast *upmixer);
/* Clears signal history and steering state; strengths are kept. */
void fx_upmix_fast_reset(fx_upmix_fast *upmixer);
/* Range check shared by the setter, the engine config path and the live path. */
int fx_upmix_fast_strength_valid(float strength);
/* Steering strengths 0 (this share off) .. 10 (strong); out-of-range values are clamped.
 * Center and Surround are independent: either may be 0 while the other steers. */
void fx_upmix_fast_set_strength(fx_upmix_fast *upmixer, float center, float surround);
/* Frames every channel is delayed by. */
size_t fx_upmix_fast_latency(const fx_upmix_fast *upmixer);
/* Current engagement (0 = pass-through, 1 = full upmix) for tests and diagnostics. */
float fx_upmix_fast_engagement(const fx_upmix_fast *upmixer);
/* Eight input and eight output channel buffers; input and output must not alias. */
void fx_upmix_fast_process(fx_upmix_fast *upmixer, const float *const *input,
                           float *const *output, size_t frames);

#endif
