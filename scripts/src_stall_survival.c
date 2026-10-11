/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <dlfcn.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "resample-worker.h"

/* A healthy but stalled engine (scheduling stall, catch-up callbacks) must
 * not wedge the stream: the composition conceals with counted silence and
 * resumes real output once supply returns. A dead engine still fails. */
static pid_t find_sox_child(void)
{
    DIR *tasks = opendir("/proc/self/task");
    if (!tasks) return -1;
    struct dirent *entry;
    pid_t found = -1;
    while ((entry = readdir(tasks))) {
        char path[512], actual[PATH_MAX];
        snprintf(path, sizeof(path), "/proc/self/task/%s/children", entry->d_name);
        FILE *file = fopen(path, "r");
        if (!file) continue;
        int pid;
        while (fscanf(file, "%d", &pid) == 1) {
            snprintf(path, sizeof(path), "/proc/%d/exe", pid);
            ssize_t size = readlink(path, actual, sizeof(actual) - 1);
            if (size <= 0) continue;
            actual[size] = 0;
            char *name = strrchr(actual, '/');
            if (name && !strcmp(name + 1, "sox")) found = pid;
        }
        fclose(file);
    }
    closedir(tasks);
    return found;
}

static void check(int condition, const char *what)
{
    if (!condition) { printf("STALL-SURVIVAL-FAIL %s\n", what); fflush(stdout); exit(3); }
}

int main(int argc, char **argv)
{
    check(argc == 2, "usage: stall-survival <library>");
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    check(library != NULL, "dlopen library");
    int (*init)(struct resample *) = dlsym(library, "fx_resample_init");
    int (*error)(struct resample *) = dlsym(library, "fx_resample_error");
    void (*stats)(struct resample *, struct fx_resample_stats *) = dlsym(library, "fx_resample_stats");
    check(init && error && stats, "dlsym composition entry points");

    struct resample r = {.channels = 2, .i_rate = 44100, .o_rate = 192000, .quality = 4};
    check(!init(&r), "init 44.1kHz to 192kHz composition");
    static float inbuf[2][512], outbuf[2][2048];
    for (unsigned c = 0; c < 2; ++c)
        for (unsigned i = 0; i < 512; ++i) inbuf[c][i] = (float)i / 512.0f;
    const void *src[2] = {inbuf[0], inbuf[1]};
    void *dst[2] = {outbuf[0], outbuf[1]};
    struct fx_resample_stats s;

    for (unsigned n = 0; n < 300; ++n) {
        uint32_t take = 235, made = 1024;
        resample_update_rate(&r, 1.0);
        resample_process(&r, src, &take, dst, &made);
        check(!error(&r), "warmup stays error free");
        usleep(5000);
    }
    stats(&r, &s);
    check(s.real_frames > 100000, "warmup reaches real output");
    check(!s.worker.error, "worker stays healthy during warmup");
    uint64_t real_before = s.real_frames;

    pid_t sox = find_sox_child();
    check(sox > 0, "worker engine child exists");
    check(!kill(sox, SIGSTOP), "stall the healthy engine");
    for (unsigned n = 0; n < 200; ++n) {
        uint32_t take = 235, made = 1024;
        resample_update_rate(&r, 1.0);
        resample_process(&r, src, &take, dst, &made);
        check(!error(&r), "stalled engine stays error free");
        usleep(5000);
    }
    stats(&r, &s);
    check(!s.worker.error, "stalled engine reports no worker error");
    check(s.underruns > 10, "stall is concealed with counted silence");
    check(s.lead_frames == 144000 && s.reserve_frames == 144000, "startup reserve stays frozen");
    check(!kill(sox, SIGCONT), "resume the engine");

    for (unsigned n = 0; n < 200; ++n) {
        uint32_t take = 235, made = 1024;
        resample_update_rate(&r, 1.0);
        resample_process(&r, src, &take, dst, &made);
        check(!error(&r), "recovery stays error free");
        usleep(5000);
    }
    stats(&r, &s);
    check(!error(&r) && !s.worker.error, "recovered stream is healthy");
    check(s.real_frames > real_before + 50000, "real output resumes after the stall");
    printf("STALL-SURVIVAL-OK underruns=%u real=%llu lead=%llu\n",
        s.underruns, (unsigned long long)s.real_frames, (unsigned long long)s.lead_frames);
    resample_free(&r);
    dlclose(library);
    return 0;
}
