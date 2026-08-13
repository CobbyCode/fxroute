#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
EBUR128_CFLAGS="$(pkg-config --cflags libebur128)"
EBUR128_LIBS="$(pkg-config --libs libebur128)"
LILV_CFLAGS="$(pkg-config --cflags lilv-0)"
LILV_LIBS="$(pkg-config --libs lilv-0)"
SAMPLERATE_CFLAGS="$(pkg-config --cflags samplerate)"
SAMPLERATE_LIBS="$(pkg-config --libs samplerate)"
SPEEXDSP_CFLAGS="$(pkg-config --cflags speexdsp)"
SPEEXDSP_LIBS="$(pkg-config --libs speexdsp)"
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic $EBUR128_CFLAGS $LILV_CFLAGS $SAMPLERATE_CFLAGS $SPEEXDSP_CFLAGS dsp.c autogain.c crystalizer.c lv2_host.c offline.c $EBUR128_LIBS $LILV_LIBS $SAMPLERATE_LIBS $SPEEXDSP_LIBS -lm -pthread -o build/fxroute-dsp-offline
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic $EBUR128_CFLAGS autogain.c test_autogain.c $EBUR128_LIBS -lm -pthread -o build/test-autogain
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic $SPEEXDSP_CFLAGS crystalizer.c test_crystalizer.c $SPEEXDSP_LIBS -lm -o build/test-crystalizer
./build/test-autogain
./build/test-crystalizer
if pkg-config --exists libpipewire-0.3; then
    ${CC:-cc} -std=gnu11 -O2 -Wall -Wextra -Werror $(pkg-config --cflags libpipewire-0.3) $EBUR128_CFLAGS $LILV_CFLAGS $SAMPLERATE_CFLAGS $SPEEXDSP_CFLAGS dsp.c autogain.c crystalizer.c lv2_host.c pipewire_engine.c $(pkg-config --libs libpipewire-0.3) $EBUR128_LIBS $LILV_LIBS $SAMPLERATE_LIBS $SPEEXDSP_LIBS -lm -pthread -o build/fxroute-dsp
else
    printf '%s\n' 'libpipewire-0.3 headers unavailable; built offline self-test only' >&2
fi
