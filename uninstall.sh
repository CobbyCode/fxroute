#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -euo pipefail

APP_NAME="FXRoute"
SERVICE_NAME="fxroute"
PROJECT_DIRNAME="fxroute"
INSTALL_ROOT_DEFAULT="$HOME/$PROJECT_DIRNAME"
INSTALL_ROOT="$INSTALL_ROOT_DEFAULT"
REMOVE_PROJECT_DIR=0
REMOVE_EASYEFFECTS_BOOTSTRAP=0
ASSUME_YES=0
INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"

usage() {
  cat <<EOF
Usage: ./uninstall.sh [options]

Options:
  --target <dir>                Uninstall from this directory (default: $INSTALL_ROOT_DEFAULT)
  --remove-project-dir          Remove the project directory after uninstall
  --remove-easyeffects-bootstrap Remove Direct/Neutral bootstrap presets created for FXRoute (asks unless -y)
  -y, --yes                     Assume yes for optional removals
  -h, --help                    Show this help

Safe by default:
- removes FXRoute user service
- removes watchdog timer/service
- removes FXRoute helper scripts
- removes EasyEffects autostart entry created by installer
- removes the optional FXRoute Caddy reverse proxy service/config if present
- restores the previous default `caddy.service` when FXRoute had disabled it to take over port 80

Cautious by default:
- does NOT remove the project directory unless requested
- does NOT remove EasyEffects presets unless explicitly requested
- only offers to uninstall EasyEffects itself if FXRoute originally installed it
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
  [[ -n "$path" ]] && printf '%s\n' "$path"
}

firewall_offline_cmd_path() {
  local path=""
  path="$(command -v firewall-offline-cmd 2>/dev/null || true)"
  if [[ -z "$path" && -x /usr/bin/firewall-offline-cmd ]]; then
    path="/usr/bin/firewall-offline-cmd"
  fi
  [[ -n "$path" ]] && printf '%s\n' "$path"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || { echo "--target requires a directory" >&2; exit 1; }
      INSTALL_ROOT="$2"
      shift 2
      ;;
    --remove-project-dir)
      REMOVE_PROJECT_DIR=1
      shift
      ;;
    --remove-easyeffects-bootstrap)
      REMOVE_EASYEFFECTS_BOOTSTRAP=1
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

remove_file_if_exists() {
  local path="$1"
  if [[ -e "$path" || -L "$path" ]]; then
    rm -f "$path"
    log "Removed $path"
  fi
}

remove_service() {
  systemctl --user disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  remove_file_if_exists "$HOME/.config/systemd/user/$SERVICE_NAME.service"
}

remove_watchdog() {
  systemctl --user disable --now easyeffects-stale-watchdog.timer >/dev/null 2>&1 || true
  remove_file_if_exists "$HOME/.config/systemd/user/easyeffects-stale-watchdog.service"
  remove_file_if_exists "$HOME/.config/systemd/user/easyeffects-stale-watchdog.timer"
}

remove_helpers() {
  remove_file_if_exists "$HOME/.local/bin/fxroute-status"
  remove_file_if_exists "$HOME/.local/bin/fxroute-logs"
  remove_file_if_exists "$HOME/.local/bin/fxroute-restart"
  remove_file_if_exists "$HOME/.local/bin/fxroute-update"
  remove_file_if_exists "$HOME/.local/bin/fxroute-update-ytdlp"
}

remove_autostart() {
  remove_file_if_exists "$HOME/.config/autostart/easyeffects.desktop"
}

remove_optional_caddy_proxy() {
  local service_name="fxroute-caddy.service"
  local service_path="/etc/systemd/system/$service_name"
  local config_path="/etc/fxroute/Caddyfile"
  local sudo_cmd=()

  if [[ ! -e "$service_path" && ! -e "$config_path" ]]; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot remove optional FXRoute Caddy proxy because sudo is unavailable"
    return 0
  fi

  "${sudo_cmd[@]}" systemctl disable --now "$service_name" >/dev/null 2>&1 || true
  "${sudo_cmd[@]}" rm -f "$service_path" "$config_path"
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
    return 0
  fi

  if ! "${sudo_cmd[@]}" systemctl enable --now caddy.service >/dev/null 2>&1; then
    warn "Could not restore the previously active system caddy.service"
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
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot restore the previous hostname because sudo is unavailable"
    return 0
  fi

  if ! "${sudo_cmd[@]}" hostnamectl set-hostname "$hostname_before"; then
    warn "Failed to restore hostname to $hostname_before"
    return 0
  fi

  if systemctl is-active avahi-daemon >/dev/null 2>&1; then
    "${sudo_cmd[@]}" systemctl restart avahi-daemon >/dev/null 2>&1 || true
  fi

  log "Restored hostname to $hostname_before"
}

