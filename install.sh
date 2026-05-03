#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

APP_NAME="FXRoute"
SERVICE_NAME="fxroute"
PROJECT_DIRNAME="fxroute"
DEFAULT_INSTALL_ROOT="$HOME/$PROJECT_DIRNAME"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$SCRIPT_DIR"
INSTALL_ROOT="$DEFAULT_INSTALL_ROOT"
LOCAL_PROJECT_MODE=0
ASSUME_YES=0

VALIDATION_RESULTS=()
WARNINGS=()
EASYEFFECTS_MODE="missing"
EASYEFFECTS_SOCKET=""
PACKAGE_MANAGER=""
PACKAGE_INSTALL_CMD=()
SUDO_CMD=()
INSTALL_STATE_FILE="$HOME/.config/fxroute/install-state.json"
FXROUTE_BACKUP_DIR="$HOME/.config/fxroute/backups"
EASYEFFECTS_INSTALLED_BY_FXROUTE=0
EASYEFFECTS_INSTALL_METHOD=""
EASYEFFECTS_AUTOSTART_BACKED_UP=0
EASYEFFECTS_WATCHDOG_SERVICE_BACKED_UP=0
EASYEFFECTS_WATCHDOG_TIMER_BACKED_UP=0
MDNS_HOSTNAME=""
LAN_HOSTNAME_BEFORE=""
LAN_HOSTNAME_AFTER=""
LAN_HOSTNAME_CHANGED_BY_FXROUTE=0
AVAHI_WAS_PRESENT_BEFORE=0
AVAHI_WAS_ACTIVE_BEFORE=0
AVAHI_WAS_ENABLED_BEFORE=0
AVAHI_INSTALLED_BY_FXROUTE=0
AVAHI_ENABLED_BY_FXROUTE=0
CADDY_WAS_PRESENT_BEFORE=0
CADDY_SERVICE_WAS_ACTIVE_BEFORE=0
CADDY_INSTALLED_BY_FXROUTE=0
DEFAULT_CADDY_DISABLED_BY_FXROUTE=0
CADDY_PROXY_ENABLED=0
CADDY_CERT_PATH=""
MDNS_GUARD_ENABLED=0
FIREWALLD_WAS_ACTIVE_BEFORE=0
HTTP_WAS_ALLOWED_BEFORE=0
HTTPS_WAS_ALLOWED_BEFORE=0
MDNS_WAS_ALLOWED_BEFORE=0
HTTP_OPENED_BY_FXROUTE=0
HTTPS_OPENED_BY_FXROUTE=0
MDNS_OPENED_BY_FXROUTE=0

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  --target <dir>        Install or refresh into this directory (default: $DEFAULT_INSTALL_ROOT)
  --local-project       Install in-place from the current project directory
  --source <dir>        Use a different local project source directory
  -y, --yes             Assume yes for package / Flatpak install prompts
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

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command missing: $1"
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
  hostname -I 2>/dev/null | awk '{print $1}'
}

avahi_is_present() {
  case "$PACKAGE_MANAGER" in
    apt)
      dpkg-query -W -f='${Status}' avahi-daemon 2>/dev/null | grep -q "install ok installed"
      ;;
    dnf|zypper)
      rpm -q avahi >/dev/null 2>&1
      ;;
    *)
      command -v avahi-daemon >/dev/null 2>&1 \
        || [[ -x /usr/sbin/avahi-daemon ]] \
        || [[ -x /usr/bin/avahi-daemon ]] \
        || systemctl list-unit-files avahi-daemon.service --no-legend 2>/dev/null | grep -q '^avahi-daemon\.service'
      ;;
  esac
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

firewalld_query_service() {
  local service="$1"
  local firewall_cmd=""
  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 1
  firewalld_is_active || return 1
  "${SUDO_CMD[@]}" "$firewall_cmd" --query-service="$service" >/dev/null 2>&1
}

