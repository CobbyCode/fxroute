#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

APP_NAME="FXRoute"
SERVICE_NAME="fxroute"
PROJECT_DIRNAME="fxroute"
DEFAULT_INSTALL_ROOT="$HOME/$PROJECT_DIRNAME"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"
INSTALL_ROOT="$DEFAULT_INSTALL_ROOT"
INSTALL_ROOT_EXPLICIT=0
LOCAL_PROJECT_MODE=0
ASSUME_YES=0
PROVIDER_LIST=""
PROVIDER_SELECTION_EXPLICIT=0
PROVIDERS_ONLY_MODE=0
AUTO_LAN_NAME=0
AUTO_CADDY=0
AUTO_DEVICE_NAME=""
TARGET_USER_ARG=""
SELECT_SPOTIFY_DESKTOP=0
SELECT_SPOTIFYD=0
SELECT_QOBUZ=0
SELECT_TIDAL=0
HOST_ARCH="$(uname -m)"

SPOTIFYD_VERSION="0.4.2"
SPOTIFYD_ARM64_ARTIFACT_VERSION="1"
SPOTIFYD_ARM64_ARCHIVE="spotifyd-${SPOTIFYD_VERSION}-linux-aarch64-fxroute-${SPOTIFYD_ARM64_ARTIFACT_VERSION}.tar.gz"
SPOTIFYD_ARM64_SHA256="6afe154e14801df34eac5d161b5891155fa7d57f42affb38a4d96c999e5ebbe6"
SPOTIFYD_ARM64_DOWNLOAD_URL="https://github.com/CobbyCode/fxroute/releases/download/spotifyd-arm64-v${SPOTIFYD_ARM64_ARTIFACT_VERSION}/${SPOTIFYD_ARM64_ARCHIVE}"
QBZD_VERSION="2.0.2"
SPOTIFYD_ZEROCONF_PORT="4444"
CIFS_HELPER_SHA256="8c848fc5cff8d1e320c54e99caad66ac53cbf0ba81329e1c651102115ba7aa7d"
CIFS_HELPER_LEGACY_SHA256="a878afbf1927bdd14ed3049df39a41929a54cd18a1ab89a377ba1ed4c4b453d8"
PROVIDER_HELPER_SHA256="990a00b8b05750e4db6fb2a2dbb487d29742a50645edbb61487c6891f5e5a906"
SYSTEM_UPDATE_HELPER_SHA256="b9e67b2f396e814930d1ebfeba8f6d9d483b601a3fbd27cc7dd8c32b7d3506eb"
POWER_POLKIT_TEMPLATE_SHA256="67497733c846fda6eddd11f80eb626bdffa0d76530ad7bb5fe5a65ebf1669806"
QOBUZ_VOLUME_MODE_KEY="qconnect.volume_mode"
QOBUZ_REQUIRED_VOLUME_MODE="locked"
TIDAL_REQUIREMENTS_FILE="requirements-tidal.txt"
SPOTIFY_APT_SOURCE_FILE="/etc/apt/sources.list.d/spotify.list"
SPOTIFY_APT_KEY_FILE="/usr/share/keyrings/spotify-archive-keyring.gpg"
SPOTIFY_APT_KEY_URL="https://download.spotify.com/debian/pubkey_5384CE82BA52C83A.asc"
SPOTIFY_APT_KEY_FINGERPRINT="E1096BCBFF6D418796DE78515384CE82BA52C83A"
# Machine-readable provider contract markers, kept in sync with
# installer_contract.py (pinned on both sides by scripts/test_provider_admin.py).
PROVIDER_CONTRACT_PREFIX="PROVIDER_CONTRACT="
PROVIDER_CONTRACT_HELPER_MISSING="${PROVIDER_CONTRACT_PREFIX}helper-missing"

# Read one KEY=VALUE value from installer_contract.py without importing it
# (the shell contract is parsed, never executed). Prints nothing on failure.
provider_contract_literal() {
  local key="$1"
  local file="${INSTALL_ROOT:-$SCRIPT_DIR}/installer_contract.py"
  [[ -f "$file" ]] || return 1
  sed -n "s/^${key} = \"\([^\" ]*\)\"$/\1/p" "$file" | head -n 1
}

# Pinned self-check: the shell markers must match the Python module so the
# 503 contract cannot drift between the two sides.
verify_provider_contract_literals() {
  local prefix helper_missing
  prefix="$(provider_contract_literal MARKER_PREFIX)" || return 1
  helper_missing="$(provider_contract_literal MARKER_HELPER_MISSING)" || return 1
  [[ "$prefix" == "$PROVIDER_CONTRACT_PREFIX" ]] || return 1
  [[ "$helper_missing" == "$PROVIDER_CONTRACT_HELPER_MISSING" ]] || return 1
}

SPOTIFY_DESKTOP_PRESENT_BEFORE=0
SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_INSTALL_METHOD=""
SPOTIFY_DESKTOP_INSTALLED_VERSION=""
SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_REPO_SHA256=""
SPOTIFY_DESKTOP_KEY_FINGERPRINT=""
SPOTIFYD_PRESENT_BEFORE=0
SPOTIFYD_INSTALLED_BY_FXROUTE=0
SPOTIFYD_SOURCE_BUILT=0
SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE=0
SPOTIFYD_CONFIG_INSTALLED_BY_FXROUTE=0
SPOTIFYD_DEVICE_NAME_CHANGED=0
SPOTIFYD_DSP_SINK_ROUTED=0
SPOTIFYD_CONNECT_NAME=""
SPOTIFYD_BINARY_PATH=""
SPOTIFYD_BINARY_SHA256=""
SPOTIFYD_BINARY_IDENTITY_CHANGED=0
SPOTIFYD_SERVICE_PATH="$HOME/.config/systemd/user/spotifyd.service"
SPOTIFYD_SERVICE_SHA256=""
SPOTIFYD_SERVICE_IDENTITY_CHANGED=0
SPOTIFYD_SERVICE_SETUP_FAILED=0
SPOTIFYD_CONFIG_PATH="$HOME/.config/spotifyd/spotifyd.conf"
QBZD_PRESENT_BEFORE=0
QBZD_INSTALLED_BY_FXROUTE=0
QBZD_SERVICE_INSTALLED_BY_FXROUTE=0
QBZD_BINARY_PATH=""
QBZD_BINARY_SHA256=""
QBZD_SERVICE_PATH="$HOME/.config/systemd/user/qbzd.service"
QBZD_SERVICE_SHA256=""
QBZD_SERVICE_IDENTITY_CHANGED=0
QBZD_SERVICE_SETUP_FAILED=0
QBZD_VOLUME_MODE_BEFORE=""
QBZD_VOLUME_MODE_AFTER=""
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_QCONNECT_STARTUP_MODE_BEFORE=""
QBZD_QCONNECT_STARTUP_MODE_AFTER=""
QBZD_QCONNECT_CHANGED_BY_FXROUTE=0
QBZD_AUDIO_BACKEND_BEFORE=""
QBZD_AUDIO_DEVICE_BEFORE=""
QBZD_AUDIO_SKIP_SINK_SWITCH_BEFORE=""
QBZD_AUDIO_CHANGED_BY_FXROUTE=0
QBZD_BINARY_IDENTITY_CHANGED=0
TIDAL_PRESENT_BEFORE=0
TIDAL_INSTALLED_BY_FXROUTE=0
TIDAL_INSTALLED_VERSION=""
TIDAL_DEPENDENCY_SELECTED=0
QOBUZ_PROVIDER_STATUS="not selected"
SPOTIFYD_PROVIDER_STATUS="not selected"
SPOTIFY_DESKTOP_PROVIDER_STATUS="not selected"
TIDAL_PROVIDER_STATUS="not selected"
FXROUTE_TARGET_USER=""
FXROUTE_TARGET_UID=""
FXROUTE_TARGET_HOME=""
FXROUTE_TARGET_GROUP=""
FXROUTE_RUNTIME_DIR=""
USER_LINGER_WAS_ENABLED=0
USER_LINGER_ENABLED_BY_FXROUTE=0
AUDIO_GROUP_ADDED_BY_FXROUTE=0
JOURNAL_GROUP_ADDED_BY_FXROUTE=0

VALIDATION_RESULTS=()
WARNINGS=()
PACKAGE_MANAGER=""
PACKAGE_INSTALL_CMD=()
PKG_REFRESH_DONE=0
SUDO_CMD=()
PROVIDER_HELPER_PATH="/usr/local/sbin/fxroute-provider-privileged"
PROVIDER_SUDOERS_FILE="/etc/sudoers.d/fxroute-provider-privileged"
INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
INSTALL_CONFIG_FILE="$HOME/.config/fxroute/install-config.env"
ROOT_INSTALL_STATE_FILE="/var/lib/fxroute/state/$(id -u)/install-state.json"
FXROUTE_BACKUP_DIR="/var/lib/fxroute/backups/$(id -u)"
FXROUTE_ACTIVE_TEMP_DIR=""
FXROUTE_ACTIVE_STAGED_BINARY=""
MDNS_HOSTNAME=""
LAN_HOSTNAME_BEFORE=""
LAN_HOSTNAME_AFTER=""
LAN_HOSTNAME_CHANGED_BY_FXROUTE=0
AVAHI_WAS_PRESENT_BEFORE=0
AVAHI_WAS_ACTIVE_BEFORE=0
AVAHI_WAS_ENABLED_BEFORE=0
AVAHI_INSTALLED_BY_FXROUTE=0
AVAHI_ENABLED_BY_FXROUTE=0
AVAHI_IPV4_MDNS_CONFIGURED_BY_FXROUTE=0
AVAHI_IPV4_MDNS_CONFIG_BACKED_UP=0
CADDY_WAS_PRESENT_BEFORE=0
CADDY_SERVICE_WAS_ACTIVE_BEFORE=0
CADDY_INSTALLED_BY_FXROUTE=0
DEFAULT_CADDY_DISABLED_BY_FXROUTE=0
CADDY_PROXY_ENABLED=0
CADDY_CERT_PATH=""
CADDY_SERVICE_SHA256=""
CADDY_CONFIG_SHA256=""
CADDY_CERT_SHA256=""
CADDY_DATA_DIR_CREATED_BY_FXROUTE=0
CIFS_HELPER_INSTALLED_BY_FXROUTE=0
CIFS_SUDOERS_RULE_INSTALLED_BY_FXROUTE=0
CIFS_SUDOERS_SHA256=""
SYSTEM_UPDATE_OWNED_BY_FXROUTE=0
SYSTEM_UPDATE_SERVICE_SHA256=""
SYSTEM_UPDATE_TIMER_SHA256=""
MDNS_GUARD_ENABLED=0
MDNS_GUARD_OWNED_BY_FXROUTE=0
MDNS_GUARD_LEGACY_OWNERSHIP=0
MDNS_GUARD_SCRIPT_SHA256=""
MDNS_GUARD_SERVICE_SHA256=""
MDNS_GUARD_TIMER_SHA256=""
MDNS_GUARD_TARGET_UID=""
FIREWALLD_WAS_ACTIVE_BEFORE=0
HTTP_WAS_ALLOWED_BEFORE=0
HTTPS_WAS_ALLOWED_BEFORE=0
MDNS_WAS_ALLOWED_BEFORE=0
HTTP_OPENED_BY_FXROUTE=0
HTTPS_OPENED_BY_FXROUTE=0
POWER_POLKIT_INSTALLED=0
POWER_POLKIT_RULE_PATH=""
POWER_POLKIT_RULE_PRE_EXISTED=0
POWER_POLKIT_BACKUP_SHA256=""
POWER_POLKIT_RULE_SHA256=""
MDNS_OPENED_BY_FXROUTE=0
INSTALL_STATE_LOADED=0
STATE_CHECKPOINT_ENABLED=0
PROVIDER_PRIVILEGE_INSTALLED_BY_FXROUTE=0
PROVIDER_PRIVILEGE_SUDOERS_SHA256=""

# Firewall ownership is tracked per concrete backend/rule pair. The legacy
# aggregate flags below remain in the state file for older installations.
FIREWALLD_HTTP_80_TCP_OPENED_BY_FXROUTE=0
FIREWALLD_HTTPS_443_TCP_OPENED_BY_FXROUTE=0
FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE=0
FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=0
FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=0
UFW_HTTP_80_TCP_OPENED_BY_FXROUTE=0
UFW_HTTPS_443_TCP_OPENED_BY_FXROUTE=0
UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE=0
UFW_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=0
UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=0
FIREWALL_LEGACY_STATE_PRESENT=0
FIREWALLD_RULE_FORMAT="rich-priority"
FIREWALLD_LEGACY_PORT_MIGRATION=0
SPOTIFY_DESKTOP_AVAILABLE=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  --target <dir>        Install or refresh into a dedicated fxroute directory (default: $DEFAULT_INSTALL_ROOT)
  --user <name>         Run FXRoute and its audio graph as this Unix user
  --local-project       Install in-place from the current project directory
  --source <dir>        Use a different local project source directory
  --providers-only      Install only the selected providers into an existing FXRoute checkout (Settings -> Providers backend; no service/env changes)
  --with-lan-name       Image mode: enable the .local LAN name non-interactively
  --with-caddy          Image mode: enable the HTTPS reverse proxy non-interactively
  --device-name <name>  Image mode: set the .local device name (with --with-lan-name)
  --providers <list>    Select comma-separated providers: spotify-desktop, spotifyd, qobuz, tidal, none
  --spotify-desktop     Select Spotify Desktop installation
  --spotifyd            Select spotifyd installation
  --qobuz               Select Qobuz/qbzd installation
  --tidal               Select the TIDAL Python dependency
  -y, --yes             Assume yes for package and legacy firewall prompts
  -h, --help            Show this help

Pass 1 is a pragmatic local installer. It installs dependencies, prepares the venv,
creates a user service, and can either sync the current project into ~/fxroute
or run directly from the local project tree.
EOF
}

log() { printf '[fxroute] %s\n' "$*"; }
warn() { printf '[fxroute][warn] %s\n' "$*" >&2; WARNINGS+=("$*"); }
die() { printf '[fxroute][error] %s\n' "$*" >&2; exit 1; }
pass() { printf '[pass] %s\n' "$*"; VALIDATION_RESULTS+=("PASS: $*"); }
fail() { printf '[fail] %s\n' "$*"; VALIDATION_RESULTS+=("FAIL: $*"); }

cleanup_active_temp_dir() {
  local active_dir="${FXROUTE_ACTIVE_TEMP_DIR:-}"
  local staged_binary="${FXROUTE_ACTIVE_STAGED_BINARY:-}"
  FXROUTE_ACTIVE_TEMP_DIR=""
  FXROUTE_ACTIVE_STAGED_BINARY=""
  [[ -z "$staged_binary" ]] || rm -f -- "$staged_binary" || true
  [[ -n "$active_dir" ]] || return 0
  rm -rf -- "$active_dir" || true
}

