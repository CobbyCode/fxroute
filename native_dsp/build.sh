#!/bin/sh
set -eu
cd "$(dirname "$0")"
mkdir -p build
${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pedantic dsp.c offline.c -lm -o build/fxroute-dsp-offline
if pkg-config --exists libpipewire-0.3; then
    ${CC:-cc} -std=gnu11 -O2 -Wall -Wextra -Werror $(pkg-config --cflags libpipewire-0.3) dsp.c pipewire_engine.c $(pkg-config --libs libpipewire-0.3) -lm -pthread -o build/fxroute-dsp
else
    printf '%s\n' 'libpipewire-0.3 headers unavailable; built offline self-test only' >&2
fi
