#!/usr/bin/env bash
# QEMU-Abnahme fuer das FXRoute-Ubuntu-ISO (Phase 1, Draft).
# Ablauf: Live-Boot (Try -> FXRoute per HTTP) -> Neuinstallation per
# "Install FXRoute" mit Test-Seed -> Appliance-Boot (Autologin/Kiosk/FXRoute).
#
# Usage: ubuntu/test-ubuntu-iso.sh [live|install|appliance|all] [ISO]
# appliance reuses install.qcow2 and ovmf-vars.fd; it does not need an ISO.
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

# Preflight runs only from main, so helpers can be sourced safely.
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

# GRUB-Eintraege (verifiziert am ISO): 0=Try FXRoute Live,
# 1=Install FXRoute, 2=Ubuntu (safe graphics) (+EFI-Eintraege).
# QEMU-Monitor per Python-Stdlib (kein socat noetig).
qemu_monitor() {
  python3 - "$1" "$2" <<'PY'
import socket, sys
path, cmd = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(path)
s.recv(4096)
s.sendall((cmd + '\n').encode())
s.recv(4096)
s.close()
PY
}
select_grub_entry() {
  local monitor="$1" downs="$2"
  local i=0
  sleep 5
  while (( i < downs )); do
    qemu_monitor "$monitor" "sendkey down"
    sleep 1
    i=$(( i + 1 ))
  done
  sleep 1
  qemu_monitor "$monitor" "sendkey ret"
}

boot_iso() {
  local disk="$1" monitor="$2" pidfile="$3" grub_downs="$4"
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
    -device virtio-rng-pci -device intel-hda -device hda-duplex -vga virtio -display none \
    -serial "file:$TEST_ROOT/serial.log" \
    -pidfile "$pidfile" -daemonize
  sleep 25
  select_grub_entry "$monitor" "$grub_downs"
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
    -device virtio-rng-pci -device intel-hda -device hda-duplex -vga virtio -display none \
    -serial "file:$TEST_ROOT/serial-appliance.log" \
    -pidfile "$pidfile" -daemonize
}

ssh_run() {
  SSH_ASKPASS="$TEST_ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$TEST_PASSWORD" setsid -w ssh \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -p "$SSH_PORT" "test@127.0.0.1" "$@"
}

TEST_ASKPASS="$ROOT_DIR/ubuntu/test-askpass.sh"

live_ssh_run() {
  SSH_ASKPASS="$TEST_ASKPASS" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$TEST_PASSWORD" setsid -w ssh \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -p "$SSH_PORT" "ubuntu@127.0.0.1" "$@"
}

wait_for_live_ssh() {
  local timeout_s="$1" i=0
  while (( i < timeout_s )); do
    if live_ssh_run true >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
    i=$(( i + 10 ))
  done
  return 1
}

wait_for_check() {
  local timeout_s="$1" elapsed=0
  shift
  while (( elapsed < timeout_s )); do
    if "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 10
    elapsed=$(( elapsed + 10 ))
  done
  # Show the last failure for diagnosis.
  "$@"
}