determine_fxroute_target_identity() {
  local candidate=""
  local candidates=()
  local login_user=""

  if [[ -n "$TARGET_USER_ARG" ]]; then
    candidate="$TARGET_USER_ARG"
  elif [[ "$(id -u)" -eq 0 && -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    candidate="$SUDO_USER"
  elif [[ "$(id -u)" -eq 0 ]]; then
    login_user="$(logname 2>/dev/null || true)"
    if [[ -n "$login_user" && "$login_user" != "root" ]] \
      && id -u "$login_user" >/dev/null 2>&1; then
      candidate="$login_user"
    else
      while IFS= read -r login_user; do
        [[ -n "$login_user" ]] && candidates+=("$login_user")
      done < <(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ /^\// && $7 !~ /(nologin|false)$/ {print $1}')
      if [[ ${#candidates[@]} -eq 1 ]]; then
        candidate="${candidates[0]}"
        log "Root installer invocation detected; using the only eligible audio user: $candidate"
      elif [[ ${#candidates[@]} -gt 1 ]]; then
        die "Multiple eligible users found (${candidates[*]}). Re-run with --user <name> so FXRoute and PipeWire share one user session."
      else
        candidate="root"
      fi
    fi
  else
    candidate="$(id -un)"
  fi

  [[ -n "$candidate" ]] || die "Could not determine the FXRoute target user"
  if [[ "$(id -u)" -ne 0 && "$candidate" != "$(id -un)" ]]; then
    die "Only root may select a different FXRoute target user"
  fi
  FXROUTE_TARGET_USER="$candidate"
  FXROUTE_TARGET_UID="$(id -u "$FXROUTE_TARGET_USER" 2>/dev/null || true)"
  [[ -n "$FXROUTE_TARGET_UID" ]] || die "Could not determine the FXRoute target user UID for $FXROUTE_TARGET_USER"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || die "--target requires a directory"
      INSTALL_ROOT="$2"
      INSTALL_ROOT_EXPLICIT=1
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || die "--user requires a Unix username"
      TARGET_USER_ARG="$2"
      shift 2
      ;;
    --source)
      [[ $# -ge 2 ]] || die "--source requires a directory"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --providers)
      [[ $# -ge 2 ]] || die "--providers requires a comma-separated list"
      PROVIDER_LIST="$2"
      PROVIDER_SELECTION_EXPLICIT=1
      shift 2
      ;;
    --spotify-desktop)
      SELECT_SPOTIFY_DESKTOP=1
      PROVIDER_SELECTION_EXPLICIT=1
      shift
      ;;
    --spotifyd)
      SELECT_SPOTIFYD=1
      PROVIDER_SELECTION_EXPLICIT=1
      shift
      ;;
    --qobuz)
      SELECT_QOBUZ=1
      PROVIDER_SELECTION_EXPLICIT=1
      shift
      ;;
    --tidal)
      SELECT_TIDAL=1
      PROVIDER_SELECTION_EXPLICIT=1
      shift
      ;;
    --local-project)
      LOCAL_PROJECT_MODE=1
      shift
      ;;
    --providers-only)
      # Settings -> Providers backend: install only the selected providers in
      # an existing FXRoute checkout (no venv/service/env/validator churn).
      PROVIDERS_ONLY_MODE=1
      shift
      ;;
    --with-lan-name)
      # Image first-boot: enable Avahi + .local name non-interactively.
      AUTO_LAN_NAME=1
      shift
      ;;
    --with-caddy)
      # Image first-boot: enable the HTTPS reverse proxy non-interactively.
      AUTO_CADDY=1
      shift
      ;;
    --device-name)
      [[ $# -ge 2 ]] || die "--device-name requires a hostname"
      AUTO_DEVICE_NAME="$2"
      shift 2
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
      die "Unknown argument: $1"
      ;;
  esac
done

determine_fxroute_target_identity

configure_target_user_environment() {
  local passwd_entry=""
  local passwd_name=""
  local passwd_password=""
  local passwd_uid=""
  local passwd_gid=""
  local passwd_gecos=""
  local passwd_shell=""

  passwd_entry="$(getent passwd "$FXROUTE_TARGET_USER" 2>/dev/null || true)"
  [[ -n "$passwd_entry" ]] || die "Could not read passwd entry for FXRoute target user $FXROUTE_TARGET_USER"
  IFS=: read -r passwd_name passwd_password passwd_uid passwd_gid passwd_gecos FXROUTE_TARGET_HOME passwd_shell <<<"$passwd_entry"
  [[ "$FXROUTE_TARGET_HOME" == /* && -d "$FXROUTE_TARGET_HOME" ]] \
    || die "FXRoute target user $FXROUTE_TARGET_USER has no usable home directory"

  FXROUTE_TARGET_GROUP="$(id -gn "$FXROUTE_TARGET_USER" 2>/dev/null || true)"
  [[ -n "$FXROUTE_TARGET_GROUP" ]] || die "Could not determine the FXRoute target user group for $FXROUTE_TARGET_USER"
  FXROUTE_RUNTIME_DIR="/run/user/$FXROUTE_TARGET_UID"

  HOME="$FXROUTE_TARGET_HOME"
  export HOME
  DEFAULT_INSTALL_ROOT="$HOME/$PROJECT_DIRNAME"
  if [[ $INSTALL_ROOT_EXPLICIT -eq 0 ]]; then
    INSTALL_ROOT="$DEFAULT_INSTALL_ROOT"
  fi
  SPOTIFYD_SERVICE_PATH="$HOME/.config/systemd/user/spotifyd.service"
  SPOTIFYD_CONFIG_PATH="$HOME/.config/spotifyd/spotifyd.conf"
  QBZD_SERVICE_PATH="$HOME/.config/systemd/user/qbzd.service"
  INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
  INSTALL_CONFIG_FILE="$HOME/.config/fxroute/install-config.env"
  ROOT_INSTALL_STATE_FILE="/var/lib/fxroute/state/$FXROUTE_TARGET_UID/install-state.json"
  FXROUTE_BACKUP_DIR="/var/lib/fxroute/backups/$FXROUTE_TARGET_UID"
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
      HOME="$FXROUTE_TARGET_HOME" \
      LC_ALL=C \
      XDG_CONFIG_HOME="$FXROUTE_TARGET_HOME/.config" \
      XDG_DATA_HOME="$FXROUTE_TARGET_HOME/.local/share" \
      XDG_CACHE_HOME="$FXROUTE_TARGET_HOME/.cache" \
      XDG_RUNTIME_DIR="$FXROUTE_RUNTIME_DIR" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$FXROUTE_RUNTIME_DIR/bus" \
      PIPEWIRE_REMOTE=pipewire-0 \
      PULSE_SERVER="$FXROUTE_RUNTIME_DIR/pulse/native" \
      "$@"
  else
    HOME="$FXROUTE_TARGET_HOME" \
      LC_ALL=C \
      XDG_CONFIG_HOME="$FXROUTE_TARGET_HOME/.config" \
      XDG_DATA_HOME="$FXROUTE_TARGET_HOME/.local/share" \
      XDG_CACHE_HOME="$FXROUTE_TARGET_HOME/.cache" \
      XDG_RUNTIME_DIR="$FXROUTE_RUNTIME_DIR" \
      DBUS_SESSION_BUS_ADDRESS="unix:path=$FXROUTE_RUNTIME_DIR/bus" \
      PIPEWIRE_REMOTE=pipewire-0 \
      PULSE_SERVER="$FXROUTE_RUNTIME_DIR/pulse/native" \
      "$@"
  fi
}

ensure_target_user_ownership() {
  local path=""
  local parent_path=""
  local configured_music_root=""
  local configured_downloads_subdir=""
  local parent_paths=(
    "$HOME/.config"
    "$HOME/.config/fxroute"
    "$HOME/.config/systemd"
    "$HOME/.config/systemd/user"
    "$HOME/.config/pipewire"
    "$HOME/.config/pipewire/pipewire-pulse.conf.d"
    "$HOME/.config/pipewire/pipewire.conf.d"
    "$HOME/.config/spotifyd"
    "$HOME/.config/qbzd"
    "$HOME/.config/autostart"
    "$HOME/.local"
    "$HOME/.local/bin"
    "$HOME/.lv2"
  )
  local paths=(
    "$HOME/.config/fxroute"
    "$HOME/.config/fxroute/install-state.json"
    "$HOME/.config/fxroute/install-config.env"
    "$HOME/.config/systemd/user/fxroute.service"
    "$HOME/.config/systemd/user/spotifyd.service"
    "$HOME/.config/systemd/user/qbzd.service"
    "$HOME/.config/systemd/user/fxroute-spotify-cache-cleanup.service"
    "$HOME/.config/systemd/user/fxroute-spotify-cache-cleanup.timer"
    "$HOME/.config/pipewire/pipewire-pulse.conf.d"
    "$HOME/.config/pipewire/pipewire-pulse.conf.d/50-fxroute-dsp-sink.conf"
    "$HOME/.config/pipewire/pipewire.conf.d"
    "$HOME/.config/pipewire/pipewire.conf.d/90-fxroute-clock-rate.conf"
    "$HOME/.config/spotifyd"
    "$HOME/.config/spotifyd/spotifyd.conf"
    "$HOME/.config/qbzd"
    "$HOME/.config/autostart/fxroute-spotify.desktop"
    "$HOME/.local/bin/fxroute-status"
    "$HOME/.local/bin/fxroute-logs"
    "$HOME/.local/bin/fxroute-restart"
    "$HOME/.local/bin/fxroute-update"
    "$HOME/.local/bin/fxroute-update-ytdlp"
    "$HOME/.local/bin/spotifyd"
    "$HOME/.local/bin/qbzd"
    "$HOME/.lv2/calf.lv2"
  )
  if [[ -f "$INSTALL_ROOT/.env" ]]; then
    configured_music_root="$(read_env_value MUSIC_ROOT "$INSTALL_ROOT/.env" 2>/dev/null || true)"
    configured_downloads_subdir="$(read_env_value DOWNLOADS_SUBDIR "$INSTALL_ROOT/.env" 2>/dev/null || true)"
    [[ -n "$configured_music_root" ]] || configured_music_root="$HOME/Music"
    [[ -n "$configured_downloads_subdir" ]] || configured_downloads_subdir="incoming"
    configured_music_root="$(expand_config_path "$configured_music_root" "$INSTALL_ROOT")" \
      || die "MUSIC_ROOT in $INSTALL_ROOT/.env is not a valid path"
    configured_downloads_subdir="$(normalize_downloads_subdir "$configured_downloads_subdir")" \
      || die "DOWNLOADS_SUBDIR in $INSTALL_ROOT/.env must be a relative path without '.' or '..' components"
    validate_download_path \
      "$(expand_config_path "$configured_music_root/$configured_downloads_subdir" "$INSTALL_ROOT")" \
      "$configured_music_root"
  fi

  paths+=("$INSTALL_ROOT")
  [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]] || return 0
  for parent_path in "${parent_paths[@]}"; do
    [[ -d "$parent_path" ]] || continue
    [[ ! -L "$parent_path" ]] || die "Refusing to use a symlinked target-user directory: $parent_path"
    if path_has_symlink_component "$parent_path"; then
      die "Refusing to use a symlinked parent in the target-user directory: $parent_path"
    fi
    [[ "$(stat -c '%u' "$parent_path" 2>/dev/null || true)" == "$FXROUTE_TARGET_UID" ]] \
      || die "Target-user directory is not owned by $FXROUTE_TARGET_USER: $parent_path"
  done
  for path in "${paths[@]}"; do
    [[ -e "$path" || -L "$path" ]] || continue
    [[ $LOCAL_PROJECT_MODE -eq 1 && "$path" == "$INSTALL_ROOT" ]] && continue
    [[ ! -L "$path" ]] || die "Refusing to use a symlinked target-user path: $path"
    if path_has_symlink_component "$path"; then
      die "Refusing to use a symlinked parent in the target-user path: $path"
    fi
    [[ "$(stat -c '%u' "$path" 2>/dev/null || true)" == "$FXROUTE_TARGET_UID" ]] \
      || die "Target-user path is not owned by $FXROUTE_TARGET_USER: $path"
  done
}

prepare_target_user_directory() {
  local path="$1"

  [[ ! -L "$path" ]] || die "Refusing to use a symlink as a target-user writable directory: $path"
  if path_has_symlink_component "$path"; then
    die "Refusing to use a symlinked parent as a target-user writable directory: $path"
  fi
  run_as_target_user mkdir -p "$path"
}

ensure_target_fxroute_service_is_owned() {
  local service_path="$HOME/.config/systemd/user/$SERVICE_NAME.service"

  [[ -e "$service_path" || -L "$service_path" ]] || return 0
  if [[ ! -f "$service_path" || -L "$service_path" ]] \
    || ! grep -Fxq 'Description=FXRoute' "$service_path" \
    || ! grep -Fxq "WorkingDirectory=$INSTALL_ROOT" "$service_path" \
    || ! grep -Fxq "ExecStart=$INSTALL_ROOT/.venv/bin/python3 $INSTALL_ROOT/main.py" "$service_path"; then
    die "Refusing to overwrite an existing non-FXRoute-owned user service at $service_path"
  fi
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

reject_managed_user_symlinks() {
  local path=""
  local managed_paths=(
    "$HOME/.config"
    "$HOME/.config/fxroute"
    "$HOME/.config/fxroute/install-state.json"
    "$HOME/.config/fxroute/install-config.env"
    "$HOME/.config/systemd"
    "$HOME/.config/systemd/user"
    "$HOME/.config/systemd/user/$SERVICE_NAME.service"
    "$HOME/.config/systemd/user/spotifyd.service"
    "$HOME/.config/systemd/user/qbzd.service"
    "$HOME/.config/systemd/user/fxroute-spotify-cache-cleanup.service"
    "$HOME/.config/systemd/user/fxroute-spotify-cache-cleanup.timer"
    "$HOME/.config/pipewire"
    "$HOME/.config/pipewire/pipewire-pulse.conf.d"
    "$HOME/.config/pipewire/pipewire-pulse.conf.d/50-fxroute-dsp-sink.conf"
    "$HOME/.config/pipewire/pipewire.conf.d"
    "$HOME/.config/pipewire/pipewire.conf.d/90-fxroute-clock-rate.conf"
    "$HOME/.config/spotifyd"
    "$HOME/.config/spotifyd/spotifyd.conf"
    "$HOME/.config/qbzd"
    "$HOME/.config/autostart"
    "$HOME/.config/autostart/fxroute-spotify.desktop"
    "$HOME/.local"
    "$HOME/.local/bin"
    "$HOME/.local/bin/fxroute-status"
    "$HOME/.local/bin/fxroute-logs"
    "$HOME/.local/bin/fxroute-restart"
    "$HOME/.local/bin/fxroute-update"
    "$HOME/.local/bin/fxroute-update-ytdlp"
    "$HOME/.local/bin/spotifyd"
    "$HOME/.local/bin/qbzd"
    "$HOME/.lv2"
    "$HOME/.lv2/calf.lv2"
    "$INSTALL_ROOT/.env"
    "$INSTALL_ROOT/.venv"
    "$INSTALL_ROOT/native_dsp"
    "$INSTALL_ROOT/native_dsp/build"
    "$INSTALL_ROOT/scripts"
    "$INSTALL_ROOT/scripts/update_fxroute.sh"
    "$INSTALL_ROOT/scripts/system-package-update.sh"
    "$INSTALL_ROOT/scripts/fxroute-provider-privileged"
    "$INSTALL_ROOT/scripts/spotify-cache-cleanup.sh"
    "$INSTALL_ROOT/scripts/spotify-autostart.sh"
    "$INSTALL_ROOT/assets"
    "$INSTALL_ROOT/assets/polkit/50-fxroute-power.rules"
  )

  for path in "${managed_paths[@]}"; do
    [[ ! -L "$path" ]] || die "Refusing to follow a symlink in the FXRoute target-user configuration path: $path"
    if path_has_symlink_component "$path"; then
      die "Refusing to follow a symlinked parent in the FXRoute managed path: $path"
    fi
  done
}

ensure_no_foreign_fxroute_services() {
  local account=""
  local password=""
  local uid=""
  local gid=""
  local gecos=""
  local home=""
  local shell=""
  local unit_path=""
  local wants_path=""
  local conflicts=()
  local foreign_dsp_users=""
  local configured_dsp_binary=""
  local system_unit_path=""

  [[ "$(id -u)" -eq 0 ]] || return 0

  while IFS=: read -r account password uid gid gecos home shell; do
    [[ -n "$account" && "$account" != "$FXROUTE_TARGET_USER" && "$home" == /* ]] || continue
    unit_path="$home/.config/systemd/user/$SERVICE_NAME.service"
    [[ -f "$unit_path" ]] || continue
    grep -Fxq 'Description=FXRoute' "$unit_path" || continue
    grep -Eq '^ExecStart=.*main\.py([[:space:]]|$)' "$unit_path" || continue
    wants_path="$home/.config/systemd/user/default.target.wants/$SERVICE_NAME.service"
    if [[ -L "$wants_path" ]] \
      || systemctl --user --machine="${account}@" is-active --quiet "$SERVICE_NAME.service" \
      || systemctl --user --machine="${account}@" is-enabled --quiet "$SERVICE_NAME.service"; then
      conflicts+=("$account")
    fi
  done < <(getent passwd)

  system_unit_path="$("${SUDO_CMD[@]}" systemctl show "$SERVICE_NAME.service" -p FragmentPath --value 2>/dev/null || true)"
  if [[ -n "$system_unit_path" && -f "$system_unit_path" ]] \
    && grep -Fxq 'Description=FXRoute' "$system_unit_path" \
    && grep -Eq '^ExecStart=.*main\.py([[:space:]]|$)' "$system_unit_path" \
    && ( "${SUDO_CMD[@]}" systemctl is-active --quiet "$SERVICE_NAME.service" \
      || "${SUDO_CMD[@]}" systemctl is-enabled --quiet "$SERVICE_NAME.service" ); then
    die "An active or enabled system-wide FXRoute service conflicts with the target-user install. Stop or uninstall it before installing for $FXROUTE_TARGET_USER."
  fi

  configured_dsp_binary="$(configured_dsp_binary)"
  foreign_dsp_users="$(ps -eo user=,args= 2>/dev/null \
    | awk -v target="$FXROUTE_TARGET_USER" -v expected_binary="$configured_dsp_binary" \
      '$2 !~ /(^|\/)awk$/ && ($0 ~ /native_dsp\/build\/fxroute-dsp/ || index($0, expected_binary) > 0) { if ($1 != target) print $1 }' \
    | sort -u)"
  if [[ -n "$foreign_dsp_users" ]]; then
    die "Native DSP processes already run as another user (${foreign_dsp_users//$'\n'/, }). Stop the old FXRoute service/process before installing for $FXROUTE_TARGET_USER."
  fi
  if [[ ${#conflicts[@]} -gt 0 ]]; then
    die "FXRoute user service already exists for another user (${conflicts[*]}). Stop or uninstall it before installing for $FXROUTE_TARGET_USER."
  fi
}

expand_path() {
  python3 - <<'PY' "$1"
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

SOURCE_DIR="$(expand_path "$SOURCE_DIR")"
INSTALL_ROOT="$(expand_path "$INSTALL_ROOT")"

if [[ $PROVIDERS_ONLY_MODE -eq 1 && $INSTALL_ROOT_EXPLICIT -eq 0 && -f "$INSTALL_CONFIG_FILE" ]]; then
  # Provider-only runs service an existing install wherever it lives.
  recorded_providers_root="$(sed -n 's/^FXROUTE_INSTALL_ROOT=//p' "$INSTALL_CONFIG_FILE" | tail -n 1)"
  if [[ -n "$recorded_providers_root" && -d "$(expand_path "$recorded_providers_root")" ]]; then
    INSTALL_ROOT="$(expand_path "$recorded_providers_root")"
  fi
fi

[[ -f "$SOURCE_DIR/main.py" && -f "$SOURCE_DIR/requirements.txt" && -f "$SOURCE_DIR/.env.example" ]] || die "Source directory does not look like the FXRoute project: $SOURCE_DIR"

if [[ $LOCAL_PROJECT_MODE -eq 1 ]]; then
  INSTALL_ROOT="$SOURCE_DIR"
fi

if [[ "$INSTALL_ROOT" == "$SOURCE_DIR" ]]; then
  LOCAL_PROJECT_MODE=1
fi

ensure_install_root_is_safe() {
  local recorded_root=""
  local recorded_state_root=""

  [[ "$INSTALL_ROOT" != "/" && "$INSTALL_ROOT" != "$HOME" ]] \
    || die "Refusing to use / or the home directory as the FXRoute install root"
  [[ ! -L "$INSTALL_ROOT" ]] \
    || die "Refusing to use a symlink as the FXRoute install root"
  if path_has_symlink_component "$INSTALL_ROOT"; then
    die "Refusing to use an FXRoute install root with a symlinked parent: $INSTALL_ROOT"
  fi
  if [[ $LOCAL_PROJECT_MODE -eq 0 || "$INSTALL_ROOT" == "$HOME"/* ]]; then
    [[ "$(basename "$INSTALL_ROOT")" == "$PROJECT_DIRNAME" ]] \
      || die "FXRoute install roots must be dedicated directories named $PROJECT_DIRNAME"
  fi
  [[ "$INSTALL_ROOT" != *[[:space:]]* && "$INSTALL_ROOT" != *%* \
    && "$INSTALL_ROOT" != *\"* && "$INSTALL_ROOT" != *\\* ]] \
    || die "FXRoute install roots contain characters that cannot be represented safely in systemd units or install state"
  reject_managed_user_symlinks
  ensure_target_fxroute_service_is_owned

  if [[ -f "$INSTALL_CONFIG_FILE" ]]; then
    recorded_root="$(sed -n 's/^FXROUTE_INSTALL_ROOT=//p' "$INSTALL_CONFIG_FILE" | tail -n 1)"
    if [[ -n "$recorded_root" && "$(expand_path "$recorded_root")" != "$INSTALL_ROOT" ]]; then
      die "An FXRoute install is already recorded at $(expand_path "$recorded_root"); uninstall it before selecting another target"
    fi
  fi
  if [[ -f "$INSTALL_STATE_FILE" ]]; then
    recorded_state_root="$(python3 - <<'PY' "$INSTALL_STATE_FILE" 2>/dev/null || true
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text()).get("install_root", "")
except (OSError, ValueError):
    value = ""
if value:
    print(value)
PY
    )"
    if [[ -n "$recorded_state_root" && "$(expand_path "$recorded_state_root")" != "$INSTALL_ROOT" ]]; then
      die "The existing FXRoute install state belongs to $(expand_path "$recorded_state_root"); uninstall it before selecting another target"
    fi
  fi

  if [[ -d "$INSTALL_ROOT" && $LOCAL_PROJECT_MODE -eq 0 \
    && -n "$(find "$INSTALL_ROOT" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" \
    && -z "$recorded_root" && -z "$recorded_state_root" ]]; then
    die "Refusing to take ownership of a non-empty unrecorded target $INSTALL_ROOT; use --local-project for an existing checkout or choose an empty dedicated fxroute directory"
  fi
}

ensure_install_root_is_safe

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command missing: $1"
}

provider_privilege_rule_content() {
  # Central allow-list for Settings -> Providers: exactly one sudoers entry,
  # the root-owned helper only. The helper itself allow-lists every action
  # and argument (packages per manager, fixed firewall rule ids, fixed
  # service/group operations); sudoers therefore needs no command suffix
  # beyond the helper path. Never point sudo at the user-writable
  # install.sh: a wildcard on a user-writable script is root execution of
  # attacker-controlled code.
  local install_user="$1"
  printf '%s ALL=(root) NOPASSWD: %s\n' "$install_user" "$PROVIDER_HELPER_PATH"
}

provider_helper_usable() {
  # True when the installed helper is intact (root-owned, exact sha) and
  # the caller may run it without a password.
  [[ -f "$PROVIDER_HELPER_PATH" && ! -L "$PROVIDER_HELPER_PATH" ]] || return 1
  [[ "$(stat -c '%u' "$PROVIDER_HELPER_PATH" 2>/dev/null || true)" == "0" ]] || return 1
  [[ "$(sha256sum "$PROVIDER_HELPER_PATH" 2>/dev/null | awk '{print $1}')" == "$PROVIDER_HELPER_SHA256" ]] || return 1
  local query_rc=0
  sudo -n "$PROVIDER_HELPER_PATH" fw-query mdns_5353_udp >/dev/null 2>&1 || query_rc=$?
  # 0 = open, 1 = closed/no backend (both fine); 2 = rejected/error.
  [[ $query_rc -le 1 ]]
}

install_provider_privileged_helper() {
  # Install the root-owned provider helper plus its sudoers entry. Runs
  # during the full installation (root context via SUDO_CMD) so a fresh
  # image is UI-ready without any manual bootstrap. Refuses to overwrite
  # foreign files; repairs only FXRoute-owned ones.
  local helper_src="$INSTALL_ROOT/scripts/fxroute-provider-privileged"
  local sudoers_path="$PROVIDER_SUDOERS_FILE"
  local tmp_sudoers=""
  local existing_sudoers=""
  local sudoers_rule=""
  local helper_sha256=""
  local existing_helper_sha256=""
  local tmp_helper=""
  local sudoers_rule_present=0
  local install_helper=0
  local install_user="$FXROUTE_TARGET_USER"

  [[ -f "$helper_src" && ! -L "$helper_src" ]] || die "Missing provider privilege helper: $helper_src"
  helper_sha256="$(sha256sum "$helper_src" | awk '{print $1}')"
  [[ "$helper_sha256" == "$PROVIDER_HELPER_SHA256" ]] \
    || die "Refusing to install an unverified provider privilege helper"
  [[ "$install_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "Invalid install user for provider helper"
  if [[ -e "$PROVIDER_HELPER_PATH" || -L "$PROVIDER_HELPER_PATH" ]]; then
    [[ -f "$PROVIDER_HELPER_PATH" && ! -L "$PROVIDER_HELPER_PATH" ]] \
      || die "Refusing to overwrite a non-regular provider privilege helper"
    existing_helper_sha256="$("${SUDO_CMD[@]}" sha256sum "$PROVIDER_HELPER_PATH" | awk '{print $1}')"
    [[ "$existing_helper_sha256" == "$PROVIDER_HELPER_SHA256" ]] \
      || die "Refusing to overwrite a non-FXRoute-owned provider privilege helper"
  else
    install_helper=1
  fi
  if [[ $install_helper -eq 1 ]]; then
    tmp_helper="$(mktemp)"
    if ! "${SUDO_CMD[@]}" install -m 600 "$helper_src" "$tmp_helper" \
      || [[ "$("${SUDO_CMD[@]}" sha256sum "$tmp_helper" | awk '{print $1}')" != "$PROVIDER_HELPER_SHA256" ]]; then
      "${SUDO_CMD[@]}" rm -f "$tmp_helper"
      die "Refusing to install a changed provider privilege helper"
    fi
    if ! "${SUDO_CMD[@]}" install -m 755 "$tmp_helper" "$PROVIDER_HELPER_PATH"; then
      "${SUDO_CMD[@]}" rm -f "$tmp_helper"
      die "Could not install the provider privilege helper"
    fi
    "${SUDO_CMD[@]}" rm -f "$tmp_helper"
    PROVIDER_PRIVILEGE_INSTALLED_BY_FXROUTE=1
  fi
  [[ "$("${SUDO_CMD[@]}" sha256sum "$PROVIDER_HELPER_PATH" | awk '{print $1}')" == "$PROVIDER_HELPER_SHA256" ]] \
    || die "Installed provider privilege helper failed verification"
  tmp_sudoers="$(mktemp)"
  sudoers_rule="$(provider_privilege_rule_content "$install_user")"
  if [[ -L "$sudoers_path" || ( -e "$sudoers_path" && ! -f "$sudoers_path" ) ]]; then
    rm -f "$tmp_sudoers"
    die "Refusing to overwrite a non-regular provider sudoers file"
  fi
  existing_sudoers="$("${SUDO_CMD[@]}" cat "$sudoers_path" 2>/dev/null || true)"
  if [[ -e "$sudoers_path" && "$PROVIDER_PRIVILEGE_INSTALLED_BY_FXROUTE" -eq 1 \
    && -n "$PROVIDER_PRIVILEGE_SUDOERS_SHA256" \
    && "$("${SUDO_CMD[@]}" sha256sum "$sudoers_path" | awk '{print $1}')" != "$PROVIDER_PRIVILEGE_SUDOERS_SHA256" ]]; then
    rm -f "$tmp_sudoers"
    die "Refusing to overwrite a changed provider sudoers file"
  fi
  if grep -Fqx -- "$sudoers_rule" <<<"$existing_sudoers"; then
    sudoers_rule_present=1
  fi
  if [[ -n "$existing_sudoers" ]]; then
    printf '%s\n' "$existing_sudoers" > "$tmp_sudoers"
  fi
  if [[ $sudoers_rule_present -eq 0 ]]; then
    printf '%s\n' "$sudoers_rule" >> "$tmp_sudoers"
    PROVIDER_PRIVILEGE_INSTALLED_BY_FXROUTE=1
  fi
  if command -v visudo >/dev/null 2>&1; then
    "${SUDO_CMD[@]}" visudo -cf "$tmp_sudoers" >/dev/null
  fi
  "${SUDO_CMD[@]}" install -m 440 "$tmp_sudoers" "$sudoers_path"
  rm -f "$tmp_sudoers"
  PROVIDER_PRIVILEGE_SUDOERS_SHA256="$("${SUDO_CMD[@]}" sha256sum "$sudoers_path" | awk '{print $1}')"
  pass "provider privilege helper installed"
}

parse_provider_selection() {
  local selection="${1:-}"
  local token=""
  local tokens=()

  SELECT_SPOTIFY_DESKTOP=0
  SELECT_SPOTIFYD=0
  SELECT_QOBUZ=0
  SELECT_TIDAL=0

  selection="${selection//[[:space:]]/}"
  [[ -z "$selection" || "$selection" == "none" ]] && return 0

  IFS=',' read -r -a tokens <<<"$selection"
  for token in "${tokens[@]}"; do
    case "$token" in
      spotify-desktop) SELECT_SPOTIFY_DESKTOP=1 ;;
      spotifyd) SELECT_SPOTIFYD=1 ;;
      qobuz) SELECT_QOBUZ=1 ;;
      tidal) SELECT_TIDAL=1 ;;
      none) die "Provider 'none' cannot be combined with another provider" ;;
      *) die "Unknown provider '$token'. Expected spotify-desktop, spotifyd, qobuz, tidal, or none." ;;
    esac
  done
}

spotify_desktop_supported() {
  [[ "${HOST_ARCH:-$(uname -m)}" == "x86_64" ]] || return 1
  [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" \
    || "${XDG_SESSION_TYPE:-}" == "x11" \
    || "${XDG_SESSION_TYPE:-}" == "wayland" ]]
}

prompt_optional_provider() {
  local label="$1"
  local reply=""

  printf "Install ${label}? [y/N] "
  read -r reply || return 1
  [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

select_optional_providers() {
  local flag_spotify_desktop="$SELECT_SPOTIFY_DESKTOP"
  local flag_spotifyd="$SELECT_SPOTIFYD"
  local flag_qobuz="$SELECT_QOBUZ"
  local flag_tidal="$SELECT_TIDAL"
  local normalized_provider_list="${PROVIDER_LIST//[[:space:]]/}"

  if [[ -n "$normalized_provider_list" ]]; then
    parse_provider_selection "$normalized_provider_list"
    if [[ "$normalized_provider_list" == "none" && ( "$flag_spotify_desktop" -eq 1 || "$flag_spotifyd" -eq 1 || "$flag_qobuz" -eq 1 || "$flag_tidal" -eq 1 ) ]]; then
      die "Provider 'none' cannot be combined with component flags"
    fi
    (( flag_spotify_desktop == 1 )) && SELECT_SPOTIFY_DESKTOP=1
    (( flag_spotifyd == 1 )) && SELECT_SPOTIFYD=1
    (( flag_qobuz == 1 )) && SELECT_QOBUZ=1
    (( flag_tidal == 1 )) && SELECT_TIDAL=1
    return 0
  fi

  [[ $PROVIDER_SELECTION_EXPLICIT -eq 1 ]] && return 0
  [[ $ASSUME_YES -eq 0 && -t 0 && -t 1 ]] || return 0

  echo
  echo "Optional streaming components:"
  echo "Selections are independent; Spotify Desktop and spotifyd may both be installed."
  if spotify_desktop_supported; then
    prompt_optional_provider "Spotify Desktop" && SELECT_SPOTIFY_DESKTOP=1
  else
    echo " - Spotify Desktop is not offered on this architecture or without a desktop session."
  fi
  prompt_optional_provider "spotifyd" && SELECT_SPOTIFYD=1
  prompt_optional_provider "Qobuz/qbzd" && SELECT_QOBUZ=1
  prompt_optional_provider "TIDAL support (tidalapi)" && SELECT_TIDAL=1
  return 0
}

spotifyd_arch_for_host() {
  case "${1:-${HOST_ARCH:-$(uname -m)}}" in
    x86_64) printf 'x86_64\n' ;;
    aarch64|arm64) printf 'aarch64\n' ;;
    armv7l|armv7|armhf) printf 'armv7\n' ;;
    *) return 1 ;;
  esac
}

qbzd_arch_for_host() {
  case "${1:-${HOST_ARCH:-$(uname -m)}}" in
    x86_64|amd64) printf 'amd64\n' ;;
    aarch64|arm64) printf 'aarch64\n' ;;
    *) return 1 ;;
  esac
}

root_state_is_trusted() {
  local state_dir="$(dirname "$ROOT_INSTALL_STATE_FILE")"
  local state_mode=""
  local dir_mode=""
  local recorded_root=""

  [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]] || return 1
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
  recorded_root="$(python3 - <<'PY' "$ROOT_INSTALL_STATE_FILE"
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text()).get("install_root", "")
except (OSError, ValueError):
    raise SystemExit(1)
print(value)
PY
  )" || return 1
  [[ -n "$recorded_root" && "$(expand_path "$recorded_root")" == "$(expand_path "$INSTALL_ROOT")" ]] || return 1
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

previous_install_state_field() {
  local field="$1"
  local state_file="$INSTALL_STATE_FILE"

  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]]; then
    root_state_is_trusted || return 1
    state_file="$ROOT_INSTALL_STATE_FILE"
  fi
  [[ -f "$state_file" && ! -L "$state_file" ]] || return 1
  python3 - <<'PY' "$state_file" "$field"
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text())
except (OSError, ValueError):
    raise SystemExit(1)
for part in sys.argv[2].split('.'):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit(1)
    value = value[part]
if isinstance(value, bool):
    print("true" if value else "false")
elif value is not None:
    print(value)
PY
}

load_provider_ownership_state() {
  local value=""
  local mdns_owned_state=""

  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" \
    && ( -e "$ROOT_INSTALL_STATE_FILE" || -L "$ROOT_INSTALL_STATE_FILE" ) ]]; then
    root_state_is_trusted || die "Existing root-owned FXRoute install state does not belong to the selected target $INSTALL_ROOT"
  fi
  if previous_install_state_field install_root >/dev/null 2>&1; then
    INSTALL_STATE_LOADED=1
  fi
  [[ "$(previous_install_state_field user_linger_enabled_by_fxroute 2>/dev/null || true)" == "true" ]] \
    && USER_LINGER_ENABLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.flatpak_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.apt_repo_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.apt_key_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.spotify_desktop.apt_repo_sha256 2>/dev/null)"; then
    SPOTIFY_DESKTOP_REPO_SHA256="$value"
  fi
  if value="$(previous_install_state_field providers.spotify_desktop.apt_key_fingerprint 2>/dev/null)"; then
    SPOTIFY_DESKTOP_KEY_FINGERPRINT="$value"
  fi
  if value="$(previous_install_state_field providers.spotify_desktop.installed_version 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFY_DESKTOP_INSTALLED_VERSION="$value"
  fi
  [[ "$(previous_install_state_field providers.spotifyd.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFYD_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotifyd.source_built 2>/dev/null || true)" == "true" ]] && SPOTIFYD_SOURCE_BUILT=1
  [[ "$(previous_install_state_field providers.spotifyd.service_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotifyd.config_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFYD_CONFIG_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.spotifyd.binary_path 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFYD_BINARY_PATH="$value"
  fi
  if value="$(previous_install_state_field providers.spotifyd.binary_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFYD_BINARY_SHA256="$value"
  fi
  if value="$(previous_install_state_field providers.spotifyd.service_path 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFYD_SERVICE_PATH="$value"
  fi
  if value="$(previous_install_state_field providers.spotifyd.service_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFYD_SERVICE_SHA256="$value"
  fi
  if value="$(previous_install_state_field providers.spotifyd.config_path 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFYD_CONFIG_PATH="$value"
  fi
  [[ "$(previous_install_state_field providers.qobuz.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && QBZD_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.qobuz.service_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && QBZD_SERVICE_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.qobuz.binary_path 2>/dev/null)"; then
    [[ -n "$value" ]] && QBZD_BINARY_PATH="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.binary_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && QBZD_BINARY_SHA256="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.service_path 2>/dev/null)"; then
    [[ -n "$value" ]] && QBZD_SERVICE_PATH="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.service_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && QBZD_SERVICE_SHA256="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.volume_mode_before 2>/dev/null)"; then
    QBZD_VOLUME_MODE_BEFORE="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.volume_mode_after 2>/dev/null)"; then
    QBZD_VOLUME_MODE_AFTER="$value"
  fi
  [[ "$(previous_install_state_field providers.qobuz.volume_mode_changed_by_fxroute 2>/dev/null || true)" == "true" ]] && QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.qobuz.qconnect_startup_mode_before 2>/dev/null)"; then
    QBZD_QCONNECT_STARTUP_MODE_BEFORE="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.qconnect_startup_mode_after 2>/dev/null)"; then
    QBZD_QCONNECT_STARTUP_MODE_AFTER="$value"
  fi
  [[ "$(previous_install_state_field providers.qobuz.qconnect_changed_by_fxroute 2>/dev/null || true)" == "true" ]] && QBZD_QCONNECT_CHANGED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.qobuz.audio_backend_before 2>/dev/null)"; then
    QBZD_AUDIO_BACKEND_BEFORE="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.audio_device_before 2>/dev/null)"; then
    QBZD_AUDIO_DEVICE_BEFORE="$value"
  fi
  if value="$(previous_install_state_field providers.qobuz.audio_skip_sink_switch_before 2>/dev/null)"; then
    QBZD_AUDIO_SKIP_SINK_SWITCH_BEFORE="$value"
  fi
  [[ "$(previous_install_state_field providers.qobuz.audio_changed_by_fxroute 2>/dev/null || true)" == "true" ]] && QBZD_AUDIO_CHANGED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.tidal.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && TIDAL_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.tidal.installed_version 2>/dev/null)"; then
    [[ -n "$value" ]] && TIDAL_INSTALLED_VERSION="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.avahi_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && AVAHI_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.avahi_enabled_by_fxroute 2>/dev/null || true)" == "true" ]] && AVAHI_ENABLED_BY_FXROUTE=1
  if value="$(previous_install_state_field lan_comfort.hostname_before 2>/dev/null)"; then
    LAN_HOSTNAME_BEFORE="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.hostname_after 2>/dev/null)"; then
    LAN_HOSTNAME_AFTER="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.hostname_changed_by_fxroute 2>/dev/null || true)" == "true" ]] && LAN_HOSTNAME_CHANGED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.avahi_was_present_before 2>/dev/null || true)" == "true" ]] && AVAHI_WAS_PRESENT_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.avahi_was_active_before 2>/dev/null || true)" == "true" ]] && AVAHI_WAS_ACTIVE_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.avahi_was_enabled_before 2>/dev/null || true)" == "true" ]] && AVAHI_WAS_ENABLED_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.avahi_ipv4_mdns_configured_by_fxroute 2>/dev/null || true)" == "true" ]] && AVAHI_IPV4_MDNS_CONFIGURED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.avahi_ipv4_mdns_config_backed_up 2>/dev/null || true)" == "true" ]] && AVAHI_IPV4_MDNS_CONFIG_BACKED_UP=1
  [[ "$(previous_install_state_field lan_comfort.caddy_was_present_before 2>/dev/null || true)" == "true" ]] && CADDY_WAS_PRESENT_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.caddy_service_was_active_before 2>/dev/null || true)" == "true" ]] && CADDY_SERVICE_WAS_ACTIVE_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.caddy_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && CADDY_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.default_caddy_disabled_by_fxroute 2>/dev/null || true)" == "true" ]] && DEFAULT_CADDY_DISABLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.caddy_proxy_enabled 2>/dev/null || true)" == "true" ]] && CADDY_PROXY_ENABLED=1
  [[ "$(previous_install_state_field lan_comfort.caddy_data_dir_created_by_fxroute 2>/dev/null || true)" == "true" ]] && CADDY_DATA_DIR_CREATED_BY_FXROUTE=1
  if value="$(previous_install_state_field lan_comfort.caddy_service_sha256 2>/dev/null)"; then
    CADDY_SERVICE_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.caddy_config_sha256 2>/dev/null)"; then
    CADDY_CONFIG_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.caddy_cert_sha256 2>/dev/null)"; then
    CADDY_CERT_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.caddy_cert_path 2>/dev/null)"; then
    [[ -n "$value" ]] && CADDY_CERT_PATH="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.cifs_helper_installed_by_fxroute 2>/dev/null || true)" == "true" ]] \
    && CIFS_HELPER_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.cifs_sudoers_rule_installed_by_fxroute 2>/dev/null || true)" == "true" ]] \
    && CIFS_SUDOERS_RULE_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field lan_comfort.cifs_sudoers_sha256 2>/dev/null)"; then
    CIFS_SUDOERS_SHA256="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.system_update_owned_by_fxroute 2>/dev/null || true)" == "true" ]] \
    && SYSTEM_UPDATE_OWNED_BY_FXROUTE=1
  if value="$(previous_install_state_field lan_comfort.system_update_service_sha256 2>/dev/null)"; then
    SYSTEM_UPDATE_SERVICE_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.system_update_timer_sha256 2>/dev/null)"; then
    SYSTEM_UPDATE_TIMER_SHA256="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.mdns_guard_enabled 2>/dev/null || true)" == "true" ]] && MDNS_GUARD_ENABLED=1
  if mdns_owned_state="$(previous_install_state_field lan_comfort.mdns_guard_owned_by_fxroute 2>/dev/null)"; then
    [[ "$mdns_owned_state" == "true" ]] && MDNS_GUARD_OWNED_BY_FXROUTE=1
  elif [[ "$(previous_install_state_field lan_comfort.mdns_guard_enabled 2>/dev/null || true)" == "true" ]]; then
    MDNS_GUARD_LEGACY_OWNERSHIP=1
  fi
  if value="$(previous_install_state_field lan_comfort.mdns_guard_script_sha256 2>/dev/null)"; then
    MDNS_GUARD_SCRIPT_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.mdns_guard_service_sha256 2>/dev/null)"; then
    MDNS_GUARD_SERVICE_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.mdns_guard_timer_sha256 2>/dev/null)"; then
    MDNS_GUARD_TIMER_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.mdns_guard_target_uid 2>/dev/null)"; then
    MDNS_GUARD_TARGET_UID="$value"
  fi
  if [[ "$mdns_owned_state" == "true" && ( -z "$MDNS_GUARD_SCRIPT_SHA256" \
    || -z "$MDNS_GUARD_SERVICE_SHA256" || -z "$MDNS_GUARD_TIMER_SHA256" ) ]]; then
    MDNS_GUARD_LEGACY_OWNERSHIP=1
  fi
  [[ "$(previous_install_state_field lan_comfort.firewalld_was_active_before 2>/dev/null || true)" == "true" ]] && FIREWALLD_WAS_ACTIVE_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.http_was_allowed_before 2>/dev/null || true)" == "true" ]] && HTTP_WAS_ALLOWED_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.https_was_allowed_before 2>/dev/null || true)" == "true" ]] && HTTPS_WAS_ALLOWED_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.mdns_was_allowed_before 2>/dev/null || true)" == "true" ]] && MDNS_WAS_ALLOWED_BEFORE=1
  [[ "$(previous_install_state_field lan_comfort.http_opened_by_fxroute 2>/dev/null || true)" == "true" ]] && HTTP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.https_opened_by_fxroute 2>/dev/null || true)" == "true" ]] && HTTPS_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.mdns_opened_by_fxroute 2>/dev/null || true)" == "true" ]] && MDNS_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.http_80_tcp 2>/dev/null || true)" == "true" ]] && FIREWALLD_HTTP_80_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.https_443_tcp 2>/dev/null || true)" == "true" ]] && FIREWALLD_HTTPS_443_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.mdns_5353_udp 2>/dev/null || true)" == "true" ]] && FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.fxroute_http_8000_tcp 2>/dev/null || true)" == "true" ]] && FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.spotifyd_zeroconf_4444_tcp 2>/dev/null || true)" == "true" ]] && FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.ufw_owned_rules.http_80_tcp 2>/dev/null || true)" == "true" ]] && UFW_HTTP_80_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.ufw_owned_rules.https_443_tcp 2>/dev/null || true)" == "true" ]] && UFW_HTTPS_443_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.ufw_owned_rules.mdns_5353_udp 2>/dev/null || true)" == "true" ]] && UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.ufw_owned_rules.fxroute_http_8000_tcp 2>/dev/null || true)" == "true" ]] && UFW_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE=1
  [[ "$(previous_install_state_field lan_comfort.ufw_owned_rules.spotifyd_zeroconf_4444_tcp 2>/dev/null || true)" == "true" ]] && UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE=1
  if [[ "$(previous_install_state_field lan_comfort.legacy_firewall_ownership_present 2>/dev/null || true)" == "true" ]]; then
    FIREWALL_LEGACY_STATE_PRESENT=1
  elif [[ -z "$(previous_install_state_field lan_comfort.firewall_ownership_schema 2>/dev/null || true)" ]] \
    && previous_install_state_field lan_comfort.http_opened_by_fxroute >/dev/null 2>&1; then
    FIREWALL_LEGACY_STATE_PRESENT=1
  fi
  if value="$(previous_install_state_field lan_comfort.firewalld_rule_format 2>/dev/null)"; then
    [[ -n "$value" ]] && FIREWALLD_RULE_FORMAT="$value"
  elif [[ $INSTALL_STATE_LOADED -eq 1 ]] \
    && { [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.http_80_tcp 2>/dev/null || true)" == "true" ]] \
      || [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.https_443_tcp 2>/dev/null || true)" == "true" ]] \
      || [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.mdns_5353_udp 2>/dev/null || true)" == "true" ]] \
      || [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.fxroute_http_8000_tcp 2>/dev/null || true)" == "true" ]] \
      || [[ "$(previous_install_state_field lan_comfort.firewalld_owned_rules.spotifyd_zeroconf_4444_tcp 2>/dev/null || true)" == "true" ]] \
      || [[ "$(previous_install_state_field lan_comfort.http_opened_by_fxroute 2>/dev/null || true)" == "true" ]]; }; then
    FIREWALLD_RULE_FORMAT="legacy-port"
  fi
  [[ "$FIREWALLD_RULE_FORMAT" == "legacy-port" ]] && FIREWALLD_LEGACY_PORT_MIGRATION=1
  [[ "$(previous_install_state_field lan_comfort.power_polkit_installed 2>/dev/null || true)" == "true" ]] && POWER_POLKIT_INSTALLED=1
  if value="$(previous_install_state_field lan_comfort.power_polkit_rule_path 2>/dev/null)"; then
    [[ -n "$value" ]] && POWER_POLKIT_RULE_PATH="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.power_polkit_backup_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && POWER_POLKIT_BACKUP_SHA256="$value"
  fi
  if value="$(previous_install_state_field lan_comfort.power_polkit_rule_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && POWER_POLKIT_RULE_SHA256="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.power_polkit_rule_pre_existed 2>/dev/null || true)" == "true" ]] && POWER_POLKIT_RULE_PRE_EXISTED=1
  [[ "$(previous_install_state_field providers.privilege_escalation.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && PROVIDER_PRIVILEGE_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.privilege_escalation.sudoers_sha256 2>/dev/null)"; then
    [[ -n "$value" ]] && PROVIDER_PRIVILEGE_SUDOERS_SHA256="$value"
  fi
  return 0
}

bt_plugin_present() {
  local candidates=(
    /usr/lib64/spa-0.2/bluez5/libspa-bluez5.so
    /usr/lib/spa-0.2/bluez5/libspa-bluez5.so
    /usr/lib/x86_64-linux-gnu/spa-0.2/bluez5/libspa-bluez5.so
    /usr/lib/aarch64-linux-gnu/spa-0.2/bluez5/libspa-bluez5.so
    /usr/lib/arm-linux-gnueabihf/spa-0.2/bluez5/libspa-bluez5.so
  )
  local path

  for path in "${candidates[@]}"; do
    [[ -f "$path" ]] && return 0
  done

  shopt -s nullglob
  local matches=(/usr/lib/*-linux-gnu/spa-0.2/bluez5/libspa-bluez5.so)
  shopt -u nullglob
  [[ ${#matches[@]} -gt 0 ]]
}

backup_user_file_once() {
  local path="$1"
  local backup_name="$2"
  local backup_path="$FXROUTE_BACKUP_DIR/$backup_name"

  [[ -e "$path" || -L "$path" ]] || return 1
  [[ -e "$backup_path" || -L "$backup_path" ]] && return 0

  mkdir -p "$FXROUTE_BACKUP_DIR"
  cp -a "$path" "$backup_path"
  return 0
}

valid_local_hostname() {
  local value="${1,,}"
  [[ "$value" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
}

primary_lan_ip() {
  local ip=""
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [[ -z "$ip" ]] && command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}' || true)"
  fi
  if [[ -z "$ip" ]] && command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 addr show scope global 2>/dev/null | awk '/inet / {sub(/\/.*/, "", $2); print $2; exit}' || true)"
  fi
  [[ -n "$ip" ]] && printf '%s\n' "$ip"
  return 0
}

avahi_is_present() {
  case "$PACKAGE_MANAGER" in
    apt)
      dpkg-query -W -f='${Status}' avahi-daemon 2>/dev/null | grep -q "install ok installed"
      ;;
    dnf|zypper)
      rpm -q avahi >/dev/null 2>&1
      ;;
    pacman)
      pacman -Q avahi >/dev/null 2>&1
      ;;
    *)
      command -v avahi-daemon >/dev/null 2>&1 \
        || [[ -x /usr/sbin/avahi-daemon ]] \
        || [[ -x /usr/bin/avahi-daemon ]] \
        || systemctl list-unit-files avahi-daemon.service --no-legend 2>/dev/null | grep -q '^avahi-daemon\.service'
      ;;
  esac
}

configure_avahi_ipv4_mdns_for_fxroute() {
  local config_path="/etc/avahi/avahi-daemon.conf"
  local backup_path="${config_path}.pre-fxroute-ipv4-mdns"
  local tmp_config=""

  [[ -f "$config_path" ]] || {
    warn "Optional .local setup could not find ${config_path}; skipping IPv4-only mDNS hardening"
    return 0
  }

  tmp_config="$(mktemp)"
  if ! python3 - "$config_path" "$tmp_config" <<'PY'
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
lines = source.read_text().splitlines()

desired = {
    "server": {
        "use-ipv4": "yes",
        "use-ipv6": "no",
    },
    "publish": {
        "publish-aaaa-on-ipv4": "no",
        "publish-a-on-ipv6": "no",
    },
}

out = []
section = None
seen_sections = set()
seen_keys = {name: set() for name in desired}

def flush_missing(section_name):
    if section_name in desired:
        for key, value in desired[section_name].items():
            if key not in seen_keys[section_name]:
                out.append(f"{key}={value}")
                seen_keys[section_name].add(key)

for raw in lines:
    stripped = raw.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        flush_missing(section)
        section = stripped[1:-1].strip().lower()
        seen_sections.add(section)
        out.append(raw)
        continue

    if section in desired:
        candidate = stripped
        if candidate.startswith("#"):
            candidate = candidate[1:].strip()
        if "=" in candidate:
            key = candidate.split("=", 1)[0].strip().lower()
            if key in desired[section]:
                out.append(f"{key}={desired[section][key]}")
                seen_keys[section].add(key)
                continue

    out.append(raw)

flush_missing(section)
for section_name, values in desired.items():
    if section_name not in seen_sections:
        if out and out[-1] != "":
            out.append("")
        out.append(f"[{section_name}]")
    for key, value in values.items():
        if key not in seen_keys[section_name]:
            out.append(f"{key}={value}")

target.write_text("\n".join(out) + "\n")
PY
  then
    rm -f "$tmp_config"
    warn "Optional .local setup could not prepare IPv4-only Avahi config"
    return 0
  fi

  if cmp -s "$config_path" "$tmp_config"; then
    rm -f "$tmp_config"
    AVAHI_IPV4_MDNS_CONFIGURED_BY_FXROUTE=1
    pass "Avahi mDNS already configured for IPv4-only FXRoute .local access"
    return 0
  fi

  if [[ ! -e "$backup_path" && ! -L "$backup_path" ]]; then
    log "cp -a $config_path $backup_path"
    if ! "${SUDO_CMD[@]}" cp -a "$config_path" "$backup_path"; then
      rm -f "$tmp_config"
      warn "Optional .local setup could not back up ${config_path}; skipping IPv4-only mDNS hardening"
      return 0
    fi
    AVAHI_IPV4_MDNS_CONFIG_BACKED_UP=1
  fi

  log "install -m 644 $tmp_config $config_path"
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_config" "$config_path"; then
    rm -f "$tmp_config"
    warn "Optional .local setup could not write IPv4-only Avahi config"
    return 0
  fi
  rm -f "$tmp_config"

  AVAHI_IPV4_MDNS_CONFIGURED_BY_FXROUTE=1
  pass "Avahi mDNS configured for IPv4-only FXRoute .local access"
}

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

firewalld_is_active() {
  local firewall_cmd=""
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 1
  "${SUDO_CMD[@]}" "$firewall_cmd" --state >/dev/null 2>&1
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

firewall_rule_state_var() {
  case "$1:$2" in
    firewalld:http_80_tcp) printf 'FIREWALLD_HTTP_80_TCP_OPENED_BY_FXROUTE\n' ;;
    firewalld:https_443_tcp) printf 'FIREWALLD_HTTPS_443_TCP_OPENED_BY_FXROUTE\n' ;;
    firewalld:mdns_5353_udp) printf 'FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE\n' ;;
    firewalld:fxroute_http_8000_tcp) printf 'FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE\n' ;;
    firewalld:spotifyd_zeroconf_4444_tcp) printf 'FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE\n' ;;
    ufw:http_80_tcp) printf 'UFW_HTTP_80_TCP_OPENED_BY_FXROUTE\n' ;;
    ufw:https_443_tcp) printf 'UFW_HTTPS_443_TCP_OPENED_BY_FXROUTE\n' ;;
    ufw:mdns_5353_udp) printf 'UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE\n' ;;
    ufw:fxroute_http_8000_tcp) printf 'UFW_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE\n' ;;
    ufw:spotifyd_zeroconf_4444_tcp) printf 'UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE\n' ;;
    *) return 1 ;;
  esac
}

firewall_rule_owned() {
  local backend="$1"
  local rule_id="$2"
  local state_var=""
  state_var="$(firewall_rule_state_var "$backend" "$rule_id")" || return 1
  [[ "${!state_var:-0}" -eq 1 ]]
}

mark_firewall_rule_owned() {
  local backend="$1"
  local rule_id="$2"
  local state_var=""
  state_var="$(firewall_rule_state_var "$backend" "$rule_id")" || return 1
  printf -v "$state_var" '%s' 1
  case "$rule_id" in
    http_80_tcp) HTTP_OPENED_BY_FXROUTE=1 ;;
    https_443_tcp) HTTPS_OPENED_BY_FXROUTE=1 ;;
    mdns_5353_udp) MDNS_OPENED_BY_FXROUTE=1 ;;
  esac
}

firewalld_query_port() {
  local port="$1"
  local firewall_cmd=""
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 1
  firewalld_is_active || return 2
  if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --query-port="$port"; then
    return 0
  elif [[ $? -ne 1 ]]; then
    return 2
  fi
  firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-port="$port"
}

firewalld_rule_service() {
  case "$1" in
    http_80_tcp) printf 'http\n' ;;
    https_443_tcp) printf 'https\n' ;;
    mdns_5353_udp) printf 'mdns\n' ;;
    *) return 1 ;;
  esac
}

