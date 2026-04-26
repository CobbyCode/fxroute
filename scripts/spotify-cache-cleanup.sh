#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail

spotify_running() {
  if command -v flatpak >/dev/null 2>&1; then
    if flatpak ps --columns=application 2>/dev/null | grep -qx "com.spotify.Client"; then
      return 0
    fi
  fi

  if pgrep -xu "$(id -un)" spotify >/dev/null 2>&1; then
    return 0
  fi

  return 1
}

if spotify_running; then
  echo "Spotify is running; skipping cache cleanup"
  exit 0
fi

cache_dirs=(
  "$HOME/.cache/spotify/Storage"
  "$HOME/.cache/spotify/Data"
  "$HOME/.cache/spotify/Browser"
  "$HOME/.config/spotify/PersistentCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/Default/Cache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/Default/Code Cache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/Default/GPUCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/Default/DawnWebGPUCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/Default/DawnGraphiteCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/Default/Shared Dictionary/cache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/ShaderCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/GrShaderCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/GraphiteDawnCache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/component_crx_cache"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/extensions_crx_cache"
  "$HOME/.var/app/com.spotify.Client/cache/mesa_shader_cache"
  "$HOME/.var/app/com.spotify.Client/cache/tmp"
)

cache_files=(
  "$HOME/.var/app/com.spotify.Client/cache/spotify/BrowserMetrics-spare.pma"
  "$HOME/.var/app/com.spotify.Client/cache/spotify/chrome_debug.log"
)

cleaned_any=0
for dir in "${cache_dirs[@]}"; do
  [[ -d "$dir" ]] || continue
  find "$dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  cleaned_any=1
done

for file in "${cache_files[@]}"; do
  [[ -e "$file" ]] || continue
  rm -f "$file"
  cleaned_any=1
done

if [[ $cleaned_any -eq 1 ]]; then
  echo "Spotify cache cleanup completed"
else
  echo "No Spotify cache directories found"
fi
