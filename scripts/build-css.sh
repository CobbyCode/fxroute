#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/static/style.css"
cat "$ROOT/static/css/_tokens.css" \
    "$ROOT/static/css/_base.css" \
    "$ROOT/static/css/_layout.css" \
    "$ROOT/static/css/_overlays.css" \
    "$ROOT/static/css/_library.css" \
    "$ROOT/static/css/_radio.css" \
    "$ROOT/static/css/_streaming.css" \
    "$ROOT/static/css/_effects.css" \
    "$ROOT/static/css/_measurement.css" \
    "$ROOT/static/css/_playback.css" \
    "$ROOT/static/css/_responsive.css" >"$OUT"
echo "built $OUT ($(wc -l <"$OUT") lines)"
