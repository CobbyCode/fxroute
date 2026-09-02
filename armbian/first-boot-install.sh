#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

BASE_DIR="/opt/fxroute-armbian"
SOURCE_ARCHIVE="$BASE_DIR/source.tar"
SOURCE_DIR="$BASE_DIR/source"
ACCOUNT_FILE="/var/lib/armbian-web-config/account"
CONFIGURED_MARKER="/var/lib/armbian-web-config/configured"
STATE_DIR="/var/lib/fxroute-armbian"
COMPLETE_MARKER="$STATE_DIR/install-complete"
FAILED_MARKER="$STATE_DIR/install-failed"
IN_PROGRESS_MARKER="$STATE_DIR/install-in-progress"
CLEANUP_PENDING_MARKER="$STATE_DIR/install-cleanup-pending"
SERVICE_NAME="fxroute-armbian-first-boot.service"
FXROUTE_USER=""
staging_dir=""
retry_attempt=0
completed=0

[[ "$(id -u)" -eq 0 ]] || {
  printf '%s\n' "FXRoute Armbian first-boot setup must run as root" >&2
  exit 1
}

write_durable_marker() {
  local path="$1"
  local content="$2"
  local temporary="${path}.tmp"

  printf '%s\n' "$content" > "$temporary" || return 1
  chmod 600 "$temporary" || return 1
  sync -f "$temporary" || return 1
  mv -f -- "$temporary" "$path" || return 1
  sync -f "$path" || return 1
}

finish_success_cleanup() {
  local timestamp=""

  # Remove the login gate before the onboarding state so a power loss cannot
  # restart the interactive Armbian helper while cleanup is pending.
  rm -f -- /root/.not_logged_in_yet \
    /etc/profile.d/armbian-check-first-login.sh || return 1
  rm -f -- "$IN_PROGRESS_MARKER" "$FAILED_MARKER" || return 1
  rm -rf -- /var/lib/armbian-web-config || return 1
  rm -f -- /etc/default/armbian-web-config || return 1
  rm -rf -- /opt/fxroute-armbian || return 1

  sync -f "$STATE_DIR" || return 1
  timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)" || return 1
  write_durable_marker "$COMPLETE_MARKER" "$timestamp" || return 1
  rm -f -- "$CLEANUP_PENDING_MARKER" || true

  # Disable future starts only after the completion marker is durable.
  systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl disable armbian-web-config.service >/dev/null 2>&1 || true
}

mkdir -p "$STATE_DIR"
if [[ -f "$COMPLETE_MARKER" ]]; then
  rm -f -- "$CLEANUP_PENDING_MARKER"
  exit 0
fi
if [[ -f "$CLEANUP_PENDING_MARKER" ]]; then
  finish_success_cleanup
  exit 0
fi
[[ -f "$CONFIGURED_MARKER" && ! -L "$CONFIGURED_MARKER" ]] || {
  printf 'End-user onboarding has not completed: %s\n' "$CONFIGURED_MARKER" >&2
  exit 1
}
if [[ -f "$FAILED_MARKER" || -f "$IN_PROGRESS_MARKER" ]]; then
  retry_attempt=1
fi
rm -f -- "$FAILED_MARKER"
write_durable_marker "$IN_PROGRESS_MARKER" "first-boot setup is in progress"

cleanup() {
  local status=$?
  local timestamp=""

  [[ -z "$staging_dir" ]] || rm -rf -- "$staging_dir"
  if [[ "$status" -eq 0 && "$completed" -eq 1 ]]; then
    if ! {
      timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)" &&
      write_durable_marker "$CLEANUP_PENDING_MARKER" "$timestamp" &&
      finish_success_cleanup
    }; then
      status=1
    fi
  fi
  if [[ "$status" -ne 0 ]]; then
    write_durable_marker "$FAILED_MARKER" "first-boot setup failed with status $status" || true
  fi
  exit "$status"
}
trap cleanup EXIT

