#!/usr/bin/env bash
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_SELECTION="${1:-all}"
ISO_PATH="${2:-${FXROUTE_ISO:-$ROOT_DIR/dist/fxroute-leap-16-x86_64.iso}}"
TEST_ROOT="${FXROUTE_ISO_TEST_DIR:-$ROOT_DIR/dist/iso-test}"
SSH_KEY="${FXROUTE_SSH_KEY:-$HOME/.ssh/id_ed25519_vm}"
VM_RAM="${FXROUTE_VM_RAM:-4096}"
VM_CPUS="${FXROUTE_VM_CPUS:-4}"
VM_DISK_GB="${FXROUTE_VM_DISK_GB:-40}"
TIMEOUT_SECONDS="${FXROUTE_ISO_TEST_TIMEOUT:-5400}"
KEEP_TEST_ROOT="${FXROUTE_KEEP_ISO_TEST:-0}"

usage() {
  cat <<EOF
Usage: $0 [all|headless|desktop] [ISO]

Boot a fresh QEMU/KVM guest for each selected profile and verify the
installed FXRoute service, DSP setup, and desktop selection.

The guest root account must have the public key used to build the ISO. Set
FXROUTE_SSH_KEY to the matching private key. Test disks and logs are stored
under FXROUTE_ISO_TEST_DIR (default: dist/iso-test).
EOF
}

die() {
  printf '[iso-test][error] %s\n' "$*" >&2
  exit 1
}

case "$PROFILE_SELECTION" in
  all) PROFILES=(headless desktop) ;;
  headless|desktop) PROFILES=("$PROFILE_SELECTION") ;;
  -h|--help) usage; exit 0 ;;
  *) die "Profile must be all, headless, or desktop" ;;
esac

[[ -f "$ISO_PATH" ]] || die "ISO not found: $ISO_PATH"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img is required"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 is required"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v ssh >/dev/null 2>&1 || die "ssh is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
[[ -f "$SSH_KEY" ]] || die "SSH private key not found: $SSH_KEY"

if [[ -e "$TEST_ROOT" ]]; then
  die "Test root already exists; remove it before rerunning: $TEST_ROOT"
fi
mkdir -p "$TEST_ROOT"
PIDS=()
wait_for_qemu_processes() {
  local alive=0
  local pid=""

  for _ in $(seq 1 30); do
    alive=0
    for pid in "${PIDS[@]:-}"; do
      [[ -n "$pid" ]] || continue
      if kill -0 "$pid" 2>/dev/null; then
        alive=1
      fi
    done
    [[ "$alive" -eq 0 ]] && return 0
    sleep 1
  done

  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -KILL "$pid" 2>/dev/null || true
  done
}

cleanup() {
  local pid
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill "$pid" 2>/dev/null || true
  done
  wait_for_qemu_processes
  if [[ "$KEEP_TEST_ROOT" -eq 0 ]]; then
    rm -rf -- "$TEST_ROOT"
  else
    printf '[iso-test] keeping test root: %s\n' "$TEST_ROOT"
  fi
}
trap cleanup EXIT

find_free_port() {
  python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

port_for_profile() {
  case "$1" in
    headless) printf '%s\n' "${FXROUTE_ISO_TEST_HEADLESS_HTTP_PORT:-$(find_free_port)}" ;;
    desktop) printf '%s\n' "${FXROUTE_ISO_TEST_DESKTOP_HTTP_PORT:-$(find_free_port)}" ;;
  esac
}

ssh_port_for_profile() {
  case "$1" in
    headless) printf '%s\n' "${FXROUTE_ISO_TEST_HEADLESS_SSH_PORT:-$(find_free_port)}" ;;
    desktop) printf '%s\n' "${FXROUTE_ISO_TEST_DESKTOP_SSH_PORT:-$(find_free_port)}" ;;
  esac
}

select_boot_entry() {
  local profile="$1"
  local monitor="$2"
  local down_count=""

  # The BIOS GRUB menu contains "Boot from Hard Disk", the regular installer,
  # Desktop, and Headless in that order. Start from the first entry so this
  # remains independent of any saved GRUB selection.
  case "$profile" in
    desktop) down_count=2 ;;
    headless) down_count=3 ;;
    *) die "Unknown profile: $profile" ;;
  esac

  for _ in $(seq 1 20); do
    [[ -S "$monitor" ]] && break
    sleep 1
  done
  [[ -S "$monitor" ]] || die "QEMU monitor did not start for $profile"
  python3 - "$monitor" "$down_count" <<'PY'
import os
from pathlib import Path
import socket
import sys
import tempfile
import time

monitor, down_count = sys.argv[1:]
screen_fd, screen_name = tempfile.mkstemp(
    dir="/tmp", prefix="fxroute-iso-grub-", suffix=".ppm"
)
os.close(screen_fd)
screen_path = Path(screen_name)

sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.settimeout(5)
sock.connect(monitor)
sock.setblocking(False)


def drain_monitor_output():
    while True:
        try:
            if not sock.recv(4096):
                return
        except BlockingIOError:
            return


def monitor_command(command):
    sock.sendall((command + "\n").encode())
    time.sleep(0.05)
    drain_monitor_output()


def grub_menu_ready(path):
    try:
        data = path.read_bytes()
    except OSError:
        return False
    header = data.split(b"\n", 3)
    if len(header) != 4 or header[0] != b"P6":
        return False
    try:
        width, height = (int(value) for value in header[1].split())
        max_value = int(header[2])
    except ValueError:
        return False
    if width < 1 or height < 1 or max_value != 255:
        return False
    pixels = header[3]
    if len(pixels) < width * height * 3:
        return False

    # The openSUSE GRUB theme draws the active menu item as a wide, uniform
    # bar. This distinguishes the menu from SeaBIOS and its loading screen.
    background = tuple(pixels[:3])
    highlighted_rows = 0
    for row in range(height):
        row_start = row * width * 3
        row_end = row_start + width * 3
        previous = background
        run_length = 0
        longest_run = 0
        for offset in range(row_start, row_end, 3):
            color = tuple(pixels[offset : offset + 3])
            if color != background and color == previous:
                run_length += 1
            elif color != background:
                run_length = 1
            else:
                run_length = 0
            previous = color
            longest_run = max(longest_run, run_length)
        if longest_run >= max(64, width // 4):
            highlighted_rows += 1
    return highlighted_rows >= 5


try:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            screen_path.unlink()
        except FileNotFoundError:
            pass
        monitor_command(f"screendump {screen_path}")
        for _ in range(10):
            if grub_menu_ready(screen_path):
                monitor_command("sendkey home")
                for _ in range(int(down_count)):
                    monitor_command("sendkey down")
                monitor_command("sendkey ret")
                raise SystemExit(0)
            time.sleep(0.05)
        time.sleep(0.2)
    raise SystemExit("timed out waiting for the GRUB menu")
finally:
    sock.close()
    try:
        screen_path.unlink()
    except FileNotFoundError:
        pass
PY
}

ssh_guest() {
  local ssh_port="$1"
  shift
  ssh -i "$SSH_KEY" \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o ServerAliveInterval=5 \
    -o ServerAliveCountMax=1 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -p "$ssh_port" root@127.0.0.1 "$@"
}

stop_guest() {
  local profile="$1"
  local pid="$2"
  local ssh_port="$3"

  ssh_guest "$ssh_port" systemctl poweroff >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  printf '[iso-test] forcing QEMU shutdown for %s\n' "$profile" >&2
  kill "$pid" 2>/dev/null || true
}

wait_for_guest() {
  local profile="$1"
  local pid="$2"
  local http_port="$3"
  local ssh_port="$4"
  local started=$SECONDS
  local status_file="$TEST_ROOT/$profile-status.json"

  while (( SECONDS - started < TIMEOUT_SECONDS )); do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '[iso-test] QEMU exited while waiting for %s\n' "$profile" >&2
      tail -100 "$TEST_ROOT/$profile-serial.log" >&2 || true
      tail -100 "$TEST_ROOT/$profile-qemu.log" >&2 || true
      return 1
    fi
    if ssh_guest "$ssh_port" test -f /var/lib/fxroute-iso/install-failed >/dev/null 2>&1; then
      printf '[iso-test] %s first-boot setup failed\n' "$profile" >&2
      ssh_guest "$ssh_port" cat /var/lib/fxroute-iso/install-failed >&2 || true
      ssh_guest "$ssh_port" systemctl status fxroute-first-boot.service --no-pager -l >&2 || true
      return 1
    fi
    if ssh_guest "$ssh_port" test -f /var/lib/fxroute-iso/install-complete >/dev/null 2>&1; then
      if curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
          "http://127.0.0.1:$http_port/api/status" > "$status_file"; then
        printf '[iso-test] %s guest is installed and FXRoute is serving HTTP\n' "$profile"
        return 0
      fi
    fi
    sleep 5
  done
  printf '[iso-test] timeout waiting for %s\n' "$profile" >&2
  tail -160 "$TEST_ROOT/$profile-serial.log" >&2 || true
  tail -160 "$TEST_ROOT/$profile-qemu.log" >&2 || true
  return 1
}

