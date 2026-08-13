#!/bin/sh
set -eu
cd "$(dirname "$0")"
./build.sh
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
printf 'rate 48000\ninputs 2\noutputs 2\nmatrix 0 0 1\nmatrix 1 1 1\n' > "$tmp/test.conf"
dd if=/dev/zero of="$tmp/in.f32" bs=8 count=16 status=none
./build/fxroute-dsp-offline "$tmp/test.conf" "$tmp/in.f32" "$tmp/out.f32" >/dev/null
test "$(wc -c < "$tmp/out.f32")" -eq 128
printf '%s\n' 'native DSP offline self-test passed'
