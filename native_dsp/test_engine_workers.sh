#!/bin/sh
set -eu
cd "$(dirname "$0")"
packages="libpipewire-0.3 libebur128 lilv-0 samplerate speexdsp"
if ! pkg-config --exists $packages; then
    printf '%s\n' 'Worker status test skipped: native DSP/PipeWire development files unavailable'
    exit 77
fi
mkdir -p build
${CC:-cc} -std=gnu11 -O2 -Wall -Wextra -Werror \
    $(pkg-config --cflags $packages) \
    dsp.c worker_pool.c timing.c autogain.c crystalizer.c lv2_host.c test_engine_workers.c \
    $(pkg-config --libs $packages) -lm -pthread -Wl,--wrap=fx_crystalizer_process \
    -o build/test-engine-workers
directory=$(mktemp -d "${TMPDIR:-/tmp}/fxroute-worker-status.XXXXXX")
trap 'rm -f "$directory/serial.conf" "$directory/parallel.conf"; rmdir "$directory"' EXIT
printf 'rate 48000\ninputs 2\noutputs 4\nmatrix 0 0 1\nmatrix 1 1 1\n' > "$directory/serial.conf"
cp "$directory/serial.conf" "$directory/parallel.conf"
printf 'peq 0 bell 1000 0.7 1\n' >> "$directory/parallel.conf"
./build/test-engine-workers --test-status "$directory/serial.conf" "$directory/parallel.conf"
