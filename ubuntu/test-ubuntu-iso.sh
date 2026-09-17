#!/usr/bin/env bash
# QEMU-Abnahme fuer das FXRoute-Ubuntu-ISO (Phase 1, Draft).
# Ablauf: Live-Boot (Try -> FXRoute per HTTP) -> Neuinstallation per
# "Install FXRoute" mit Test-Seed -> Appliance-Boot (Autologin/Kiosk/FXRoute).
#
# Aufruf: ubuntu/test-ubuntu-iso.sh [live|install|all] [ISO]
# Benoetigt: qemu-system-x86_64 (KVM), OVMF, ovmf-vars-Kopie, curl, ssh.
# Like the Leap tester, this script is calibrated on real runs; timings below
# are start values and get adjusted once the first manual boot is measured.
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-all}"
ISO="${2:-$ROOT_DIR/dist/fxroute-ubuntu-26.04-x86_64.iso}"
TEST_ROOT="${FXROUTE_UBUNTU_TEST_DIR:-$ROOT_DIR/dist/ubuntu-iso-test}"
RAM_MB="${FXROUTE_UBUNTU_TEST_RAM:-6144}"
CPUS="${FXROUTE_UBUNTU_TEST_CPUS:-4}"
DISK_GB="${FXROUTE_UBUNTU_TEST_DISK_GB:-40}"
HTTP_PORT="${FXROUTE_UBUNTU_TEST_HTTP_PORT:-18000}"
SSH_PORT="${FXROUTE_UBUNTU_TEST_SSH_PORT:-18022}"
TEST_PASSWORD="${FXROUTE_UBUNTU_TEST_PASSWORD:-test}"
OVMF_CODE="${FXROUTE_UBUNTU_OVMF_CODE:-/usr/share/qemu/ovmf-x86_64-4m-code.bin}"

die() { printf '[ubuntu-test][error] %s\n' "$*" >&2; exit 1; }
log() { printf '[ubuntu-test] %s\n' "$*"; }

[[ -f "$ISO" ]] || die "ISO not found: $ISO (build with ubuntu/build-ubuntu-iso.sh first)"
[[ -f "$OVMF_CODE" ]] || die "OVMF code not found: $OVMF_CODE"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 is required"

mkdir -p "$TEST_ROOT"

wait_for_http() {
  local url="$1" timeout_s="$2" i=0
  while (( i < timeout_s )); do
    if curl --fail --silent --max-time 5 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
    i=$(( i + 10 ))
  done
  return 1
}

# GRUB-Auswahl per Monitor-sendkey: Eintraege sind
# 0=Try or Install Ubuntu, 1=safe graphics, 2=Install FXRoute (+EFI-Eintraege).
select_grub_entry() {
  local monitor="$1" downs="$2"
  local i=0
  while (( i < downs )); do
    printf 'sendkey down\n' | socat - "UNIX-CONNECT:$monitor" >/dev/null 2>&1
    sleep 1
    i=$(( i + 1 ))
  done
  sleep 1
  printf 'sendkey ret\n' | socat - "UNIX-CONNECT:$monitor" >/dev/null 2>&1
}

boot_iso() {
  local disk="$1" monitor="$2" pidfile="$3" grub_downs="$4" extra_args="${5:-}"
  local vars="$TEST_ROOT/ovmf-vars.fd"
  rm -f "$vars"
  cp -- /usr/share/qemu/ovmf-x86_64-4m-vars.bin "$vars"
  # TODO: calibrate -boot order/splashwait on the first manual run.
  qemu-system-x86_64 \
    -name fxroute-ubuntu-test -accel kvm -cpu host -smp "$CPUS" -m "${RAM_MB}M" \
    -drive "file=$disk,format=qcow2,if=virtio" \
    -cdrom "$ISO" \
    -boot order=d,menu=off \
    -drive "file=$OVMF_CODE,format=raw,if=pflash,readonly=on" \
    -drive "file=$vars,format=raw,if=pflash" \
    -nic "user,model=virtio-net-pci,hostfwd=tcp::$HTTP_PORT-:8000,hostfwd=tcp::$SSH_PORT-:22" \
    -monitor "unix:$monitor,server=on,wait=off" \
    -device virtio-rng-pci -device ich9-hda -vga virtio -display none \
    -serial "file:$TEST_ROOT/serial.log" \
    -pidfile "$pidfile" -daemonize \
    $extra_args
  sleep 25
  if [[ "$grub_downs" -gt 0 ]]; then
    select_grub_entry "$monitor" "$grub_downs"
  fi
}

