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
LOCAL_PROJECT_MODE=0
ASSUME_YES=0
PROVIDER_LIST=""
PROVIDER_SELECTION_EXPLICIT=0
SELECT_SPOTIFY_DESKTOP=0
SELECT_SPOTIFYD=0
SELECT_QOBUZ=0
SELECT_TIDAL=0
HOST_ARCH="$(uname -m)"

SPOTIFYD_VERSION="0.4.2"
QBZD_VERSION="2.0.2"
SPOTIFYD_ZEROCONF_PORT="4444"
QOBUZ_VOLUME_MODE_KEY="qconnect.volume_mode"
QOBUZ_REQUIRED_VOLUME_MODE="locked"
TIDAL_REQUIREMENTS_FILE="requirements-tidal.txt"
SPOTIFY_APT_SOURCE_FILE="/etc/apt/sources.list.d/spotify.list"
SPOTIFY_APT_KEY_FILE="/usr/share/keyrings/spotify-archive-keyring.gpg"
SPOTIFY_APT_KEY_URL="https://download.spotify.com/debian/pubkey_5384CE82BA52C83A.asc"
SPOTIFY_APT_KEY_FINGERPRINT="E1096BCBFF6D418796DE78515384CE82BA52C83A"

SPOTIFY_DESKTOP_PRESENT_BEFORE=0
SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_INSTALL_METHOD=""
SPOTIFY_DESKTOP_INSTALLED_VERSION=""
SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE=0
SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE=0
SPOTIFYD_PRESENT_BEFORE=0
SPOTIFYD_INSTALLED_BY_FXROUTE=0
SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE=0
SPOTIFYD_CONFIG_INSTALLED_BY_FXROUTE=0
SPOTIFYD_BINARY_PATH=""
SPOTIFYD_BINARY_SHA256=""
SPOTIFYD_SERVICE_PATH="$HOME/.config/systemd/user/spotifyd.service"
SPOTIFYD_SERVICE_SHA256=""
SPOTIFYD_CONFIG_PATH="$HOME/.config/spotifyd/spotifyd.conf"
QBZD_PRESENT_BEFORE=0
QBZD_INSTALLED_BY_FXROUTE=0
QBZD_SERVICE_INSTALLED_BY_FXROUTE=0
QBZD_BINARY_PATH=""
QBZD_BINARY_SHA256=""
QBZD_SERVICE_PATH="$HOME/.config/systemd/user/qbzd.service"
QBZD_SERVICE_SHA256=""
QBZD_VOLUME_MODE_BEFORE=""
QBZD_VOLUME_MODE_AFTER=""
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
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

VALIDATION_RESULTS=()
WARNINGS=()
PACKAGE_MANAGER=""
PACKAGE_INSTALL_CMD=()
PKG_REFRESH_DONE=0
SUDO_CMD=()
INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
INSTALL_CONFIG_FILE="$HOME/.config/fxroute/install-config.env"
FXROUTE_BACKUP_DIR="$HOME/.config/fxroute/backups"
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
MDNS_GUARD_ENABLED=0
MDNS_GUARD_OWNED_BY_FXROUTE=0
FIREWALLD_WAS_ACTIVE_BEFORE=0
HTTP_WAS_ALLOWED_BEFORE=0
HTTPS_WAS_ALLOWED_BEFORE=0
MDNS_WAS_ALLOWED_BEFORE=0
HTTP_OPENED_BY_FXROUTE=0
HTTPS_OPENED_BY_FXROUTE=0
POWER_POLKIT_INSTALLED=0
POWER_POLKIT_RULE_PATH=""
POWER_POLKIT_RULE_PRE_EXISTED=0
MDNS_OPENED_BY_FXROUTE=0
INSTALL_STATE_LOADED=0
STATE_CHECKPOINT_ENABLED=0

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
SPOTIFY_DESKTOP_AVAILABLE=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  --target <dir>        Install or refresh into this directory (default: $DEFAULT_INSTALL_ROOT)
  --local-project       Install in-place from the current project directory
  --source <dir>        Use a different local project source directory
  --providers <list>    Select comma-separated providers: spotify-desktop, spotifyd, qobuz, tidal, none
  --spotify-desktop     Select Spotify Desktop installation
  --spotifyd            Select spotifyd installation
  --qobuz               Select Qobuz/qbzd installation
  --tidal               Select the TIDAL Python dependency
  -y, --yes             Assume yes for package install prompts
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

