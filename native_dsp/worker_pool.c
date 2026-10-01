#define _GNU_SOURCE
#include "worker_pool.h"

#include <errno.h>
#include <fenv.h>
#include <limits.h>
#include <sched.h>
#include <pthread.h>
#include <stdio.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <time.h>
#if defined(__linux__)
#include <linux/futex.h>
#include <sys/syscall.h>
#include <unistd.h>
#endif

struct worker {
    pthread_t thread;
    void *handle;
    struct fx_workers *pool;
    unsigned lane;
    _Atomic uint32_t command, done;
    uint64_t wake_ns;
    int tid, fp_ready;
};
struct fx_workers {
    struct worker worker[FX_WORKER_LIMIT];
    unsigned count, started, jobs;
    uint32_t sequence;
    _Atomic int stop;
    void (*function)(void *, unsigned);
    void *context;
    int measure, scheduler, priority;
    uint64_t dispatch_ns;
    fx_worker_policy policy;
    fenv_t environment;
};

uint64_t fx_workers_now_ns(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * UINT64_C(1000000000) + (uint64_t)t.tv_nsec;
}

#if defined(__linux__)
_Static_assert(sizeof(_Atomic uint32_t) == sizeof(uint32_t), "futex word size");
static void wake_word(_Atomic uint32_t *word) {
    (void)syscall(SYS_futex, word, FUTEX_WAKE_PRIVATE, 1, NULL, NULL, 0);
}
static void wait_word(_Atomic uint32_t *word, uint32_t value) {
    /* EAGAIN/EINTR/spurious returns are harmless: every caller rechecks the
     * acquire predicate. No callback mutex and no RT spin starving helpers. */
    (void)syscall(SYS_futex, word, FUTEX_WAIT_PRIVATE, value, NULL, NULL, 0);
}
static void *worker_main(void *context) {
    struct worker *w = context;
    fx_workers *p = w->pool;
    volatile unsigned char stack[65536];
    for (size_t i = 0; i < sizeof stack; i += 4096) stack[i] = 0;
    (void)stack[0];
    w->tid = (int)syscall(SYS_gettid);
    char name[16]; snprintf(name, sizeof name, "fxdsp-w%u", w->lane);
    (void)pthread_setname_np(pthread_self(), name);
    fenv_t environment;
    w->fp_ready = !fegetenv(&environment) && !fesetenv(&environment);
    uint32_t previous = 0;
    atomic_store_explicit(&w->done, 1, memory_order_release);
    wake_word(&w->done);
    for (;;) {
        uint32_t command = atomic_load_explicit(&w->command, memory_order_acquire);
        while (command == previous && !atomic_load_explicit(&p->stop, memory_order_acquire)) {
            wait_word(&w->command, previous);
            command = atomic_load_explicit(&w->command, memory_order_acquire);
        }
        if (atomic_load_explicit(&p->stop, memory_order_acquire)) break;
        w->wake_ns = p->measure ? fx_workers_now_ns() - p->dispatch_ns : 0;
        (void)fesetenv(&p->environment);
        for (unsigned job = w->lane; job < p->jobs; job += p->count + 1)
            p->function(p->context, job);
        previous = command;
        atomic_store_explicit(&w->done, command, memory_order_release);
        wake_word(&w->done);
    }
    return NULL;
}
#endif

void fx_workers_destroy(fx_workers *p) {
    if (!p) return;
#if defined(__linux__)
    atomic_store_explicit(&p->stop, 1, memory_order_release);
    for (unsigned i = 0; i < p->started; i++) {
        atomic_fetch_add_explicit(&p->worker[i].command, 1, memory_order_release);
        wake_word(&p->worker[i].command);
    }
    for (unsigned i = 0; i < p->started; i++) {
        if (p->policy.join_thread) p->policy.join_thread(p->worker[i].handle, p->policy.context);
        else pthread_join(p->worker[i].thread, NULL);
    }
#endif
    free(p);
}

