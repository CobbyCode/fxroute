/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FXROUTE_UPMIX_HQ_H
#define FXROUTE_UPMIX_HQ_H

#include "upmix_common.h"

#include <stddef.h>

/* Stereo to Surround upmixer, frequency-selective spectral engine ("HQ").
 *
 * Same contract as the Fast engine (upmix_fast.h): the eight Surround program
 * positions FL FR FC LFE SL SR BL BR, FL/FR carry the stereo programme, Center
 * and the Side/Rear pairs are derived, LFE is never touched, strengths run
 * 0..10 as steering strengths, the destination flags select what the routing
 * has, and the multichannel detection with its fixed 10 s return and the
 * direct path it selects are shared.
 *
 * The steering is decided per frequency bin. Every hop an STFT (Hann, 75 %
 * overlap) of FL/FR yields smoothed inter-channel statistics per bin. A
 * correlated, centred bin feeds Center and is taken out of FL/FR by an
 * energy-preserving rotation; the part of a bin that is not tied to the
 * dominant front direction (the ambience, found by nulling that direction)
 * feeds the surround pair(s) as two mutually decorrelated feeds. Panned
 * sources keep their front image because they are nulled out of the ambience
 * and only magnitude gains touch the front path. With both pairs present the
 * more diffuse a bin is, the more of it goes to the rear pair.
 *
 * On a stereo programme every channel passes through the same delay of
 * fft_size - 1 frames (the STFT latency), so inter-channel timing is identical
 * whatever the steering does. Real multichannel content is not upmixed and
 * does not pay for it: while the shared detection holds, the input passes
 * straight through, sample exact and without delay, and no transform runs.
 * The two paths are crossfaded, so entering the direct path skips one delay
 * of programme and returning to the spectral path (once the hold has run out)
 * repeats one. All state is allocated at creation; process performs no
 * allocation, locking or I/O. */
#define FX_UPMIX_HQ_CHANNELS 8U

typedef struct fx_upmix_hq fx_upmix_hq;

/* 1024, 2048 or 4096. */
int fx_upmix_hq_fft_size_valid(unsigned fft_size);
/* Returns NULL for an unsupported rate or FFT size or when allocation fails. */
fx_upmix_hq *fx_upmix_hq_create(unsigned rate, unsigned fft_size, unsigned center_out,
                                unsigned side_out, unsigned rear_out);
void fx_upmix_hq_destroy(fx_upmix_hq *upmixer);
/* Clears signal history and steering state; strengths are kept. */
void fx_upmix_hq_reset(fx_upmix_hq *upmixer);
/* Steering strengths 0 (this share off) .. 10 (strong); out-of-range values are clamped.
 * Center and Surround are independent: either may be 0 while the other steers. */
void fx_upmix_hq_set_strength(fx_upmix_hq *upmixer, float center, float surround);
/* Frames every channel is delayed by on the path selected right now:
 * fft_size - 1 on the spectral path, or 0 while multichannel content passes
 * straight through. Call it from the thread that runs process. */
size_t fx_upmix_hq_latency(const fx_upmix_hq *upmixer);
/* Current engagement (0 = pass-through, 1 = full upmix) for tests and diagnostics. */
float fx_upmix_hq_engagement(const fx_upmix_hq *upmixer);
/* Eight input and eight output channel buffers; input and output must not alias. */
void fx_upmix_hq_process(fx_upmix_hq *upmixer, const float *const *input,
                         float *const *output, size_t frames);

#endif
