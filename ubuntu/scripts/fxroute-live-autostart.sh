#!/usr/bin/env bash
# FXRoute live-session entry (Casper "Try" mode, RAM overlay only).
# Started via /etc/xdg/autostart in the live GNOME session. Exits
# immediately on installed systems (no live medium there). Never touches
# internal disks, never runs the installer or first-boot.
set -Eeuo pipefail

# Live guard: only the Casper live session runs this. The stock Desktop
# kernel cmdline carries no boot=casper flag (casper defaults apply from
# the initrd), so the live medium mount decides, not the cmdline.
if [[ ! -d /cdrom/casper && ! -d /cdrom/CASPER ]] \
  && ! grep -q 'boot=casper' /proc/cmdline 2>/dev/null; then
  exit 0
fi
# During an autoinstall run the live session belongs to the installer:
# stay out of its way (the target gets FXRoute via first-boot).
# (QEMU SSH introspection is a separate test-seed unit, not this script.)
grep -q 'autoinstall' /proc/cmdline 2>/dev/null && exit 0

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
    bash "$src/install.sh" \
      --source "$src" \
      --target "$HOME/fxroute" \
      --providers none \
      --yes >> "$LOG" 2>&1 || note "install.sh failed (see log)"
  fi
fi

systemctl --user start fxroute.service 2>/dev/null || note "fxroute.service start failed"
touch "$MARKER"

# Wait for the backend (generous budget: first live boot compiles the DSP
# engine), then open the kiosk. This replaces the autostart process.
status_url="http://127.0.0.1:8000/api/status"
deadline=$(( SECONDS + 1500 ))
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
mkdir -p "$HOME/Desktop" 2>/dev/null || true
printf '%s\n' \
  'FXRoute could not start: the backend did not come up on 127.0.0.1:8000.' \
  'Diagnosis: journalctl --user -u fxroute -n 50' \
  > "$HOME/Desktop/FXRoute-NOT-STARTED.txt" 2>/dev/null || true
exec firefox --kiosk http://127.0.0.1:8000/