[[ -f "$SOURCE_ARCHIVE" && ! -L "$SOURCE_ARCHIVE" ]] || {
  printf 'Missing FXRoute source archive: %s\n' "$SOURCE_ARCHIVE" >&2
  exit 1
}
read_account_user() {
  [[ -f "$ACCOUNT_FILE" && ! -L "$ACCOUNT_FILE" ]] || {
    printf 'Missing end-user account metadata: %s\n' "$ACCOUNT_FILE" >&2
    return 1
  }
  FXROUTE_USER="$(<"$ACCOUNT_FILE")"
  [[ "$FXROUTE_USER" =~ ^[a-z_][a-z0-9_.-]{0,31}$ && "$FXROUTE_USER" != root ]] \
    || { printf 'Invalid end-user account name\n' >&2; return 1; }
  getent passwd "$FXROUTE_USER" >/dev/null 2>&1 \
    || { printf 'End-user account does not exist: %s\n' "$FXROUTE_USER" >&2; return 1; }
}

reset_incomplete_install() {
  local user_home="$1"
  local root_state_dir=""
  local root_state_file=""
  local target="$user_home/fxroute"

  root_state_dir="/var/lib/fxroute/state/$(id -u "$FXROUTE_USER")"
  root_state_file="$root_state_dir/install-state.json"
  [[ "$retry_attempt" -eq 1 ]] || return 0
  if [[ -s "$root_state_file" ]] && python3 - "$root_state_file" "$target" <<'PY'
import json
import sys
from pathlib import Path

try:
    state = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    raise SystemExit(1)
if state.get("install_root") != sys.argv[2]:
    raise SystemExit(1)
PY
  then
    return 0
  fi

  rm -rf -- "$target"
  rm -f -- "$user_home/.config/fxroute/install-state.json"
  rm -f -- "$user_home/.config/fxroute/install-config.env"
  rm -rf -- "$root_state_dir"
}

wait_for_valid_clock() {
  local deadline=$((SECONDS + 300))
  local epoch=""

  while (( SECONDS < deadline )); do
    epoch="$(date -u +%s)"
    if [[ "$epoch" =~ ^[0-9]+$ ]] && (( epoch >= 1577836800 )); then
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "System clock did not synchronize before FXRoute installation" >&2
  return 1
}

while [[ "$(systemctl show --property=SubState --value armbian-firstrun.service 2>/dev/null || true)" == "running" ]]; do
  sleep 1
done

wait_for_valid_clock
read_account_user
fxroute_home="$(getent passwd "$FXROUTE_USER" | cut -d: -f6)"
[[ "$fxroute_home" == /home/* && -d "$fxroute_home" ]] \
  || { printf 'FXRoute user has no usable home directory: %s\n' "$FXROUTE_USER" >&2; exit 1; }
reset_incomplete_install "$fxroute_home"

staging_dir="$(mktemp -d "$BASE_DIR/source.XXXXXX")"
tar --extract --file "$SOURCE_ARCHIVE" --directory "$staging_dir" --no-same-owner
[[ -f "$staging_dir/install.sh" ]] || {
  printf '%s\n' "The FXRoute source archive does not contain install.sh" >&2
  exit 1
}
rm -rf -- "$SOURCE_DIR"
mv -- "$staging_dir" "$SOURCE_DIR"
staging_dir=""
chmod 755 "$SOURCE_DIR/install.sh"

install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-fxroute-armbian.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
chmod 644 /etc/ssh/sshd_config.d/90-fxroute-armbian.conf
ssh-keygen -A
sshd -t 2>/dev/null || {
  printf '%s\n' "sshd configuration validation failed" >&2
  exit 1
}
ssh_service=""
if systemctl enable --now ssh.service >/dev/null 2>&1; then
  ssh_service="ssh.service"
elif systemctl enable --now sshd.service >/dev/null 2>&1; then
  ssh_service="sshd.service"
else
  printf '%s\n' "Could not enable the SSH service" >&2
  exit 1
fi
if ! systemctl reload-or-restart "$ssh_service" >/dev/null 2>&1; then
  printf '%s\n' "Could not reload the SSH service after hardening" >&2
  exit 1
fi

export HOME="$fxroute_home"
"$SOURCE_DIR/install.sh" \
  --source "$SOURCE_DIR" \
  --target "$fxroute_home/fxroute" \
  --user "$FXROUTE_USER" \
  --providers none \
  --yes

completed=1
