#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_NAME="FXRoute"
SERVICE_NAME="fxroute"
PROJECT_DIRNAME="fxroute"
INSTALL_ROOT_DEFAULT="$HOME/$PROJECT_DIRNAME"
INSTALL_ROOT="$INSTALL_ROOT_DEFAULT"
INSTALL_ROOT_EXPLICIT=0
TARGET_USER_ARG=""
FXROUTE_TARGET_USER="$(id -un)"
FXROUTE_TARGET_UID="$(id -u)"
FXROUTE_RUNTIME_DIR="/run/user/$FXROUTE_TARGET_UID"
REMOVE_PROJECT_DIR=0
ASSUME_YES=0
INSTALL_ROOT_LOCAL_PROJECT=0
INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
INSTALL_CONFIG_FILE="$HOME/.config/fxroute/install-config.env"
ROOT_INSTALL_STATE_FILE="/var/lib/fxroute/state/$(id -u)/install-state.json"
FXROUTE_BACKUP_DIR="/var/lib/fxroute/backups/$(id -u)"
ROOT_STATE_REQUIRED=0
ROOT_STATE_TRUSTED=0
CIFS_HELPER_INSTALLED_BY_FXROUTE=0
CIFS_SUDOERS_RULE_INSTALLED_BY_FXROUTE=0
CIFS_SUDOERS_SHA256=""
SPOTIFY_APT_SOURCE_FILE="/etc/apt/sources.list.d/spotify.list"
SPOTIFY_APT_KEY_FILE="/usr/share/keyrings/spotify-archive-keyring.gpg"
SPOTIFY_APT_KEY_FINGERPRINT="E1096BCBFF6D418796DE78515384CE82BA52C83A"
SPOTIFYD_ZEROCONF_PORT="4444"
CIFS_HELPER_SHA256="a878afbf1927bdd14ed3049df39a41929a54cd18a1ab89a377ba1ed4c4b453d8"
SYSTEM_UPDATE_HELPER_SHA256="b9e67b2f396e814930d1ebfeba8f6d9d483b601a3fbd27cc7dd8c32b7d3506eb"
PRESERVE_INSTALL_STATE=0
PROVIDER_LAN_CLEANUP_DEFERRED=0
CORE_SERVICE_CLEANUP_DEFERRED=0

# Provider credentials, sessions, caches, and user configuration are never
# removed by this script. Owned binaries and user units are handled separately.
PROVIDER_DATA_PATHS=(
  "$HOME/.config/spotify"
  "$HOME/.cache/spotify"
  "$HOME/.local/share/spotify"
  "$HOME/.var/app/com.spotify.Client"
  "$HOME/.config/spotifyd"
  "$HOME/.cache/spotifyd"
  "$HOME/.config/qbzd"
  "$HOME/.cache/qbzd"
  "$HOME/.local/share/qbzd"
  "$HOME/.config/qbz"
  "$HOME/.cache/qbz"
  "$HOME/.local/share/qbz"
  "$HOME/.config/fxroute/tidal-session.json"
)

