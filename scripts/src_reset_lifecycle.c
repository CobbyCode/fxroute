/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dirent.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>
#include "resample-worker.h"

static void assert_adjacent_engine(const char *library)
{
    char expected[PATH_MAX], actual[PATH_MAX];
    assert(realpath(library, expected));
    char *name = strrchr(expected, '/'); assert(name);
    strcpy(name + 1, "sox");
    DIR *tasks = opendir("/proc/self/task"); assert(tasks);
    struct dirent *entry;
    int found = 0;
    while ((entry = readdir(tasks))) {
        char path[512];
        snprintf(path, sizeof(path), "/proc/self/task/%s/children", entry->d_name);
        FILE *file = fopen(path, "r");
        if (!file) continue;
        int pid;
        while (fscanf(file, "%d", &pid) == 1) {
            snprintf(path, sizeof(path), "/proc/%d/exe", pid);
            ssize_t size = readlink(path, actual, sizeof(actual) - 1);
            assert(size > 0); actual[size] = 0;
            assert(!strcmp(actual, expected) && "SRC must use the loaded generation's engine");
            found = 1;
        }
        fclose(file);
    }
    closedir(tasks);
    assert(found && "Unequal-rate SRC must start the pinned engine");
}

int main(int argc, char **argv)
{
    struct rlimit limit = {0, 0}; assert(!setrlimit(RLIMIT_CORE, &limit));
    assert(argc == 3);
    char loaded[PATH_MAX]; assert(realpath(argv[1], loaded));
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL); assert(library);
    int (*init)(struct resample *) = dlsym(library, "fx_resample_init"); assert(init);
    struct resample r = {.channels = 2, .i_rate = 44100, .o_rate = 192000, .quality = 4};
    if (!strcmp(argv[2], "missing-engine")) {
        assert(init(&r) < 0 && !r.data);
        resample_reset(&r);
        resample_reset(&r);
    } else if (!strcmp(argv[2], "relocated") || !strcmp(argv[2], "activation-race")) {
        assert(!init(&r));
        assert_adjacent_engine(loaded);
    } else {
        assert(!init(&r));
        resample_free(&r);
        resample_reset(&r);
        resample_reset(&r);
        /* Nominal conversion rejects >32 channels after installing callbacks. */
        r.channels = 33;
        assert(init(&r) < 0 && !r.data);
        resample_reset(&r);
        resample_free(&r);
        r.channels = 2;
        assert(!init(&r));
        resample_reset(&r);
    }
    resample_free(&r);
    dlclose(library);
    return 0;
}
