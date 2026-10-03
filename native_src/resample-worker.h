/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FX_RESAMPLE_WORKER_H
#define FX_RESAMPLE_WORKER_H
#include "resample.h"
#include "worker.h"
struct fx_resample_stats {
    /* Diagnostic fields are sampled independently across processing threads.
     * Within one generation a snapshot keeps reserve_frames <= lead_frames and
     * publishes each latency before the frame counter that depends on it. */
    struct fx_worker_stats worker;
    /* Silent output frames emitted before the first real output: the minimum
     * startup reserve plus any readiness extension. reserve_frames is the
     * minimum-reserve part; neither grows after real output begins. */
    uint64_t lead_frames, reserve_frames, real_frames;
    /* First real output latency from the first callback offering input;
     * 0 until real output begins. */
    uint64_t first_real_ns;
    unsigned underruns, input_pending, buffered;
};
int fx_resample_init(struct resample *);
void fx_resample_finish(struct resample *);
int fx_resample_finished(struct resample *);
int fx_resample_error(struct resample *);
void fx_resample_stats(struct resample *,struct fx_resample_stats *);
void fx_resample_note_callback(struct resample *,uint64_t);
const char *fx_resample_profile(struct resample *);
#endif
