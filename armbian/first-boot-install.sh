#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

BASE_DIR="/opt/fxroute-armbian"
SOURCE_ARCHIVE="$BASE_DIR/source.tar"
SOURCE_DIR="$BASE_DIR/source"
PROVISION_FILE="$BASE_DIR/provision.env"
STATE_DIR="/var/lib/fxroute-armbian"
COMPLETE_MARKER="$STATE_DIR/install-complete"
FAILED_MARKER="$STATE_DIR/install-failed"
IN_PROGRESS_MARKER="$STATE_DIR/install-in-progress"
SERVICE_NAME="fxroute-armbian-first-boot.service"
FXROUTE_USER=""
FXROUTE_PASSWORD_HASH=""
FXROUTE_SSH_PUBLIC_KEY=""
staging_dir=""
retry_attempt=0
completed=0

[[ "$(id -u)" -eq 0 ]] || {
  printf '%s\n' "FXRoute Armbian first-boot setup must run as root" >&2
  exit 1
}

mkdir -p "$STATE_DIR"
if [[ -f "$COMPLETE_MARKER" ]]; then
  exit 0
fi
if [[ -f "$FAILED_MARKER" || -f "$IN_PROGRESS_MARKER" ]]; then
  retry_attempt=1
fi
rm -f -- "$FAILED_MARKER"
touch "$IN_PROGRESS_MARKER"
chmod 600 "$IN_PROGRESS_MARKER"

cleanup() {
  local status=$?
  local complete_marker_tmp="${COMPLETE_MARKER}.tmp"

  [[ -z "$staging_dir" ]] || rm -rf -- "$staging_dir"
  if [[ "$status" -eq 0 && "$completed" -eq 1 ]]; then
    rm -f -- "$IN_PROGRESS_MARKER" "$FAILED_MARKER"
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    rm -rf -- /opt/fxroute-armbian
    date -u +%Y-%m-%dT%H:%M:%SZ > "$complete_marker_tmp"
    chmod 600 "$complete_marker_tmp"
    mv -f -- "$complete_marker_tmp" "$COMPLETE_MARKER"
  elif [[ "$status" -ne 0 ]]; then
    rm -f -- "$complete_marker_tmp"
    printf 'first-boot setup failed with status %s\n' "$status" > "$FAILED_MARKER"
  fi
  exit "$status"
}
trap cleanup EXIT

read_provision_value() {
  local wanted="$1"
  local encoded=""
  encoded="$(awk -F= -v wanted="$wanted" '$1 == wanted { sub(/^[^=]*=/, "", $0); print; exit }' \
    "$PROVISION_FILE")"
  [[ "$encoded" =~ ^[A-Za-z0-9+/]+={0,2}$ ]] || {
    printf 'Invalid base64 provisioning value: %s\n' "$wanted" >&2
    return 1
  }
  printf '%s' "$encoded" | base64 --decode
}

[[ -f "$SOURCE_ARCHIVE" && ! -L "$SOURCE_ARCHIVE" ]] || {
  printf 'Missing FXRoute source archive: %s\n' "$SOURCE_ARCHIVE" >&2
  exit 1
}
[[ -f "$PROVISION_FILE" && ! -L "$PROVISION_FILE" ]] || {
  printf 'Missing FXRoute provisioning metadata: %s\n' "$PROVISION_FILE" >&2
  exit 1
}

FXROUTE_USER="$(read_provision_value FXROUTE_USER_B64)"
FXROUTE_PASSWORD_HASH="$(read_provision_value FXROUTE_PASSWORD_HASH_B64)"
FXROUTE_SSH_PUBLIC_KEY="$(read_provision_value FXROUTE_SSH_PUBLIC_KEY_B64)"
[[ "$FXROUTE_USER" =~ ^[a-z_][a-z0-9_.-]{0,31}$ && "$FXROUTE_USER" != root ]] \
  || { printf 'Invalid FXRoute provisioning user: %s\n' "$FXROUTE_USER" >&2; exit 1; }
[[ "$FXROUTE_PASSWORD_HASH" =~ ^\$6\$[A-Za-z0-9./]+\$[A-Za-z0-9./]+$ ]] \
  || { printf '%s\n' "The FXRoute provisioning password is not a SHA-512 crypt hash" >&2; exit 1; }
[[ "$FXROUTE_SSH_PUBLIC_KEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] \
  || { printf '%s\n' "The FXRoute provisioning key is not a supported OpenSSH public key" >&2; exit 1; }

provision_user() {
  local user_home=""
  local user_group=""
  local ssh_dir=""
  local authorized_keys=""
  local group=""

  if ! getent passwd "$FXROUTE_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$FXROUTE_USER"
  fi
  user_home="$(getent passwd "$FXROUTE_USER" | cut -d: -f6)"
  [[ "$user_home" == /home/* && -d "$user_home" ]] \
    || { printf 'FXRoute user has no usable home directory: %s\n' "$FXROUTE_USER" >&2; exit 1; }
  user_group="$(id -gn "$FXROUTE_USER")"

  usermod --password "$FXROUTE_PASSWORD_HASH" "$FXROUTE_USER"
  for group in sudo wheel audio; do
    if getent group "$group" >/dev/null 2>&1; then
      usermod -aG "$group" "$FXROUTE_USER"
    fi
  done

  ssh_dir="$user_home/.ssh"
  authorized_keys="$ssh_dir/authorized_keys"
  [[ ! -L "$ssh_dir" && ! -L "$authorized_keys" ]] || {
    printf '%s\n' "Refusing symlinked SSH configuration for the FXRoute user" >&2
    exit 1
  }
  install -d -o "$FXROUTE_USER" -g "$user_group" -m 700 "$ssh_dir"
  if [[ ! -e "$authorized_keys" ]]; then
    install -o "$FXROUTE_USER" -g "$user_group" -m 600 /dev/null "$authorized_keys"
  fi
  grep -Fqx -- "$FXROUTE_SSH_PUBLIC_KEY" "$authorized_keys" \
    || printf '%s\n' "$FXROUTE_SSH_PUBLIC_KEY" >> "$authorized_keys"
  chown "$FXROUTE_USER:$user_group" "$authorized_keys"
  chmod 600 "$authorized_keys"
}

reset_incomplete_install() {
  local user_home="$1"
  local root_state_dir="/var/lib/fxroute/state/$(id -u "$FXROUTE_USER")"
  local root_state_file="$root_state_dir/install-state.json"
  local target="$user_home/fxroute"

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

while [[ "$(systemctl show --property=SubState --value armbian-firstrun.service 2>/dev/null || true)" == "running" ]]; do
  sleep 1
done

provision_user
fxroute_home="$(getent passwd "$FXROUTE_USER" | cut -d: -f6)"
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
