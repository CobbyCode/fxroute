#define _GNU_SOURCE
#include <pipewire/pipewire.h>
#include <pipewire/thread.h>
#include "crystalizer.h"
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/time.h>
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

struct status_backend { unsigned created, joined; pthread_t thread; };
static void *status_create(void *(*function)(void *), void *argument, void *context) {
    struct status_backend *backend = context;
    backend->created++;
    assert(!pthread_create(&backend->thread, NULL, function, argument));
    return &backend->thread;
}
static void status_join(void *thread, void *context) {
    struct status_backend *backend = context;
    assert(!pthread_join(*(pthread_t *)thread, NULL)); backend->joined++;
}
static int status_refuse_rt(void *thread, int priority, void *context) {
    (void)thread; (void)priority; (void)context;
    return -EPERM;
}
static fxdsp *status_load(const char *path) {
    char error[256]; fxdsp *dsp = fxdsp_load(path, error, sizeof error); assert(dsp); return dsp;
}
static void status_reply(struct engine *engine, const char *expected) {
    int sockets[2]; assert(!socketpair(AF_UNIX, SOCK_DGRAM, 0, sockets));
    struct timeval timeout = {.tv_sec=1};
    assert(!setsockopt(sockets[1], SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout));
    engine->control_fd = sockets[0]; reply_timing(engine, NULL, 0);
    char reply[1024]; ssize_t size = recv(sockets[1], reply, sizeof reply - 1, 0);
    assert(size > 0); reply[size] = '\0'; assert(strstr(reply, expected));
    assert(strstr(reply, "\"callback\":{") && strstr(reply, "\"barrier\":{") && strstr(reply, "\"wake\":{"));
    close(sockets[0]); close(sockets[1]); engine->control_fd = -1;
}
static int test_status(const char *serial, const char *parallel) {
    struct engine engine = {.control_fd=-1};
    struct status_backend backend = {0};
    fx_worker_policy policy = {.count=1, .create_thread=status_create, .join_thread=status_join, .context=&backend};
    fxdsp *dsp = status_load(serial);
    atomic_init(&engine.dsp, dsp); atomic_init(&engine.processing, 0);
    assert(!setenv("FXROUTE_DSP_WORKERS", "0", 1));
    engine.worker_limit = fxdsp_engine_worker_limit(dsp, &policy);
    policy.count = engine.worker_limit; engine.workers = fx_workers_create(&policy);
    assert(!engine.worker_limit && !backend.created); prepare_graph(&engine, dsp);
    status_reply(&engine, "\"workers\":0,\"worker_limit\":0,\"worker_jobs\":1,\"worker_used\":0,\"worker_mode\":\"disabled\"");
    assert(!setenv("FXROUTE_DSP_WORKERS", "3", 1)); policy.count = 1;
    engine.worker_limit = fxdsp_engine_worker_limit(dsp, &policy);
    assert(engine.worker_limit == 1);
    engine.workers = fx_workers_create(&policy); assert(engine.workers); prepare_graph(&engine, dsp);
    status_reply(&engine, "\"workers\":1,\"worker_limit\":1,\"worker_jobs\":1,\"worker_used\":0,\"worker_mode\":\"serial\"");
    assert(fxdsp_swap_config(&engine, parallel, -30));
    status_reply(&engine, "\"workers\":1,\"worker_limit\":1,\"worker_jobs\":4,\"worker_used\":1,\"worker_mode\":\"pool\"");
    assert(fxdsp_swap_config(&engine, serial, -30));
    assert(backend.created == 1 && !backend.joined);
    fxdsp_free(atomic_load(&engine.dsp)); fx_workers_destroy(engine.workers);
    assert(backend.joined == 1);
    backend = (struct status_backend){0}; dsp = status_load(serial); atomic_store(&engine.dsp, dsp);
    policy.require_rt = 1; policy.acquire_rt = status_refuse_rt;
    engine.worker_limit = fxdsp_engine_worker_limit(dsp, &policy);
    engine.workers = fx_workers_create(&policy); assert(!engine.workers); prepare_graph(&engine, dsp);
    status_reply(&engine, "\"workers\":0,\"worker_limit\":1,\"worker_jobs\":1,\"worker_used\":0,\"worker_mode\":\"degraded\"");
    assert(backend.created == 1 && backend.joined == 1);
    assert(fxdsp_swap_config(&engine, parallel, -30));
    status_reply(&engine, "\"workers\":0,\"worker_limit\":1,\"worker_jobs\":4,\"worker_used\":0,\"worker_mode\":\"degraded\"");
    assert(backend.created == 1 && backend.joined == 1);
    fxdsp_free(atomic_load(&engine.dsp));
    dsp = status_load(serial); atomic_store(&engine.dsp, dsp);
    engine.workers = start_workers(&engine, dsp); prepare_graph(&engine, dsp);
    assert(engine.worker_limit > 0 && !engine.workers);
    status_reply(&engine, "\"worker_mode\":\"degraded\"");
    fxdsp_free(dsp);
    assert(!unsetenv("FXROUTE_DSP_WORKERS"));
    puts("worker status disabled/serial/pool/degraded tests passed");
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 4 && !strcmp(argv[1], "--test-status")) return test_status(argv[2], argv[3]);
    int result = engine_main(argc, argv);
    fprintf(stderr, "FXDSP_TEST_COUNTS {\"created\":%u,\"admitted\":%u,\"joined\":%u}\n",
            created, admitted, joined);
    return result;
}
