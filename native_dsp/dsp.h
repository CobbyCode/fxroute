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
void fxdsp_meter(const fxdsp *dsp, unsigned output, float *peak, float *rms);
void fxdsp_set_mute(fxdsp *dsp, uint32_t mask, int muted);
unsigned fxdsp_peaks(const fxdsp *dsp, float *peaks, unsigned count);
void fxdsp_reset_peaks(fxdsp *dsp);

#endif