verify_ssh_hardening() {
  local ssh_port="$1"
  ssh_guest "$ssh_port" bash -s <<'EOF'
set -Eeuo pipefail
sshd_config=/etc/ssh/sshd_config.d/90-fxroute-iso.conf
test -f "$sshd_config"
grep -Fxq 'PasswordAuthentication no' "$sshd_config"
grep -Fxq 'KbdInteractiveAuthentication no' "$sshd_config"
grep -Fxq 'PermitRootLogin prohibit-password' "$sshd_config"
sshd -T | grep -Fx 'passwordauthentication no' >/dev/null
sshd -T | grep -Fx 'kbdinteractiveauthentication no' >/dev/null
sshd -T | grep -E '^permitrootlogin (prohibit-password|without-password)$' >/dev/null
root_password_hash="$(getent shadow root | cut -d: -f2)"
case "$root_password_hash" in
  ""|\!*|\**)
    ;;
  *)
    printf 'root password is unexpectedly set\n' >&2
    exit 1
    ;;
esac
EOF
}

reboot_guest() {
  local profile="$1"
  local pid="$2"
  local ssh_port="$3"
  local old_boot_id
  local new_boot_id

  printf '[iso-test] rebooting %s guest to verify its configured boot target\n' "$profile"
  old_boot_id="$(ssh_guest "$ssh_port" cat /proc/sys/kernel/random/boot_id)"
  ssh_guest "$ssh_port" systemctl reboot >/dev/null 2>&1 || true
  for _ in $(seq 1 90); do
    kill -0 "$pid" 2>/dev/null || {
      printf '[iso-test] QEMU exited while rebooting %s\n' "$profile" >&2
      return 1
    }
    new_boot_id="$(ssh_guest "$ssh_port" cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
    if [[ -n "$new_boot_id" && "$new_boot_id" != "$old_boot_id" ]]; then
      return 0
    fi
    sleep 2
  done
  printf '[iso-test] timeout waiting for %s guest reboot\n' "$profile" >&2
  return 1
}

verify_headless() {
  local ssh_port="$1"
  ssh_guest "$ssh_port" bash -s <<'EOF'
set -Eeuo pipefail
test -f /var/lib/fxroute-iso/install-complete
test -x /usr/local/libexec/fxroute-first-boot-install.sh
systemctl is-enabled fxroute-first-boot.service
systemctl is-active fxroute-first-boot.service
test "$(systemctl get-default)" = multi-user.target
! rpm -q plasma6-session >/dev/null 2>&1
rpm -q openssh-server >/dev/null
test -d /home/fxroute/fxroute/.git
test "$(runuser -u fxroute -- git -C /home/fxroute/fxroute remote get-url origin)" = 'https://github.com/CobbyCode/fxroute.git'
runuser -u fxroute -- env HOME=/home/fxroute bash -lc 'cd /home/fxroute/fxroute && scripts/update_fxroute.sh --check >/tmp/fxroute-update-check.log 2>&1'
grep -Eqi 'already up to date|update available|reconciliation is incomplete' /tmp/fxroute-update-check.log
test -x /home/fxroute/fxroute/native_dsp/build/fxroute-dsp
systemctl --user --machine=fxroute@ is-enabled fxroute.service
systemctl --user --machine=fxroute@ is-active fxroute.service
runuser -u fxroute -- env XDG_RUNTIME_DIR=/run/user/$(id -u fxroute) pactl list sinks short | awk '{print $2}' | grep -Fx 'fxroute_dsp_sink' >/dev/null
curl --fail --silent http://127.0.0.1:8000/api/status >/dev/null
EOF
}

