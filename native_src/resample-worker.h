/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FX_RESAMPLE_WORKER_H
#define FX_RESAMPLE_WORKER_H
#include "resample.h"
#include "worker.h"
struct fx_resample_stats {
    /* Diagnostic fields are sampled independently across processing threads. */
    struct fx_worker_stats worker;
    uint64_t lead_frames, real_frames;
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