ensure_firewalld_service_open() {
  local service="$1"
  local purpose="$2"
  local firewall_cmd=""

  firewall_cmd="$(firewall_cmd_path || true)"
  [[ -n "$firewall_cmd" ]] || return 0
  firewalld_is_active || return 0

  if "${SUDO_CMD[@]}" "$firewall_cmd" --query-service="$service" >/dev/null 2>&1; then
    return 0
  fi

  log "$firewall_cmd --permanent --add-service=$service"
  if ! "${SUDO_CMD[@]}" "$firewall_cmd" --permanent --add-service="$service"; then
    warn "Optional LAN comfort could not open firewalld service '$service' for $purpose"
    return 0
  fi

  log "$firewall_cmd --reload"
  if ! "${SUDO_CMD[@]}" "$firewall_cmd" --reload; then
    warn "Optional LAN comfort opened firewalld service '$service' permanently, but reload failed"
    return 0
  fi

  case "$service" in
    http) HTTP_OPENED_BY_FXROUTE=1 ;;
    https) HTTPS_OPENED_BY_FXROUTE=1 ;;
    mdns) MDNS_OPENED_BY_FXROUTE=1 ;;
  esac

  pass "firewalld service opened ($service)"
}

ufw_is_active() {
  command -v ufw >/dev/null 2>&1 || return 1
  "${SUDO_CMD[@]}" ufw status 2>/dev/null | grep -qi '^Status: active\|^Status: Aktiv'
}

ensure_ufw_port_open() {
  local service="$1"
  local purpose="$2"
  local port=""
  local rule=""

  ufw_is_active || return 0

  case "$service" in
    http) port="80/tcp" ;;
    https) port="443/tcp" ;;
    mdns) port="5353/udp" ;;
    fxroute-http) port="8000/tcp" ;;
    *) return 0 ;;
  esac

  if "${SUDO_CMD[@]}" ufw status 2>/dev/null | grep -Eq "^${port}[[:space:]]+ALLOW|^${port//\//\\/}[[:space:]]+ALLOW"; then
    return 0
  fi

  rule="$port"
  log "ufw allow $rule"
  if ! "${SUDO_CMD[@]}" ufw allow "$rule" comment "$purpose"; then
    warn "Optional LAN comfort could not open ufw rule '$rule' for $purpose"
    return 0
  fi

  case "$service" in
    http|fxroute-http) HTTP_OPENED_BY_FXROUTE=1 ;;
    https) HTTPS_OPENED_BY_FXROUTE=1 ;;
    mdns) MDNS_OPENED_BY_FXROUTE=1 ;;
  esac

  pass "ufw rule opened ($rule)"
}

ensure_lan_firewall_service_open() {
  local service="$1"
  local purpose="$2"

  ensure_firewalld_service_open "$service" "$purpose"
  ensure_ufw_port_open "$service" "$purpose"
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
    if firewalld_query_service http; then
      HTTP_WAS_ALLOWED_BEFORE=1
    fi
    if firewalld_query_service https; then
      HTTPS_WAS_ALLOWED_BEFORE=1
    fi
    if firewalld_query_service mdns; then
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
  else
    die "Unsupported distro. Expected apt, dnf, or zypper."
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
      run_cmd "${SUDO_CMD[@]}" apt-get update
      run_cmd "${SUDO_CMD[@]}" apt-get install -y "${packages[@]}"
      ;;
    dnf)
      run_cmd "${SUDO_CMD[@]}" dnf install -y "${packages[@]}"
      ;;
    zypper)
      run_cmd "${SUDO_CMD[@]}" zypper --non-interactive install --no-recommends "${packages[@]}"
      ;;
  esac
}