determine_fxroute_target_identity() {
  if [[ ${EUID:-$(id -u)} -eq 0 && -n "${SUDO_USER:-}" ]]; then
    FXROUTE_TARGET_USER="$SUDO_USER"
  else
    FXROUTE_TARGET_USER="$(id -un)"
  fi
  FXROUTE_TARGET_UID="$(id -u "$FXROUTE_TARGET_USER" 2>/dev/null || true)"
  [[ -n "$FXROUTE_TARGET_UID" ]] || die "Could not determine the FXRoute target user UID for $FXROUTE_TARGET_USER"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || die "--target requires a directory"
      INSTALL_ROOT="$2"
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

expand_path() {
  python3 - <<'PY' "$1"
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

SOURCE_DIR="$(expand_path "$SOURCE_DIR")"
INSTALL_ROOT="$(expand_path "$INSTALL_ROOT")"

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
  [[ "$INSTALL_ROOT" != *[[:space:]]* && "$INSTALL_ROOT" != *%* \
    && "$INSTALL_ROOT" != *\"* && "$INSTALL_ROOT" != *\\* ]] \
    || die "FXRoute install roots contain characters that cannot be represented safely in systemd units or install state"

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
}

ensure_install_root_is_safe

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command missing: $1"
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

previous_install_state_field() {
  local field="$1"
  [[ -f "$INSTALL_STATE_FILE" ]] || return 1
  python3 - <<'PY' "$INSTALL_STATE_FILE" "$field"
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

  [[ -f "$INSTALL_STATE_FILE" ]] && INSTALL_STATE_LOADED=1
  [[ "$(previous_install_state_field providers.spotify_desktop.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.flatpak_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.apt_repo_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1
  [[ "$(previous_install_state_field providers.spotify_desktop.apt_key_installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE=1
  if value="$(previous_install_state_field providers.spotify_desktop.installed_version 2>/dev/null)"; then
    [[ -n "$value" ]] && SPOTIFY_DESKTOP_INSTALLED_VERSION="$value"
  fi
  [[ "$(previous_install_state_field providers.spotifyd.installed_by_fxroute 2>/dev/null || true)" == "true" ]] && SPOTIFYD_INSTALLED_BY_FXROUTE=1
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
  if value="$(previous_install_state_field lan_comfort.caddy_cert_path 2>/dev/null)"; then
    [[ -n "$value" ]] && CADDY_CERT_PATH="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.mdns_guard_enabled 2>/dev/null || true)" == "true" ]] && MDNS_GUARD_ENABLED=1
  if [[ "$(previous_install_state_field lan_comfort.mdns_guard_owned_by_fxroute 2>/dev/null || true)" == "true" ]]; then
    MDNS_GUARD_OWNED_BY_FXROUTE=1
  elif [[ -z "$(previous_install_state_field lan_comfort.mdns_guard_owned_by_fxroute 2>/dev/null || true)" \
    && $MDNS_GUARD_ENABLED -eq 1 ]]; then
    MDNS_GUARD_OWNED_BY_FXROUTE=1
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
  [[ "$(previous_install_state_field lan_comfort.power_polkit_installed 2>/dev/null || true)" == "true" ]] && POWER_POLKIT_INSTALLED=1
  if value="$(previous_install_state_field lan_comfort.power_polkit_rule_path 2>/dev/null)"; then
    [[ -n "$value" ]] && POWER_POLKIT_RULE_PATH="$value"
  fi
  [[ "$(previous_install_state_field lan_comfort.power_polkit_rule_pre_existed 2>/dev/null || true)" == "true" ]] && POWER_POLKIT_RULE_PRE_EXISTED=1
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
  firewalld_is_active || return 1
  "${SUDO_CMD[@]}" "$firewall_cmd" --query-port="$port" >/dev/null 2>&1 \
    || "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-port="$port" >/dev/null 2>&1
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

  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 1
  firewalld_query_port "$(firewall_rule_port "$rule_id")" && return 0
  service="$(firewalld_rule_service "$rule_id" 2>/dev/null || true)"
  [[ -n "$service" ]] || return 1
  "${SUDO_CMD[@]}" "$firewall_cmd" --query-service="$service" >/dev/null 2>&1 \
    || "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --query-service="$service" >/dev/null 2>&1
}

ensure_firewalld_rule() {
  local rule_id="$1"
  local purpose="$2"
  local port=""
  local firewall_cmd=""

  port="$(firewall_rule_port "$rule_id")" || return 0
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 0
  firewalld_is_active || return 0

  if firewalld_query_rule "$rule_id"; then
    return 0
  fi

  log "$firewall_cmd --permanent --add-port=$port"
  if ! "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --add-port="$port"; then
    warn "Optional LAN comfort could not open firewalld port '$port' for $purpose"
    return 0
  fi
  mark_firewall_rule_owned firewalld "$rule_id"

  log "$firewall_cmd --reload"
  if ! "${SUDO_CMD[@]}" "$firewall_cmd" --reload; then
    warn "Optional LAN comfort opened firewalld port '$port' permanently, but reload failed"
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

  port="$(firewall_rule_port "$rule_id")" || return 0
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

ensure_lan_firewall_rule() {
  local rule_id="$1"
  local purpose="$2"

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

choose_sudo() {
  if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
    SUDO_CMD=()
  elif command -v sudo >/dev/null 2>&1; then
    SUDO_CMD=(sudo)
  else
    die "sudo is required for package installation"
  fi
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

ensure_native_packages() {
  local core_packages=()
  local support_packages=(curl git socat)
  local audio_stack_packages=()
  local missing_packages=()
  local missing_support=()
  local missing_audio_stack=()
  local need_venv_pkg=0
  local need_bt_plugin_pkg=0

  case "$PACKAGE_MANAGER" in
    apt)
      core_packages=(python3 python3-pip python3-venv mpv ffmpeg playerctl)
      audio_stack_packages=(bluez wireplumber pipewire-bin pipewire-pulse pulseaudio-utils libspa-0.2-bluetooth)
      ;;
    dnf)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl)
      audio_stack_packages=(bluez wireplumber pipewire-utils pipewire-pulseaudio pulseaudio-utils)
      ;;
    zypper)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl)
      audio_stack_packages=(bluez wireplumber pipewire-tools pipewire-pulseaudio pulseaudio-utils pipewire-spa-plugins-0_2)
      ;;
    pacman)
      core_packages=(python python-pip mpv ffmpeg playerctl)
      audio_stack_packages=(bluez bluez-utils wireplumber pipewire pipewire-pulse libpulse)
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

  if [[ $need_venv_pkg -eq 1 ]] && ! python3 -m venv --help >/dev/null 2>&1; then
    case "$PACKAGE_MANAGER" in
      dnf)
        pkg_install python3-virtualenv
        ;;
      zypper)
        pkg_install python3-virtualenv
        ;;
      pacman)
        # python on Arch/Manjaro ships the venv module; no extra package needed.
        ;;
      *)
        die "python3 venv support is missing after package install"
        ;;
    esac
  fi

  for cmd in python3 mpv ffmpeg playerctl curl git socat bluetoothctl wpctl pw-cli pactl; do
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

