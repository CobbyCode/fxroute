#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
DEPS="libpipewire-0.3 libavcodec libavutil libswresample"
${CC:-cc} -std=gnu11 ${CFLAGS:--O2} -Wall -Wextra -Werror \
    $(pkg-config --cflags $DEPS) surround.c ingress.c \
    $(pkg-config --libs $DEPS) -lm -pthread -o build/fxroute-surround-input
