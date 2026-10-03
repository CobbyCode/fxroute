/* SPDX-License-Identifier: AGPL-3.0-only */
#ifndef FX_WORKER_H
#define FX_WORKER_H
#include <stdint.h>
enum fx_profile { FX_LOW, FX_STANDARD, FX_HIGH };
/* Upper bound for worker channels; callers size per-channel tables from it. */
#define FX_WORKER_MAX_CHANNELS 32
struct fx_worker;
struct fx_worker_stats {
    uint64_t accepted, produced, delivered, max_input, max_output;
    /* Engine first-output latency in this generation: first produced frame
     * after the first accepted frame; 0 until the engine produced output. */
    uint64_t first_output_ns;
    unsigned generation, acknowledged, spawns;
    int error;
};
struct fx_worker *fx_worker_create(enum fx_profile, unsigned, unsigned, unsigned, unsigned);
unsigned fx_worker_push(struct fx_worker *, const float *const *, unsigned);
unsigned fx_worker_pull(struct fx_worker *, float *const *, unsigned);
unsigned fx_worker_available(struct fx_worker *);
void fx_worker_finish(struct fx_worker *);
void fx_worker_reset(struct fx_worker *);
int fx_worker_finished(struct fx_worker *);
int fx_worker_error(struct fx_worker *);
void fx_worker_stats(struct fx_worker *, struct fx_worker_stats *);
void fx_worker_destroy(struct fx_worker *);
#endif