install_network_library_helper() {
  local helper_src="$INSTALL_ROOT/scripts/fxroute-cifs-mount"
  local helper_path="/usr/local/sbin/fxroute-cifs-mount"
  local sudoers_path="/etc/sudoers.d/fxroute-cifs-mount"
  local tmp_sudoers=""

  [[ -f "$helper_src" ]] || die "Missing network library mount helper: $helper_src"
  "${SUDO_CMD[@]}" install -m 755 "$helper_src" "$helper_path"
  tmp_sudoers="$(mktemp)"
  local install_user="$FXROUTE_TARGET_USER"
  [[ "$install_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || die "Invalid install user for CIFS helper"
  printf '%s ALL=(root) NOPASSWD: %s *\n' "$install_user" "$helper_path" > "$tmp_sudoers"
  if command -v visudo >/dev/null 2>&1; then
    "${SUDO_CMD[@]}" visudo -cf "$tmp_sudoers" >/dev/null
  fi
  "${SUDO_CMD[@]}" install -m 440 "$tmp_sudoers" "$sudoers_path"
  rm -f "$tmp_sudoers"
  pass "network library CIFS helper installed"
}

sync_project_tree() {
  mkdir -p "$INSTALL_ROOT"
  if [[ $LOCAL_PROJECT_MODE -eq 1 ]]; then
    log "Using local project install mode at $INSTALL_ROOT"
    pass "project install mode: local-project"
    return
  fi

  log "Syncing project into $INSTALL_ROOT"
  tar \
    --exclude='.venv' \
    --exclude='.env' \
    --exclude='__pycache__' \
    --exclude='backups' \
    --exclude='*.pyc' \
    --exclude='outputs/*.patch' \
    -C "$SOURCE_DIR" -cf - . | tar -C "$INSTALL_ROOT" -xf -
  cleanup_obsolete_root_modules
  pass "project synced to target directory"
}

cleanup_obsolete_root_modules() {
  local helper="$INSTALL_ROOT/scripts/fxroute_obsolete_root_cleanup.py"
  [[ -f "$helper" ]] || {
    log "Obsolete root-module cleanup helper not found; skipping."
    return 0
  }
  if python3 "$helper" --root "$INSTALL_ROOT"; then
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

  cat > "$env_file" <<EOF
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

  mkdir -p "$music_root/$downloads_dir"
  pass ".env created"
}

flatpak_app_installed() {
  local app_id="$1"

  command -v flatpak >/dev/null 2>&1 || return 1

  if flatpak list --app --user --columns=application 2>/dev/null | grep -Fxq "$app_id"; then
    return 0
  fi

  if flatpak list --app --system --columns=application 2>/dev/null | grep -Fxq "$app_id"; then
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

  if [[ $existing_source -eq 0 ]]; then
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
    fi
    rm -f "$tmp_key"
    SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1
    if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_source" "$SPOTIFY_APT_SOURCE_FILE"; then
      rm -f "$tmp_source"
      return 1
    fi
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
  user_unit_exists spotifyd.service && SPOTIFYD_PRESENT_BEFORE=1

  if [[ -n "$(qbzd_binary_path || true)" ]]; then
    QBZD_PRESENT_BEFORE=1
    if [[ $SELECT_QOBUZ -eq 0 ]]; then
      QOBUZ_PROVIDER_STATUS="already present (not selected; preserved)"
      pass "qbzd already present; not selected, preserving it"
    fi
  elif [[ $SELECT_QOBUZ -eq 0 ]]; then
    QOBUZ_PROVIDER_STATUS="not selected; not installed"
  fi
  user_unit_exists qbzd.service && QBZD_PRESENT_BEFORE=1
}

spotifyd_binary_path() {
  if [[ -e "$HOME/.local/bin/spotifyd" || -L "$HOME/.local/bin/spotifyd" ]]; then
    printf '%s\n' "$HOME/.local/bin/spotifyd"
    return 0
  fi
  command -v spotifyd 2>/dev/null || true
}

write_spotifyd_config() {
  local config_dir="$HOME/.config/spotifyd"
  local config_path="$config_dir/spotifyd.conf"

  SPOTIFYD_CONFIG_PATH="$config_path"
  mkdir -p "$config_dir"
  if [[ -f "$config_path" ]]; then
    pass "spotifyd config preserved; FXRoute service pins Zeroconf port ${SPOTIFYD_ZEROCONF_PORT}"
    return 0
  fi

  cat > "$config_path" <<EOF
[global]
device_name = "FXRoute"
device_type = "speaker"
backend = "pulseaudio"
use_mpris = true
dbus_type = "session"
# Remote Connect volume is routed to the FXRoute master by the spotifyd
# volume watch; the source itself must never attenuate (unity contract).
volume_controller = "none"
zeroconf_port = ${SPOTIFYD_ZEROCONF_PORT}
EOF
  chmod 600 "$config_path"
  SPOTIFYD_CONFIG_INSTALLED_BY_FXROUTE=1
  pass "spotifyd config created for FXRoute/PipeWire-Pulse"
}

install_spotifyd_binary() {
  local release_arch=""
  local archive=""
  local archive_url=""
  local checksum=""
  local work=""
  local extracted=""
  local binary=""

  if [[ -n "$(spotifyd_binary_path || true)" ]]; then
    SPOTIFYD_PRESENT_BEFORE=1
    SPOTIFYD_BINARY_PATH="$(spotifyd_binary_path || true)"
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
      ;;
    aarch64)
      archive="spotifyd-linux-aarch64-full.tar.gz"
      checksum="f93451e7e6537e4fad76c4cfb6e28f20da39c0c3614ee523cc8d6c98b4a913649598aed66bb2a032ab48b90a38970a2c0206eaec656721b131d3b28c4ac5112c"
      ;;
    armv7)
      archive="spotifyd-linux-armv7-full.tar.gz"
      checksum="befed77ab3ba5b688ad0c054890576010135a8ba007422385ee3a68763a7ae99a566e010fb29f75c429354245c600da8abc826de4a57ad616c7b2a05dcf8b9b8"
      ;;
  esac

  archive_url="https://github.com/Spotifyd/spotifyd/releases/download/v${SPOTIFYD_VERSION}/${archive}"
  work="$(mktemp -d -t fxroute-spotifyd.XXXXXX)"
  trap 'rm -rf "$work"' RETURN
  run_cmd curl -fL --retry 3 -o "$work/$archive" "$archive_url"
  printf '%s  %s\n' "$checksum" "$work/$archive" | sha512sum -c -
  run_cmd tar -xzf "$work/$archive" -C "$work"
  extracted="$(find "$work" -type f -name spotifyd -perm -u+x -print -quit)"
  [[ -n "$extracted" ]] || die "spotifyd archive did not contain an executable"
  mkdir -p "$HOME/.local/bin"
  install -m 755 "$extracted" "$HOME/.local/bin/spotifyd"
  SPOTIFYD_BINARY_PATH="$HOME/.local/bin/spotifyd"
  SPOTIFYD_INSTALLED_BY_FXROUTE=1
  SPOTIFYD_BINARY_SHA256="$(sha256sum "$SPOTIFYD_BINARY_PATH" | awk '{print $1}')"
  pass "spotifyd v${SPOTIFYD_VERSION} installed (${release_arch}, full/MPRIS)"
}

configure_spotifyd_service() {
  local service_dir="$HOME/.config/systemd/user"
  local service_path="$service_dir/spotifyd.service"
  local config_path="$HOME/.config/spotifyd/spotifyd.conf"
  local binary_path="$(spotifyd_binary_path || true)"

  SPOTIFYD_SERVICE_PATH="$service_path"
  [[ -n "$binary_path" ]] || {
    warn "spotifyd service skipped because no spotifyd binary is available"
    return 0
  }

  if user_unit_exists spotifyd.service; then
    pass "existing spotifyd user service preserved"
    return 0
  fi

  mkdir -p "$service_dir"
  cat > "$service_path" <<EOF
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

  if systemctl --user daemon-reload && systemctl --user enable --now spotifyd.service; then
    pass "spotifyd user service enabled"
  else
    warn "spotifyd service was installed, but could not be enabled in this shell"
  fi
}

install_spotifyd() {
  local was_present=0
  local spotifyd_path=""

  if ! spotifyd_arch_for_host >/dev/null 2>&1; then
    SPOTIFYD_PROVIDER_STATUS="unsupported architecture"
    warn "spotifyd is not available for ${HOST_ARCH}; skipping"
    return 0
  fi

  [[ -n "$(spotifyd_binary_path || true)" ]] && was_present=1
  install_spotifyd_binary
  [[ -n "$(spotifyd_binary_path || true)" ]] || return 0
  if [[ ! -x "$(spotifyd_binary_path || true)" ]]; then
    SPOTIFYD_PROVIDER_STATUS="existing path is not executable; preserved"
    return 0
  fi
  write_spotifyd_config
  configure_spotifyd_service
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
  echo "spotifyd first run: select FXRoute in Spotify Connect. If OAuth is needed, stop the service and run:"
  echo "  systemctl --user stop spotifyd && ${spotifyd_path:-$HOME/.local/bin/spotifyd} authenticate --config-path $HOME/.config/spotifyd/spotifyd.conf"
  echo "  systemctl --user start spotifyd"
}