usage() {
  cat <<EOF
Usage: ./uninstall.sh [options]

Options:
  --target <dir>                Uninstall from this directory (default: $INSTALL_ROOT_DEFAULT)
  --user <name>                 Select the FXRoute user when invoked as root
  --remove-project-dir          Remove the project directory after uninstall
  -y, --yes                     Assume yes for optional removals
  -h, --help                    Show this help

Safe by default:
- removes FXRoute user service
- removes optional Spotify cache cleanup timer/service
- removes optional system package update timer/service
- removes FXRoute helper scripts
- removes Spotify autostart
- prompts before removing FXRoute-owned Spotify Desktop, spotifyd, qbzd, or TIDAL components
- preserves provider configuration, credentials, sessions, and caches
- removes the optional FXRoute Caddy reverse proxy service/config if present
- restores the previous default \`caddy.service\` when FXRoute had disabled it to take over port 80

Cautious by default:
- does NOT remove the project directory unless requested
EOF
}

log() { printf '[fxroute-uninstall] %s\n' "$*"; }
warn() { printf '[fxroute-uninstall][warn] %s\n' "$*" >&2; }

firewall_cmd_path() {
  local path=""
  path="$(command -v firewall-cmd 2>/dev/null || true)"
  if [[ -z "$path" && -x /usr/bin/firewall-cmd ]]; then
    path="/usr/bin/firewall-cmd"
  fi
  if [[ -n "$path" ]]; then
    printf '%s\n' "$path"
  fi
  return 0
}

firewall_offline_cmd_path() {
  local path=""
  path="$(command -v firewall-offline-cmd 2>/dev/null || true)"
  if [[ -z "$path" && -x /usr/bin/firewall-offline-cmd ]]; then
    path="/usr/bin/firewall-offline-cmd"
  fi
  if [[ -n "$path" ]]; then
    printf '%s\n' "$path"
  fi
  return 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || { echo "--target requires a directory" >&2; exit 1; }
      INSTALL_ROOT="$2"
      INSTALL_ROOT_EXPLICIT=1
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || { echo "--user requires a Unix username" >&2; exit 1; }
      TARGET_USER_ARG="$2"
      shift 2
      ;;
    --remove-project-dir)
      REMOVE_PROJECT_DIR=1
      shift
      ;;
    -y|--yes)
      ASSUME_YES=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

configure_target_user_environment() {
  local passwd_entry=""
  local passwd_name=""
  local passwd_password=""
  local passwd_uid=""
  local passwd_gid=""
  local passwd_gecos=""
  local passwd_shell=""

  if [[ -n "$TARGET_USER_ARG" ]]; then
    [[ "$(id -u)" -eq 0 ]] || {
      echo "Only root may select a different FXRoute user" >&2
      exit 1
    }
    FXROUTE_TARGET_USER="$TARGET_USER_ARG"
  fi

  passwd_entry="$(getent passwd "$FXROUTE_TARGET_USER" 2>/dev/null || true)"
  [[ -n "$passwd_entry" ]] || {
    echo "Could not find FXRoute user $FXROUTE_TARGET_USER" >&2
    exit 1
  }
  IFS=: read -r passwd_name passwd_password passwd_uid passwd_gid passwd_gecos HOME passwd_shell <<<"$passwd_entry"
  [[ "$HOME" == /* && -d "$HOME" ]] || {
    echo "FXRoute user $FXROUTE_TARGET_USER has no usable home directory" >&2
    exit 1
  }
  export HOME
  FXROUTE_TARGET_UID="$(id -u "$FXROUTE_TARGET_USER")"
  FXROUTE_RUNTIME_DIR="/run/user/$FXROUTE_TARGET_UID"
  INSTALL_ROOT_DEFAULT="$HOME/$PROJECT_DIRNAME"
  [[ $INSTALL_ROOT_EXPLICIT -eq 1 ]] || INSTALL_ROOT="$INSTALL_ROOT_DEFAULT"
  INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
  INSTALL_CONFIG_FILE="$HOME/.config/fxroute/install-config.env"
  ROOT_INSTALL_STATE_FILE="/var/lib/fxroute/state/$FXROUTE_TARGET_UID/install-state.json"
  FXROUTE_BACKUP_DIR="/var/lib/fxroute/backups/$FXROUTE_TARGET_UID"
  PROVIDER_DATA_PATHS=(
    "$HOME/.config/spotify"
    "$HOME/.cache/spotify"
    "$HOME/.local/share/spotify"
    "$HOME/.var/app/com.spotify.Client"
    "$HOME/.config/spotifyd"
    "$HOME/.cache/spotifyd"
    "$HOME/.config/qbzd"
    "$HOME/.cache/qbzd"
    "$HOME/.local/share/qbzd"
    "$HOME/.config/qbz"
    "$HOME/.cache/qbz"
    "$HOME/.local/share/qbz"
    "$HOME/.config/fxroute/tidal-session.json"
  )
}

configure_target_user_environment

user_systemctl() {
  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]]; then
    systemctl --user --machine="${FXROUTE_TARGET_USER}@" "$@"
  else
    XDG_RUNTIME_DIR="$FXROUTE_RUNTIME_DIR" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$FXROUTE_RUNTIME_DIR/bus" \
      systemctl --user "$@"
  fi
}

run_as_target_user() {
  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]]; then
    runuser -u "$FXROUTE_TARGET_USER" -- env \
      HOME="$HOME" \
      XDG_CONFIG_HOME="$HOME/.config" \
      XDG_DATA_HOME="$HOME/.local/share" \
      XDG_CACHE_HOME="$HOME/.cache" \
      XDG_RUNTIME_DIR="$FXROUTE_RUNTIME_DIR" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$FXROUTE_RUNTIME_DIR/bus" \
      PIPEWIRE_REMOTE=pipewire-0 \
      PULSE_SERVER="$FXROUTE_RUNTIME_DIR/pulse/native" \
      "$@"
  else
    HOME="$HOME" \
      XDG_CONFIG_HOME="$HOME/.config" \
      XDG_DATA_HOME="$HOME/.local/share" \
      XDG_CACHE_HOME="$HOME/.cache" \
      XDG_RUNTIME_DIR="$FXROUTE_RUNTIME_DIR" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$FXROUTE_RUNTIME_DIR/bus" \
      PIPEWIRE_REMOTE=pipewire-0 \
      PULSE_SERVER="$FXROUTE_RUNTIME_DIR/pulse/native" \
      "$@"
  fi
}

confirm() {
  local prompt="$1"
  if [[ $ASSUME_YES -eq 1 ]]; then
    return 0
  fi
  read -r -p "$prompt [y/N] " reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

canonical_path() {
  python3 - <<'PY' "$1"
import os
import sys
print(os.path.realpath(os.path.abspath(os.path.expanduser(sys.argv[1]))))
PY
}

canonical_config_path() {
  python3 - <<'PY' "$1" "$2"
import os
import sys

value = sys.argv[1].strip()
base = sys.argv[2]
if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
    value = value[1:-1]
value = os.path.expandvars(os.path.expanduser(value))
if not os.path.isabs(value):
    value = os.path.join(base, value)
print(os.path.realpath(os.path.abspath(value)))
PY
}

path_is_within() {
  local child="$1"
  local parent="$2"
  [[ "$parent" == "/" && "$child" == /* ]] && return 0
  [[ "$child" == "$parent" || "$child" == "$parent"/* ]]
}

path_has_symlink_component() {
  local path="$1"
  local current="/"
  local component=""
  local components=()

  [[ "$path" == /* ]] || return 1
  IFS='/' read -r -a components <<<"${path#/}"
  for component in "${components[@]}"; do
    [[ -n "$component" ]] || continue
    current="${current%/}/$component"
    [[ -L "$current" ]] && return 0
  done
  return 1
}

root_state_is_trusted() {
  local state_dir="$(dirname "$ROOT_INSTALL_STATE_FILE")"
  local state_mode=""
  local dir_mode=""

  [[ "$ROOT_INSTALL_STATE_FILE" == /var/lib/fxroute/state/* ]] || return 1
  [[ -f "$ROOT_INSTALL_STATE_FILE" && ! -L "$ROOT_INSTALL_STATE_FILE" ]] || return 1
  if path_has_symlink_component "$ROOT_INSTALL_STATE_FILE"; then
    return 1
  fi
  [[ "$(stat -c '%u:%g' "$ROOT_INSTALL_STATE_FILE" 2>/dev/null || true)" == "0:0" ]] || return 1
  state_mode="$(stat -c '%a' "$ROOT_INSTALL_STATE_FILE" 2>/dev/null || true)"
  [[ "$state_mode" == 600 || "$state_mode" == 640 || "$state_mode" == 644 ]] || return 1
  [[ -d "$state_dir" && ! -L "$state_dir" ]] || return 1
  [[ "$(stat -c '%u:%g' "$state_dir" 2>/dev/null || true)" == "0:0" ]] || return 1
  dir_mode="$(stat -c '%a' "$state_dir" 2>/dev/null || true)"
  [[ "$dir_mode" == 700 || "$dir_mode" == 750 || "$dir_mode" == 755 ]] || return 1
  python3 - <<'PY' "$ROOT_INSTALL_STATE_FILE" "$FXROUTE_TARGET_USER" "$FXROUTE_TARGET_UID"
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    raise SystemExit(1)
if not isinstance(payload, dict):
    raise SystemExit(1)
if payload.get("install_user") != sys.argv[2]:
    raise SystemExit(1)
if str(payload.get("install_uid")) != sys.argv[3]:
    raise SystemExit(1)
if not isinstance(payload.get("install_root"), str) or not payload["install_root"].startswith("/"):
    raise SystemExit(1)
PY
}

read_state_field_from_file() {
  local state_file="$1"
  local field="$2"
  [[ -f "$state_file" && ! -L "$state_file" ]] || return 1
  python3 - <<'PY' "$state_file" "$field"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
value = payload
for part in sys.argv[2].split('.'):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(1)
    value = value[part]
if isinstance(value, bool):
    print('true' if value else 'false')
elif value is None:
    print('')
else:
    print(value)
PY
}

if [[ -f "$INSTALL_STATE_FILE" || -f "$INSTALL_CONFIG_FILE" || -f "$ROOT_INSTALL_STATE_FILE" ]]; then
  ROOT_STATE_REQUIRED=1
fi
if root_state_is_trusted; then
  ROOT_STATE_TRUSTED=1
fi

validate_install_root_selection() {
  local root="$1"

  [[ "$root" != "/" && "$root" != "$HOME" ]] || {
    echo "Refusing to use / or the target user's home as an FXRoute project target" >&2
    exit 1
  }
  [[ ! -L "$root" ]] || {
    echo "Refusing to follow a symlink as the FXRoute project target: $root" >&2
    exit 1
  }
  if path_has_symlink_component "$root"; then
    echo "Refusing to follow a symlinked parent as the FXRoute project target: $root" >&2
    exit 1
  fi
  if [[ $INSTALL_ROOT_LOCAL_PROJECT -eq 0 && "$(basename "$root")" != "$PROJECT_DIRNAME" ]]; then
    echo "FXRoute project targets must be dedicated directories named $PROJECT_DIRNAME" >&2
    exit 1
  fi
}

load_recorded_install_root() {
  local configured_root=""
  local recorded_local_project=""
  local state_file="$INSTALL_STATE_FILE"

  if [[ $ROOT_STATE_TRUSTED -eq 1 ]]; then
    state_file="$ROOT_INSTALL_STATE_FILE"
  elif [[ $INSTALL_ROOT_EXPLICIT -eq 0 && ( -f "$INSTALL_STATE_FILE" || -f "$INSTALL_CONFIG_FILE" ) ]]; then
    echo "A root-owned FXRoute install state is required to select a recorded project target; rerun with --target or reinstall FXRoute" >&2
    exit 1
  fi
  if [[ -f "$state_file" ]]; then
    recorded_local_project="$(read_state_field_from_file "$state_file" local_project 2>/dev/null || true)"
    [[ "$recorded_local_project" == "true" ]] && INSTALL_ROOT_LOCAL_PROJECT=1
  fi
  [[ $ROOT_STATE_TRUSTED -eq 1 && -f "$state_file" ]] || return 0
  configured_root="$(read_state_field_from_file "$state_file" install_root 2>/dev/null || true)"
  [[ -n "$configured_root" ]] || return 0
  validate_install_root_selection "$configured_root"
  if [[ $INSTALL_ROOT_EXPLICIT -eq 1 ]]; then
    if [[ "$(canonical_path "$configured_root")" != "$(canonical_path "$INSTALL_ROOT")" ]]; then
      echo "The recorded FXRoute install root is $configured_root; refusing to use a different explicit target" >&2
      exit 1
    fi
    return 0
  fi
  INSTALL_ROOT="$(canonical_path "$configured_root")"
}

load_recorded_install_root
validate_install_root_selection "$INSTALL_ROOT"
INSTALL_ROOT="$(canonical_path "$INSTALL_ROOT")"

validate_install_root_for_removal() {
  local root="$INSTALL_ROOT"
  local protected_path=""
  local music_root_value="$HOME/Music"
  local configured_music_root=""
  local music_root=""

  [[ "$root" != "/" && "$root" != "$HOME" ]] || {
    warn "Refusing to remove / or the home directory as an FXRoute project target"
    return 1
  }
  [[ ! -L "$root" ]] || {
    warn "Refusing to remove $root because it is not a dedicated FXRoute project directory"
    return 1
  }
  if [[ $INSTALL_ROOT_LOCAL_PROJECT -eq 0 && "$(basename "$root")" != "$PROJECT_DIRNAME" ]]; then
    warn "Refusing to remove $root because it is not a dedicated FXRoute project directory"
    return 1
  fi
  if [[ $INSTALL_ROOT_EXPLICIT -eq 0 ]] && ! path_is_within "$root" "$HOME"; then
    warn "Refusing to remove an externally recorded project target without an explicit --target"
    return 1
  fi
  for protected_path in \
    "$HOME/.config" "$HOME/.cache" "$HOME/.local" "$HOME/.var" \
    "$INSTALL_STATE_FILE" "$INSTALL_CONFIG_FILE" "$FXROUTE_BACKUP_DIR"; do
    if path_is_within "$protected_path" "$root"; then
      warn "Refusing to remove $root because it overlaps protected FXRoute or provider data"
      return 1
    fi
  done
  for protected_path in "${PROVIDER_DATA_PATHS[@]}"; do
    if path_is_within "$protected_path" "$root"; then
      warn "Refusing to remove $root because it overlaps provider data at $protected_path"
      return 1
    fi
  done
  if [[ -f "$root/.env" ]]; then
    configured_music_root="$(awk -F= '$0 !~ /^[[:space:]]*#/ && $1 == "MUSIC_ROOT" {sub(/^[^=]*=/, "", $0); print $0; exit}' "$root/.env")"
    [[ -n "$configured_music_root" ]] && music_root_value="$configured_music_root"
  fi
  music_root="$(canonical_config_path "$music_root_value" "$root")" || {
    warn "Refusing to remove $root because MUSIC_ROOT could not be validated"
    return 1
  }
  if path_is_within "$music_root" "$root" || path_is_within "$root" "$music_root"; then
    warn "Refusing to remove $root because it overlaps configured MUSIC_ROOT $music_root"
    return 1
  fi
  if [[ -e "$root/.git" ]]; then
    warn "Refusing to remove the Git checkout at $root"
    return 1
  fi
  if [[ -e "$root" && ( ! -f "$root/main.py" || ! -f "$root/requirements.txt" ) ]]; then
    warn "Refusing to remove $root because it does not look like an FXRoute install"
    return 1
  fi
  return 0
}

remove_file_if_exists() {
  local path="$1"
  local remove_cmd=(rm)
  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != root \
    && ( "$path" == "$HOME" || "$path" == "$HOME"/* ) ]]; then
    remove_cmd=(run_as_target_user rm)
  fi
  if [[ -e "$path" || -L "$path" ]]; then
    if "${remove_cmd[@]}" -f "$path"; then
      log "Removed $path"
    else
      warn "Could not remove $path; keeping install state for deferred cleanup"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
}

verify_owned_binary_identity() {
  local path="$1"
  local expected_sha256="$2"
  local label="$3"
  local actual_sha256=""

  [[ -e "$path" || -L "$path" ]] || return 0
  if [[ ! -f "$path" || -L "$path" || -z "$expected_sha256" ]]; then
    warn "Refusing to remove $label because its recorded identity is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  actual_sha256="$(sha256sum "$path" | awk '{print $1}')"
  if [[ "$actual_sha256" != "$expected_sha256" ]]; then
    warn "Refusing to remove $label because its checksum changed"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  return 0
}

user_unit_file_matches() {
  local path="$1"
  local marker=""

  [[ -f "$path" && ! -L "$path" ]] || return 1
  shift
  for marker in "$@"; do
    grep -Fq -- "$marker" "$path" || return 1
  done
}

remove_service() {
  local service_path="$HOME/.config/systemd/user/$SERVICE_NAME.service"

  if [[ -e "$service_path" || -L "$service_path" ]] \
    && ! user_unit_file_matches "$service_path" \
      "Description=FXRoute" \
      "WorkingDirectory=$INSTALL_ROOT" \
      "ExecStart=$INSTALL_ROOT/.venv/bin/python3 $INSTALL_ROOT/main.py"; then
    warn "Refusing to remove the FXRoute service because its unit content is not FXRoute-owned"
    PRESERVE_INSTALL_STATE=1
    CORE_SERVICE_CLEANUP_DEFERRED=1
    return 0
  fi
  if ! stop_owned_user_service "$SERVICE_NAME.service" "$service_path"; then
    CORE_SERVICE_CLEANUP_DEFERRED=1
    return 0
  fi
  remove_file_if_exists "$service_path"
}

systemd_unit_is_loaded() {
  local unit="$1"
  local load_state=""

  shift
  if ! load_state="$("$@" systemctl show "$unit" -p LoadState --value 2>/dev/null)"; then
    return 2
  fi
  case "$load_state" in
    not-found) return 1 ;;
    "") return 2 ;;
    *) return 0 ;;
  esac
}

user_systemd_unit_is_loaded() {
  local unit="$1"
  local load_state=""

  if ! load_state="$(user_systemctl show "$unit" -p LoadState --value 2>/dev/null)"; then
    return 2
  fi
  case "$load_state" in
    not-found) return 1 ;;
    "") return 2 ;;
    *) return 0 ;;
  esac
}

systemd_unit_fragment_matches() {
  local unit="$1"
  local expected_path="$2"
  local fragment_path=""

  shift 2
  if ! fragment_path="$("$@" systemctl show "$unit" -p FragmentPath --value 2>/dev/null)"; then
    return 2
  fi
  [[ "$fragment_path" == "$expected_path" ]]
}

stop_owned_user_service() {
  local unit="$1"
  local expected_path="${2:-}"
  local active_status=0
  local fragment_path=""
  local loaded_status=0

  if [[ -n "$expected_path" ]]; then
    if user_systemd_unit_is_loaded "$unit"; then
      if ! fragment_path="$(user_systemctl show "$unit" -p FragmentPath --value 2>/dev/null)"; then
        warn "Could not verify the loaded unit path for '$unit'"
        PRESERVE_INSTALL_STATE=1
        return 1
      fi
      if [[ -z "$fragment_path" || "$fragment_path" != "$expected_path" ]]; then
        warn "Refusing to stop '$unit' because its loaded unit path is not FXRoute-owned"
        PRESERVE_INSTALL_STATE=1
        return 1
      fi
    else
      loaded_status=$?
      if [[ $loaded_status -ne 1 ]]; then
        warn "Could not verify whether FXRoute-owned user service '$unit' is loaded"
        PRESERVE_INSTALL_STATE=1
        return 1
      fi
    fi
  fi

  if user_systemctl disable --now "$unit" >/dev/null 2>&1; then
    return 0
  fi
  if user_systemctl is-active --quiet "$unit" >/dev/null 2>&1; then
    warn "Could not stop active FXRoute-owned user service '$unit'"
    PRESERVE_INSTALL_STATE=1
    return 1
  else
    active_status=$?
  fi
  case "$active_status" in
    3|4) return 0 ;;
    *)
      warn "Could not verify FXRoute-owned user service '$unit'"
      PRESERVE_INSTALL_STATE=1
      return 1
      ;;
  esac
}

remove_dsp_ingress_sink() {
  local config_file="$HOME/.config/pipewire/pipewire-pulse.conf.d/50-fxroute-dsp-sink.conf"
  remove_file_if_exists "$config_file"
  run_as_target_user rmdir "$(dirname "$config_file")" >/dev/null 2>&1 || true
  user_systemctl restart pipewire-pulse.service >/dev/null 2>&1 || true
}

remove_user_linger_if_owned() {
  local linger_owned=""
  local sudo_cmd=()

  linger_owned="$(read_install_state_field user_linger_enabled_by_fxroute 2>/dev/null || true)"
  [[ "$linger_owned" == "true" ]] || return 0
  if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo_cmd=(sudo)
    else
      warn "Cannot disable FXRoute-owned user lingering because sudo is unavailable"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi
  if ! "${sudo_cmd[@]}" loginctl disable-linger "$FXROUTE_TARGET_USER"; then
    warn "Could not disable FXRoute-owned user lingering for $FXROUTE_TARGET_USER"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  log "Disabled user lingering enabled by FXRoute for $FXROUTE_TARGET_USER"
}

remove_audio_group_if_owned() {
  local group_owned=""
  local sudo_cmd=()

  group_owned="$(read_install_state_field audio_group_added_by_fxroute 2>/dev/null || true)"
  [[ "$group_owned" == "true" ]] || return 0
  if [[ "$(id -u)" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      sudo_cmd=(sudo)
    else
      warn "Cannot remove FXRoute-owned audio group membership because sudo is unavailable"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi
  if ! "${sudo_cmd[@]}" gpasswd -d "$FXROUTE_TARGET_USER" audio; then
    warn "Could not remove $FXROUTE_TARGET_USER from the audio group"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  log "Removed $FXROUTE_TARGET_USER from the audio group added by FXRoute"
}

remove_spotify_cleanup_helper() {
  local service_name="fxroute-spotify-cache-cleanup.service"
  local timer_name="fxroute-spotify-cache-cleanup.timer"
  local service_path="$HOME/.config/systemd/user/$service_name"
  local timer_path="$HOME/.config/systemd/user/$timer_name"
  local script_path="$INSTALL_ROOT/scripts/spotify-cache-cleanup.sh"

  if [[ -e "$service_path" || -L "$service_path" ]] \
    && ! user_unit_file_matches "$service_path" \
      "Description=FXRoute Spotify cache cleanup" \
      "ExecStart=$script_path"; then
    warn "Refusing to remove the Spotify cache cleanup service because its unit content is not FXRoute-owned"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ -e "$timer_path" || -L "$timer_path" ]] \
    && ! user_unit_file_matches "$timer_path" \
      "Description=Run FXRoute Spotify cache cleanup periodically" \
      "Persistent=true" \
      "WantedBy=timers.target"; then
    warn "Refusing to remove the Spotify cache cleanup timer because its unit content is not FXRoute-owned"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! stop_owned_user_service "$timer_name" "$timer_path"; then
    return 0
  fi
  if ! stop_owned_user_service "$service_name" "$service_path"; then
    return 0
  fi
  remove_file_if_exists "$service_path"
  remove_file_if_exists "$timer_path"
}

system_update_units_are_owned() {
  local service_path="/etc/systemd/system/fxroute-system-update.service"
  local timer_path="/etc/systemd/system/fxroute-system-update.timer"
  local owned=""
  local service_sha256=""
  local timer_sha256=""

  owned="$(read_install_state_field lan_comfort.system_update_owned_by_fxroute 2>/dev/null || true)"
  [[ "$owned" == "true" ]] || return 1
  service_sha256="$(read_install_state_field lan_comfort.system_update_service_sha256 2>/dev/null || true)"
  timer_sha256="$(read_install_state_field lan_comfort.system_update_timer_sha256 2>/dev/null || true)"

  if [[ -e "$service_path" || -L "$service_path" ]]; then
    [[ -f "$service_path" && ! -L "$service_path" ]] \
      && [[ -n "$service_sha256" ]] \
      && [[ "$(sha256sum "$service_path" | awk '{print $1}')" == "$service_sha256" ]] \
      || return 1
  fi
  if [[ -e "$timer_path" || -L "$timer_path" ]]; then
    [[ -f "$timer_path" && ! -L "$timer_path" ]] \
      && [[ -n "$timer_sha256" ]] \
      && [[ "$(sha256sum "$timer_path" | awk '{print $1}')" == "$timer_sha256" ]] \
      || return 1
  fi
  return 0
}

stop_system_update_units() {
  local sudo_cmd=("$@")
  local service_name="fxroute-system-update.service"
  local timer_name="fxroute-system-update.timer"
  local unit=""
  local load_state=""
  local active_state=""
  local unit_file_state=""

  for unit in "$service_name" "$timer_name"; do
    if ! load_state="$("${sudo_cmd[@]}" systemctl show "$unit" -p LoadState --value 2>/dev/null)"; then
      return 1
    fi
    [[ "$load_state" == "not-found" ]] && continue
    [[ -n "$load_state" ]] || return 1
    if ! active_state="$("${sudo_cmd[@]}" systemctl show "$unit" -p ActiveState --value 2>/dev/null)"; then
      return 1
    fi
    case "$active_state" in
      inactive|failed|dead) ;;
      active|activating|deactivating|reloading)
        "${sudo_cmd[@]}" systemctl stop "$unit" >/dev/null 2>&1 || return 1
        active_state="$("${sudo_cmd[@]}" systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
        case "$active_state" in
          inactive|failed|dead) ;;
          *) return 1 ;;
        esac
        ;;
      *)
        return 1
        ;;
    esac
  done

  if ! load_state="$("${sudo_cmd[@]}" systemctl show "$timer_name" -p LoadState --value 2>/dev/null)"; then
    return 1
  fi
  [[ "$load_state" == "not-found" ]] && return 0
  "${sudo_cmd[@]}" systemctl disable "$timer_name" >/dev/null 2>&1 || true
  unit_file_state="$("${sudo_cmd[@]}" systemctl show "$timer_name" -p UnitFileState --value 2>/dev/null)" || return 1
  case "$unit_file_state" in
    enabled|enabled-runtime|linked|linked-runtime|alias)
      return 1
      ;;
    "")
      return 1
      ;;
  esac
  return 0
}

restore_system_update_transaction() {
  local backup_dir="$1"
  local service_path="/etc/systemd/system/fxroute-system-update.service"
  local timer_path="/etc/systemd/system/fxroute-system-update.timer"
  local helper_path="/usr/local/sbin/fxroute-system-package-update"
  local service_name="fxroute-system-update.service"
  local timer_name="fxroute-system-update.timer"
  local service_present="$2"
  local timer_present="$3"
  local helper_present="$4"
  local service_was_active="$5"
  local timer_was_enabled="$6"
  local timer_was_active="$7"
  local sudo_cmd=("${@:8}")
  local restore_failed=0

  "${sudo_cmd[@]}" systemctl disable --now "$timer_name" >/dev/null 2>&1 || true
  "${sudo_cmd[@]}" systemctl stop "$service_name" >/dev/null 2>&1 || true

  if [[ "$service_present" -eq 1 ]]; then
    "${sudo_cmd[@]}" install -m 644 "$backup_dir/service" "$service_path" || restore_failed=1
  else
    "${sudo_cmd[@]}" rm -f "$service_path" || restore_failed=1
  fi
  if [[ "$timer_present" -eq 1 ]]; then
    "${sudo_cmd[@]}" install -m 644 "$backup_dir/timer" "$timer_path" || restore_failed=1
  else
    "${sudo_cmd[@]}" rm -f "$timer_path" || restore_failed=1
  fi
  if [[ "$helper_present" -eq 1 ]]; then
    "${sudo_cmd[@]}" install -m 755 "$backup_dir/helper" "$helper_path" || restore_failed=1
  else
    "${sudo_cmd[@]}" rm -f "$helper_path" || restore_failed=1
  fi
  "${sudo_cmd[@]}" systemctl daemon-reload >/dev/null 2>&1 || restore_failed=1

  if [[ "$service_was_active" -eq 1 ]]; then
    "${sudo_cmd[@]}" systemctl start "$service_name" >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ "$timer_was_enabled" -eq 1 ]]; then
    if [[ "$timer_was_active" -eq 1 ]]; then
      "${sudo_cmd[@]}" systemctl enable --now "$timer_name" >/dev/null 2>&1 || restore_failed=1
    else
      "${sudo_cmd[@]}" systemctl enable "$timer_name" >/dev/null 2>&1 || restore_failed=1
    fi
  elif [[ "$timer_was_active" -eq 1 ]]; then
    "${sudo_cmd[@]}" systemctl start "$timer_name" >/dev/null 2>&1 || restore_failed=1
  fi

  return "$restore_failed"
}

remove_optional_system_update_helper() {
  local service_path="/etc/systemd/system/fxroute-system-update.service"
  local timer_path="/etc/systemd/system/fxroute-system-update.timer"
  local helper_path="/usr/local/sbin/fxroute-system-package-update"
  local sudo_cmd=()
  local backup_dir=""
  local service_present=0
  local timer_present=0
  local helper_present=0
  local service_was_active=0
  local timer_was_enabled=0
  local timer_was_active=0
  local load_state=""
  local unit_state=""

  if [[ ! -e "$service_path" && ! -e "$timer_path" && ! -e "$helper_path" && ! -L "$helper_path" ]]; then
    return 0
  fi

  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    sudo_cmd=()
  elif command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot remove optional FXRoute system update helper because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if ! system_update_units_are_owned; then
    warn "Refusing to remove non-FXRoute-owned system auto-update units"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ -e "$helper_path" || -L "$helper_path" ]]; then
    if [[ ! -f "$helper_path" || -L "$helper_path" \
      || "$("${sudo_cmd[@]}" sha256sum "$helper_path" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
      warn "Refusing to remove an unverified FXRoute system auto-update helper"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    helper_present=1
  fi
  [[ -f "$service_path" ]] && service_present=1
  [[ -f "$timer_path" ]] && timer_present=1
  if ! load_state="$("${sudo_cmd[@]}" systemctl show fxroute-system-update.service -p LoadState --value 2>/dev/null)" \
    || [[ -z "$load_state" ]]; then
    warn "Could not verify the system auto-update service state"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ "$load_state" != "not-found" ]]; then
    if ! unit_state="$("${sudo_cmd[@]}" systemctl show fxroute-system-update.service -p ActiveState --value 2>/dev/null)" \
      || [[ -z "$unit_state" ]]; then
      warn "Could not verify the system auto-update service state"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    case "$unit_state" in
      active|activating|deactivating|reloading) service_was_active=1 ;;
    esac
  fi
  if ! load_state="$("${sudo_cmd[@]}" systemctl show fxroute-system-update.timer -p LoadState --value 2>/dev/null)" \
    || [[ -z "$load_state" ]]; then
    warn "Could not verify the system auto-update timer state"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ "$load_state" != "not-found" ]]; then
    if ! unit_state="$("${sudo_cmd[@]}" systemctl show fxroute-system-update.timer -p UnitFileState --value 2>/dev/null)" \
      || [[ -z "$unit_state" ]]; then
      warn "Could not verify the system auto-update timer state"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    case "$unit_state" in
      enabled|enabled-runtime|linked|linked-runtime|alias) timer_was_enabled=1 ;;
    esac
    if ! unit_state="$("${sudo_cmd[@]}" systemctl show fxroute-system-update.timer -p ActiveState --value 2>/dev/null)" \
      || [[ -z "$unit_state" ]]; then
      warn "Could not verify the system auto-update timer state"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    case "$unit_state" in
      active|activating|deactivating|reloading) timer_was_active=1 ;;
    esac
  fi
  if ! backup_dir="$("${sudo_cmd[@]}" mktemp -d -t fxroute-system-update-backup.XXXXXX)"; then
    warn "Could not create a rollback area for the system auto-update helper"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ $service_present -eq 1 ]] && ! "${sudo_cmd[@]}" cp -p "$service_path" "$backup_dir/service"; then
    warn "Could not back up the system auto-update service before removing it"
    "${sudo_cmd[@]}" rm -rf "$backup_dir"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ $timer_present -eq 1 ]] && ! "${sudo_cmd[@]}" cp -p "$timer_path" "$backup_dir/timer"; then
    warn "Could not back up the system auto-update timer before removing it"
    "${sudo_cmd[@]}" rm -rf "$backup_dir"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ $helper_present -eq 1 ]] && ! "${sudo_cmd[@]}" cp -p "$helper_path" "$backup_dir/helper"; then
    warn "Could not back up the system auto-update helper before removing it"
    "${sudo_cmd[@]}" rm -rf "$backup_dir"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! stop_system_update_units "${sudo_cmd[@]}"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      "${sudo_cmd[@]}" || warn "Could not fully restore the system auto-update state"
    warn "Refusing to remove system auto-update units that could not be stopped safely"
    "${sudo_cmd[@]}" rm -rf "$backup_dir"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ -e "$helper_path" || -L "$helper_path" ]]; then
    if [[ ! -f "$helper_path" || -L "$helper_path" \
      || "$("${sudo_cmd[@]}" sha256sum "$helper_path" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        "${sudo_cmd[@]}" || warn "Could not fully restore the system auto-update state"
      warn "Refusing to remove a changed FXRoute system auto-update helper"
      "${sudo_cmd[@]}" rm -rf "$backup_dir"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi

  if ! "${sudo_cmd[@]}" rm -f "$service_path" "$timer_path"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      "${sudo_cmd[@]}" || warn "Could not fully restore the system auto-update state"
    warn "Could not remove optional FXRoute system update helper files"
    "${sudo_cmd[@]}" rm -rf "$backup_dir"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ $helper_present -eq 1 ]]; then
    if ! "${sudo_cmd[@]}" rm -f "$helper_path"; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        "${sudo_cmd[@]}" || warn "Could not fully restore the system auto-update state"
      warn "Could not remove the FXRoute system auto-update helper"
      "${sudo_cmd[@]}" rm -rf "$backup_dir"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi
  if ! "${sudo_cmd[@]}" systemctl daemon-reload >/dev/null 2>&1; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      "${sudo_cmd[@]}" || warn "Could not fully restore the system auto-update state"
    warn "Could not reload systemd after removing the system auto-update helper"
    "${sudo_cmd[@]}" rm -rf "$backup_dir"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  "${sudo_cmd[@]}" rm -rf "$backup_dir"
  log "Removed optional FXRoute system update helper"
}

remove_owned_helper() {
  local path="$1"
  local expected=""

  [[ -e "$path" || -L "$path" ]] || return 0
  case "$(basename "$path")" in
    fxroute-status)
      expected="#!/usr/bin/env bash"$'\n'"exec systemctl --user status $SERVICE_NAME"
      ;;
    fxroute-logs)
      expected="#!/usr/bin/env bash"$'\n'"exec journalctl --user -u $SERVICE_NAME -f"
      ;;
    fxroute-restart)
      expected="#!/usr/bin/env bash"$'\n'"exec systemctl --user restart $SERVICE_NAME"
      ;;
    fxroute-update)
      expected="#!/usr/bin/env bash"$'\n'"set -euo pipefail"$'\n'"exec \"$INSTALL_ROOT/scripts/update_fxroute.sh\" \"\$@\""
      ;;
    fxroute-update-ytdlp)
      expected="#!/usr/bin/env bash"$'\n'"set -euo pipefail"$'\n'"exec \"$INSTALL_ROOT/.venv/bin/pip\" install -U yt-dlp"
      ;;
    *)
      warn "Refusing to remove an unrecognized FXRoute helper: $path"
      PRESERVE_INSTALL_STATE=1
      return 0
      ;;
  esac
  if [[ ! -f "$path" || -L "$path" ]] || ! cmp -s "$path" <(printf '%s\n' "$expected"); then
    warn "Refusing to remove a changed FXRoute helper: $path"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  remove_file_if_exists "$path"
}

remove_helpers() {
  remove_owned_helper "$HOME/.local/bin/fxroute-status"
  remove_owned_helper "$HOME/.local/bin/fxroute-logs"
  remove_owned_helper "$HOME/.local/bin/fxroute-restart"
  remove_owned_helper "$HOME/.local/bin/fxroute-update"
  remove_owned_helper "$HOME/.local/bin/fxroute-update-ytdlp"
}

target_cifs_entries_present() {
  local mount_root="/var/lib/fxroute/music-libraries/$FXROUTE_TARGET_UID"
  [[ -f /etc/fstab ]] || return 1
  awk -v prefix="$mount_root/" '$2 ~ ("^" prefix) && $3 == "cifs" {found = 1} END {exit found ? 0 : 1}' /etc/fstab
}

remove_network_library_helper() {
  local sudo_cmd=()
  local helper_path="/usr/local/sbin/fxroute-cifs-mount"
  local sudoers_path="/etc/sudoers.d/fxroute-cifs-mount"
  local sudoers_rule=""
  local existing_sudoers=""
  local tmp_sudoers=""
  local helper_sha256=""
  local target_entries=0
  local helper_owned=0
  local helper_verified=0
  local sudoers_owned=0
  local recorded_sudoers_sha256=""
  local fstab_status=0

  if [[ "$(id -u)" -eq 0 ]]; then
    sudo_cmd=()
  elif command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Could not remove the network library mount helper because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if target_cifs_entries_present; then
    target_entries=1
  else
    fstab_status=$?
    if [[ $fstab_status -ne 1 ]]; then
      warn "Could not inspect /etc/fstab for FXRoute CIFS entries"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi
  [[ "$(read_install_state_field lan_comfort.cifs_helper_installed_by_fxroute 2>/dev/null || true)" == "true" ]] \
    && helper_owned=1
  [[ "$(read_install_state_field lan_comfort.cifs_sudoers_rule_installed_by_fxroute 2>/dev/null || true)" == "true" ]] \
    && sudoers_owned=1
  recorded_sudoers_sha256="$(read_install_state_field lan_comfort.cifs_sudoers_sha256 2>/dev/null || true)"
  if [[ -e "$helper_path" || -L "$helper_path" ]]; then
    if [[ ! -f "$helper_path" || -L "$helper_path" ]]; then
      helper_owned=0
    else
      helper_sha256="$("${sudo_cmd[@]}" sha256sum "$helper_path" | awk '{print $1}')"
      [[ "$helper_sha256" == "$CIFS_HELPER_SHA256" ]] && helper_verified=1
      [[ $helper_verified -eq 1 ]] || helper_owned=0
    fi
    if [[ $helper_verified -eq 0 ]]; then
      warn "Refusing to remove a changed FXRoute CIFS mount helper"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
  if [[ $target_entries -eq 1 && "$FXROUTE_TARGET_USER" == root ]]; then
    warn "Cannot remove FXRoute CIFS entries for the root user safely"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ $target_entries -eq 1 && "$FXROUTE_TARGET_USER" != root ]]; then
    if [[ ! -x "$helper_path" ]]; then
      warn "Cannot remove FXRoute CIFS entries because the privileged helper is unavailable"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    if [[ $helper_owned -eq 1 && $helper_verified -eq 1 ]]; then
      if [[ "$(id -u)" -eq 0 ]]; then
        if ! SUDO_USER="$FXROUTE_TARGET_USER" "$helper_path" --remove-all 2>/dev/null; then
          warn "Could not remove FXRoute CIFS mount entries for $FXROUTE_TARGET_USER"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      elif ! sudo "$helper_path" --remove-all 2>/dev/null; then
        warn "Could not remove FXRoute CIFS mount entries for $FXROUTE_TARGET_USER"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    else
      warn "Refusing to run an unowned or unverified FXRoute CIFS mount helper"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi
  if [[ $target_entries -eq 1 ]] && target_cifs_entries_present; then
    warn "FXRoute CIFS entries remain after helper cleanup"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  sudoers_rule="$FXROUTE_TARGET_USER ALL=(root) NOPASSWD: $helper_path *"
  existing_sudoers="$("${sudo_cmd[@]}" cat "$sudoers_path" 2>/dev/null || true)"
  if [[ $sudoers_owned -eq 1 && -e "$sudoers_path" \
    && ( -z "$recorded_sudoers_sha256" \
      || "$("${sudo_cmd[@]}" sha256sum "$sudoers_path" | awk '{print $1}')" != "$recorded_sudoers_sha256" ) ]]; then
    warn "Refusing to modify a changed FXRoute CIFS sudoers file"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ $sudoers_owned -eq 1 ]] && grep -Fqx -- "$sudoers_rule" <<<"$existing_sudoers"; then
    tmp_sudoers="$(mktemp)"
    awk -v rule="$sudoers_rule" '$0 != rule {print}' <<<"$existing_sudoers" > "$tmp_sudoers"
    if command -v visudo >/dev/null 2>&1; then
      if ! "${sudo_cmd[@]}" visudo -cf "$tmp_sudoers" >/dev/null; then
        rm -f "$tmp_sudoers"
        warn "Could not validate the remaining FXRoute CIFS sudoers rules"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if ! "${sudo_cmd[@]}" install -m 440 "$tmp_sudoers" "$sudoers_path"; then
      rm -f "$tmp_sudoers"
      warn "Could not update the FXRoute CIFS sudoers rules"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    rm -f "$tmp_sudoers"
  fi

  existing_sudoers="$("${sudo_cmd[@]}" cat "$sudoers_path" 2>/dev/null || true)"
  if [[ $sudoers_owned -eq 1 && -z "$(grep -Ev '^[[:space:]]*(#|$)' <<<"$existing_sudoers" || true)" ]]; then
    "${sudo_cmd[@]}" rm -f "$sudoers_path"
  fi
  if [[ $helper_owned -eq 1 ]]; then
    "${sudo_cmd[@]}" rm -f "$helper_path"
  fi
}

remove_autostart() {
  remove_file_if_exists "$HOME/.config/autostart/fxroute-spotify.desktop"
}

provider_sudo_available() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || command -v sudo >/dev/null 2>&1
}

spotify_apt_source_is_owned() {
  local expected_sha256=""
  local actual_sha256=""
  local canonical_line="deb [arch=amd64 signed-by=${SPOTIFY_APT_KEY_FILE}] https://repository.spotify.com stable non-free"

  [[ -f "$SPOTIFY_APT_SOURCE_FILE" && ! -L "$SPOTIFY_APT_SOURCE_FILE" ]] || return 1
  expected_sha256="$(read_install_state_field "providers.spotify_desktop.apt_repo_sha256" 2>/dev/null || true)"
  if [[ -n "$expected_sha256" ]]; then
    actual_sha256="$(sha256sum "$SPOTIFY_APT_SOURCE_FILE" 2>/dev/null | awk '{print $1}')" || return 1
    [[ "$actual_sha256" == "$expected_sha256" ]]
    return
  fi
  grep -Fxq "$canonical_line" "$SPOTIFY_APT_SOURCE_FILE"
}

spotify_apt_key_is_owned() {
  local expected_fingerprint=""
  local actual_fingerprint=""

  [[ -f "$SPOTIFY_APT_KEY_FILE" && ! -L "$SPOTIFY_APT_KEY_FILE" ]] || return 1
  command -v gpg >/dev/null 2>&1 || return 1
  expected_fingerprint="$(read_install_state_field "providers.spotify_desktop.apt_key_fingerprint" 2>/dev/null || true)"
  [[ -n "$expected_fingerprint" ]] || expected_fingerprint="$SPOTIFY_APT_KEY_FINGERPRINT"
  actual_fingerprint="$(gpg --show-keys --with-colons --fingerprint "$SPOTIFY_APT_KEY_FILE" 2>/dev/null \
    | awk -F: '$1 == "fpr" {print toupper($10); exit}')"
  [[ "$actual_fingerprint" == "$expected_fingerprint" \
    && "$actual_fingerprint" == "$SPOTIFY_APT_KEY_FINGERPRINT" ]]
}

remove_owned_spotify_desktop() {
  local installed_by_fxroute=""
  local flatpak_installed_by_fxroute=""
  local apt_repo_installed_by_fxroute=""
  local apt_key_installed_by_fxroute=""
  local installed_version=""
  local current_version=""
  local install_method=""
  local sudo_cmd=()
  local component_ok=1

  installed_by_fxroute="$(read_install_state_field "providers.spotify_desktop.installed_by_fxroute" 2>/dev/null || true)"
  [[ "$installed_by_fxroute" == "true" ]] || return 0
  flatpak_installed_by_fxroute="$(read_install_state_field "providers.spotify_desktop.flatpak_installed_by_fxroute" 2>/dev/null || true)"
  apt_repo_installed_by_fxroute="$(read_install_state_field "providers.spotify_desktop.apt_repo_installed_by_fxroute" 2>/dev/null || true)"
  apt_key_installed_by_fxroute="$(read_install_state_field "providers.spotify_desktop.apt_key_installed_by_fxroute" 2>/dev/null || true)"
  [[ -n "$apt_key_installed_by_fxroute" ]] || apt_key_installed_by_fxroute="$apt_repo_installed_by_fxroute"
  installed_version="$(read_install_state_field "providers.spotify_desktop.installed_version" 2>/dev/null || true)"
  install_method="$(read_install_state_field "providers.spotify_desktop.install_method" 2>/dev/null || true)"

  if ! confirm "Remove FXRoute-owned Spotify Desktop (${install_method:-native})? Spotify profile/cache data will be preserved."; then
    warn "Keeping FXRoute-owned Spotify Desktop"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! provider_sudo_available; then
    warn "Cannot remove FXRoute-owned Spotify Desktop because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  [[ ${EUID:-$(id -u)} -eq 0 ]] || sudo_cmd=(sudo)

  if [[ "$install_method" == "flatpak" ]]; then
    if command -v flatpak >/dev/null 2>&1 && "${sudo_cmd[@]}" flatpak info --system com.spotify.Client >/dev/null 2>&1; then
      current_version="$("${sudo_cmd[@]}" flatpak info --system --show=version com.spotify.Client 2>/dev/null || true)"
    fi
  elif command -v dpkg-query >/dev/null 2>&1 && dpkg-query -W -f='${Status}' spotify-client 2>/dev/null | grep -q 'ok installed'; then
    current_version="$(dpkg-query -W -f='${Version}' spotify-client 2>/dev/null || true)"
  fi
  if [[ -n "$current_version" && ( -z "$installed_version" || "$current_version" != "$installed_version" ) ]]; then
    warn "Refusing to remove FXRoute-owned Spotify Desktop because its recorded version changed"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ "$flatpak_installed_by_fxroute" == "true" || "$install_method" == "flatpak" ]]; then
    if command -v flatpak >/dev/null 2>&1 \
      && "${sudo_cmd[@]}" flatpak list --app --system --columns=application 2>/dev/null | grep -Fxq com.spotify.Client; then
      if ! "${sudo_cmd[@]}" flatpak uninstall --system --assumeyes com.spotify.Client; then
        warn "Failed to remove the FXRoute-installed Spotify Flatpak"
        component_ok=0
      fi
    else
      log "FXRoute-installed Spotify Flatpak is already absent"
    fi
  elif command -v apt-get >/dev/null 2>&1; then
    if dpkg-query -W -f='${Status}' spotify-client 2>/dev/null | grep -q 'ok installed'; then
      if ! "${sudo_cmd[@]}" apt-get remove -y spotify-client; then
        warn "Failed to remove the FXRoute-installed Spotify package"
        component_ok=0
      fi
    else
      log "FXRoute-installed Spotify package is already absent"
    fi
  else
    warn "Cannot remove native Spotify Desktop because apt-get is unavailable"
    component_ok=0
  fi

  if [[ $component_ok -eq 0 ]]; then
    PRESERVE_INSTALL_STATE=1
  fi
  if [[ "$apt_repo_installed_by_fxroute" == "true" && $component_ok -eq 1 ]]; then
    local repo_paths=()
    local apt_source_removed=0
    if [[ -e "$SPOTIFY_APT_SOURCE_FILE" || -L "$SPOTIFY_APT_SOURCE_FILE" ]]; then
      if spotify_apt_source_is_owned; then
        repo_paths+=("$SPOTIFY_APT_SOURCE_FILE")
        apt_source_removed=1
      else
        warn "Refusing to remove the Spotify apt source because its contents changed"
        PRESERVE_INSTALL_STATE=1
      fi
    fi
    if [[ "$apt_key_installed_by_fxroute" == "true" \
      && $apt_source_removed -eq 1 \
      && ( -e "$SPOTIFY_APT_KEY_FILE" || -L "$SPOTIFY_APT_KEY_FILE" ) ]]; then
      if spotify_apt_key_is_owned; then
        repo_paths+=("$SPOTIFY_APT_KEY_FILE")
      else
        warn "Refusing to remove the Spotify apt keyring because its fingerprint changed"
        PRESERVE_INSTALL_STATE=1
      fi
    fi
    if [[ ${#repo_paths[@]} -eq 0 ]]; then
      return 0
    fi
    if "${sudo_cmd[@]}" rm -f "${repo_paths[@]}"; then
      log "Removed the FXRoute-owned Spotify apt repository and keyring"
    else
      warn "Failed to remove the FXRoute-owned Spotify apt repository and keyring"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
}

remove_owned_spotifyd() {
  local installed_by_fxroute=""
  local service_installed_by_fxroute=""
  local binary_path=""
  local service_path=""

  installed_by_fxroute="$(read_install_state_field "providers.spotifyd.installed_by_fxroute" 2>/dev/null || true)"
  service_installed_by_fxroute="$(read_install_state_field "providers.spotifyd.service_installed_by_fxroute" 2>/dev/null || true)"
  [[ "$installed_by_fxroute" == "true" || "$service_installed_by_fxroute" == "true" ]] || return 0

  if ! confirm "Remove FXRoute-owned spotifyd binary and user service? spotifyd config/cache/session data will be preserved."; then
    warn "Keeping FXRoute-owned spotifyd components"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  binary_path="$(read_install_state_field "providers.spotifyd.binary_path" 2>/dev/null || true)"
  service_path="$(read_install_state_field "providers.spotifyd.service_path" 2>/dev/null || true)"
  [[ -n "$binary_path" ]] || binary_path="$HOME/.local/bin/spotifyd"
  [[ -n "$service_path" ]] || service_path="$HOME/.config/systemd/user/spotifyd.service"

  if [[ "$installed_by_fxroute" == "true" ]] && ! verify_owned_binary_identity "$binary_path" "$(read_install_state_field "providers.spotifyd.binary_sha256" 2>/dev/null || true)" "FXRoute-owned spotifyd"; then
    return 0
  fi
  if [[ "$service_installed_by_fxroute" == "true" ]]; then
    if [[ "$service_path" != "$HOME/.config/systemd/user/spotifyd.service" ]]; then
      warn "Refusing to remove an unexpected spotifyd service path: $service_path"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    if [[ -e "$service_path" || -L "$service_path" ]] \
      && ! verify_owned_binary_identity "$service_path" "$(read_install_state_field "providers.spotifyd.service_sha256" 2>/dev/null || true)" "FXRoute-owned spotifyd service"; then
      return 0
    fi
    if ! stop_owned_user_service spotifyd.service "$HOME/.config/systemd/user/spotifyd.service"; then
      return 0
    fi
    if [[ -e "$service_path" || -L "$service_path" ]]; then
      remove_file_if_exists "$service_path"
    else
      log "FXRoute-owned spotifyd service is already absent"
    fi
  fi
  if [[ "$installed_by_fxroute" == "true" ]]; then
    if [[ -e "$binary_path" || -L "$binary_path" ]]; then
      if [[ "$binary_path" == "$HOME/.local/bin/spotifyd" ]]; then
        remove_file_if_exists "$binary_path"
      else
        warn "Refusing to remove an unexpected spotifyd binary path: $binary_path"
        PRESERVE_INSTALL_STATE=1
      fi
    else
      log "FXRoute-owned spotifyd binary is already absent"
    fi
  fi
  user_systemctl daemon-reload >/dev/null 2>&1 || true
}

remove_owned_qbzd() {
  local installed_by_fxroute=""
  local service_installed_by_fxroute=""
  local binary_path=""
  local service_path=""

  installed_by_fxroute="$(read_install_state_field "providers.qobuz.installed_by_fxroute" 2>/dev/null || true)"
  service_installed_by_fxroute="$(read_install_state_field "providers.qobuz.service_installed_by_fxroute" 2>/dev/null || true)"
  [[ "$installed_by_fxroute" == "true" || "$service_installed_by_fxroute" == "true" ]] || return 0

  if ! confirm "Remove FXRoute-owned qbzd binary and user service? Qobuz config, OAuth data, and cache will be preserved."; then
    warn "Keeping FXRoute-owned qbzd components"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  binary_path="$(read_install_state_field "providers.qobuz.binary_path" 2>/dev/null || true)"
  service_path="$(read_install_state_field "providers.qobuz.service_path" 2>/dev/null || true)"
  [[ -n "$binary_path" ]] || binary_path="$HOME/.local/bin/qbzd"
  [[ -n "$service_path" ]] || service_path="$HOME/.config/systemd/user/qbzd.service"

  if [[ "$installed_by_fxroute" == "true" ]] && ! verify_owned_binary_identity "$binary_path" "$(read_install_state_field "providers.qobuz.binary_sha256" 2>/dev/null || true)" "FXRoute-owned qbzd"; then
    return 0
  fi
  if [[ "$service_installed_by_fxroute" == "true" ]]; then
    if [[ "$service_path" != "$HOME/.config/systemd/user/qbzd.service" ]]; then
      warn "Refusing to remove an unexpected qbzd service path: $service_path"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    if [[ -e "$service_path" || -L "$service_path" ]] \
      && ! verify_owned_binary_identity "$service_path" "$(read_install_state_field "providers.qobuz.service_sha256" 2>/dev/null || true)" "FXRoute-owned qbzd service"; then
      return 0
    fi
    if ! stop_owned_user_service qbzd.service "$HOME/.config/systemd/user/qbzd.service"; then
      return 0
    fi
    if [[ -e "$service_path" || -L "$service_path" ]]; then
      remove_file_if_exists "$service_path"
    else
      log "FXRoute-owned qbzd service is already absent"
    fi
  fi
  if [[ "$installed_by_fxroute" == "true" ]]; then
    if [[ -e "$binary_path" || -L "$binary_path" ]]; then
      if [[ "$binary_path" == "$HOME/.local/bin/qbzd" ]]; then
        remove_file_if_exists "$binary_path"
      else
        warn "Refusing to remove an unexpected qbzd binary path: $binary_path"
        PRESERVE_INSTALL_STATE=1
      fi
    else
      log "FXRoute-owned qbzd binary is already absent"
    fi
  fi
  user_systemctl daemon-reload >/dev/null 2>&1 || true
}

read_qbzd_volume_mode_for_uninstall() {
  local binary_path="$1"
  [[ -x "$binary_path" ]] || return 1
  run_as_target_user "$binary_path" settings show --quiet --json 2>/dev/null | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
    value = payload.get("qconnect.volume_mode", "")
except (ValueError, OSError):
    raise SystemExit(1)
if not value:
    raise SystemExit(1)
print(value)
'
}

clear_qbzd_volume_ownership_record() {
  local state_file="$INSTALL_STATE_FILE"
  local temp_file=""

  [[ -f "$state_file" ]] || return 0
  temp_file="$(mktemp "$(dirname "$state_file")/.install-state.XXXXXX")" || return 1
  if ! python3 - "$state_file" "$temp_file" <<'PY'
import json
import sys
from pathlib import Path

state_path = Path(sys.argv[1])
temp_path = Path(sys.argv[2])
try:
    payload = json.loads(state_path.read_text())
    qobuz = payload["providers"]["qobuz"]
    qobuz["volume_mode_changed_by_fxroute"] = False
    qobuz["volume_mode_before"] = ""
    qobuz["volume_mode_after"] = ""
    temp_path.write_text(json.dumps(payload, indent=2) + "\n")
except (KeyError, OSError, TypeError, ValueError):
    raise SystemExit(1)
PY
  then
    rm -f "$temp_file"
    return 1
  fi
  chmod 600 "$temp_file"
  if ! mv -f "$temp_file" "$state_file"; then
    rm -f "$temp_file"
    return 1
  fi
  return 0
}

restore_qbzd_volume_mode_if_owned() {
  local changed_by_fxroute=""
  local mode_before=""
  local mode_after=""
  local binary_path=""
  local current_mode=""

  changed_by_fxroute="$(read_install_state_field "providers.qobuz.volume_mode_changed_by_fxroute" 2>/dev/null || true)"
  [[ "$changed_by_fxroute" == "true" ]] || return 0
  mode_before="$(read_install_state_field "providers.qobuz.volume_mode_before" 2>/dev/null || true)"
  mode_after="$(read_install_state_field "providers.qobuz.volume_mode_after" 2>/dev/null || true)"
  if [[ -z "$mode_before" || -z "$mode_after" ]]; then
    warn "Cannot restore the previous qbzd volume mode because its ownership record is incomplete"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi

  binary_path="$(read_install_state_field "providers.qobuz.binary_path" 2>/dev/null || true)"
  if [[ -z "$binary_path" || ! -x "$binary_path" ]]; then
    warn "Cannot restore qbzd qconnect.volume_mode=$mode_before because its recorded qbzd binary is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  if ! verify_owned_binary_identity "$binary_path" "$(read_install_state_field "providers.qobuz.binary_sha256" 2>/dev/null || true)" "recorded qbzd"; then
    return 1
  fi

  current_mode="$(read_qbzd_volume_mode_for_uninstall "$binary_path" || true)"
  if [[ "$current_mode" == "$mode_before" ]]; then
    if ! clear_qbzd_volume_ownership_record; then
      warn "qbzd volume mode is restored, but its ownership record could not be cleared"
      PRESERVE_INSTALL_STATE=1
      return 1
    fi
    log "qbzd volume mode is already restored to $mode_before"
    return 0
  fi
  if [[ "$current_mode" != "$mode_after" ]]; then
    warn "Not restoring qbzd volume mode: it changed from FXRoute's recorded value '$mode_after' after installation"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  if ! confirm "FXRoute set qbzd qconnect.volume_mode=$mode_after for the unity volume bridge. Restore $mode_before?"; then
    warn "Keeping qbzd qconnect.volume_mode=$mode_after"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  if ! run_as_target_user "$binary_path" settings set --quiet qconnect.volume_mode "$mode_before" >/dev/null 2>&1; then
    warn "Failed to restore qbzd qconnect.volume_mode=$mode_before"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  if [[ "$(read_qbzd_volume_mode_for_uninstall "$binary_path" || true)" != "$mode_before" ]]; then
    warn "qbzd did not retain restored qconnect.volume_mode=$mode_before"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  if ! clear_qbzd_volume_ownership_record; then
    warn "qbzd volume mode is restored, but its ownership record could not be cleared"
    PRESERVE_INSTALL_STATE=1
    return 1
  fi
  log "Restored qbzd qconnect.volume_mode=$mode_before"
}

tidalapi_installed_version() {
  local python_path="$INSTALL_ROOT/.venv/bin/python3"
  [[ -x "$python_path" ]] || return 1
  run_as_target_user "$python_path" - <<'PY'
from importlib.metadata import version
try:
    print(version("tidalapi"))
except Exception:
    raise SystemExit(1)
PY
}

remove_owned_tidal_dependency() {
  local installed_by_fxroute=""
  local pip_path="$INSTALL_ROOT/.venv/bin/pip"
  local marker_path="$INSTALL_ROOT/.venv/.fxroute-tidal-requirements.sha256"
  local installed_version=""
  local current_version=""

  installed_by_fxroute="$(read_install_state_field "providers.tidal.installed_by_fxroute" 2>/dev/null || true)"
  [[ "$installed_by_fxroute" == "true" ]] || return 0

  if ! confirm "Remove the FXRoute-owned TIDAL Python dependency? TIDAL session data will be preserved."; then
    warn "Keeping the FXRoute-owned TIDAL dependency"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ ! -x "$pip_path" ]]; then
    warn "Cannot remove the FXRoute-owned TIDAL dependency because the FXRoute virtualenv is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  installed_version="$(read_install_state_field "providers.tidal.installed_version" 2>/dev/null || true)"
  current_version="$(tidalapi_installed_version 2>/dev/null || true)"
  if [[ -n "$current_version" && ( -z "$installed_version" || "$current_version" != "$installed_version" ) ]]; then
    warn "Refusing to remove the FXRoute-owned TIDAL dependency because its version changed"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if run_as_target_user "$pip_path" uninstall -y tidalapi; then
    remove_file_if_exists "$marker_path"
    log "Removed the FXRoute-owned TIDAL Python dependency"
  else
    warn "Failed to remove the FXRoute-owned TIDAL Python dependency"
    PRESERVE_INSTALL_STATE=1
  fi
}

preserve_provider_data() {
  local path=""
  log "Provider config, credentials, sessions, and caches are preserved by default"
  for path in "${PROVIDER_DATA_PATHS[@]}"; do
    [[ -e "$path" || -L "$path" ]] && log "Preserved provider data: $path"
  done
  return 0
}

remove_owned_streaming_components() {
  local preserve_before=0
  local preserve_at_start="$PRESERVE_INSTALL_STATE"

  if [[ $preserve_at_start -eq 1 ]]; then
    PROVIDER_LAN_CLEANUP_DEFERRED=1
  fi

  if ! restore_qbzd_volume_mode_if_owned; then
    warn "Skipping qbzd binary/service removal until its FXRoute volume-mode change can be restored"
    PROVIDER_LAN_CLEANUP_DEFERRED=1
  else
    preserve_before=$PRESERVE_INSTALL_STATE
    remove_owned_qbzd
    if [[ $preserve_before -eq 0 && $PRESERVE_INSTALL_STATE -eq 1 ]]; then
      PROVIDER_LAN_CLEANUP_DEFERRED=1
    fi
  fi
  preserve_before=$PRESERVE_INSTALL_STATE
  remove_owned_spotify_desktop
  if [[ $preserve_before -eq 0 && $PRESERVE_INSTALL_STATE -eq 1 ]]; then
    PROVIDER_LAN_CLEANUP_DEFERRED=1
  fi
  preserve_before=$PRESERVE_INSTALL_STATE
  remove_owned_spotifyd
  if [[ $preserve_before -eq 0 && $PRESERVE_INSTALL_STATE -eq 1 ]]; then
    PROVIDER_LAN_CLEANUP_DEFERRED=1
  fi
  remove_owned_tidal_dependency
  preserve_provider_data
}

remove_mdns_guard_table_direct() {
  local sudo_cmd=("$@")
  local nft_path=""
  local ruleset=""

  nft_path="$(command -v nft 2>/dev/null || true)"
  [[ -n "$nft_path" ]] || return 1
  if ! "${sudo_cmd[@]}" "$nft_path" list table inet fxroute_mdnsguard >/dev/null 2>&1; then
    ruleset="$("${sudo_cmd[@]}" "$nft_path" list ruleset 2>/dev/null)" || return 1
    grep -Fq 'table inet fxroute_mdnsguard' <<<"$ruleset" && return 1
    return 0
  fi
  mdns_guard_table_matches "${sudo_cmd[@]}" || return 1
  if ! "${sudo_cmd[@]}" "$nft_path" delete table inet fxroute_mdnsguard >/dev/null 2>&1; then
    return 1
  fi
  if "${sudo_cmd[@]}" "$nft_path" list table inet fxroute_mdnsguard >/dev/null 2>&1; then
    return 1
  fi
  ruleset="$("${sudo_cmd[@]}" "$nft_path" list ruleset 2>/dev/null)" || return 1
  grep -Fq 'table inet fxroute_mdnsguard' <<<"$ruleset" && return 1
  return 0
}

mdns_guard_table_matches() {
  local sudo_cmd=("$@")
  local nft_path=""
  local expected_uid=""
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"

  nft_path="$(command -v nft 2>/dev/null || true)"
  [[ -n "$nft_path" ]] || return 1
  expected_uid="$(read_install_state_field "lan_comfort.mdns_guard_target_uid" 2>/dev/null || true)"
  if [[ -z "$expected_uid" && -f "$script_path" && ! -L "$script_path" ]]; then
    expected_uid="$(sed -n 's/^USER_ID="\([0-9][0-9]*\)"$/\1/p' "$script_path")"
  fi
  [[ "$expected_uid" =~ ^[0-9]+$ ]] || return 1
  "${sudo_cmd[@]}" "$nft_path" list table inet fxroute_mdnsguard 2>/dev/null \
    | awk -v uid="$expected_uid" '
      BEGIN {
        v4 = "^[[:space:]]*meta skuid[[:space:]]+" uid "[[:space:]]+ip daddr 224\\.0\\.0\\.251[[:space:]]+udp dport 5353[[:space:]]+counter[[:space:]]+packets[[:space:]]+[0-9]+[[:space:]]+bytes[[:space:]]+[0-9]+[[:space:]]+drop[[:space:]]+comment[[:space:]]+\"Block desktop user-space mDNS v4 to keep Avahi host advertisement stable\"[[:space:]]*$"
        v6 = "^[[:space:]]*meta skuid[[:space:]]+" uid "[[:space:]]+ip6 daddr ff02::fb[[:space:]]+udp dport 5353[[:space:]]+counter[[:space:]]+packets[[:space:]]+[0-9]+[[:space:]]+bytes[[:space:]]+[0-9]+[[:space:]]+drop[[:space:]]+comment[[:space:]]+\"Block desktop user-space mDNS v6 to keep Avahi host advertisement stable\"[[:space:]]*$"
      }
      /^[[:space:]]*table inet fxroute_mdnsguard[[:space:]]*\{[[:space:]]*$/ { tables++; next }
      /^[[:space:]]*chain output[[:space:]]*\{[[:space:]]*$/ { chains++; next }
      /^[[:space:]]*type filter hook output priority[[:space:]]+[^;]+;[[:space:]]*policy accept;[[:space:]]*$/ { next }
      $0 ~ v4 { v4_rules++; next }
      $0 ~ v6 { v6_rules++; next }
      /^[[:space:]]*\}[[:space:]]*$/ || /^[[:space:]]*$/ { next }
      { invalid++; next }
      END { exit !(tables == 1 && chains == 1 && v4_rules == 1 && v6_rules == 1 && invalid == 0) }
    '
}

mdns_guard_artifacts_match() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local script_sha256=""
  local service_sha256=""
  local timer_sha256=""
  local actual_sha256=""

  script_sha256="$(read_install_state_field "lan_comfort.mdns_guard_script_sha256" 2>/dev/null || true)"
  service_sha256="$(read_install_state_field "lan_comfort.mdns_guard_service_sha256" 2>/dev/null || true)"
  timer_sha256="$(read_install_state_field "lan_comfort.mdns_guard_timer_sha256" 2>/dev/null || true)"

  if [[ -e "$script_path" || -L "$script_path" ]]; then
    [[ -f "$script_path" && ! -L "$script_path" ]] || return 1
    [[ -n "$script_sha256" ]] || return 1
    actual_sha256="$(sha256sum "$script_path" | awk '{print $1}')" || return 1
    [[ "$actual_sha256" == "$script_sha256" ]] || return 1
  fi
  if [[ -e "$service_path" || -L "$service_path" ]]; then
    [[ -f "$service_path" && ! -L "$service_path" ]] || return 1
    [[ -n "$service_sha256" ]] || return 1
    actual_sha256="$(sha256sum "$service_path" | awk '{print $1}')" || return 1
    [[ "$actual_sha256" == "$service_sha256" ]] || return 1
  fi
  if [[ -e "$timer_path" || -L "$timer_path" ]]; then
    [[ -f "$timer_path" && ! -L "$timer_path" ]] || return 1
    [[ -n "$timer_sha256" ]] || return 1
    actual_sha256="$(sha256sum "$timer_path" | awk '{print $1}')" || return 1
    [[ "$actual_sha256" == "$timer_sha256" ]] || return 1
  fi
  return 0
}

legacy_mdns_guard_artifacts_match() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"

  if [[ -e "$script_path" || -L "$script_path" ]] \
    && { [[ -f "$script_path" && ! -L "$script_path" ]] \
      && grep -Fq '#!/usr/bin/env bash' "$script_path" \
      && grep -Fq 'TABLE="fxroute_mdnsguard"' "$script_path" \
      && grep -Fq 'meta skuid' "$script_path" \
      && grep -Fq 'case "${1:-apply}"' "$script_path"; }; then
    :
  elif [[ -e "$script_path" || -L "$script_path" ]]; then
    return 1
  fi
  if [[ -e "$service_path" || -L "$service_path" ]] \
    && { [[ -f "$service_path" && ! -L "$service_path" ]] \
      && grep -Fq 'ExecStart=/usr/local/sbin/fxroute-mdns-guard.sh apply' "$service_path" \
      && grep -Fq 'ExecReload=/usr/local/sbin/fxroute-mdns-guard.sh apply' "$service_path"; }; then
    :
  elif [[ -e "$service_path" || -L "$service_path" ]]; then
    return 1
  fi
  if [[ -e "$timer_path" || -L "$timer_path" ]] \
    && { [[ -f "$timer_path" && ! -L "$timer_path" ]] \
      && grep -Fq 'Unit=fxroute-mdns-guard.service' "$timer_path"; }; then
    :
  elif [[ -e "$timer_path" || -L "$timer_path" ]]; then
    return 1
  fi
  return 0
}

remove_optional_mdns_guard() {
  local sudo_cmd=()
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local marker_present=0
  local guard_owned=""
  local legacy_owned=0
  local timer_was_active=0
  local timer_was_enabled=0
  local timer_loaded=0
  local service_loaded=0
  local unit_status=0

  if [[ -e "$service_path" || -L "$service_path" \
    || -e "$timer_path" || -L "$timer_path" \
    || -e "$script_path" || -L "$script_path" ]]; then
    marker_present=1
  fi

  if guard_owned="$(read_install_state_field "lan_comfort.mdns_guard_owned_by_fxroute" 2>/dev/null)"; then
    :
  elif [[ "$(read_install_state_field "lan_comfort.mdns_guard_enabled" 2>/dev/null || true)" == "true" ]]; then
    guard_owned="true"
    legacy_owned=1
  else
    guard_owned=""
  fi
  if [[ "$guard_owned" == "true" \
    && ( -z "$(read_install_state_field "lan_comfort.mdns_guard_script_sha256" 2>/dev/null || true)" \
      || -z "$(read_install_state_field "lan_comfort.mdns_guard_service_sha256" 2>/dev/null || true)" \
      || -z "$(read_install_state_field "lan_comfort.mdns_guard_timer_sha256" 2>/dev/null || true)" ) ]]; then
    legacy_owned=1
  fi
  if [[ "$guard_owned" != "true" ]]; then
    if [[ "$guard_owned" == "false" \
      && "$(read_install_state_field "lan_comfort.mdns_guard_enabled" 2>/dev/null || true)" == "true" ]]; then
      warn "Preserving install state for an active mDNS guard without verified FXRoute ownership"
      PRESERVE_INSTALL_STATE=1
      PROVIDER_LAN_CLEANUP_DEFERRED=1
    fi
    log "Preserving mDNS guard artifacts without FXRoute ownership"
    return 0
  fi
  if [[ $legacy_owned -eq 1 ]]; then
    if ! legacy_mdns_guard_artifacts_match; then
      warn "Cannot safely migrate legacy mDNS guard ownership; keeping install state"
      PRESERVE_INSTALL_STATE=1
      PROVIDER_LAN_CLEANUP_DEFERRED=1
      return 0
    fi
  elif ! mdns_guard_artifacts_match; then
    warn "Preserving mDNS guard artifacts whose content no longer matches FXRoute"
    PRESERVE_INSTALL_STATE=1
    PROVIDER_LAN_CLEANUP_DEFERRED=1
    return 0
  fi

  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    sudo_cmd=()
  elif command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot remove optional FXRoute mDNS guard because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ $marker_present -eq 1 || "$guard_owned" == "true" ]]; then
    if systemd_unit_is_loaded fxroute-mdns-guard.timer "${sudo_cmd[@]}"; then
      timer_loaded=1
    else
      unit_status=$?
      if [[ $unit_status -eq 2 ]]; then
        warn "Could not verify a loaded FXRoute mDNS guard timer"
        PRESERVE_INSTALL_STATE=1
        PROVIDER_LAN_CLEANUP_DEFERRED=1
        return 0
      fi
    fi
    if [[ $timer_loaded -eq 1 ]] && systemctl is-active --quiet fxroute-mdns-guard.timer; then
      timer_was_active=1
    fi
    if [[ $timer_loaded -eq 1 ]] && systemctl is-enabled --quiet fxroute-mdns-guard.timer; then
      timer_was_enabled=1
    fi
    if systemd_unit_is_loaded fxroute-mdns-guard.service "${sudo_cmd[@]}"; then
      service_loaded=1
    else
      unit_status=$?
      if [[ $unit_status -eq 2 ]]; then
        warn "Could not verify a loaded FXRoute mDNS guard service"
        PRESERVE_INSTALL_STATE=1
        PROVIDER_LAN_CLEANUP_DEFERRED=1
        return 0
      fi
    fi
    if [[ $timer_loaded -eq 1 ]] \
      && ! systemd_unit_fragment_matches fxroute-mdns-guard.timer "$timer_path" "${sudo_cmd[@]}"; then
      warn "Refusing to stop the loaded FXRoute mDNS guard timer from a foreign unit path"
      PRESERVE_INSTALL_STATE=1
      PROVIDER_LAN_CLEANUP_DEFERRED=1
      return 0
    fi
    if [[ $service_loaded -eq 1 ]] \
      && ! systemd_unit_fragment_matches fxroute-mdns-guard.service "$service_path" "${sudo_cmd[@]}"; then
      warn "Refusing to stop the loaded FXRoute mDNS guard service from a foreign unit path"
      PRESERVE_INSTALL_STATE=1
      PROVIDER_LAN_CLEANUP_DEFERRED=1
      return 0
    fi
    if [[ -e "$timer_path" || -L "$timer_path" || $timer_loaded -eq 1 ]] \
      && ! "${sudo_cmd[@]}" systemctl disable --now fxroute-mdns-guard.timer >/dev/null 2>&1; then
        warn "Could not stop the FXRoute mDNS guard timer"
        if [[ $timer_was_enabled -eq 1 ]]; then
          "${sudo_cmd[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
        fi
        if [[ $timer_was_active -eq 1 ]]; then
          "${sudo_cmd[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
        fi
        PRESERVE_INSTALL_STATE=1
        PROVIDER_LAN_CLEANUP_DEFERRED=1
        return 0
    fi
    if [[ -e "$service_path" || -L "$service_path" || $service_loaded -eq 1 ]] \
      && ! "${sudo_cmd[@]}" systemctl disable --now fxroute-mdns-guard.service >/dev/null 2>&1; then
        warn "Could not stop the FXRoute mDNS guard service"
        if [[ $timer_was_enabled -eq 1 ]]; then
          "${sudo_cmd[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
        fi
        if [[ $timer_was_active -eq 1 ]]; then
          "${sudo_cmd[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
        fi
        PRESERVE_INSTALL_STATE=1
        PROVIDER_LAN_CLEANUP_DEFERRED=1
      return 0
    fi
  fi
  if [[ $marker_present -eq 0 ]]; then
    if ! command -v nft >/dev/null 2>&1; then
      warn "Cannot verify the FXRoute mDNS guard table because nft is unavailable"
      PRESERVE_INSTALL_STATE=1
      PROVIDER_LAN_CLEANUP_DEFERRED=1
      return 0
    fi
    if ! remove_mdns_guard_table_direct "${sudo_cmd[@]}"; then
      warn "Could not verify or remove the FXRoute mDNS guard table"
      PRESERVE_INSTALL_STATE=1
      PROVIDER_LAN_CLEANUP_DEFERRED=1
      return 0
    fi
    return 0
  fi
  if ! remove_mdns_guard_table_direct "${sudo_cmd[@]}"; then
    warn "Could not remove the FXRoute mDNS guard rules"
    if [[ $timer_was_enabled -eq 1 ]]; then
      "${sudo_cmd[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    if [[ $timer_was_active -eq 1 ]]; then
      "${sudo_cmd[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    PRESERVE_INSTALL_STATE=1
    PROVIDER_LAN_CLEANUP_DEFERRED=1
    return 0
  fi
  if ! "${sudo_cmd[@]}" rm -f "$service_path" "$timer_path" "$script_path"; then
    warn "Could not remove the FXRoute mDNS guard files"
    if [[ $timer_was_enabled -eq 1 ]]; then
      "${sudo_cmd[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    if [[ $timer_was_active -eq 1 ]]; then
      "${sudo_cmd[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    PRESERVE_INSTALL_STATE=1
    PROVIDER_LAN_CLEANUP_DEFERRED=1
    return 0
  fi
  "${sudo_cmd[@]}" systemctl daemon-reload >/dev/null 2>&1 || true
  log "Removed optional FXRoute mDNS guard"
}

firewall_rule_port() {
  case "$1" in
    http_80_tcp) printf '80/tcp\n' ;;
    https_443_tcp) printf '443/tcp\n' ;;
    mdns_5353_udp) printf '5353/udp\n' ;;
    fxroute_http_8000_tcp) printf '8000/tcp\n' ;;
    spotifyd_zeroconf_4444_tcp) printf '%s/tcp\n' "$SPOTIFYD_ZEROCONF_PORT" ;;
    *) return 1 ;;
  esac
}

firewall_cleanup_sudo() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    return 0
  fi
  command -v sudo >/dev/null 2>&1
}

legacy_firewall_rule_field() {
  case "$1" in
    http_80_tcp) printf 'http_opened_by_fxroute\n' ;;
    https_443_tcp) printf 'https_opened_by_fxroute\n' ;;
    mdns_5353_udp) printf 'mdns_opened_by_fxroute\n' ;;
    *) return 1 ;;
  esac
}

legacy_ufw_rule_field() {
  case "$1" in
    http_80_tcp|fxroute_http_8000_tcp) printf 'http_opened_by_fxroute\n' ;;
    https_443_tcp) printf 'https_opened_by_fxroute\n' ;;
    mdns_5353_udp) printf 'mdns_opened_by_fxroute\n' ;;
    *) return 1 ;;
  esac
}

firewall_legacy_state_present() {
  local schema=""
  local legacy=""

  legacy="$(read_install_state_field "lan_comfort.legacy_firewall_ownership_present" 2>/dev/null || true)"
  [[ "$legacy" == "true" ]] && return 0
  schema="$(read_install_state_field "lan_comfort.firewall_ownership_schema" 2>/dev/null || true)"
  [[ -z "$schema" ]]
}

firewalld_rule_service() {
  case "$1" in
    http_80_tcp) printf 'http\n' ;;
    https_443_tcp) printf 'https\n' ;;
    mdns_5353_udp) printf 'mdns\n' ;;
    *) return 1 ;;
  esac
}

firewalld_rule_rich_rule() {
  local rule_id="$1"
  local port=""
  local port_number=""
  local protocol=""

  port="$(firewall_rule_port "$rule_id")" || return 1
  port_number="${port%/*}"
  protocol="${port#*/}"
  printf 'rule priority="100" port port="%s" protocol="%s" accept\n' "$port_number" "$protocol"
}

firewalld_query_status() {
  local status=0

  if "$@" >/dev/null 2>&1; then
    return 0
  else
    status=$?
  fi
  [[ $status -eq 1 ]] && return 1
  return 2
}

firewalld_legacy_rich_rule() {
  local rule_id="$1"
  local port=""
  local port_number=""
  local protocol=""

  port="$(firewall_rule_port "$rule_id")" || return 1
  port_number="${port%/*}"
  protocol="${port#*/}"
  printf 'rule priority="-100" port port="%s" protocol="%s" accept\n' "$port_number" "$protocol"
}

legacy_firewalld_baseline_field() {
  case "$1" in
    http_80_tcp) printf 'http_was_allowed_before\n' ;;
    https_443_tcp) printf 'https_was_allowed_before\n' ;;
    mdns_5353_udp) printf 'mdns_was_allowed_before\n' ;;
    *) return 1 ;;
  esac
}

firewalld_legacy_rule_is_owned() {
  # Older state files shared one aggregate flag between UFW and firewalld.
  # That is not enough evidence to remove a firewalld service safely.
  return 1
}

firewalld_rule_is_owned() {
  local rule_id="$1"
  local nested=""

  nested="$(read_install_state_field "lan_comfort.firewalld_owned_rules.${rule_id}" 2>/dev/null || true)"
  case "$nested" in
    true) return 0 ;;
    false)
      firewall_legacy_state_present || return 1
      firewalld_legacy_rule_is_owned "$rule_id"
      ;;
    "") firewalld_legacy_rule_is_owned "$rule_id" ;;
    *) return 1 ;;
  esac
}

legacy_firewalld_cleanup_uncertain() {
  local rule_id="$1"
  local nested=""
  local legacy_field=""

  firewall_legacy_state_present || return 1
  nested="$(read_install_state_field "lan_comfort.firewalld_owned_rules.${rule_id}" 2>/dev/null || true)"
  [[ "$nested" != "true" ]] || return 1
  legacy_field="$(legacy_firewall_rule_field "$rule_id" 2>/dev/null || true)"
  [[ -n "$legacy_field" ]] || return 1
  [[ "$(read_install_state_field "lan_comfort.${legacy_field}" 2>/dev/null || true)" == "true" ]]
}

ufw_rule_line() {
  local rule_id="$1"
  local port=""
  local added=""
  local sudo_cmd=()

  port="$(firewall_rule_port "$rule_id")" || return 1
  firewall_cleanup_sudo || return 2
  [[ ${EUID:-$(id -u)} -eq 0 ]] || sudo_cmd=(sudo)
  command -v ufw >/dev/null 2>&1 || return 2
  if ! added="$("${sudo_cmd[@]}" ufw show added 2>/dev/null)"; then
    return 2
  fi
  grep -Ei "^ufw allow ${port}([[:space:]]|$)" <<<"$added" | head -n1 || true
}

ufw_rule_is_owned() {
  local rule_id="$1"
  local nested=""
  local legacy_field=""
  local ownership_state=""

  nested="$(read_install_state_field "lan_comfort.ufw_owned_rules.${rule_id}" 2>/dev/null || true)"
  case "$nested" in
    true) return 0 ;;
    false) firewall_legacy_state_present || return 1 ;;
    "") ;;
    *) return 1 ;;
  esac
  legacy_field="$(legacy_ufw_rule_field "$rule_id" 2>/dev/null || true)"
  [[ -n "$legacy_field" ]] || return 1
  [[ "$(read_install_state_field "lan_comfort.${legacy_field}" 2>/dev/null || true)" == "true" ]] || return 1
  ownership_state="$(ufw_rule_ownership_state "$rule_id")" || return 2
  case "$ownership_state" in
    owned) return 0 ;;
    absent) return 1 ;;
    foreign) return 3 ;;
    *) return 2 ;;
  esac
}

ufw_rule_ownership_state() {
  local rule_id="$1"
  local rule_line=""
  local comment_field=""
  local port=""

  port="$(firewall_rule_port "$rule_id")" || return 1
  rule_line="$(ufw_rule_line "$rule_id")" || return $?
  [[ -n "$rule_line" ]] || {
    printf 'absent\n'
    return 0
  }
  comment_field="${rule_line#* comment }"
  [[ "$comment_field" != "$rule_line" ]] || {
    printf 'foreign\n'
    return 0
  }
  case "$rule_id" in
    http_80_tcp)
      case "$comment_field" in
        "'FXRoute port-80 LAN access'"|"\"FXRoute port-80 LAN access\""|"FXRoute port-80 LAN access") printf 'owned\n' ;;
        *) printf 'foreign\n' ;;
      esac
      ;;
    https_443_tcp)
      case "$comment_field" in
        "'FXRoute port-443 LAN access'"|"\"FXRoute port-443 LAN access\""|"FXRoute port-443 LAN access") printf 'owned\n' ;;
        *) printf 'foreign\n' ;;
      esac
      ;;
    mdns_5353_udp)
      case "$comment_field" in
        "'spotifyd Zeroconf mDNS discovery'"|"\"spotifyd Zeroconf mDNS discovery\""|"spotifyd Zeroconf mDNS discovery"|"'Qobuz Connect discovery'"|"\"Qobuz Connect discovery\""|"Qobuz Connect discovery"|"'.local LAN access'"|"\".local LAN access\""|".local LAN access") printf 'owned\n' ;;
        *) printf 'foreign\n' ;;
      esac
      ;;
    fxroute_http_8000_tcp)
      case "$comment_field" in
        "'FXRoute HTTP LAN access'"|"\"FXRoute HTTP LAN access\""|"FXRoute HTTP LAN access") printf 'owned\n' ;;
        *) printf 'foreign\n' ;;
      esac
      ;;
    spotifyd_zeroconf_4444_tcp)
      case "$comment_field" in
        "'spotifyd Zeroconf TCP authentication'"|"\"spotifyd Zeroconf TCP authentication\""|"spotifyd Zeroconf TCP authentication") printf 'owned\n' ;;
        *) printf 'foreign\n' ;;
      esac
      ;;
    *) return 1 ;;
  esac
}

remove_owned_firewalld_rule() {
  local rule_id="$1"
  local purpose="$2"
  local port=""
  local sudo_cmd=()
  local firewall_cmd=""
  local firewall_offline_cmd=""
  local legacy_owned=0
  local service=""
  local runtime_service_present=0
  local permanent_service_present=0
  local rich_rule=""
  local legacy_rich_rule=""
  local runtime_rich_present=0
  local permanent_rich_present=0
  local runtime_legacy_rich_present=0
  local permanent_legacy_rich_present=0
  local offline_rich_present=0
  local query_status=0
  local legacy_port_owned=0
  local runtime_port_present=0
  local permanent_port_present=0
  local offline_port_present=0
  local rule_format=""
  local ownership_schema=""
  local nested_ownership=""

  if ! firewalld_rule_is_owned "$rule_id"; then
    if legacy_firewalld_cleanup_uncertain "$rule_id"; then
      warn "Cannot safely migrate legacy firewalld ownership for '$rule_id'; keeping install state"
      PRESERVE_INSTALL_STATE=1
    fi
    return 0
  fi
  port="$(firewall_rule_port "$rule_id")" || return 0
  rich_rule="$(firewalld_rule_rich_rule "$rule_id")" || return 0
  legacy_rich_rule="$(firewalld_legacy_rich_rule "$rule_id")" || return 0
  rule_format="$(read_install_state_field "lan_comfort.firewalld_rule_format" 2>/dev/null || true)"
  ownership_schema="$(read_install_state_field "lan_comfort.firewall_ownership_schema" 2>/dev/null || true)"
  nested_ownership="$(read_install_state_field "lan_comfort.firewalld_owned_rules.${rule_id}" 2>/dev/null || true)"
  if [[ "$nested_ownership" == "true" \
    && ( "$rule_format" == "legacy-port" || ( -z "$rule_format" && "$ownership_schema" == "2" ) ) ]]; then
    legacy_port_owned=1
  fi
  if firewall_legacy_state_present \
    && [[ "$nested_ownership" == "false" || -z "$nested_ownership" ]] \
    && firewalld_legacy_rule_is_owned "$rule_id"; then
    legacy_owned=1
    service="$(firewalld_rule_service "$rule_id")"
  fi

  if [[ $legacy_port_owned -eq 1 ]]; then
    if ! confirm "FXRoute opened firewalld port '$port' for $purpose. Remove that firewall opening?"; then
      warn "Keeping firewalld port '$port'"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  elif [[ $legacy_owned -eq 1 ]]; then
    if ! confirm "FXRoute opened firewalld service '$service' for $purpose. Remove that firewall opening?"; then
      warn "Keeping firewalld service '$service'"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  elif ! confirm "FXRoute opened firewalld port '$port' for $purpose. Remove that firewall opening?"; then
    warn "Keeping firewalld port '$port'"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! firewall_cleanup_sudo; then
    warn "Cannot remove firewalld port '$port' because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  [[ ${EUID:-$(id -u)} -eq 0 ]] || sudo_cmd=(sudo)

  firewall_cmd="$(firewall_cmd_path || true)"
  firewall_offline_cmd="$(firewall_offline_cmd_path || true)"

  if [[ $legacy_port_owned -eq 1 ]]; then
    if [[ -n "$firewall_cmd" ]] && "${sudo_cmd[@]}" "$firewall_cmd" --state >/dev/null 2>&1; then
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --query-port="$port"; then
        runtime_port_present=1
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify runtime legacy firewalld port '$port'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-port="$port"; then
        permanent_port_present=1
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify permanent legacy firewalld port '$port'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if [[ $runtime_port_present -eq 1 ]] \
        && ! "${sudo_cmd[@]}" "$firewall_cmd" --remove-port="$port" >/dev/null 2>&1; then
        warn "Failed to remove runtime legacy firewalld port '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
      if [[ $permanent_port_present -eq 1 ]] \
        && ! "${sudo_cmd[@]}" "$firewall_cmd" --permanent --remove-port="$port" >/dev/null 2>&1; then
        warn "Failed to remove permanent legacy firewalld port '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
      if [[ $runtime_port_present -eq 1 || $permanent_port_present -eq 1 ]]; then
        if ! "${sudo_cmd[@]}" "$firewall_cmd" --reload >/dev/null 2>&1; then
          warn "Removed legacy firewalld port '$port', but reload failed"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
        log "Removed legacy FXRoute-owned firewalld port '$port'"
        return 0
      fi
    fi
    if [[ -n "$firewall_offline_cmd" ]]; then
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-port="$port"; then
        offline_port_present=1
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify offline legacy firewalld port '$port'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if [[ $offline_port_present -eq 1 ]]; then
        if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --remove-port="$port" >/dev/null 2>&1; then
          warn "Failed to remove legacy firewalld port '$port' from offline config"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
        log "Removed legacy FXRoute-owned firewalld port '$port' from offline config"
        return 0
      fi
    fi
  fi

  if [[ $legacy_owned -eq 1 ]]; then
    if [[ -n "$firewall_cmd" ]] && "${sudo_cmd[@]}" "$firewall_cmd" --state >/dev/null 2>&1; then
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --query-service="$service"; then
        runtime_service_present=1
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify runtime firewalld service '$service'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-service="$service"; then
        permanent_service_present=1
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify permanent firewalld service '$service'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if [[ $runtime_service_present -eq 1 ]]; then
        if ! "${sudo_cmd[@]}" "$firewall_cmd" --remove-service="$service" >/dev/null 2>&1; then
          warn "Failed to remove runtime firewalld service '$service'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if [[ $permanent_service_present -eq 1 ]]; then
        if ! "${sudo_cmd[@]}" "$firewall_cmd" --permanent --remove-service="$service" >/dev/null 2>&1; then
          warn "Failed to remove permanent firewalld service '$service'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if [[ $runtime_service_present -eq 1 || $permanent_service_present -eq 1 ]]; then
        if ! "${sudo_cmd[@]}" "$firewall_cmd" --reload >/dev/null 2>&1; then
          warn "Removed firewalld service '$service', but reload failed"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
        log "Removed legacy FXRoute-owned firewalld service '$service'"
      else
        log "Legacy FXRoute-owned firewalld service '$service' is already absent"
      fi
      return 0
    fi
    if [[ -n "$firewall_offline_cmd" ]]; then
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-service="$service"; then
        :
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify legacy offline firewalld service '$service'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
        log "Legacy FXRoute-owned firewalld service '$service' is already absent from offline config"
        return 0
      fi
      if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --remove-service="$service" >/dev/null 2>&1; then
        warn "Failed to remove legacy firewalld service '$service' from offline config"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
      log "Removed legacy FXRoute-owned firewalld service '$service' from offline config"
      return 0
    fi
    warn "Could not find firewalld tooling to remove legacy service '$service'"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ -n "$firewall_cmd" ]] && "${sudo_cmd[@]}" "$firewall_cmd" --state >/dev/null 2>&1; then
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --query-rich-rule="$rich_rule"; then
      runtime_rich_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify runtime firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-rich-rule="$rich_rule"; then
      permanent_rich_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify permanent firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --query-rich-rule="$legacy_rich_rule"; then
      runtime_legacy_rich_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify legacy runtime firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-rich-rule="$legacy_rich_rule"; then
      permanent_legacy_rich_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify legacy permanent firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $runtime_rich_present -eq 0 && $permanent_rich_present -eq 0 \
      && $runtime_legacy_rich_present -eq 0 && $permanent_legacy_rich_present -eq 0 ]]; then
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --query-port="$port"; then
        warn "Refusing to remove firewalld port '$port' because its current rule is not FXRoute-identifiable"
        PRESERVE_INSTALL_STATE=1
        return 0
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify runtime firewalld port '$port'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-port="$port"; then
        warn "Refusing to remove firewalld port '$port' because its current rule is not FXRoute-identifiable"
        PRESERVE_INSTALL_STATE=1
        return 0
      else
        query_status=$?
        if [[ $query_status -ne 1 ]]; then
          warn "Could not verify permanent firewalld port '$port'"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      log "FXRoute-owned firewalld rule for '$port' is already absent"
      return 0
    fi
    if [[ $runtime_rich_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --remove-rich-rule="$rich_rule" >/dev/null 2>&1; then
        warn "Failed to remove runtime firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $permanent_rich_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --permanent --remove-rich-rule="$rich_rule" >/dev/null 2>&1; then
        warn "Failed to remove permanent firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $runtime_legacy_rich_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --remove-rich-rule="$legacy_rich_rule" >/dev/null 2>&1; then
        warn "Failed to remove the legacy runtime firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $permanent_legacy_rich_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --permanent --remove-rich-rule="$legacy_rich_rule" >/dev/null 2>&1; then
        warn "Failed to remove the legacy permanent firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if ! "${sudo_cmd[@]}" "$firewall_cmd" --reload >/dev/null 2>&1; then
      warn "Removed firewalld rule for '$port', but reload failed"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    log "Removed FXRoute-owned firewalld rule for '$port'"
    return 0
  fi

  if [[ -n "$firewall_offline_cmd" ]]; then
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-rich-rule="$rich_rule"; then
      offline_rich_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify offline firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-rich-rule="$legacy_rich_rule"; then
      offline_rich_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify legacy offline firewalld rule for '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $offline_rich_present -eq 1 ]]; then
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-rich-rule="$rich_rule"; then
        if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --remove-rich-rule="$rich_rule" >/dev/null 2>&1; then
          warn "Failed to remove firewalld rule for '$port' from offline config"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-rich-rule="$legacy_rich_rule"; then
        if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --remove-rich-rule="$legacy_rich_rule" >/dev/null 2>&1; then
          warn "Failed to remove legacy firewalld rule for '$port' from offline config"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
      fi
      log "Removed FXRoute-owned firewalld rule for '$port' from offline config"
      return 0
    fi
    if firewalld_query_status "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-port="$port"; then
      warn "Refusing to remove offline firewalld port '$port' because its current rule is not FXRoute-identifiable"
      PRESERVE_INSTALL_STATE=1
      return 0
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Could not verify offline firewalld port '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    log "FXRoute-owned firewalld rule for '$port' is already absent from offline config"
    return 0
  fi

  warn "Could not find firewalld tooling to remove port '$port'"
  PRESERVE_INSTALL_STATE=1
}

remove_owned_ufw_rule() {
  local rule_id="$1"
  local purpose="$2"
  local port=""
  local sudo_cmd=()
  local ownership_state=""
  local rule_line=""
  local comment_field=""
  local rule_comment=""
  local ownership_result=0

  if ufw_rule_is_owned "$rule_id"; then
    :
  else
    ownership_result=$?
    if [[ $ownership_result -eq 2 || $ownership_result -eq 3 ]]; then
      port="$(firewall_rule_port "$rule_id" || true)"
      warn "Could not safely migrate current UFW ownership for '$port'"
      PRESERVE_INSTALL_STATE=1
    fi
    return 0
  fi
  port="$(firewall_rule_port "$rule_id")" || return 0

  if ! ownership_state="$(ufw_rule_ownership_state "$rule_id")"; then
    warn "Could not verify current UFW ownership for '$port'"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  case "$ownership_state" in
    absent)
      log "FXRoute-owned UFW port '$port' is already absent"
      return 0
      ;;
    foreign)
      warn "Refusing to remove UFW port '$port' because its current comment is not FXRoute-owned"
      PRESERVE_INSTALL_STATE=1
      return 0
      ;;
  esac
  rule_line="$(ufw_rule_line "$rule_id")" || {
    warn "Could not read the current UFW rule for '$port'"
    PRESERVE_INSTALL_STATE=1
    return 0
  }
  comment_field="${rule_line#* comment }"
  if [[ "$comment_field" == "$rule_line" ]]; then
    warn "Refusing to remove UFW port '$port' without its exact FXRoute comment"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  case "$comment_field" in
    \'*\') rule_comment="${comment_field:1:${#comment_field}-2}" ;;
    \"*\") rule_comment="${comment_field:1:${#comment_field}-2}" ;;
    *) rule_comment="$comment_field" ;;
  esac

  if ! confirm "FXRoute opened UFW port '$port' for $purpose. Remove that firewall opening?"; then
    warn "Keeping UFW port '$port'"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! firewall_cleanup_sudo; then
    warn "Cannot remove UFW port '$port' because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  [[ ${EUID:-$(id -u)} -eq 0 ]] || sudo_cmd=(sudo)
  command -v ufw >/dev/null 2>&1 || {
    warn "Could not find UFW tooling to remove port '$port'"
    PRESERVE_INSTALL_STATE=1
    return 0
  }

  if "${sudo_cmd[@]}" ufw --force delete allow "$port" comment "$rule_comment" >/dev/null 2>&1; then
    log "Removed FXRoute-owned UFW port '$port'"
    return 0
  fi
  if ! ownership_state="$(ufw_rule_ownership_state "$rule_id")"; then
    warn "Could not verify the persistent UFW rule '$port' after deletion failed"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ "$ownership_state" == "absent" ]]; then
    log "FXRoute-owned UFW port '$port' is already absent"
    return 0
  fi
  warn "Failed to remove UFW port '$port'"
  PRESERVE_INSTALL_STATE=1
}

remove_system_power_polkit_rule() {
  local rule_installed
  local rule_pre_existed
  local rule_path
  local backup_path
  local backup_sha256
  local rule_sha256
  local expected_rule_sha256
  local sudo_cmd=()
  local rule_name="50-fxroute-power.rules"
  local rule_default="/etc/polkit-1/rules.d/$rule_name"
  local backup_default="$FXROUTE_BACKUP_DIR/${rule_name}.pre-fxroute"

  rule_installed="$(read_install_state_field "lan_comfort.power_polkit_installed" 2>/dev/null || true)"
  rule_pre_existed="$(read_install_state_field "lan_comfort.power_polkit_rule_pre_existed" 2>/dev/null || true)"
  rule_path="$(read_install_state_field "lan_comfort.power_polkit_rule_path" 2>/dev/null || true)"
  if [[ -n "$rule_path" && "$rule_path" != "$rule_default" ]]; then
    warn "Refusing to use an unexpected FXRoute polkit rule path: $rule_path"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  rule_path="$rule_default"
  [[ "$rule_installed" == "true" ]] || {
    return 0
  }

  if [[ "$(id -u)" -eq 0 ]]; then
    sudo_cmd=()
  elif command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot remove FXRoute polkit power rule because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ ! -e "$rule_path" ]]; then
    log "FXRoute polkit power rule already absent at $rule_path"
    return 0
  fi

  rule_sha256="$("${sudo_cmd[@]}" sha256sum "$rule_path" | awk '{print $1}')"
  expected_rule_sha256="$(read_install_state_field "lan_comfort.power_polkit_rule_sha256" 2>/dev/null || true)"
  if [[ -z "$expected_rule_sha256" || "$rule_sha256" != "$expected_rule_sha256" ]]; then
    warn "Refusing to remove the changed or unverified FXRoute polkit power rule"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ -e "$backup_default" ]]; then
    backup_sha256="$(read_install_state_field "lan_comfort.power_polkit_backup_sha256" 2>/dev/null || true)"
    if [[ ! -f "$backup_default" || -L "$backup_default" || -z "$backup_sha256" \
      || "$("${sudo_cmd[@]}" stat -c '%u' "$backup_default" 2>/dev/null || true)" != "0" \
      || "$("${sudo_cmd[@]}" sha256sum "$backup_default" | awk '{print $1}')" != "$backup_sha256" ]]; then
      warn "Refusing to restore an unverified FXRoute polkit backup"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    if confirm "FXRoute added /etc/polkit-1/rules.d/$rule_name. Restore the previous rule from backup?"; then
      if "${sudo_cmd[@]}" install -m 644 "$backup_default" "$rule_path"; then
        if "${sudo_cmd[@]}" rm -f "$backup_default"; then
          log "Restored prior polkit rule from backup"
        else
          warn "Restored prior polkit rule, but could not remove its backup"
          PRESERVE_INSTALL_STATE=1
        fi
        return 0
      fi
      warn "Failed to restore the pre-FXRoute polkit rule from backup"
    fi
  elif [[ "$rule_pre_existed" == "true" ]]; then
    warn "The previous polkit rule backup is unavailable; refusing to remove the current rule"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if confirm "Remove FXRoute polkit power rule at $rule_path? This is what enables the suspend/shutdown menu."; then
    if "${sudo_cmd[@]}" rm -f "$rule_path"; then
      log "Removed FXRoute polkit power rule"
    else
      warn "Could not remove FXRoute polkit power rule"
      PRESERVE_INSTALL_STATE=1
    fi
  else
    warn "Keeping FXRoute polkit power rule"
    PRESERVE_INSTALL_STATE=1
  fi
}

remove_optional_caddy_proxy() {
  local service_name="fxroute-caddy.service"
  local service_path="/etc/systemd/system/$service_name"
  local config_path="/etc/fxroute/Caddyfile"
  local cert_path="/etc/fxroute/certs/fxroute-local-root.crt"
  local caddy_data_dir="/var/lib/fxroute-caddy"
  local proxy_enabled=""
  local service_sha256=""
  local config_sha256=""
  local cert_sha256=""
  local recorded_cert_path=""
  local data_created=""
  local actual_sha256=""
  local active_state=""
  local unit_file_state=""
  local fragment_path=""
  local path=""
  local loaded_status=0
  local sudo_cmd=()

  proxy_enabled="$(read_install_state_field "lan_comfort.caddy_proxy_enabled" 2>/dev/null || true)"
  [[ "$proxy_enabled" == "true" ]] || return 0
  service_sha256="$(read_install_state_field "lan_comfort.caddy_service_sha256" 2>/dev/null || true)"
  config_sha256="$(read_install_state_field "lan_comfort.caddy_config_sha256" 2>/dev/null || true)"
  cert_sha256="$(read_install_state_field "lan_comfort.caddy_cert_sha256" 2>/dev/null || true)"
  recorded_cert_path="$(read_install_state_field "lan_comfort.caddy_cert_path" 2>/dev/null || true)"
  data_created="$(read_install_state_field "lan_comfort.caddy_data_dir_created_by_fxroute" 2>/dev/null || true)"
  if [[ -n "$recorded_cert_path" && "$recorded_cert_path" != "$cert_path" ]]; then
    warn "Refusing to remove Caddy artifacts because the recorded certificate path is unexpected"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ "$(id -u)" -eq 0 ]]; then
    sudo_cmd=()
  elif command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot remove optional FXRoute Caddy proxy because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  for path in "$service_path" "$config_path" "$cert_path" "$caddy_data_dir"; do
    if path_has_symlink_component "$path"; then
      warn "Refusing to remove Caddy artifacts through a symlinked parent: $path"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  done

  for path in "$service_path" "$config_path"; do
    if [[ -e "$path" || -L "$path" ]]; then
      [[ -f "$path" && ! -L "$path" && "$("${sudo_cmd[@]}" stat -c '%u' "$path" 2>/dev/null || true)" == "0" ]] \
        || {
          warn "Refusing to remove unexpected Caddy artifact $path"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
      if [[ "$path" == "$service_path" ]]; then
        [[ -n "$service_sha256" ]] || {
          warn "Refusing to remove Caddy service without a recorded checksum"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
        actual_sha256="$("${sudo_cmd[@]}" sha256sum "$path" | awk '{print $1}')"
        [[ "$actual_sha256" == "$service_sha256" ]] || {
          warn "Refusing to remove a changed Caddy service"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
        grep -Fqx 'Description=FXRoute Caddy reverse proxy' "$path" || {
          warn "Refusing to remove a non-FXRoute Caddy service"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
        grep -Fq -- "--config $config_path" "$path" || {
          warn "Refusing to remove a Caddy service with an unexpected config path"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
      else
        [[ -n "$config_sha256" ]] || {
          warn "Refusing to remove Caddy configuration without a recorded checksum"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
        actual_sha256="$("${sudo_cmd[@]}" sha256sum "$path" | awk '{print $1}')"
        [[ "$actual_sha256" == "$config_sha256" ]] || {
          warn "Refusing to remove a changed Caddy configuration"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
        grep -Fq 'reverse_proxy 127.0.0.1:' "$path" || {
          warn "Refusing to remove a non-FXRoute Caddy configuration"
          PRESERVE_INSTALL_STATE=1
          return 0
        }
      fi
    fi
  done
  if [[ -e "$cert_path" || -L "$cert_path" ]]; then
    [[ -f "$cert_path" && ! -L "$cert_path" && "$recorded_cert_path" == "$cert_path" \
      && -n "$cert_sha256" \
      && "$("${sudo_cmd[@]}" stat -c '%u' "$cert_path" 2>/dev/null || true)" == "0" ]] || {
      warn "Refusing to remove an unexpected Caddy certificate"
      PRESERVE_INSTALL_STATE=1
      return 0
    }
    actual_sha256="$("${sudo_cmd[@]}" sha256sum "$cert_path" | awk '{print $1}')"
    [[ "$actual_sha256" == "$cert_sha256" ]] || {
      warn "Refusing to remove a changed Caddy certificate"
      PRESERVE_INSTALL_STATE=1
      return 0
    }
  fi
  if [[ -e "$caddy_data_dir" || -L "$caddy_data_dir" ]]; then
    if [[ "$data_created" == "true" ]]; then
      [[ -d "$caddy_data_dir" && ! -L "$caddy_data_dir" \
        && "$("${sudo_cmd[@]}" stat -c '%u' "$caddy_data_dir" 2>/dev/null || true)" == "0" ]] || {
        warn "Refusing to remove an unexpected Caddy data directory"
        PRESERVE_INSTALL_STATE=1
        return 0
      }
    else
      log "Keeping pre-existing Caddy data directory"
    fi
  fi

  if systemd_unit_is_loaded "$service_name" "${sudo_cmd[@]}"; then
    if ! fragment_path="$("${sudo_cmd[@]}" systemctl show "$service_name" -p FragmentPath --value 2>/dev/null)" \
      || [[ "$fragment_path" != "$service_path" ]]; then
      warn "Refusing to stop Caddy because its loaded unit path is not FXRoute-owned"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    if ! active_state="$("${sudo_cmd[@]}" systemctl show "$service_name" -p ActiveState --value 2>/dev/null)"; then
      warn "Could not verify the FXRoute Caddy service state"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    case "$active_state" in
      active|activating|deactivating|reloading)
        if ! "${sudo_cmd[@]}" systemctl stop "$service_name" >/dev/null 2>&1; then
          warn "Could not stop the FXRoute Caddy service"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
        active_state="$("${sudo_cmd[@]}" systemctl show "$service_name" -p ActiveState --value 2>/dev/null || true)"
        ;;
    esac
    case "$active_state" in
      inactive|failed|dead) ;;
      *)
        warn "Refusing to remove an active FXRoute Caddy service"
        PRESERVE_INSTALL_STATE=1
        return 0
        ;;
    esac
    if ! "${sudo_cmd[@]}" systemctl disable "$service_name" >/dev/null 2>&1; then
      unit_file_state="$("${sudo_cmd[@]}" systemctl show "$service_name" -p UnitFileState --value 2>/dev/null || true)"
      case "$unit_file_state" in
        enabled|enabled-runtime|linked|linked-runtime|alias|"")
          warn "Could not disable the FXRoute Caddy service"
          PRESERVE_INSTALL_STATE=1
          return 0
          ;;
      esac
    fi
  else
    loaded_status=$?
    if [[ $loaded_status -ne 1 ]]; then
      warn "Could not verify whether the FXRoute Caddy service is loaded"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi

  if ! "${sudo_cmd[@]}" rm -f "$service_path" "$config_path" "$cert_path"; then
    warn "Could not remove the FXRoute Caddy service or configuration"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ "$data_created" == "true" && -e "$caddy_data_dir" ]]; then
    if ! "${sudo_cmd[@]}" rm -rf "$caddy_data_dir"; then
      warn "Could not remove the FXRoute Caddy data"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  fi
  "${sudo_cmd[@]}" rmdir /etc/fxroute/certs >/dev/null 2>&1 || true
  "${sudo_cmd[@]}" rmdir /etc/fxroute >/dev/null 2>&1 || true
  "${sudo_cmd[@]}" systemctl daemon-reload >/dev/null 2>&1 || true
  log "Removed optional FXRoute Caddy reverse proxy"
}

restore_default_caddy_service_if_needed() {
  local was_active_before
  local disabled_by_fxroute
  local sudo_cmd=()

  was_active_before="$(read_install_state_field "lan_comfort.caddy_service_was_active_before" 2>/dev/null || true)"
  disabled_by_fxroute="$(read_install_state_field "lan_comfort.default_caddy_disabled_by_fxroute" 2>/dev/null || true)"
  [[ "$was_active_before" == "true" && "$disabled_by_fxroute" == "true" ]] || return 0

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot restore the previous system caddy.service because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if ! "${sudo_cmd[@]}" systemctl enable --now caddy.service >/dev/null 2>&1; then
    warn "Could not restore the previously active system caddy.service"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  log "Restored previously active system caddy.service"
}

restore_hostname_if_requested() {
  local changed_by_fxroute
  local hostname_before
  local hostname_after
  local current_hostname
  local sudo_cmd=()

  changed_by_fxroute="$(read_install_state_field "lan_comfort.hostname_changed_by_fxroute" 2>/dev/null || true)"
  hostname_before="$(read_install_state_field "lan_comfort.hostname_before" 2>/dev/null || true)"
  hostname_after="$(read_install_state_field "lan_comfort.hostname_after" 2>/dev/null || true)"
  current_hostname="$(hostname 2>/dev/null || true)"

  [[ "$changed_by_fxroute" == "true" ]] || return 0
  [[ -n "$hostname_before" && -n "$hostname_after" ]] || return 0
  [[ "$current_hostname" == "$hostname_after" ]] || return 0
  [[ "$hostname_before" != "$hostname_after" ]] || return 0

  if ! confirm "FXRoute changed the hostname from $hostname_before to $hostname_after for .local access. Restore the previous hostname?"; then
    warn "Keeping hostname $current_hostname"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot restore the previous hostname because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if ! "${sudo_cmd[@]}" hostnamectl set-hostname "$hostname_before"; then
    warn "Failed to restore hostname to $hostname_before"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if systemctl is-active avahi-daemon >/dev/null 2>&1; then
    "${sudo_cmd[@]}" systemctl restart avahi-daemon >/dev/null 2>&1 || true
  fi

  log "Restored hostname to $hostname_before"
}

restore_avahi_config_if_requested() {
  local configured_by_fxroute
  local config_path="/etc/avahi/avahi-daemon.conf"
  local backup_path="${config_path}.pre-fxroute-ipv4-mdns"
  local sudo_cmd=()

  configured_by_fxroute="$(read_install_state_field "lan_comfort.avahi_ipv4_mdns_configured_by_fxroute" 2>/dev/null || true)"
  [[ "$configured_by_fxroute" == "true" ]] || return 0
  [[ -e "$backup_path" || -L "$backup_path" ]] || return 0

  if ! confirm "FXRoute adjusted Avahi for IPv4-only .local access. Restore the previous Avahi config?"; then
    warn "Keeping current Avahi config"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot restore the previous Avahi config because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if ! "${sudo_cmd[@]}" install -m 644 "$backup_path" "$config_path"; then
    warn "Failed to restore ${config_path}"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! "${sudo_cmd[@]}" rm -f "$backup_path"; then
    warn "Restored Avahi config, but could not remove its backup"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if systemctl is-active avahi-daemon >/dev/null 2>&1; then
    "${sudo_cmd[@]}" systemctl restart avahi-daemon >/dev/null 2>&1 || warn "Restored Avahi config, but failed to restart avahi-daemon"
  fi

  log "Restored previous Avahi config"
}

remove_avahi_if_requested() {
  local installed_by_fxroute
  local enabled_by_fxroute
  local was_active_before
  local was_enabled_before
  local sudo_cmd=()
  local avahi_pkg=""

  installed_by_fxroute="$(read_install_state_field "lan_comfort.avahi_installed_by_fxroute" 2>/dev/null || true)"
  enabled_by_fxroute="$(read_install_state_field "lan_comfort.avahi_enabled_by_fxroute" 2>/dev/null || true)"
  was_active_before="$(read_install_state_field "lan_comfort.avahi_was_active_before" 2>/dev/null || true)"
  was_enabled_before="$(read_install_state_field "lan_comfort.avahi_was_enabled_before" 2>/dev/null || true)"

  [[ "$installed_by_fxroute" == "true" || "$enabled_by_fxroute" == "true" ]] || return 0

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot adjust Avahi cleanup because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    avahi_pkg="avahi-daemon"
  elif command -v dnf >/dev/null 2>&1 || command -v zypper >/dev/null 2>&1 || command -v pacman >/dev/null 2>&1; then
    avahi_pkg="avahi"
  fi

  if [[ "$installed_by_fxroute" == "true" && -n "$avahi_pkg" ]]; then
    if ! confirm "FXRoute installed Avahi for .local access. Remove Avahi too?"; then
      warn "Keeping Avahi installed"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi

    "${sudo_cmd[@]}" systemctl disable --now avahi-daemon >/dev/null 2>&1 || true
    case "$avahi_pkg" in
      avahi-daemon)
        if ! "${sudo_cmd[@]}" apt-get remove -y avahi-daemon >/dev/null 2>&1; then
          warn "Failed to remove Avahi with apt"
          PRESERVE_INSTALL_STATE=1
          return 0
        fi
        ;;
      avahi)
        if command -v dnf >/dev/null 2>&1; then
          if ! "${sudo_cmd[@]}" dnf remove -y avahi >/dev/null 2>&1; then
            warn "Failed to remove Avahi with dnf"
            PRESERVE_INSTALL_STATE=1
            return 0
          fi
        elif command -v zypper >/dev/null 2>&1; then
          if ! "${sudo_cmd[@]}" zypper --non-interactive remove avahi >/dev/null 2>&1; then
            warn "Failed to remove Avahi with zypper"
            PRESERVE_INSTALL_STATE=1
            return 0
          fi
        elif command -v pacman >/dev/null 2>&1; then
          if "${sudo_cmd[@]}" pacman -Q avahi >/dev/null 2>&1; then
            if ! "${sudo_cmd[@]}" pacman -R --noconfirm avahi >/dev/null 2>&1; then
              warn "Failed to remove Avahi with pacman"
              PRESERVE_INSTALL_STATE=1
              return 0
            fi
          else
            log "FXRoute-installed Avahi is already absent"
          fi
        fi
        ;;
    esac
    log "Removed Avahi installed for FXRoute LAN comfort"
    return 0
  fi

  if [[ "$installed_by_fxroute" == "true" ]]; then
    warn "Cannot remove FXRoute-installed Avahi because its package manager is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ "$enabled_by_fxroute" == "true" && "$was_active_before" != "true" && "$was_enabled_before" != "true" ]]; then
    if ! confirm "FXRoute enabled Avahi for .local access. Disable Avahi again?"; then
      warn "Keeping Avahi enabled"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi

    if ! "${sudo_cmd[@]}" systemctl disable --now avahi-daemon >/dev/null 2>&1; then
      warn "Failed to disable Avahi"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    log "Disabled Avahi enabled for FXRoute LAN comfort"
  fi
}

read_install_state_field() {
  local field="$1"
  local state_file="$INSTALL_STATE_FILE"
  if [[ ${ROOT_STATE_REQUIRED:-0} -eq 1 && ${ROOT_STATE_TRUSTED:-0} -ne 1 ]]; then
    return 1
  fi
  if [[ ${ROOT_STATE_TRUSTED:-0} -eq 1 ]]; then
    state_file="$ROOT_INSTALL_STATE_FILE"
  fi
  [[ -f "$state_file" ]] || return 1
  python3 - <<'PY' "$state_file" "$field"
import json, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text())
value = payload
for part in sys.argv[2].split('.'):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(1)
    value = value[part]
if isinstance(value, bool):
    print('true' if value else 'false')
elif value is None:
    print('')
else:
    print(value)
PY
}

validate_install_root_identity() {
  local recorded_root=""

  recorded_root="$(read_install_state_field install_root 2>/dev/null || true)"
  [[ -n "$recorded_root" ]] || return 0
  if [[ -L "$recorded_root" ]]; then
    echo "The FXRoute install state records a symlink target; refusing to continue" >&2
    exit 1
  fi
  if [[ "$(canonical_path "$recorded_root")" != "$INSTALL_ROOT" ]]; then
    echo "The install state belongs to $recorded_root, not $INSTALL_ROOT; refusing to continue" >&2
    exit 1
  fi
}

remove_project_dir_if_requested() {
  [[ $REMOVE_PROJECT_DIR -eq 1 ]] || return 0
  if [[ $PRESERVE_INSTALL_STATE -eq 1 ]]; then
    warn "Skipping project directory removal while install state is retained for deferred cleanup"
    return 0
  fi
  if ! validate_install_root_for_removal; then
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! confirm "Remove project directory $INSTALL_ROOT?"; then
    warn "Skipping project directory removal"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != root \
    && ( "$INSTALL_ROOT" == "$HOME" || "$INSTALL_ROOT" == "$HOME"/* ) ]]; then
    if ! run_as_target_user rm -rf -- "$INSTALL_ROOT"; then
      warn "Failed to remove project directory $INSTALL_ROOT"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
  elif ! rm -rf -- "$INSTALL_ROOT"; then
    warn "Failed to remove project directory $INSTALL_ROOT"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  log "Removed $INSTALL_ROOT"
}

remove_install_records() {
  local sudo_cmd=()
  if [[ $PRESERVE_INSTALL_STATE -eq 0 ]]; then
    remove_file_if_exists "$INSTALL_STATE_FILE"
    remove_file_if_exists "$INSTALL_CONFIG_FILE"
  else
    log "Keeping FXRoute install state for a later provider cleanup retry"
  fi
  if [[ $PRESERVE_INSTALL_STATE -eq 0 ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
      sudo_cmd=()
    elif command -v sudo >/dev/null 2>&1; then
      sudo_cmd=(sudo)
    fi
    if [[ ${#sudo_cmd[@]} -gt 0 || "$(id -u)" -eq 0 ]]; then
      if ! "${sudo_cmd[@]}" rm -f "$ROOT_INSTALL_STATE_FILE"; then
        warn "Could not remove the root-owned FXRoute install state"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
      "${sudo_cmd[@]}" rmdir "$FXROUTE_BACKUP_DIR" >/dev/null 2>&1 || true
      "${sudo_cmd[@]}" rmdir "$(dirname "$FXROUTE_BACKUP_DIR")" >/dev/null 2>&1 || true
      "${sudo_cmd[@]}" rmdir "$(dirname "$ROOT_INSTALL_STATE_FILE")" >/dev/null 2>&1 || true
      "${sudo_cmd[@]}" rmdir "$(dirname "$(dirname "$ROOT_INSTALL_STATE_FILE")")" >/dev/null 2>&1 || true
      "${sudo_cmd[@]}" rmdir /var/lib/fxroute >/dev/null 2>&1 || true
    fi
  fi
}

main() {
  local firewall_rule=""
  if [[ $ROOT_STATE_REQUIRED -eq 1 && $ROOT_STATE_TRUSTED -ne 1 ]]; then
    warn "No trusted root-owned FXRoute install state is available; privileged ownership cleanup and project removal are disabled"
    PRESERVE_INSTALL_STATE=1
  fi
  validate_install_root_identity
  log "Stopping and removing FXRoute user service"
  remove_service

  log "Removing owned optional streaming components"
  remove_owned_streaming_components

  log "Removing FXRoute DSP ingress sink"
  remove_dsp_ingress_sink

  log "Removing optional Spotify cache cleanup helper"
  remove_spotify_cleanup_helper

  log "Removing optional system update helper"
  remove_optional_system_update_helper

  log "Removing helper scripts"
  remove_helpers

  log "Removing network library mount helper"
  remove_network_library_helper

  log "Removing Spotify autostart"
  remove_autostart

  if [[ $PROVIDER_LAN_CLEANUP_DEFERRED -eq 1 ]]; then
    log "Keeping provider-dependent mDNS/Avahi/firewall integration while provider cleanup is deferred"
  else
    log "Removing optional FXRoute mDNS guard"
    remove_optional_mdns_guard
  fi

  log "Removing optional FXRoute system power polkit rule"
  remove_system_power_polkit_rule

  log "Removing optional FXRoute Caddy reverse proxy"
  remove_optional_caddy_proxy

  log "Restoring previously active system caddy.service if needed"
  restore_default_caddy_service_if_needed

  user_systemctl daemon-reload >/dev/null 2>&1 || true
  user_systemctl reset-failed >/dev/null 2>&1 || true

  if [[ $CORE_SERVICE_CLEANUP_DEFERRED -eq 0 ]]; then
    log "Removing FXRoute user-session persistence"
    remove_user_linger_if_owned
    log "Removing FXRoute-owned audio group membership"
    remove_audio_group_if_owned
  else
    log "Keeping FXRoute user-session persistence while service cleanup is deferred"
  fi

  if [[ $PROVIDER_LAN_CLEANUP_DEFERRED -eq 0 ]]; then
    restore_hostname_if_requested
    restore_avahi_config_if_requested
    remove_avahi_if_requested
  fi
  if [[ $CORE_SERVICE_CLEANUP_DEFERRED -eq 0 ]]; then
    for firewall_rule in \
      http_80_tcp \
      https_443_tcp \
      fxroute_http_8000_tcp; do
      remove_owned_firewalld_rule "$firewall_rule" "FXRoute LAN access"
      remove_owned_ufw_rule "$firewall_rule" "FXRoute LAN access"
    done
  else
    log "Keeping core LAN firewall openings while FXRoute service cleanup is deferred"
  fi
  if [[ $PROVIDER_LAN_CLEANUP_DEFERRED -eq 0 ]]; then
    for firewall_rule in \
      mdns_5353_udp \
      spotifyd_zeroconf_4444_tcp; do
      remove_owned_firewalld_rule "$firewall_rule" "FXRoute provider access"
      remove_owned_ufw_rule "$firewall_rule" "FXRoute provider access"
    done
  fi
  remove_project_dir_if_requested
  remove_install_records

  log "Uninstall complete"
}

main "$@"
