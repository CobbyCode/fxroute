/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FXROUTE_SURROUND_H
#define FXROUTE_SURROUND_H
#include <stddef.h>
#include <stdint.h>

#define SURROUND_CHANNELS 8
#define SURROUND_OUTPUTS 10
#define SURROUND_RATE 48000
#define SURROUND_LOOKAHEAD 2050
#define SURROUND_SILENCE_RESET 8192
/* No IEC or confirmed raw-DTS sync for this long ends a compressed stream.
 * Longest S/PDIF burst period (E-AC-3, 6144 frames) plus 512 frames. */
#define SURROUND_SYNC_TIMEOUT 6656
/* Confirmed raw DTS accepts sync spacing up to 8192 carrier frames. */
#define SURROUND_RAW_SYNC_TIMEOUT 8704
#define SURROUND_LIVE_LATENCY 3074

typedef struct surround surround;
/* Eight discrete positions followed by the normalized stereo fold-down. */
typedef void (*surround_emit)(void *data, uint64_t frame, const float pcm[SURROUND_OUTPUTS]);
/* Safety suppression of previously emitted but not yet pulled frames < before. */
typedef void (*surround_mute)(void *data, uint64_t before);
extern const char *const surround_positions[8];
surround *surround_create(surround_emit emit, void *data);
void surround_set_mute(surround *decoder, surround_mute mute);
void surround_destroy(surround *decoder);
void surround_push(surround *decoder, const float *stereo, size_t frames);
void surround_finish(surround *decoder);
/* Discontinuities require a fresh valid burst, a full silence reset or a
 * sync timeout. */
void surround_reset(surround *decoder);
int surround_status_json(const surround *decoder, char *buffer, size_t size);
int surround_write_status(const surround *decoder, const char *path);
int surround_write_status_json(const char *json, const char *path);
#endif