check_installed_os() {
  ssh_run '
    set -eu
    root_type=$(findmnt -n -o FSTYPE /)
    root_source=$(findmnt -n -o SOURCE /)
    test -n "$root_type"
    test "$root_type" != overlay
    case "$root_source" in /dev/*) ;; *) exit 1 ;; esac
    cmdline=$(cat /proc/cmdline)
    case " $cmdline " in *" boot=casper "*|*" autoinstall "*|*" autoinstall="*) exit 1 ;; esac
    test -f /var/lib/fxroute-iso/install-complete
  '
}

check_appliance() {
  check_installed_os || return 1
  local profile="${FXROUTE_UBUNTU_TEST_PROFILE:-desktop}"
  if [[ "$profile" == headless ]]; then
    ssh_run '
      set -eu
      export XDG_RUNTIME_DIR=/run/user/$(id -u)
      export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
      systemctl --user is-active --quiet fxroute.service
      systemctl --user is-active --quiet pipewire.service
      systemctl --user is-active --quiet wireplumber.service
      # Headless contract: no display manager, no graphical session,
      # no Firefox; SSH active; multi-user default target.
      if systemctl is-active --quiet display-manager.service; then exit 1; fi
      test "$(systemctl get-default)" = multi-user.target
      if loginctl list-sessions --no-legend | grep -q seat; then exit 1; fi
      if pgrep -f "(^|/)[f]irefox([[:space:]]|$)" >/dev/null; then exit 1; fi
      if pgrep -x gnome-shell >/dev/null; then exit 1; fi
      systemctl is-active --quiet ssh.service
      curl --fail --silent http://127.0.0.1:8000/api/status >/dev/null
    '
  else
    ssh_run '
      set -eu
      export XDG_RUNTIME_DIR=/run/user/$(id -u)
      export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
      systemctl --user is-active --quiet fxroute.service
      systemctl --user is-active --quiet pipewire.service
      systemctl --user is-active --quiet wireplumber.service
      systemctl is-active --quiet display-manager.service
      session=$(loginctl show-seat seat0 -p ActiveSession --value)
      test -n "$session"
      test "$(loginctl show-session "$session" -p Name --value)" = test
      test "$(loginctl show-session "$session" -p Active --value)" = yes
      test "$(loginctl show-session "$session" -p Service --value)" = gdm-autologin
      session_type=$(loginctl show-session "$session" -p Type --value)
      case "$session_type" in wayland|x11) ;; *) exit 1 ;; esac
      pgrep -u "$(id -u)" -f "(^|/)[f]irefox[[:space:]].*--kiosk([[:space:]]|$)" >/dev/null
      test "$(gsettings get org.gnome.desktop.session idle-delay)" = "uint32 0"
      test "$(gsettings get org.gnome.desktop.screensaver lock-enabled)" = false
    '
  fi
}

shutdown_guest() {
  local monitor="$1" pidfile="$2" pid elapsed=0
  pid="$(cat "$pidfile")"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || die "Invalid PID file: $pidfile"
  qemu_monitor "$monitor" system_powerdown
  while kill -0 "$pid" 2>/dev/null; do
    (( elapsed < 180 )) || die "Guest did not shut down cleanly; left running ($pidfile)"
    sleep 5
    elapsed=$(( elapsed + 5 ))
  done
}

ensure_guests_stopped() {
  local pidfile pid
  for pidfile in "$TEST_ROOT/live.pid" "$TEST_ROOT/install.pid" "$TEST_ROOT/appliance.pid"; do
    [[ -f "$pidfile" ]] || continue
    pid="$(cat "$pidfile")"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || die "Invalid PID file: $pidfile"
    if kill -0 "$pid" 2>/dev/null; then
      die "Guest still running ($pidfile); shut it down before testing"
    fi
  done
}

phase_appliance() {
  local disk="$TEST_ROOT/install.qcow2" monitor="$TEST_ROOT/appliance-monitor.sock" pidfile="$TEST_ROOT/appliance.pid"
  [[ -f "$disk" ]] || die "Installed disk not found: $disk"
  [[ -f "$TEST_ROOT/ovmf-vars.fd" ]] || die "Installed OVMF vars not found"
  ensure_guests_stopped
  log "booting installed disk without ISO"
  boot_disk "$disk" "$monitor" "$pidfile"
  log "waiting for appliance FXRoute on :$HTTP_PORT"
  wait_for_http "http://127.0.0.1:$HTTP_PORT/api/status" 1800 \
    || die "appliance FXRoute did not come up; see $TEST_ROOT/serial-appliance.log"
  log "waiting for installed OS, services, seat0 autologin, kiosk and desktop settings"
  wait_for_check 600 check_appliance \
    || die "appliance checks failed; guest left running for diagnosis"
  log "appliance checks passed"
  shutdown_guest "$monitor" "$pidfile"
}

phase_live() {
  log "phase live: booting Try FXRoute Live (default entry)"
  local disk="$TEST_ROOT/live-check.qcow2" monitor="$TEST_ROOT/live-monitor.sock" pidfile="$TEST_ROOT/live.pid"
  qemu-img create -f qcow2 "$disk" "${DISK_GB}G" >/dev/null
  # Kernel extras (console=ttyS0, live SSH hook) are baked into the
  # test-seed ISO Try entry at build time; plain default boot here.
  boot_iso "$disk" "$monitor" "$pidfile" 0
  log "waiting for live SSH (test hook)"
  wait_for_live_ssh 1200 || log "live SSH did not come up (continuing with HTTP only)"
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
  # GRUB order: 0=Try FXRoute Live, 1=Install FXRoute Desktop,
  # 2=Install FXRoute Headless (FXROUTE_UBUNTU_TEST_GRUB selects the
  # profile under test; default = desktop).
  local profile="${FXROUTE_UBUNTU_TEST_PROFILE:-desktop}"
  local grub_downs=1
  case "$profile" in
    desktop) grub_downs=1 ;;
    headless) grub_downs=2 ;;
    *) die "Unknown profile: $profile (desktop|headless)" ;;
  esac
  log "phase install: booting Install FXRoute $profile (GRUB entry $grub_downs, test seed)"
  local disk="$TEST_ROOT/install.qcow2" monitor="$TEST_ROOT/install-monitor.sock" pidfile="$TEST_ROOT/install.pid"
  rm -f "$disk"
  qemu-img create -f qcow2 "$disk" "${DISK_GB}G" >/dev/null
  rm -f "$TEST_ROOT/serial.log"
  boot_iso "$disk" "$monitor" "$pidfile" "$grub_downs"
  # Fast-fail if GRUB selection missed the Install entry (no autoinstall on
  # the kernel cmdline): better than a blind 60 min wait.
  log "waiting for installer SSH to verify the Install entry boot"
  if wait_for_live_ssh 900; then
    if live_ssh_run "grep -q autoinstall /proc/cmdline"; then
      live_ssh_run "grep -q 'autoinstallpath=/cdrom/fxroute-seed/$profile.yaml' /proc/cmdline" \
        || die "wrong profile seed on cmdline (expected $profile); see $TEST_ROOT/serial.log"
      log "Install FXRoute $profile entry confirmed (autoinstall + profile seed on cmdline)"
    else
      die "guest booted without autoinstall (wrong GRUB entry); see $TEST_ROOT/serial.log"
    fi
  else
    die "installer SSH did not come up; see $TEST_ROOT/serial.log"
  fi
  # EFI may boot the installed LVM root directly even with the ISO attached.
  log "waiting for installed OS and install-complete marker (60 min budget)"
  wait_for_check 3600 check_installed_os \
    || die "installed OS did not become ready; guest left running; see $TEST_ROOT/serial.log"
  shutdown_guest "$monitor" "$pidfile"
  phase_appliance
}

main() {
  case "$MODE" in
    live|install|all)
      [[ -f "$ISO" ]] || die "ISO not found: $ISO (build with ubuntu/build-ubuntu-iso.sh first)" ;;
    appliance) ;;
    *) die "Unknown mode: $MODE (live|install|appliance|all)" ;;
  esac
  [[ -f "$OVMF_CODE" ]] || die "OVMF code not found: $OVMF_CODE"
  command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 is required"
  mkdir -p "$TEST_ROOT"
  ensure_guests_stopped
  case "$MODE" in
    live) phase_live ;;
    install) phase_install ;;
    appliance) phase_appliance ;;
    all) phase_live; phase_install ;;
  esac
  log "done: $MODE"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main
fi
