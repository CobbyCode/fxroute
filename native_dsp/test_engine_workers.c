#define _GNU_SOURCE
#include <pipewire/pipewire.h>
#include <pipewire/thread.h>
#include "crystalizer.h"
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

/* Observe real SPA operations without replacing their implementation. */
static unsigned created, admitted, joined;
static struct spa_thread *observed_create(struct spa_thread_utils *utils,
        const struct spa_dict *props, void *(*function)(void *), void *argument) {
    created++;
    fprintf(stderr, "FXDSP_TEST_EVENT create\n");
    return spa_thread_utils_create(utils, props, function, argument);
}
static int observed_acquire(struct spa_thread_utils *utils, struct spa_thread *thread, int priority) {
    admitted++;
    fprintf(stderr, "FXDSP_TEST_EVENT acquire\n");
    return spa_thread_utils_acquire_rt(utils, thread, priority);
}
static int observed_join(struct spa_thread_utils *utils, struct spa_thread *thread, void **result) {
    joined++;
    fprintf(stderr, "FXDSP_TEST_EVENT join\n");
    return spa_thread_utils_join(utils, thread, result);
}
static void observed_filter_destroy(struct pw_filter *filter) {
    fprintf(stderr, "FXDSP_TEST_EVENT filter_destroy_begin\n");
    pw_filter_destroy(filter);
    fprintf(stderr, "FXDSP_TEST_EVENT filter_destroy_end\n");
}
static int observed_quit(struct pw_main_loop *loop) {
    static const char event[] = "FXDSP_TEST_EVENT quit_requested\n";
    (void)write(STDERR_FILENO, event, sizeof event - 1);
    return pw_main_loop_quit(loop);
}

#define spa_thread_utils_create observed_create
#define spa_thread_utils_acquire_rt observed_acquire
#define spa_thread_utils_join observed_join
#define pw_filter_destroy observed_filter_destroy
#define pw_main_loop_quit observed_quit
#define main engine_main
#include "pipewire_engine.c"
#undef main

void __real_fx_crystalizer_process(fx_crystalizer *, const float *, float *, size_t);
void __wrap_fx_crystalizer_process(fx_crystalizer *c, const float *input, float *output, size_t frames) {
    static _Atomic int claimed;
    const char *entered = getenv("FXDSP_TEST_ENTERED"), *release = getenv("FXDSP_TEST_RELEASE");
    const char *armed = getenv("FXDSP_TEST_ARMED");
    char name[16]; int expected = 0;
    if (entered && release && armed && !access(armed, F_OK) &&
        !pthread_getname_np(pthread_self(), name, sizeof name) &&
        !strncmp(name, "fxdsp-w", 7) && atomic_compare_exchange_strong(&claimed, &expected, 1)) {
        FILE *file = fopen(entered, "w");
        if (!file) abort();
        fprintf(file, "%ld\n", syscall(SYS_gettid)); fclose(file);
        struct timespec pause = {.tv_nsec=1000000};
        uint64_t deadline = fx_workers_now_ns() + UINT64_C(10000000000);
        while (access(release, F_OK)) {
            if (fx_workers_now_ns() > deadline) abort();
            nanosleep(&pause, NULL);
        }
    }
    __real_fx_crystalizer_process(c, input, output, frames);
}

int main(int argc, char **argv) {
    int result = engine_main(argc, argv);
    fprintf(stderr, "FXDSP_TEST_COUNTS {\"created\":%u,\"admitted\":%u,\"joined\":%u}\n",
            created, admitted, joined);
    return result;
}