firewalld_query_rule() {
  local rule_id="$1"
  local port=""
  local service=""
  local firewall_cmd=""
  local query_status=0

  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 1
  if firewalld_query_port "$(firewall_rule_port "$rule_id")"; then
    return 0
  else
    query_status=$?
    [[ $query_status -eq 1 ]] || return 2
  fi
  service="$(firewalld_rule_service "$rule_id" 2>/dev/null || true)"
  [[ -n "$service" ]] || return 1
  if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --query-service="$service"; then
    return 0
  else
    query_status=$?
    [[ $query_status -eq 1 ]] || return 2
  fi
  firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-service="$service"
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

firewalld_query_rich_rule() {
  local rich_rule="$1"
  local firewall_cmd=""

  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 1
  firewalld_is_active || return 2
  if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --query-rich-rule="$rich_rule"; then
    return 0
  elif [[ $? -ne 1 ]]; then
    return 2
  fi
  firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-rich-rule="$rich_rule"
}

confirm_legacy_firewalld_port_migration() {
  local port="$1"
  local reply=""

  [[ $ASSUME_YES -eq 1 ]] && return 0
  if [[ ! -t 0 || ! -t 1 ]]; then
    warn "Preserving historical firewalld port '$port'; rerun interactively or use --yes to migrate it"
    return 1
  fi
  printf "Migrate the historical FXRoute firewalld port '$port' to an owned rich rule? [y/N] "
  read -r reply || return 1
  [[ "${reply,,}" == "y" || "${reply,,}" == "yes" ]]
}

migrate_legacy_firewalld_port() {
  local rule_id="$1"
  local port=""
  local firewall_cmd=""
  local runtime_present=0
  local permanent_present=0
  local query_status=0

  [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || return 1
  firewall_rule_owned firewalld "$rule_id" || return 1
  port="$(firewall_rule_port "$rule_id")" || return 1
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 2
  firewalld_is_active || return 2

  if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --query-port="$port"; then
    runtime_present=1
  else
    query_status=$?
    [[ $query_status -eq 1 ]] || return 2
  fi
  if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-port="$port"; then
    permanent_present=1
  else
    query_status=$?
    [[ $query_status -eq 1 ]] || return 2
  fi
  [[ $runtime_present -eq 1 || $permanent_present -eq 1 ]] || return 1
  confirm_legacy_firewalld_port_migration "$port" || return 1

  if [[ $runtime_present -eq 1 ]] \
    && ! "${SUDO_CMD[@]}" "$firewall_cmd" --remove-port="$port"; then
    return 2
  fi
  if [[ $permanent_present -eq 1 ]] \
    && ! "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --remove-port="$port"; then
    return 2
  fi
  "${SUDO_CMD[@]}" "$firewall_cmd" --reload || return 2
  return 0
}

ensure_firewalld_rule() {
  local rule_id="$1"
  local purpose="$2"
  local port=""
  local firewall_cmd=""
  local rich_rule=""
  local legacy_rich_rule=""
  local query_status=0
  local legacy_runtime_present=0
  local legacy_permanent_present=0

  port="$(firewall_rule_port "$rule_id")" || return 0
  rich_rule="$(firewalld_rule_rich_rule "$rule_id")" || return 0
  legacy_rich_rule="$(firewalld_legacy_rich_rule "$rule_id")" || return 0
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 0
  firewalld_is_active || return 0

  if migrate_legacy_firewalld_port "$rule_id"; then
    [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || FIREWALLD_RULE_FORMAT="rich-priority"
  else
    query_status=$?
    if [[ $query_status -eq 2 ]]; then
      warn "Optional LAN comfort could not migrate the legacy firewalld port for '$port'"
      return 0
    fi
  fi

  if firewalld_query_rich_rule "$rich_rule"; then
    [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || FIREWALLD_RULE_FORMAT="rich-priority"
    # A rule already carrying the exact FXRoute rich-rule signature was
    # created by FXRoute in this or an earlier run. Record ownership so a
    # later uninstall removes it; re-runs over an existing rule used to skip
    # this and left the rule behind.
    mark_firewall_rule_owned firewalld "$rule_id"
    return 0
  else
    query_status=$?
    if [[ $query_status -ne 1 ]]; then
      warn "Optional LAN comfort could not verify firewalld rich rule for '$port'"
      return 0
    fi
  fi
  if firewalld_query_rich_rule "$legacy_rich_rule"; then
    if ! firewall_rule_owned firewalld "$rule_id"; then
      if [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]]; then
        FIREWALLD_RULE_FORMAT="legacy-port"
      else
        FIREWALLD_RULE_FORMAT="legacy-rich"
      fi
      return 0
    fi
    if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --query-rich-rule="$legacy_rich_rule"; then
      legacy_runtime_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Optional LAN comfort could not verify legacy runtime firewalld rule for '$port'"
        return 0
      fi
    fi
    if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-rich-rule="$legacy_rich_rule"; then
      legacy_permanent_present=1
    else
      query_status=$?
      if [[ $query_status -ne 1 ]]; then
        warn "Optional LAN comfort could not verify legacy permanent firewalld rule for '$port'"
        return 0
      fi
    fi
    if [[ $legacy_runtime_present -eq 1 ]] \
      && ! "${SUDO_CMD[@]}" "$firewall_cmd" --remove-rich-rule="$legacy_rich_rule"; then
      warn "Optional LAN comfort could not migrate the legacy runtime firewalld rule for '$port'"
      return 0
    fi
    if [[ $legacy_permanent_present -eq 1 ]] \
      && ! "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --remove-rich-rule="$legacy_rich_rule"; then
      warn "Optional LAN comfort could not migrate the legacy permanent firewalld rule for '$port'"
      return 0
    fi
    if ! "${SUDO_CMD[@]}" "$firewall_cmd" --reload; then
      warn "Optional LAN comfort could not reload firewalld after migrating '$port'"
      return 0
    fi
    [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || FIREWALLD_RULE_FORMAT="rich-priority"
  else
    query_status=$?
    if [[ $query_status -ne 1 ]]; then
      warn "Optional LAN comfort could not verify legacy firewalld rich rule for '$port'"
      return 0
    fi
  fi
  if firewalld_query_rule "$rule_id"; then
    # A bare port/service rule is not the FXRoute rich-rule signature; it
    # may belong to the user, so do not mark ownership here.
    return 0
  else
    query_status=$?
    if [[ $query_status -ne 1 ]]; then
      warn "Optional LAN comfort could not verify firewalld rule for '$port'"
      return 0
    fi
  fi

  log "$firewall_cmd --permanent --add-rich-rule=$rich_rule"
  if ! "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --add-rich-rule="$rich_rule"; then
    warn "Optional LAN comfort could not open firewalld rich rule for '$port' for $purpose"
    return 0
  fi
  [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || FIREWALLD_RULE_FORMAT="rich-priority"
  mark_firewall_rule_owned firewalld "$rule_id"

  log "$firewall_cmd --reload"
  if ! "${SUDO_CMD[@]}" "$firewall_cmd" --reload; then
    warn "Optional LAN comfort opened firewalld rich rule for '$port' permanently, but reload failed"
    return 0
  fi

  pass "firewalld port opened ($port)"
}

ufw_is_active() {
  command -v ufw >/dev/null 2>&1 || return 1
  "${SUDO_CMD[@]}" ufw status 2>/dev/null | grep -qi '^Status: active\|^Status: Aktiv'
}

ensure_ufw_rule() {
  local rule_id="$1"
  local purpose="$2"
  local port=""
  local query_rc=0

  port="$(firewall_rule_port "$rule_id")" || return 0
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: only the two provider rule ids. One
    # helper fw-open call owns the whole backends decision (firewalld or
    # ufw or inactive-noop) and re-validates rule + purpose itself; the
    # ownership flags are recorded for whichever backend applied.
    case "$rule_id" in
      mdns_5353_udp|spotifyd_zeroconf_4444_tcp) ;;
      *) return 0 ;;
    esac
    provider_privileged fw-query "$rule_id" >/dev/null 2>&1 || query_rc=$?
    if [[ $query_rc -eq 0 ]]; then
      mark_firewall_rule_owned firewalld "$rule_id"
      mark_firewall_rule_owned ufw "$rule_id"
      return 0
    elif [[ $query_rc -ne 1 ]]; then
      warn "Optional LAN comfort could not verify firewall rule for '$port'"
      return 0
    fi
    log "provider helper fw-open $rule_id"
    if ! provider_privileged fw-open "$rule_id" "$purpose"; then
      warn "Optional LAN comfort could not open firewall rule '$port' for $purpose"
      return 0
    fi
    mark_firewall_rule_owned firewalld "$rule_id"
    mark_firewall_rule_owned ufw "$rule_id"
    pass "firewall rule opened ($port)"
    return 0
  fi
  ufw_is_active || return 0

  if "${SUDO_CMD[@]}" ufw status 2>/dev/null | grep -Eq "^${port}([[:space:]]+\\(v6\\))?[[:space:]]+ALLOW"; then
    return 0
  fi

  log "ufw allow $port"
  if ! "${SUDO_CMD[@]}" ufw allow "$port" comment "$purpose"; then
    warn "Optional LAN comfort could not open ufw rule '$port' for $purpose"
    return 0
  fi
  mark_firewall_rule_owned ufw "$rule_id"

  pass "ufw rule opened ($port)"
}

ensure_firewalld_rule_providers_only() {
  # Narrow provider-path variant: only the two provider rule ids, no
  # legacy port migration (interactive prompt). The single fw-open call in
  # ensure_ufw_rule already owns the firewalld-or-ufw-or-noop decision, so
  # this function only records the firewalld-side ownership view: when the
  # rule queries open afterwards, it was opened (or already open) on the
  # firewalld backend.
  local rule_id="$1"
  local query_rc=0

  case "$rule_id" in
    mdns_5353_udp|spotifyd_zeroconf_4444_tcp) ;;
    *) return 0 ;;
  esac
  firewall_rule_port "$rule_id" >/dev/null || return 0
  provider_privileged fw-query "$rule_id" >/dev/null 2>&1 || query_rc=$?
  if [[ $query_rc -eq 0 ]]; then
    [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || FIREWALLD_RULE_FORMAT="rich-priority"
    mark_firewall_rule_owned firewalld "$rule_id"
  elif [[ $query_rc -ne 1 ]]; then
    warn "Optional LAN comfort could not verify firewalld rich rule for '$(firewall_rule_port "$rule_id")'"
  fi
}

ensure_lan_firewall_rule() {
  local rule_id="$1"
  local purpose="$2"

  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: only the two provider rule ids, no
    # legacy migration prompts. ensure_ufw_rule performs the single owning
    # fw-open; the firewalld view is recorded afterwards from the query.
    ensure_ufw_rule "$rule_id" "$purpose"
    ensure_firewalld_rule_providers_only "$rule_id"
    return 0
  fi
  ensure_firewalld_rule "$rule_id" "$purpose"
  ensure_ufw_rule "$rule_id" "$purpose"
}

ensure_lan_firewall_service_open() {
  local service="$1"
  local rule_id=""

  case "$service" in
    http) rule_id="http_80_tcp" ;;
    https) rule_id="https_443_tcp" ;;
    mdns) rule_id="mdns_5353_udp" ;;
    fxroute-http) rule_id="fxroute_http_8000_tcp" ;;
    *) return 0 ;;
  esac
  ensure_lan_firewall_rule "$rule_id" "$2"
}

finalize_firewalld_rule_format() {
  local rule_id=""
  local port=""
  local firewall_cmd=""
  local query_status=0

  [[ $FIREWALLD_LEGACY_PORT_MIGRATION -eq 1 ]] || return 0
  FIREWALLD_RULE_FORMAT="legacy-port"
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 0
  firewalld_is_active || return 0

  for rule_id in \
    http_80_tcp \
    https_443_tcp \
    mdns_5353_udp \
    fxroute_http_8000_tcp \
    spotifyd_zeroconf_4444_tcp; do
    firewall_rule_owned firewalld "$rule_id" || continue
    port="$(firewall_rule_port "$rule_id")" || continue
    if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --query-port="$port"; then
      return 0
    else
      query_status=$?
      [[ $query_status -eq 1 ]] || return 0
    fi
    if firewalld_query_status "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-port="$port"; then
      return 0
    else
      query_status=$?
      [[ $query_status -eq 1 ]] || return 0
    fi
  done

  FIREWALLD_RULE_FORMAT="rich-priority"
  FIREWALLD_LEGACY_PORT_MIGRATION=0
}

choose_sudo() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    SUDO_CMD=()
  elif command -v sudo >/dev/null 2>&1; then
    SUDO_CMD=(sudo)
  else
    die "sudo is required for package installation"
  fi
}

provider_privileged() {
  # Run one allow-listed privileged provider step through the root-owned
  # helper. The helper validates action + arguments itself; callers pass
  # only fixed action names and values from the provider allow-lists, never
  # user input. Non-interactive: uses "sudo -n" so a password prompt can
  # never stall a Settings-UI request.
  sudo -n "$PROVIDER_HELPER_PATH" "$@"
}

capture_lan_comfort_baseline() {
  if [[ $INSTALL_STATE_LOADED -eq 1 ]] \
    && previous_install_state_field lan_comfort.hostname_before >/dev/null 2>&1; then
    return 0
  fi

  LAN_HOSTNAME_BEFORE="$(hostname 2>/dev/null || true)"
  LAN_HOSTNAME_AFTER="$LAN_HOSTNAME_BEFORE"

  if avahi_is_present; then
    AVAHI_WAS_PRESENT_BEFORE=1
  fi
  if systemctl is-active avahi-daemon >/dev/null 2>&1; then
    AVAHI_WAS_ACTIVE_BEFORE=1
  fi
  if systemctl is-enabled avahi-daemon >/dev/null 2>&1; then
    AVAHI_WAS_ENABLED_BEFORE=1
  fi

  if command -v caddy >/dev/null 2>&1; then
    CADDY_WAS_PRESENT_BEFORE=1
  fi
  if systemctl is-active caddy.service >/dev/null 2>&1; then
    CADDY_SERVICE_WAS_ACTIVE_BEFORE=1
  fi

  if firewalld_is_active; then
    FIREWALLD_WAS_ACTIVE_BEFORE=1
    if firewalld_query_port 80/tcp; then
      HTTP_WAS_ALLOWED_BEFORE=1
    fi
    if firewalld_query_port 443/tcp; then
      HTTPS_WAS_ALLOWED_BEFORE=1
    fi
    if firewalld_query_port 5353/udp; then
      MDNS_WAS_ALLOWED_BEFORE=1
    fi
  fi
}

confirm_supported_distro() {
  if command -v apt-get >/dev/null 2>&1; then
    PACKAGE_MANAGER="apt"
    PACKAGE_INSTALL_CMD=("${SUDO_CMD[@]}" apt-get update)
  elif command -v dnf >/dev/null 2>&1; then
    PACKAGE_MANAGER="dnf"
  elif command -v zypper >/dev/null 2>&1; then
    PACKAGE_MANAGER="zypper"
  elif command -v pacman >/dev/null 2>&1; then
    PACKAGE_MANAGER="pacman"
  else
    die "Unsupported distro. Expected apt, dnf, zypper, or pacman."
  fi
  pass "distro detection: $PACKAGE_MANAGER"
}

run_cmd() {
  log "$*"
  "$@"
}

pkg_install() {
  local packages=("$@")
  # Provider installs (Settings -> Providers, --providers-only) run without
  # a TTY: route package installs through the root-owned helper, whose
  # per-manager package names are allow-listed on both sides. The helper
  # skips already-installed packages itself, so callers pass the missing
  # set and stay idempotent.
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    run_cmd provider_privileged packages "${packages[@]}"
    return 0
  fi
  case "$PACKAGE_MANAGER" in
    apt)
      if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
        run_cmd "${SUDO_CMD[@]}" apt-get update
        PKG_REFRESH_DONE=1
      fi
      run_cmd "${SUDO_CMD[@]}" apt-get install -y "${packages[@]}"
      ;;
    dnf)
      if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
        run_cmd "${SUDO_CMD[@]}" dnf install -y --refresh "${packages[@]}"
        PKG_REFRESH_DONE=1
      else
        run_cmd "${SUDO_CMD[@]}" dnf install -y "${packages[@]}"
      fi
      ;;
    zypper)
      if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
        run_cmd "${SUDO_CMD[@]}" zypper --non-interactive refresh
        PKG_REFRESH_DONE=1
      fi
      run_cmd "${SUDO_CMD[@]}" zypper --non-interactive install --no-recommends "${packages[@]}"
      ;;
    pacman)
      if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
        log "Arch/Manjaro uses a rolling-release model; running a full system update (pacman -Syu) before the first package installation"
        run_cmd "${SUDO_CMD[@]}" pacman -Syu --needed --noconfirm "${packages[@]}"
        PKG_REFRESH_DONE=1
      else
        run_cmd "${SUDO_CMD[@]}" pacman -S --needed --noconfirm "${packages[@]}"
      fi
      ;;
  esac
}

package_installed() {
  local package="$1"
  case "$PACKAGE_MANAGER" in
    apt) dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'ok installed' ;;
    dnf|zypper) rpm -q "$package" >/dev/null 2>&1 ;;
    pacman) pacman -Q "$package" >/dev/null 2>&1 ;;
    *) return 1 ;;
  esac
}

smb_packages_for_manager() {
  case "$1" in
    apt) echo "smbclient cifs-utils libglib2.0-bin gvfs gvfs-backends gvfs-fuse" ;;
    dnf) echo "samba-client cifs-utils glib2 gvfs gvfs-smb gvfs-fuse" ;;
    zypper) echo "samba-client cifs-utils glib2-tools gvfs gvfs-backend-samba gvfs-fuse" ;;
    pacman) echo "smbclient cifs-utils glib2 gvfs gvfs-smb" ;;
    *) return 1 ;;
  esac
}

ensure_smb_packages() {
  local packages=()
  local missing=()
  local pkg=""
  read -r -a packages <<<"$(smb_packages_for_manager "$PACKAGE_MANAGER")"
  for pkg in "${packages[@]}"; do
    package_installed "$pkg" || missing+=("$pkg")
  done
  [[ ${#missing[@]} -eq 0 ]] || pkg_install "${missing[@]}"
}

zypper_python_package() {
  local package_kind="$1"
  local os_release_path="${2:-/etc/os-release}"
  local distro_id=""
  local version_id=""

  distro_id="$(sed -n 's/^ID=//p' "$os_release_path" 2>/dev/null | head -n 1 | tr -d '"')"
  version_id="$(sed -n 's/^VERSION_ID=//p' "$os_release_path" 2>/dev/null | head -n 1 | tr -d '"')"
  if [[ "$distro_id" == "opensuse-leap" ]]; then
    case "$version_id" in
      16|16.*)
        case "$package_kind" in
          pip) printf 'python313-pip\n' ;;
          virtualenv) printf 'python313-virtualenv\n' ;;
          *) return 1 ;;
        esac
        return 0
        ;;
    esac
  fi
  case "$package_kind" in
    pip) printf 'python3-pip\n' ;;
    virtualenv) printf 'python3-virtualenv\n' ;;
    *) return 1 ;;
  esac
}

debian_trixie_backports_available() {
  local os_release_path="${1:-/etc/os-release}"
  local sources_root="${2:-/etc/apt}"
  local codename=""
  if [[ -f "$os_release_path" ]]; then
    codename="$(grep -E '^VERSION_CODENAME=' "$os_release_path" | cut -d= -f2 | tr -d '"')"
  fi
  [[ "$codename" == "trixie" ]] || return 1
  grep -rq "trixie-backports" "$sources_root/sources.list" "$sources_root/sources.list.d/" 2>/dev/null
}

debian_trixie_backports_active() {
  # True once the host actually runs the backported PipeWire runtime, so the
  # matching -dev packages must come from the same suite. The runtime is only
  # switched by ensure_debian_pipewire_backports; a merely configured
  # trixie-backports suite with the stock 1.4.2 runtime keeps the main -dev
  # packages.
  [[ "${PACKAGE_MANAGER:-}" == "apt" ]] || return 1
  debian_trixie_backports_available "$@" || return 1
  local installed=""
  installed="$(dpkg-query -W -f='${Version}' pipewire 2>/dev/null || true)"
  [[ "$installed" == *bpo13* ]]
}

ensure_debian_pipewire_backports() {
  # PipeWire minor releases carry null-sink/graph behaviors FXRoute relies
  # on (verified on real hardware: the 1.4.9 null-sink follows the graph
  # clock while the 1.4.2 ingress stays pinned at its 48 kHz default and
  # forces a permanent resampling stage).  On Debian 13 (trixie, including
  # Armbian) the fixed stack comes from trixie-backports; every other
  # distro keeps its default packages.  A failed backports install must
  # never abort the installer: the distribution stack keeps working.
  [[ "${PACKAGE_MANAGER:-}" == "apt" ]] || return 0
  if ! debian_trixie_backports_available; then
    log "PipeWire backports: no Debian 13 host with trixie-backports configured; keeping distribution packages"
    return 0
  fi
  local backport_packages=(pipewire pipewire-bin pipewire-pulse libpipewire-0.3-0t64 libpipewire-0.3-modules libspa-0.2-modules libspa-0.2-bluetooth wireplumber libwireplumber-0.5-0)
  if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
    run_cmd "${SUDO_CMD[@]}" apt-get update
    PKG_REFRESH_DONE=1
  fi
  if run_cmd "${SUDO_CMD[@]}" apt-get install -y -t trixie-backports "${backport_packages[@]}"; then
    pass "PipeWire audio stack from trixie-backports"
  else
    warn "PipeWire backports install failed; keeping distribution packages (null-sink rate following needs PipeWire 1.4.9 or newer)"
  fi
  return 0
}

ensure_native_packages() {
  local core_packages=()
  local support_packages=(curl git socat tar)
  local audio_stack_packages=()
  local missing_packages=()
  local missing_support=()
  local missing_audio_stack=()
  local need_venv_pkg=0
  local need_bt_plugin_pkg=0
  local zypper_pip_package=""
  local zypper_venv_package=""

  case "$PACKAGE_MANAGER" in
    apt)
      core_packages=(python3 python3-pip python3-venv mpv ffmpeg playerctl)
      audio_stack_packages=(bluez wireplumber pipewire-bin pipewire-pulse pulseaudio-utils libspa-0.2-bluetooth rtkit)
      ;;
    dnf)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl)
      audio_stack_packages=(bluez wireplumber pipewire-utils pipewire-pulseaudio pulseaudio-utils rtkit)
      ;;
    zypper)
      zypper_pip_package="$(zypper_python_package pip)"
      zypper_venv_package="$(zypper_python_package virtualenv)"
      core_packages=(python3 "$zypper_pip_package" mpv ffmpeg playerctl)
      audio_stack_packages=(bluez wireplumber pipewire-tools pipewire-pulseaudio pulseaudio-utils pipewire-spa-plugins-0_2 rtkit)
      ;;
    pacman)
      core_packages=(python python-pip mpv ffmpeg playerctl)
      audio_stack_packages=(bluez bluez-utils wireplumber pipewire pipewire-pulse libpulse rtkit)
      ;;
  esac

  for cmd in python3 mpv ffmpeg playerctl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing_packages+=("$cmd")
    fi
  done
  for cmd in "${support_packages[@]}"; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing_support+=("$cmd")
    fi
  done
  for cmd in bluetoothctl wpctl pw-cli pactl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing_audio_stack+=("$cmd")
    fi
  done
  if ! package_installed rtkit; then
    missing_audio_stack+=(rtkit)
  fi
  ensure_smb_packages

  if ! bt_plugin_present; then
    need_bt_plugin_pkg=1
  fi

  if ! python3 -m venv --help >/dev/null 2>&1; then
    need_venv_pkg=1
  fi

  if [[ ${#missing_packages[@]} -eq 0 && ${#missing_support[@]} -eq 0 && ${#missing_audio_stack[@]} -eq 0 && $need_venv_pkg -eq 0 && $need_bt_plugin_pkg -eq 0 ]]; then
    pass "native packages already available"
    return
  fi

  if [[ ${#missing_packages[@]} -gt 0 ]]; then
    pkg_install "${core_packages[@]}"
  fi
  if [[ ${#missing_support[@]} -gt 0 ]]; then
    pkg_install "${support_packages[@]}"
  fi
  if [[ ${#missing_audio_stack[@]} -gt 0 || $need_bt_plugin_pkg -eq 1 ]]; then
    pkg_install "${audio_stack_packages[@]}"
  fi
  ensure_debian_pipewire_backports

  if [[ $need_venv_pkg -eq 1 ]] && ! python3 -m venv --help >/dev/null 2>&1; then
    case "$PACKAGE_MANAGER" in
      dnf)
        pkg_install python3-virtualenv
        ;;
      zypper)
        pkg_install "$zypper_venv_package"
        ;;
      pacman)
        # python on Arch/Manjaro ships the venv module; no extra package needed.
        ;;
      *)
        die "python3 venv support is missing after package install"
        ;;
    esac
  fi

  for cmd in python3 mpv ffmpeg playerctl curl git socat tar bluetoothctl wpctl pw-cli pactl; do
    command -v "$cmd" >/dev/null 2>&1 || die "Expected command missing after package install: $cmd"
  done
  if ! bt_plugin_present; then
    warn "PipeWire BlueZ SPA plugin is still missing; Bluetooth input mode will not be available until the host provides libspa-bluez5.so"
  fi
  pass "native packages installed"
}

ensure_dbus_send_binary() {
  # dbus-send lives in different binary packages per distro:
  #   apt (Debian/Ubuntu/Armbian): dbus-bin (dbus-send was split out of dbus)
  #   dnf (Fedora/RHEL):           dbus-tools (split when dbus-broker took over)
  #   zypper (openSUSE Tumbleweed): dbus-1-tools
  #   pacman (Arch/Manjaro):       dbus (still a single package)
  # Plain "dbus" is wrong on Fedora/openSUSE and only coincidentally works on
  # Debian/Arch.  Install exactly the package that ships dbus-send and
  # nothing larger so the host's dbus daemon choice stays untouched.
  local pkg=""
  local dbus_send_pkg=""

  command -v dbus-send >/dev/null 2>&1 && return 0

  case "$PACKAGE_MANAGER" in
    apt)       dbus_send_pkg="dbus-bin" ;;
    dnf)       dbus_send_pkg="dbus-tools" ;;
    zypper)    dbus_send_pkg="dbus-1-tools" ;;
    pacman)    dbus_send_pkg="dbus" ;;
    *)
      warn "dbus-send is missing and the package manager is unknown; suspend/shutdown actions will be unavailable"
      return 0
      ;;
  esac

  if package_installed "$dbus_send_pkg"; then
    log "Found dbus-send package: $dbus_send_pkg"
    return 0
  fi

  pkg="$dbus_send_pkg"
  log "installing dbus-send (from package '$pkg', distro: $PACKAGE_MANAGER)"
  if pkg_install "$pkg"; then
    command -v dbus-send >/dev/null 2>&1 && pass "dbus-send available via $pkg" \
      || warn "Package '$pkg' installed but dbus-send binary is still missing"
  else
    warn "Could not install '$pkg' for dbus-send; suspend/shutdown actions will be unavailable"
  fi
}

ensure_firewall_cmd_binary() {
  # firewall-cmd ships in the firewalld package on every supported distro,
  # but only Fedora/RHEL, openSUSE and Arch/Manjaro treat firewalld as their
  # regular firewall stack.  On Debian/Ubuntu the installer must follow the
  # stack that is actually active: with UFW running the UFW path is used,
  # and a host without an active supported firewall must not gain a
  # firewalld stack (and its enabled unit) solely for FXRoute.
  local pkg=""
  local firewall_pkg=""

  command -v firewall-cmd >/dev/null 2>&1 && return 0

  case "$PACKAGE_MANAGER" in
    dnf|zypper|pacman) firewall_pkg="firewalld" ;;
    apt)
      if ufw_is_active; then
        log "firewall-cmd is missing and UFW is active; FXRoute firewall rules will use UFW"
        return 0
      fi
      warn "firewall-cmd is missing and no active UFW/firewalld stack was detected; FXRoute will skip firewall configuration"
      return 0
      ;;
    *)
      warn "firewall-cmd is missing and the package manager is unknown; FXRoute will skip firewall configuration"
      return 0
      ;;
  esac

  if package_installed "$firewall_pkg"; then
    log "Found firewall-cmd package: $firewall_pkg"
    return 0
  fi

  pkg="$firewall_pkg"
  log "installing firewall-cmd (from package '$pkg', distro: $PACKAGE_MANAGER)"
  if pkg_install "$pkg"; then
    command -v firewall-cmd >/dev/null 2>&1 && pass "firewall-cmd available via $pkg" \
      || warn "Package '$pkg' installed but firewall-cmd binary is still missing"
  else
    warn "Could not install '$pkg' for firewall-cmd; FXRoute will skip firewall configuration"
  fi
}

