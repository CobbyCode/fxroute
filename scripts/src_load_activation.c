/* SPDX-License-Identifier: AGPL-3.0-only */
#define _GNU_SOURCE
#include <assert.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* Switch the alias after mapping, before the plugin constructor resolves it. */
int dladdr(const void *address, Dl_info *info)
{
    int (*original)(const void *, Dl_info *) = dlsym(RTLD_NEXT, "dladdr");
    int result = original(address, info);
    const char *alias = getenv("FX_SRC_TEST_ALIAS");
    const char *next = getenv("FX_SRC_TEST_NEXT");
    if (result && alias && next && !strcmp(info->dli_fname, alias)) {
        char temporary[4096];
        assert(snprintf(temporary, sizeof(temporary), "%s.next", alias) < sizeof(temporary));
        assert(!symlink(next, temporary));
        assert(!rename(temporary, alias));
        unsetenv("FX_SRC_TEST_ALIAS");
    }
    return result;
}
