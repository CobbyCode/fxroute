#!/usr/bin/env bash
# FXRoute live-session entry (Casper "Try" mode, RAM overlay only).
# Started via /etc/xdg/autostart in the live GNOME session. Exits
# immediately on installed systems (no live medium there). Never touches
# internal disks, never runs the installer or first-boot.
set -Eeuo pipefail

# Live guard: only the Casper live session runs this. The stock Desktop
# kernel cmdline carries no boot=casper flag (casper defaults apply from
# the initrd), so the live medium mount decides, not the cmdline.
# FXROUTE_LIVE_FORCE=1 skips the guards (test hook, never set on the ISO).
if [[ "${FXROUTE_LIVE_FORCE:-0}" != 1 ]]; then
  if [[ ! -d /cdrom/casper && ! -d /cdrom/CASPER ]] \
    && ! grep -q 'boot=casper' /proc/cmdline 2>/dev/null; then
    exit 0
  fi
  # During an autoinstall run the live session belongs to the installer:
  # stay out of its way (the target gets FXRoute via first-boot).
  # (QEMU SSH introspection is a separate test-seed unit, not this script.)
  grep -q 'autoinstall' /proc/cmdline 2>/dev/null && exit 0
fi

MARKER="$HOME/.local/share/fxroute/live-ready"
LOG="$HOME/.local/share/fxroute/live-autostart.log"

mkdir -p "$(dirname "$MARKER")"
[[ -f "$MARKER" ]] && exit 0
: > "$LOG"

note() { printf '%s\n' "$*" >> "$LOG" 2>/dev/null || true; }

# Visible failure hint on the live desktop. Failures leave no MARKER, so the
# next login retries instead of sticking with a dead kiosk forever.
write_hint() {
  mkdir -p "$HOME/Desktop" 2>/dev/null || true
  printf '%s\n' \
    'FXRoute could not start: the backend did not come up on 127.0.0.1:8000.' \
    'Diagnosis: journalctl --user -u fxroute -n 50' \
    > "$HOME/Desktop/FXRoute-NOT-STARTED.txt" 2>/dev/null || true
}

# The autostart fires seconds after the desktop appears, routinely before
# NetworkManager is online. install.sh runs apt-get update/install once with
# no retry, so starting it offline guarantees "Failed to fetch" failures.
wait_for_network() {
  local timeout="${FXROUTE_LIVE_NETWORK_TIMEOUT:-600}" waited=0
  while (( waited < timeout )); do
    if command -v nm-online >/dev/null 2>&1; then
      nm-online -q -t 30 >/dev/null 2>&1 && return 0
    elif curl -fsI --max-time 10 http://archive.ubuntu.com/ >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
    waited=$(( waited + 5 ))
  done
  return 1
}

# Ubuntu's own updaters (unattended-upgrades, update-notifier) may hold the
# apt lock in the live session; installing against them fails or corrupts.
wait_for_apt_lock() {
  local timeout="${FXROUTE_LIVE_APT_TIMEOUT:-300}" waited=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
     || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do
    (( waited >= timeout )) && return 1
    sleep 5
    waited=$(( waited + 5 ))
  done
  return 0
}

# Mirror flakes and half-fetched lists are routine on first live boot.
# -n: there is no TTY to type a sudo password; live sudo is passwordless.
apt_update_with_retry() {
  local retries="${FXROUTE_LIVE_APT_RETRIES:-5}"
  local delay="${FXROUTE_LIVE_APT_RETRY_DELAY:-10}" attempt=1
  while (( attempt <= retries )); do
    if sudo -n apt-get update >>"$LOG" 2>&1; then
      return 0
    fi
    note "apt-get update attempt $attempt/$retries failed, retrying"
    sleep "$delay"
    attempt=$(( attempt + 1 ))
  done
  return 1
}

note "FXRoute live autostart start"

if [[ ! -f "$HOME/fxroute/main.py" ]]; then
  note "running install.sh in live session"
  if [[ -n "${FXROUTE_LIVE_SOURCE_DIR:-}" ]]; then
    src="$FXROUTE_LIVE_SOURCE_DIR"
  elif [[ -d /opt/fxroute-iso/source ]]; then
    src="/opt/fxroute-iso/source"
  elif [[ -d /cdrom/fxroute-iso ]]; then
    # Fallback: extract the shipped source into the RAM session.
    mkdir -p /tmp/fxroute-iso-source
    tar --extract --file /cdrom/fxroute-iso/source.tar \
      --directory /tmp/fxroute-iso-source --no-same-owner >> "$LOG" 2>&1 || true
    src="/tmp/fxroute-iso-source"
  else
    note "no FXRoute source payload on live medium"
    exit 0
  fi
  if [[ -f "$src/install.sh" ]]; then
    if ! wait_for_network; then
      note "no network for the live install; will retry on next login"
      write_hint
      exit 1
    fi
    if ! wait_for_apt_lock; then
      note "apt is locked by another process; will retry on next login"
      write_hint
      exit 1
    fi
    if ! apt_update_with_retry; then
      note "apt-get update kept failing; will retry on next login"
      write_hint
      exit 1
    fi
    if ! bash "$src/install.sh" \
      --source "$src" \
      --target "$HOME/fxroute" \
      --providers none \
      --yes >> "$LOG" 2>&1; then
      note "install.sh failed (see log)"
      write_hint
      exit 1
    fi
  fi
fi

if ! systemctl --user start fxroute.service 2>/dev/null; then
  note "fxroute.service start failed"
  write_hint
  exit 1
fi
touch "$MARKER"

# Wait for the backend (generous budget: first live boot compiles the DSP
# engine), then open the kiosk. This replaces the autostart process.
status_url="http://127.0.0.1:8000/api/status"
deadline=$(( SECONDS + ${FXROUTE_LIVE_BACKEND_TIMEOUT:-1500} ))
while (( SECONDS < deadline )); do
  remaining=$(( deadline - SECONDS ))
  request_timeout=$(( remaining < 30 ? remaining : 30 ))
  if curl --fail --silent --connect-timeout 5 \
      --max-time "$request_timeout" "$status_url" >/dev/null 2>&1; then
    note "backend ready, opening kiosk"
    exec firefox --kiosk http://127.0.0.1:8000/
  fi
  sleep 2
done
note "backend did not come up within budget"
write_hint
exec firefox --kiosk http://127.0.0.1:8000/
