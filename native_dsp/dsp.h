#ifndef FXROUTE_DSP_H
#define FXROUTE_DSP_H

#include <stddef.h>
#include <stdint.h>

#define FXDSP_MAX_CHANNELS 32

typedef struct fxdsp fxdsp;

fxdsp *fxdsp_load(const char *path, char *error, size_t error_size);
void fxdsp_free(fxdsp *dsp);
unsigned fxdsp_inputs(const fxdsp *dsp);
unsigned fxdsp_outputs(const fxdsp *dsp);
unsigned fxdsp_rate(const fxdsp *dsp);
void fxdsp_process(fxdsp *dsp, const float *const *input, float *const *output, size_t frames);
void fxdsp_process_tapped(fxdsp *dsp, const float *const *input, float *const *output,
                          float *const *post_effect, size_t frames);
void fxdsp_meter(const fxdsp *dsp, unsigned output, float *peak, float *rms);
void fxdsp_set_mute(fxdsp *dsp, uint32_t mask, int muted);
void fxdsp_set_effect_bypass(fxdsp *dsp, int bypassed);
int fxdsp_effect_bypass(const fxdsp *dsp);
void fxdsp_set_output_gain_db(fxdsp *dsp, float gain_db);
float fxdsp_output_gain_db(const fxdsp *dsp);
unsigned fxdsp_peaks(const fxdsp *dsp, float *peaks, unsigned count);
void fxdsp_reset_peaks(fxdsp *dsp);
int fxdsp_compatible(const fxdsp *a, const fxdsp *b);
int fxdsp_live_begin(fxdsp *dsp);
int fxdsp_live_control(fxdsp *dsp, const char *stage_id, const char *symbol, float value);
int fxdsp_live_param(fxdsp *dsp, const char *stage_id, const char *key, float value);
int fxdsp_live_matrix(fxdsp *dsp, unsigned output, unsigned input, float gain);
int fxdsp_live_peq(fxdsp *dsp, unsigned output, unsigned filter, const char *type, float frequency, float q, float gain_db);
int fxdsp_live_output(fxdsp *dsp, unsigned output, float gain_db, float delay_ms, int invert);
int fxdsp_live_commit(fxdsp *dsp);

#endif