install_network_library_helper() {
  local helper_src="$INSTALL_ROOT/scripts/fxroute-cifs-mount"
  local helper_path="/usr/local/sbin/fxroute-cifs-mount"
  local sudoers_path="/etc/sudoers.d/fxroute-cifs-mount"
  local tmp_sudoers=""
  local existing_sudoers=""
  local sudoers_rule=""
  local helper_sha256=""
  local tmp_helper=""
  local existing_helper_sha256=""
  local sudoers_rule_present=0
  local install_helper=0

  [[ -f "$helper_src" && ! -L "$helper_src" ]] || die "Missing network library mount helper: $helper_src"
  helper_sha256="$(sha256sum "$helper_src" | awk '{print $1}')"
  [[ "$helper_sha256" == "$CIFS_HELPER_SHA256" ]] \
    || die "Refusing to install an unverified network library mount helper"
  if [[ -e "$helper_path" || -L "$helper_path" ]]; then
    [[ -f "$helper_path" && ! -L "$helper_path" ]] \
      || die "Refusing to overwrite a non-regular network library mount helper"
    existing_helper_sha256="$("${SUDO_CMD[@]}" sha256sum "$helper_path" | awk '{print $1}')"
    [[ "$existing_helper_sha256" == "$CIFS_HELPER_SHA256" || "$existing_helper_sha256" == "$CIFS_HELPER_LEGACY_SHA256" ]] \
      || die "Refusing to overwrite a non-FXRoute-owned network library mount helper"
    [[ "$existing_helper_sha256" == "$CIFS_HELPER_SHA256" ]] || install_helper=1
  else
    install_helper=1
  fi
  if [[ $install_helper -eq 1 ]]; then
    tmp_helper="$(mktemp)"
    if ! "${SUDO_CMD[@]}" install -m 600 "$helper_src" "$tmp_helper" \
      || [[ "$("${SUDO_CMD[@]}" sha256sum "$tmp_helper" | awk '{print $1}')" != "$CIFS_HELPER_SHA256" ]]; then
      "${SUDO_CMD[@]}" rm -f "$tmp_helper"
      die "Refusing to install a changed network library mount helper"
    fi
    if ! "${SUDO_CMD[@]}" install -m 755 "$tmp_helper" "$helper_path"; then
      "${SUDO_CMD[@]}" rm -f "$tmp_helper"
      die "Could not install the network library mount helper"
    fi
    "${SUDO_CMD[@]}" rm -f "$tmp_helper"
    CIFS_HELPER_INSTALLED_BY_FXROUTE=1
  fi
  [[ "$("${SUDO_CMD[@]}" sha256sum "$helper_path" | awk '{print $1}')" == "$CIFS_HELPER_SHA256" ]] \
    || die "Installed network library mount helper failed verification"
  tmp_sudoers="$(mktemp)"
  local install_user="$FXROUTE_TARGET_USER"
  [[ "$install_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "Invalid install user for CIFS helper"
  sudoers_rule="$install_user ALL=(root) NOPASSWD: $helper_path *"
  if [[ -L "$sudoers_path" || ( -e "$sudoers_path" && ! -f "$sudoers_path" ) ]]; then
    rm -f "$tmp_sudoers"
    die "Refusing to overwrite a non-regular FXRoute CIFS sudoers file"
  fi
  existing_sudoers="$("${SUDO_CMD[@]}" cat "$sudoers_path" 2>/dev/null || true)"
  if [[ -e "$sudoers_path" && "$CIFS_SUDOERS_RULE_INSTALLED_BY_FXROUTE" -eq 1 \
    && -n "$CIFS_SUDOERS_SHA256" \
    && "$("${SUDO_CMD[@]}" sha256sum "$sudoers_path" | awk '{print $1}')" != "$CIFS_SUDOERS_SHA256" ]]; then
    rm -f "$tmp_sudoers"
    die "Refusing to overwrite a changed FXRoute CIFS sudoers file"
  fi
  if grep -Fqx -- "$sudoers_rule" <<<"$existing_sudoers"; then
    sudoers_rule_present=1
  fi
  if [[ -n "$existing_sudoers" ]]; then
    printf '%s\n' "$existing_sudoers" > "$tmp_sudoers"
  fi
  if [[ $sudoers_rule_present -eq 0 ]]; then
    printf '%s\n' "$sudoers_rule" >> "$tmp_sudoers"
    CIFS_SUDOERS_RULE_INSTALLED_BY_FXROUTE=1
  fi
  if command -v visudo >/dev/null 2>&1; then
    "${SUDO_CMD[@]}" visudo -cf "$tmp_sudoers" >/dev/null
  fi
  "${SUDO_CMD[@]}" install -m 440 "$tmp_sudoers" "$sudoers_path"
  rm -f "$tmp_sudoers"
  CIFS_SUDOERS_SHA256="$("${SUDO_CMD[@]}" sha256sum "$sudoers_path" | awk '{print $1}')"
  pass "network library CIFS helper installed"
}

prepare_external_install_root() {
  local path="$1"
  local parent_path=""
  local parent_mode=""

  [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]] || {
    run_as_target_user mkdir -p "$path"
    return 0
  }
  parent_path="$(dirname "$path")"
  while [[ "$parent_path" != "/" ]]; do
    [[ -d "$parent_path" && ! -L "$parent_path" ]] \
      || die "External FXRoute target parent is not a trusted directory: $parent_path"
    [[ "$(stat -c '%u' "$parent_path" 2>/dev/null || true)" == "0" ]] \
      || die "External FXRoute target parent is not root-owned: $parent_path"
    parent_mode="$(stat -c '%A' "$parent_path" 2>/dev/null || true)"
    [[ "${parent_mode:5:1}" != "w" && "${parent_mode:8:1}" != "w" ]] \
      || die "External FXRoute target parent is writable by a non-root user: $parent_path"
    parent_path="$(dirname "$parent_path")"
  done
  [[ ! -L "$path" ]] || die "Refusing to use a symlink as the external FXRoute target: $path"
  if [[ -e "$path" ]]; then
    [[ -d "$path" ]] || die "External FXRoute target is not a directory: $path"
    [[ "$(stat -c '%u' "$path" 2>/dev/null || true)" == "$FXROUTE_TARGET_UID" ]] \
      || die "External FXRoute target is not owned by $FXROUTE_TARGET_USER: $path"
  else
    install -d -o "$FXROUTE_TARGET_USER" -g "$FXROUTE_TARGET_GROUP" -m 755 "$path" \
      || die "Could not create external FXRoute target $path"
  fi
}

sync_project_tree() {
  local archive=""

  if [[ $LOCAL_PROJECT_MODE -eq 1 ]]; then
    log "Using local project install mode at $INSTALL_ROOT"
    pass "project install mode: local-project"
    return
  fi

  if [[ "$INSTALL_ROOT" == "$HOME"/* ]]; then
    run_as_target_user mkdir -p "$INSTALL_ROOT"
  else
    prepare_external_install_root "$INSTALL_ROOT"
  fi

  log "Syncing project into $INSTALL_ROOT"
  archive="$(mktemp /tmp/fxroute-project-sync.XXXXXX)"
  trap 'rm -f "${archive:-}"' RETURN
  tar \
    --exclude='.venv' \
    --exclude='.env' \
    --exclude='__pycache__' \
    --exclude='backups' \
    --exclude='*.pyc' \
    --exclude='outputs/*.patch' \
    -C "$SOURCE_DIR" -cf "$archive" .
  chmod 644 "$archive"
  run_as_target_user tar -C "$INSTALL_ROOT" -xf "$archive"
  rm -f "$archive"
  trap - RETURN
  ensure_target_user_ownership
  cleanup_obsolete_root_modules
  pass "project synced to target directory"
}

cleanup_obsolete_root_modules() {
  local helper="$INSTALL_ROOT/scripts/fxroute_obsolete_root_cleanup.py"
  [[ -f "$helper" ]] || {
    log "Obsolete root-module cleanup helper not found; skipping."
    return 0
  }
  if run_as_target_user python3 "$helper" --root "$INSTALL_ROOT"; then
    log "Obsolete pre-package root modules removed."
  else
    warn "Obsolete root-module cleanup reported an error."
  fi
}

pick_port() {
  local preferred="$1"
  python3 - <<'PY' "$preferred"
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 20):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    print(start)
PY
}

read_env_value() {
  local key="$1"
  local env_file="${2:-$INSTALL_ROOT/.env}"
  [[ -f "$env_file" ]] || return 0
  awk -F= -v wanted="$key" '
    $0 !~ /^[[:space:]]*#/ && $1 == wanted {
      sub(/^[^=]*=/, "", $0)
      print $0
      exit
    }
  ' "$env_file"
}

expand_config_path() {
  python3 - <<'PY' "$1" "${2:-$PWD}"
import os
import sys

value = sys.argv[1].strip()
base = sys.argv[2]
if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
    value = value[1:-1]
if not value:
    raise SystemExit(1)
value = os.path.expandvars(os.path.expanduser(value))
if not os.path.isabs(value):
    value = os.path.join(base, value)
print(os.path.realpath(os.path.abspath(value)))
PY
}

normalize_downloads_subdir() {
  python3 - <<'PY' "$1"
import sys

value = sys.argv[1].strip()
if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
    value = value[1:-1]
parts = value.split("/")
if not value or value.startswith("/") or any(part in ("", ".", "..") for part in parts):
    raise SystemExit(1)
print("/".join(parts))
PY
}

validate_download_path() {
  local path="$1"
  local music_root="${2:-}"

  [[ "$path" == /* && "$path" != "/" ]] \
    || die "FXRoute download directory must be an absolute path below a dedicated music root: $path"
  [[ -n "$music_root" && "$music_root" != "/" && "$path" == "$music_root"/* ]] \
    || die "FXRoute download directory escapes MUSIC_ROOT: $path"
  case "$path" in
    /bin|/bin/*|/boot|/boot/*|/dev|/dev/*|/etc|/etc/*|/lib|/lib/*|/lib64|/lib64/*|/proc|/proc/*|/run|/run/*|/sbin|/sbin/*|/sys|/sys/*|/tmp|/tmp/*|/usr|/usr/*)
      die "Refusing to change ownership of system path configured as FXRoute download directory: $path"
      ;;
  esac
  [[ ! -L "$path" ]] \
    || die "FXRoute download directory must not be a symlink: $path"
}

configured_dsp_binary() {
  local configured=""
  configured="$(read_env_value FXROUTE_DSP_BINARY "$INSTALL_ROOT/.env" 2>/dev/null || true)"
  configured="${configured//\"/}"
  configured="${configured//\'/}"
  [[ -n "$configured" ]] || configured="$INSTALL_ROOT/native_dsp/build/fxroute-dsp"
  printf '%s\n' "$configured"
}

env_setting_enabled() {
  local normalized="${1,,}"
  case "$normalized" in
    1|true|yes|on|enabled)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

env_interval_hours_or_default() {
  local raw_value="$1"
  local default_value="$2"
  if [[ "$raw_value" =~ ^[1-9][0-9]*$ ]]; then
    printf '%s\n' "$raw_value"
  else
    printf '%s\n' "$default_value"
  fi
}

create_env_if_missing() {
  local env_file="$INSTALL_ROOT/.env"
  local env_example="$INSTALL_ROOT/.env.example"
  local music_root="$HOME/Music"
  local downloads_dir="incoming"
  local download_path=""
  local log_level="INFO"
  local host="0.0.0.0"
  local port="8000"
  local max_downloads="1"
  local spotify_autostart="on"
  if [[ "$(uname -m)" != "x86_64" ]]; then
    spotify_autostart="off"
  fi

  if [[ -f "$env_file" ]]; then
    log "Keeping existing .env"
    music_root="$(read_env_value MUSIC_ROOT "$env_file" || true)"
    downloads_dir="$(read_env_value DOWNLOADS_SUBDIR "$env_file" || true)"
    [[ -n "$music_root" ]] || music_root="$HOME/Music"
    [[ -n "$downloads_dir" ]] || downloads_dir="incoming"
    music_root="$(expand_config_path "$music_root" "$INSTALL_ROOT")" \
      || die "MUSIC_ROOT in $env_file is not a valid path"
    downloads_dir="$(normalize_downloads_subdir "$downloads_dir")" \
      || die "DOWNLOADS_SUBDIR in $env_file must be a relative path without '.' or '..' components"
    download_path="$(expand_config_path "$music_root/$downloads_dir" "$INSTALL_ROOT")" \
      || die "The configured FXRoute download directory is not a valid path"
    validate_download_path "$download_path" "$music_root"
    run_as_target_user mkdir -p "$download_path"
    pass ".env preserved"
    return
  fi

  [[ -f "$env_example" ]] || die "Missing .env.example in install root"

  local chosen_port
  chosen_port="$(pick_port "$port")"
  if [[ "$chosen_port" != "$port" ]]; then
    warn "Port $port is busy, defaulting .env to $chosen_port"
    port="$chosen_port"
  fi

  music_root="$(expand_config_path "$music_root" "$INSTALL_ROOT")" \
    || die "Default MUSIC_ROOT is not a valid path"
  downloads_dir="$(normalize_downloads_subdir "$downloads_dir")" \
    || die "Default DOWNLOADS_SUBDIR is not a valid relative path"
  download_path="$(expand_config_path "$music_root/$downloads_dir" "$INSTALL_ROOT")" \
    || die "The default FXRoute download directory is not a valid path"
  validate_download_path "$download_path" "$music_root"

  run_as_target_user tee "$env_file" >/dev/null <<EOF
MUSIC_ROOT=$music_root
DOWNLOADS_SUBDIR=$downloads_dir
LOG_LEVEL=$log_level
HOST=$host
PORT=$port
MAX_DOWNLOADS=$max_downloads
SPOTIFY_AUTOSTART=$spotify_autostart
SPOTIFY_CACHE_CLEANUP=off
SPOTIFY_CACHE_CLEANUP_INTERVAL_HOURS=24
SYSTEM_AUTO_UPDATE=off
SYSTEM_AUTO_UPDATE_INTERVAL_HOURS=24
EOF

  run_as_target_user mkdir -p "$download_path"
  pass ".env created"
}

flatpak_app_installed() {
  local app_id="$1"

  command -v flatpak >/dev/null 2>&1 || return 1

  if run_as_target_user flatpak list --app --user --columns=application 2>/dev/null | grep -Fxq "$app_id"; then
    return 0
  fi

  if run_as_target_user flatpak list --app --system --columns=application 2>/dev/null | grep -Fxq "$app_id"; then
    return 0
  fi

  return 1
}

install_missing_provider_packages() {
  local package_text="$1"
  local packages=()
  local missing=()
  local package=""

  read -r -a packages <<<"$package_text"
  for package in "${packages[@]}"; do
    package_installed "$package" || missing+=("$package")
  done
  [[ ${#missing[@]} -eq 0 ]] || pkg_install "${missing[@]}"
}

spotify_keyring_packages_for_manager() {
  case "$1" in
    apt) echo "gnome-keyring libsecret-1-0 libpam-gnome-keyring" ;;
    dnf|pacman) echo "gnome-keyring libsecret" ;;
    zypper) echo "gnome-keyring libsecret-1-0" ;;
    *) return 1 ;;
  esac
}

spotify_desktop_install_method() {
  if command -v spotify >/dev/null 2>&1; then
    printf 'native\n'
    return 0
  fi
  if flatpak_app_installed "com.spotify.Client"; then
    printf 'flatpak\n'
    return 0
  fi
  return 1
}

spotify_desktop_installed_version() {
  case "${1:-}" in
    native)
      dpkg-query -W -f='${Version}' spotify-client 2>/dev/null || true
      ;;
    flatpak)
      flatpak info --system --show=version com.spotify.Client 2>/dev/null || true
      ;;
  esac
}

install_spotify_desktop_apt() {
  local existing_source=0
  local tmp_source=""
  local tmp_key=""
  local key_fingerprint=""
  local key_present=0

  if grep -Rqs "repository\.spotify\.com" /etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
    existing_source=1
  fi

  if [[ $existing_source -eq 0 && $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: source + fingerprint-pinned keyring are
    # written atomically by the root-owned helper (action spotify-apt-repo),
    # so no repo line or key bytes cross the sudo boundary here.
    require_cmd gpg
    log "Installing official Spotify apt repository (provider helper)"
    if ! provider_privileged spotify-apt-repo; then
      return 1
    fi
    SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE=1
    SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1
    SPOTIFY_DESKTOP_KEY_FINGERPRINT="$SPOTIFY_APT_KEY_FINGERPRINT"
    if [[ -f "$SPOTIFY_APT_SOURCE_FILE" ]]; then
      SPOTIFY_DESKTOP_REPO_SHA256="$(sha256sum "$SPOTIFY_APT_SOURCE_FILE" | awk '{print $1}')"
    fi
    # The source was added after the normal package refresh; the helper
    # refreshed apt itself after writing the source, so mark the refresh
    # done to avoid a second non-interactive update before spotify-client.
    PKG_REFRESH_DONE=1
  fi

  if [[ $existing_source -eq 0 && $PROVIDERS_ONLY_MODE -eq 0 ]]; then
    require_cmd gpg
    tmp_source="$(mktemp)"
    printf '%s\n' \
      "deb [arch=amd64 signed-by=${SPOTIFY_APT_KEY_FILE}] https://repository.spotify.com stable non-free" \
      > "$tmp_source"
    if ! printf '%s\n' "Installing official Spotify apt repository"; then
      rm -f "$tmp_source"
      return 1
    fi
    [[ -e "$SPOTIFY_APT_KEY_FILE" ]] && key_present=1
    if [[ $key_present -eq 1 ]]; then
      key_fingerprint="$(gpg --show-keys --with-colons --fingerprint "$SPOTIFY_APT_KEY_FILE" 2>/dev/null | awk -F: '$1 == "fpr" {print toupper($10); exit}')"
    else
      tmp_key="$(mktemp)"
      if ! curl -fsSL -o "$tmp_key" "$SPOTIFY_APT_KEY_URL"; then
        rm -f "$tmp_source" "$tmp_key"
        return 1
      fi
      key_fingerprint="$(gpg --show-keys --with-colons --fingerprint "$tmp_key" 2>/dev/null | awk -F: '$1 == "fpr" {print toupper($10); exit}')"
    fi
    if [[ "$key_fingerprint" != "$SPOTIFY_APT_KEY_FINGERPRINT" ]]; then
      warn "Spotify apt signing key fingerprint mismatch; refusing to install the repository"
      rm -f "$tmp_source" "$tmp_key"
      return 1
    fi
    if [[ $key_present -eq 0 ]]; then
      SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE=1
      if ! gpg --dearmor < "$tmp_key" | "${SUDO_CMD[@]}" tee "$SPOTIFY_APT_KEY_FILE" >/dev/null; then
        rm -f "$tmp_source" "$tmp_key"
        return 1
      fi
      SPOTIFY_DESKTOP_KEY_FINGERPRINT="$key_fingerprint"
    fi
    rm -f "$tmp_key"
    SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1
    if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_source" "$SPOTIFY_APT_SOURCE_FILE"; then
      rm -f "$tmp_source"
      return 1
    fi
    SPOTIFY_DESKTOP_REPO_SHA256="$(sha256sum "$SPOTIFY_APT_SOURCE_FILE" | awk '{print $1}')"
    rm -f "$tmp_source"
    # The source was added after the normal package refresh; refresh it once
    # before asking the shared package installer for spotify-client.
    PKG_REFRESH_DONE=0
  fi

  pkg_install spotify-client
  command -v spotify >/dev/null 2>&1
}

install_spotify_desktop_flatpak() {
  local remote_exists=0

  if ! command -v flatpak >/dev/null 2>&1; then
    install_missing_provider_packages "flatpak"
  fi
  require_cmd flatpak

  if flatpak remotes --system --columns=name 2>/dev/null | grep -Fxq flathub; then
    remote_exists=1
  fi
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: no system flatpak mutations without a
    # TTY. The x86_64 desktop case is covered by the full installer; the
    # Settings UI reports this honestly instead of failing on sudo.
    die "Spotify Desktop Flatpak setup needs the full installer on this host; rerun install.sh interactively"
  fi
  if [[ $remote_exists -eq 0 ]]; then
    run_cmd "${SUDO_CMD[@]}" flatpak remote-add --if-not-exists --system flathub https://flathub.org/repo/flathub.flatpakrepo
  fi
  run_cmd "${SUDO_CMD[@]}" flatpak install --system --assumeyes flathub com.spotify.Client
  flatpak_app_installed "com.spotify.Client"
}

install_spotify_desktop() {
  local method=""

  if ! spotify_desktop_supported; then
    SPOTIFY_DESKTOP_PROVIDER_STATUS="not supported on this architecture/session"
    warn "Spotify Desktop is offered only on x86_64 desktop sessions; skipping it here"
    return 0
  fi

  install_missing_provider_packages "$(spotify_keyring_packages_for_manager "$PACKAGE_MANAGER")"
  method="$(spotify_desktop_install_method || true)"
  if [[ -n "$method" ]]; then
    SPOTIFY_DESKTOP_PRESENT_BEFORE=1
    SPOTIFY_DESKTOP_AVAILABLE=1
    SPOTIFY_DESKTOP_INSTALL_METHOD="$method"
    if [[ -z "$SPOTIFY_DESKTOP_INSTALLED_VERSION" && $SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE -eq 0 ]]; then
      SPOTIFY_DESKTOP_INSTALLED_VERSION="$(spotify_desktop_installed_version "$method")"
    fi
    SPOTIFY_DESKTOP_PROVIDER_STATUS="already present"
    pass "Spotify Desktop already present ($method); not reinstalling"
    return 0
  fi

  if [[ "$PACKAGE_MANAGER" == "apt" ]]; then
    install_missing_provider_packages "ca-certificates gnupg"
    SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE=1
    SPOTIFY_DESKTOP_INSTALL_METHOD="native"
    if ! install_spotify_desktop_apt; then
      die "Spotify Desktop installation through the official apt repository failed"
    fi
    SPOTIFY_DESKTOP_INSTALLED_VERSION="$(spotify_desktop_installed_version native)"
  else
    SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE=1
    SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE=1
    SPOTIFY_DESKTOP_INSTALL_METHOD="flatpak"
    if ! install_spotify_desktop_flatpak; then
      die "Spotify Desktop installation through Flatpak failed"
    fi
    SPOTIFY_DESKTOP_INSTALLED_VERSION="$(spotify_desktop_installed_version flatpak)"
  fi
  if [[ -n "$(spotify_desktop_install_method || true)" ]]; then
    SPOTIFY_DESKTOP_AVAILABLE=1
  fi
  SPOTIFY_DESKTOP_PROVIDER_STATUS="installed by FXRoute"
  pass "Spotify Desktop installed ($SPOTIFY_DESKTOP_INSTALL_METHOD)"
}

detect_existing_provider_components() {
  local method=""

  method="$(spotify_desktop_install_method || true)"
  if [[ -n "$method" ]]; then
    SPOTIFY_DESKTOP_PRESENT_BEFORE=1
    SPOTIFY_DESKTOP_AVAILABLE=1
    SPOTIFY_DESKTOP_INSTALL_METHOD="$method"
    if [[ $SELECT_SPOTIFY_DESKTOP -eq 0 ]]; then
      SPOTIFY_DESKTOP_PROVIDER_STATUS="already present (not selected; preserved)"
      pass "Spotify Desktop already present ($method); not selected, preserving it"
    fi
  elif [[ $SELECT_SPOTIFY_DESKTOP -eq 0 ]]; then
    SPOTIFY_DESKTOP_PROVIDER_STATUS="not selected; not installed"
  fi

  if [[ -n "$(spotifyd_binary_path || true)" ]]; then
    SPOTIFYD_PRESENT_BEFORE=1
    if [[ $SELECT_SPOTIFYD -eq 0 ]]; then
      SPOTIFYD_PROVIDER_STATUS="already present (not selected; preserved)"
      pass "spotifyd already present; not selected, preserving it"
    fi
  elif [[ $SELECT_SPOTIFYD -eq 0 ]]; then
    SPOTIFYD_PROVIDER_STATUS="not selected; not installed"
  fi
  if user_unit_exists spotifyd.service; then
    SPOTIFYD_PRESENT_BEFORE=1
  fi

  if [[ -n "$(qbzd_binary_path || true)" ]]; then
    QBZD_PRESENT_BEFORE=1
    if [[ $SELECT_QOBUZ -eq 0 ]]; then
      QOBUZ_PROVIDER_STATUS="already present (not selected; preserved)"
      pass "qbzd already present; not selected, preserving it"
    fi
  elif [[ $SELECT_QOBUZ -eq 0 ]]; then
    QOBUZ_PROVIDER_STATUS="not selected; not installed"
  fi
  if user_unit_exists qbzd.service; then
    QBZD_PRESENT_BEFORE=1
  fi
}

spotifyd_binary_path() {
  if [[ -e "$HOME/.local/bin/spotifyd" || -L "$HOME/.local/bin/spotifyd" ]]; then
    printf '%s\n' "$HOME/.local/bin/spotifyd"
    return 0
  fi
  command -v spotifyd 2>/dev/null || true
}

fxroute_spotify_humanize_label() {
  # Turn a hostname fragment into a short display label (Title Case).
  local part="${1:-}" norm="" word="" lower="" title="" out=""
  norm="$(printf '%s' "$part" | tr 'A-Z' 'a-z' | sed -e 's/[-_]/ /g; s/  */ /g; s/^ //; s/ $//')"
  [[ -n "$norm" ]] || return 1
  for word in $norm; do
    lower="${word,,}"
    title="${lower^}"
    if [[ -n "$out" ]]; then
      out+=" $title"
    else
      out="$title"
    fi
  done
  out="${out:0:20}"
  out="${out%"${out##*[![:space:]]}"}"
  [[ -n "$out" ]] || return 1
  printf '%s\n' "$out"
}

fxroute_spotify_persistent_suffix() {
  # Print the stable 4-char device suffix, creating it once when needed.
  local file="${1:-$HOME/.config/fxroute/device-suffix}" cur=""
  if [[ -f "$file" ]]; then
    cur="$(tr -d '[:space:]' <"$file" 2>/dev/null | tr 'a-z' 'A-Z')"
    if [[ "$cur" =~ ^[0-9A-F]{4}$ ]]; then
      printf '%s\n' "$cur"
      return 0
    fi
  fi
  if command -v od >/dev/null 2>&1; then
    cur="$(od -An -tx1 -N2 /dev/urandom 2>/dev/null | tr -d ' \n' | tr 'a-z' 'A-Z')"
  fi
  if [[ ! "$cur" =~ ^[0-9A-F]{4}$ ]]; then
    cur="$(printf '%04X' $(( (RANDOM << 15 | RANDOM) % 65536 )))"
  fi
  run_as_target_user mkdir -p "$(dirname "$file")"
  printf '%s\n' "$cur" | run_as_target_user tee "$file" >/dev/null
  printf '%s\n' "$cur"
}

fxroute_spotify_connect_name() {
  # Short unique Spotify Connect name from hostname/machine-id (read-only).
  # Never touches hostname, Avahi, Caddy, or DNS; never prints .local.
  local host="${1:-$(hostname 2>/dev/null || true)}"
  local mid="${2:-$(cat /etc/machine-id 2>/dev/null || true)}"
  local suffix_file="${3:-$HOME/.config/fxroute/device-suffix}"
  local rest="" label="" clean="" suffix=""
  host="$(printf '%s' "$host" | tr 'A-Z' 'a-z' | sed -e 's/^[[:space:]]*//; s/[[:space:]]*$//; s/\.local$//; s/\.*$//')"
  host="${host%%.*}"
  if [[ -n "$host" && "$host" != localhost && "$host" != fxroute ]]; then
    if [[ "$host" =~ ^fxroute-([0-9a-f]{6})$ ]]; then
      suffix="${BASH_REMATCH[1]:0:4}"
      printf 'FXRoute %s\n' "${suffix^^}"
      return 0
    fi
    rest="$host"
    if [[ "$host" == fxroute-* ]]; then
      rest="${host#fxroute-}"
    elif [[ "$host" == fxroute* ]]; then
      rest="${host#fxroute}"
    fi
    rest="${rest##[-_]}"
    label="$(fxroute_spotify_humanize_label "$rest" || true)"
    if [[ -n "$label" ]]; then
      printf 'FXRoute %s\n' "$label"
      return 0
    fi
  fi
  clean="$(printf '%s' "$mid" | tr 'A-Z' 'a-z' | tr -cd '0-9a-f')"
  if [[ ${#clean} -ge 4 ]]; then
    suffix="${clean:0:4}"
    printf 'FXRoute %s\n' "${suffix^^}"
    return 0
  fi
  printf 'FXRoute %s\n' "$(fxroute_spotify_persistent_suffix "$suffix_file")"
}

sync_spotifyd_device_name() {
  # Align an FXRoute-managed spotifyd device_name with the Connect name.
  # Custom names (anything not starting with FXRoute) are never rewritten.
  local config_path="${1:-$HOME/.config/spotifyd/spotifyd.conf}"
  local desired="" current=""
  desired="$(fxroute_spotify_connect_name 2>/dev/null || true)"
  [[ -n "$desired" ]] || return 0
  SPOTIFYD_CONNECT_NAME="$desired"
  [[ -f "$config_path" ]] || return 0
  current="$(sed -n -E 's/^[[:space:]]*device_name[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']*)["'"'"'].*/\1/p' "$config_path" | head -n 1)"
  if [[ -z "$current" ]]; then
    if grep -q -E '^[[:space:]]*\[global\][[:space:]]*$' "$config_path"; then
      run_as_target_user sed -i -E '/^[[:space:]]*\[global\][[:space:]]*$/a device_name = "'"$desired"'"' "$config_path"
    else
      printf '\n[global]\ndevice_name = "%s"\n' "$desired" | run_as_target_user tee -a "$config_path" >/dev/null
    fi
    SPOTIFYD_DEVICE_NAME_CHANGED=1
    pass "spotifyd Connect name set to ${desired}"
    return 0
  fi
  if [[ "$current" != "FXRoute" && "$current" != "FXRoute "* ]]; then
    return 0
  fi
  if [[ "$current" == "$desired" ]]; then
    return 0
  fi
  run_as_target_user sed -i -E 's/^([[:space:]]*device_name[[:space:]]*=[[:space:]]*)["'"'"'].*["'"'"'](.*)$/\1"'"$desired"'"\2/' "$config_path"
  SPOTIFYD_DEVICE_NAME_CHANGED=1
  pass "spotifyd Connect name updated to ${desired}"
}

sync_spotifyd_dsp_sink() {
  # Route spotifyd into the FXRoute DSP graph. Without an explicit device
  # spotifyd renders to the PulseAudio default sink (hardware), bypassing
  # DSP, ownership claims, peak monitoring and the remote volume bridge.
  # A present device line is never rewritten.
  local config_path="${1:-$HOME/.config/spotifyd/spotifyd.conf}"
  local current=""

  [[ -f "$config_path" ]] || return 0
  current="$(sed -n -E 's/^[[:space:]]*device[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']*)["'"'"'].*/\1/p' "$config_path" | head -n 1)"
  if [[ -n "$current" ]]; then
    return 0
  fi
  if grep -q -E '^[[:space:]]*\[global\][[:space:]]*$' "$config_path"; then
    run_as_target_user sed -i -E '/^[[:space:]]*\[global\][[:space:]]*$/a device = "fxroute_dsp_sink"' "$config_path"
  else
    printf '\n[global]\ndevice = "fxroute_dsp_sink"\n' | run_as_target_user tee -a "$config_path" >/dev/null
  fi
  SPOTIFYD_DSP_SINK_ROUTED=1
  pass "spotifyd output routed to the FXRoute DSP sink"
}

write_spotifyd_config() {
  local config_dir="$HOME/.config/spotifyd"
  local config_path="$config_dir/spotifyd.conf"
  local connect_name=""

  SPOTIFYD_CONFIG_PATH="$config_path"
  run_as_target_user mkdir -p "$config_dir"
  if [[ -f "$config_path" ]]; then
    sync_spotifyd_device_name "$config_path"
    sync_spotifyd_dsp_sink "$config_path"
    pass "spotifyd config preserved; FXRoute service pins Zeroconf port ${SPOTIFYD_ZEROCONF_PORT}"
    return 0
  fi

  connect_name="$(fxroute_spotify_connect_name 2>/dev/null || true)"
  [[ -n "$connect_name" ]] || connect_name="FXRoute 0000"
  SPOTIFYD_CONNECT_NAME="$connect_name"
  run_as_target_user tee "$config_path" >/dev/null <<EOF
[global]
device_name = "${connect_name}"
device_type = "speaker"
backend = "pulseaudio"
# Render into the FXRoute DSP graph (ownership claims, peak monitoring and
# the remote volume bridge all observe this sink); never hardware-direct.
device = "fxroute_dsp_sink"
use_mpris = true
dbus_type = "session"
# Remote Connect volume is routed to the FXRoute master by the spotifyd
# volume watch; the source itself must never attenuate (unity contract).
volume_controller = "none"
zeroconf_port = ${SPOTIFYD_ZEROCONF_PORT}
EOF
  run_as_target_user chmod 600 "$config_path"
  SPOTIFYD_CONFIG_INSTALLED_BY_FXROUTE=1
  pass "spotifyd config created for FXRoute/PipeWire-Pulse (${connect_name})"
}

spotifyd_runtime_missing_libraries() {
  local binary="${1:-}"
  local ldd_output=""

  [[ -x "$binary" ]] || return 0
  command -v ldd >/dev/null 2>&1 || return 0
  ldd_output="$(run_as_target_user ldd "$binary" 2>&1)" || return 1
  awk '
    /not found$/ { print $1; next }
    /version .* not found/ || /not a dynamic executable/ || /wrong ELF class/ { print }
  ' <<<"$ldd_output"
}

install_spotifyd_binary() {
  local release_arch=""
  local archive=""
  local archive_url=""
  local checksum=""
  local checksum_algorithm=""
  local work=""
  local extracted=""
  local existing_path=""
  local current_sha256=""
  local destination="$HOME/.local/bin/spotifyd"
  local staged_binary=""

  existing_path="$(spotifyd_binary_path || true)"
  if [[ $SPOTIFYD_INSTALLED_BY_FXROUTE -eq 1 ]]; then
    if [[ -z "$SPOTIFYD_BINARY_PATH" || "$existing_path" != "$SPOTIFYD_BINARY_PATH" ]]; then
      SPOTIFYD_BINARY_IDENTITY_CHANGED=1
      warn "FXRoute-managed spotifyd binary identity is unavailable or its path changed; refusing to replace it during provider setup"
      return 0
    fi
  fi
  if [[ -n "$existing_path" ]]; then
    SPOTIFYD_PRESENT_BEFORE=1
    SPOTIFYD_BINARY_PATH="$existing_path"
    if [[ ! -f "$SPOTIFYD_BINARY_PATH" || -L "$SPOTIFYD_BINARY_PATH" ]]; then
      warn "spotifyd path exists but is not a regular non-symlink file; preserving it and skipping provider setup"
      return 0
    fi
    current_sha256="$(sha256sum "$SPOTIFYD_BINARY_PATH" | awk '{print $1}')"
    if [[ $SPOTIFYD_INSTALLED_BY_FXROUTE -eq 1 ]]; then
      if [[ -z "$SPOTIFYD_BINARY_SHA256" || "$current_sha256" != "$SPOTIFYD_BINARY_SHA256" ]]; then
        SPOTIFYD_BINARY_IDENTITY_CHANGED=1
        warn "FXRoute-owned spotifyd binary identity is unavailable or changed; preserving it and skipping provider setup"
      fi
    fi
    if [[ ! -x "$SPOTIFYD_BINARY_PATH" ]]; then
      warn "spotifyd path exists but is not executable; preserving it and skipping provider setup"
      return 0
    fi
    pass "spotifyd binary already present; not reinstalling"
    return 0
  fi

  release_arch="$(spotifyd_arch_for_host || true)"
  [[ -n "$release_arch" ]] || {
    SPOTIFYD_PROVIDER_STATUS="unsupported architecture"
    warn "spotifyd v${SPOTIFYD_VERSION} has no confirmed binary for ${HOST_ARCH}; skipping"
    return 0
  }

  case "$release_arch" in
    x86_64)
      archive="spotifyd-linux-x86_64-full.tar.gz"
      checksum="a5872771a22c0dc4f7cb409cc1e47b09262a4d9939e4e8829592846d2dc93b722254b1e154efecf28a9c2309df5701b6ae6aa7c4862b8a89b34bf851268057b7"
      archive_url="https://github.com/Spotifyd/spotifyd/releases/download/v${SPOTIFYD_VERSION}/${archive}"
      checksum_algorithm="sha512"
      ;;
    aarch64)
      archive="$SPOTIFYD_ARM64_ARCHIVE"
      checksum="$SPOTIFYD_ARM64_SHA256"
      archive_url="$SPOTIFYD_ARM64_DOWNLOAD_URL"
      checksum_algorithm="sha256"
      ;;
    armv7)
      archive="spotifyd-linux-armv7-full.tar.gz"
      checksum="befed77ab3ba5b688ad0c054890576010135a8ba007422385ee3a68763a7ae99a566e010fb29f75c429354245c600da8abc826de4a57ad616c7b2a05dcf8b9b8"
      archive_url="https://github.com/Spotifyd/spotifyd/releases/download/v${SPOTIFYD_VERSION}/${archive}"
      checksum_algorithm="sha512"
      ;;
  esac

  work="$(mktemp -d -t fxroute-spotifyd.XXXXXX)"
  FXROUTE_ACTIVE_TEMP_DIR="$work"
  trap 'rm -rf "${work:-}" || true; [[ -z "${staged_binary:-}" ]] || run_as_target_user rm -f "$staged_binary" || true; FXROUTE_ACTIVE_STAGED_BINARY=""; FXROUTE_ACTIVE_TEMP_DIR=""; trap - RETURN' RETURN
  if ! run_cmd curl -fL --retry 3 -o "$work/$archive" "$archive_url"; then
    warn "spotifyd archive could not be downloaded"
    return 1
  fi
  if [[ "$checksum_algorithm" == "sha256" ]]; then
    if ! printf '%s  %s\n' "$checksum" "$work/$archive" | sha256sum -c -; then
      warn "spotifyd archive checksum mismatch"
      return 1
    fi
  else
    if ! printf '%s  %s\n' "$checksum" "$work/$archive" | sha512sum -c -; then
      warn "spotifyd archive checksum mismatch"
      return 1
    fi
  fi
  if ! run_cmd tar -xzf "$work/$archive" -C "$work"; then
    warn "spotifyd archive could not be extracted"
    return 1
  fi
  extracted="$(find "$work" -type f -name spotifyd -print -quit)"
  if [[ -z "$extracted" ]]; then
    warn "spotifyd archive did not contain a binary"
    return 1
  fi
  chmod -R a+rX "$work"
  run_as_target_user mkdir -p "$(dirname -- "$destination")"
  if ! staged_binary="$(run_as_target_user mktemp "$HOME/.local/bin/.spotifyd.XXXXXX")"; then
    warn "Could not stage $destination"
    return 1
  fi
  FXROUTE_ACTIVE_STAGED_BINARY="$staged_binary"
  if ! run_as_target_user install -m 755 "$extracted" "$staged_binary"; then
    warn "Could not stage $destination"
    return 1
  fi
  if ! run_as_target_user mv -f "$staged_binary" "$destination"; then
    warn "Could not install $destination atomically"
    return 1
  fi
  staged_binary=""
  FXROUTE_ACTIVE_STAGED_BINARY=""
  SPOTIFYD_BINARY_PATH="$destination"
  SPOTIFYD_INSTALLED_BY_FXROUTE=1
  SPOTIFYD_BINARY_SHA256="$(sha256sum "$SPOTIFYD_BINARY_PATH" | awk '{print $1}')"
  rm -rf "$work"
  FXROUTE_ACTIVE_TEMP_DIR=""
  trap - RETURN
  pass "spotifyd v${SPOTIFYD_VERSION} installed (${release_arch}, full/MPRIS)"
}

configure_spotifyd_service() {
  local service_dir="$HOME/.config/systemd/user"
  local service_path="$service_dir/spotifyd.service"
  local config_path="$HOME/.config/spotifyd/spotifyd.conf"
  local binary_path="$(spotifyd_binary_path || true)"
  local current_sha256=""

  SPOTIFYD_SERVICE_PATH="$service_path"
  [[ -n "$binary_path" ]] || {
    warn "spotifyd service skipped because no spotifyd binary is available"
    return 0
  }
  ensure_target_user_ownership

  if user_unit_exists spotifyd.service; then
    if [[ $SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]]; then
      if [[ ! -f "$service_path" || -L "$service_path" || -z "$SPOTIFYD_SERVICE_SHA256" ]]; then
        SPOTIFYD_SERVICE_IDENTITY_CHANGED=1
        warn "FXRoute-owned spotifyd service identity is unavailable; preserving the existing unit"
        return 0
      fi
      current_sha256="$(sha256sum "$service_path" | awk '{print $1}')"
      if [[ "$current_sha256" != "$SPOTIFYD_SERVICE_SHA256" ]]; then
        SPOTIFYD_SERVICE_IDENTITY_CHANGED=1
        warn "FXRoute-owned spotifyd service checksum changed; preserving the existing unit"
        return 0
      fi
      if user_systemctl daemon-reload && user_systemctl enable --now spotifyd.service; then
        pass "FXRoute-owned spotifyd user service enabled"
      else
        SPOTIFYD_SERVICE_SETUP_FAILED=1
        warn "FXRoute-owned spotifyd service is present but could not be enabled in this shell"
      fi
    else
      pass "existing spotifyd user service preserved"
    fi
    return 0
  fi

  run_as_target_user mkdir -p "$service_dir"
  run_as_target_user tee "$service_path" >/dev/null <<EOF
[Unit]
Description=spotifyd Spotify Connect receiver for FXRoute
After=pipewire.service pipewire-pulse.service

[Service]
Type=simple
ExecStart=$binary_path --no-daemon --config-path $config_path --zeroconf-port $SPOTIFYD_ZEROCONF_PORT
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=%t/bus
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE=1
  SPOTIFYD_SERVICE_SHA256="$(sha256sum "$service_path" | awk '{print $1}')"

  if user_systemctl daemon-reload && user_systemctl enable --now spotifyd.service; then
    pass "spotifyd user service enabled"
  else
    SPOTIFYD_SERVICE_SETUP_FAILED=1
    warn "spotifyd service was installed, but could not be enabled in this shell"
  fi
}

spotifyd_service_identity_is_intact() {
  local service_path="$HOME/.config/systemd/user/spotifyd.service"
  local current_sha256=""

  [[ $SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]] || return 0
  user_unit_exists spotifyd.service || return 0
  [[ -f "$service_path" && ! -L "$service_path" && -n "$SPOTIFYD_SERVICE_SHA256" ]] || return 1
  current_sha256="$(sha256sum "$service_path" | awk '{print $1}')"
  [[ "$current_sha256" == "$SPOTIFYD_SERVICE_SHA256" ]]
}

install_spotifyd() {
  local was_present=0
  local spotifyd_path=""
  local missing_runtime=""
  local service_disable_failed=0

  if ! spotifyd_arch_for_host >/dev/null 2>&1; then
    SPOTIFYD_PROVIDER_STATUS="unsupported architecture"
    warn "spotifyd is not available for ${HOST_ARCH}; skipping"
    return 0
  fi

  [[ -n "$(spotifyd_binary_path || true)" ]] && was_present=1
  if ! install_spotifyd_binary; then
    SPOTIFYD_PROVIDER_STATUS="unavailable; prebuilt download or verification failed"
    warn "spotifyd prebuilt could not be installed; leaving the provider unavailable"
    return 0
  fi
  if [[ $SPOTIFYD_BINARY_IDENTITY_CHANGED -eq 1 ]]; then
    SPOTIFYD_PROVIDER_STATUS="owned binary changed; preserved"
    return 0
  fi
  spotifyd_path="$(spotifyd_binary_path || true)"
  [[ -n "$spotifyd_path" ]] || return 0
  if [[ ! -f "$spotifyd_path" || -L "$spotifyd_path" ]]; then
    SPOTIFYD_PROVIDER_STATUS="existing path is not a regular non-symlink file; preserved"
    return 0
  fi
  if [[ ! -x "$spotifyd_path" ]]; then
    SPOTIFYD_PROVIDER_STATUS="existing path is not executable; preserved"
    return 0
  fi
  if ! spotifyd_service_identity_is_intact; then
    SPOTIFYD_SERVICE_IDENTITY_CHANGED=1
    SPOTIFYD_PROVIDER_STATUS="owned service changed; preserved"
    return 0
  fi
  spotifyd_path="$(spotifyd_binary_path || true)"
  if ! missing_runtime="$(spotifyd_runtime_missing_libraries "$spotifyd_path")"; then
    SPOTIFYD_PROVIDER_STATUS="unavailable; runtime dependency check failed"
    warn "spotifyd runtime dependencies could not be verified; preserving the binary and service"
    return 0
  fi
  if [[ -n "$missing_runtime" ]]; then
    if user_unit_exists spotifyd.service && [[ $SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -ne 1 ]]; then
      SPOTIFYD_PROVIDER_STATUS="unavailable; missing runtime libraries: ${missing_runtime//$'\n'/, }; existing foreign service preserved"
      warn "spotifyd cannot run on this host; preserving the existing non-FXRoute spotifyd.service and skipping replacement so it cannot continue using the incompatible binary"
      return 0
    fi
    if [[ $SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]]; then
      if ! user_systemctl disable --now spotifyd.service >/dev/null 2>&1; then
        service_disable_failed=1
        warn "spotifyd has missing runtime libraries, but its FXRoute-owned user service could not be disabled"
      fi
    fi
    SPOTIFYD_PROVIDER_STATUS="unavailable; missing runtime libraries: ${missing_runtime//$'\n'/, }"
    if [[ $service_disable_failed -eq 1 ]]; then
      SPOTIFYD_PROVIDER_STATUS+="; service disable failed"
    fi
    warn "spotifyd cannot run on this host; missing runtime libraries: ${missing_runtime//$'\n'/, }. The pinned prebuilt stays unavailable."
    return 0
  fi
  write_spotifyd_config
  configure_spotifyd_service
  if [[ $SPOTIFYD_SERVICE_IDENTITY_CHANGED -eq 1 || $SPOTIFYD_SERVICE_SETUP_FAILED -eq 1 ]]; then
    SPOTIFYD_PROVIDER_STATUS="owned service unavailable; preserved"
    return 0
  fi
  if [[ $SPOTIFYD_DSP_SINK_ROUTED -eq 1 ]] && user_unit_exists spotifyd.service \
    && user_systemctl is-active --quiet spotifyd.service >/dev/null 2>&1; then
    if user_systemctl restart spotifyd.service >/dev/null 2>&1; then
      pass "spotifyd restarted on the FXRoute DSP sink"
    else
      warn "spotifyd output routed but the service could not be restarted"
    fi
  fi
  if [[ $SPOTIFYD_DEVICE_NAME_CHANGED -eq 1 ]] && user_unit_exists spotifyd.service \
    && user_systemctl is-active --quiet spotifyd.service >/dev/null 2>&1; then
    if user_systemctl restart spotifyd.service >/dev/null 2>&1; then
      pass "spotifyd restarted with Connect name ${SPOTIFYD_CONNECT_NAME}"
    else
      warn "spotifyd Connect name updated but the service could not be restarted"
    fi
  fi
  if [[ $SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]]; then
    ensure_lan_firewall_rule mdns_5353_udp "spotifyd Zeroconf mDNS discovery"
    ensure_lan_firewall_rule spotifyd_zeroconf_4444_tcp "spotifyd Zeroconf TCP authentication"
  fi
  spotifyd_path="$(spotifyd_binary_path || true)"
  if [[ $was_present -eq 1 ]]; then
    SPOTIFYD_PROVIDER_STATUS="already present; service/config preserved or completed"
  else
    SPOTIFYD_PROVIDER_STATUS="installed/configured by FXRoute"
  fi
  echo "spotifyd first run: select ${SPOTIFYD_CONNECT_NAME:-FXRoute} in Spotify Connect. If OAuth is needed, stop the service and run:"
  echo "  systemctl --user stop spotifyd && ${spotifyd_path:-$HOME/.local/bin/spotifyd} authenticate --config-path $HOME/.config/spotifyd/spotifyd.conf"
  echo "  systemctl --user start spotifyd"
}

qobuz_runtime_packages_for_manager() {
  case "$1" in
    apt)
      local alsa_package="libasound2"
      if apt-cache show libasound2t64 >/dev/null 2>&1; then
        alsa_package="libasound2t64"
      fi
      echo "$alsa_package libdbus-1-3 avahi-daemon libavahi-client3 libnss-mdns"
      ;;
    dnf) echo "alsa-lib dbus-libs avahi nss-mdns" ;;
    zypper) echo "alsa libdbus-1-3 avahi libavahi-client3 nss-mdns" ;;
    pacman) echo "alsa-lib dbus avahi nss-mdns" ;;
    *) return 1 ;;
  esac
}

