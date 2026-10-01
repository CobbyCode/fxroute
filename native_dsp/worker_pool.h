#ifndef FXROUTE_WORKER_POOL_H
#define FXROUTE_WORKER_POOL_H

#include <stdint.h>

#define FX_WORKER_LIMIT 3U
typedef struct fx_workers fx_workers;
typedef struct {
    unsigned count;
    int require_rt, priority;
    void *(*create_thread)(void *(*function)(void *), void *argument, void *context);
    void (*join_thread)(void *thread, void *context);
    /* Negative errno on refusal; nonnegative may be an asynchronous request.
     * Kernel scheduler verification, not this return value, admits the pool. */
    int (*acquire_rt)(void *thread, int priority, void *context);
    void *context;
} fx_worker_policy;
typedef struct {
    uint64_t barrier_ns, wake_ns[FX_WORKER_LIMIT];
    unsigned wakes;
} fx_worker_sample;

/* Prepare/destroy only while the graph is not being processed. NULL is serial.
 * A live policy requires every helper to have verified RR/FIFO scheduling. */
fx_workers *fx_workers_create(const fx_worker_policy *policy);
void fx_workers_destroy(fx_workers *pool);
unsigned fx_workers_count(const fx_workers *pool);
int fx_workers_scheduler(const fx_workers *pool, int *priority);
/* One caller per pool. Jobs retain fixed lane ownership; return drains all
 * helpers before the context or any referenced channel state may be reused. */
fx_worker_sample fx_workers_run(fx_workers *pool, unsigned jobs,
                               void (*function)(void *, unsigned), void *context,
                               int measure);
uint64_t fx_workers_now_ns(void);

#endif
