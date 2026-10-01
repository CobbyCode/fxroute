#ifndef FXROUTE_TIMING_H
#define FXROUTE_TIMING_H
#include <stdatomic.h>
#include <stdint.h>

#define FX_TIMING_BINS 1024U
typedef struct {
    _Atomic uint32_t bins[FX_TIMING_BINS];
    _Atomic uint64_t maximum;
    _Atomic uint32_t reset_epoch;
} fx_timing;
typedef struct {
    uint64_t samples, p50_ns, p99_ns, max_ns;
} fx_timing_summary;
int fx_timing_available(const fx_timing *timing);
void fx_timing_record(fx_timing *timing, uint64_t nanoseconds);
fx_timing_summary fx_timing_read(const fx_timing *timing);
/* Reset on the sole producer, with no concurrent record calls. Readers may
 * run concurrently and retry a snapshot crossing a reset generation. */
void fx_timing_reset(fx_timing *timing);
#endif