# qbzd renders through the ALSA->PipeWire bridge (its ALSA plugin); without
# it qbzd falls back to raw hardware ALSA and never reaches Playing state on
# the FXRoute DSP sink. Only apt is covered: the other managers pull the
# bridge with their PipeWire stacks. On Debian 13 with the trixie-backports
# PipeWire stack the bridge must come from backports as well, because mixing
# it with the distribution revision makes apt refuse the whole transaction.
ensure_qbzd_alsa_pipewire_bridge() {
  if [[ "$PACKAGE_MANAGER" != "apt" ]]; then
    return 0
  fi
  if package_installed pipewire-alsa; then
    return 0
  fi
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: the helper owns the whole
    # backports-vs-plain decision (same rule as below) and validates it
    # itself, so no apt command line ever crosses the sudo boundary here.
    if debian_trixie_backports_available; then
      run_cmd provider_privileged backports-pipewire-alsa
    else
      pkg_install pipewire-alsa
    fi
    pass "ALSA->PipeWire bridge installed for qbzd"
    return 0
  fi
  if debian_trixie_backports_available; then
    if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
      run_cmd "${SUDO_CMD[@]}" apt-get update
      PKG_REFRESH_DONE=1
    fi
    run_cmd "${SUDO_CMD[@]}" apt-get install -y -t trixie-backports pipewire-alsa
  else
    pkg_install pipewire-alsa
  fi
  pass "ALSA->PipeWire bridge installed for qbzd"
}

ensure_qobuz_runtime_dependencies() {
  local was_present="$AVAHI_WAS_PRESENT_BEFORE"
  local was_active="$AVAHI_WAS_ACTIVE_BEFORE"
  local was_enabled="$AVAHI_WAS_ENABLED_BEFORE"

  if [[ "$was_present" == "0" ]]; then
    AVAHI_INSTALLED_BY_FXROUTE=1
  fi
  install_missing_provider_packages "$(qobuz_runtime_packages_for_manager "$PACKAGE_MANAGER")"
  ensure_qbzd_alsa_pipewire_bridge
  if avahi_is_present && ! systemctl is-active avahi-daemon >/dev/null 2>&1; then
    if [[ "$was_active" == "0" || "$was_enabled" == "0" ]]; then
      AVAHI_ENABLED_BY_FXROUTE=1
    fi
    if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
      if provider_privileged avahi-enable; then
        pass "Avahi enabled for Qobuz Connect discovery"
      else
        warn "Avahi is installed but could not be enabled for Qobuz Connect discovery"
      fi
    elif "${SUDO_CMD[@]}" systemctl enable --now avahi-daemon; then
      pass "Avahi enabled for Qobuz Connect discovery"
    else
      warn "Avahi is installed but could not be enabled for Qobuz Connect discovery"
    fi
  fi
}

qbzd_binary_path() {
  if [[ -e "$HOME/.local/bin/qbzd" || -L "$HOME/.local/bin/qbzd" ]]; then
    printf '%s\n' "$HOME/.local/bin/qbzd"
    return 0
  fi
  command -v qbzd 2>/dev/null || true
}

install_qbzd_binary() {
  local release_arch=""
  local archive=""
  local checksum=""
  local archive_url=""
  local work=""
  local extracted=""
  local current_sha256=""
  local existing_path=""

  existing_path="$(qbzd_binary_path || true)"
  if [[ $QBZD_INSTALLED_BY_FXROUTE -eq 1 || $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 1 ]]; then
    if [[ -z "$QBZD_BINARY_PATH" || "$existing_path" != "$QBZD_BINARY_PATH" ]]; then
      QBZD_BINARY_IDENTITY_CHANGED=1
      warn "FXRoute-managed qbzd binary identity is unavailable or its path changed; refusing to replace it during provider setup"
      return 0
    fi
  fi
  if [[ -n "$existing_path" ]]; then
    QBZD_PRESENT_BEFORE=1
    QBZD_BINARY_PATH="$existing_path"
    if [[ ! -f "$QBZD_BINARY_PATH" || -L "$QBZD_BINARY_PATH" ]]; then
      warn "qbzd path exists but is not a regular non-symlink file; preserving it and skipping provider setup"
      return 0
    fi
    if [[ -f "$QBZD_BINARY_PATH" ]]; then
      current_sha256="$(sha256sum "$QBZD_BINARY_PATH" | awk '{print $1}')"
      if [[ $QBZD_INSTALLED_BY_FXROUTE -eq 1 || $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 1 ]]; then
        if [[ -z "$QBZD_BINARY_SHA256" || "$current_sha256" != "$QBZD_BINARY_SHA256" ]]; then
          QBZD_BINARY_IDENTITY_CHANGED=1
          warn "FXRoute-owned qbzd binary identity is unavailable or changed; preserving it and skipping Qobuz provider setup"
        fi
      else
        QBZD_BINARY_SHA256="$current_sha256"
      fi
    fi
    if [[ ! -x "$QBZD_BINARY_PATH" ]]; then
      warn "qbzd path exists but is not executable; preserving it and skipping provider setup"
      return 0
    fi
    pass "qbzd binary already present; not reinstalling"
    return 0
  fi

  release_arch="$(qbzd_arch_for_host || true)"
  [[ -n "$release_arch" ]] || {
    QOBUZ_PROVIDER_STATUS="unsupported architecture"
    warn "qbzd v${QBZD_VERSION} has no confirmed binary for ${HOST_ARCH}; skipping"
    return 0
  }

  case "$release_arch" in
    amd64)
      archive="qbzd-${QBZD_VERSION}-linux-amd64.tar.gz"
      checksum="6bcdb2616f339b7905fc58f48edf7e3bc2e0e9eadc1ce6235b60c7fdf34b804c"
      ;;
    aarch64)
      archive="qbzd-${QBZD_VERSION}-linux-aarch64.tar.gz"
      checksum="adade56509544c00187476d58acef78538d3e5d475370263d98397dc61f200c9"
      ;;
  esac

  archive_url="https://github.com/vicrodh/qbz/releases/download/v${QBZD_VERSION}/${archive}"
  work="$(mktemp -d -t fxroute-qbzd.XXXXXX)"
  FXROUTE_ACTIVE_TEMP_DIR="$work"
  trap 'rm -rf "${work:-}"' RETURN
  run_cmd curl -fL --retry 3 -o "$work/$archive" "$archive_url"
  printf '%s  %s\n' "$checksum" "$work/$archive" | sha256sum -c -
  run_cmd tar -xzf "$work/$archive" -C "$work"
  extracted="$(find "$work" -type f -name qbzd -perm -u+x -print -quit)"
  [[ -n "$extracted" ]] || die "qbzd archive did not contain an executable"
  chmod -R a+rX "$work"
  run_as_target_user mkdir -p "$HOME/.local/bin"
  run_as_target_user install -m 755 "$extracted" "$HOME/.local/bin/qbzd"
  QBZD_BINARY_PATH="$HOME/.local/bin/qbzd"
  QBZD_INSTALLED_BY_FXROUTE=1
  QBZD_BINARY_SHA256="$(sha256sum "$QBZD_BINARY_PATH" | awk '{print $1}')"
  rm -rf "$work"
  FXROUTE_ACTIVE_TEMP_DIR=""
  trap - RETURN
  pass "qbzd v${QBZD_VERSION} installed (${release_arch})"
}

