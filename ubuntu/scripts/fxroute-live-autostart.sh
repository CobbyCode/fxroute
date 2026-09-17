#!/usr/bin/env bash
# FXRoute live-session autostart (Casper "Try" mode, RAM overlay only).
# Runs once per live login as the live user. Never touches internal disks,
# never runs the installer or first-boot. Installs FXRoute into the live
# session (deps are pre-baked in the squashfs) and opens the kiosk.
set -Eeuo pipefail

MARKER="$HOME/.local/share/fxroute/live-ready"
LOG="$HOME/.local/share/fxroute/live-autostart.log"

mkdir -p "$(dirname "$MARKER")"
[[ -f "$MARKER" ]] && exit 0
: > "$LOG"

note() { printf '%s\n' "$*" >> "$LOG" 2>/dev/null || true; }

note "FXRoute live autostart start"

if [[ ! -f "$HOME/fxroute/main.py" ]]; then
  note "running install.sh in live session"
  if [[ -d /opt/fxroute-iso/source ]]; then
    bash /opt/fxroute-iso/source/install.sh \
      --source /opt/fxroute-iso/source \
      --target "$HOME/fxroute" \
      --providers none \
      --yes >> "$LOG" 2>&1 || note "install.sh failed (see log)"
  else
    note "no FXRoute source payload on live medium"
  fi
fi

systemctl --user start fxroute.service 2>/dev/null || note "fxroute.service start failed"

touch "$MARKER"
note "FXRoute live autostart done"
