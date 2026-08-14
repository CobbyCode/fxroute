#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! pkg-config --exists libebur128 lilv-0 samplerate speexdsp; then
    printf '%s\n' 'master gain test skipped: native DSP build dependencies unavailable'
    exit 77
fi
mkdir -p build
EBUR128_CFLAGS="$(pkg-config --cflags libebur128)"
EBUR128_LIBS="$(pkg-config --libs libebur128)"
LILV_CFLAGS="$(pkg-config --cflags lilv-0)"
LILV_LIBS="$(pkg-config --libs lilv-0)"
SAMPLERATE_CFLAGS="$(pkg-config --cflags samplerate)"
SAMPLERATE_LIBS="$(pkg-config --libs samplerate)"
SPEEXDSP_CFLAGS="$(pkg-config --cflags speexdsp)"
SPEEXDSP_LIBS="$(pkg-config --libs speexdsp)"
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic \
    $EBUR128_CFLAGS $LILV_CFLAGS $SAMPLERATE_CFLAGS $SPEEXDSP_CFLAGS \
    dsp.c autogain.c crystalizer.c lv2_host.c test_master_gain.c \
    $EBUR128_LIBS $LILV_LIBS $SAMPLERATE_LIBS $SPEEXDSP_LIBS \
    -lm -pthread -o build/test-master-gain
./build/test-master-gain
