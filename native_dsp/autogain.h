#ifndef FXROUTE_AUTOGAIN_H
#define FXROUTE_AUTOGAIN_H

#include <stddef.h>

typedef struct fx_autogain fx_autogain;

typedef enum {
    FX_AUTOGAIN_MOMENTARY = 0,
    FX_AUTOGAIN_SHORTTERM,
    FX_AUTOGAIN_INTEGRATED,
    FX_AUTOGAIN_GEOMETRIC_MEAN_MSI,
    FX_AUTOGAIN_GEOMETRIC_MEAN_MS,
    FX_AUTOGAIN_GEOMETRIC_MEAN_MI,
    FX_AUTOGAIN_GEOMETRIC_MEAN_SI
} fx_autogain_reference;

typedef struct {
    fx_autogain_reference reference;
    double target_lufs;
    double silence_threshold_lufs;
    unsigned maximum_history_seconds;
} fx_autogain_config;

typedef struct {
    double gain;
    double reference_lufs;
    double momentary_lufs;
    double shortterm_lufs;
    double integrated_lufs;
    double relative_threshold_lufs;
    double loudness_range_lu;
} fx_autogain_measurement;

fx_autogain_config fx_autogain_default_config(void);
fx_autogain *fx_autogain_init(unsigned sample_rate, size_t maximum_block_frames,
                              const fx_autogain_config *config);
void fx_autogain_free(fx_autogain *autogain);
int fx_autogain_process(fx_autogain *autogain, const float *left, const float *right,
                        float *out_left, float *out_right, size_t frames);
fx_autogain_measurement fx_autogain_get_measurement(const fx_autogain *autogain);

#endif
