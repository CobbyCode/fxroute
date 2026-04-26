#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CLEANUP_SCRIPT="$SCRIPT_DIR/spotify-cache-cleanup.sh"

if [[ -x "$CLEANUP_SCRIPT" ]]; then
  "$CLEANUP_SCRIPT" || true
fi

if command -v flatpak >/dev/null 2>&1; then
  if flatpak list --app --columns=application 2>/dev/null | grep -Fxq "com.spotify.Client"; then
    exec flatpak run com.spotify.Client
  fi
fi

if command -v spotify >/dev/null 2>&1; then
  exec spotify
fi

echo "Spotify desktop app not found" >&2
exit 1