ensure_native_packages() {
  local core_packages=()
  local support_packages=(curl git socat)
  local audio_stack_packages=()
  local flatpak_runtime_packages=()
  local missing_packages=()
  local missing_support=()
  local missing_audio_stack=()
  local missing_flatpak_runtime=()
  local need_venv_pkg=0
  local need_bt_plugin_pkg=0

  case "$PACKAGE_MANAGER" in
    apt)
      core_packages=(python3 python3-pip python3-venv mpv ffmpeg playerctl flatpak)
      audio_stack_packages=(bluez wireplumber pipewire-bin pipewire-pulse pulseaudio-utils libspa-0.2-bluetooth)
      flatpak_runtime_packages=(libxcb-cursor0)
      ;;
    dnf)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl flatpak)
      audio_stack_packages=(bluez wireplumber pipewire-utils pipewire-pulseaudio pulseaudio-utils)
      ;;
    zypper)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl flatpak)
      audio_stack_packages=(bluez wireplumber pipewire-tools pipewire-pulseaudio pulseaudio-utils pipewire-spa-plugins-0_2)
      ;;
  esac

  for cmd in python3 mpv ffmpeg playerctl flatpak; do
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

  if ! bt_plugin_present; then
    need_bt_plugin_pkg=1
  fi

  if ! python3 -m venv --help >/dev/null 2>&1; then
    need_venv_pkg=1
  fi

  if [[ ${#flatpak_runtime_packages[@]} -gt 0 ]]; then
    case "$PACKAGE_MANAGER" in
      apt)
        for pkg in "${flatpak_runtime_packages[@]}"; do
          dpkg -s "$pkg" >/dev/null 2>&1 || missing_flatpak_runtime+=("$pkg")
        done
        ;;
    esac
  fi

  if [[ ${#missing_packages[@]} -eq 0 && ${#missing_support[@]} -eq 0 && ${#missing_audio_stack[@]} -eq 0 && ${#missing_flatpak_runtime[@]} -eq 0 && $need_venv_pkg -eq 0 && $need_bt_plugin_pkg -eq 0 ]]; then
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
  if [[ ${#missing_flatpak_runtime[@]} -gt 0 ]]; then
    pkg_install "${missing_flatpak_runtime[@]}"
  fi

  if [[ $need_venv_pkg -eq 1 ]] && ! python3 -m venv --help >/dev/null 2>&1; then
    case "$PACKAGE_MANAGER" in
      dnf)
        pkg_install python3-virtualenv
        ;;
      zypper)
        pkg_install python3-virtualenv
        ;;
      *)
        die "python3 venv support is missing after package install"
        ;;
    esac
  fi

  for cmd in python3 mpv ffmpeg playerctl flatpak curl git socat bluetoothctl wpctl pw-cli pactl; do
    command -v "$cmd" >/dev/null 2>&1 || die "Expected command missing after package install: $cmd"
  done
  if ! bt_plugin_present; then
    warn "PipeWire BlueZ SPA plugin is still missing; Bluetooth input mode will not be available until the host provides libspa-bluez5.so"
  fi
  pass "native packages installed"
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
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.env' \
    --exclude='__pycache__' \
    --exclude='backups' \
    --exclude='*.pyc' \
    --exclude='outputs/*.patch' \
    -C "$SOURCE_DIR" -cf - . | tar -C "$INSTALL_ROOT" -xf -
  pass "project synced to target directory"
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
SPOTIFY_AUTOSTART=off
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

detect_easyeffects_mode() {
  local runtime_dir="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  local native_socket="$runtime_dir/EasyEffectsServer"
  local flatpak_socket="$runtime_dir/.flatpak/com.github.wwmm.easyeffects/xdg-run/EasyEffectsServer"
  local flatpak_tmp_socket="$runtime_dir/.flatpak/com.github.wwmm.easyeffects/tmp/EasyEffectsServer"
  local native_available=0
  local flatpak_available=0

  command -v easyeffects >/dev/null 2>&1 && native_available=1 || true
  if flatpak_app_installed "com.github.wwmm.easyeffects"; then
    flatpak_available=1
  fi

  if [[ -S "$flatpak_socket" && ! -S "$native_socket" ]]; then
    EASYEFFECTS_MODE="flatpak"
    EASYEFFECTS_SOCKET="$flatpak_socket"
  elif [[ -S "$flatpak_tmp_socket" && ! -S "$native_socket" ]]; then
    EASYEFFECTS_MODE="flatpak"
    EASYEFFECTS_SOCKET="$flatpak_tmp_socket"
  elif [[ -S "$native_socket" ]]; then
    EASYEFFECTS_MODE="native"
    EASYEFFECTS_SOCKET="$native_socket"
  elif [[ $native_available -eq 1 ]]; then
    EASYEFFECTS_MODE="native"
    EASYEFFECTS_SOCKET="$native_socket"
  elif [[ $flatpak_available -eq 1 ]]; then
    EASYEFFECTS_MODE="flatpak"
    EASYEFFECTS_SOCKET="$flatpak_socket"
  else
    EASYEFFECTS_MODE="missing"
    EASYEFFECTS_SOCKET="$flatpak_socket"
  fi

  log "EasyEffects mode detected: $EASYEFFECTS_MODE"
}

ensure_flathub_remote() {
  if flatpak remote-list --columns=name --user | grep -qx 'flathub'; then
    return
  fi
  if flatpak remote-list --columns=name | grep -qx 'flathub'; then
    pass "Flathub remote detected (system), adding user remote for user-scoped install"
  fi
  run_cmd flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
}

ensure_easyeffects() {
  detect_easyeffects_mode
  if [[ "$EASYEFFECTS_MODE" != "missing" ]]; then
    EASYEFFECTS_INSTALLED_BY_FXROUTE=0
    EASYEFFECTS_INSTALL_METHOD="$EASYEFFECTS_MODE"
    pass "EasyEffects detected ($EASYEFFECTS_MODE)"
    return
  fi

  ensure_flathub_remote
  if [[ $ASSUME_YES -eq 1 ]]; then
    run_cmd flatpak install --user -y flathub com.github.wwmm.easyeffects
  else
    run_cmd flatpak install --user flathub com.github.wwmm.easyeffects
  fi
  detect_easyeffects_mode
  EASYEFFECTS_INSTALLED_BY_FXROUTE=1
  EASYEFFECTS_INSTALL_METHOD="flatpak"
  [[ "$EASYEFFECTS_MODE" == "flatpak" ]] || warn "EasyEffects install finished but mode is $EASYEFFECTS_MODE"
  pass "EasyEffects installed or refreshed via Flatpak"
}

setup_python_env() {
  local venv_dir="$INSTALL_ROOT/.venv"
  run_cmd python3 -m venv "$venv_dir"
  run_cmd "$venv_dir/bin/python3" -m pip install --upgrade pip setuptools wheel
  run_cmd "$venv_dir/bin/pip" install -r "$INSTALL_ROOT/requirements.txt"
  pass "Python venv created"
  pass "pip install -r requirements.txt"
}

write_service_unit() {
  local service_dir="$HOME/.config/systemd/user"
  mkdir -p "$service_dir"

  cat > "$service_dir/$SERVICE_NAME.service" <<EOF
[Unit]
Description=FXRoute
After=default.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_ROOT
EnvironmentFile=$INSTALL_ROOT/.env
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

install_watchdog_if_needed() {
  local user_systemd_dir="$HOME/.config/systemd/user"
  local watchdog_timer_src="$INSTALL_ROOT/systemd-user/easyeffects-stale-watchdog.timer"
  local watchdog_script="$INSTALL_ROOT/scripts/easyeffects-stale-watchdog.sh"
  local watchdog_service_path="$user_systemd_dir/easyeffects-stale-watchdog.service"
  local watchdog_timer_path="$user_systemd_dir/easyeffects-stale-watchdog.timer"

  [[ -f "$watchdog_timer_src" && -f "$watchdog_script" ]] || return
  chmod +x "$watchdog_script"

  if [[ "$EASYEFFECTS_MODE" == "flatpak" ]]; then
    mkdir -p "$user_systemd_dir"
    if backup_user_file_once "$watchdog_service_path" "easyeffects-stale-watchdog.service.pre-fxroute"; then
      EASYEFFECTS_WATCHDOG_SERVICE_BACKED_UP=1
    fi
    if backup_user_file_once "$watchdog_timer_path" "easyeffects-stale-watchdog.timer.pre-fxroute"; then
      EASYEFFECTS_WATCHDOG_TIMER_BACKED_UP=1
    fi
    cat > "$watchdog_service_path" <<EOF
[Unit]
Description=Recover EasyEffects from a stale Flatpak runtime socket
After=default.target

[Service]
Type=oneshot
ExecStart=$watchdog_script
EOF
    cp "$watchdog_timer_src" "$watchdog_timer_path"
    if systemctl --user daemon-reload && systemctl --user enable --now easyeffects-stale-watchdog.timer; then
      pass "Flatpak EasyEffects watchdog timer enabled"
    else
      warn "Flatpak EasyEffects watchdog files were installed, but the timer could not be enabled in this shell"
    fi
  else
    warn "EasyEffects watchdog parity is only wired for Flatpak mode in pass 1"
    systemctl --user disable --now easyeffects-stale-watchdog.timer >/dev/null 2>&1 || true
    rm -f "$user_systemd_dir/easyeffects-stale-watchdog.service" "$user_systemd_dir/easyeffects-stale-watchdog.timer"
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

ensure_bootstrap_easyeffects_presets() {
  local bootstrap_dir="$INSTALL_ROOT/assets/easyeffects-bootstrap"
  local home_output_dir="$HOME/.var/app/com.github.wwmm.easyeffects/data/easyeffects/output"

  [[ -f "$bootstrap_dir/Direct.json" && -f "$bootstrap_dir/Neutral.json" ]] || return

  mkdir -p "$home_output_dir"
  [[ -f "$home_output_dir/Direct.json" ]] || cp "$bootstrap_dir/Direct.json" "$home_output_dir/Direct.json"
  [[ -f "$home_output_dir/Neutral.json" ]] || cp "$bootstrap_dir/Neutral.json" "$home_output_dir/Neutral.json"
  pass "EasyEffects bootstrap presets ensured"
}

setup_easyeffects_autostart() {
  local autostart_dir="$HOME/.config/autostart"
  local desktop_file="$autostart_dir/easyeffects.desktop"
  local exec_cmd="flatpak run com.github.wwmm.easyeffects --gapplication-service"
  mkdir -p "$autostart_dir"

  if [[ "$EASYEFFECTS_MODE" == "native" ]]; then
    exec_cmd="easyeffects --gapplication-service"
  fi

  if backup_user_file_once "$desktop_file" "easyeffects.desktop.pre-fxroute"; then
    EASYEFFECTS_AUTOSTART_BACKED_UP=1
  fi

  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Exec=$exec_cmd
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Name=EasyEffects Service
Comment=Start EasyEffects in background for FXRoute
EOF

  pass "EasyEffects autostart configured ($EASYEFFECTS_MODE)"

  if systemctl --user daemon-reload >/dev/null 2>&1; then
    if systemctl --user start app-easyeffects@autostart.service >/dev/null 2>&1; then
      pass "EasyEffects background service started"
      return
    fi
  fi

  if nohup bash -lc "$exec_cmd" >/tmp/fxroute-easyeffects-start.log 2>&1 & then
    pass "EasyEffects background service launch requested"
  else
    warn "EasyEffects autostart was configured, but the background service could not be started in this shell"
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
  LAN_HOSTNAME_AFTER="${LAN_HOSTNAME_AFTER:-$(hostname 2>/dev/null || true)}"
  mkdir -p "$(dirname "$state_file")"
  cat > "$state_file" <<EOF
{
  "easyeffects": {
    "installed_by_fxroute": $( [[ $EASYEFFECTS_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "install_method": "${EASYEFFECTS_INSTALL_METHOD:-$EASYEFFECTS_MODE}",
    "detected_mode": "$EASYEFFECTS_MODE",
    "autostart_backed_up": $( [[ $EASYEFFECTS_AUTOSTART_BACKED_UP -eq 1 ]] && echo true || echo false ),
    "watchdog_service_backed_up": $( [[ $EASYEFFECTS_WATCHDOG_SERVICE_BACKED_UP -eq 1 ]] && echo true || echo false ),
    "watchdog_timer_backed_up": $( [[ $EASYEFFECTS_WATCHDOG_TIMER_BACKED_UP -eq 1 ]] && echo true || echo false )
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
    "caddy_was_present_before": $( [[ $CADDY_WAS_PRESENT_BEFORE -eq 1 ]] && echo true || echo false ),
    "caddy_service_was_active_before": $( [[ $CADDY_SERVICE_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "caddy_installed_by_fxroute": $( [[ $CADDY_INSTALLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "default_caddy_disabled_by_fxroute": $( [[ $DEFAULT_CADDY_DISABLED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "caddy_proxy_enabled": $( [[ $CADDY_PROXY_ENABLED -eq 1 ]] && echo true || echo false ),
    "caddy_cert_path": "${CADDY_CERT_PATH}",
    "mdns_guard_enabled": $( [[ $MDNS_GUARD_ENABLED -eq 1 ]] && echo true || echo false ),
    "firewalld_was_active_before": $( [[ $FIREWALLD_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_was_allowed_before": $( [[ $HTTP_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "https_was_allowed_before": $( [[ $HTTPS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "mdns_was_allowed_before": $( [[ $MDNS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_opened_by_fxroute": $( [[ $HTTP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "https_opened_by_fxroute": $( [[ $HTTPS_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
    "mdns_opened_by_fxroute": $( [[ $MDNS_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false )
  }
}
EOF
  pass "install state recorded"
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
exec "$INSTALL_ROOT/install.sh" --local-project
EOF

  cat > "$bin_dir/fxroute-update-ytdlp" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$INSTALL_ROOT/.venv/bin/pip" install -U yt-dlp
EOF

  chmod +x "$bin_dir"/fxroute-*
  pass "helper commands installed in $bin_dir"
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

validate_tools() {
  mpv --version >/dev/null 2>&1 && pass "mpv available" || fail "mpv available"
  ffmpeg -version >/dev/null 2>&1 && pass "ffmpeg available" || fail "ffmpeg available"
  playerctl --version >/dev/null 2>&1 && pass "playerctl available" || fail "playerctl available"
  command -v pactl >/dev/null 2>&1 && pass "pactl available" || fail "pactl available"
  command -v wpctl >/dev/null 2>&1 && pass "wpctl available" || fail "wpctl available"
  command -v pw-cli >/dev/null 2>&1 && pass "pw-cli available" || fail "pw-cli available"
  command -v bluetoothctl >/dev/null 2>&1 && pass "bluetoothctl available" || fail "bluetoothctl available"
  "$INSTALL_ROOT/.venv/bin/yt-dlp" --version >/dev/null 2>&1 && pass "yt-dlp available from venv" || fail "yt-dlp available from venv"

  detect_easyeffects_mode
  if [[ "$EASYEFFECTS_MODE" == "missing" ]]; then
    fail "EasyEffects mode detected"
  else
    pass "EasyEffects mode detected ($EASYEFFECTS_MODE)"
  fi

  if [[ -S "$EASYEFFECTS_SOCKET" ]]; then
    pass "EasyEffects socket found"
  else
    fail "EasyEffects socket found"
    warn "EasyEffects socket is not present yet. This can be normal until EasyEffects has been launched in the user session."
  fi

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
}

print_summary() {
  local env_file="$INSTALL_ROOT/.env"
  local port="8000"
  local lan_ip=""
  local ee_launch_cmd=""
  [[ -f "$env_file" ]] && port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  lan_ip="$(primary_lan_ip)"

  case "$EASYEFFECTS_MODE" in
    flatpak) ee_launch_cmd="flatpak run com.github.wwmm.easyeffects" ;;
    native) ee_launch_cmd="easyeffects" ;;
    *) ee_launch_cmd="flatpak run com.github.wwmm.easyeffects" ;;
  esac

  echo
  if [[ ${#WARNINGS[@]} -eq 0 ]] && ! printf '%s\n' "${VALIDATION_RESULTS[@]}" | grep -q '^FAIL:'; then
    echo "FXRoute installed successfully"
  elif printf '%s\n' "${VALIDATION_RESULTS[@]}" | grep -q '^FAIL:'; then
    echo "FXRoute installed with warnings"
  else
    echo "FXRoute installed with warnings"
  fi

  echo "Install path: $INSTALL_ROOT"
  echo "EasyEffects mode: $EASYEFFECTS_MODE"
  echo "Music folder: $HOME/Music"
  echo
  echo "Open FXRoute:"
  echo " - Local: http://localhost:${port}"
  [[ -n "$lan_ip" ]] && echo " - LAN IP: http://${lan_ip}:${port}"
  if [[ $CADDY_PROXY_ENABLED -eq 1 ]]; then
    if [[ -n "$lan_ip" ]]; then
      echo " - Browser mic HTTPS: https://${lan_ip}"
    fi
    if [[ -n "$MDNS_HOSTNAME" ]]; then
      echo " - Optional .local HTTPS: https://${MDNS_HOSTNAME}.local"
    fi
    if [[ -n "$CADDY_CERT_PATH" ]]; then
      echo " - Install this certificate on client devices, then reload the browser: ${CADDY_CERT_PATH}"
    fi
  else
    echo " - If you want browser microphone capture on LAN devices, enable the optional Caddy HTTPS step."
  fi
  echo " - If LAN access fails, check the host firewall for TCP ${port}."
  echo
  echo "Service: systemctl --user status $SERVICE_NAME"
  echo "Logs: journalctl --user -u $SERVICE_NAME -f"
  echo "Helpers: fxroute-status, fxroute-logs, fxroute-restart, fxroute-update, fxroute-update-ytdlp"

  if ! printf '%s\n' "${VALIDATION_RESULTS[@]}" | grep -q '^PASS: EasyEffects socket found'; then
    echo
    echo "EasyEffects next step:"
    echo " - Launch EasyEffects once in the graphical user session so its socket appears."
    echo " - If you do not see a tray/app icon yet, open your desktop app launcher and search for EasyEffects."
    echo " - Manual launch command: ${ee_launch_cmd}"
  fi

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
    dnf|zypper) avahi_pkg="avahi" ;;
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

install_mdns_guard() {
  local script_path="/usr/local/sbin/fxroute-mdns-guard.sh"
  local service_path="/etc/systemd/system/fxroute-mdns-guard.service"
  local timer_path="/etc/systemd/system/fxroute-mdns-guard.timer"
  local tmp_script=""
  local tmp_service=""
  local tmp_timer=""

  (systemctl is-active avahi-daemon >/dev/null 2>&1 || [[ -n "$MDNS_HOSTNAME" ]]) || return 0

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

  cat > "$tmp_script" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
NFT="/usr/sbin/nft"
[[ -x "$NFT" ]] || NFT="$(command -v nft)"
TABLE="fxroute_mdnsguard"
USER_ID="$(id -u paul 2>/dev/null || echo 1000)"

apply_rules() {
  "$NFT" delete table inet "$TABLE" 2>/dev/null || true
  "$NFT" -f - <<RULES
 table inet ${TABLE} {
   chain output {
     type filter hook output priority 5; policy accept;
     meta skuid ${USER_ID} ip daddr 224.0.0.251 udp dport 5353 counter drop comment "Block user-space mDNS v4 to keep Avahi host advertisement stable"
     meta skuid ${USER_ID} ip6 daddr ff02::fb udp dport 5353 counter drop comment "Block user-space mDNS v6 to keep Avahi host advertisement stable"
   }
 }
RULES
}

remove_rules() {
  "$NFT" delete table inet "$TABLE" 2>/dev/null || true
}

status_rules() {
  "$NFT" list table inet "$TABLE"
}

case "${1:-apply}" in
  apply) apply_rules ;;
  remove) remove_rules ;;
  status) status_rules ;;
  *) echo "usage: $0 [apply|remove|status]" >&2; exit 2 ;;
 esac
EOF

  cat > "$tmp_service" <<'EOF'
[Unit]
Description=FXRoute mDNS guard for Spotify/Avahi coexistence
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
  pass "FXRoute mDNS guard installed"
  return 0
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
    echo "Optional HTTPS step for browser microphone access:"
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
  choose_sudo
  confirm_supported_distro
  capture_lan_comfort_baseline
  ensure_native_packages
  sync_project_tree
  create_env_if_missing
  ensure_easyeffects
  ensure_bootstrap_easyeffects_presets
  setup_python_env
  configure_pipewire_samplerates_if_available
  write_service_unit
  install_watchdog_if_needed
  setup_easyeffects_autostart
  setup_spotify_autostart
  install_helpers
  configure_optional_maintenance_helpers
  validate_http
  validate_tools
  offer_optional_local_lan_name
  install_mdns_guard
  offer_optional_caddy_proxy
  print_summary
  write_install_state
}

main "$@"
