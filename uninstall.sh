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
REMOVE_PROJECT_DIR=0
ASSUME_YES=0
INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
INSTALL_CONFIG_FILE="$HOME/.config/fxroute/install-config.env"
FXROUTE_BACKUP_DIR="$HOME/.config/fxroute/backups"
SPOTIFY_APT_SOURCE_FILE="/etc/apt/sources.list.d/spotify.list"
SPOTIFY_APT_KEY_FILE="/usr/share/keyrings/spotify-archive-keyring.gpg"
SPOTIFYD_ZEROCONF_PORT="4444"
PRESERVE_INSTALL_STATE=0
PROVIDER_LAN_CLEANUP_DEFERRED=0

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

path_is_within() {
  local child="$1"
  local parent="$2"
  [[ "$child" == "$parent" || "$child" == "$parent"/* ]]
}

load_recorded_install_root() {
  local configured_root=""

  [[ -f "$INSTALL_CONFIG_FILE" ]] || return 0
  configured_root="$(sed -n 's/^FXROUTE_INSTALL_ROOT=//p' "$INSTALL_CONFIG_FILE" | tail -n 1)"
  [[ -n "$configured_root" ]] || return 0
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
INSTALL_ROOT="$(canonical_path "$INSTALL_ROOT")"

validate_install_root_for_removal() {
  local root="$INSTALL_ROOT"
  local protected_path=""

  [[ "$root" != "/" && "$root" != "$HOME" ]] || {
    warn "Refusing to remove / or the home directory as an FXRoute project target"
    return 1
  }
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
  if [[ -e "$path" || -L "$path" ]]; then
    if rm -f "$path"; then
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
  if [[ ! -f "$path" || -z "$expected_sha256" ]]; then
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

remove_service() {
  systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  remove_file_if_exists "$HOME/.config/systemd/user/$SERVICE_NAME.service"
}

remove_dsp_ingress_sink() {
  local config_file="$HOME/.config/pipewire/pipewire-pulse.conf.d/50-fxroute-dsp-sink.conf"
  remove_file_if_exists "$config_file"
  rmdir "$(dirname "$config_file")" >/dev/null 2>&1 || true
  systemctl --user restart pipewire-pulse.service >/dev/null 2>&1 || true
}

remove_spotify_cleanup_helper() {
  systemctl --user disable --now fxroute-spotify-cache-cleanup.timer >/dev/null 2>&1 || true
  remove_file_if_exists "$HOME/.config/systemd/user/fxroute-spotify-cache-cleanup.service"
  remove_file_if_exists "$HOME/.config/systemd/user/fxroute-spotify-cache-cleanup.timer"
}

remove_optional_system_update_helper() {
  local service_path="/etc/systemd/system/fxroute-system-update.service"
  local timer_path="/etc/systemd/system/fxroute-system-update.timer"
  local sudo_cmd=()

  if [[ ! -e "$service_path" && ! -e "$timer_path" ]]; then
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

  "${sudo_cmd[@]}" systemctl disable --now fxroute-system-update.timer >/dev/null 2>&1 || true
  if ! "${sudo_cmd[@]}" rm -f "$service_path" "$timer_path"; then
    warn "Could not remove optional FXRoute system update helper files"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  "${sudo_cmd[@]}" systemctl daemon-reload >/dev/null 2>&1 || true
  log "Removed optional FXRoute system update helper"
}

remove_helpers() {
  remove_file_if_exists "$HOME/.local/bin/fxroute-status"
  remove_file_if_exists "$HOME/.local/bin/fxroute-logs"
  remove_file_if_exists "$HOME/.local/bin/fxroute-restart"
  remove_file_if_exists "$HOME/.local/bin/fxroute-update"
  remove_file_if_exists "$HOME/.local/bin/fxroute-update-ytdlp"
}

remove_network_library_helper() {
  if command -v sudo >/dev/null 2>&1; then
    sudo /usr/local/sbin/fxroute-cifs-mount --remove-all 2>/dev/null || true
    if ! sudo rm -f /usr/local/sbin/fxroute-cifs-mount /etc/sudoers.d/fxroute-cifs-mount; then
      warn "Could not remove the network library mount helper"
      PRESERVE_INSTALL_STATE=1
    fi
  elif [[ $EUID -eq 0 ]]; then
    SUDO_USER="${SUDO_USER:-$(logname 2>/dev/null || true)}" /usr/local/sbin/fxroute-cifs-mount --remove-all 2>/dev/null || true
    if ! rm -f /usr/local/sbin/fxroute-cifs-mount /etc/sudoers.d/fxroute-cifs-mount; then
      warn "Could not remove the network library mount helper"
      PRESERVE_INSTALL_STATE=1
    fi
  else
    warn "Could not remove the network library mount helper because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
  fi
}

remove_autostart() {
  remove_file_if_exists "$HOME/.config/autostart/fxroute-spotify.desktop"
}

provider_sudo_available() {
  [[ ${EUID:-$(id -u)} -eq 0 ]] || command -v sudo >/dev/null 2>&1
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
    local repo_paths=("$SPOTIFY_APT_SOURCE_FILE")
    [[ "$apt_key_installed_by_fxroute" == "true" ]] && repo_paths+=("$SPOTIFY_APT_KEY_FILE")
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

  if [[ "$service_installed_by_fxroute" == "true" \
    && ! -e "$service_path" && ! -L "$service_path" ]]; then
    warn "Cannot remove FXRoute-owned spotifyd service because its recorded unit is missing"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ "$installed_by_fxroute" == "true" ]] && ! verify_owned_binary_identity "$binary_path" "$(read_install_state_field "providers.spotifyd.binary_sha256" 2>/dev/null || true)" "FXRoute-owned spotifyd"; then
    return 0
  fi
  if [[ "$service_installed_by_fxroute" == "true" ]] && ! verify_owned_binary_identity "$service_path" "$(read_install_state_field "providers.spotifyd.service_sha256" 2>/dev/null || true)" "FXRoute-owned spotifyd service"; then
    return 0
  fi
  if [[ "$service_installed_by_fxroute" == "true" ]]; then
    if [[ "$service_path" == "$HOME/.config/systemd/user/spotifyd.service" ]]; then
      systemctl --user disable --now spotifyd.service >/dev/null 2>&1 || true
      remove_file_if_exists "$service_path"
    else
      warn "Refusing to remove an unexpected spotifyd service path: $service_path"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
  if [[ "$installed_by_fxroute" == "true" ]]; then
    if [[ "$binary_path" == "$HOME/.local/bin/spotifyd" ]]; then
      remove_file_if_exists "$binary_path"
    else
      warn "Refusing to remove an unexpected spotifyd binary path: $binary_path"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
  systemctl --user daemon-reload >/dev/null 2>&1 || true
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

  if [[ "$service_installed_by_fxroute" == "true" \
    && ! -e "$service_path" && ! -L "$service_path" ]]; then
    warn "Cannot remove FXRoute-owned qbzd service because its recorded unit is missing"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  if [[ "$installed_by_fxroute" == "true" ]] && ! verify_owned_binary_identity "$binary_path" "$(read_install_state_field "providers.qobuz.binary_sha256" 2>/dev/null || true)" "FXRoute-owned qbzd"; then
    return 0
  fi
  if [[ "$service_installed_by_fxroute" == "true" ]] && ! verify_owned_binary_identity "$service_path" "$(read_install_state_field "providers.qobuz.service_sha256" 2>/dev/null || true)" "FXRoute-owned qbzd service"; then
    return 0
  fi
  if [[ "$service_installed_by_fxroute" == "true" ]]; then
    if [[ "$service_path" == "$HOME/.config/systemd/user/qbzd.service" ]]; then
      systemctl --user disable --now qbzd.service >/dev/null 2>&1 || true
      remove_file_if_exists "$service_path"
    else
      warn "Refusing to remove an unexpected qbzd service path: $service_path"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
  if [[ "$installed_by_fxroute" == "true" ]]; then
    if [[ "$binary_path" == "$HOME/.local/bin/qbzd" ]]; then
      remove_file_if_exists "$binary_path"
    else
      warn "Refusing to remove an unexpected qbzd binary path: $binary_path"
      PRESERVE_INSTALL_STATE=1
    fi
  fi
  systemctl --user daemon-reload >/dev/null 2>&1 || true
}

read_qbzd_volume_mode_for_uninstall() {
  local binary_path="$1"
  [[ -x "$binary_path" ]] || return 1
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
  if ! "$binary_path" settings set --quiet qconnect.volume_mode "$mode_before" >/dev/null 2>&1; then
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
  "$python_path" - <<'PY'
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

  if "$pip_path" uninstall -y tidalapi; then
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
  "${sudo_cmd[@]}" "$nft_path" delete table inet fxroute_mdnsguard >/dev/null 2>&1
}

remove_optional_mdns_guard() {
  local sudo_cmd=()
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local marker_present=0
  local guard_owned=""

  if [[ -e "$service_path" || -L "$service_path" \
    || -e "$timer_path" || -L "$timer_path" \
    || -e "$script_path" || -L "$script_path" ]]; then
    marker_present=1
  elif ! command -v nft >/dev/null 2>&1; then
    return 0
  fi

  guard_owned="$(read_install_state_field "lan_comfort.mdns_guard_owned_by_fxroute" 2>/dev/null || true)"
  if [[ -z "$guard_owned" ]]; then
    guard_owned="$(read_install_state_field "lan_comfort.mdns_guard_enabled" 2>/dev/null || true)"
  fi
  if [[ "$guard_owned" != "true" ]]; then
    log "Preserving mDNS guard artifacts without FXRoute ownership"
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

  if [[ $marker_present -eq 0 ]]; then
    if ! remove_mdns_guard_table_direct "${sudo_cmd[@]}"; then
      warn "Could not verify or remove the FXRoute mDNS guard table"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    return 0
  fi

  "${sudo_cmd[@]}" systemctl disable --now fxroute-mdns-guard.timer fxroute-mdns-guard.service >/dev/null 2>&1 || true
  if [[ -x "$script_path" ]]; then
    "${sudo_cmd[@]}" "$script_path" remove >/dev/null 2>&1 || true
  fi
  if ! remove_mdns_guard_table_direct "${sudo_cmd[@]}"; then
    warn "Could not remove the FXRoute mDNS guard rules"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! "${sudo_cmd[@]}" rm -f "$service_path" "$timer_path" "$script_path"; then
    warn "Could not remove the FXRoute mDNS guard files"
    PRESERVE_INSTALL_STATE=1
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

ufw_legacy_rule_has_fxroute_comment() {
  local rule_id="$1"
  local added=""
  local sudo_cmd=()

  firewall_cleanup_sudo || return 1
  [[ ${EUID:-$(id -u)} -eq 0 ]] || sudo_cmd=(sudo)
  command -v ufw >/dev/null 2>&1 || return 1
  added="$("${sudo_cmd[@]}" ufw show added 2>/dev/null || true)"
  case "$rule_id" in
    http_80_tcp) grep -Eqi '80/tcp.*(FXRoute|port-80)' <<<"$added" ;;
    https_443_tcp) grep -Eqi '443/tcp.*(FXRoute|port-443)' <<<"$added" ;;
    mdns_5353_udp) grep -Eqi '5353/udp.*(FXRoute|Qobuz Connect|\.local LAN|spotifyd)' <<<"$added" ;;
    fxroute_http_8000_tcp) grep -Eqi '8000/tcp.*FXRoute' <<<"$added" ;;
    spotifyd_zeroconf_4444_tcp) grep -Eqi '4444/tcp.*spotifyd' <<<"$added" ;;
    *) return 1 ;;
  esac
}

ufw_rule_is_owned() {
  local rule_id="$1"
  local nested=""
  local legacy_field=""

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
  ufw_legacy_rule_has_fxroute_comment "$rule_id"
}

remove_owned_firewalld_rule() {
  local rule_id="$1"
  local purpose="$2"
  local port=""
  local sudo_cmd=()
  local firewall_cmd=""
  local firewall_offline_cmd=""
  local runtime_present=0
  local permanent_present=0
  local legacy_owned=0
  local service=""
  local runtime_service_present=0
  local permanent_service_present=0

  firewalld_rule_is_owned "$rule_id" || return 0
  port="$(firewall_rule_port "$rule_id")" || return 0
  if firewall_legacy_state_present \
    && [[ "$(read_install_state_field "lan_comfort.firewalld_owned_rules.${rule_id}" 2>/dev/null || true)" == "false" || -z "$(read_install_state_field "lan_comfort.firewalld_owned_rules.${rule_id}" 2>/dev/null || true)" ]] \
    && firewalld_legacy_rule_is_owned "$rule_id"; then
    legacy_owned=1
    service="$(firewalld_rule_service "$rule_id")"
  fi

  if [[ $legacy_owned -eq 1 ]]; then
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

  if [[ $legacy_owned -eq 1 ]]; then
    if [[ -n "$firewall_cmd" ]] && "${sudo_cmd[@]}" "$firewall_cmd" --state >/dev/null 2>&1; then
      if "${sudo_cmd[@]}" "$firewall_cmd" --query-service="$service" >/dev/null 2>&1; then
        runtime_service_present=1
      fi
      if "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-service="$service" >/dev/null 2>&1; then
        permanent_service_present=1
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
      if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-service="$service" >/dev/null 2>&1; then
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
    if "${sudo_cmd[@]}" "$firewall_cmd" --query-port="$port" >/dev/null 2>&1; then
      runtime_present=1
    fi
    if "${sudo_cmd[@]}" "$firewall_cmd" --permanent --query-port="$port" >/dev/null 2>&1; then
      permanent_present=1
    fi
    if [[ $runtime_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --remove-port="$port" >/dev/null 2>&1; then
        warn "Failed to remove runtime firewalld port '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $permanent_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --permanent --remove-port="$port" >/dev/null 2>&1; then
        warn "Failed to remove permanent firewalld port '$port'"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
    fi
    if [[ $runtime_present -eq 1 || $permanent_present -eq 1 ]]; then
      if ! "${sudo_cmd[@]}" "$firewall_cmd" --reload >/dev/null 2>&1; then
        warn "Removed firewalld port '$port', but reload failed"
        PRESERVE_INSTALL_STATE=1
        return 0
      fi
      log "Removed FXRoute-owned firewalld port '$port'"
    else
      log "FXRoute-owned firewalld port '$port' is already absent"
    fi
    return 0
  fi

  if [[ -n "$firewall_offline_cmd" ]]; then
    if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --query-port="$port" >/dev/null 2>&1; then
      log "FXRoute-owned firewalld port '$port' is already absent from offline config"
      return 0
    fi
    if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --remove-port="$port" >/dev/null 2>&1; then
      warn "Failed to remove firewalld port '$port' from offline config"
      PRESERVE_INSTALL_STATE=1
      return 0
    fi
    log "Removed FXRoute-owned firewalld port '$port' from offline config"
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
  local status=""

  ufw_rule_is_owned "$rule_id" || return 0
  port="$(firewall_rule_port "$rule_id")" || return 0

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

  if "${sudo_cmd[@]}" ufw --force delete allow "$port" >/dev/null 2>&1; then
    log "Removed FXRoute-owned UFW port '$port'"
    return 0
  fi
  if ! status="$("${sudo_cmd[@]}" ufw show added 2>/dev/null)"; then
    warn "Could not verify the persistent UFW rule '$port' after deletion failed"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! grep -Fq "ufw allow $port" <<<"$status"; then
    log "FXRoute-owned UFW port '$port' is already absent"
    return 0
  fi
  warn "Failed to remove UFW port '$port'"
  PRESERVE_INSTALL_STATE=1
}

remove_system_power_polkit_rule() {
  local rule_installed
  local rule_path
  local backup_path
  local sudo_cmd=()
  local rule_name="50-fxroute-power.rules"
  local rule_default="/etc/polkit-1/rules.d/$rule_name"
  local backup_default="$FXROUTE_BACKUP_DIR/${rule_name}.pre-fxroute"

  rule_installed="$(read_install_state_field "lan_comfort.power_polkit_installed" 2>/dev/null || true)"
  rule_path="$(read_install_state_field "lan_comfort.power_polkit_rule_path" 2>/dev/null || true)"
  [[ -z "$rule_path" ]] && rule_path="$rule_default"
  if [[ "$rule_installed" != "true" && ! -e "$rule_path" ]]; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
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

  if [[ -e "$backup_default" ]]; then
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
  local sudo_cmd=()

  if [[ ! -e "$service_path" && ! -e "$config_path" && ! -e "$cert_path" && ! -e "$caddy_data_dir" ]]; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot remove optional FXRoute Caddy proxy because sudo is unavailable"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi

  "${sudo_cmd[@]}" systemctl disable --now "$service_name" >/dev/null 2>&1 || true
  if ! "${sudo_cmd[@]}" rm -f "$service_path" "$config_path" "$cert_path"; then
    warn "Could not remove the FXRoute Caddy service or configuration"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  if ! "${sudo_cmd[@]}" rm -rf "$caddy_data_dir"; then
    warn "Could not remove the FXRoute Caddy data"
    PRESERVE_INSTALL_STATE=1
    return 0
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
  if ! rm -rf -- "$INSTALL_ROOT"; then
    warn "Failed to remove project directory $INSTALL_ROOT"
    PRESERVE_INSTALL_STATE=1
    return 0
  fi
  log "Removed $INSTALL_ROOT"
}

remove_install_records() {
  if [[ $PRESERVE_INSTALL_STATE -eq 0 ]]; then
    remove_file_if_exists "$INSTALL_STATE_FILE"
    remove_file_if_exists "$INSTALL_CONFIG_FILE"
  else
    log "Keeping FXRoute install state for a later provider cleanup retry"
  fi
  rmdir "$FXROUTE_BACKUP_DIR" >/dev/null 2>&1 || true
  rmdir "$(dirname "$FXROUTE_BACKUP_DIR")" >/dev/null 2>&1 || true
}

main() {
  local firewall_rule=""
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

  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl --user reset-failed >/dev/null 2>&1 || true

  if [[ $PROVIDER_LAN_CLEANUP_DEFERRED -eq 0 ]]; then
    restore_hostname_if_requested
    restore_avahi_config_if_requested
    remove_avahi_if_requested
    for firewall_rule in \
      http_80_tcp \
      https_443_tcp \
      mdns_5353_udp \
      fxroute_http_8000_tcp \
      spotifyd_zeroconf_4444_tcp; do
      remove_owned_firewalld_rule "$firewall_rule" "FXRoute LAN/provider access"
      remove_owned_ufw_rule "$firewall_rule" "FXRoute LAN/provider access"
    done
  fi
  remove_project_dir_if_requested
  remove_install_records

  log "Uninstall complete"
}

main "$@"
