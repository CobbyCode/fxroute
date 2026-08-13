#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
SPEEXDSP_CFLAGS="$(pkg-config --cflags speexdsp)"
SPEEXDSP_LIBS="$(pkg-config --libs speexdsp)"
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic $SPEEXDSP_CFLAGS \
    crystalizer.c test_crystalizer_benchmark.c $SPEEXDSP_LIBS -lm -o build/benchmark-crystalizer
exec ./build/benchmark-crystalizer "${CRYSTALIZER_BENCHMARK_LIMIT:-5}"
