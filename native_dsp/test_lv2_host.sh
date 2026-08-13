#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! pkg-config --exists lilv-0; then
    printf '%s\n' 'LV2 host test skipped: lilv-0 development files unavailable'
    exit 77
fi
mkdir -p build
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic \
    $(pkg-config --cflags lilv-0) lv2_host.c test_lv2_host.c \
    $(pkg-config --libs lilv-0) -lm -o build/test-lv2-host
./build/test-lv2-host
