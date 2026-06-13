#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT_DIR/pipewire_stage1/fxroute_21_passthrough.c"
OUT_DIR="$ROOT_DIR/pipewire_stage1/build"
OUT="$OUT_DIR/fxroute_21_passthrough"

if ! command -v pkg-config >/dev/null 2>&1; then
  echo "Missing dependency: pkg-config" >&2
  exit 2
fi

missing=()
if ! pkg-config --exists libpipewire-0.3; then
  missing+=("pkg-config module libpipewire-0.3 (package: libpipewire-0.3-dev on Debian/Ubuntu, pipewire-devel on Fedora/openSUSE)")
fi
if ! pkg-config --exists libspa-0.2; then
  missing+=("pkg-config module libspa-0.2 (package: libspa-0.2-dev on Debian/Ubuntu, pipewire-devel on Fedora/openSUSE)")
fi
if ! command -v gcc >/dev/null 2>&1; then
  missing+=("gcc")
fi

if [ "${#missing[@]}" -ne 0 ]; then
  echo "Cannot compile PipeWire helper on this host. Missing:" >&2
  printf ' - %s\n' "${missing[@]}" >&2
  exit 2
fi

mkdir -p "$OUT_DIR"
gcc -std=c11 -Wall -Wextra -O2 -g \
  $(pkg-config --cflags libpipewire-0.3 libspa-0.2) \
  "$SRC" \
  $(pkg-config --libs libpipewire-0.3 libspa-0.2) \
  -lm \
  -o "$OUT"

echo "Built: $OUT"