remove_firewalld_service_if_requested() {
  local service="$1"
  local purpose="$2"
  local opened_by_fxroute
  local sudo_cmd=()
  local firewall_cmd=""
  local firewall_offline_cmd=""

  opened_by_fxroute="$(read_install_state_field "lan_comfort.${service}_opened_by_fxroute" 2>/dev/null || true)"
  [[ "$opened_by_fxroute" == "true" ]] || return 0

  if ! confirm "FXRoute opened firewalld service '$service' for $purpose. Remove that firewall opening?"; then
    warn "Keeping firewalld service '$service'"
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo_cmd=(sudo)
  else
    warn "Cannot adjust firewalld cleanup because sudo is unavailable"
    return 0
  fi

  firewall_cmd="$(firewall_cmd_path)"
  firewall_offline_cmd="$(firewall_offline_cmd_path)"

  if [[ -n "$firewall_cmd" ]] && "${sudo_cmd[@]}" "$firewall_cmd" --state >/dev/null 2>&1; then
    if ! "${sudo_cmd[@]}" "$firewall_cmd" --permanent --remove-service="$service" >/dev/null 2>&1; then
      warn "Failed to remove firewalld service '$service'"
      return 0
    fi
    if ! "${sudo_cmd[@]}" "$firewall_cmd" --reload >/dev/null 2>&1; then
      warn "Removed firewalld service '$service' permanently, but reload failed"
      return 0
    fi
    log "Removed firewalld service '$service' opened for FXRoute"
    return 0
  fi

  if [[ -n "$firewall_offline_cmd" ]]; then
    if ! "${sudo_cmd[@]}" "$firewall_offline_cmd" --remove-service="$service" >/dev/null 2>&1; then
      warn "Failed to remove firewalld service '$service' from offline config"
      return 0
    fi
    log "Removed firewalld service '$service' opened for FXRoute (offline config)"
    return 0
  fi

  warn "Could not find firewalld tooling to remove service '$service'"
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
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    avahi_pkg="avahi-daemon"
  elif command -v dnf >/dev/null 2>&1 || command -v zypper >/dev/null 2>&1; then
    avahi_pkg="avahi"
  fi

  if [[ "$installed_by_fxroute" == "true" && -n "$avahi_pkg" ]]; then
    if ! confirm "FXRoute installed Avahi for .local access. Remove Avahi too?"; then
      warn "Keeping Avahi installed"
      return 0
    fi

    "${sudo_cmd[@]}" systemctl disable --now avahi-daemon >/dev/null 2>&1 || true
    case "$avahi_pkg" in
      avahi-daemon)
        if ! "${sudo_cmd[@]}" apt-get remove -y avahi-daemon >/dev/null 2>&1; then
          warn "Failed to remove Avahi with apt"
          return 0
        fi
        ;;
      avahi)
        if command -v dnf >/dev/null 2>&1; then
          if ! "${sudo_cmd[@]}" dnf remove -y avahi >/dev/null 2>&1; then
            warn "Failed to remove Avahi with dnf"
            return 0
          fi
        elif command -v zypper >/dev/null 2>&1; then
          if ! "${sudo_cmd[@]}" zypper --non-interactive remove avahi >/dev/null 2>&1; then
            warn "Failed to remove Avahi with zypper"
            return 0
          fi
        fi
        ;;
    esac
    log "Removed Avahi installed for FXRoute LAN comfort"
    return 0
  fi

  if [[ "$enabled_by_fxroute" == "true" && "$was_active_before" != "true" && "$was_enabled_before" != "true" ]]; then
    if ! confirm "FXRoute enabled Avahi for .local access. Disable Avahi again?"; then
      warn "Keeping Avahi enabled"
      return 0
    fi

    "${sudo_cmd[@]}" systemctl disable --now avahi-daemon >/dev/null 2>&1 || warn "Failed to disable Avahi"
    log "Disabled Avahi enabled for FXRoute LAN comfort"
  fi
}

