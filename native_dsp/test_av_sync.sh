#!/bin/sh
set -eu
cd "$(dirname "$0")"
packages="libebur128 lilv-0 samplerate speexdsp"
if ! pkg-config --exists $packages; then
    printf '%s\n' 'AV Sync test skipped: native DSP development files unavailable'
    exit 77
fi
mkdir -p build
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic \
    $(pkg-config --cflags $packages) \
    dsp.c worker_pool.c timing.c autogain.c crystalizer.c upmix_fast.c upmix_hq.c lv2_host.c test_av_sync.c \
    $(pkg-config --libs $packages) -lm -pthread -o build/test-av-sync
./build/test-av-sync