boot_disk() {
  local disk="$1" monitor="$2" pidfile="$3"
  local vars="$TEST_ROOT/ovmf-vars.fd"
  qemu-system-x86_64 \
    -name fxroute-ubuntu-appliance -accel kvm -cpu host -smp "$CPUS" -m "${RAM_MB}M" \
    -drive "file=$disk,format=qcow2,if=virtio" \
    -boot order=c,menu=off \
    -drive "file=$OVMF_CODE,format=raw,if=pflash,readonly=on" \
    -drive "file=$vars,format=raw,if=pflash" \
    -nic "user,model=virtio-net-pci,hostfwd=tcp::$HTTP_PORT-:8000,hostfwd=tcp::$SSH_PORT-:22" \
    -monitor "unix:$monitor,server=on,wait=off" \
    -device virtio-rng-pci -device ich9-hda -vga virtio -display none \
    -serial "file:$TEST_ROOT/serial-appliance.log" \
    -pidfile "$pidfile" -daemonize
}

ssh_run() {
  ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -p "$SSH_PORT" "test@127.0.0.1" "$@"
}

phase_live() {
  log "phase live: booting Try or Install Ubuntu (default entry)"
  local disk="$TEST_ROOT/live-check.qcow2" monitor="$TEST_ROOT/live-monitor.sock" pidfile="$TEST_ROOT/live.pid"
  qemu-img create -f qcow2 "$disk" "${DISK_GB}G" >/dev/null
  boot_iso "$disk" "$monitor" "$pidfile" 0
  log "waiting for live FXRoute on :$HTTP_PORT (live install.sh needs minutes)"
  if wait_for_http "http://127.0.0.1:$HTTP_PORT/api/status" 2400; then
    log "live FXRoute reachable"
    curl --silent "http://127.0.0.1:$HTTP_PORT/api/status" | head -c 500
    echo
  else
    die "live FXRoute did not come up; see $TEST_ROOT/serial.log"
  fi
  kill "$(cat "$pidfile")" 2>/dev/null || true
}

phase_install() {
  log "phase install: booting Install FXRoute (GRUB entry 2, test seed)"
  local disk="$TEST_ROOT/install.qcow2" monitor="$TEST_ROOT/install-monitor.sock" pidfile="$TEST_ROOT/install.pid"
  rm -f "$disk"
  qemu-img create -f qcow2 "$disk" "${DISK_GB}G" >/dev/null
  boot_iso "$disk" "$monitor" "$pidfile" 2
  # TODO: replace fixed sleep with install-completion detection (serial log
  # marker from late-commands + reboot watch) after the first manual run.
  log "waiting for autoinstall + first-boot (fixed 60 min budget, draft)"
  sleep 3600
  kill "$(cat "$pidfile")" 2>/dev/null || true
  log "phase appliance: booting installed disk without ISO"
  local amonitor="$TEST_ROOT/appliance-monitor.sock" apidfile="$TEST_ROOT/appliance.pid"
  boot_disk "$disk" "$amonitor" "$apidfile"
  log "waiting for appliance FXRoute on :$HTTP_PORT"
  wait_for_http "http://127.0.0.1:$HTTP_PORT/api/status" 1800 \
    || die "appliance FXRoute did not come up"
  log "checking install-complete marker via SSH"
  ssh_run "test -f /var/lib/fxroute-iso/install-complete" \
    || die "install-complete marker missing"
  ssh_run "systemctl --user is-active fxroute.service" \
    || die "fxroute user service not active"
  log "appliance checks passed"
  kill "$(cat "$apidfile")" 2>/dev/null || true
}

case "$MODE" in
  live) phase_live ;;
  install) phase_install ;;
  all) phase_live; phase_install ;;
  *) die "Unknown mode: $MODE (live|install|all)" ;;
esac
log "done: $MODE"