remove_bootstrap_presets_if_requested() {
  [[ $REMOVE_EASYEFFECTS_BOOTSTRAP -eq 1 ]] || return 0
  local output_dir="$HOME/.var/app/com.github.wwmm.easyeffects/data/easyeffects/output"
  local direct="$output_dir/Direct.json"
  local neutral="$output_dir/Neutral.json"

  if ! confirm "Remove EasyEffects bootstrap presets Direct.json and Neutral.json?"; then
    warn "Skipping EasyEffects preset removal"
    return 0
  fi

  remove_file_if_exists "$direct"
  remove_file_if_exists "$neutral"
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

remove_easyeffects_if_requested() {
  local installed_by_fxroute
  installed_by_fxroute="$(read_install_state_field "easyeffects.installed_by_fxroute" 2>/dev/null || true)"
  [[ "$installed_by_fxroute" == "true" ]] || return 0

  local install_method
  install_method="$(read_install_state_field "easyeffects.install_method" 2>/dev/null || true)"
  [[ -n "$install_method" ]] || install_method="flatpak"

  if ! confirm "FXRoute installed EasyEffects via $install_method. Remove EasyEffects too?"; then
    warn "Keeping EasyEffects installed"
    return 0
  fi

  case "$install_method" in
    flatpak)
      if flatpak info --user com.github.wwmm.easyeffects >/dev/null 2>&1; then
        flatpak uninstall --user -y com.github.wwmm.easyeffects >/dev/null 2>&1 || {
          warn "Failed to uninstall EasyEffects Flatpak"
          return 0
        }
        log "Removed EasyEffects Flatpak"
      else
        warn "EasyEffects Flatpak not found anymore"
      fi
      ;;
    native)
      if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
        if command -v apt-get >/dev/null 2>&1; then
          sudo apt-get remove -y easyeffects >/dev/null 2>&1 || warn "Failed to uninstall native EasyEffects with apt"
        elif command -v dnf >/dev/null 2>&1; then
          sudo dnf remove -y easyeffects >/dev/null 2>&1 || warn "Failed to uninstall native EasyEffects with dnf"
        elif command -v zypper >/dev/null 2>&1; then
          sudo zypper --non-interactive remove easyeffects >/dev/null 2>&1 || warn "Failed to uninstall native EasyEffects with zypper"
        else
          warn "No supported package manager found to remove native EasyEffects"
        fi
      else
        warn "Could not auto-remove native EasyEffects because passwordless sudo is unavailable"
      fi
      ;;
    *)
      warn "Unknown EasyEffects install method '$install_method', leaving package installed"
      ;;
  esac
}

remove_project_dir_if_requested() {
  [[ $REMOVE_PROJECT_DIR -eq 1 ]] || return 0
  if ! confirm "Remove project directory $INSTALL_ROOT?"; then
    warn "Skipping project directory removal"
    return 0
  fi
  rm -rf "$INSTALL_ROOT"
  log "Removed $INSTALL_ROOT"
}

main() {
  log "Stopping and removing FXRoute user service"
  remove_service

  log "Removing EasyEffects watchdog units"
  remove_watchdog

  log "Removing helper scripts"
  remove_helpers

  log "Removing EasyEffects autostart entry"
  remove_autostart

  log "Removing optional FXRoute Caddy reverse proxy"
  remove_optional_caddy_proxy

  log "Restoring previously active system caddy.service if needed"
  restore_default_caddy_service_if_needed

  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl --user reset-failed >/dev/null 2>&1 || true

  restore_hostname_if_requested
  remove_avahi_if_requested
  remove_firewalld_service_if_requested mdns ".local LAN access"
  remove_firewalld_service_if_requested http "port-80 LAN access"
  remove_bootstrap_presets_if_requested
  remove_easyeffects_if_requested
  remove_project_dir_if_requested
  remove_file_if_exists "$INSTALL_STATE_FILE"

  log "Uninstall complete"
}

main "$@"
