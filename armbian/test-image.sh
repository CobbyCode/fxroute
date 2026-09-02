#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

MACHINE="raspi4b"
FXROUTE_USER="fxroute"
SSH_KEY_FILE="${FXROUTE_ARMBIAN_SSH_KEY:-}"
SETUP_PASSWORD="${FXROUTE_ARMBIAN_SETUP_PASSWORD:-${FXROUTE_WIFI_SETUP_PASSWORD:-}}"
TIMEOUT_SECONDS="${FXROUTE_ARMBIAN_TEST_TIMEOUT:-7200}"
KEEP_WORK=0
IMAGE=""

usage() {
  cat <<EOF
Usage: $0 [options] IMAGE

Boot an Armbian image under QEMU and verify the common ARM64 FXRoute path.

Options:
  --machine <name>       raspi4b (default) or virt
  --user <name>          End-user account to create in the QEMU check (default: $FXROUTE_USER)
  --ssh-key-file <path>  Private key to use for the automated onboarding
  --setup-password <pass> Temporary image setup password for the automated onboarding
  --timeout <seconds>    Guest readiness timeout (default: $TIMEOUT_SECONDS)
  --keep-work            Keep serial and QEMU logs
  -h, --help             Show this help

QEMU does not emulate a Raspberry Pi 5. Use --machine raspi4b to verify the
shared Pi 4/Pi 5 kernel and rootfs reach basic.target; QEMU's Pi 4 machine has
no usable network device for API validation. Pi 5 boot, storage, Ethernet,
USB, thermal, and audio hardware still require a physical board test.

The virt machine expects a generic UEFI arm64 Armbian image, such as the
uefi-arm64 board output. Override the host AAVMF files with
FXROUTE_ARMBIAN_UEFI_CODE and FXROUTE_ARMBIAN_UEFI_VARS_TEMPLATE when needed.
EOF
}

