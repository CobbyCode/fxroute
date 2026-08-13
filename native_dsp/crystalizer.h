#ifndef FXROUTE_CRYSTALIZER_H
#define FXROUTE_CRYSTALIZER_H

#include <stddef.h>

typedef struct fx_crystalizer fx_crystalizer;

/* EasyEffects 8.2.8 Crystalizer preset used by c9e4876. All processing state is
 * allocated at creation; process and reset perform no allocation, locking or I/O. */
fx_crystalizer *fx_crystalizer_create(unsigned rate);
void fx_crystalizer_destroy(fx_crystalizer *crystalizer);
void fx_crystalizer_reset(fx_crystalizer *crystalizer);
void fx_crystalizer_set_band_intensity_db(fx_crystalizer *crystalizer, size_t band, float db);
void fx_crystalizer_process(fx_crystalizer *crystalizer, const float *input, float *output, size_t frames);
size_t fx_crystalizer_latency(const fx_crystalizer *crystalizer);

size_t fx_crystalizer_band_count(void);
float fx_crystalizer_band_edge(size_t index);
float fx_crystalizer_base_intensity(const fx_crystalizer *crystalizer, size_t index);
float fx_crystalizer_adaptive_intensity(const fx_crystalizer *crystalizer, size_t index);
unsigned fx_crystalizer_oversampling_quality(const fx_crystalizer *crystalizer);
float fx_crystalizer_transition_band(const fx_crystalizer *crystalizer);

#endif