fx_workers *fx_workers_create(const fx_worker_policy *policy) {
    if (!policy || !policy->count || policy->count > FX_WORKER_LIMIT) return NULL;
    if (!!policy->create_thread != !!policy->join_thread ||
        (policy->require_rt && policy->create_thread && !policy->acquire_rt)) return NULL;
#if !defined(__linux__)
    return NULL;
#else
    fx_workers *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    p->count = policy->count;
    p->policy = *policy;
    p->sequence = 1; /* done=1 is the initialization handshake. */
    for (unsigned i = 0; i < p->count; i++) {
        struct worker *w = &p->worker[i];
        w->pool = p; w->lane = i + 1;
        if (!atomic_is_lock_free(&w->command) || !atomic_is_lock_free(&w->done) || !atomic_is_lock_free(&p->stop)) goto fail;
        if (policy->create_thread) {
            w->handle = policy->create_thread(worker_main, w, policy->context);
            if (!w->handle) goto fail;
        } else {
            if (pthread_create(&w->thread, NULL, worker_main, w)) goto fail;
            w->handle = &w->thread;
        }
        p->started++;
        while (!atomic_load_explicit(&w->done, memory_order_acquire)) wait_word(&w->done, 0);
        if (!w->fp_ready) goto fail;
        if (policy->require_rt) {
            struct sched_param requested = {.sched_priority = policy->priority};
            int result = policy->acquire_rt
                ? policy->acquire_rt(w->handle, policy->priority, policy->context)
                : -pthread_setschedparam(w->thread, SCHED_RR, &requested);
            /* SPA may return a positive asynchronous request sequence. */
            if (result < 0) goto fail;
            int scheduler; struct sched_param actual;
            uint64_t deadline = fx_workers_now_ns() + UINT64_C(500000000);
            for (;;) {
                /* Broker grants may be asynchronous. Query the kernel TID,
                 * not pthread's cached policy; never poll from processing. */
                scheduler = sched_getscheduler(w->tid);
                if (scheduler < 0 || sched_getparam(w->tid, &actual)) goto fail;
                scheduler &= ~SCHED_RESET_ON_FORK;
                if ((scheduler == SCHED_RR || scheduler == SCHED_FIFO) && actual.sched_priority > 0 &&
                    (policy->priority < 0 || actual.sched_priority == policy->priority)) break;
                if (fx_workers_now_ns() >= deadline) goto fail;
                struct timespec pause = {.tv_nsec = 1000000}; nanosleep(&pause, NULL);
            }
            if (i && (p->scheduler != scheduler || p->priority != actual.sched_priority)) goto fail;
            p->scheduler = scheduler; p->priority = actual.sched_priority;
        } else {
            /* Do not inherit RT from an offline caller or graph-control thread. */
            struct sched_param normal = {.sched_priority = 0};
            int scheduler = sched_getscheduler(w->tid);
            if (scheduler < 0 || sched_setscheduler(w->tid, SCHED_OTHER | (scheduler & SCHED_RESET_ON_FORK), &normal)) goto fail;
        }
    }
    return p;
fail:
    fx_workers_destroy(p);
    return NULL;
#endif
}

unsigned fx_workers_count(const fx_workers *p) { return p ? p->count : 0; }
int fx_workers_scheduler(const fx_workers *p, int *priority) {
    *priority = p ? p->priority : 0;
    return p ? p->scheduler : SCHED_OTHER;
}

fx_worker_sample fx_workers_run(fx_workers *p, unsigned jobs,
                               void (*function)(void *, unsigned), void *context, int measure) {
    fx_worker_sample sample = {0};
    if (!p || jobs < 2) {
        for (unsigned job = 0; job < jobs; job++) function(context, job);
        return sample;
    }
#if defined(__linux__)
    unsigned active = jobs - 1 < p->count ? jobs - 1 : p->count;
    /* Preserve the processing caller's current rounding and denormal mode,
     * including a PipeWire data-loop environment unlike the control thread. */
    if (fegetenv(&p->environment)) {
        for (unsigned job = 0; job < jobs; job++) function(context, job);
        return sample;
    }
    uint32_t sequence = ++p->sequence;
    p->jobs = jobs; p->function = function; p->context = context; p->measure = measure;
    if (measure) p->dispatch_ns = fx_workers_now_ns();
    for (unsigned i = 0; i < active; i++) {
        atomic_store_explicit(&p->worker[i].command, sequence, memory_order_release);
        wake_word(&p->worker[i].command);
    }
    for (unsigned job = 0; job < jobs; job += p->count + 1) function(context, job);
    uint64_t barrier = measure ? fx_workers_now_ns() : 0;
    for (unsigned i = 0; i < active; i++) {
        struct worker *w = &p->worker[i];
        uint32_t done;
        while ((done = atomic_load_explicit(&w->done, memory_order_acquire)) != sequence)
            wait_word(&w->done, done);
        sample.wake_ns[i] = w->wake_ns;
    }
    if (measure) sample.barrier_ns = fx_workers_now_ns() - barrier;
    sample.wakes = active;
#endif
    return sample;
}
