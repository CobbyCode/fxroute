#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[easyeffects-stale-watchdog] %s\n' "$*"
}

USER_ID="$(id -u)"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${USER_ID}}"
SOCKET_PATH="${RUNTIME_DIR}/.flatpak/com.github.wwmm.easyeffects/xdg-run/EasyEffectsServer"
ALT_SOCKET_PATH="${RUNTIME_DIR}/.flatpak/com.github.wwmm.easyeffects/tmp/EasyEffectsServer"
LOCK_PATH="${RUNTIME_DIR}/.flatpak/com.github.wwmm.easyeffects/xdg-run/easyeffects.lock"
ALT_LOCK_PATH="${RUNTIME_DIR}/.flatpak/com.github.wwmm.easyeffects/tmp/easyeffects.lock"
FLATPAK_APP="com.github.wwmm.easyeffects"
START_ERR="/tmp/easyeffects-stale-watchdog.err"
START_OUT="/tmp/easyeffects-stale-watchdog.out"

session_env="$(systemctl --user show-environment 2>/dev/null || true)"
DISPLAY_VALUE="${DISPLAY:-$(printf '%s\n' "$session_env" | sed -n 's/^DISPLAY=//p' | head -n1)}"
WAYLAND_VALUE="${WAYLAND_DISPLAY:-$(printf '%s\n' "$session_env" | sed -n 's/^WAYLAND_DISPLAY=//p' | head -n1)}"
BUS_VALUE="${DBUS_SESSION_BUS_ADDRESS:-$(printf '%s\n' "$session_env" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p' | head -n1)}"
if [[ -z "$BUS_VALUE" ]]; then
  BUS_VALUE="unix:path=${RUNTIME_DIR}/bus"
fi

has_easyeffects_process() {
  pgrep -u "$USER_ID" -f 'easyeffects --gapplication-service' >/dev/null 2>&1
}

has_easyeffects_sink() {
  pw-cli ls Node 2>/dev/null | grep -q 'node.name = "easyeffects_sink"'
}

socket_has_owner() {
  if [[ ! -S "$SOCKET_PATH" ]]; then
    return 1
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof "$SOCKET_PATH" >/dev/null 2>&1
    return $?
  fi
  return 1
}

if [[ ! -S "$SOCKET_PATH" && -S "$ALT_SOCKET_PATH" ]]; then
  SOCKET_PATH="$ALT_SOCKET_PATH"
  LOCK_PATH="$ALT_LOCK_PATH"
fi

if [[ ! -S "$SOCKET_PATH" ]]; then
  log "No EasyEffects socket present, nothing to do"
  exit 0
fi

if has_easyeffects_process; then
  log "EasyEffects process is running, nothing to do"
  exit 0
fi

if has_easyeffects_sink; then
  log "EasyEffects sink is still present in PipeWire, nothing to do"
  exit 0
fi

if socket_has_owner; then
  log "Socket still has an owner, refusing stale recovery"
  exit 0
fi

log "Detected stale EasyEffects runtime socket without process or sink, attempting recovery"
rm -f "$SOCKET_PATH" "$LOCK_PATH"

if [[ -z "$DISPLAY_VALUE" && -z "$WAYLAND_VALUE" ]]; then
  log "No DISPLAY or WAYLAND_DISPLAY found in user session env, aborting recovery"
  exit 1
fi

nohup env \
  DISPLAY="$DISPLAY_VALUE" \
  WAYLAND_DISPLAY="$WAYLAND_VALUE" \
  XDG_RUNTIME_DIR="$RUNTIME_DIR" \
  DBUS_SESSION_BUS_ADDRESS="$BUS_VALUE" \
  /usr/bin/flatpak run --command=easyeffects "$FLATPAK_APP" --gapplication-service \
  >"$START_OUT" 2>"$START_ERR" &

sleep 6

if has_easyeffects_process && has_easyeffects_sink; then
  log "Recovery succeeded, EasyEffects process and sink are back"
  exit 0
fi

log "Recovery failed, EasyEffects did not come back cleanly"
if [[ -f "$START_ERR" ]]; then
  tail -n 40 "$START_ERR" >&2 || true
fi
exit 1
