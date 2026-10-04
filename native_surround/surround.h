/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FXROUTE_SURROUND_H
#define FXROUTE_SURROUND_H
#include <stddef.h>
#include <stdint.h>

#define SURROUND_CHANNELS 8
#define SURROUND_RATE 48000
#define SURROUND_LOOKAHEAD 4096
#define SURROUND_SILENCE_RESET 8192
#define SURROUND_LIVE_LATENCY 8192

typedef struct surround surround;
typedef void (*surround_emit)(void *data, uint64_t frame, const float pcm[8]);
extern const char *const surround_positions[8];
surround *surround_create(surround_emit emit, void *data);
void surround_destroy(surround *decoder);
void surround_push(surround *decoder, const float *stereo, size_t frames);
void surround_finish(surround *decoder);
/* Discontinuities require a fresh valid burst or a full silence reset. */
void surround_reset(surround *decoder);
int surround_status_json(const surround *decoder, char *buffer, size_t size);
int surround_write_status(const surround *decoder, const char *path);
#endif
