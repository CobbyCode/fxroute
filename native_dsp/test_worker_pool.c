#define _POSIX_C_SOURCE 200809L
#include "worker_pool.h"
#include "timing.h"
#include <assert.h>
#include <fenv.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

struct jobs { unsigned count[32], phase; pthread_t owner[32]; };
static void run_job(void *context, unsigned job) {
    struct jobs *j = context;
    if (j->phase) assert(pthread_equal(j->owner[job], pthread_self()));
    else j->owner[job] = pthread_self();
    j->count[job]++;
}
static void check_rounding(void *context, unsigned job) {
    (void)context; (void)job;
    assert(fegetround() == FE_UPWARD);
}
static int refuse_rt(void *thread, int priority, void *context) {
    (void)thread; (void)priority; (void)context;
    return -1;
}
static int pretend_rt(void *thread, int priority, void *context) {
    (void)thread; (void)priority; (void)context;
    return 0;
}
struct backend { unsigned created, joined; pthread_t thread[3]; };
static void *limited_create(void *(*function)(void *), void *argument, void *context) {
    struct backend *b = context;
    if (b->created == 2) return NULL;
    pthread_t *thread = &b->thread[b->created++];
    assert(!pthread_create(thread, NULL, function, argument));
    return thread;
}
static void counted_join(void *thread, void *context) {
    struct backend *b = context;
    assert(!pthread_join(*(pthread_t *)thread, NULL));
    b->joined++;
}
int main(void) {
    fx_worker_policy policy = {.count = 3};
    for (unsigned cycle = 0; cycle < 20; cycle++) {
        fx_workers *pool = fx_workers_create(&policy);
        assert(pool && fx_workers_count(pool) == 3);
        struct jobs jobs = {0};
        for (jobs.phase = 0; jobs.phase < 1000; jobs.phase++) {
            fx_workers_run(pool, 8, run_job, &jobs, 0);
            for (unsigned i = 0; i < 8; i++) assert(jobs.count[i] == jobs.phase + 1);
        }
        assert(!pthread_equal(jobs.owner[0], jobs.owner[1]));
        assert(pthread_equal(jobs.owner[0], jobs.owner[4]));
        fx_workers_run(pool, 0, run_job, &jobs, 0);
        memset(&jobs, 0, sizeof jobs);
        fx_workers_run(pool, 1, run_job, &jobs, 0);
        assert(jobs.count[0] == 1 && jobs.count[1] == 0);
        fx_workers_destroy(pool);
    }
    policy.count = 100;
    assert(fx_workers_create(&policy) == NULL);
    policy.count = 3; policy.require_rt = 1; policy.priority = 20;
    policy.acquire_rt = refuse_rt;
    assert(fx_workers_create(&policy) == NULL);
    policy.acquire_rt = pretend_rt;
    assert(fx_workers_create(&policy) == NULL);
    struct backend backend = {0};
    fx_worker_policy partial = {.count=3, .create_thread=limited_create, .join_thread=counted_join, .context=&backend};
    assert(fx_workers_create(&partial) == NULL);
    assert(backend.created == 2 && backend.joined == 2);
    struct jobs serial = {0};
    fx_workers_run(NULL, 8, run_job, &serial, 0);
    for (unsigned i = 0; i < 8; i++) {
        assert(serial.count[i] == 1);
        assert(pthread_equal(serial.owner[i], pthread_self()));
    }
    puts("worker pool lifecycle, stable ownership, visibility and RT fallback passed");
    fx_timing timing = {0};
    assert(fx_timing_available(&timing));
    for (unsigned i = 1; i <= 10000; i++) fx_timing_record(&timing, (uint64_t)i * 1000);
    fx_timing_summary summary = fx_timing_read(&timing);
    assert(summary.samples == 10000 && summary.max_ns == 10000000);
    assert(summary.p99_ns >= 9900000 && summary.p99_ns <= 10000000);
    fx_timing_reset(&timing);
    assert(fx_timing_read(&timing).samples == 0);
    fx_worker_policy measured = {.count = 3};
    fx_workers *p = fx_workers_create(&measured);
    struct jobs measured_jobs = {0};
    fx_worker_sample sample = fx_workers_run(p, 8, run_job, &measured_jobs, 1);
    assert(sample.wakes == 3 && sample.barrier_ns > 0);
    for (unsigned i = 0; i < sample.wakes; i++) assert(sample.wake_ns[i] > 0);
    fx_workers_destroy(p);
    p = fx_workers_create(&measured);
    assert(!fesetround(FE_UPWARD));
    fx_workers_run(p, 8, check_rounding, NULL, 0);
    assert(!fesetround(FE_TONEAREST));
    fx_workers_destroy(p);
    return 0;
}