die() {
  printf '[armbian-qemu][error] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --machine)
      [[ $# -ge 2 ]] || die "--machine requires a value"
      MACHINE="$2"
      shift 2
      ;;
    --user)
      [[ $# -ge 2 ]] || die "--user requires a value"
      FXROUTE_USER="$2"
      shift 2
      ;;
    --ssh-key-file)
      [[ $# -ge 2 ]] || die "--ssh-key-file requires a path"
      SSH_KEY_FILE="$2"
      shift 2
      ;;
    --setup-password)
      [[ $# -ge 2 ]] || die "--setup-password requires a value"
      SETUP_PASSWORD="$2"
      shift 2
      ;;
    --timeout)
      [[ $# -ge 2 ]] || die "--timeout requires a value"
      TIMEOUT_SECONDS="$2"
      shift 2
      ;;
    --keep-work)
      KEEP_WORK=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      die "Unknown argument: $1"
      ;;
    *)
      [[ -z "$IMAGE" ]] || die "Only one image may be supplied"
      IMAGE="$1"
      shift
      ;;
  esac
done

[[ -n "$IMAGE" && -f "$IMAGE" ]] || die "An Armbian image path is required"
[[ "$MACHINE" == "raspi4b" || "$MACHINE" == "virt" ]] \
  || {
    case "$MACHINE" in
      rpi5|rpi5b|raspi5)
        die "QEMU does not emulate Pi 5; physical hardware validation is required"
        ;;
      *)
        die "Unsupported QEMU machine: $MACHINE"
        ;;
    esac
  }
[[ "$FXROUTE_USER" =~ ^[a-z_][a-z0-9_.-]{0,31}$ && "$FXROUTE_USER" != root ]] \
  || die "Invalid FXRoute user: $FXROUTE_USER"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "Timeout must be a positive integer"
command -v qemu-system-aarch64 >/dev/null 2>&1 || die "qemu-system-aarch64 is required"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img is required"
if [[ "$MACHINE" == "raspi4b" ]]; then
  command -v mcopy >/dev/null 2>&1 || die "mcopy is required for raspi4b"
else
  command -v curl >/dev/null 2>&1 || die "curl is required for virt"
  command -v ssh >/dev/null 2>&1 || die "ssh is required for virt"
  command -v python3 >/dev/null 2>&1 || die "python3 is required for virt"
  command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is required for virt"
  command -v head >/dev/null 2>&1 || die "head is required for virt"
  command -v tr >/dev/null 2>&1 || die "tr is required for virt"
fi

UEFI_CODE="${FXROUTE_ARMBIAN_UEFI_CODE:-/usr/share/qemu/aavmf-aarch64-code.bin}"
UEFI_VARS_TEMPLATE="${FXROUTE_ARMBIAN_UEFI_VARS_TEMPLATE:-/usr/share/qemu/aavmf-aarch64-vars.bin}"
if [[ "$MACHINE" == "virt" ]]; then
  [[ -f "$UEFI_CODE" ]] || die "AAVMF code firmware is required: $UEFI_CODE"
  [[ -f "$UEFI_VARS_TEMPLATE" ]] || die "AAVMF variable template is required: $UEFI_VARS_TEMPLATE"
fi

IMAGE="$(realpath -m "$IMAGE")"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/fxroute-armbian-qemu.XXXXXX")"
SERIAL_LOG="$WORK_DIR/serial.log"
QEMU_LOG="$WORK_DIR/qemu.log"
STATUS_JSON="$WORK_DIR/status.json"
DISK_IMAGE="$WORK_DIR/image.qcow2"
BOOT_PARTITION_OFFSET="4M"
KERNEL_IMAGE="$WORK_DIR/vmlinuz"
DTB_IMAGE="$WORK_DIR/bcm2711-rpi-4-b.dtb"
INITRD_IMAGE="$WORK_DIR/initrd.img"
CMDLINE_FILE="$WORK_DIR/cmdline.txt"
UEFI_VARS="$WORK_DIR/aavmf-vars.bin"
SETUP_PAGE="$WORK_DIR/setup.html"
qemu_pid=""

cleanup() {
  local status=$?
  if [[ -n "$qemu_pid" ]] && kill -0 "$qemu_pid" >/dev/null 2>&1; then
    kill "$qemu_pid" >/dev/null 2>&1 || true
    wait "$qemu_pid" >/dev/null 2>&1 || true
  fi
  if [[ "$KEEP_WORK" -eq 1 ]]; then
    printf '[armbian-qemu] keeping logs: %s\n' "$WORK_DIR"
  else
    rm -rf -- "$WORK_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT

qemu-img create -q -f qcow2 -F raw -b "$IMAGE" "$DISK_IMAGE"

find_free_port() {
  local start="$1"
  python3 - "$start" <<'PY'
import socket
import sys

start = int(sys.argv[1])
for port in range(start, start + 100):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit("no free host port")
PY
}

netdev_args=()
if [[ "$MACHINE" == "raspi4b" ]]; then
  mcopy -i "$IMAGE@@$BOOT_PARTITION_OFFSET" "::vmlinuz" "$KERNEL_IMAGE"
  mcopy -i "$IMAGE@@$BOOT_PARTITION_OFFSET" "::initrd.img" "$INITRD_IMAGE"
  mcopy -i "$IMAGE@@$BOOT_PARTITION_OFFSET" "::cmdline.txt" "$CMDLINE_FILE"
  KERNEL_CMDLINE="$(tr -d '\r\n' <"$CMDLINE_FILE")"
  mcopy -i "$IMAGE@@$BOOT_PARTITION_OFFSET" "::bcm2711-rpi-4-b.dtb" "$DTB_IMAGE"
  KERNEL_CMDLINE="${KERNEL_CMDLINE//console=serial0/console=ttyAMA1}"
  KERNEL_CMDLINE="${KERNEL_CMDLINE// console=tty1/}"
  KERNEL_CMDLINE="earlycon=pl011,mmio32,0xfe201000 $KERNEL_CMDLINE"
  machine_args=(
    -M "raspi4b,usb=on"
    -kernel "$KERNEL_IMAGE"
    -dtb "$DTB_IMAGE"
    -initrd "$INITRD_IMAGE"
    -append "$KERNEL_CMDLINE"
    -drive "file=$DISK_IMAGE,format=qcow2,if=sd"
  )
else
  ssh_port="$(find_free_port 22000)"
  setup_port="$(find_free_port 27000)"
  http_port="$(find_free_port 28000)"
  netdev_args=(
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${ssh_port}-:22,hostfwd=tcp:127.0.0.1:${setup_port}-:443,hostfwd=tcp:127.0.0.1:${http_port}-:8000"
  )
  cp -- "$UEFI_VARS_TEMPLATE" "$UEFI_VARS"
  machine_args=(
    -M virt
    -drive "if=pflash,format=raw,readonly=on,file=$UEFI_CODE"
    -drive "if=pflash,format=raw,file=$UEFI_VARS"
    -drive "file=$DISK_IMAGE,format=qcow2,if=none,id=virtio-disk"
    -device "virtio-blk-pci,drive=virtio-disk"
    -device "virtio-net-device,netdev=net0"
    -device virtio-rng-device
  )
  printf '%s\n' "[armbian-qemu] virt is a generic ARM64 probe; board boot is validated with raspi4b"
fi

if [[ "$MACHINE" == "virt" ]]; then
  if [[ -z "$SSH_KEY_FILE" ]]; then
    SSH_KEY_FILE="$WORK_DIR/test-ssh-key"
    ssh-keygen -q -t ed25519 -N "" -C "fxroute-qemu-test" -f "$SSH_KEY_FILE"
  else
    [[ -f "$SSH_KEY_FILE" ]] || die "SSH key file does not exist: $SSH_KEY_FILE"
  fi
  [[ "$SETUP_PASSWORD" =~ ^[A-Za-z0-9._-]{8,63}$ ]] \
    || die "virt requires --setup-password or FXROUTE_ARMBIAN_SETUP_PASSWORD"
  ssh_public_key="$(ssh-keygen -y -f "$SSH_KEY_FILE")"
  account_password=""
  while [[ ${#account_password} -lt 16 ]]; do
    account_password="$(head -c 64 /dev/urandom | tr -dc 'A-Za-z0-9')"
  done
  account_password="${account_password:0:16}"
fi

qemu=(
  qemu-system-aarch64
  "${machine_args[@]}"
  "${netdev_args[@]}"
  -cpu cortex-a72
  -m 2048
  -smp 4
  -nographic
  -monitor none
  -serial "file:$SERIAL_LOG"
  -snapshot
  -no-reboot
)

printf '[armbian-qemu] booting %s with %s\n' "$IMAGE" "$MACHINE"
"${qemu[@]}" >"$QEMU_LOG" 2>&1 &
qemu_pid=$!

if [[ "$MACHINE" == "raspi4b" ]]; then
  boot_deadline=$((SECONDS + TIMEOUT_SECONDS))
  booted=0
  while (( SECONDS < boot_deadline )); do
    if grep -aEq 'Reached target .*basic\.target' "$SERIAL_LOG" 2>/dev/null; then
      booted=1
      break
    fi
    if ! kill -0 "$qemu_pid" >/dev/null 2>&1; then
      printf '%s\n' "QEMU exited before the Pi reached basic.target" >&2
      tail -80 "$SERIAL_LOG" "$QEMU_LOG" >&2 2>/dev/null || true
      exit 1
    fi
    sleep 2
  done
  [[ "$booted" -eq 1 ]] || {
    printf '%s\n' "Timed out waiting for the Pi to reach basic.target" >&2
    tail -80 "$SERIAL_LOG" "$QEMU_LOG" >&2 2>/dev/null || true
    exit 1
  }
  printf '%s\n' "[armbian-qemu] Pi kernel and rootfs reached basic.target; network/API checks require hardware or generic virt"
  exit 0
fi

setup_deadline=$((SECONDS + TIMEOUT_SECONDS))
setup_ready=0
while (( SECONDS < setup_deadline )); do
  if curl --fail --silent --show-error --insecure --connect-timeout 2 --max-time 5 \
      "https://127.0.0.1:$setup_port/" > "$SETUP_PAGE"; then
    setup_ready=1
    break
  fi
  if ! kill -0 "$qemu_pid" >/dev/null 2>&1; then
    printf '%s\n' "QEMU exited before the first-boot setup page became ready" >&2
    tail -80 "$SERIAL_LOG" "$QEMU_LOG" >&2 2>/dev/null || true
    exit 1
  fi
  sleep 2
done
[[ "$setup_ready" -eq 1 ]] || {
  printf '%s\n' "Timed out waiting for the first-boot setup page" >&2
  tail -80 "$SERIAL_LOG" "$QEMU_LOG" >&2 2>/dev/null || true
  exit 1
}

curl --fail --silent --show-error --insecure --max-time 30 \
  --data-urlencode "username=$FXROUTE_USER" \
  --data-urlencode "account_password=$account_password" \
  --data-urlencode "account_password_confirm=$account_password" \
  --data-urlencode "ssh_key=$ssh_public_key" \
  --data-urlencode "setup_password=$SETUP_PASSWORD" \
  --data-urlencode "wifi_country=GB" \
  "https://127.0.0.1:$setup_port/setup" >/dev/null

deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
      "http://127.0.0.1:$http_port/api/status" > "$STATUS_JSON" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$qemu_pid" >/dev/null 2>&1; then
    printf '%s\n' "QEMU exited before FXRoute became ready" >&2
    tail -80 "$SERIAL_LOG" "$QEMU_LOG" >&2 2>/dev/null || true
    exit 1
  fi
  sleep 2
done

[[ -s "$STATUS_JSON" ]] || {
  printf '%s\n' "Timed out waiting for /api/status" >&2
  tail -80 "$SERIAL_LOG" "$QEMU_LOG" >&2 2>/dev/null || true
  exit 1
}
python3 - "$STATUS_JSON" <<'PY'
import json
import sys

value = json.loads(open(sys.argv[1], encoding="utf-8").read())
if not isinstance(value, dict):
    raise SystemExit("/api/status did not return a JSON object")
PY

ssh=(
  ssh
  -i "$SSH_KEY_FILE"
  -p "$ssh_port"
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o ConnectionAttempts=1
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  "$FXROUTE_USER@127.0.0.1"
)
ssh_deadline=$((SECONDS + 60))
while (( SECONDS < ssh_deadline )); do
  if "${ssh[@]}" true >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

"${ssh[@]}" sh -s <<'REMOTE_CHECK'
set -eu
test "$(uname -m)" = aarch64
test "$(systemctl --user is-active fxroute.service)" = active
test "$(systemctl --user is-active pipewire.service)" = active
test "$(systemctl --user is-active pipewire-pulse.service)" = active
test "$(systemctl --user is-active wireplumber.service)" = active
test -S "$XDG_RUNTIME_DIR/pipewire-0"
test -S "$XDG_RUNTIME_DIR/pulse/native"
pactl list sinks short | awk '{print $2}' | grep -Fxq fxroute_dsp_sink
REMOTE_CHECK

printf '%s\n' "[armbian-qemu] ARM64 user-session and FXRoute DSP ingress checks passed"
