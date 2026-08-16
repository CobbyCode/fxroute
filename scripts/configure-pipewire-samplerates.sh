#!/usr/bin/env bash
set -euo pipefail

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
CONFIG_FILE="$CONFIG_DIR/90-fxroute-clock-rate.conf"
LEGACY_FILE="$CONFIG_DIR/20-audio-mini-pc-samplerates.conf"
BACKUP_SUFFIX="$(date +%Y%m%d_%H%M%S)"
# Canonical FXRoute-managed PipeWire clock rates.  FXRoute processes at most
# 384 kHz; the old full list (including 705600/768000) is deliberately gone
# from the managed configuration.  The DAC's own native capability is not
# affected: PipeWire still negotiates whatever the hardware reports.
DESIRED_CONFIG_CONTENT=$(cat <<'EOF'
# Managed by FXRoute. Changes take effect after restarting PipeWire/session or rebooting.
context.properties = {
    default.clock.rate = 44100
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
}
EOF
)

usage() {
  cat <<'EOF'
Configure the FXRoute-managed PipeWire clock-rate drop-in for the current user.

Usage:
  configure-pipewire-samplerates.sh [apply|status|remove]

Commands:
  apply   Write the canonical FXRoute drop-in (90-fxroute-clock-rate.conf),
          remove the superseded 20-audio-mini-pc-samplerates.conf drop-in and
          its backups, and restart user audio services.  Idempotent: repeated
          runs leave the file byte-identical and restart nothing.
  status  Show current PipeWire metadata and any installed drop-in file.
  remove  Remove the FXRoute drop-in and the legacy drop-in, restart user
          audio services, and return to system defaults.
EOF
}

restart_services() {
  systemctl --user restart wireplumber.service pipewire.service pipewire-pulse.service
}

remove_legacy_dropin() {
  # The pre-0.7.5x "audio-mini-pc" drop-in carried a different, larger rate
  # list (up to 705600/768000).  It must not coexist with the canonical
  # FXRoute drop-in: PipeWire merges all drop-ins and the alphabetically
  # later file's context.properties wins, which made the two files fight.
  if [[ -e "$LEGACY_FILE" ]]; then
    rm -f "$LEGACY_FILE"
    echo "Removed superseded PipeWire drop-in: $LEGACY_FILE"
  fi
  local backups=( "$LEGACY_FILE".bak.* )
  if [[ -e "${backups[0]:-}" ]]; then
    rm -f "${backups[@]}"
    echo "Removed superseded PipeWire drop-in backups: $LEGACY_FILE.bak.*"
  fi
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
  remove_legacy_dropin

  if [[ -f "$CONFIG_FILE" ]] && [[ "$(cat "$CONFIG_FILE")" == "$DESIRED_CONFIG_CONTENT" ]]; then
    pw-metadata -n settings 0 clock.force-rate 0 >/dev/null || true
    echo "PipeWire FXRoute clock-rate drop-in already up to date: $CONFIG_FILE"
    show_status
    return 0
  fi

  if [[ -f "$CONFIG_FILE" ]]; then
    cp "$CONFIG_FILE" "$CONFIG_FILE.bak.$BACKUP_SUFFIX"
  fi

  # Write the complete canonical content atomically instead of appending to
  # any previous FXRoute-managed state, so repeated runs never stack blocks.
  local tmp_file="$CONFIG_FILE.tmp.$$"
  printf '%s\n' "$DESIRED_CONFIG_CONTENT" > "$tmp_file"
  mv "$tmp_file" "$CONFIG_FILE"

  restart_services
  sleep 2
  pw-metadata -n settings 0 clock.force-rate 0 >/dev/null || true
  echo "Applied PipeWire FXRoute clock-rate drop-in: $CONFIG_FILE"
  show_status
}

remove_config() {
  rm -f "$CONFIG_FILE"
  remove_legacy_dropin
  restart_services
  sleep 2
  echo "Removed PipeWire FXRoute clock-rate drop-in: $CONFIG_FILE"
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