read_qbzd_volume_mode() {
  local binary_path=""
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || return 1
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

set_qbzd_volume_mode() {
  local mode="$1"
  local binary_path=""
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || return 1
  run_as_target_user "$binary_path" settings set --quiet "$QOBUZ_VOLUME_MODE_KEY" "$mode"
}

configure_qbzd_volume_mode() {
  local current_mode=""

  current_mode="$(read_qbzd_volume_mode || true)"
  [[ -n "$current_mode" ]] || die "Could not read qbzd qconnect.volume_mode; refusing to start Qobuz without the FXRoute locked/unity volume contract"
  QBZD_VOLUME_MODE_AFTER="$QOBUZ_REQUIRED_VOLUME_MODE"
  if [[ "$current_mode" == "$QOBUZ_REQUIRED_VOLUME_MODE" ]]; then
    pass "qbzd QConnect volume mode already locked (FXRoute unity contract)"
    return 0
  fi

  if [[ $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 0 || "$current_mode" != "$QBZD_VOLUME_MODE_AFTER" ]]; then
    QBZD_VOLUME_MODE_BEFORE="$current_mode"
  fi
  # Mark the side effect before invoking qbzd so an exit checkpoint can still
  # offer restoration if the command changes the setting but read-back fails.
  QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=1
  if ! set_qbzd_volume_mode "$QOBUZ_REQUIRED_VOLUME_MODE"; then
    die "Could not set qbzd qconnect.volume_mode=$QOBUZ_REQUIRED_VOLUME_MODE"
  fi
  if [[ "$(read_qbzd_volume_mode || true)" != "$QOBUZ_REQUIRED_VOLUME_MODE" ]]; then
    die "qbzd did not retain qconnect.volume_mode=$QOBUZ_REQUIRED_VOLUME_MODE"
  fi
  pass "qbzd QConnect volume mode set to locked (FXRoute master/unity contract)"
}

read_qbzd_qconnect_startup_mode() {
  local binary_path=""
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || return 1
  run_as_target_user "$binary_path" settings show --quiet --json 2>/dev/null | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
    value = payload.get("qconnect.startup_mode", "")
except (ValueError, OSError):
    raise SystemExit(1)
if not value:
    raise SystemExit(1)
print(value)
'
}

configure_qbzd_qconnect() {
  local current_mode=""
  local binary_path=""

  current_mode="$(read_qbzd_qconnect_startup_mode || true)"
  [[ -n "$current_mode" ]] || die "Could not read qbzd qconnect.startup_mode; refusing to leave Qobuz Connect disabled"
  QBZD_QCONNECT_STARTUP_MODE_AFTER="on"
  if [[ "$current_mode" == "on" ]]; then
    pass "qbzd Qobuz Connect auto-connect already enabled"
    return 0
  fi

  if [[ $QBZD_QCONNECT_CHANGED_BY_FXROUTE -eq 0 ]]; then
    QBZD_QCONNECT_STARTUP_MODE_BEFORE="$current_mode"
  fi
  # Mark the side effect before invoking qbzd so an exit checkpoint can still
  # offer restoration if the command changes the setting but read-back fails.
  # The device name is deliberately untouched: it stays whatever the owner or
  # qbzd setup chose.
  QBZD_QCONNECT_CHANGED_BY_FXROUTE=1
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || die "qbzd binary disappeared before Qobuz Connect could be enabled"
  if ! run_as_target_user "$binary_path" qconnect enable --quiet; then
    die "Could not enable qbzd Qobuz Connect auto-connect"
  fi
  if [[ "$(read_qbzd_qconnect_startup_mode || true)" != "on" ]]; then
    die "qbzd did not retain qconnect.startup_mode=on"
  fi
  pass "qbzd Qobuz Connect auto-connect enabled"
}

read_qbzd_audio_output() {
  local binary_path=""
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || return 1
  run_as_target_user "$binary_path" settings show --quiet --json 2>/dev/null | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except (ValueError, OSError):
    raise SystemExit(1)
for key in ("audio.backend", "audio.device", "audio.skip_sink_switch"):
    value = payload.get(key, "")
    if not value:
        raise SystemExit(1)
    print(f"{key}={value}")
'
}

configure_qbzd_audio_output() {
  local current=""
  local backend=""
  local device=""
  local skip=""
  local binary_path=""

  current="$(read_qbzd_audio_output || true)"
  [[ -n "$current" ]] || die "Could not read qbzd audio output settings; refusing to leave Qobuz off the DSP sink"
  backend="$(sed -n 's/^audio\.backend=//p' <<<"$current" | head -n 1)"
  device="$(sed -n 's/^audio\.device=//p' <<<"$current" | head -n 1)"
  skip="$(sed -n 's/^audio\.skip_sink_switch=//p' <<<"$current" | head -n 1)"
  if [[ "$backend" == "pipewire" && "$device" == "fxroute_dsp_sink" && "$skip" == "true" ]]; then
    pass "qbzd audio output already targets the FXRoute DSP sink"
    return 0
  fi

  if [[ $QBZD_AUDIO_CHANGED_BY_FXROUTE -eq 0 ]]; then
    QBZD_AUDIO_BACKEND_BEFORE="$backend"
    QBZD_AUDIO_DEVICE_BEFORE="$device"
    QBZD_AUDIO_SKIP_SINK_SWITCH_BEFORE="$skip"
  fi
  # Mark the side effect before invoking qbzd so an exit checkpoint can still
  # offer restoration if a command changes a setting but read-back fails.
  # The device name is deliberately untouched: it stays whatever the owner or
  # qbzd setup chose.
  QBZD_AUDIO_CHANGED_BY_FXROUTE=1
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || die "qbzd binary disappeared before its audio output could be routed"
  if ! run_as_target_user "$binary_path" settings set --quiet audio.backend pipewire; then
    die "Could not set qbzd audio.backend=pipewire"
  fi
  if ! run_as_target_user "$binary_path" settings set --quiet audio.device fxroute_dsp_sink; then
    die "Could not set qbzd audio.device=fxroute_dsp_sink"
  fi
  if ! run_as_target_user "$binary_path" settings set --quiet audio.skip_sink_switch true; then
    die "Could not set qbzd audio.skip_sink_switch=true"
  fi
  current="$(read_qbzd_audio_output || true)"
  backend="$(sed -n 's/^audio\.backend=//p' <<<"$current" | head -n 1)"
  device="$(sed -n 's/^audio\.device=//p' <<<"$current" | head -n 1)"
  skip="$(sed -n 's/^audio\.skip_sink_switch=//p' <<<"$current" | head -n 1)"
  if [[ "$backend" != "pipewire" || "$device" != "fxroute_dsp_sink" || "$skip" != "true" ]]; then
    die "qbzd did not retain the FXRoute DSP sink audio output"
  fi
  pass "qbzd audio output routed to the FXRoute DSP sink"
}

configure_qbzd_service() {
  local service_dir="$HOME/.config/systemd/user"
  local service_path="$service_dir/qbzd.service"
  local binary_path="$(qbzd_binary_path || true)"
  local current_sha256=""

  QBZD_SERVICE_PATH="$service_path"
  [[ -n "$binary_path" ]] || {
    warn "qbzd service skipped because no qbzd binary is available"
    return 0
  }
  ensure_target_user_ownership

  if user_unit_exists qbzd.service; then
    if [[ $QBZD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]]; then
      if [[ ! -f "$service_path" || -L "$service_path" || -z "$QBZD_SERVICE_SHA256" ]]; then
        QBZD_SERVICE_IDENTITY_CHANGED=1
        warn "FXRoute-owned qbzd service identity is unavailable; preserving the existing unit"
        return 0
      fi
      current_sha256="$(sha256sum "$service_path" | awk '{print $1}')"
      if [[ "$current_sha256" != "$QBZD_SERVICE_SHA256" ]]; then
        QBZD_SERVICE_IDENTITY_CHANGED=1
        warn "FXRoute-owned qbzd service checksum changed; preserving the existing unit"
        return 0
      fi
      if ! user_systemctl daemon-reload || ! user_systemctl enable --now qbzd.service; then
        QBZD_SERVICE_SETUP_FAILED=1
        warn "FXRoute-owned qbzd service is present but could not be enabled in this shell"
        return 0
      fi
    fi
    if [[ $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 1 || $QBZD_QCONNECT_CHANGED_BY_FXROUTE -eq 1 || $QBZD_AUDIO_CHANGED_BY_FXROUTE -eq 1 ]] \
      && [[ -f "$service_path" && ! -L "$service_path" ]] \
      && user_systemctl is-active --quiet qbzd.service; then
      if ! user_systemctl restart qbzd.service; then
        die "Could not restart the existing qbzd service after updating its Qobuz Connect settings"
      fi
    fi
    pass "existing qbzd user service preserved"
    return 0
  fi

  run_as_target_user mkdir -p "$service_dir"
  run_as_target_user tee "$service_path" >/dev/null <<EOF
[Unit]
Description=qbzd Qobuz Connect receiver for FXRoute

[Service]
Type=simple
ExecStart=$binary_path run
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Restart=on-failure
RestartSec=10
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF
  QBZD_SERVICE_INSTALLED_BY_FXROUTE=1
  QBZD_SERVICE_SHA256="$(sha256sum "$service_path" | awk '{print $1}')"

  if user_systemctl daemon-reload && user_systemctl enable --now qbzd.service; then
    pass "qbzd user service enabled"
  else
    QBZD_SERVICE_SETUP_FAILED=1
    warn "qbzd service was installed, but could not be enabled in this shell"
  fi
}

qbzd_service_identity_is_intact() {
  local service_path="$HOME/.config/systemd/user/qbzd.service"
  local current_sha256=""

  [[ $QBZD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]] || return 0
  user_unit_exists qbzd.service || return 0
  [[ -f "$service_path" && ! -L "$service_path" && -n "$QBZD_SERVICE_SHA256" ]] || return 1
  current_sha256="$(sha256sum "$service_path" | awk '{print $1}')"
  [[ "$current_sha256" == "$QBZD_SERVICE_SHA256" ]]
}

install_qobuz() {
  local qbzd_path=""

  if [[ -z "$(qbzd_binary_path || true)" ]] && ! qbzd_arch_for_host >/dev/null 2>&1; then
    QOBUZ_PROVIDER_STATUS="unsupported architecture"
    warn "Qobuz/qbzd is not available for ${HOST_ARCH}; skipping"
    return 0
  fi

  install_qbzd_binary
  if [[ $QBZD_BINARY_IDENTITY_CHANGED -eq 1 ]]; then
    QOBUZ_PROVIDER_STATUS="owned binary changed; preserved"
    return 0
  fi
  qbzd_path="$(qbzd_binary_path || true)"
  [[ -n "$qbzd_path" ]] || return 0
  if [[ ! -f "$qbzd_path" || -L "$qbzd_path" ]]; then
    QOBUZ_PROVIDER_STATUS="existing path is not a regular non-symlink file; preserved"
    return 0
  fi
  if [[ ! -x "$qbzd_path" ]]; then
    QOBUZ_PROVIDER_STATUS="existing path is not executable; preserved"
    return 0
  fi
  if ! qbzd_service_identity_is_intact; then
    QBZD_SERVICE_IDENTITY_CHANGED=1
    QOBUZ_PROVIDER_STATUS="owned service changed; preserved"
    return 0
  fi
  ensure_qobuz_runtime_dependencies
  configure_qbzd_volume_mode
  configure_qbzd_qconnect
  configure_qbzd_audio_output
  configure_qbzd_service
  if [[ $QBZD_SERVICE_IDENTITY_CHANGED -eq 1 || $QBZD_SERVICE_SETUP_FAILED -eq 1 ]]; then
    QOBUZ_PROVIDER_STATUS="owned service unavailable; preserved"
    return 0
  fi
  ensure_lan_firewall_service_open mdns "Qobuz Connect discovery"
  qbzd_path="$(qbzd_binary_path || true)"
  if [[ $QBZD_INSTALLED_BY_FXROUTE -eq 1 || $QBZD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]]; then
    QOBUZ_PROVIDER_STATUS="installed/configured by FXRoute"
  else
    QOBUZ_PROVIDER_STATUS="already present; service preserved"
  fi
  echo "Qobuz first run: run '${qbzd_path:-$HOME/.local/bin/qbzd} setup' and complete the browser-based OAuth login."
  echo "Then enable Qobuz Connect in qbzd and select its device from the Qobuz app."
}

ensure_target_user_cache_ownership() {
  local cache_dir="$HOME/.cache"
  local owner_uid=""

  [[ ! -L "$cache_dir" ]] || die "Refusing to use a symlinked target-user cache directory: $cache_dir"
  if path_has_symlink_component "$cache_dir"; then
    die "Refusing to use a symlinked parent as a target-user cache directory: $cache_dir"
  fi
  if [[ ! -e "$cache_dir" ]]; then
    run_as_target_user mkdir -p "$cache_dir"
    return 0
  fi
  [[ -d "$cache_dir" ]] || die "Target-user cache path is not a directory: $cache_dir"
  owner_uid="$(stat -c '%u' "$cache_dir" 2>/dev/null || true)"
  [[ -n "$owner_uid" ]] || die "Could not stat the target-user cache directory: $cache_dir"
  if [[ "$owner_uid" != "$FXROUTE_TARGET_UID" ]]; then
    if [[ "$(id -u)" -eq 0 ]]; then
      log "Target-user cache directory is owned by uid $owner_uid; taking ownership for $FXROUTE_TARGET_USER"
      chown "$FXROUTE_TARGET_UID:$FXROUTE_TARGET_GROUP" "$cache_dir" \
        || die "Could not take ownership of the target-user cache directory: $cache_dir"
    else
      die "Target-user cache directory is not owned by $FXROUTE_TARGET_USER ($cache_dir); re-run once from a shell with sudo so the installer can repair it"
    fi
  fi
}

configure_optional_streaming() {
  # Provider daemons (qbzd fatally requires a writable user cache dir)
  # must never start into a foreign-owned ~/.cache. The Qobuz volume
  # bridge tails the journal as the target user, which needs the
  # systemd-journal group wherever entries land outside user files.
  ensure_target_user_cache_ownership
  ensure_target_user_journal_access
  detect_existing_provider_components

  if [[ $SELECT_SPOTIFY_DESKTOP -eq 1 ]]; then
    install_spotify_desktop
  fi
  if [[ $SELECT_SPOTIFYD -eq 1 ]]; then
    install_spotifyd
  fi
  if [[ $SELECT_QOBUZ -eq 1 ]]; then
    install_qobuz
  fi

  ensure_tidal_dependency
  if [[ $SELECT_TIDAL -eq 1 ]]; then
    echo "TIDAL first run: use FXRoute's existing PKCE login flow; no credentials are stored by the installer."
  fi
}

setup_python_env() {
  local venv_dir="$INSTALL_ROOT/.venv"
  local marker="$venv_dir/.fxroute-requirements.sha256"
  prepare_target_user_directory "$venv_dir"
  log "python3 -m venv $venv_dir"
  run_as_target_user python3 -m venv "$venv_dir"
  log "$venv_dir/bin/python3 -m pip install --upgrade pip setuptools wheel"
  run_as_target_user "$venv_dir/bin/python3" -m pip install --upgrade pip setuptools wheel
  log "$venv_dir/bin/pip install -r $INSTALL_ROOT/requirements.txt"
  run_as_target_user "$venv_dir/bin/pip" install -r "$INSTALL_ROOT/requirements.txt"
  run_as_target_user tee "$marker" >/dev/null <<<"$(sha256sum "$INSTALL_ROOT/requirements.txt" | awk '{print $1}')"
  pass "Python venv created"
  pass "pip install -r requirements.txt"
}

tidalapi_version() {
  local python_bin="$INSTALL_ROOT/.venv/bin/python3"
  [[ -x "$python_bin" ]] || return 0
  run_as_target_user "$python_bin" - <<'PY'
try:
    from importlib.metadata import version
except ImportError:
    raise SystemExit(0)
try:
    print(version("tidalapi"))
except Exception:
    pass
PY
}

ensure_tidal_dependency() {
  local current_version=""
  local marker="$INSTALL_ROOT/.venv/.fxroute-tidal-requirements.sha256"

  current_version="$(tidalapi_version || true)"
  if [[ -n "$current_version" ]]; then
    TIDAL_PRESENT_BEFORE=1
  fi

  if [[ $SELECT_TIDAL -eq 0 ]]; then
    if [[ -n "$current_version" ]]; then
      TIDAL_PROVIDER_STATUS="already present (not selected; preserved)"
      pass "TIDAL dependency already present ($current_version); not selected, preserving it"
    else
      TIDAL_PROVIDER_STATUS="not selected; not installed"
      pass "TIDAL dependency not selected"
    fi
    return 0
  fi

  TIDAL_DEPENDENCY_SELECTED=1
  if [[ "$current_version" == "0.8.11" ]]; then
    TIDAL_INSTALLED_VERSION="$current_version"
    TIDAL_PROVIDER_STATUS="already present"
    pass "TIDAL dependency already available (tidalapi $current_version)"
    return 0
  fi

  [[ -f "$INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE" ]] || die "Missing optional TIDAL requirements: $INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE"
  [[ -n "$current_version" ]] || TIDAL_INSTALLED_BY_FXROUTE=1
  log "$INSTALL_ROOT/.venv/bin/pip install -r $INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE"
  run_as_target_user "$INSTALL_ROOT/.venv/bin/pip" install -r "$INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE"
  run_as_target_user tee "$marker" >/dev/null <<<"$(sha256sum "$INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE" | awk '{print $1}')"
  TIDAL_INSTALLED_VERSION="$(tidalapi_version || true)"
  TIDAL_PROVIDER_STATUS="installed by FXRoute"
  pass "TIDAL dependency installed (PKCE flow remains in FXRoute)"
}

write_service_unit() {
  local service_dir="$HOME/.config/systemd/user"
  run_as_target_user mkdir -p "$service_dir"

  run_as_target_user tee "$service_dir/$SERVICE_NAME.service" >/dev/null <<EOF
[Unit]
Description=FXRoute
After=default.target pipewire.service pipewire-pulse.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_ROOT
EnvironmentFile=$INSTALL_ROOT/.env
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$INSTALL_ROOT/.venv/bin/python3 $INSTALL_ROOT/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

  if user_systemctl daemon-reload; then
    if user_systemctl enable "$SERVICE_NAME" && user_systemctl restart "$SERVICE_NAME"; then
      pass "systemd user service enabled"
    else
      fail "systemd user service enable/start"
      warn "systemctl --user enable/restart $SERVICE_NAME failed, likely because no active user bus is available in this shell"
    fi
  else
    fail "systemd user daemon-reload"
  fi
}

user_unit_exists() {
  local unit="$1"
  [[ -e "$HOME/.config/systemd/user/$unit" || -L "$HOME/.config/systemd/user/$unit" \
    || -e "/etc/systemd/user/$unit" || -L "/etc/systemd/user/$unit" \
    || -e "/usr/local/lib/systemd/user/$unit" || -L "/usr/local/lib/systemd/user/$unit" \
    || -e "/usr/lib/systemd/user/$unit" || -L "/usr/lib/systemd/user/$unit" ]]
}

enable_user_audio_services() {
  local targets=()
  local missing_units=()

  if user_unit_exists pipewire.socket; then
    targets+=(pipewire.socket)
  elif user_unit_exists pipewire.service; then
    targets+=(pipewire.service)
  else
    missing_units+=(pipewire.socket pipewire.service)
  fi

  if user_unit_exists wireplumber.service; then
    targets+=(wireplumber.service)
  else
    missing_units+=(wireplumber.service)
  fi

  if user_unit_exists pipewire-pulse.socket; then
    targets+=(pipewire-pulse.socket)
  elif user_unit_exists pipewire-pulse.service; then
    targets+=(pipewire-pulse.service)
  else
    missing_units+=(pipewire-pulse.socket pipewire-pulse.service)
  fi

  if [[ ${#targets[@]} -gt 0 ]]; then
    if user_systemctl enable --now "${targets[@]}" >/dev/null 2>&1; then
      pass "PipeWire/WirePlumber user services enabled (${targets[*]})"
    else
      warn "PipeWire/WirePlumber user services could not be enabled in this shell: ${targets[*]}; on headless systems start them with: systemctl --user enable --now ${targets[*]}"
    fi
  fi

  if [[ ${#missing_units[@]} -gt 0 ]]; then
    warn "PipeWire user units not found: ${missing_units[*]}; FXRoute needs pipewire with a session manager (wireplumber) and the pipewire-pulse compatibility server for the DSP ingress sink"
  fi
}

enable_user_session_persistence() {
  local install_user="$FXROUTE_TARGET_USER"
  local deadline=$((SECONDS + 30))
  if loginctl show-user "$install_user" -p Linger --value 2>/dev/null | grep -qx 'yes'; then
    USER_LINGER_WAS_ENABLED=1
    pass "user session persistence already active (loginctl enable-linger)"
  elif "${SUDO_CMD[@]}" loginctl enable-linger "$install_user"; then
    USER_LINGER_ENABLED_BY_FXROUTE=1
    pass "user session persistence enabled (loginctl enable-linger)"
  else
    die "loginctl enable-linger failed; refusing to install a headless FXRoute service without persistent user-session support"
  fi

  # On a headless system, linger alone does not start the user manager.
  # Explicitly trigger it so the session bus appears promptly.
  local manager_unit="user@${FXROUTE_TARGET_UID}.service"
  if "${SUDO_CMD[@]}" systemctl start "$manager_unit" >/dev/null 2>&1; then
    log "Started user session manager ($manager_unit)"
  fi

  # On a headless system, the user dbus socket is not auto-created without
  # a login session.  Start dbus.service explicitly so the session bus appears.
  user_systemctl start dbus.service >/dev/null 2>&1 || true

  while (( SECONDS < deadline )); do
    if [[ -d "$FXROUTE_RUNTIME_DIR" && -S "$FXROUTE_RUNTIME_DIR/bus" ]]; then
      pass "target user session bus available ($FXROUTE_RUNTIME_DIR/bus)"
      return 0
    fi
    sleep 1
  done
  die "target user session bus did not become available at $FXROUTE_RUNTIME_DIR/bus"
}

alsa_hardware_present() {
  compgen -G /dev/snd/controlC* >/dev/null 2>&1
}

target_user_in_audio_group() {
  id -nG "$FXROUTE_TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx audio
}

target_user_can_open_alsa_control() {
  local card=""
  for card in /dev/snd/controlC*; do
    [[ -e "$card" ]] || continue
    if run_as_target_user sh -c "exec 3<>'$card'" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

target_user_has_seat_session() {
  loginctl list-sessions --no-legend 2>/dev/null \
    | awk -v user="$FXROUTE_TARGET_USER" '$3 == user && $4 != "-" { found = 1 } END { exit found ? 0 : 1 }'
}

target_user_in_journal_group() {
  id -nG "$FXROUTE_TARGET_USER" 2>/dev/null | tr ' ' '\n' | grep -qx systemd-journal
}

ensure_target_user_journal_access() {
  local install_user="$FXROUTE_TARGET_USER"

  if ! getent group systemd-journal >/dev/null 2>&1; then
    warn "No systemd-journal group exists; the Qobuz remote volume bridge cannot follow the journal"
    return 0
  fi

  if target_user_in_journal_group; then
    pass "target user can read the journal"
    return 0
  fi

  # The Qobuz volume bridge tails the qbzd journal as the target user. On
  # hosts where new entries land outside the user-UID journal files, that
  # tail needs traverse/read rights on the runtime journal, which only the
  # systemd-journal group grants.
  log "Adding $install_user to the systemd-journal group for Qobuz Connect volume tracking"
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: fixed group operation through the
    # root-owned helper (SUDO_USER-derived user, no username argument).
    if ! provider_privileged journal-group; then
      warn "Could not add $install_user to the systemd-journal group"
      return 0
    fi
  elif ! "${SUDO_CMD[@]}" usermod -aG systemd-journal "$install_user"; then
    warn "Could not add $install_user to the systemd-journal group"
    return 0
  fi
  JOURNAL_GROUP_ADDED_BY_FXROUTE=1
  pass "target user added to the systemd-journal group"

  # Supplementary groups are fixed when the user manager starts. A
  # providers-only run must never restart the running session (it would
  # kill FXRoute mid-install): there the group applies after the next
  # login or reboot.
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    warn "The systemd-journal group applies after the next login or reboot"
    return 0
  fi
  # Never restart a session on a seat: that would kill a running desktop.
  if target_user_has_seat_session; then
    warn "A graphical seat session is active; the systemd-journal group applies after the next login or reboot"
    return 0
  fi

  local manager_unit="user@${FXROUTE_TARGET_UID}.service"
  local deadline=$((SECONDS + 30))
  if ! "${SUDO_CMD[@]}" systemctl restart "$manager_unit" >/dev/null 2>&1; then
    warn "User session restart failed; the systemd-journal group takes effect after the next login or reboot"
    return 0
  fi
  while (( SECONDS < deadline )); do
    if [[ -d "$FXROUTE_RUNTIME_DIR" && -S "$FXROUTE_RUNTIME_DIR/bus" ]]; then
      pass "target user session restarted with the systemd-journal group"
      return 0
    fi
    sleep 1
  done
  warn "User session bus did not return after restart; the systemd-journal group takes effect after the next login or reboot"
}

ensure_target_user_audio_access() {
  local install_user="$FXROUTE_TARGET_USER"

  if ! alsa_hardware_present; then
    warn "No ALSA sound hardware found; FXRoute will install, but no hardware output will be selectable until audio hardware is present"
    return 0
  fi

  if ! getent group audio >/dev/null 2>&1; then
    warn "The audio group does not exist; cannot grant $install_user ALSA device access"
    return 0
  fi

  if target_user_in_audio_group; then
    if target_user_can_open_alsa_control; then
      pass "target user can reach the ALSA audio hardware"
    else
      warn "ALSA hardware is present, but the target user cannot open a control device despite audio group membership"
    fi
    return 0
  fi

  # ALSA hardware and an audio group exist, but the target user is not a
  # member. Add them unconditionally: logind seat ACLs cover only active
  # sessions, so on a headless boot with linger (no login) the user would
  # otherwise lose ALSA access and WirePlumber would not create a hardware
  # sink. The current session may reach the devices via ACL right now, but
  # that access disappears with the session.
  log "Adding $install_user to the audio group for ALSA device access"
  if ! "${SUDO_CMD[@]}" usermod -aG audio "$install_user"; then
    warn "Could not add $install_user to the audio group"
    return 0
  fi
  AUDIO_GROUP_ADDED_BY_FXROUTE=1
  pass "target user added to the audio group"

  # Supplementary groups are fixed when the user manager starts. Restart it so
  # PipeWire/WirePlumber and FXRoute inherit the audio group without a re-login.
  # Never restart a session on a seat: that would kill a running desktop.
  if target_user_has_seat_session; then
    warn "A graphical seat session is active; the audio group applies after the next login or reboot"
    return 0
  fi

  local manager_unit="user@${FXROUTE_TARGET_UID}.service"
  local deadline=$((SECONDS + 30))
  if ! "${SUDO_CMD[@]}" systemctl restart "$manager_unit" >/dev/null 2>&1; then
    warn "User session restart failed; the audio group takes effect after the next login or reboot"
    return 0
  fi
  while (( SECONDS < deadline )); do
    if [[ -d "$FXROUTE_RUNTIME_DIR" && -S "$FXROUTE_RUNTIME_DIR/bus" ]]; then
      pass "target user session restarted with the audio group"
      return 0
    fi
    sleep 1
  done
  warn "User session bus did not return after restart; the audio group takes effect after the next login or reboot"
}

configure_dsp_ingress_sink() {
  local config_dir="$HOME/.config/pipewire/pipewire-pulse.conf.d"
  local config_file="$config_dir/50-fxroute-dsp-sink.conf"

  run_as_target_user mkdir -p "$config_dir"
  run_as_target_user tee "$config_file" >/dev/null <<'EOF'
pulse.cmd = [
  { cmd = "load-module" args = "module-null-sink sink_name=fxroute_dsp_sink sink_properties=device.description=FXRoute_DSP_Ingress" flags = [ ] }
]
EOF
  if user_systemctl restart pipewire-pulse.service; then
    pass "FXRoute DSP ingress sink configured"
  else
    warn "FXRoute DSP ingress sink config was written, but pipewire-pulse could not be restarted in this shell"
  fi
}

disable_legacy_samplerate_override() {
  user_systemctl stop switch-sample-rate.service >/dev/null 2>&1 || true
  user_systemctl disable switch-sample-rate.service >/dev/null 2>&1 || true
  run_as_target_user rm -f "$HOME/.config/systemd/user/switch-sample-rate.service" "$HOME/switch-sample-rate.sh"
  user_systemctl daemon-reload >/dev/null 2>&1 || true
}

configure_pipewire_samplerates_if_available() {
  local script="$INSTALL_ROOT/scripts/configure-pipewire-samplerates.sh"

  [[ -f "$script" ]] || return
  run_as_target_user chmod +x "$script"

  disable_legacy_samplerate_override

  if run_as_target_user "$script" apply; then
    pass "PipeWire samplerate allowed-rates configured"
  else
    fail "PipeWire samplerate allowed-rates configured"
    warn "PipeWire samplerate setup could not be applied automatically in this shell"
  fi
}

detect_spotify_autostart_command() {
  if flatpak_app_installed "com.spotify.Client"; then
    printf 'flatpak run com.spotify.Client\n'
    return 0
  fi

  if command -v spotify >/dev/null 2>&1; then
    printf 'spotify\n'
    return 0
  fi

  return 1
}

setup_spotify_autostart() {
  local env_file="$INSTALL_ROOT/.env"
  local enabled_value="$(read_env_value SPOTIFY_AUTOSTART "$env_file")"
  local autostart_dir="$HOME/.config/autostart"
  local desktop_file="$autostart_dir/fxroute-spotify.desktop"
  local legacy_desktop_file="$autostart_dir/spotify-clean-start.desktop"
  local script_path="$INSTALL_ROOT/scripts/spotify-autostart.sh"

  run_as_target_user mkdir -p "$autostart_dir"

  if ! env_setting_enabled "$enabled_value"; then
    run_as_target_user rm -f "$desktop_file"
    pass "Spotify autostart disabled"
    return
  fi

  if ! detect_spotify_autostart_command >/dev/null; then
    run_as_target_user rm -f "$desktop_file"
    warn "Spotify autostart is enabled in .env, but no local Spotify desktop app (Flatpak or native) was found"
    return
  fi

  [[ -f "$script_path" ]] || {
    warn "Spotify autostart could not be enabled because $script_path is missing"
    return
  }

  run_as_target_user chmod +x "$script_path"

  if [[ -f "$legacy_desktop_file" ]] && grep -q "spotify-clean-start.sh" "$legacy_desktop_file"; then
    run_as_target_user rm -f "$legacy_desktop_file"
    pass "legacy Spotify clean-start autostart replaced"
  fi

  run_as_target_user tee "$desktop_file" >/dev/null <<EOF
[Desktop Entry]
Type=Application
Exec=$script_path
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Name=Spotify
Comment=Start Spotify automatically for FXRoute
EOF

  pass "Spotify autostart configured"
}

write_install_state() {
  local state_file="$INSTALL_STATE_FILE"
  local root_state_file="$ROOT_INSTALL_STATE_FILE"
  local temp_file=""
  LAN_HOSTNAME_AFTER="${LAN_HOSTNAME_AFTER:-$(hostname 2>/dev/null || true)}"
  temp_file="$(mktemp /tmp/fxroute-install-state.XXXXXX)"
  if ! cat > "$temp_file" <<EOF
  {
  "install_root": "${INSTALL_ROOT}",
  "local_project": $( [[ $LOCAL_PROJECT_MODE -eq 1 ]] && echo true || echo false ),
  "install_user": "${FXROUTE_TARGET_USER}",
  "install_uid": "${FXROUTE_TARGET_UID}",
  "runtime_dir": "${FXROUTE_RUNTIME_DIR}",
  "user_linger_was_enabled": $( [[ $USER_LINGER_WAS_ENABLED -eq 1 ]] && echo true || echo false ),
  "user_linger_enabled_by_fxroute": $( [[ $USER_LINGER_ENABLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
  "audio_group_added_by_fxroute": $( [[ $AUDIO_GROUP_ADDED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
  "journal_group_added_by_fxroute": $( [[ $JOURNAL_GROUP_ADDED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
  "providers": {
    "spotify_desktop": {
      "selected": $( [[ $SELECT_SPOTIFY_DESKTOP -eq 1 ]] && echo true || echo false ),
      "present_before": $( [[ $SPOTIFY_DESKTOP_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
      "install_method": "${SPOTIFY_DESKTOP_INSTALL_METHOD}",
      "installed_version": "${SPOTIFY_DESKTOP_INSTALLED_VERSION}",
      "installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "flatpak_installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "apt_repo_installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "apt_key_installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "apt_repo_sha256": "${SPOTIFY_DESKTOP_REPO_SHA256}",
      "apt_key_fingerprint": "${SPOTIFY_DESKTOP_KEY_FINGERPRINT}"
    },
    "spotifyd": {
      "selected": $( [[ $SELECT_SPOTIFYD -eq 1 ]] && echo true || echo false ),
      "present_before": $( [[ $SPOTIFYD_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
      "installed_by_fxroute": $( [[ $SPOTIFYD_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "service_installed_by_fxroute": $( [[ $SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "config_installed_by_fxroute": $( [[ $SPOTIFYD_CONFIG_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "binary_path": "${SPOTIFYD_BINARY_PATH}",
      "binary_sha256": "${SPOTIFYD_BINARY_SHA256}",
      "service_path": "${SPOTIFYD_SERVICE_PATH}",
      "service_sha256": "${SPOTIFYD_SERVICE_SHA256}",
      "config_path": "${SPOTIFYD_CONFIG_PATH}",
      "source_built": $( [[ $SPOTIFYD_SOURCE_BUILT -eq 1 ]] && echo true || echo false )
    },
    "qobuz": {
      "selected": $( [[ $SELECT_QOBUZ -eq 1 ]] && echo true || echo false ),
      "present_before": $( [[ $QBZD_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
      "installed_by_fxroute": $( [[ $QBZD_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "service_installed_by_fxroute": $( [[ $QBZD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "binary_path": "${QBZD_BINARY_PATH}",
      "binary_sha256": "${QBZD_BINARY_SHA256}",
      "service_path": "${QBZD_SERVICE_PATH}",
      "service_sha256": "${QBZD_SERVICE_SHA256}",
      "volume_mode_before": "${QBZD_VOLUME_MODE_BEFORE}",
      "volume_mode_after": "${QBZD_VOLUME_MODE_AFTER}",
      "volume_mode_changed_by_fxroute": $( [[ $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "qconnect_startup_mode_before": "${QBZD_QCONNECT_STARTUP_MODE_BEFORE}",
      "qconnect_startup_mode_after": "${QBZD_QCONNECT_STARTUP_MODE_AFTER}",
      "qconnect_changed_by_fxroute": $( [[ $QBZD_QCONNECT_CHANGED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "audio_backend_before": "${QBZD_AUDIO_BACKEND_BEFORE}",
      "audio_device_before": "${QBZD_AUDIO_DEVICE_BEFORE}",
      "audio_skip_sink_switch_before": "${QBZD_AUDIO_SKIP_SINK_SWITCH_BEFORE}",
      "audio_changed_by_fxroute": $( [[ $QBZD_AUDIO_CHANGED_BY_FXROUTE -eq 1 ]] && echo true || echo false )
    },
    "tidal": {
      "selected": $( [[ $TIDAL_DEPENDENCY_SELECTED -eq 1 ]] && echo true || echo false ),
      "present_before": $( [[ $TIDAL_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
      "installed_by_fxroute": $( [[ $TIDAL_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "installed_version": "${TIDAL_INSTALLED_VERSION}"
    },
    "privilege_escalation": {
      "installed_by_fxroute": $( [[ $PROVIDER_PRIVILEGE_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "sudoers_sha256": "${PROVIDER_PRIVILEGE_SUDOERS_SHA256}"
    }
  },
  "dsp": {
    "engine": "native",
    "ingress_sink": "fxroute_dsp_sink"
  },
  "lan_comfort": {
    "hostname_before": "$LAN_HOSTNAME_BEFORE",
    "hostname_after": "$LAN_HOSTNAME_AFTER",
    "hostname_changed_by_fxroute": $( [[ $LAN_HOSTNAME_CHANGED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "avahi_was_present_before": $( [[ $AVAHI_WAS_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
    "avahi_was_active_before": $( [[ $AVAHI_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "avahi_was_enabled_before": $( [[ $AVAHI_WAS_ENABLED_BEFORE -eq 1 ]] && echo true || echo false ),
    "avahi_installed_by_fxroute": $( [[ $AVAHI_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "avahi_enabled_by_fxroute": $( [[ $AVAHI_ENABLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "avahi_ipv4_mdns_configured_by_fxroute": $( [[ $AVAHI_IPV4_MDNS_CONFIGURED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "avahi_ipv4_mdns_config_backed_up": $( [[ $AVAHI_IPV4_MDNS_CONFIG_BACKED_UP -eq 1 ]] && echo true || echo false ),
    "caddy_was_present_before": $( [[ $CADDY_WAS_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
    "caddy_service_was_active_before": $( [[ $CADDY_SERVICE_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "caddy_installed_by_fxroute": $( [[ $CADDY_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "default_caddy_disabled_by_fxroute": $( [[ $DEFAULT_CADDY_DISABLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "caddy_proxy_enabled": $( [[ $CADDY_PROXY_ENABLED -eq 1 ]] && echo true || echo false ),
    "caddy_cert_path": "${CADDY_CERT_PATH}",
    "caddy_service_sha256": "${CADDY_SERVICE_SHA256}",
    "caddy_config_sha256": "${CADDY_CONFIG_SHA256}",
    "caddy_cert_sha256": "${CADDY_CERT_SHA256}",
    "caddy_data_dir_created_by_fxroute": $( [[ $CADDY_DATA_DIR_CREATED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "cifs_helper_installed_by_fxroute": $( [[ $CIFS_HELPER_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "cifs_sudoers_rule_installed_by_fxroute": $( [[ $CIFS_SUDOERS_RULE_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "cifs_sudoers_sha256": "${CIFS_SUDOERS_SHA256}",
    "system_update_owned_by_fxroute": $( [[ $SYSTEM_UPDATE_OWNED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "system_update_service_sha256": "${SYSTEM_UPDATE_SERVICE_SHA256}",
    "system_update_timer_sha256": "${SYSTEM_UPDATE_TIMER_SHA256}",
    "mdns_guard_enabled": $( [[ $MDNS_GUARD_ENABLED -eq 1 ]] && echo true || echo false ),
    "mdns_guard_owned_by_fxroute": $( [[ $MDNS_GUARD_OWNED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "mdns_guard_script_sha256": "${MDNS_GUARD_SCRIPT_SHA256}",
    "mdns_guard_service_sha256": "${MDNS_GUARD_SERVICE_SHA256}",
    "mdns_guard_timer_sha256": "${MDNS_GUARD_TIMER_SHA256}",
    "mdns_guard_target_uid": "${MDNS_GUARD_TARGET_UID}",
    "firewalld_was_active_before": $( [[ $FIREWALLD_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_was_allowed_before": $( [[ $HTTP_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "https_was_allowed_before": $( [[ $HTTPS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "mdns_was_allowed_before": $( [[ $MDNS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_opened_by_fxroute": $( [[ $HTTP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "https_opened_by_fxroute": $( [[ $HTTPS_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "mdns_opened_by_fxroute": $( [[ $MDNS_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "firewall_ownership_schema": 3,
    "firewalld_rule_format": "${FIREWALLD_RULE_FORMAT}",
    "legacy_firewall_ownership_present": $( [[ $FIREWALL_LEGACY_STATE_PRESENT -eq 1 ]] && echo true || echo false ),
    "firewalld_owned_rules": {
      "http_80_tcp": $( [[ $FIREWALLD_HTTP_80_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "https_443_tcp": $( [[ $FIREWALLD_HTTPS_443_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "mdns_5353_udp": $( [[ $FIREWALLD_MDNS_5353_UDP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "fxroute_http_8000_tcp": $( [[ $FIREWALLD_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "spotifyd_zeroconf_4444_tcp": $( [[ $FIREWALLD_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false )
    },
    "ufw_owned_rules": {
      "http_80_tcp": $( [[ $UFW_HTTP_80_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "https_443_tcp": $( [[ $UFW_HTTPS_443_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "mdns_5353_udp": $( [[ $UFW_MDNS_5353_UDP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "fxroute_http_8000_tcp": $( [[ $UFW_FXROUTE_HTTP_8000_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "spotifyd_zeroconf_4444_tcp": $( [[ $UFW_SPOTIFYD_ZEROCONF_4444_TCP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false )
    },
    "power_polkit_installed": $( [[ $POWER_POLKIT_INSTALLED -eq 1 ]] && echo true || echo false ),
    "power_polkit_rule_path": "${POWER_POLKIT_RULE_PATH}",
    "power_polkit_backup_sha256": "${POWER_POLKIT_BACKUP_SHA256}",
    "power_polkit_rule_sha256": "${POWER_POLKIT_RULE_SHA256}",
    "power_polkit_rule_pre_existed": $( [[ $POWER_POLKIT_RULE_PRE_EXISTED -eq 1 ]] && echo true || echo false )
  }
}
EOF
  then
    rm -f "$temp_file"
    return 1
  fi
  chmod 644 "$temp_file"
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Non-interactive provider path: the unprivileged run writes the user
    # state below; the root-owned mirror is a fixed helper action (source
    # path fixed, uid validated inside the helper).
    :
  elif ! "${SUDO_CMD[@]}" install -d -o root -g root -m 755 "$(dirname "$root_state_file")" \
    || ! "${SUDO_CMD[@]}" install -o root -g root -m 644 "$temp_file" "$root_state_file"; then
    rm -f "$temp_file"
    return 1
  fi
  if ! run_as_target_user mkdir -p "$(dirname "$state_file")" \
    || ! run_as_target_user install -m 600 "$temp_file" "$state_file"; then
    rm -f "$temp_file"
    return 1
  fi
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    # Mirror the user state to the root-owned state dir through the fixed
    # helper action. Best effort: a failed mirror must not fail the
    # provider install (the user state is authoritative for the UI).
    provider_privileged state-mirror "$FXROUTE_TARGET_UID" >/dev/null 2>&1 \
      || warn "Root-owned install state mirror skipped; user state is authoritative"
  fi
  rm -f "$temp_file"
  pass "install state recorded"
}

write_install_config() {
  local install_user=""
  local temp_file=""
  install_user="$FXROUTE_TARGET_USER"
  temp_file="$(mktemp /tmp/fxroute-install-config.XXXXXX)"
  if ! cat > "$temp_file" <<EOF
FXROUTE_INSTALL_ROOT=$INSTALL_ROOT
FXROUTE_SERVICE_NAME=$SERVICE_NAME
FXROUTE_INSTALL_STATE=$INSTALL_STATE_FILE
FXROUTE_POWER_USER=$install_user
EOF
  then
    rm -f "$temp_file"
    return 1
  fi
  chmod 644 "$temp_file"
  if ! run_as_target_user mkdir -p "$(dirname "$INSTALL_CONFIG_FILE")" \
    || ! run_as_target_user install -m 600 "$temp_file" "$INSTALL_CONFIG_FILE"; then
    rm -f "$temp_file"
    return 1
  fi
  rm -f "$temp_file"
  pass "install config recorded"
}

checkpoint_install_state_on_exit() {
  local exit_status="$?"
  cleanup_active_temp_dir
  if [[ "$exit_status" -ne 0 && "$STATE_CHECKPOINT_ENABLED" -eq 1 ]]; then
    write_install_config >/dev/null 2>&1 || true
    write_install_state >/dev/null 2>&1 || true
  fi
  ensure_target_user_ownership >/dev/null 2>&1 || true
}

write_user_helper() {
  local path="$1"
  local temp_file=""

  temp_file="$(run_as_target_user mktemp "$(dirname "$path")/.fxroute-helper.XXXXXX")"
  if ! run_as_target_user tee "$temp_file" >/dev/null; then
    run_as_target_user rm -f "$temp_file"
    die "Could not stage FXRoute helper $path"
  fi
  if [[ -e "$path" || -L "$path" ]]; then
    if [[ ! -f "$path" || -L "$path" ]] || ! run_as_target_user cmp -s "$path" "$temp_file"; then
      run_as_target_user rm -f "$temp_file"
      die "Refusing to overwrite a non-FXRoute-owned helper: $path"
    fi
  fi
  run_as_target_user chmod 755 "$temp_file"
  if ! run_as_target_user mv -f "$temp_file" "$path"; then
    run_as_target_user rm -f "$temp_file"
    die "Could not install FXRoute helper $path"
  fi
}

install_helpers() {
  local bin_dir="$HOME/.local/bin"
  run_as_target_user mkdir -p "$bin_dir"

  write_user_helper "$bin_dir/fxroute-status" <<EOF
#!/usr/bin/env bash
exec systemctl --user status $SERVICE_NAME
EOF
  write_user_helper "$bin_dir/fxroute-logs" <<EOF
#!/usr/bin/env bash
exec journalctl --user -u $SERVICE_NAME -f
EOF
  write_user_helper "$bin_dir/fxroute-restart" <<EOF
#!/usr/bin/env bash
exec systemctl --user restart $SERVICE_NAME
EOF
  write_user_helper "$bin_dir/fxroute-update" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/scripts/update_fxroute.sh" "\$@"
EOF
  write_user_helper "$bin_dir/fxroute-update-ytdlp" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/.venv/bin/pip" install -U yt-dlp
EOF
  pass "helper commands installed in $bin_dir"
}

build_native_dsp_engine() {
  local build_script="$INSTALL_ROOT/native_dsp/build.sh"
  local binary="$INSTALL_ROOT/native_dsp/build/fxroute-dsp"
  local dsp_packages=()

  [[ -f "$build_script" ]] || die "Missing FXRoute native DSP build script: $build_script"
  prepare_target_user_directory "$INSTALL_ROOT/native_dsp/build"
  case "$PACKAGE_MANAGER" in
    apt) dsp_packages=(gcc libc6-dev pkg-config libpipewire-0.3-dev libspa-0.2-dev liblilv-dev lilv-utils lv2-dev lsp-plugins-lv2 zam-plugins calf-plugins libebur128-dev libsamplerate0-dev libspeexdsp-dev) ;;
    dnf) dsp_packages=(gcc pkgconf-pkg-config pipewire-devel lilv lilv-devel lv2-devel lsp-plugins-lv2 lv2-zam-plugins lv2-calf-plugins libebur128-devel libsamplerate-devel speexdsp-devel) ;;
    zypper) dsp_packages=(gcc gcc-c++ cmake pkgconf-pkg-config pipewire-devel lilv liblilv-0-devel lv2-devel lv2-lsp-plugins lv2-zam-plugins libebur128-devel libsamplerate-devel speexdsp-devel libexpat-devel fluidsynth-devel) ;;
    pacman) dsp_packages=(gcc pkgconf libpipewire lilv lilv-tools lv2 lsp-plugins zam-plugins calf libebur128 libsamplerate speexdsp) ;;
  esac
  # On Debian 13 the backported PipeWire 1.4.9 runtime needs its matching
  # -dev packages from the same suite; the trixie/main 1.4.2 -dev packages
  # conflict with the backported runtime libraries. Install them from
  # trixie-backports only when that runtime is actually active.
  if [[ "$PACKAGE_MANAGER" == "apt" ]] && debian_trixie_backports_active; then
    local dsp_backports_packages=()
    local dsp_main_packages=()
    local dsp_package=""
    for dsp_package in "${dsp_packages[@]}"; do
      case "$dsp_package" in
        libpipewire-0.3-dev|libspa-0.2-dev) dsp_backports_packages+=("$dsp_package") ;;
        *) dsp_main_packages+=("$dsp_package") ;;
      esac
    done
    [[ ${#dsp_main_packages[@]} -eq 0 ]] || pkg_install "${dsp_main_packages[@]}"
    if [[ ${#dsp_backports_packages[@]} -gt 0 ]]; then
      if [[ $PKG_REFRESH_DONE -eq 0 ]]; then
        run_cmd "${SUDO_CMD[@]}" apt-get update
        PKG_REFRESH_DONE=1
      fi
      run_cmd "${SUDO_CMD[@]}" apt-get install -y -t trixie-backports "${dsp_backports_packages[@]}"
    fi
  else
    [[ ${#dsp_packages[@]} -eq 0 ]] || pkg_install "${dsp_packages[@]}"
  fi

  if [[ "$PACKAGE_MANAGER" == "zypper" ]] && ! lv2_plugin_available 'http://calf.sourceforge.net/plugins/BassEnhancer'; then
    install_calf_lv2_from_source
  fi

  log "Building FXRoute native DSP engine"
  run_as_target_user bash "$build_script"
  [[ -x "$binary" ]] || die "Native DSP build did not produce $binary"
  pass "FXRoute native DSP engine built"
}

install_calf_lv2_from_source() {
  local version="0.90.9"
  local checksum="2d304eed88e87438b2b8857a2f4480046bf4003bce2e17a042abdbbf7d59122f"
  local work archive source build stage bundle binary candidate previous
  work="$(mktemp -d -t fxroute-calf.XXXXXX)"
  archive="$work/calf.tar.gz"
  source="$work/calf-$version"
  build="$work/build"
  stage="$work/stage"
  FXROUTE_ACTIVE_TEMP_DIR="$work"
  trap 'rm -rf "${work:-}"' RETURN

  run_cmd curl -fL --retry 3 -o "$archive" "https://github.com/calf-studio-gear/calf/archive/$version.tar.gz"
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum -c -
  run_cmd tar -xzf "$archive" -C "$work"
  run_cmd cmake -S "$source" -B "$build" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
    -DLV2DIR=/usr/lib64/lv2 -DWANT_GUI=OFF -DWANT_JACK=OFF \
    -DWANT_LASH=OFF -DWANT_SORDI=OFF
  run_cmd cmake --build "$build" --parallel
  run_cmd env DESTDIR="$stage" cmake --install "$build"
  bundle="$stage/usr/lib64/lv2/calf.lv2"
  binary="$stage/usr/lib64/calf/libcalf.so"
  [[ -d "$bundle" && -f "$binary" ]] || die "Calf LV2 source build did not produce the expected bundle"
  rm -f "$bundle/calf.so"
  cp "$binary" "$bundle/calf.so"
  chmod -R a+rX "$work"
  run_as_target_user mkdir -p "$HOME/.lv2"
  candidate="$HOME/.lv2/.calf.lv2.new.$$"
  previous="$HOME/.lv2/.calf.lv2.old.$$"
  run_as_target_user rm -rf "$candidate" "$previous"
  run_as_target_user cp -a "$bundle" "$candidate"
  if [[ -e "$HOME/.lv2/calf.lv2" ]]; then
    run_as_target_user mv "$HOME/.lv2/calf.lv2" "$previous"
  fi
  if run_as_target_user mv "$candidate" "$HOME/.lv2/calf.lv2"; then
    run_as_target_user rm -rf "$previous"
  else
    [[ ! -e "$previous" ]] || run_as_target_user mv "$previous" "$HOME/.lv2/calf.lv2"
    die "Failed to install the staged Calf LV2 bundle"
  fi
  lv2_plugin_available 'http://calf.sourceforge.net/plugins/BassEnhancer' \
    || die "Calf Bass Enhancer is unavailable after source installation"
  rm -rf "$work"
  FXROUTE_ACTIVE_TEMP_DIR=""
  trap - RETURN
  pass "Calf Bass Enhancer LV2 installed"
}

configure_spotify_cache_cleanup_helper() {
  local env_file="$INSTALL_ROOT/.env"
  local user_systemd_dir="$HOME/.config/systemd/user"
  local service_name="fxroute-spotify-cache-cleanup.service"
  local timer_name="fxroute-spotify-cache-cleanup.timer"
  local script_path="$INSTALL_ROOT/scripts/spotify-cache-cleanup.sh"
  local enabled_value="$(read_env_value SPOTIFY_CACHE_CLEANUP "$env_file")"
  local interval_value="$(read_env_value SPOTIFY_CACHE_CLEANUP_INTERVAL_HOURS "$env_file")"
  local interval_hours="$(env_interval_hours_or_default "$interval_value" 24)"

  run_as_target_user mkdir -p "$user_systemd_dir"

  if ! env_setting_enabled "$enabled_value"; then
    user_systemctl disable --now "$timer_name" >/dev/null 2>&1 || true
    run_as_target_user rm -f "$user_systemd_dir/$service_name" "$user_systemd_dir/$timer_name"
    user_systemctl daemon-reload >/dev/null 2>&1 || true
    pass "Spotify cache cleanup helper disabled"
    return
  fi

  [[ -f "$script_path" ]] || {
    warn "Spotify cache cleanup helper could not be enabled because $script_path is missing"
    return
  }

  run_as_target_user chmod +x "$script_path"

  run_as_target_user tee "$user_systemd_dir/$service_name" >/dev/null <<EOF
[Unit]
Description=FXRoute Spotify cache cleanup
After=default.target

[Service]
Type=oneshot
ExecStart=$script_path
EOF

  run_as_target_user tee "$user_systemd_dir/$timer_name" >/dev/null <<EOF
[Unit]
Description=Run FXRoute Spotify cache cleanup periodically

[Timer]
OnBootSec=20min
OnUnitActiveSec=${interval_hours}h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  if user_systemctl daemon-reload && user_systemctl enable --now "$timer_name"; then
    pass "Spotify cache cleanup helper enabled (${interval_hours}h)"
  else
    warn "Spotify cache cleanup helper files were installed, but the timer could not be enabled in this shell"
  fi
}

system_update_units_are_owned() {
  local service_path="/etc/systemd/system/fxroute-system-update.service"
  local timer_path="/etc/systemd/system/fxroute-system-update.timer"

  if [[ ! -e "$service_path" && ! -L "$service_path" \
    && ! -e "$timer_path" && ! -L "$timer_path" ]]; then
    return 0
  fi
  if [[ -e "$service_path" || -L "$service_path" ]]; then
    [[ "$SYSTEM_UPDATE_OWNED_BY_FXROUTE" -eq 1 && -n "$SYSTEM_UPDATE_SERVICE_SHA256" \
      && -f "$service_path" && ! -L "$service_path" \
      && "$("${SUDO_CMD[@]}" sha256sum "$service_path" | awk '{print $1}')" == "$SYSTEM_UPDATE_SERVICE_SHA256" ]] \
      || return 1
  fi
  if [[ -e "$timer_path" || -L "$timer_path" ]]; then
    [[ "$SYSTEM_UPDATE_OWNED_BY_FXROUTE" -eq 1 && -n "$SYSTEM_UPDATE_TIMER_SHA256" \
      && -f "$timer_path" && ! -L "$timer_path" \
      && "$("${SUDO_CMD[@]}" sha256sum "$timer_path" | awk '{print $1}')" == "$SYSTEM_UPDATE_TIMER_SHA256" ]] \
      || return 1
  fi
  return 0
}

stop_system_update_units() {
  local service_name="fxroute-system-update.service"
  local timer_name="fxroute-system-update.timer"
  local unit=""
  local load_state=""
  local active_state=""
  local unit_file_state=""

  for unit in "$service_name" "$timer_name"; do
    if ! load_state="$(${SUDO_CMD[@]} systemctl show "$unit" -p LoadState --value 2>/dev/null)"; then
      return 1
    fi
    [[ "$load_state" == "not-found" ]] && continue
    [[ -n "$load_state" ]] || return 1
    if ! active_state="$(${SUDO_CMD[@]} systemctl show "$unit" -p ActiveState --value 2>/dev/null)"; then
      return 1
    fi
    case "$active_state" in
      inactive|failed|dead) ;;
      active|activating|deactivating|reloading)
        "${SUDO_CMD[@]}" systemctl stop "$unit" >/dev/null 2>&1 || return 1
        active_state="$(${SUDO_CMD[@]} systemctl show "$unit" -p ActiveState --value 2>/dev/null)" || return 1
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

  if ! load_state="$(${SUDO_CMD[@]} systemctl show "$timer_name" -p LoadState --value 2>/dev/null)"; then
    return 1
  fi
  [[ "$load_state" == "not-found" ]] && return 0
  "${SUDO_CMD[@]}" systemctl disable "$timer_name" >/dev/null 2>&1 || true
  unit_file_state="$(${SUDO_CMD[@]} systemctl show "$timer_name" -p UnitFileState --value 2>/dev/null)" || return 1
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
  local service_name="fxroute-system-update.service"
  local timer_name="fxroute-system-update.timer"
  local service_path="/etc/systemd/system/$service_name"
  local timer_path="/etc/systemd/system/$timer_name"
  local helper_path="/usr/local/sbin/fxroute-system-package-update"
  local service_present="$2"
  local timer_present="$3"
  local helper_present="$4"
  local service_was_active="$5"
  local timer_was_enabled="$6"
  local timer_was_active="$7"
  local restore_failed=0

  "${SUDO_CMD[@]}" systemctl disable --now "$timer_name" >/dev/null 2>&1 || true
  "${SUDO_CMD[@]}" systemctl stop "$service_name" >/dev/null 2>&1 || true

  if [[ "$service_present" -eq 1 ]]; then
    "${SUDO_CMD[@]}" install -m 644 "$backup_dir/service" "$service_path" || restore_failed=1
  else
    "${SUDO_CMD[@]}" rm -f "$service_path" || restore_failed=1
  fi
  if [[ "$timer_present" -eq 1 ]]; then
    "${SUDO_CMD[@]}" install -m 644 "$backup_dir/timer" "$timer_path" || restore_failed=1
  else
    "${SUDO_CMD[@]}" rm -f "$timer_path" || restore_failed=1
  fi
  if [[ "$helper_present" -eq 1 ]]; then
    "${SUDO_CMD[@]}" install -m 755 "$backup_dir/helper" "$helper_path" || restore_failed=1
  else
    "${SUDO_CMD[@]}" rm -f "$helper_path" || restore_failed=1
  fi
  "${SUDO_CMD[@]}" systemctl daemon-reload >/dev/null 2>&1 || restore_failed=1

  if [[ "$service_was_active" -eq 1 ]]; then
    "${SUDO_CMD[@]}" systemctl start "$service_name" >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ "$timer_was_enabled" -eq 1 ]]; then
    if [[ "$timer_was_active" -eq 1 ]]; then
      "${SUDO_CMD[@]}" systemctl enable --now "$timer_name" >/dev/null 2>&1 || restore_failed=1
    else
      "${SUDO_CMD[@]}" systemctl enable "$timer_name" >/dev/null 2>&1 || restore_failed=1
    fi
  elif [[ "$timer_was_active" -eq 1 ]]; then
    "${SUDO_CMD[@]}" systemctl start "$timer_name" >/dev/null 2>&1 || restore_failed=1
  fi

  return "$restore_failed"
}

configure_system_auto_update_helper() {
  local env_file="$INSTALL_ROOT/.env"
  local service_name="fxroute-system-update.service"
  local timer_name="fxroute-system-update.timer"
  local service_path="/etc/systemd/system/$service_name"
  local timer_path="/etc/systemd/system/$timer_name"
  local helper_path="/usr/local/sbin/fxroute-system-package-update"
  local script_path="$INSTALL_ROOT/scripts/system-package-update.sh"
  local enabled_value="$(read_env_value SYSTEM_AUTO_UPDATE "$env_file")"
  local interval_value="$(read_env_value SYSTEM_AUTO_UPDATE_INTERVAL_HOURS "$env_file")"
  local interval_hours="$(env_interval_hours_or_default "$interval_value" 24)"
  local tmp_service=""
  local tmp_timer=""
  local tmp_helper=""
  local helper_sha256=""
  local backup_dir=""
  local service_present=0
  local timer_present=0
  local helper_present=0
  local service_was_active=0
  local timer_was_enabled=0
  local timer_was_active=0
  local load_state=""
  local unit_state=""

  if ! env_setting_enabled "$enabled_value"; then
    if ! system_update_units_are_owned; then
      warn "Refusing to remove non-FXRoute-owned system auto-update units"
      return
    fi
    if [[ -e "$helper_path" || -L "$helper_path" ]]; then
      if [[ "$SYSTEM_UPDATE_OWNED_BY_FXROUTE" -ne 1 || ! -f "$helper_path" || -L "$helper_path" \
        || "$("${SUDO_CMD[@]}" sha256sum "$helper_path" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
        warn "Refusing to remove an unverified system auto-update helper"
        return
      fi
      helper_present=1
    fi
  else
    if ! system_update_units_are_owned; then
      warn "Refusing to overwrite non-FXRoute-owned system auto-update units"
      return
    fi
    if [[ -e "$helper_path" || -L "$helper_path" ]]; then
      if [[ ! -f "$helper_path" || -L "$helper_path" \
      || "$("${SUDO_CMD[@]}" sha256sum "$helper_path" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
        warn "Refusing to overwrite a non-FXRoute-owned system auto-update helper"
        return
      fi
      if [[ "$SYSTEM_UPDATE_OWNED_BY_FXROUTE" -ne 1 ]]; then
        warn "Refusing to take ownership of a pre-existing system auto-update helper"
        return
      fi
      helper_present=1
    fi

    [[ -f "$script_path" && ! -L "$script_path" ]] || {
      warn "System auto-update helper could not be enabled because $script_path is missing"
      return
    }
    helper_sha256="$(sha256sum "$script_path" | awk '{print $1}')"
    [[ "$helper_sha256" == "$SYSTEM_UPDATE_HELPER_SHA256" ]] || {
      warn "System auto-update helper could not be enabled because its source is unverified"
      return
    }
  fi

  [[ -f "$service_path" ]] && service_present=1
  [[ -f "$timer_path" ]] && timer_present=1
  [[ -f "$helper_path" ]] && helper_present=1
  if ! load_state="$(${SUDO_CMD[@]} systemctl show "$service_name" -p LoadState --value 2>/dev/null)" \
    || [[ -z "$load_state" ]]; then
    warn "Could not verify the system auto-update service state"
    return
  fi
  if [[ "$load_state" != "not-found" ]]; then
    if ! unit_state="$(${SUDO_CMD[@]} systemctl show "$service_name" -p ActiveState --value 2>/dev/null)" \
      || [[ -z "$unit_state" ]]; then
      warn "Could not verify the system auto-update service state"
      return
    fi
    case "$unit_state" in
      active|activating|deactivating|reloading) service_was_active=1 ;;
    esac
  fi
  if ! load_state="$(${SUDO_CMD[@]} systemctl show "$timer_name" -p LoadState --value 2>/dev/null)" \
    || [[ -z "$load_state" ]]; then
    warn "Could not verify the system auto-update timer state"
    return
  fi
  if [[ "$load_state" != "not-found" ]]; then
    if ! unit_state="$(${SUDO_CMD[@]} systemctl show "$timer_name" -p UnitFileState --value 2>/dev/null)" \
      || [[ -z "$unit_state" ]]; then
      warn "Could not verify the system auto-update timer state"
      return
    fi
    case "$unit_state" in
      enabled|enabled-runtime|linked|linked-runtime|alias) timer_was_enabled=1 ;;
    esac
    if ! unit_state="$(${SUDO_CMD[@]} systemctl show "$timer_name" -p ActiveState --value 2>/dev/null)" \
      || [[ -z "$unit_state" ]]; then
      warn "Could not verify the system auto-update timer state"
      return
    fi
    case "$unit_state" in
      active|activating|deactivating|reloading) timer_was_active=1 ;;
    esac
  fi
  if ! backup_dir="$(${SUDO_CMD[@]} mktemp -d -t fxroute-system-update-backup.XXXXXX)"; then
    warn "Could not create a rollback area for the system auto-update helper"
    return
  fi
  if [[ $service_present -eq 1 ]] && ! "${SUDO_CMD[@]}" cp -p "$service_path" "$backup_dir/service"; then
    warn "Could not back up the system auto-update service before changing it"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  if [[ $timer_present -eq 1 ]] && ! "${SUDO_CMD[@]}" cp -p "$timer_path" "$backup_dir/timer"; then
    warn "Could not back up the system auto-update timer before changing it"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  if [[ $helper_present -eq 1 ]] && ! "${SUDO_CMD[@]}" cp -p "$helper_path" "$backup_dir/helper"; then
    warn "Could not back up the system auto-update helper before changing it"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi

  if ! env_setting_enabled "$enabled_value"; then
    if ! stop_system_update_units; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        || warn "Could not fully restore the system auto-update state"
      warn "Refusing to remove system auto-update units that could not be stopped safely"
      "${SUDO_CMD[@]}" rm -rf "$backup_dir"
      return
    fi
    if ! "${SUDO_CMD[@]}" rm -f "$service_path" "$timer_path"; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        || warn "Could not fully restore the system auto-update state"
      warn "Could not remove optional system auto-update unit files"
      "${SUDO_CMD[@]}" rm -rf "$backup_dir"
      return
    fi
    if [[ -e "$helper_path" || -L "$helper_path" ]]; then
      if [[ "$SYSTEM_UPDATE_OWNED_BY_FXROUTE" -ne 1 || ! -f "$helper_path" || -L "$helper_path" \
        || "$("${SUDO_CMD[@]}" sha256sum "$helper_path" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
        restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
          "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
          || warn "Could not fully restore the system auto-update state"
        warn "Refusing to remove a non-FXRoute-owned system auto-update helper"
        "${SUDO_CMD[@]}" rm -rf "$backup_dir"
        return
      fi
      if ! "${SUDO_CMD[@]}" rm -f "$helper_path"; then
        restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
          "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
          || warn "Could not fully restore the system auto-update state"
        warn "Could not remove the system auto-update helper"
        "${SUDO_CMD[@]}" rm -rf "$backup_dir"
        return
      fi
    fi
    if ! "${SUDO_CMD[@]}" systemctl daemon-reload >/dev/null 2>&1; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        || warn "Could not fully restore the system auto-update state"
      warn "Could not reload systemd after disabling the system auto-update helper"
      "${SUDO_CMD[@]}" rm -rf "$backup_dir"
      return
    fi
    SYSTEM_UPDATE_OWNED_BY_FXROUTE=0
    SYSTEM_UPDATE_SERVICE_SHA256=""
    SYSTEM_UPDATE_TIMER_SHA256=""
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    pass "Optional system auto-update helper disabled"
    return
  fi

  if ! stop_system_update_units; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Refusing to overwrite system auto-update units that could not be stopped safely"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi

  if [[ -e "$helper_path" || -L "$helper_path" ]]; then
    if [[ ! -f "$helper_path" || -L "$helper_path" \
      || "$("${SUDO_CMD[@]}" sha256sum "$helper_path" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        || warn "Could not fully restore the system auto-update state"
      warn "Refusing to overwrite a changed system auto-update helper"
      "${SUDO_CMD[@]}" rm -rf "$backup_dir"
      return
    fi
    if [[ "$SYSTEM_UPDATE_OWNED_BY_FXROUTE" -ne 1 ]]; then
      restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
        "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
        || warn "Could not fully restore the system auto-update state"
      warn "Refusing to take ownership of a pre-existing system auto-update helper"
      "${SUDO_CMD[@]}" rm -rf "$backup_dir"
      return
    fi
  fi

  if ! tmp_helper="$(mktemp)"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Could not create a staging file for the system auto-update helper"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  if ! "${SUDO_CMD[@]}" install -m 600 "$script_path" "$tmp_helper" \
    || [[ "$("${SUDO_CMD[@]}" sha256sum "$tmp_helper" | awk '{print $1}')" != "$SYSTEM_UPDATE_HELPER_SHA256" ]]; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "System auto-update helper could not be installed in the root-owned system path"
    "${SUDO_CMD[@]}" rm -f "$tmp_helper"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  if ! "${SUDO_CMD[@]}" install -m 755 "$tmp_helper" "$helper_path"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "System auto-update helper could not be installed in the root-owned system path"
    "${SUDO_CMD[@]}" rm -f "$tmp_helper"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  "${SUDO_CMD[@]}" rm -f "$tmp_helper"
  if ! tmp_service="$(mktemp)"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Could not create a staging file for the system auto-update service"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  if ! tmp_timer="$(mktemp)"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Could not create a staging file for the system auto-update timer"
    rm -f "$tmp_service"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi

  if ! cat > "$tmp_service" <<EOF
[Unit]
Description=FXRoute optional system package update helper
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$helper_path
EOF
  then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Could not stage the system auto-update service"
    rm -f "$tmp_service" "$tmp_timer"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi

  if ! cat > "$tmp_timer" <<EOF
[Unit]
Description=Run FXRoute optional system package updates periodically

[Timer]
OnBootSec=30min
OnUnitActiveSec=${interval_hours}h
Persistent=true

[Install]
WantedBy=timers.target
EOF
  then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Could not stage the system auto-update timer"
    rm -f "$tmp_service" "$tmp_timer"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi

  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_service" "$service_path"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Failed to install optional system auto-update service"
    rm -f "$tmp_service" "$tmp_timer"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_timer" "$timer_path"; then
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Failed to install optional system auto-update timer"
    rm -f "$tmp_service" "$tmp_timer"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    return
  fi
  rm -f "$tmp_service" "$tmp_timer"
  SYSTEM_UPDATE_SERVICE_SHA256="$("${SUDO_CMD[@]}" sha256sum "$service_path" | awk '{print $1}')"
  SYSTEM_UPDATE_TIMER_SHA256="$("${SUDO_CMD[@]}" sha256sum "$timer_path" | awk '{print $1}')"
  SYSTEM_UPDATE_OWNED_BY_FXROUTE=1

  if "${SUDO_CMD[@]}" systemctl daemon-reload && "${SUDO_CMD[@]}" systemctl enable --now "$timer_name"; then
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
    pass "Optional system auto-update helper enabled (${interval_hours}h)"
  else
    restore_system_update_transaction "$backup_dir" "$service_present" "$timer_present" \
      "$helper_present" "$service_was_active" "$timer_was_enabled" "$timer_was_active" \
      || warn "Could not fully restore the system auto-update state"
    warn "Optional system auto-update helper could not be activated; previous state restored"
    "${SUDO_CMD[@]}" rm -rf "$backup_dir"
  fi
}

configure_optional_maintenance_helpers() {
  configure_spotify_cache_cleanup_helper
  configure_system_auto_update_helper
}

validate_http() {
  local env_file="$INSTALL_ROOT/.env"
  local port
  local service_pid=""
  local service_state=""
  local port_listing=""
  port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  [[ -n "$port" ]] || port=8000

  # A cold start on a fresh install (venv bytecode compile, HDA init, first
  # PipeWire graph build) can take well over 30 seconds on slow hosts.
  # FXROUTE_HTTP_VALIDATION_DEADLINE exists for tests to shorten the wait.
  local deadline=$((SECONDS + ${FXROUTE_HTTP_VALIDATION_DEADLINE:-120}))
  while (( SECONDS < deadline )); do
    service_pid="$(user_systemctl show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
    service_state="$(user_systemctl show -p ActiveState --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
    if [[ -n "$service_pid" && "$service_pid" != "0" && "$service_state" == "active" ]]; then
      port_listing="$(ss -ltnp 2>/dev/null | grep -E ":${port}\\b" || true)"
      if grep -q "pid=${service_pid}," <<<"$port_listing"; then
        pass "HTTP port owned by FXRoute service"
        break
      fi
    fi
    sleep 1
  done

  service_pid="$(user_systemctl show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
  service_state="$(user_systemctl show -p ActiveState --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
  if [[ -z "$service_pid" || "$service_pid" == "0" || "$service_state" != "active" ]]; then
    fail "FXRoute service running"
    warn "FXRoute user service is not active after install; check: systemctl --user status ${SERVICE_NAME}.service"
    return 0
  fi

  port_listing="$(ss -ltnp 2>/dev/null | grep -E ":${port}\\b" || true)"
  if ! grep -q "pid=${service_pid}," <<<"$port_listing"; then
    fail "HTTP port owned by FXRoute service"
    warn "Port ${port} is not owned by ${SERVICE_NAME}.service MainPID ${service_pid}; another process may be answering health checks"
    [[ -n "$port_listing" ]] && warn "Port ${port} listeners: ${port_listing//$'\n'/; }"
    # Explicit success return: a bare `return` here would inherit the status
    # of the `[[ ... ]] && warn` line and, under `set -e`, silently abort the
    # whole installer right after reporting this validation failure.
    return 0
  fi
  if [[ "$port" == "8000" ]]; then
    ensure_lan_firewall_service_open fxroute-http "FXRoute HTTP LAN access"
  fi

  if curl -fsS "http://127.0.0.1:${port}/api/status" >/dev/null 2>&1; then
    pass "HTTP health response"
  else
    fail "HTTP health response"
    warn "FXRoute did not answer on http://127.0.0.1:${port}/api/status yet"
  fi
}

lv2_plugin_available() {
  # grep -q must not be fed from a live pipe under `set -o pipefail`:
  # grep exits as soon as it sees the match, the producer gets SIGPIPE and
  # the pipeline reports 141 even though the plugin is present. Capture the
  # full listing first (same pattern as verify_lv2_plugins).
  local uri="$1"
  local discovered=""
  discovered="$(lv2ls 2>/dev/null || true)"
  grep -Fxq "$uri" <<<"$discovered"
}

verify_lv2_plugins() {
  local required_uris=(
    http://lsp-plug.in/plugins/lv2/para_equalizer_x32_lr
    http://lsp-plug.in/plugins/lv2/loud_comp_stereo
    http://lsp-plug.in/plugins/lv2/sc_limiter_stereo
    urn:zamaudio:ZaMaximX2
    http://calf.sourceforge.net/plugins/BassEnhancer
  )
  local missing=()
  local discovered=""
  local uri=""

  if ! command -v lv2ls >/dev/null 2>&1; then
    fail "LV2 plugin discovery tool (lv2ls) available"
    die "LV2 plugin verification needs lv2ls from lilv-utils (Debian/Ubuntu), lilv (Fedora/openSUSE), or lilv-tools (Arch/Manjaro)"
  fi

  discovered="$(lv2ls 2>/dev/null || true)"
  for uri in "${required_uris[@]}"; do
    if grep -Fxq "$uri" <<<"$discovered"; then
      pass "LV2 plugin available: $uri"
    else
      missing+=("$uri")
    fi
  done

  if [[ ${#missing[@]} -gt 0 ]]; then
    for uri in "${missing[@]}"; do
      fail "LV2 plugin available: $uri"
    done
    die "FXRoute DSP effects need these LV2 plugins: ${missing[*]}; install lsp-plugins-lv2 zam-plugins calf-plugins (Debian/Ubuntu/Armbian), lsp-plugins-lv2 lv2-zam-plugins lv2-calf-plugins (Fedora), lv2-lsp-plugins lv2-zam-plugins (openSUSE), or lsp-plugins zam-plugins calf (Arch/Manjaro)"
  fi
}

validate_tools() {
  local dsp_binary="$(configured_dsp_binary)"
  local dsp_path="$dsp_binary"
  [[ "$dsp_path" == /* ]] || dsp_path="$INSTALL_ROOT/$dsp_path"
  mpv --version >/dev/null 2>&1 && pass "mpv available" || fail "mpv available"
  ffmpeg -version >/dev/null 2>&1 && pass "ffmpeg available" || fail "ffmpeg available"
  playerctl --version >/dev/null 2>&1 && pass "playerctl available" || fail "playerctl available"
  command -v pactl >/dev/null 2>&1 && pass "pactl available" || fail "pactl available"
  command -v wpctl >/dev/null 2>&1 && pass "wpctl available" || fail "wpctl available"
  command -v pw-cli >/dev/null 2>&1 && pass "pw-cli available" || fail "pw-cli available"
  command -v bluetoothctl >/dev/null 2>&1 && pass "bluetoothctl available" || fail "bluetoothctl available"
  for cmd in smbclient mount.cifs gio; do
    command -v "$cmd" >/dev/null 2>&1 && pass "$cmd available" || fail "$cmd available"
  done
  run_as_target_user "$INSTALL_ROOT/.venv/bin/yt-dlp" --version >/dev/null 2>&1 \
    && pass "yt-dlp available from venv" \
    || fail "yt-dlp available from venv"

  [[ -x "$dsp_path" ]] \
    && pass "FXRoute native DSP engine available" \
    || fail "FXRoute native DSP engine available"
  run_as_target_user pactl list sinks short 2>/dev/null | awk '{print $2}' | grep -Fxq fxroute_dsp_sink \
    && pass "FXRoute DSP ingress sink available" \
    || fail "FXRoute DSP ingress sink available"

  if bt_plugin_present; then
    pass "PipeWire BlueZ SPA plugin found"
  else
    fail "PipeWire BlueZ SPA plugin found"
    warn "Bluetooth input mode needs the PipeWire BlueZ SPA plugin (libspa-bluez5.so) on the host."
  fi

  # bluetoothctl show can block forever when bluetoothd is installed but
  # inactive (fresh BlueZ install, no controller). Bound the probe so a
  # host without an active Bluetooth stack cannot stall the installer.
  local bt_probe=()
  if command -v timeout >/dev/null 2>&1; then
    bt_probe=(timeout 15 bluetoothctl show)
  else
    bt_probe=(bluetoothctl show)
  fi
  if "${bt_probe[@]}" >/dev/null 2>&1; then
    pass "BlueZ controller query works"
  else
    warn "bluetoothctl show failed in this shell. Bluetooth input mode will stay unavailable until BlueZ is active and an adapter/controller is visible."
  fi

  if user_systemctl is-enabled "$SERVICE_NAME" >/dev/null 2>&1; then
    pass "service enabled"
  else
    fail "service enabled"
  fi

  verify_lv2_plugins
}

pipewire_link_present() {
  local graph="$1"
  local source="$2"
  local target="$3"

  awk -v source="$source" -v target="$target" '
    {
      line = $0
      trimmed = line
      sub(/^[[:space:]]+/, "", trimmed)
      if (trimmed == "") next
      if (index(trimmed, source " -> " target) > 0) {
        found = 1
        next
      }
      if (substr(trimmed, 1, 1) == "|") {
        if (current == target && index(trimmed, "|<- " source) > 0) found = 1
        if (current == source && index(trimmed, "|-> " target) > 0) found = 1
      } else {
        current = trimmed
      }
    }
    END { exit found ? 0 : 1 }
  ' <<<"$graph"
}

pipewire_port_linked() {
  local graph="$1"
  local port="$2"

  awk -v port="$port" '
    {
      line = $0
      trimmed = line
      sub(/^[[:space:]]+/, "", trimmed)
      if (trimmed == "") next
      if (substr(trimmed, 1, 1) == "|") {
        if (current == port || index(trimmed, "|<- " port) > 0 || index(trimmed, "|-> " port) > 0) found = 1
      } else {
        current = trimmed
      }
    }
    END { exit found ? 0 : 1 }
  ' <<<"$graph"
}

validate_pipewire_session() {
  local failures=()
  local output=""
  local service_pid=""
  local service_user=""
  local dsp_binary="$(configured_dsp_binary)"
  local dsp_path="$dsp_binary"
  [[ "$dsp_path" == /* ]] || dsp_path="$INSTALL_ROOT/$dsp_path"
  local unit_state=""
  local unit=""

  for unit in pipewire.service pipewire-pulse.service wireplumber.service "${SERVICE_NAME}.service"; do
    unit_state="$(user_systemctl show "$unit" -p ActiveState --value 2>/dev/null || true)"
    if [[ "$unit_state" != "active" ]]; then
      failures+=("$unit is not active in the $FXROUTE_TARGET_USER user manager")
    fi
  done

  if [[ ! -S "$FXROUTE_RUNTIME_DIR/pipewire-0" ]]; then
    failures+=("PipeWire socket is missing: $FXROUTE_RUNTIME_DIR/pipewire-0")
  fi
  if [[ ! -S "$FXROUTE_RUNTIME_DIR/pulse/native" ]]; then
    failures+=("PipeWire-Pulse socket is missing: $FXROUTE_RUNTIME_DIR/pulse/native")
  fi

  if ! output="$(run_as_target_user wpctl status 2>&1)"; then
    failures+=("wpctl status failed: ${output//$'\n'/; }")
  elif ! grep -Fq "pipewire-0" <<<"$output"; then
    failures+=("wpctl did not report the target PipeWire remote pipewire-0")
  fi

  if ! output="$(run_as_target_user pw-cli info 0 2>&1)"; then
    failures+=("pw-cli info 0 failed: ${output//$'\n'/; }")
  elif ! grep -Fq 'name: "pipewire-0"' <<<"$output"; then
    failures+=("pw-cli did not report the target PipeWire core")
  fi

  if ! output="$(run_as_target_user pw-link -l 2>&1)"; then
    failures+=("pw-link -l failed: ${output//$'\n'/; }")
  elif ! pipewire_link_present "$output" \
      "fxroute_dsp_sink:monitor_FL" "fxroute_dsp:input_1" \
    || ! pipewire_link_present "$output" \
      "fxroute_dsp_sink:monitor_FR" "fxroute_dsp:input_2" \
    || ! pipewire_port_linked "$output" "fxroute_dsp:output_1" \
    || ! pipewire_port_linked "$output" "fxroute_dsp:output_2"; then
    failures+=("PipeWire DSP graph links are incomplete")
  fi

  if ! output="$(run_as_target_user pactl info 2>&1)"; then
    failures+=("pactl info failed: ${output//$'\n'/; }")
  elif ! grep -Fq "Server String: $FXROUTE_RUNTIME_DIR/pulse/native" <<<"$output" \
    && ! grep -Fq "Server String: unix:$FXROUTE_RUNTIME_DIR/pulse/native" <<<"$output"; then
    failures+=("pactl is not using $FXROUTE_RUNTIME_DIR/pulse/native")
  fi

  if ! output="$(run_as_target_user pactl list sinks short 2>&1)"; then
    failures+=("pactl list sinks short failed: ${output//$'\n'/; }")
  elif ! awk '{print $2}' <<<"$output" | grep -Fxq fxroute_dsp_sink; then
    failures+=("FXRoute DSP ingress sink is not visible in the target PipeWire-Pulse graph")
  fi

  if alsa_hardware_present; then
    if ! output="$(run_as_target_user wpctl status 2>&1)"; then
      failures+=("wpctl status failed: ${output//$'\n'/; }")
    elif ! grep -Fq '[alsa]' <<<"$output"; then
      failures+=("ALSA hardware is present, but the $FXROUTE_TARGET_USER session cannot reach it (audio group or device access missing)")
    fi
  else
    warn "No ALSA sound hardware found; FXRoute is installed, but no hardware output will be selectable until audio hardware is present"
  fi

  service_pid="$(user_systemctl show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
  if [[ -n "$service_pid" && "$service_pid" != "0" ]]; then
    service_user="$(ps -o user= -p "$service_pid" 2>/dev/null | tr -d '[:space:]')"
    [[ "$service_user" == "$FXROUTE_TARGET_USER" ]] \
      || failures+=("FXRoute service PID $service_pid runs as ${service_user:-unknown}, not $FXROUTE_TARGET_USER")
  fi

  if ! ps -eo user=,args= 2>/dev/null \
    | awk -v expected_user="$FXROUTE_TARGET_USER" \
        -v expected_binary="$dsp_binary" -v expected_path="$dsp_path" \
      '$2 !~ /(^|\/)awk$/ && $1 == expected_user && (index($0, expected_binary) > 0 || index($0, expected_path) > 0) { found = 1 } END { exit found ? 0 : 1 }'; then
    failures+=("native DSP engine is not running as $FXROUTE_TARGET_USER: $dsp_binary")
  fi

  if [[ ${#failures[@]} -gt 0 ]]; then
    local reason=""
    for reason in "${failures[@]}"; do
      fail "PipeWire session: $reason"
      warn "PipeWire session validation: $reason"
    done
    die "Functional PipeWire/WirePlumber validation failed for $FXROUTE_TARGET_USER; FXRoute was not installed successfully"
  fi

  pass "functional PipeWire/WirePlumber session verified for $FXROUTE_TARGET_USER ($FXROUTE_RUNTIME_DIR)"
}

print_summary() {
  local env_file="$INSTALL_ROOT/.env"
  local port="8000"
  local lan_ip=""
  [[ -f "$env_file" ]] && port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  lan_ip="$(primary_lan_ip)"

  echo
  if [[ ${#WARNINGS[@]} -eq 0 ]] && ! printf '%s\n' "${VALIDATION_RESULTS[@]}" | grep -q '^FAIL:'; then
    echo "FXRoute installed successfully"
  elif printf '%s\n' "${VALIDATION_RESULTS[@]}" | grep -q '^FAIL:'; then
    echo "FXRoute installed with warnings"
  else
    echo "FXRoute installed with warnings"
  fi

  echo "Install path: $INSTALL_ROOT"
  echo "DSP engine: FXRoute native (ingress: fxroute_dsp_sink)"
  echo "Music folder: $HOME/Music"
  echo "Streaming components:"
  echo " - Spotify Desktop: $SPOTIFY_DESKTOP_PROVIDER_STATUS"
  echo " - spotifyd: $SPOTIFYD_PROVIDER_STATUS"
  echo " - Qobuz/qbzd: $QOBUZ_PROVIDER_STATUS"
  echo " - TIDAL: $TIDAL_PROVIDER_STATUS"
  echo
  echo "Open FXRoute:"
  echo " - Local: http://localhost:${port}"
  [[ -n "$lan_ip" ]] && echo " - LAN IP: http://${lan_ip}:${port}"
  if [[ $CADDY_PROXY_ENABLED -eq 1 ]]; then
    if [[ -n "$lan_ip" ]]; then
      echo " - LAN HTTPS: https://${lan_ip}"
    fi
    if [[ -n "$MDNS_HOSTNAME" ]]; then
      echo " - Optional .local HTTPS: https://${MDNS_HOSTNAME}.local"
    fi
    if [[ -n "$CADDY_CERT_PATH" ]]; then
      echo " - Install this certificate on client devices, then reload the browser: ${CADDY_CERT_PATH}"
    fi
  else
    echo " - Optional LAN HTTPS is available via the Caddy setup step."
  fi
  echo " - If LAN access fails, check the host firewall for TCP ${port}."
  echo
  echo "Service: systemctl --user status $SERVICE_NAME"
  echo "Logs: journalctl --user -u $SERVICE_NAME -f"
  echo "Helpers: fxroute-status, fxroute-logs, fxroute-restart, fxroute-update, fxroute-update-ytdlp"

  if [[ $MDNS_GUARD_ENABLED -eq 1 ]]; then
    echo "mDNS guard: installed (keeps Spotify user-space mDNS from overriding Avahi host advertisement)"
  fi

  if [[ ${#WARNINGS[@]} -gt 0 ]]; then
    echo
    echo "Warnings:"
    printf ' - %s\n' "${WARNINGS[@]}"
  fi

  return 0
}

offer_optional_local_lan_name() {
  local env_file="$INSTALL_ROOT/.env"
  local port="8000"
  local current_host=""
  local reply=""
  local desired_host=""
  local avahi_pkg=""
  local avahi_active=0

  [[ -f "$env_file" ]] && port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  current_host="$(hostname 2>/dev/null || true)"

  if systemctl is-active avahi-daemon >/dev/null 2>&1 && [[ -n "$current_host" ]] && valid_local_hostname "$current_host"; then
    avahi_active=1
    MDNS_HOSTNAME="$current_host"
  fi

  if [[ $AUTO_LAN_NAME -eq 1 ]]; then
    # Image first-boot: skip every prompt and keep a valid current name, or
    # apply the requested/generated one. Never interactive.
    if [[ -n "$AUTO_DEVICE_NAME" ]]; then
      desired_host="${AUTO_DEVICE_NAME,,}"
      valid_local_hostname "$desired_host" || {
        warn "Ignoring invalid --device-name '$AUTO_DEVICE_NAME'"
        return 0
      }
    else
      desired_host="$(valid_local_hostname "$current_host" && printf '%s' "$current_host" || printf 'fxroute')"
    fi
    if [[ "$desired_host" == "$current_host" ]] && systemctl is-active avahi-daemon >/dev/null 2>&1; then
      MDNS_HOSTNAME="$desired_host"
      pass "optional .local LAN name already active (${MDNS_HOSTNAME}.local:${port})"
      return 0
    fi
    case "$PACKAGE_MANAGER" in
      apt) avahi_pkg="avahi-daemon" ;;
      dnf|zypper|pacman) avahi_pkg="avahi" ;;
      *)
        warn "Skipping automatic .local setup on unsupported distro package manager: $PACKAGE_MANAGER"
        return 0
        ;;
    esac
    if ! pkg_install "$avahi_pkg"; then
      warn "Automatic .local setup failed while installing Avahi"
      return 0
    fi
    if [[ $AVAHI_WAS_PRESENT_BEFORE -eq 0 ]] && avahi_is_present; then
      AVAHI_INSTALLED_BY_FXROUTE=1
    fi
    configure_avahi_ipv4_mdns_for_fxroute
    if [[ "$desired_host" != "$current_host" ]]; then
      log "hostnamectl set-hostname $desired_host"
      if ! "${SUDO_CMD[@]}" hostnamectl set-hostname "$desired_host"; then
        warn "Automatic .local setup failed while setting hostname"
        return 0
      fi
      if [[ "$LAN_HOSTNAME_BEFORE" != "$desired_host" ]]; then
        LAN_HOSTNAME_CHANGED_BY_FXROUTE=1
        LAN_HOSTNAME_AFTER="$desired_host"
      fi
    fi
    log "systemctl enable --now avahi-daemon"
    if ! "${SUDO_CMD[@]}" systemctl enable --now avahi-daemon; then
      warn "Automatic .local setup could not start avahi-daemon"
      return 0
    fi
    if [[ $AVAHI_WAS_ACTIVE_BEFORE -eq 0 || $AVAHI_WAS_ENABLED_BEFORE -eq 0 ]]; then
      AVAHI_ENABLED_BY_FXROUTE=1
    fi
    if [[ "$LAN_HOSTNAME_BEFORE" != "$desired_host" ]]; then
      log "systemctl restart avahi-daemon"
      "${SUDO_CMD[@]}" systemctl restart avahi-daemon >/dev/null 2>&1 || true
    fi
    ensure_lan_firewall_service_open mdns ".local LAN access"
    MDNS_HOSTNAME="$desired_host"
    pass "optional .local LAN name configured (${MDNS_HOSTNAME}.local:${port})"
    return 0
  fi

  [[ -t 0 && -t 1 ]] || return 0

  echo
  echo "Optional LAN setup:"
  if [[ $avahi_active -eq 1 ]]; then
    echo "Current .local LAN name: http://${MDNS_HOSTNAME}.local:${port}"
    echo "You can keep it or switch to a dedicated FXRoute hostname such as fxroute.local or fxroute-test.local."
    printf "Change or reconfigure the .local LAN name? [y/N] "
  else
    echo "Enable Avahi mDNS so FXRoute can also be reached as http://<name>.local:${port} ?"
    echo "This changes the system hostname and may require one more sudo step."
    printf "Enable .local LAN name? [y/N] "
  fi
  read -r reply || return 0
  case "${reply,,}" in
    y|yes) ;;
    *)
      if [[ $avahi_active -eq 1 ]]; then
        echo "Keeping current .local LAN name: http://${MDNS_HOSTNAME}.local:${port}"
      fi
      return 0
      ;;
  esac

  desired_host="fxroute"
  while true; do
    printf "Hostname [${desired_host}]: "
    read -r reply || return 0
    desired_host="${reply:-$desired_host}"
    desired_host="${desired_host,,}"
    if valid_local_hostname "$desired_host"; then
      break
    fi
    echo "Please use only lowercase letters, digits, and hyphens, without leading or trailing hyphens."
  done

  case "$PACKAGE_MANAGER" in
    apt) avahi_pkg="avahi-daemon" ;;
    dnf|zypper|pacman) avahi_pkg="avahi" ;;
    *)
      warn "Skipping optional .local setup on unsupported distro package manager: $PACKAGE_MANAGER"
      return 0
      ;;
  esac

  if ! pkg_install "$avahi_pkg"; then
    warn "Optional .local setup failed while installing Avahi"
    return 0
  fi
  if [[ $AVAHI_WAS_PRESENT_BEFORE -eq 0 ]] && avahi_is_present; then
    AVAHI_INSTALLED_BY_FXROUTE=1
  fi

  configure_avahi_ipv4_mdns_for_fxroute

  log "hostnamectl set-hostname $desired_host"
  if ! "${SUDO_CMD[@]}" hostnamectl set-hostname "$desired_host"; then
    warn "Optional .local setup failed while setting hostname"
    return 0
  fi
  if [[ "$LAN_HOSTNAME_BEFORE" != "$desired_host" ]]; then
    LAN_HOSTNAME_CHANGED_BY_FXROUTE=1
    LAN_HOSTNAME_AFTER="$desired_host"
  fi

  log "systemctl enable --now avahi-daemon"
  if ! "${SUDO_CMD[@]}" systemctl enable --now avahi-daemon; then
    warn "Optional .local setup could not start avahi-daemon automatically"
    return 0
  fi
  if [[ "$LAN_HOSTNAME_BEFORE" != "$desired_host" ]]; then
    log "systemctl restart avahi-daemon"
    if ! "${SUDO_CMD[@]}" systemctl restart avahi-daemon; then
      warn "Optional .local setup changed the hostname, but avahi-daemon could not be restarted"
      return 0
    fi
  fi
  if [[ $AVAHI_WAS_ACTIVE_BEFORE -eq 0 || $AVAHI_WAS_ENABLED_BEFORE -eq 0 ]]; then
    AVAHI_ENABLED_BY_FXROUTE=1
  fi

  ensure_lan_firewall_service_open mdns ".local LAN access"

  MDNS_HOSTNAME="$desired_host"
  pass "optional .local LAN name configured (${MDNS_HOSTNAME}.local:${port})"
  echo
  echo "Optional LAN name ready: http://${MDNS_HOSTNAME}.local:${port}"
}

mdns_guard_needed() {
  # nft meta skuid cannot distinguish providers running as the same user. Keep
  # the guard only for the desktop-only combination where blocking user-space
  # mDNS is still useful for Avahi host advertisement.
  [[ "${SELECT_SPOTIFYD:-0}" -eq 0 && "${SPOTIFYD_PRESENT_BEFORE:-0}" -eq 0 ]] || return 1
  [[ "${SELECT_QOBUZ:-0}" -eq 0 && "${QBZD_PRESENT_BEFORE:-0}" -eq 0 ]] || return 1
  [[ "${SPOTIFY_DESKTOP_AVAILABLE:-0}" -eq 1 ]]
}

render_mdns_guard_script() {
  cat <<EOF
#!/usr/bin/env bash
set -euo pipefail
NFT="/usr/sbin/nft"
[[ -x "\$NFT" ]] || NFT="\$(command -v nft)"
TABLE="fxroute_mdnsguard"
USER_ID="${FXROUTE_TARGET_UID}"

table_is_owned() {
  local ruleset=""

  ruleset="\$("\$NFT" list table inet "\$TABLE" 2>/dev/null)" || return 1
  awk -v uid="\$USER_ID" '
    BEGIN {
      v4 = "^[[:space:]]*meta skuid[[:space:]]+" uid "[[:space:]]+ip daddr 224\\.0\\.0\\.251[[:space:]]+udp dport 5353[[:space:]]+counter[[:space:]]+packets[[:space:]]+[0-9]+[[:space:]]+bytes[[:space:]]+[0-9]+[[:space:]]+drop[[:space:]]+comment[[:space:]]+\"Block desktop user-space mDNS v4 to keep Avahi host advertisement stable\"[[:space:]]*$"
      v6 = "^[[:space:]]*meta skuid[[:space:]]+" uid "[[:space:]]+ip6 daddr ff02::fb[[:space:]]+udp dport 5353[[:space:]]+counter[[:space:]]+packets[[:space:]]+[0-9]+[[:space:]]+bytes[[:space:]]+[0-9]+[[:space:]]+drop[[:space:]]+comment[[:space:]]+\"Block desktop user-space mDNS v6 to keep Avahi host advertisement stable\"[[:space:]]*$"
    }
    /^[[:space:]]*table inet fxroute_mdnsguard[[:space:]]*\{[[:space:]]*$/ { tables++; next }
    /^[[:space:]]*chain output[[:space:]]*\{[[:space:]]*$/ { chains++; next }
    /^[[:space:]]*type filter hook output priority[[:space:]]+[^;]+;[[:space:]]*policy accept;[[:space:]]*$/ { next }
    \$0 ~ v4 { v4_rules++; next }
    \$0 ~ v6 { v6_rules++; next }
    /^[[:space:]]*\}[[:space:]]*$/ || /^[[:space:]]*$/ { next }
    { invalid++; next }
    END { exit !(tables == 1 && chains == 1 && v4_rules == 1 && v6_rules == 1 && invalid == 0) }
  ' <<<"\$ruleset"
}

apply_rules() {
  if "\$NFT" list table inet "\$TABLE" >/dev/null 2>&1; then
    if ! table_is_owned; then
      echo "Refusing to modify a foreign nft table named \$TABLE" >&2
      return 1
    fi
    "\$NFT" -f - <<RULES
 flush chain inet \${TABLE} output
 add rule inet \${TABLE} output meta skuid \${USER_ID} ip daddr 224.0.0.251 udp dport 5353 counter drop comment "Block desktop user-space mDNS v4 to keep Avahi host advertisement stable"
 add rule inet \${TABLE} output meta skuid \${USER_ID} ip6 daddr ff02::fb udp dport 5353 counter drop comment "Block desktop user-space mDNS v6 to keep Avahi host advertisement stable"
RULES
  else
    "\$NFT" -f - <<RULES
 add table inet \${TABLE}
 add chain inet \${TABLE} output { type filter hook output priority 5; policy accept; }
 add rule inet \${TABLE} output meta skuid \${USER_ID} ip daddr 224.0.0.251 udp dport 5353 counter drop comment "Block desktop user-space mDNS v4 to keep Avahi host advertisement stable"
 add rule inet \${TABLE} output meta skuid \${USER_ID} ip6 daddr ff02::fb udp dport 5353 counter drop comment "Block desktop user-space mDNS v6 to keep Avahi host advertisement stable"
RULES
  fi
}

remove_rules() {
  if "\$NFT" list table inet "\$TABLE" >/dev/null 2>&1; then
    if ! table_is_owned; then
      echo "Refusing to remove a foreign nft table named \$TABLE" >&2
      return 1
    fi
    "\$NFT" delete table inet "\$TABLE"
    return
  fi
  "\$NFT" list ruleset >/dev/null 2>&1
}

status_rules() {
  "\$NFT" list table inet "\$TABLE"
}

case "\${1:-apply}" in
  apply) apply_rules ;;
  remove) remove_rules ;;
  status) status_rules ;;
  *) echo "usage: \$0 [apply|remove|status]" >&2; exit 2 ;;
esac
EOF
}

render_mdns_guard_service() {
  cat <<'EOF'
[Unit]
Description=FXRoute mDNS guard for Spotify Desktop/Avahi coexistence
After=firewalld.service network-online.target
Wants=firewalld.service network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fxroute-mdns-guard.sh apply
ExecReload=/usr/local/sbin/fxroute-mdns-guard.sh apply
EOF
}

render_mdns_guard_timer() {
  cat <<'EOF'
[Unit]
Description=Re-apply FXRoute mDNS guard periodically

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
Persistent=true
Unit=fxroute-mdns-guard.service

[Install]
WantedBy=timers.target
EOF
}

migrate_legacy_mdns_guard_ownership() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local target_uid=""

  [[ $MDNS_GUARD_LEGACY_OWNERSHIP -eq 1 ]] || return 0
  [[ -f "$script_path" && ! -L "$script_path" ]] || return 1
  [[ -f "$service_path" && ! -L "$service_path" ]] || return 1
  [[ -f "$timer_path" && ! -L "$timer_path" ]] || return 1
  if ! grep -Fq '#!/usr/bin/env bash' "$script_path" \
    || ! grep -Fq 'TABLE="fxroute_mdnsguard"' "$script_path" \
    || ! grep -Eq '^USER_ID=' "$script_path" \
    || ! grep -Fq 'meta skuid' "$script_path" \
    || ! grep -Fq 'case "${1:-apply}"' "$script_path" \
    || ! grep -Fq 'ExecStart=/usr/local/sbin/fxroute-mdns-guard.sh apply' "$service_path" \
    || ! grep -Fq 'ExecReload=/usr/local/sbin/fxroute-mdns-guard.sh apply' "$service_path" \
    || ! grep -Fq 'Unit=fxroute-mdns-guard.service' "$timer_path"; then
    return 1
  fi
  target_uid="$(sed -n 's/^USER_ID="\([0-9][0-9]*\)"$/\1/p' "$script_path")"
  [[ "$target_uid" =~ ^[0-9]+$ ]] || return 1
  MDNS_GUARD_SCRIPT_SHA256="$(sha256sum "$script_path" | awk '{print $1}')"
  MDNS_GUARD_SERVICE_SHA256="$(sha256sum "$service_path" | awk '{print $1}')"
  MDNS_GUARD_TIMER_SHA256="$(sha256sum "$timer_path" | awk '{print $1}')"
  MDNS_GUARD_TARGET_UID="$target_uid"
  MDNS_GUARD_ENABLED=1
  MDNS_GUARD_OWNED_BY_FXROUTE=1
  MDNS_GUARD_LEGACY_OWNERSHIP=0
  return 0
}

remove_mdns_guard_table_direct() {
  local nft_path=""
  local ruleset=""

  nft_path="$(command -v nft 2>/dev/null || true)"
  [[ -n "$nft_path" ]] || return 1
  if ! "${SUDO_CMD[@]}" "$nft_path" list table inet fxroute_mdnsguard >/dev/null 2>&1; then
    ruleset="$("${SUDO_CMD[@]}" "$nft_path" list ruleset 2>/dev/null)" || return 1
    grep -Fq 'table inet fxroute_mdnsguard' <<<"$ruleset" && return 1
    return 0
  fi
  mdns_guard_table_matches || return 1
  if ! "${SUDO_CMD[@]}" "$nft_path" delete table inet fxroute_mdnsguard >/dev/null 2>&1; then
    return 1
  fi
  if "${SUDO_CMD[@]}" "$nft_path" list table inet fxroute_mdnsguard >/dev/null 2>&1; then
    return 1
  fi
  ruleset="$("${SUDO_CMD[@]}" "$nft_path" list ruleset 2>/dev/null)" || return 1
  grep -Fq 'table inet fxroute_mdnsguard' <<<"$ruleset" && return 1
  return 0
}

mdns_guard_table_matches() {
  local nft_path=""
  local ruleset=""
  local expected_uid="${FXROUTE_TARGET_UID:-$MDNS_GUARD_TARGET_UID}"

  nft_path="$(command -v nft 2>/dev/null || true)"
  [[ -n "$nft_path" ]] || return 1
  [[ "$expected_uid" =~ ^[0-9]+$ ]] || return 1
  ruleset="$("${SUDO_CMD[@]}" "$nft_path" list table inet fxroute_mdnsguard 2>/dev/null)" || return 1
  grep -Fq 'chain output' <<<"$ruleset" \
    && grep -Fq 'hook output' <<<"$ruleset" \
    && grep -Fq 'priority' <<<"$ruleset" \
    && grep -Fq 'policy accept' <<<"$ruleset" \
    && grep -Eq "meta skuid[[:space:]]+${expected_uid}.*ip daddr 224\\.0\\.0\\.251.*udp dport 5353.*drop.*Block desktop user-space mDNS v4 to keep Avahi host advertisement stable" <<<"$ruleset" \
    && grep -Eq "meta skuid[[:space:]]+${expected_uid}.*ip6 daddr ff02::fb.*udp dport 5353.*drop.*Block desktop user-space mDNS v6 to keep Avahi host advertisement stable" <<<"$ruleset"
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

mdns_guard_artifact_matches() {
  local path="$1"
  local expected_sha256="$2"
  local actual_sha256=""

  [[ -e "$path" || -L "$path" ]] || return 0
  [[ -f "$path" && ! -L "$path" ]] || return 1
  [[ -n "$expected_sha256" ]] || return 1
  actual_sha256="$(sha256sum "$path" | awk '{print $1}')" || return 1
  [[ "$actual_sha256" == "$expected_sha256" ]]
}

mdns_guard_artifacts_match() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"

  mdns_guard_artifact_matches "$script_path" "$MDNS_GUARD_SCRIPT_SHA256" || return 1
  mdns_guard_artifact_matches "$service_path" "$MDNS_GUARD_SERVICE_SHA256" || return 1
  mdns_guard_artifact_matches "$timer_path" "$MDNS_GUARD_TIMER_SHA256" || return 1
}

restore_mdns_guard_artifacts() {
  local backup_dir="$1"
  local restore_failed=0

  if [[ -f "$backup_dir/script" ]]; then
    "${SUDO_CMD[@]}" cp -p "$backup_dir/script" /usr/local/sbin/fxroute-mdns-guard.sh || restore_failed=1
  else
    "${SUDO_CMD[@]}" rm -f /usr/local/sbin/fxroute-mdns-guard.sh || restore_failed=1
  fi
  if [[ -f "$backup_dir/service" ]]; then
    "${SUDO_CMD[@]}" cp -p "$backup_dir/service" /etc/systemd/system/fxroute-mdns-guard.service || restore_failed=1
  else
    "${SUDO_CMD[@]}" rm -f /etc/systemd/system/fxroute-mdns-guard.service || restore_failed=1
  fi
  if [[ -f "$backup_dir/timer" ]]; then
    "${SUDO_CMD[@]}" cp -p "$backup_dir/timer" /etc/systemd/system/fxroute-mdns-guard.timer || restore_failed=1
  else
    "${SUDO_CMD[@]}" rm -f /etc/systemd/system/fxroute-mdns-guard.timer || restore_failed=1
  fi
  return "$restore_failed"
}

remove_mdns_guard_if_present() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local marker_present=0
  local timer_was_active=0
  local timer_was_enabled=0
  local timer_loaded=0
  local service_loaded=0
  local unit_status=0

  if [[ -e "$script_path" || -L "$script_path" \
    || -e "$service_path" || -L "$service_path" \
    || -e "$timer_path" || -L "$timer_path" ]]; then
    marker_present=1
  fi

  if [[ $MDNS_GUARD_LEGACY_OWNERSHIP -eq 1 && $marker_present -eq 1 ]]; then
    if ! migrate_legacy_mdns_guard_ownership; then
      warn "Cannot verify ownership of the legacy FXRoute mDNS guard; preserving it"
      return 0
    fi
  elif [[ $MDNS_GUARD_LEGACY_OWNERSHIP -eq 1 ]]; then
    # The legacy state explicitly enabled the guard, but its artifacts are
    # already gone. Keep ownership so the direct nft cleanup can verify the
    # table without executing a mutable root script.
    MDNS_GUARD_OWNED_BY_FXROUTE=1
    MDNS_GUARD_LEGACY_OWNERSHIP=0
  fi
  if [[ $MDNS_GUARD_OWNED_BY_FXROUTE -ne 1 ]]; then
    if [[ -e "$script_path" || -L "$script_path" \
      || -e "$service_path" || -L "$service_path" \
      || -e "$timer_path" || -L "$timer_path" ]]; then
      warn "Preserving mDNS guard artifacts without FXRoute ownership"
    fi
    return 0
  fi
  if ! mdns_guard_artifacts_match; then
    warn "Preserving mDNS guard artifacts whose content no longer matches FXRoute"
    MDNS_GUARD_OWNED_BY_FXROUTE=0
    MDNS_GUARD_SCRIPT_SHA256=""
    MDNS_GUARD_SERVICE_SHA256=""
    MDNS_GUARD_TIMER_SHA256=""
    MDNS_GUARD_TARGET_UID=""
    return 0
  fi

  if [[ $marker_present -eq 1 || $MDNS_GUARD_OWNED_BY_FXROUTE -eq 1 ]]; then
    if systemd_unit_is_loaded fxroute-mdns-guard.timer "${SUDO_CMD[@]}"; then
      timer_loaded=1
    else
      unit_status=$?
      if [[ $unit_status -eq 2 ]]; then
        warn "Could not verify a loaded FXRoute mDNS guard timer"
        MDNS_GUARD_ENABLED=1
        return 0
      fi
    fi
    if [[ $timer_loaded -eq 1 ]] \
      && "${SUDO_CMD[@]}" systemctl is-active --quiet fxroute-mdns-guard.timer; then
      timer_was_active=1
    fi
    if [[ $timer_loaded -eq 1 ]] \
      && "${SUDO_CMD[@]}" systemctl is-enabled --quiet fxroute-mdns-guard.timer; then
      timer_was_enabled=1
    fi
    if systemd_unit_is_loaded fxroute-mdns-guard.service "${SUDO_CMD[@]}"; then
      service_loaded=1
    else
      unit_status=$?
      if [[ $unit_status -eq 2 ]]; then
        warn "Could not verify a loaded FXRoute mDNS guard service"
        MDNS_GUARD_ENABLED=1
        return 0
      fi
    fi
    if [[ $timer_loaded -eq 1 ]] \
      && ! systemd_unit_fragment_matches fxroute-mdns-guard.timer "$timer_path" "${SUDO_CMD[@]}"; then
      warn "Refusing to stop the loaded FXRoute mDNS guard timer from a foreign unit path"
      MDNS_GUARD_ENABLED=1
      return 0
    fi
    if [[ $service_loaded -eq 1 ]] \
      && ! systemd_unit_fragment_matches fxroute-mdns-guard.service "$service_path" "${SUDO_CMD[@]}"; then
      warn "Refusing to stop the loaded FXRoute mDNS guard service from a foreign unit path"
      MDNS_GUARD_ENABLED=1
      return 0
    fi
    if [[ -e "$timer_path" || -L "$timer_path" || $timer_loaded -eq 1 ]] \
      && ! "${SUDO_CMD[@]}" systemctl disable --now fxroute-mdns-guard.timer >/dev/null 2>&1; then
      warn "Could not stop the FXRoute mDNS guard timer"
      if [[ $timer_was_enabled -eq 1 ]]; then
        "${SUDO_CMD[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
      fi
      if [[ $timer_was_active -eq 1 ]]; then
        "${SUDO_CMD[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
      fi
      MDNS_GUARD_ENABLED=1
      return 0
    fi
    if [[ -e "$service_path" || -L "$service_path" || $service_loaded -eq 1 ]] \
      && ! "${SUDO_CMD[@]}" systemctl disable --now fxroute-mdns-guard.service >/dev/null 2>&1; then
      warn "Could not stop the FXRoute mDNS guard service"
      if [[ $timer_was_enabled -eq 1 ]]; then
        "${SUDO_CMD[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
      fi
      if [[ $timer_was_active -eq 1 ]]; then
        "${SUDO_CMD[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
      fi
      MDNS_GUARD_ENABLED=1
      return 0
    fi
  fi
  if [[ $marker_present -eq 0 ]]; then
    if ! command -v nft >/dev/null 2>&1; then
      warn "Cannot verify the FXRoute mDNS guard table because nft is unavailable"
      MDNS_GUARD_ENABLED=1
      return 0
    fi
    if ! remove_mdns_guard_table_direct; then
      warn "Could not verify or remove the FXRoute mDNS guard table"
      MDNS_GUARD_ENABLED=1
      return 0
    fi
    MDNS_GUARD_ENABLED=0
    MDNS_GUARD_OWNED_BY_FXROUTE=0
    MDNS_GUARD_SCRIPT_SHA256=""
    MDNS_GUARD_SERVICE_SHA256=""
    MDNS_GUARD_TIMER_SHA256=""
    MDNS_GUARD_TARGET_UID=""
    return 0
  fi
  if ! remove_mdns_guard_table_direct; then
    warn "Could not remove the FXRoute mDNS guard rules"
    if [[ $timer_was_enabled -eq 1 ]]; then
      "${SUDO_CMD[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    if [[ $timer_was_active -eq 1 ]]; then
      "${SUDO_CMD[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    MDNS_GUARD_ENABLED=1
    return 0
  fi
  if ! "${SUDO_CMD[@]}" rm -f "$script_path" "$service_path" "$timer_path"; then
    warn "Could not remove the FXRoute mDNS guard files"
    if [[ $timer_was_enabled -eq 1 ]]; then
      "${SUDO_CMD[@]}" systemctl enable fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    if [[ $timer_was_active -eq 1 ]]; then
      "${SUDO_CMD[@]}" systemctl start fxroute-mdns-guard.timer >/dev/null 2>&1 || true
    fi
    return 0
  fi
  "${SUDO_CMD[@]}" systemctl daemon-reload >/dev/null 2>&1 || true
  MDNS_GUARD_ENABLED=0
  MDNS_GUARD_OWNED_BY_FXROUTE=0
  MDNS_GUARD_SCRIPT_SHA256=""
  MDNS_GUARD_SERVICE_SHA256=""
  MDNS_GUARD_TIMER_SHA256=""
  MDNS_GUARD_TARGET_UID=""
  pass "FXRoute mDNS guard removed for provider compatibility"
}

install_mdns_guard() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local tmp_script=""
  local tmp_service=""
  local tmp_timer=""
  local guard_artifact_present=0
  local guard_artifact_owned=1
  local backup_dir=""
  local previous_script_sha256="$MDNS_GUARD_SCRIPT_SHA256"
  local previous_service_sha256="$MDNS_GUARD_SERVICE_SHA256"
  local previous_timer_sha256="$MDNS_GUARD_TIMER_SHA256"
  local previous_target_uid="$MDNS_GUARD_TARGET_UID"

  if ! mdns_guard_needed; then
    remove_mdns_guard_if_present
    return 0
  fi
  (systemctl is-active avahi-daemon >/dev/null 2>&1 || [[ -n "$MDNS_HOSTNAME" ]]) || return 0

  if [[ -e "$script_path" || -L "$script_path" \
    || -e "$service_path" || -L "$service_path" \
    || -e "$timer_path" || -L "$timer_path" ]]; then
    guard_artifact_present=1
  fi
  if [[ $guard_artifact_present -eq 1 && $MDNS_GUARD_LEGACY_OWNERSHIP -eq 1 ]] \
    && ! migrate_legacy_mdns_guard_ownership; then
    warn "Cannot verify ownership of the legacy FXRoute mDNS guard; preserving it"
    MDNS_GUARD_ENABLED=1
    MDNS_GUARD_OWNED_BY_FXROUTE=0
    return 0
  fi
  if [[ $guard_artifact_present -eq 1 ]]; then
    if [[ $MDNS_GUARD_ENABLED -ne 1 || $MDNS_GUARD_OWNED_BY_FXROUTE -ne 1 ]] \
      || ! mdns_guard_artifacts_match; then
      guard_artifact_owned=0
    fi
    if [[ $guard_artifact_owned -eq 0 ]]; then
      warn "Existing FXRoute mDNS guard paths are not owned or do not match the expected guard; preserving them"
      MDNS_GUARD_ENABLED=1
      MDNS_GUARD_OWNED_BY_FXROUTE=0
      return 0
    fi
  fi

  if ! command -v nft >/dev/null 2>&1; then
    if ! pkg_install nftables; then
      warn "Could not install nftables for the FXRoute mDNS guard"
      return 0
    fi
  fi

  tmp_script="$(mktemp)"
  tmp_service="$(mktemp)"
  tmp_timer="$(mktemp)"
  trap "trap - RETURN; rm -f '$tmp_script' '$tmp_service' '$tmp_timer'" RETURN

  render_mdns_guard_script > "$tmp_script"
  chmod 755 "$tmp_script"
  render_mdns_guard_service > "$tmp_service"
  render_mdns_guard_timer > "$tmp_timer"

  MDNS_GUARD_SCRIPT_SHA256="$(sha256sum "$tmp_script" | awk '{print $1}')"
  MDNS_GUARD_SERVICE_SHA256="$(sha256sum "$tmp_service" | awk '{print $1}')"
  MDNS_GUARD_TIMER_SHA256="$(sha256sum "$tmp_timer" | awk '{print $1}')"
  MDNS_GUARD_TARGET_UID="$FXROUTE_TARGET_UID"
  if ! backup_dir="$(mktemp -d -t fxroute-mdns-guard-backup.XXXXXX)"; then
    warn "Could not create a backup area for the FXRoute mDNS guard refresh"
    MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
    MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
    MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
    MDNS_GUARD_TARGET_UID="$previous_target_uid"
    return 0
  fi
  trap "trap - RETURN; rm -f '$tmp_script' '$tmp_service' '$tmp_timer'; rm -rf '$backup_dir'" RETURN
  if [[ -f "$script_path" ]] && ! "${SUDO_CMD[@]}" cp -p "$script_path" "$backup_dir/script"; then
    warn "Could not back up the existing FXRoute mDNS guard script"
    MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
    MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
    MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
    MDNS_GUARD_TARGET_UID="$previous_target_uid"
    return 0
  fi
  if [[ -f "$service_path" ]] && ! "${SUDO_CMD[@]}" cp -p "$service_path" "$backup_dir/service"; then
    warn "Could not back up the existing FXRoute mDNS guard service"
    MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
    MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
    MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
    MDNS_GUARD_TARGET_UID="$previous_target_uid"
    return 0
  fi
  if [[ -f "$timer_path" ]] && ! "${SUDO_CMD[@]}" cp -p "$timer_path" "$backup_dir/timer"; then
    warn "Could not back up the existing FXRoute mDNS guard timer"
    MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
    MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
    MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
    MDNS_GUARD_TARGET_UID="$previous_target_uid"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 755 "$tmp_script" "$script_path"; then
    warn "Could not install FXRoute mDNS guard script"
    if restore_mdns_guard_artifacts "$backup_dir"; then
      MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
      MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
      MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
      MDNS_GUARD_TARGET_UID="$previous_target_uid"
    else
      MDNS_GUARD_ENABLED=1
      MDNS_GUARD_OWNED_BY_FXROUTE=1
    fi
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_service" "$service_path"; then
    warn "Could not install FXRoute mDNS guard service"
    if restore_mdns_guard_artifacts "$backup_dir"; then
      MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
      MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
      MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
      MDNS_GUARD_TARGET_UID="$previous_target_uid"
    else
      MDNS_GUARD_ENABLED=1
      MDNS_GUARD_OWNED_BY_FXROUTE=1
    fi
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_timer" "$timer_path"; then
    warn "Could not install FXRoute mDNS guard timer"
    if restore_mdns_guard_artifacts "$backup_dir"; then
      MDNS_GUARD_SCRIPT_SHA256="$previous_script_sha256"
      MDNS_GUARD_SERVICE_SHA256="$previous_service_sha256"
      MDNS_GUARD_TIMER_SHA256="$previous_timer_sha256"
      MDNS_GUARD_TARGET_UID="$previous_target_uid"
    else
      MDNS_GUARD_ENABLED=1
      MDNS_GUARD_OWNED_BY_FXROUTE=1
    fi
    return 0
  fi
  MDNS_GUARD_ENABLED=1
  MDNS_GUARD_OWNED_BY_FXROUTE=1
  if ! "${SUDO_CMD[@]}" systemctl daemon-reload; then
    warn "Could not reload systemd after installing FXRoute mDNS guard"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" "$tmp_script" apply; then
    warn "Could not apply the FXRoute mDNS guard rules"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" systemctl enable fxroute-mdns-guard.timer || ! "${SUDO_CMD[@]}" systemctl restart fxroute-mdns-guard.timer; then
    warn "Could not enable the FXRoute mDNS guard timer"
    return 0
  fi
  MDNS_GUARD_TARGET_UID="$FXROUTE_TARGET_UID"

  pass "FXRoute mDNS guard installed for Spotify Desktop only"
  return 0
}

configure_system_power_polkit_rule() {
  local install_user=""
  local template_src="$INSTALL_ROOT/assets/polkit/50-fxroute-power.rules"
  local rule_name="50-fxroute-power.rules"
  local rule_path="/etc/polkit-1/rules.d/$rule_name"
  local tmp_rule=""
  local rendered_rule=""
  local backup_path=""
  local template_sha256=""
  local template_snapshot=""
  local previous_rule_pre_existed=0
  local current_rule_sha256=""

  install_user="$FXROUTE_TARGET_USER"
  [[ "$install_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || {
    warn "FXRoute polkit power rule skipped because the install user is not a plain Unix name"
    return 0
  }

  [[ -f "$template_src" && ! -L "$template_src" ]] || {
    warn "FXRoute polkit power rule skipped because $template_src is missing"
    return 0
  }
  template_snapshot="$(mktemp)"
  if ! cp -- "$template_src" "$template_snapshot"; then
    warn "FXRoute polkit power rule skipped because its template could not be staged"
    rm -f "$template_snapshot"
    return 0
  fi
  template_sha256="$(sha256sum "$template_snapshot" | awk '{print $1}')"
  [[ "$template_sha256" == "$POWER_POLKIT_TEMPLATE_SHA256" ]] || {
    warn "FXRoute polkit power rule skipped because its template is unverified"
    rm -f "$template_snapshot"
    return 0
  }

  rendered_rule="$(sed -e "s/INSTALL_USER_PLACEHOLDER/${install_user}/g" "$template_snapshot")"
  [[ "$rendered_rule" != *"${install_user}"* ]] && {
    warn "FXRoute polkit power rule template did not accept the install user; skipping"
    rm -f "$template_snapshot"
    return 0
  }
  rm -f "$template_snapshot"

  tmp_rule="$(mktemp)"
  printf '%s\n' "$rendered_rule" > "$tmp_rule"

  POWER_POLKIT_RULE_PATH="$rule_path"
  previous_rule_pre_existed="$POWER_POLKIT_RULE_PRE_EXISTED"
  POWER_POLKIT_RULE_PRE_EXISTED=0

  if [[ -e "$rule_path" || -L "$rule_path" ]]; then
    if [[ ! -f "$rule_path" || -L "$rule_path" ]]; then
      warn "Could not use the existing FXRoute polkit rule because it is not a regular file"
      rm -f "$tmp_rule"
      return 0
    fi
    backup_path="$FXROUTE_BACKUP_DIR/${rule_name}.pre-fxroute"
    current_rule_sha256="$("${SUDO_CMD[@]}" sha256sum "$rule_path" | awk '{print $1}')"
    rendered_sha256="$(printf '%s\n' "$rendered_rule" | sha256sum | awk '{print $1}')"
    if [[ -n "$rendered_sha256" && "$current_rule_sha256" == "$rendered_sha256" ]]; then
      # The existing rule is exactly the FXRoute-rendered one: FXRoute (this
      # or an earlier run) created it, not the user. Treating it as
      # pre-existing made re-runs record a bogus backup and later blocked the
      # uninstaller. Drop any leftover backup copy of the same rule so the
      # uninstall can remove the rule cleanly.
      POWER_POLKIT_RULE_PRE_EXISTED=0
      POWER_POLKIT_RULE_SHA256="$current_rule_sha256"
      if [[ -f "$backup_path" && ! -L "$backup_path" ]]; then
        local backup_copy_sha256=""
        backup_copy_sha256="$("${SUDO_CMD[@]}" sha256sum "$backup_path" | awk '{print $1}')"
        if [[ "$backup_copy_sha256" == "$current_rule_sha256" ]]; then
          "${SUDO_CMD[@]}" rm -f "$backup_path"
        fi
      fi
      POWER_POLKIT_BACKUP_SHA256=""
    elif [[ -n "$POWER_POLKIT_RULE_SHA256" ]]; then
      if [[ "$current_rule_sha256" != "$POWER_POLKIT_RULE_SHA256" ]]; then
        warn "Could not verify the existing FXRoute polkit rule; refusing to replace a changed rule"
        rm -f "$tmp_rule"
        return 0
      fi
      POWER_POLKIT_RULE_PRE_EXISTED="$previous_rule_pre_existed"
    else
      POWER_POLKIT_RULE_PRE_EXISTED=1
    fi
    if [[ $POWER_POLKIT_RULE_PRE_EXISTED -eq 0 ]]; then
      :
    elif [[ -L "$backup_path" || ( -e "$backup_path" && ! -f "$backup_path" ) ]]; then
      warn "Could not use the existing FXRoute polkit backup because it is not a regular file"
      rm -f "$tmp_rule"
      return 0
    elif [[ -f "$backup_path" ]]; then
      local backup_sha256=""
      if [[ "$(${SUDO_CMD[@]} stat -c '%u' "$backup_path" 2>/dev/null || true)" != "0" ]]; then
        warn "Could not use the existing FXRoute polkit backup because it is not root-owned"
        rm -f "$tmp_rule"
        return 0
      fi
      backup_sha256="$("${SUDO_CMD[@]}" sha256sum "$backup_path" | awk '{print $1}')"
      if [[ -n "$POWER_POLKIT_BACKUP_SHA256" && "$backup_sha256" != "$POWER_POLKIT_BACKUP_SHA256" ]]; then
        warn "Could not verify the existing FXRoute polkit backup; refusing to replace the current rule"
        rm -f "$tmp_rule"
        return 0
      fi
      POWER_POLKIT_BACKUP_SHA256="$backup_sha256"
    else
      if ! "${SUDO_CMD[@]}" install -d -o root -g root -m 700 "$FXROUTE_BACKUP_DIR" \
        || ! "${SUDO_CMD[@]}" cp -a "$rule_path" "$backup_path" 2>/dev/null; then
        warn "Could not back up $rule_path before installing the FXRoute polkit power rule"
        rm -f "$tmp_rule"
        return 0
      fi
      backup_sha256="$("${SUDO_CMD[@]}" sha256sum "$backup_path" | awk '{print $1}')"
      POWER_POLKIT_BACKUP_SHA256="$backup_sha256"
    fi
  else
    POWER_POLKIT_BACKUP_SHA256=""
    POWER_POLKIT_RULE_SHA256=""
  fi

  if "${SUDO_CMD[@]}" install -d /etc/polkit-1/rules.d; then
    if "${SUDO_CMD[@]}" install -m 644 "$tmp_rule" "$rule_path"; then
      POWER_POLKIT_INSTALLED=1
      POWER_POLKIT_RULE_SHA256="$("${SUDO_CMD[@]}" sha256sum "$rule_path" | awk '{print $1}')"
      pass "polkit power rule installed (closed: suspend + power-off + hostname + avahi-restart only)"
    else
      warn "FXRoute polkit power rule could not be written to $rule_path"
    fi
  else
    warn "FXRoute polkit power rule could not create /etc/polkit-1/rules.d"
  fi

  rm -f "$tmp_rule"
}

offer_optional_caddy_proxy() {
  local env_file="$INSTALL_ROOT/.env"
  local port="8000"
  local lan_ip=""
  local reply=""
  local caddy_bin=""
  local tmp_caddy=""
  local tmp_service=""
  local service_name="fxroute-caddy"
  local service_path="/etc/systemd/system/${service_name}.service"
  local config_dir="/etc/fxroute"
  local config_path="${config_dir}/Caddyfile"
  local caddy_data_dir="/var/lib/fxroute-caddy"
  local caddy_cert_dir="${config_dir}/certs"
  local caddy_cert_path="${caddy_cert_dir}/fxroute-local-root.crt"
  local caddy_root_cert="${caddy_data_dir}/caddy/pki/authorities/local/root.crt"
  local fxroute_caddy_active=0
  local caddy_path=""

  [[ -f "$env_file" ]] && port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  if ! [[ "$port" =~ ^[0-9]+$ ]] || [[ ${#port} -gt 5 ]] || (( 10#$port < 1 || 10#$port > 65535 )); then
    warn "Optional Caddy setup skipped because PORT is not a valid TCP port"
    return 0
  fi
  lan_ip="$(primary_lan_ip)"
  [[ -n "$lan_ip" ]] || {
    warn "Optional Caddy setup skipped because no LAN IP could be detected"
    return 0
  }

  if [[ $AUTO_CADDY -eq 1 ]]; then
    # Image first-boot: enable HTTPS non-interactively (HTTP on :8000 stays
    # reachable; the proxy adds :80/:443 on top).
    :
  else
    [[ -t 0 && -t 1 ]] || return 0
  fi

  echo
  if systemctl is-active "$service_name" >/dev/null 2>&1; then
    fxroute_caddy_active=1
    echo "Optional Caddy HTTPS already active; refreshing FXRoute Caddy config: https://${lan_ip}"
    if [[ -n "$MDNS_HOSTNAME" ]]; then
      echo "Optional .local HTTPS also active: https://${MDNS_HOSTNAME}.local"
    fi
  elif [[ $AUTO_CADDY -eq 1 ]]; then
    log "--with-caddy: enabling the FXRoute HTTPS reverse proxy automatically"
  else
    echo "Optional LAN HTTPS setup:"
    echo "Enable Caddy so FXRoute can be reached as https://${lan_ip} with an installer-managed local certificate?"
    if [[ -n "$MDNS_HOSTNAME" ]]; then
      echo "If Avahi is active, the same certificate flow can also cover https://${MDNS_HOSTNAME}.local ."
    fi
    echo "This may require one more sudo step."
    printf "Enable Caddy HTTPS reverse proxy? [y/N] "
    read -r reply || return 0
    case "${reply,,}" in
      y|yes) ;;
      *) return 0 ;;
    esac
  fi

  if [[ $fxroute_caddy_active -eq 0 ]] && ss -ltn '( sport = :80 )' 2>/dev/null | tail -n +2 | grep -q .; then
    warn "Optional Caddy setup skipped because TCP port 80 is already in use"
    return 0
  fi

  caddy_bin="$(command -v caddy || true)"
  if [[ -z "$caddy_bin" ]]; then
    if ! pkg_install caddy; then
      warn "Optional Caddy setup failed while installing Caddy"
      return 0
    fi
    if [[ $CADDY_WAS_PRESENT_BEFORE -eq 0 ]] && command -v caddy >/dev/null 2>&1; then
      CADDY_INSTALLED_BY_FXROUTE=1
    fi
    caddy_bin="$(command -v caddy || true)"
  fi

  [[ -n "$caddy_bin" ]] || {
    warn "Optional Caddy setup failed because the caddy binary is not available after install"
    return 0
  }

  for caddy_path in "$service_path" "$config_dir" "$config_path" "$caddy_data_dir" "$caddy_cert_dir" "$caddy_cert_path"; do
    if [[ -L "$caddy_path" ]] || path_has_symlink_component "$caddy_path"; then
      warn "Optional Caddy setup skipped because a managed path is symlinked: $caddy_path"
      return 0
    fi
  done
  if [[ -e "$service_path" || -L "$service_path" ]]; then
    if [[ ! -f "$service_path" || -L "$service_path" || -z "$CADDY_SERVICE_SHA256" \
      || "$("${SUDO_CMD[@]}" stat -c '%u' "$service_path" 2>/dev/null || true)" != "0" \
      || "$("${SUDO_CMD[@]}" sha256sum "$service_path" | awk '{print $1}')" != "$CADDY_SERVICE_SHA256" ]]; then
      warn "Optional Caddy setup skipped because the existing service is not FXRoute-owned"
      return 0
    fi
  fi
  if [[ -e "$config_path" || -L "$config_path" ]]; then
    if [[ ! -f "$config_path" || -L "$config_path" || -z "$CADDY_CONFIG_SHA256" \
      || "$("${SUDO_CMD[@]}" stat -c '%u' "$config_path" 2>/dev/null || true)" != "0" \
      || "$("${SUDO_CMD[@]}" sha256sum "$config_path" | awk '{print $1}')" != "$CADDY_CONFIG_SHA256" ]]; then
      warn "Optional Caddy setup skipped because the existing configuration is not FXRoute-owned"
      return 0
    fi
  fi
  if [[ -e "$caddy_cert_path" || -L "$caddy_cert_path" ]]; then
    if [[ ! -f "$caddy_cert_path" || -L "$caddy_cert_path" || -z "$CADDY_CERT_SHA256" \
      || "$("${SUDO_CMD[@]}" stat -c '%u' "$caddy_cert_path" 2>/dev/null || true)" != "0" \
      || "$("${SUDO_CMD[@]}" sha256sum "$caddy_cert_path" | awk '{print $1}')" != "$CADDY_CERT_SHA256" ]]; then
      warn "Optional Caddy setup skipped because the existing certificate is not FXRoute-owned"
      return 0
    fi
  fi
  if [[ ! -e "$caddy_data_dir" && ! -L "$caddy_data_dir" ]]; then
    CADDY_DATA_DIR_CREATED_BY_FXROUTE=1
  else
    if [[ "$CADDY_DATA_DIR_CREATED_BY_FXROUTE" -ne 1 ]] \
      || [[ ! -d "$caddy_data_dir" || -L "$caddy_data_dir" ]] \
      || [[ "$("${SUDO_CMD[@]}" stat -c '%u' "$caddy_data_dir" 2>/dev/null || true)" != "0" ]]; then
      warn "Refusing to use a pre-existing Caddy data directory"
      return 0
    fi
  fi

  tmp_caddy="$(mktemp)"
  tmp_service="$(mktemp)"
  trap "trap - RETURN; rm -f '$tmp_caddy' '$tmp_service'" RETURN

  cat > "$tmp_caddy" <<EOF
(fxroute_pna_headers) {
    header {
        Access-Control-Allow-Origin "*"
        Access-Control-Allow-Methods "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        Access-Control-Allow-Headers "*"
        Access-Control-Allow-Private-Network "true"
        Vary "Origin, Access-Control-Request-Method, Access-Control-Request-Headers, Access-Control-Request-Private-Network"
    }
}

(fxroute_pna_preflight) {
    @fxroute_pna_preflight {
        method OPTIONS
        header Origin *
        header Access-Control-Request-Method *
    }
    handle @fxroute_pna_preflight {
        import fxroute_pna_headers
        respond "" 204
    }
}

http://${lan_ip} {
    import fxroute_pna_preflight
    import fxroute_pna_headers
    reverse_proxy 127.0.0.1:${port}
}

https://${lan_ip} {
    import fxroute_pna_preflight
    import fxroute_pna_headers
    tls internal
    reverse_proxy 127.0.0.1:${port}
}
EOF

  if [[ -n "$MDNS_HOSTNAME" ]]; then
    cat >> "$tmp_caddy" <<EOF

http://${MDNS_HOSTNAME}.local {
    import fxroute_pna_preflight
    import fxroute_pna_headers
    reverse_proxy 127.0.0.1:${port}
}

https://${MDNS_HOSTNAME}.local {
    import fxroute_pna_preflight
    import fxroute_pna_headers
    tls internal
    reverse_proxy 127.0.0.1:${port}
}
EOF
  fi

  cat > "$tmp_service" <<EOF
[Unit]
Description=FXRoute Caddy reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HOME=${caddy_data_dir}
Environment=XDG_CONFIG_HOME=${caddy_data_dir}/config
Environment=XDG_DATA_HOME=${caddy_data_dir}
ExecStart=${caddy_bin} run --config ${config_path} --adapter caddyfile
ExecReload=${caddy_bin} reload --config ${config_path} --adapter caddyfile
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  if ! "${SUDO_CMD[@]}" install -d "$config_dir" "$caddy_data_dir" "${caddy_data_dir}/config" "$caddy_cert_dir"; then
    warn "Optional Caddy setup failed while creating ${config_dir}"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_caddy" "$config_path"; then
    warn "Optional Caddy setup failed while writing ${config_path}"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_service" "$service_path"; then
    warn "Optional Caddy setup failed while writing ${service_path}"
    return 0
  fi
  CADDY_CONFIG_SHA256="$("${SUDO_CMD[@]}" sha256sum "$config_path" | awk '{print $1}')"
  CADDY_SERVICE_SHA256="$("${SUDO_CMD[@]}" sha256sum "$service_path" | awk '{print $1}')"

  if ! "${SUDO_CMD[@]}" systemctl daemon-reload; then
    warn "Optional Caddy setup failed during systemd daemon-reload"
    return 0
  fi
  if systemctl is-active caddy.service >/dev/null 2>&1; then
    log "system Caddy service detected on port 80, switching to the FXRoute-owned proxy service"
    if ! "${SUDO_CMD[@]}" systemctl disable --now caddy.service; then
      "${SUDO_CMD[@]}" systemctl enable --now caddy.service >/dev/null 2>&1 || \
        warn "Could not restore the default caddy.service after the disable attempt"
      warn "Optional Caddy setup could not disable the default caddy.service"
      return 0
    fi
    CADDY_SERVICE_WAS_ACTIVE_BEFORE=1
    DEFAULT_CADDY_DISABLED_BY_FXROUTE=1
  fi
  if ! "${SUDO_CMD[@]}" systemctl enable "${service_name}.service" || ! "${SUDO_CMD[@]}" systemctl restart "${service_name}.service"; then
    "${SUDO_CMD[@]}" systemctl disable --now "${service_name}.service" >/dev/null 2>&1 || true
    if [[ $DEFAULT_CADDY_DISABLED_BY_FXROUTE -eq 1 ]]; then
      if "${SUDO_CMD[@]}" systemctl enable --now caddy.service >/dev/null 2>&1; then
        DEFAULT_CADDY_DISABLED_BY_FXROUTE=0
      else
        warn "Could not restore the previously active system caddy.service"
      fi
    fi
    warn "Optional Caddy setup failed while enabling/restarting ${service_name}.service"
    return 0
  fi
  CADDY_PROXY_ENABLED=1
  sleep 3
  if ! curl -kfsS "https://${lan_ip}/api/status" >/dev/null 2>&1; then
    warn "Optional Caddy setup finished, but the HTTPS health check did not answer yet"
    return 0
  fi

  if "${SUDO_CMD[@]}" test -f "$caddy_root_cert"; then
    if ! "${SUDO_CMD[@]}" install -m 644 "$caddy_root_cert" "${caddy_cert_dir}/fxroute-local-root.crt"; then
      warn "Caddy HTTPS is active, but the root certificate could not be copied into ${caddy_cert_dir}"
    else
      CADDY_CERT_PATH="$caddy_cert_path"
      CADDY_CERT_SHA256="$("${SUDO_CMD[@]}" sha256sum "$CADDY_CERT_PATH" | awk '{print $1}')"
    fi
  else
    warn "Caddy HTTPS is active, but the generated root certificate was not found at ${caddy_root_cert}"
  fi

  ensure_lan_firewall_service_open http "FXRoute port-80 LAN access"
  ensure_lan_firewall_service_open https "FXRoute port-443 LAN access"

  pass "optional Caddy HTTPS reverse proxy configured (https://${lan_ip})"
  echo
  echo "Optional Caddy HTTPS ready: https://${lan_ip}"
  if [[ -n "$MDNS_HOSTNAME" ]]; then
    echo "Optional .local HTTPS ready: https://${MDNS_HOSTNAME}.local"
  fi
  if [[ -n "$CADDY_CERT_PATH" ]]; then
    echo "Install this certificate on client devices, then reload the browser: ${CADDY_CERT_PATH}"
  fi
}

main_providers_only() {
  # Settings -> Providers backend path: only provider installation in an
  # existing FXRoute checkout. Reuses the exact provider install flows so UI
  # installs match installer installs byte for byte, and refreshes the
  # ownership state afterwards so uninstall stays safe. Runs entirely
  # unprivileged except for the fixed root-owned helper actions
  # (provider_privileged); it never calls sudo itself, so no TTY/password
  # prompt can stall a Settings-UI request.
  require_cmd python3
  require_cmd getent
  require_cmd ps
  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]]; then
    require_cmd runuser
  fi
  # Fail fast when this checkout's installer_contract.py drifted from the
  # shell markers below: the 503 marker must never go stale silently.
  if ! verify_provider_contract_literals; then
    die "Provider contract literals diverged from installer_contract.py; sync install.sh with installer_contract.py"
  fi
  if ! provider_helper_usable >/dev/null 2>&1; then
    # Machine-readable contract line: main.py maps exactly this marker to
    # HTTP 503; the prose line stays for the shell operator only.
    printf '%s\n' "$PROVIDER_CONTRACT_HELPER_MISSING"
    die "Provider privilege helper unavailable; rerun the full install.sh once so Settings installs work without a password"
  fi
  confirm_supported_distro
  load_provider_ownership_state
  select_optional_providers
  STATE_CHECKPOINT_ENABLED=1
  trap 'checkpoint_install_state_on_exit' EXIT
  configure_optional_streaming
  ensure_target_user_ownership
  write_install_state
  echo
  echo "Provider setup finished:"
  echo " - Spotify Desktop: ${SPOTIFY_DESKTOP_PROVIDER_STATUS}"
  echo " - spotifyd: ${SPOTIFYD_PROVIDER_STATUS}"
  echo " - Qobuz/qbzd: ${QOBUZ_PROVIDER_STATUS}"
  echo " - TIDAL: ${TIDAL_PROVIDER_STATUS}"
}

main() {
  require_cmd python3
  require_cmd systemctl
  require_cmd getent
  require_cmd ps
  if [[ "$(id -u)" -eq 0 && "$FXROUTE_TARGET_USER" != "root" ]]; then
    require_cmd runuser
  fi
  choose_sudo
  confirm_supported_distro
  load_provider_ownership_state
  select_optional_providers
  if [[ $PROVIDERS_ONLY_MODE -eq 1 ]]; then
    main_providers_only
    return 0
  fi
  capture_lan_comfort_baseline
  ensure_native_packages
  ensure_dbus_send_binary
  ensure_firewall_cmd_binary
  sync_project_tree
  write_install_config
  ensure_target_user_ownership
  STATE_CHECKPOINT_ENABLED=1
  trap 'checkpoint_install_state_on_exit' EXIT
  create_env_if_missing
  ensure_no_foreign_fxroute_services
  install_network_library_helper
  install_provider_privileged_helper
  setup_python_env
  build_native_dsp_engine
  ensure_target_user_ownership
  enable_user_session_persistence
  ensure_target_user_audio_access
  enable_user_audio_services
  configure_pipewire_samplerates_if_available
  configure_dsp_ingress_sink
  ensure_target_user_ownership
  # Record the selected providers and baseline before provider side effects.
  write_install_state
  configure_optional_streaming
  ensure_target_user_ownership
  write_service_unit
  # Refresh ownership after provider setup and before late validation can abort the run.
  write_install_state
  setup_spotify_autostart
  run_as_target_user chmod +x "$INSTALL_ROOT/scripts/update_fxroute.sh"
  install_helpers
  configure_optional_maintenance_helpers
  configure_system_power_polkit_rule
  ensure_target_user_ownership
  validate_http
  validate_tools
  validate_pipewire_session
  offer_optional_local_lan_name
  install_mdns_guard
  offer_optional_caddy_proxy
  print_summary
  write_install_config
  finalize_firewalld_rule_format
  write_install_state
  ensure_target_user_ownership
}

main "$@"