qobuz_runtime_packages_for_manager() {
  case "$1" in
    apt) echo "libasound2 libdbus-1-3 avahi-daemon libavahi-client3 libnss-mdns" ;;
    dnf) echo "alsa-lib dbus-libs avahi nss-mdns" ;;
    zypper) echo "alsa libdbus-1-3 avahi libavahi-client3 nss-mdns" ;;
    pacman) echo "alsa-lib dbus avahi nss-mdns" ;;
    *) return 1 ;;
  esac
}

ensure_qobuz_runtime_dependencies() {
  local was_present="$AVAHI_WAS_PRESENT_BEFORE"
  local was_active="$AVAHI_WAS_ACTIVE_BEFORE"
  local was_enabled="$AVAHI_WAS_ENABLED_BEFORE"

  if [[ "$was_present" == "0" ]]; then
    AVAHI_INSTALLED_BY_FXROUTE=1
  fi
  install_missing_provider_packages "$(qobuz_runtime_packages_for_manager "$PACKAGE_MANAGER")"
  if avahi_is_present && ! systemctl is-active avahi-daemon >/dev/null 2>&1; then
    if [[ "$was_active" == "0" || "$was_enabled" == "0" ]]; then
      AVAHI_ENABLED_BY_FXROUTE=1
    fi
    if "${SUDO_CMD[@]}" systemctl enable --now avahi-daemon; then
      pass "Avahi enabled for Qobuz Connect discovery"
    else
      warn "Avahi is installed but could not be enabled for Qobuz Connect discovery"
    fi
  fi
  ensure_lan_firewall_service_open mdns "Qobuz Connect discovery"
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

  if [[ -n "$(qbzd_binary_path || true)" ]]; then
    QBZD_PRESENT_BEFORE=1
    QBZD_BINARY_PATH="$(qbzd_binary_path || true)"
    if [[ -f "$QBZD_BINARY_PATH" ]]; then
      current_sha256="$(sha256sum "$QBZD_BINARY_PATH" | awk '{print $1}')"
      if [[ $QBZD_INSTALLED_BY_FXROUTE -eq 1 \
        && -n "$QBZD_BINARY_SHA256" \
        && "$current_sha256" != "$QBZD_BINARY_SHA256" ]]; then
        QBZD_BINARY_IDENTITY_CHANGED=1
        warn "FXRoute-owned qbzd binary checksum changed; preserving it and skipping provider setup"
      elif [[ $QBZD_INSTALLED_BY_FXROUTE -eq 0 || -z "$QBZD_BINARY_SHA256" ]]; then
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
  trap 'rm -rf "$work"' RETURN
  run_cmd curl -fL --retry 3 -o "$work/$archive" "$archive_url"
  printf '%s  %s\n' "$checksum" "$work/$archive" | sha256sum -c -
  run_cmd tar -xzf "$work/$archive" -C "$work"
  extracted="$(find "$work" -type f -name qbzd -perm -u+x -print -quit)"
  [[ -n "$extracted" ]] || die "qbzd archive did not contain an executable"
  mkdir -p "$HOME/.local/bin"
  install -m 755 "$extracted" "$HOME/.local/bin/qbzd"
  QBZD_BINARY_PATH="$HOME/.local/bin/qbzd"
  QBZD_INSTALLED_BY_FXROUTE=1
  QBZD_BINARY_SHA256="$(sha256sum "$QBZD_BINARY_PATH" | awk '{print $1}')"
  pass "qbzd v${QBZD_VERSION} installed (${release_arch})"
}

