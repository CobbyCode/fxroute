#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
CONFIG_FILE="$CONFIG_DIR/20-audio-mini-pc-samplerates.conf"
BACKUP_SUFFIX="$(date +%Y%m%d_%H%M%S)"
RATES_DEFAULT=(44100 48000 88200 96000 176400 192000 352800 384000 705600 768000)
DESIRED_CONFIG_CONTENT=$(cat <<'EOF'
context.properties = {
    default.clock.rate = 48000
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 705600 768000 ]
}
EOF
)

usage() {
  cat <<'EOF'
Configure PipeWire allowed-rates for the current user via a drop-in file.

Usage:
  configure-pipewire-samplerates.sh [apply|status|remove]

Commands:
  apply   Create/update the user drop-in with the curated full PCM list and restart user audio services.
  status  Show current PipeWire metadata and any installed drop-in file.
  remove  Remove the user drop-in, restart user audio services, and return to system defaults.
EOF
}

restart_services() {
  systemctl --user restart wireplumber.service pipewire.service pipewire-pulse.service
}

show_status() {
  echo "== drop-in file =="
  if [[ -f "$CONFIG_FILE" ]]; then
    sed -n '1,120p' "$CONFIG_FILE"
  else
    echo "not installed"
  fi
  echo
  echo "== pw-metadata settings =="
  pw-metadata -n settings 0 | grep -E "clock.rate|clock.force-rate|clock.allowed-rates" || true
}

apply_config() {
  mkdir -p "$CONFIG_DIR"

  if [[ -f "$CONFIG_FILE" ]] && [[ "$(cat "$CONFIG_FILE")" == "$DESIRED_CONFIG_CONTENT" ]]; then
    echo "PipeWire allowed-rates drop-in already up to date: $CONFIG_FILE"
    show_status
    return 0
  fi

  if [[ -f "$CONFIG_FILE" ]]; then
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.$BACKUP_SUFFIX"
  fi

  printf '%s\n' "$DESIRED_CONFIG_CONTENT" > "$CONFIG_FILE"

  restart_services
  sleep 2
  pw-metadata -n settings 0 clock.force-rate 0 >/dev/null || true
  echo "Applied PipeWire allowed-rates drop-in: $CONFIG_FILE"
  show_status
}

remove_config() {
  rm -f "$CONFIG_FILE"
  restart_services
  sleep 2
  echo "Removed PipeWire allowed-rates drop-in: $CONFIG_FILE"
  show_status
}

cmd="${1:-apply}"
case "$cmd" in
  apply)
    apply_config
    ;;
  status)
    show_status
    ;;
  remove)
    remove_config
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 1
    ;;
esac
