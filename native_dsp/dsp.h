#ifndef FXROUTE_DSP_H
#define FXROUTE_DSP_H

#include <stddef.h>
#include <stdint.h>
#include "worker_pool.h"
#include "timing.h"

#define FXDSP_MAX_CHANNELS 32

typedef struct fxdsp fxdsp;

/* Coherent audio-thread snapshot. Every numeric latency is in stream frames. */
typedef struct {
    uint64_t dsp, intentional, align, balance, sync, total, basis_known, unknown, mixed;
} fxdsp_output_latency;
typedef struct {
    uint64_t rate, outputs, reference, enabled, bypassed, input, inputs;
    uint64_t target, minimum, maximum, common, actual, limited, balance_limited;
    uint64_t balance_unavailable;
    fxdsp_output_latency output[FXDSP_MAX_CHANNELS];
    fxdsp_output_latency source[FXDSP_MAX_CHANNELS];
} fxdsp_latency;
int fxdsp_read_latency(const fxdsp *dsp, fxdsp_latency *latency);
/* Applies with the entire sync balance at the next audio block boundary. */
int fxdsp_set_input_latency(fxdsp *dsp, float milliseconds);
int fxdsp_live_avsync(fxdsp *dsp, float target_ms, unsigned reference);

fxdsp *fxdsp_load(const char *path, char *error, size_t error_size);
void fxdsp_free(fxdsp *dsp);
/* Outside processing only. NULL policy prepares a normal-priority offline
 * pool; live callers must supply require_rt and an RT acquisition policy. */
unsigned fxdsp_prepare_workers(fxdsp *dsp, const fx_worker_policy *policy);
/* Fixed-layout capacity for a live engine. The caller owns the returned pool.
 * Binding changes only an unpublished/stopped graph, never the borrowed pool.
 * All graphs using that pool must share one serialized processing caller. */
fx_workers *fxdsp_create_engine_workers(const fxdsp *dsp, const fx_worker_policy *policy);
/* Plan once before live creation, after environment/layout/CPU policy caps. */
unsigned fxdsp_engine_worker_limit(const fxdsp *dsp, const fx_worker_policy *policy);
void fxdsp_borrow_workers(fxdsp *dsp, fx_workers *pool);
unsigned fxdsp_worker_demand(const fxdsp *dsp);
/* Available capacity, not the number of helpers used in a particular phase. */
unsigned fxdsp_worker_count(const fxdsp *dsp);
int fxdsp_worker_scheduler(const fxdsp *dsp, int *priority);
enum { FXDSP_TIMING_CALLBACK, FXDSP_TIMING_BARRIER, FXDSP_TIMING_WAKE, FXDSP_TIMING_COUNT };
/* Returns NULL for an unknown metric. */
const char *fxdsp_timing_name(unsigned metric);
/* Enable only on a stopped graph. Reset requests a drained callback boundary
 * and waits at most 250ms on the non-RT control path. A stopped graph retains
 * a pending reset for its next callback; timeout never resets mid-job. */
int fxdsp_enable_timing(fxdsp *dsp, int enabled);
int fxdsp_timing_enabled(const fxdsp *dsp);
void fxdsp_begin_callback(fxdsp *dsp);
void fxdsp_record_callback(fxdsp *dsp, uint64_t nanoseconds);
fx_timing_summary fxdsp_read_timing(const fxdsp *dsp, unsigned metric);
int fxdsp_reset_timing(fxdsp *dsp);
unsigned fxdsp_inputs(const fxdsp *dsp);
unsigned fxdsp_outputs(const fxdsp *dsp);
unsigned fxdsp_rate(const fxdsp *dsp);
void fxdsp_process(fxdsp *dsp, const float *const *input, float *const *output, size_t frames);
void fxdsp_process_tapped(fxdsp *dsp, const float *const *input, float *const *output,
                          float *const *post_effect, size_t frames);
/* Read accumulated peak/RMS for an output.  Only safe when the graph is not
 * processed concurrently (offline/single-thread use); the live engine reads
 * peaks through the atomic fxdsp_peaks instead. */
void fxdsp_meter(const fxdsp *dsp, unsigned output, float *peak, float *rms);
void fxdsp_set_mute(fxdsp *dsp, uint32_t mask, int muted);
/* Zero the masked inputs before the stage chain (bit n = input n). */
void fxdsp_set_input_mute(fxdsp *dsp, uint32_t mask, int muted);
void fxdsp_set_effect_bypass(fxdsp *dsp, int bypassed);
int fxdsp_effect_bypass(const fxdsp *dsp);
void fxdsp_set_output_gain_db(fxdsp *dsp, float gain_db);
float fxdsp_output_gain_db(const fxdsp *dsp);
unsigned fxdsp_peaks(const fxdsp *dsp, float *peaks, unsigned count);
void fxdsp_reset_peaks(fxdsp *dsp);
int fxdsp_compatible(const fxdsp *a, const fxdsp *b);
int fxdsp_live_begin(fxdsp *dsp);
int fxdsp_live_abort(fxdsp *dsp);
int fxdsp_live_control(fxdsp *dsp, const char *stage_id, const char *symbol, float value);
int fxdsp_live_param(fxdsp *dsp, const char *stage_id, const char *key, float value);
int fxdsp_live_matrix(fxdsp *dsp, unsigned output, unsigned input, float gain);
int fxdsp_live_peq(fxdsp *dsp, unsigned output, unsigned filter, const char *type, float frequency, float q, float gain_db);
int fxdsp_live_output(fxdsp *dsp, unsigned output, float gain_db, float delay_ms, int invert);
int fxdsp_live_commit(fxdsp *dsp);

#endif