verify_desktop() {
  local ssh_port="$1"
  ssh_guest "$ssh_port" bash -s <<'EOF'
set -Eeuo pipefail
test -f /var/lib/fxroute-iso/install-complete
test -x /usr/local/libexec/fxroute-first-boot-install.sh
systemctl is-enabled fxroute-first-boot.service
systemctl is-active fxroute-first-boot.service
test "$(systemctl get-default)" = graphical.target
rpm -q plasma6-session sddm-qt6 google-chrome-stable >/dev/null
grep -Fxq 'gpgkey=https://dl.google.com/linux/linux_signing_key.pub' /etc/zypp/repos.d/google-chrome.repo
test -f /etc/sddm.conf.d/10-fxroute-autologin.conf
grep -Fxq 'User=fxroute' /etc/sddm.conf.d/10-fxroute-autologin.conf
grep -Fxq 'Session=plasmawayland' /etc/sddm.conf.d/10-fxroute-autologin.conf
systemctl is-enabled display-manager.service
systemctl is-enabled sddm.service
test "$(readlink -f /etc/systemd/system/display-manager.service)" = /usr/lib/systemd/system/sddm.service
for _ in $(seq 1 90); do
  if systemctl is-active --quiet sddm.service; then
    session_id=""
    while read -r candidate; do
      [[ -n "$candidate" ]] || continue
      if [[ "$(loginctl show-session "$candidate" -p Type --value)" = wayland ]]; then
        session_id="$candidate"
        break
      fi
    done < <(loginctl list-sessions --no-legend | awk '$3 == "fxroute" {print $1}')
    if [[ -n "$session_id" ]]; then
      break
    fi
  fi
  sleep 2
done
systemctl is-active sddm.service
session_id=""
while read -r candidate; do
  [[ -n "$candidate" ]] || continue
  if [[ "$(loginctl show-session "$candidate" -p Type --value)" = wayland ]]; then
    session_id="$candidate"
    break
  fi
done < <(loginctl list-sessions --no-legend | awk '$3 == "fxroute" {print $1}')
test -n "$session_id"
test "$(loginctl show-session "$session_id" -p Type --value)" = wayland
test -x /usr/local/bin/fxroute-desktop-launcher
test -f /home/fxroute/.config/autostart/fxroute.desktop
! grep -Fq -- '--kiosk' /usr/local/bin/fxroute-desktop-launcher /home/fxroute/.config/autostart/fxroute.desktop
for _ in $(seq 1 60); do
  if pgrep -u fxroute -f '(^|/)chrome( |$)' >/dev/null &&
     pgrep -u fxroute -f '127\.0\.0\.1:8000' >/dev/null; then
    break
  fi
  sleep 2
done
pgrep -u fxroute -f '(^|/)chrome( |$)' >/dev/null
pgrep -u fxroute -f '127\.0\.0\.1:8000' >/dev/null
systemctl --user --machine=fxroute@ is-active fxroute.service
runuser -u fxroute -- env XDG_RUNTIME_DIR=/run/user/$(id -u fxroute) pactl list sinks short | awk '{print $2}' | grep -Fx 'fxroute_dsp_sink' >/dev/null
curl --fail --silent http://127.0.0.1:8000/api/status >/dev/null
test -d /home/fxroute/fxroute/.git
test "$(runuser -u fxroute -- git -C /home/fxroute/fxroute remote get-url origin)" = 'https://github.com/CobbyCode/fxroute.git'
runuser -u fxroute -- env HOME=/home/fxroute bash -lc 'cd /home/fxroute/fxroute && scripts/update_fxroute.sh --check >/tmp/fxroute-update-check.log 2>&1'
grep -Eqi 'already up to date|update available|reconciliation is incomplete' /tmp/fxroute-update-check.log
EOF
}

for profile in "${PROFILES[@]}"; do
  http_port="$(port_for_profile "$profile")"
  ssh_port="$(ssh_port_for_profile "$profile")"
  while [[ "$ssh_port" == "$http_port" ]]; do
    ssh_port="$(ssh_port_for_profile "$profile")"
  done
  disk="$TEST_ROOT/$profile.qcow2"
  serial_log="$TEST_ROOT/$profile-serial.log"
  qemu_log="$TEST_ROOT/$profile-qemu.log"
  pid_file="$TEST_ROOT/$profile.pid"
  monitor="$TEST_ROOT/$profile-monitor.sock"

  printf '[iso-test] creating fresh %s disk\n' "$profile"
  qemu-img create -f qcow2 "$disk" "${VM_DISK_GB}G" >/dev/null
  qemu-system-x86_64 \
    -name "fxroute-iso-$profile" \
    -accel kvm \
    -cpu host \
    -smp "$VM_CPUS" \
    -m "${VM_RAM}M" \
    -drive "file=$disk,format=qcow2,if=virtio" \
    -cdrom "$ISO_PATH" \
    -boot once=d,menu=off \
    -nic "user,model=virtio-net-pci,hostfwd=tcp::$http_port-:8000,hostfwd=tcp::$ssh_port-:22" \
    -monitor "unix:$monitor,server=on,wait=off" \
    -device virtio-rng-pci \
    -audiodev driver=none,id=fxroute-audio \
    -device ich9-intel-hda \
    -device hda-duplex,audiodev=fxroute-audio \
    -vga virtio \
    -display none \
    -serial "file:$serial_log" \
    -D "$qemu_log" \
    -pidfile "$pid_file" \
    -daemonize
  pid="$(<"$pid_file")"
  PIDS+=("$pid")
  select_boot_entry "$profile" "$monitor"

  wait_for_guest "$profile" "$pid" "$http_port" "$ssh_port"
  verify_ssh_hardening "$ssh_port"
  case "$profile" in
    headless) verify_headless "$ssh_port" ;;
    desktop)
      reboot_guest "$profile" "$pid" "$ssh_port"
      wait_for_guest "$profile" "$pid" "$http_port" "$ssh_port"
      verify_desktop "$ssh_port"
      ;;
  esac
  printf '[iso-test] %s profile passed\n' "$profile"
  stop_guest "$profile" "$pid" "$ssh_port"
done
