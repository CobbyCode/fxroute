#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
packages="libebur128 lilv-0 samplerate speexdsp"
${CC:-cc} -std=c11 -O2 -g -Wall -Wextra -Werror -pedantic ${BENCHMARK_CFLAGS:-} \
    $(pkg-config --cflags $packages) dsp.c worker_pool.c timing.c autogain.c crystalizer.c lv2_host.c \
    benchmark_pipeline.c $(pkg-config --libs $packages) -lm -pthread -o build/benchmark-pipeline
exec ./build/benchmark-pipeline "$@"