read_qbzd_volume_mode() {
  local binary_path=""
  binary_path="$(qbzd_binary_path || true)"
  [[ -n "$binary_path" ]] || return 1
  "$binary_path" settings show --quiet --json 2>/dev/null | python3 -c '
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
  "$binary_path" settings set --quiet "$QOBUZ_VOLUME_MODE_KEY" "$mode"
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

configure_qbzd_service() {
  local service_dir="$HOME/.config/systemd/user"
  local service_path="$service_dir/qbzd.service"
  local binary_path="$(qbzd_binary_path || true)"

  QBZD_SERVICE_PATH="$service_path"
  [[ -n "$binary_path" ]] || {
    warn "qbzd service skipped because no qbzd binary is available"
    return 0
  }

  if user_unit_exists qbzd.service; then
    if [[ $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 1 ]] && systemctl --user is-active --quiet qbzd.service; then
      if ! systemctl --user restart qbzd.service; then
        die "Could not restart the existing qbzd service after setting qconnect.volume_mode=locked"
      fi
    fi
    pass "existing qbzd user service preserved"
    return 0
  fi

  mkdir -p "$service_dir"
  cat > "$service_path" <<EOF
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

  if systemctl --user daemon-reload && systemctl --user enable --now qbzd.service; then
    pass "qbzd user service enabled"
  else
    warn "qbzd service was installed, but could not be enabled in this shell"
  fi
}

install_qobuz() {
  local qbzd_path=""

  if [[ -z "$(qbzd_binary_path || true)" ]] && ! qbzd_arch_for_host >/dev/null 2>&1; then
    QOBUZ_PROVIDER_STATUS="unsupported architecture"
    warn "Qobuz/qbzd is not available for ${HOST_ARCH}; skipping"
    return 0
  fi

  ensure_qobuz_runtime_dependencies
  install_qbzd_binary
  [[ -n "$(qbzd_binary_path || true)" ]] || return 0
  if [[ $QBZD_BINARY_IDENTITY_CHANGED -eq 1 ]]; then
    QOBUZ_PROVIDER_STATUS="owned binary changed; preserved"
    return 0
  fi
  if [[ ! -x "$(qbzd_binary_path || true)" ]]; then
    QOBUZ_PROVIDER_STATUS="existing path is not executable; preserved"
    return 0
  fi
  configure_qbzd_volume_mode
  configure_qbzd_service
  qbzd_path="$(qbzd_binary_path || true)"
  if [[ $QBZD_INSTALLED_BY_FXROUTE -eq 1 || $QBZD_SERVICE_INSTALLED_BY_FXROUTE -eq 1 ]]; then
    QOBUZ_PROVIDER_STATUS="installed/configured by FXRoute"
  else
    QOBUZ_PROVIDER_STATUS="already present; service preserved"
  fi
  echo "Qobuz first run: run '${qbzd_path:-$HOME/.local/bin/qbzd} setup' and complete the browser-based OAuth login."
  echo "Then enable Qobuz Connect in qbzd and select its device from the Qobuz app."
}

configure_optional_streaming() {
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
  run_cmd python3 -m venv "$venv_dir"
  run_cmd "$venv_dir/bin/python3" -m pip install --upgrade pip setuptools wheel
  run_cmd "$venv_dir/bin/pip" install -r "$INSTALL_ROOT/requirements.txt"
  sha256sum "$INSTALL_ROOT/requirements.txt" | awk '{print $1}' > "$marker"
  pass "Python venv created"
  pass "pip install -r requirements.txt"
}

tidalapi_version() {
  local python_bin="$INSTALL_ROOT/.venv/bin/python3"
  [[ -x "$python_bin" ]] || return 0
  "$python_bin" - <<'PY'
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
  run_cmd "$INSTALL_ROOT/.venv/bin/pip" install -r "$INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE"
  sha256sum "$INSTALL_ROOT/$TIDAL_REQUIREMENTS_FILE" | awk '{print $1}' > "$marker"
  TIDAL_INSTALLED_VERSION="$(tidalapi_version || true)"
  TIDAL_PROVIDER_STATUS="installed by FXRoute"
  pass "TIDAL dependency installed (PKCE flow remains in FXRoute)"
}

write_service_unit() {
  local service_dir="$HOME/.config/systemd/user"
  mkdir -p "$service_dir"

  cat > "$service_dir/$SERVICE_NAME.service" <<EOF
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

  if systemctl --user daemon-reload; then
    if systemctl --user enable "$SERVICE_NAME" && systemctl --user restart "$SERVICE_NAME"; then
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
    if systemctl --user enable --now "${targets[@]}" >/dev/null 2>&1; then
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
  if loginctl show-user "$install_user" -p Linger --value 2>/dev/null | grep -qx 'yes'; then
    pass "user session persistence already active (loginctl enable-linger)"
    return 0
  fi
  if "${SUDO_CMD[@]}" loginctl enable-linger "$install_user"; then
    pass "user session persistence enabled (loginctl enable-linger)"
  else
    warn "loginctl enable-linger failed; FXRoute user services will stop when the login session ends"
  fi
}

configure_dsp_ingress_sink() {
  local config_dir="$HOME/.config/pipewire/pipewire-pulse.conf.d"
  local config_file="$config_dir/50-fxroute-dsp-sink.conf"

  mkdir -p "$config_dir"
  cat > "$config_file" <<'EOF'
pulse.cmd = [
  { cmd = "load-module" args = "module-null-sink sink_name=fxroute_dsp_sink sink_properties=device.description=FXRoute_DSP_Ingress" flags = [ ] }
]
EOF
  if systemctl --user restart pipewire-pulse.service; then
    pass "FXRoute DSP ingress sink configured"
  else
    warn "FXRoute DSP ingress sink config was written, but pipewire-pulse could not be restarted in this shell"
  fi
}

disable_legacy_samplerate_override() {
  systemctl --user stop switch-sample-rate.service >/dev/null 2>&1 || true
  systemctl --user disable switch-sample-rate.service >/dev/null 2>&1 || true
  rm -f "$HOME/.config/systemd/user/switch-sample-rate.service" "$HOME/switch-sample-rate.sh"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
}

configure_pipewire_samplerates_if_available() {
  local script="$INSTALL_ROOT/scripts/configure-pipewire-samplerates.sh"

  [[ -f "$script" ]] || return
  chmod +x "$script"

  disable_legacy_samplerate_override

  if "$script" apply; then
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

  mkdir -p "$autostart_dir"

  if ! env_setting_enabled "$enabled_value"; then
    rm -f "$desktop_file"
    pass "Spotify autostart disabled"
    return
  fi

  if ! detect_spotify_autostart_command >/dev/null; then
    rm -f "$desktop_file"
    warn "Spotify autostart is enabled in .env, but no local Spotify desktop app (Flatpak or native) was found"
    return
  fi

  [[ -f "$script_path" ]] || {
    warn "Spotify autostart could not be enabled because $script_path is missing"
    return
  }

  chmod +x "$script_path"

  if [[ -f "$legacy_desktop_file" ]] && grep -q "spotify-clean-start.sh" "$legacy_desktop_file"; then
    rm -f "$legacy_desktop_file"
    pass "legacy Spotify clean-start autostart replaced"
  fi

  cat > "$desktop_file" <<EOF
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
  local temp_file=""
  LAN_HOSTNAME_AFTER="${LAN_HOSTNAME_AFTER:-$(hostname 2>/dev/null || true)}"
  mkdir -p "$(dirname "$state_file")"
  temp_file="$(mktemp "$(dirname "$state_file")/.install-state.XXXXXX")"
  if ! cat > "$temp_file" <<EOF
{
  "install_root": "${INSTALL_ROOT}",
  "providers": {
    "spotify_desktop": {
      "selected": $( [[ $SELECT_SPOTIFY_DESKTOP -eq 1 ]] && echo true || echo false ),
      "present_before": $( [[ $SPOTIFY_DESKTOP_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
      "install_method": "${SPOTIFY_DESKTOP_INSTALL_METHOD}",
      "installed_version": "${SPOTIFY_DESKTOP_INSTALLED_VERSION}",
      "installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "flatpak_installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_FLATPAK_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "apt_repo_installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "apt_key_installed_by_fxroute": $( [[ $SPOTIFY_DESKTOP_KEY_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false )
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
      "config_path": "${SPOTIFYD_CONFIG_PATH}"
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
      "volume_mode_changed_by_fxroute": $( [[ $QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE -eq 1 ]] && echo true || echo false )
    },
    "tidal": {
      "selected": $( [[ $TIDAL_DEPENDENCY_SELECTED -eq 1 ]] && echo true || echo false ),
      "present_before": $( [[ $TIDAL_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
      "installed_by_fxroute": $( [[ $TIDAL_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
      "installed_version": "${TIDAL_INSTALLED_VERSION}"
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
    "mdns_guard_enabled": $( [[ $MDNS_GUARD_ENABLED -eq 1 ]] && echo true || echo false ),
    "mdns_guard_owned_by_fxroute": $( [[ $MDNS_GUARD_OWNED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "firewalld_was_active_before": $( [[ $FIREWALLD_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_was_allowed_before": $( [[ $HTTP_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "https_was_allowed_before": $( [[ $HTTPS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "mdns_was_allowed_before": $( [[ $MDNS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_opened_by_fxroute": $( [[ $HTTP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "https_opened_by_fxroute": $( [[ $HTTPS_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "mdns_opened_by_fxroute": $( [[ $MDNS_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "firewall_ownership_schema": 2,
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
    "power_polkit_rule_pre_existed": $( [[ $POWER_POLKIT_RULE_PRE_EXISTED -eq 1 ]] && echo true || echo false )
  }
}
EOF
  then
    rm -f "$temp_file"
    return 1
  fi
  chmod 600 "$temp_file"
  if ! mv -f "$temp_file" "$state_file"; then
    rm -f "$temp_file"
    return 1
  fi
  pass "install state recorded"
}

write_install_config() {
  local install_user=""
  local temp_file=""
  install_user="$FXROUTE_TARGET_USER"
  mkdir -p "$(dirname "$INSTALL_CONFIG_FILE")"
  temp_file="$(mktemp "$(dirname "$INSTALL_CONFIG_FILE")/.install-config.XXXXXX")"
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
  if ! mv -f "$temp_file" "$INSTALL_CONFIG_FILE"; then
    rm -f "$temp_file"
    return 1
  fi
  chmod 600 "$INSTALL_CONFIG_FILE"
  pass "install config recorded"
}

checkpoint_install_state_on_exit() {
  local exit_status="$?"
  if [[ "$exit_status" -ne 0 && "$STATE_CHECKPOINT_ENABLED" -eq 1 ]]; then
    write_install_config >/dev/null 2>&1 || true
    write_install_state >/dev/null 2>&1 || true
  fi
}

install_helpers() {
  local bin_dir="$HOME/.local/bin"
  mkdir -p "$bin_dir"

  cat > "$bin_dir/fxroute-status" <<EOF
#!/usr/bin/env bash
exec systemctl --user status $SERVICE_NAME
EOF

  cat > "$bin_dir/fxroute-logs" <<EOF
#!/usr/bin/env bash
exec journalctl --user -u $SERVICE_NAME -f
EOF

  cat > "$bin_dir/fxroute-restart" <<EOF
#!/usr/bin/env bash
exec systemctl --user restart $SERVICE_NAME
EOF

  cat > "$bin_dir/fxroute-update" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/scripts/update_fxroute.sh" "\$@"
EOF

  cat > "$bin_dir/fxroute-update-ytdlp" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/.venv/bin/pip" install -U yt-dlp
EOF

  chmod +x "$bin_dir"/fxroute-*
  pass "helper commands installed in $bin_dir"
}

build_native_dsp_engine() {
  local build_script="$INSTALL_ROOT/native_dsp/build.sh"
  local binary="$INSTALL_ROOT/native_dsp/build/fxroute-dsp"
  local dsp_packages=()

  [[ -f "$build_script" ]] || die "Missing FXRoute native DSP build script: $build_script"
  case "$PACKAGE_MANAGER" in
    apt) dsp_packages=(gcc pkg-config libpipewire-0.3-dev libspa-0.2-dev liblilv-dev lilv-utils lv2-dev lsp-plugins-lv2 zam-plugins calf-plugins libebur128-dev libsamplerate0-dev libspeexdsp-dev) ;;
    dnf) dsp_packages=(gcc pkgconf-pkg-config pipewire-devel lilv lilv-devel lv2-devel lsp-plugins-lv2 zam-plugins-lv2 lv2-calf-plugins libebur128-devel libsamplerate-devel speexdsp-devel) ;;
    zypper) dsp_packages=(gcc gcc-c++ cmake pkgconf-pkg-config pipewire-devel lilv liblilv-0-devel lv2-devel lv2-lsp-plugins lv2-zam-plugins libebur128-devel libsamplerate-devel speexdsp-devel libexpat-devel fluidsynth-devel) ;;
    pacman) dsp_packages=(gcc pkgconf libpipewire lilv lv2 lsp-plugins zam-plugins calf libebur128 libsamplerate speexdsp) ;;
  esac
  [[ ${#dsp_packages[@]} -eq 0 ]] || pkg_install "${dsp_packages[@]}"

  if [[ "$PACKAGE_MANAGER" == "zypper" ]] && ! lv2ls 2>/dev/null | grep -Fxq 'http://calf.sourceforge.net/plugins/BassEnhancer'; then
    install_calf_lv2_from_source
  fi

  log "Building FXRoute native DSP engine"
  bash "$build_script"
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
  trap 'rm -rf "$work"' RETURN

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
  mkdir -p "$HOME/.lv2"
  candidate="$HOME/.lv2/.calf.lv2.new.$$"
  previous="$HOME/.lv2/.calf.lv2.old.$$"
  rm -rf "$candidate" "$previous"
  cp -a "$bundle" "$candidate"
  if [[ -e "$HOME/.lv2/calf.lv2" ]]; then
    mv "$HOME/.lv2/calf.lv2" "$previous"
  fi
  if mv "$candidate" "$HOME/.lv2/calf.lv2"; then
    rm -rf "$previous"
  else
    [[ ! -e "$previous" ]] || mv "$previous" "$HOME/.lv2/calf.lv2"
    die "Failed to install the staged Calf LV2 bundle"
  fi
  lv2ls 2>/dev/null | grep -Fxq 'http://calf.sourceforge.net/plugins/BassEnhancer' \
    || die "Calf Bass Enhancer is unavailable after source installation"
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

  mkdir -p "$user_systemd_dir"

  if ! env_setting_enabled "$enabled_value"; then
    systemctl --user disable --now "$timer_name" >/dev/null 2>&1 || true
    rm -f "$user_systemd_dir/$service_name" "$user_systemd_dir/$timer_name"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    pass "Spotify cache cleanup helper disabled"
    return
  fi

  [[ -f "$script_path" ]] || {
    warn "Spotify cache cleanup helper could not be enabled because $script_path is missing"
    return
  }

  chmod +x "$script_path"

  cat > "$user_systemd_dir/$service_name" <<EOF
[Unit]
Description=FXRoute Spotify cache cleanup
After=default.target

[Service]
Type=oneshot
ExecStart=$script_path
EOF

  cat > "$user_systemd_dir/$timer_name" <<EOF
[Unit]
Description=Run FXRoute Spotify cache cleanup periodically

[Timer]
OnBootSec=20min
OnUnitActiveSec=${interval_hours}h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  if systemctl --user daemon-reload && systemctl --user enable --now "$timer_name"; then
    pass "Spotify cache cleanup helper enabled (${interval_hours}h)"
  else
    warn "Spotify cache cleanup helper files were installed, but the timer could not be enabled in this shell"
  fi
}

configure_system_auto_update_helper() {
  local env_file="$INSTALL_ROOT/.env"
  local service_name="fxroute-system-update.service"
  local timer_name="fxroute-system-update.timer"
  local service_path="/etc/systemd/system/$service_name"
  local timer_path="/etc/systemd/system/$timer_name"
  local script_path="$INSTALL_ROOT/scripts/system-package-update.sh"
  local enabled_value="$(read_env_value SYSTEM_AUTO_UPDATE "$env_file")"
  local interval_value="$(read_env_value SYSTEM_AUTO_UPDATE_INTERVAL_HOURS "$env_file")"
  local interval_hours="$(env_interval_hours_or_default "$interval_value" 24)"
  local tmp_service=""
  local tmp_timer=""

  if ! env_setting_enabled "$enabled_value"; then
    "${SUDO_CMD[@]}" systemctl disable --now "$timer_name" >/dev/null 2>&1 || true
    "${SUDO_CMD[@]}" rm -f "$service_path" "$timer_path" >/dev/null 2>&1 || true
    "${SUDO_CMD[@]}" systemctl daemon-reload >/dev/null 2>&1 || true
    pass "Optional system auto-update helper disabled"
    return
  fi

  [[ -f "$script_path" ]] || {
    warn "System auto-update helper could not be enabled because $script_path is missing"
    return
  }

  chmod +x "$script_path"
  tmp_service="$(mktemp)"
  tmp_timer="$(mktemp)"

  cat > "$tmp_service" <<EOF
[Unit]
Description=FXRoute optional system package update helper
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$script_path
EOF

  cat > "$tmp_timer" <<EOF
[Unit]
Description=Run FXRoute optional system package updates periodically

[Timer]
OnBootSec=30min
OnUnitActiveSec=${interval_hours}h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_service" "$service_path"; then
    warn "Failed to install optional system auto-update service"
    rm -f "$tmp_service" "$tmp_timer"
    return
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_timer" "$timer_path"; then
    warn "Failed to install optional system auto-update timer"
    rm -f "$tmp_service" "$tmp_timer"
    return
  fi
  rm -f "$tmp_service" "$tmp_timer"

  if "${SUDO_CMD[@]}" systemctl daemon-reload && "${SUDO_CMD[@]}" systemctl enable --now "$timer_name"; then
    pass "Optional system auto-update helper enabled (${interval_hours}h)"
  else
    warn "Optional system auto-update helper files were installed, but the timer could not be enabled"
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
  local port_listing=""
  port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  [[ -n "$port" ]] || port=8000

  local deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    service_pid="$(systemctl --user show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
    if [[ -n "$service_pid" && "$service_pid" != "0" ]] && systemctl --user is-active --quiet "${SERVICE_NAME}.service"; then
      port_listing="$(ss -ltnp 2>/dev/null | grep -E ":${port}\\b" || true)"
      if grep -q "pid=${service_pid}," <<<"$port_listing"; then
        pass "HTTP port owned by FXRoute service"
        break
      fi
    fi
    sleep 1
  done

  service_pid="$(systemctl --user show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
  if [[ -z "$service_pid" || "$service_pid" == "0" ]] || ! systemctl --user is-active --quiet "${SERVICE_NAME}.service"; then
    fail "FXRoute service running"
    warn "FXRoute user service is not active after install; check: systemctl --user status ${SERVICE_NAME}.service"
    return
  fi

  port_listing="$(ss -ltnp 2>/dev/null | grep -E ":${port}\\b" || true)"
  if ! grep -q "pid=${service_pid}," <<<"$port_listing"; then
    fail "HTTP port owned by FXRoute service"
    warn "Port ${port} is not owned by ${SERVICE_NAME}.service MainPID ${service_pid}; another process may be answering health checks"
    [[ -n "$port_listing" ]] && warn "Port ${port} listeners: ${port_listing//$'\n'/; }"
    return
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
    die "LV2 plugin verification needs lv2ls from lilv-utils (Debian/Ubuntu) or lilv (Fedora/openSUSE/Arch)"
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
    die "FXRoute DSP effects need these LV2 plugins: ${missing[*]}; install lsp-plugins-lv2 zam-plugins calf-plugins (Debian/Ubuntu/Armbian), lsp-plugins-lv2 zam-plugins-lv2 lv2-calf-plugins (Fedora), lv2-lsp-plugins lv2-zam-plugins (openSUSE), or lsp-plugins zam-plugins calf (Arch/Manjaro)"
  fi
}

validate_tools() {
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
  "$INSTALL_ROOT/.venv/bin/yt-dlp" --version >/dev/null 2>&1 && pass "yt-dlp available from venv" || fail "yt-dlp available from venv"

  [[ -x "$INSTALL_ROOT/native_dsp/build/fxroute-dsp" ]] \
    && pass "FXRoute native DSP engine available" \
    || fail "FXRoute native DSP engine available"
  pactl list sinks short 2>/dev/null | awk '{print $2}' | grep -Fxq fxroute_dsp_sink \
    && pass "FXRoute DSP ingress sink available" \
    || fail "FXRoute DSP ingress sink available"

  if bt_plugin_present; then
    pass "PipeWire BlueZ SPA plugin found"
  else
    fail "PipeWire BlueZ SPA plugin found"
    warn "Bluetooth input mode needs the PipeWire BlueZ SPA plugin (libspa-bluez5.so) on the host."
  fi

  if bluetoothctl show >/dev/null 2>&1; then
    pass "BlueZ controller query works"
  else
    warn "bluetoothctl show failed in this shell. Bluetooth input mode will stay unavailable until BlueZ is active and an adapter/controller is visible."
  fi

  if systemctl --user is-enabled "$SERVICE_NAME" >/dev/null 2>&1; then
    pass "service enabled"
  else
    fail "service enabled"
  fi

  verify_lv2_plugins
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

apply_rules() {
  "\$NFT" delete table inet "\$TABLE" 2>/dev/null || true
  "\$NFT" -f - <<RULES
 table inet \${TABLE} {
   chain output {
     type filter hook output priority 5; policy accept;
     meta skuid \${USER_ID} ip daddr 224.0.0.251 udp dport 5353 counter drop comment "Block desktop user-space mDNS v4 to keep Avahi host advertisement stable"
     meta skuid \${USER_ID} ip6 daddr ff02::fb udp dport 5353 counter drop comment "Block desktop user-space mDNS v6 to keep Avahi host advertisement stable"
   }
 }
RULES
}

remove_rules() {
  if "\$NFT" list table inet "\$TABLE" >/dev/null 2>&1; then
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
  "${SUDO_CMD[@]}" "$nft_path" delete table inet fxroute_mdnsguard >/dev/null 2>&1
}

remove_mdns_guard_if_present() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"

  if [[ $MDNS_GUARD_OWNED_BY_FXROUTE -ne 1 ]]; then
    if [[ -e "$script_path" || -L "$script_path" \
      || -e "$service_path" || -L "$service_path" \
      || -e "$timer_path" || -L "$timer_path" ]]; then
      warn "Preserving mDNS guard artifacts without FXRoute ownership"
    fi
    return 0
  fi

  if [[ ! -e "$script_path" && ! -L "$script_path" \
    && ! -e "$service_path" && ! -L "$service_path" \
    && ! -e "$timer_path" && ! -L "$timer_path" ]]; then
    if command -v nft >/dev/null 2>&1 && ! remove_mdns_guard_table_direct; then
      warn "Could not verify or remove the FXRoute mDNS guard table"
      MDNS_GUARD_ENABLED=1
      return 0
    fi
    MDNS_GUARD_ENABLED=0
    MDNS_GUARD_OWNED_BY_FXROUTE=0
    return 0
  fi
  "${SUDO_CMD[@]}" systemctl disable --now fxroute-mdns-guard.timer fxroute-mdns-guard.service >/dev/null 2>&1 || true
  if [[ -x "$script_path" ]]; then
    "${SUDO_CMD[@]}" "$script_path" remove >/dev/null 2>&1 || true
  fi
  if ! remove_mdns_guard_table_direct; then
    warn "Could not remove the FXRoute mDNS guard rules"
    MDNS_GUARD_ENABLED=1
    return 0
  fi
  if ! "${SUDO_CMD[@]}" rm -f "$script_path" "$service_path" "$timer_path"; then
    warn "Could not remove the FXRoute mDNS guard files"
    return 0
  fi
  "${SUDO_CMD[@]}" systemctl daemon-reload >/dev/null 2>&1 || true
  MDNS_GUARD_ENABLED=0
  MDNS_GUARD_OWNED_BY_FXROUTE=0
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
  if [[ $guard_artifact_present -eq 1 ]]; then
    if [[ $MDNS_GUARD_ENABLED -ne 1 \
      || -L "$script_path" || -L "$service_path" || -L "$timer_path" \
      || ! -f "$script_path" ]]; then
      guard_artifact_owned=0
    elif ! grep -Fq 'TABLE="fxroute_mdnsguard"' "$script_path" \
      || ! grep -Fq 'ExecStart=/usr/local/sbin/fxroute-mdns-guard.sh apply' "$service_path" \
      || ! grep -Fq 'Unit=fxroute-mdns-guard.service' "$timer_path"; then
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
  cat > "$tmp_service" <<'EOF'
[Unit]
Description=FXRoute mDNS guard for Spotify Desktop/Avahi coexistence
After=firewalld.service network-online.target
Wants=firewalld.service network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fxroute-mdns-guard.sh apply
ExecReload=/usr/local/sbin/fxroute-mdns-guard.sh apply
EOF

  cat > "$tmp_timer" <<'EOF'
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

  if ! "${SUDO_CMD[@]}" install -m 755 "$tmp_script" "$script_path"; then
    warn "Could not install FXRoute mDNS guard script"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_service" "$service_path"; then
    warn "Could not install FXRoute mDNS guard service"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_timer" "$timer_path"; then
    warn "Could not install FXRoute mDNS guard timer"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" systemctl daemon-reload; then
    warn "Could not reload systemd after installing FXRoute mDNS guard"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" "$script_path" apply; then
    warn "Could not apply the FXRoute mDNS guard rules"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" systemctl enable fxroute-mdns-guard.timer || ! "${SUDO_CMD[@]}" systemctl restart fxroute-mdns-guard.timer; then
    warn "Could not enable the FXRoute mDNS guard timer"
    return 0
  fi

  MDNS_GUARD_ENABLED=1
  MDNS_GUARD_OWNED_BY_FXROUTE=1
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

  install_user="$FXROUTE_TARGET_USER"
  [[ "$install_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || {
    warn "FXRoute polkit power rule skipped because the install user is not a plain Unix name"
    return 0
  }

  [[ -f "$template_src" ]] || {
    warn "FXRoute polkit power rule skipped because $template_src is missing"
    return 0
  }

  rendered_rule="$(sed -e "s/INSTALL_USER_PLACEHOLDER/${install_user}/g" "$template_src")"
  [[ "$rendered_rule" != *"${install_user}"* ]] && {
    warn "FXRoute polkit power rule template did not accept the install user; skipping"
    return 0
  }

  tmp_rule="$(mktemp)"
  printf '%s\n' "$rendered_rule" > "$tmp_rule"

  POWER_POLKIT_RULE_PATH="$rule_path"

  if [[ -f "$rule_path" ]]; then
    POWER_POLKIT_RULE_PRE_EXISTED=1
    backup_path="$FXROUTE_BACKUP_DIR/${rule_name}.pre-fxroute"
    if [[ ! -e "$backup_path" && ! -L "$backup_path" ]]; then
      mkdir -p "$FXROUTE_BACKUP_DIR"
      if "${SUDO_CMD[@]}" cp -a "$rule_path" "$backup_path" 2>/dev/null; then
        :
      else
        warn "Could not back up $rule_path before installing the FXRoute polkit power rule"
      fi
    fi
  fi

  if "${SUDO_CMD[@]}" install -d /etc/polkit-1/rules.d; then
    if "${SUDO_CMD[@]}" install -m 644 "$tmp_rule" "$rule_path"; then
      POWER_POLKIT_INSTALLED=1
      pass "polkit power rule installed (closed: suspend + power-off only)"
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
  local caddy_root_cert="${caddy_data_dir}/caddy/pki/authorities/local/root.crt"
  local fxroute_caddy_active=0

  [[ -t 0 && -t 1 ]] || return 0

  [[ -f "$env_file" ]] && port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  lan_ip="$(primary_lan_ip)"
  [[ -n "$lan_ip" ]] || {
    warn "Optional Caddy setup skipped because no LAN IP could be detected"
    return 0
  }

  echo
  if systemctl is-active "$service_name" >/dev/null 2>&1; then
    fxroute_caddy_active=1
    echo "Optional Caddy HTTPS already active; refreshing FXRoute Caddy config: https://${lan_ip}"
    if [[ -n "$MDNS_HOSTNAME" ]]; then
      echo "Optional .local HTTPS also active: https://${MDNS_HOSTNAME}.local"
    fi
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

  if systemctl is-active caddy.service >/dev/null 2>&1; then
    log "system Caddy service detected on port 80, switching to the FXRoute-owned proxy service"
    if ! "${SUDO_CMD[@]}" systemctl disable --now caddy.service; then
      warn "Optional Caddy setup could not disable the default caddy.service"
      return 0
    fi
    if [[ $CADDY_SERVICE_WAS_ACTIVE_BEFORE -eq 1 ]]; then
      DEFAULT_CADDY_DISABLED_BY_FXROUTE=1
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

  if ! "${SUDO_CMD[@]}" systemctl daemon-reload; then
    warn "Optional Caddy setup failed during systemd daemon-reload"
    return 0
  fi
  if ! "${SUDO_CMD[@]}" systemctl enable "${service_name}.service" || ! "${SUDO_CMD[@]}" systemctl restart "${service_name}.service"; then
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
      CADDY_CERT_PATH="${caddy_cert_dir}/fxroute-local-root.crt"
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

main() {
  require_cmd python3
  require_cmd systemctl
  choose_sudo
  confirm_supported_distro
  load_provider_ownership_state
  select_optional_providers
  capture_lan_comfort_baseline
  ensure_native_packages
  ensure_dbus_send_binary
  sync_project_tree
  write_install_config
  STATE_CHECKPOINT_ENABLED=1
  trap 'checkpoint_install_state_on_exit' EXIT
  install_network_library_helper
  create_env_if_missing
  setup_python_env
  build_native_dsp_engine
  enable_user_audio_services
  configure_pipewire_samplerates_if_available
  configure_dsp_ingress_sink
  enable_user_session_persistence
  # Record the selected providers and baseline before provider side effects.
  write_install_state
  configure_optional_streaming
  write_service_unit
  # Refresh ownership after provider setup and before late validation can abort the run.
  write_install_state
  setup_spotify_autostart
  chmod +x "$INSTALL_ROOT/scripts/update_fxroute.sh"
  install_helpers
  configure_optional_maintenance_helpers
  configure_system_power_polkit_rule
  validate_http
  validate_tools
  offer_optional_local_lan_name
  install_mdns_guard
  offer_optional_caddy_proxy
  print_summary
  write_install_config
  write_install_state
}

main "$@"
