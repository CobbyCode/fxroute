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
EASYEFFECTS_INSTALLED_BY_FXROUTE=0
EASYEFFECTS_INSTALL_METHOD=""
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
FIREWALLD_WAS_ACTIVE_BEFORE=0
HTTP_WAS_ALLOWED_BEFORE=0
MDNS_WAS_ALLOWED_BEFORE=0
HTTP_OPENED_BY_FXROUTE=0
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

valid_local_hostname() {
  local value="${1,,}"
  [[ "$value" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]
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
  [[ -n "$path" ]] && printf '%s\n' "$path"
}

firewalld_is_active() {
  local firewall_cmd=""
  firewall_cmd="$(firewall_cmd_path)"
  [[ -n "$firewall_cmd" ]] || return 1
  "${SUDO_CMD[@]}" "$firewall_cmd" --state >/dev/null 2>&1
}

firewalld_query_service() {
  local service="$1"
  local firewall_cmd=""
  firewall_cmd="$(firewall_cmd_path)"
  [[ -n "$firewall_cmd" ]] || return 1
  firewalld_is_active || return 1
  "${SUDO_CMD[@]}" "$firewall_cmd" --query-service="$service" >/dev/null 2>&1
}

ensure_firewalld_service_open() {
  local service="$1"
  local purpose="$2"
  local firewall_cmd=""

  firewall_cmd="$(firewall_cmd_path)"
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
    mdns) MDNS_OPENED_BY_FXROUTE=1 ;;
  esac

  pass "firewalld service opened ($service)"
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
  local missing_packages=()
  local missing_support=()
  local need_venv_pkg=0

  case "$PACKAGE_MANAGER" in
    apt)
      core_packages=(python3 python3-pip python3-venv mpv ffmpeg playerctl flatpak)
      ;;
    dnf)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl flatpak)
      ;;
    zypper)
      core_packages=(python3 python3-pip mpv ffmpeg playerctl flatpak)
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

  if ! python3 -m venv --help >/dev/null 2>&1; then
    need_venv_pkg=1
  fi

  if [[ ${#missing_packages[@]} -eq 0 && ${#missing_support[@]} -eq 0 && $need_venv_pkg -eq 0 ]]; then
    pass "native packages already available"
    return
  fi

  if [[ ${#missing_packages[@]} -gt 0 ]]; then
    pkg_install "${core_packages[@]}"
  fi
  if [[ ${#missing_support[@]} -gt 0 ]]; then
    pkg_install "${support_packages[@]}"
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

  for cmd in python3 mpv ffmpeg playerctl flatpak curl; do
    command -v "$cmd" >/dev/null 2>&1 || die "Expected command missing after package install: $cmd"
  done
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
    if systemctl --user enable --now "$SERVICE_NAME"; then
      pass "systemd user service enabled"
    else
      fail "systemd user service enable/start"
      warn "systemctl --user enable --now $SERVICE_NAME failed, likely because no active user bus is available in this shell"
    fi
  else
    fail "systemd user daemon-reload"
  fi
}

install_watchdog_if_needed() {
  local user_systemd_dir="$HOME/.config/systemd/user"
  local watchdog_timer_src="$INSTALL_ROOT/systemd-user/easyeffects-stale-watchdog.timer"
  local watchdog_script="$INSTALL_ROOT/scripts/easyeffects-stale-watchdog.sh"

  [[ -f "$watchdog_timer_src" && -f "$watchdog_script" ]] || return
  chmod +x "$watchdog_script"

  if [[ "$EASYEFFECTS_MODE" == "flatpak" ]]; then
    mkdir -p "$user_systemd_dir"
    cat > "$user_systemd_dir/easyeffects-stale-watchdog.service" <<EOF
[Unit]
Description=Recover EasyEffects from a stale Flatpak runtime socket
After=default.target

[Service]
Type=oneshot
ExecStart=$watchdog_script
EOF
    cp "$watchdog_timer_src" "$user_systemd_dir/"
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
  local exec_cmd=""

  mkdir -p "$autostart_dir"

  if ! env_setting_enabled "$enabled_value"; then
    rm -f "$desktop_file"
    pass "Spotify autostart disabled"
    return
  fi

  if ! exec_cmd="$(detect_spotify_autostart_command)"; then
    rm -f "$desktop_file"
    warn "Spotify autostart is enabled in .env, but no local Spotify desktop app (Flatpak or native) was found"
    return
  fi

  cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Exec=$exec_cmd
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
    "detected_mode": "$EASYEFFECTS_MODE"
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
    "firewalld_was_active_before": $( [[ $FIREWALLD_WAS_ACTIVE_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_was_allowed_before": $( [[ $HTTP_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "mdns_was_allowed_before": $( [[ $MDNS_WAS_ALLOWED_BEFORE -eq 1 ]] && echo true || echo false ),
    "http_opened_by_fxroute": $( [[ $HTTP_OPENED_BY_FXROUTE -eq 1 ]] && echo true || echo false ),
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
    rm -f "$tmp_service" "$tmp_timer"
    warn "System auto-update helper could not be enabled because $service_path could not be written"
    return
  fi
  if ! "${SUDO_CMD[@]}" install -m 644 "$tmp_timer" "$timer_path"; then
    rm -f "$tmp_service" "$tmp_timer"
    warn "System auto-update helper could not be enabled because $timer_path could not be written"
    return
  fi
  rm -f "$tmp_service" "$tmp_timer"

  if "${SUDO_CMD[@]}" systemctl daemon-reload && "${SUDO_CMD[@]}" systemctl enable --now "$timer_name"; then
    pass "Optional system auto-update helper enabled (${interval_hours}h)"
  else
    warn "Optional system auto-update helper files were installed, but the timer could not be enabled in this shell"
  fi
}

configure_optional_maintenance_helpers() {
  configure_spotify_cache_cleanup_helper
  configure_system_auto_update_helper
}

validate_http() {
  local env_file="$INSTALL_ROOT/.env"
  local port
  port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  [[ -n "$port" ]] || port=8000

  sleep 3
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
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

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

  if [[ ${#WARNINGS[@]} -gt 0 ]]; then
    echo
    echo "Warnings:"
    printf ' - %s\n' "${WARNINGS[@]}"
  fi
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

  ensure_firewalld_service_open mdns ".local LAN access"

  MDNS_HOSTNAME="$desired_host"
  pass "optional .local LAN name configured (${MDNS_HOSTNAME}.local:${port})"
  echo
  echo "Optional LAN name ready: http://${MDNS_HOSTNAME}.local:${port}"
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

  [[ -n "$MDNS_HOSTNAME" ]] || return 0
  [[ -t 0 && -t 1 ]] || return 0

  [[ -f "$env_file" ]] && port="$(grep '^PORT=' "$env_file" | cut -d= -f2- | tr -d '[:space:]')"
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

  echo
  if systemctl is-active "$service_name" >/dev/null 2>&1; then
    CADDY_PROXY_ENABLED=1
    ensure_firewalld_service_open http "FXRoute port-80 LAN access"
    echo "Optional port-80 LAN URL already active: http://${MDNS_HOSTNAME}.local"
    [[ -n "$lan_ip" ]] && echo "Port-80 LAN IP also works: http://${lan_ip}"
    return 0
  fi

  echo "Optional LAN comfort step 2:"
  echo "Enable Caddy so FXRoute can also be reached without :${port}, for example http://${MDNS_HOSTNAME}.local ?"
  echo "This may require one more sudo step."
  printf "Enable port-80 Caddy reverse proxy? [y/N] "
  read -r reply || return 0
  case "${reply,,}" in
    y|yes) ;;
    *) return 0 ;;
  esac

  if ss -ltn '( sport = :80 )' 2>/dev/null | tail -n +2 | grep -q .; then
    warn "Optional Caddy setup skipped because TCP port 80 is already in use"
    return 0
  fi

  if ! pkg_install caddy; then
    warn "Optional Caddy setup failed while installing Caddy"
    return 0
  fi
  if [[ $CADDY_WAS_PRESENT_BEFORE -eq 0 ]] && command -v caddy >/dev/null 2>&1; then
    CADDY_INSTALLED_BY_FXROUTE=1
  fi

  caddy_bin="$(command -v caddy || true)"
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
:80 {
    reverse_proxy 127.0.0.1:${port}
}
EOF

  cat > "$tmp_service" <<EOF
[Unit]
Description=FXRoute Caddy reverse proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${caddy_bin} run --config ${config_path} --adapter caddyfile
ExecReload=${caddy_bin} reload --config ${config_path} --adapter caddyfile
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  if ! "${SUDO_CMD[@]}" install -d "$config_dir"; then
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
  if ! "${SUDO_CMD[@]}" systemctl enable --now "${service_name}.service"; then
    warn "Optional Caddy setup failed while enabling ${service_name}.service"
    return 0
  fi
  CADDY_PROXY_ENABLED=1
  sleep 2
  if ! curl -fsS http://127.0.0.1/api/status >/dev/null 2>&1; then
    warn "Optional Caddy setup finished, but the port-80 health check did not answer yet"
    return 0
  fi

  ensure_firewalld_service_open http "FXRoute port-80 LAN access"

  pass "optional Caddy reverse proxy configured (http://${MDNS_HOSTNAME}.local)"
  echo
  echo "Optional port-80 LAN URL ready: http://${MDNS_HOSTNAME}.local"
  [[ -n "$lan_ip" ]] && echo "Port-80 LAN IP also works: http://${lan_ip}"
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
  print_summary
  offer_optional_local_lan_name
  offer_optional_caddy_proxy
  write_install_state
}

main "$@"
