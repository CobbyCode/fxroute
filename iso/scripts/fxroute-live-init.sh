#!/usr/bin/env bash
# FXRoute live session init (volatile only, RAM overlay).
# Runs once per live boot before the display manager. Never touches internal
# disks, never runs Agama or fxroute-first-boot/install.sh, never git-fetches.
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

[[ -f /etc/fxroute-live ]] || exit 0

LIVE_USER="fxroute"
if ! id -u "$LIVE_USER" >/dev/null 2>&1; then
  # Fall back to the single regular user (same heuristic as first-boot).
  candidate="$(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ /^\// && $7 !~ /(nologin|false)$/ {print $1}' | head -n 1 || true)"
  [[ -n "$candidate" ]] || exit 0
  LIVE_USER="$candidate"
fi
LIVE_HOME="$(getent passwd "$LIVE_USER" | cut -d: -f6)"
LIVE_UID="$(id -u "$LIVE_USER")"
export HOME="$LIVE_HOME"

# Transient identity only: never write persistent hostname/machine-id here.
# /etc is already the RAM overlay, but keep it explicit and disk-safe.
hostnamectl set-hostname fxroute-live --transient 2>/dev/null || hostname fxroute-live 2>/dev/null || true

# Installer paths must stay dead in live mode (defense in depth; kernel
# cmdline already masks them).
systemctl mask --now fxroute-first-boot.service 2>/dev/null || true
systemctl mask --now agama.service agama-dbus-monitor.service 2>/dev/null || true
# No SSH daemon in live mode by default (local autologin + LAN web UI only).
# Test automation may pass fxroute.live-password=... on the kernel cmdline to
# enable SSH for the live user during QEMU verification; release boots omit
# it and keep sshd disabled with a locked password.
LIVE_PASSWORD=""
if [[ -r /proc/cmdline ]]; then
  for token in $(cat /proc/cmdline); do
    case "$token" in
      fxroute.live-password=*)
        LIVE_PASSWORD="${token#fxroute.live-password=}"
        ;;
    esac
  done
fi
if [[ -n "$LIVE_PASSWORD" ]]; then
  printf '%s:%s\n' "$LIVE_USER" "$LIVE_PASSWORD" | chpasswd 2>/dev/null || true
  systemctl enable --now sshd.service 2>/dev/null || systemctl start sshd.service 2>/dev/null || true
else
  systemctl stop sshd.service 2>/dev/null || true
  systemctl disable sshd.service 2>/dev/null || true
fi

# Ensure the live runtime exists (all in RAM overlay / tmpfs).
runuser -u "$LIVE_USER" -- env HOME="$LIVE_HOME" mkdir -p \
  "$LIVE_HOME/Music" \
  "$LIVE_HOME/.config/fxroute/measurements" \
  "$LIVE_HOME/.config/systemd/user" \
  "/run/user/$LIVE_UID" 2>/dev/null || true

# Start the user manager + PipeWire graph without requiring linger persistence.
# Each user-bus call is time-boxed so a missing bus can never stall the boot
# before the display manager (then SDDM autologin would never appear).
systemctl start "user@${LIVE_UID}.service" 2>/dev/null || true
# Cold live media (USB/squash decompression) can need many seconds for the
# first mpv exec; the app's version probe would time out and leave the
# player dead forever. Pre-warm the page cache before starting the service.
timeout 120 runuser -u "$LIVE_USER" -- mpv --version >/dev/null 2>&1 || true
live_user_systemctl() {
  timeout 30 runuser -u "$LIVE_USER" -- env HOME="$LIVE_HOME" \
    XDG_RUNTIME_DIR="/run/user/$LIVE_UID" \
    systemctl --user "$@" 2>/dev/null || true
}
live_user_systemctl start dbus.service
for unit in pipewire.socket wireplumber.service pipewire-pulse.socket; do
  live_user_systemctl start "$unit"
done
live_user_systemctl start fxroute.service

# Desktop live links (localized Desktop dir honored, failures never fatal).
DESKTOP_DIR="$LIVE_HOME/Desktop"
if [[ -f "$LIVE_HOME/.config/user-dirs.dirs" ]]; then
  entry="$(grep -E '^XDG_DESKTOP_DIR=' "$LIVE_HOME/.config/user-dirs.dirs" | tail -n 1 | cut -d= -f2- | tr -d '"' || true)"
  case "$entry" in
    '$HOME'/*) DESKTOP_DIR="$LIVE_HOME/${entry#'$HOME'/}" ;;
    /*) DESKTOP_DIR="$entry" ;;
  esac
fi
mkdir -p "$DESKTOP_DIR" 2>/dev/null || true
if [[ -w "$DESKTOP_DIR" ]]; then
  cat > "$DESKTOP_DIR/LIVE-MODE-README.txt" <<'EOF'
FXRoute Live Mode — changes and logins are not saved and will be lost after reboot.

Try FXRoute directly from this USB/ISO medium. Network, audio, DSP,
measurements and providers work in this session, but nothing is stored.
Internal drives are not automatically mounted or changed.
EOF
  chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$DESKTOP_DIR/LIVE-MODE-README.txt" 2>/dev/null || true
fi

exit 0
