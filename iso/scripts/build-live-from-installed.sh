#!/usr/bin/env bash
# FXRoute live-from-installed converter.
#
# Builds /LiveFX/squashfs.img from a REAL working Agama desktop
# installation (qcow2) instead of reimplementing the install. Only true
# live semantics are applied on top: volatile identity, no credentials,
# neutral fstab, ISO-kernel modules, live marker, internal-disk
# protection. Everything else (packages, PipeWire, desktop, helpers,
# DSP) comes from the installed system untouched.
#
# Ownership rule: the extracted tree contains root-owned files, so every
# tree mutation runs as root inside the container. The host only
# orchestrates (QEMU, SSH pipe, RPM extraction from ISO).
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
UDEV_RULE_SRC="$ROOT_DIR/iso/scripts/live-udev-nomount.rules"
LIVE_INIT_SRC="$ROOT_DIR/iso/scripts/fxroute-live-init.sh"

# Drop disk-backed fstab entries (UUID/LABEL//dev); the live system boots
# from squashfs+overlay and must never wait on install-disk partitions.
# Pseudo-filesystem lines (tmpfs, devpts, sysfs, proc) pass through.
filter_fstab() {
  awk '$1 ~ /^(UUID=|LABEL=|\/dev\/)/ {next} {print}'
}

# Scrub identity and secrets from the extracted tree. Everything here is
# true live semantics: no credential or machine identity may ship.
# Runs as root (container) in production; fixture tests run it as the
# invoking user on user-owned trees.
scrub_tree() {
  local tree="$LIVE_TREE"
  rm -f "$tree/etc/ssh"/ssh_host_*
  rm -f "$tree/etc/NetworkManager"/system-connections/*
  rm -rf "$tree/var/log"/* "$tree/var/tmp"/* "$tree/var/cache/zypp"
  rm -f "$tree/root/.bash_history" "$tree/home"/*/.bash_history
  rm -rf "$tree/home/fxroute/.cache"
  # User password must not ship: empty hash field (same as passwd -d).
  if grep -q '^fxroute:' "$tree/etc/shadow" 2>/dev/null; then
    sed -i 's/^fxroute:[^:]*:/fxroute::/' "$tree/etc/shadow"
  fi
  : > "$tree/etc/machine-id"
  printf 'fxroute-live\n' > "$tree/etc/hostname"
  printf 'fxroute-live\n' > "$tree/etc/fxroute-live"
  printf '%s\n' "$BUILD_COMMIT" > "$tree/etc/fxroute-live-commit"
  # Installed sshd/Caddy must not start live (no credentials, stale identity).
  rm -f "$tree/etc/systemd/system/multi-user.target.wants"/sshd.service \
    "$tree/etc/systemd/system/multi-user.target.wants"/caddy.service
  rm -rf "$tree/var/lib/caddy"
  # First boot already ran; it must never rerun (or reinstall) live.
  ln -sf /dev/null "$tree/etc/systemd/system/fxroute-first-boot.service"
  filter_fstab < "$tree/etc/fstab" > "$tree/etc/fstab.live"
  mv "$tree/etc/fstab.live" "$tree/etc/fstab"
  # Live sudo without credentials (helpers call sudo -n).
  mkdir -p "$tree/etc/sudoers.d"
  printf 'fxroute ALL=(ALL) NOPASSWD:ALL\n' > "$tree/etc/sudoers.d/99-fxroute-live"
  chmod 440 "$tree/etc/sudoers.d/99-fxroute-live"
  # Internal ATA/NVMe must not auto-mount live.
  mkdir -p "$tree/etc/udev/rules.d"
  cp -- "$UDEV_RULE_SRC" "$tree/etc/udev/rules.d/99-fxroute-live-nomount.rules"
  chmod 644 "$tree/etc/udev/rules.d/99-fxroute-live-nomount.rules"
  # Slim live boot init (test SSH hook, unmute, mpv pre-warm, README).
  mkdir -p "$tree/usr/local/libexec"
  cp -- "$LIVE_INIT_SRC" "$tree/usr/local/libexec/fxroute-live-init.sh"
  chmod 755 "$tree/usr/local/libexec/fxroute-live-init.sh"
  cat > "$tree/etc/systemd/system/fxroute-live-init.service" <<'UNIT_EOF'
[Unit]
Description=FXRoute live session init (volatile only)
After=network-online.target
Wants=network-online.target
Before=display-manager.service
ConditionPathExists=/etc/fxroute-live

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/fxroute-live-init.sh
RemainAfterExit=yes
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
UNIT_EOF
  mkdir -p "$tree/etc/systemd/system/multi-user.target.wants"
  ln -sf ../fxroute-live-init.service "$tree/etc/systemd/system/multi-user.target.wants/fxroute-live-init.service"
  # Pseudo-fs mountpoints for the flat-squash contract.
  mkdir -p "$tree/proc" "$tree/sys" "$tree/dev" "$tree/run" "$tree/tmp"
  chmod 1777 "$tree/tmp"
  # Live notice on the desktop next to the installed links.
  if [[ -d "$tree/home/fxroute/Desktop" ]]; then
    cat > "$tree/home/fxroute/Desktop/LIVE-MODE-README.txt" <<'README_EOF'
FXRoute Live Mode — changes and logins are not saved and will be lost after reboot.

Try FXRoute directly from this USB/ISO medium. Network, audio, DSP,
measurements and providers work in this session, but nothing is stored.
Internal drives are not automatically mounted or changed.
README_EOF
    chmod 644 "$tree/home/fxroute/Desktop/LIVE-MODE-README.txt"
  fi
}

# Sourced by fixture tests as: source <this> --source-only
if [[ "${1:-}" == "--source-only" ]]; then
  return 0 2>/dev/null || exit 0
fi

usage() {
  cat <<EOF
Usage: $0 --disk QCOW2 --base-iso ISO --output PATH [options]

Convert a working FXRoute desktop installation into a live squashfs.

Options:
  --disk PATH          Installed system disk image (qcow2, powered off)
  --base-iso PATH      Leap installer ISO (provides the booted kernel RPMs)
  --output PATH        Write squashfs image to PATH (required)
  --ssh-user NAME      Installed account for extraction (default: fxroute)
  --ssh-password PASS  Account password (for sudo during extraction;
                       or FXROUTE_LIVE_CONVERT_PASSWORD)
  --commit REV         Build commit marker (default: HEAD)
  --docker-image REF   Leap container image (default: Leap 16.0)
  --work-dir DIR       Work directory (default: mktemp)
  -h, --help           Show this help
EOF
}

die() {
  printf '[live-convert][error] %s\n' "$*" >&2
  exit 1
}

DISK=""
BASE_ISO=""
OUTPUT=""
SSH_USER="fxroute"
SSH_PASSWORD="${FXROUTE_LIVE_CONVERT_PASSWORD:-}"
BUILD_COMMIT=""
DOCKER_IMAGE="${FXROUTE_LIVE_DOCKER_IMAGE:-registry.opensuse.org/opensuse/leap:16.0}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
LIVE_EPOCH="$SOURCE_DATE_EPOCH"
if [[ "$LIVE_EPOCH" -lt 86400 ]]; then
  # Epoch-zero files are treated as unmodified by SDDM's config loader and
  # sysusers uses epoch day zero (expired). Floor at day one; the ISO
  # metadata still uses the requested epoch.
  LIVE_EPOCH=86400
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --disk) DISK="$2"; shift 2 ;;
    --base-iso) BASE_ISO="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --ssh-password) SSH_PASSWORD="$2"; shift 2 ;;
    --commit) BUILD_COMMIT="$2"; shift 2 ;;
    --docker-image) DOCKER_IMAGE="$2"; shift 2 ;;
    --work-dir) WORK_DIR_OVERRIDE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "$OUTPUT" ]] || die "--output is required"
[[ -f "${DISK:-}" ]] || die "installed disk not found: ${DISK:-}"
[[ -f "${BASE_ISO:-}" ]] || die "base ISO not found: ${BASE_ISO:-}"
[[ -n "$SSH_PASSWORD" ]] || die "--ssh-password is required for extraction"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH must be a non-negative integer"
command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 is required"
command -v ssh >/dev/null 2>&1 || die "ssh is required"
command -v docker >/dev/null 2>&1 || die "docker is required"
command -v isoinfo >/dev/null 2>&1 || die "isoinfo is required"
[[ -f "$UDEV_RULE_SRC" ]] || die "udev rule is missing: $UDEV_RULE_SRC"
[[ -f "$LIVE_INIT_SRC" ]] || die "live init script is missing: $LIVE_INIT_SRC"

if [[ -z "$BUILD_COMMIT" ]]; then
  BUILD_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
  [[ -n "$BUILD_COMMIT" ]] || die "could not determine build commit"
fi

OUTPUT="$(realpath -m "$OUTPUT")"
mkdir -p "$(dirname "$OUTPUT")"
WORK_DIR="${WORK_DIR_OVERRIDE:-$(mktemp -d "${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/live-convert.XXXXXX")}"
mkdir -p "$WORK_DIR"
TREE="$WORK_DIR/tree"
# Stale trees contain root-owned files; remove with container privileges.
if [[ -e "$TREE" ]]; then
  rm -rf -- "$TREE" 2>/dev/null || \
    docker run --rm -v "$WORK_DIR:/w:z" "$DOCKER_IMAGE" rm -rf /w/tree >/dev/null 2>&1 || true
fi
mkdir -p "$TREE"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

SSH_PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()')"
MONITOR="$WORK_DIR/monitor.sock"
PIDFILE="$WORK_DIR/qemu.pid"

ssh_run() {
  SSH_ASKPASS="$ROOT_DIR/iso/agama-askpass.sh" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$SSH_PASSWORD" setsid ssh \
    -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -p "$SSH_PORT" "$SSH_USER@127.0.0.1" "$@"
}

printf '[live-convert] extracting ISO kernel RPMs\n'
isoinfo -R -i "$BASE_ISO" -f > "$WORK_DIR/base-isol.txt" 2>/dev/null \
  || die "could not list base ISO contents"
ISO_KVER=""
for rpm_kind in kernel-default kernel-default-extra; do
  rpm_path="$(grep -E "/${rpm_kind}-[0-9][^/]*\\.x86_64\\.rpm\$" "$WORK_DIR/base-isol.txt" | head -n 1 || true)"
  [[ -n "$rpm_path" ]] || die "$rpm_kind RPM not found on base ISO"
  printf '[live-convert] staging RPM: %s\n' "$rpm_path"
  isoinfo -R -i "$BASE_ISO" -x "$rpm_path" > "$WORK_DIR/${rpm_kind}.rpm" \
    || die "could not extract $rpm_kind RPM"
  if [[ -z "$ISO_KVER" ]]; then
    rpm_file="$(basename "$rpm_path")"
    ISO_KVER="$(printf '%s' "$rpm_file" | sed -e 's/^kernel-default-//' -e 's/\.x86_64\.rpm$//' -e 's/\.[0-9][0-9]*$//')-default"
  fi
done
[[ -n "$ISO_KVER" ]] || die "could not determine ISO kernel version"

printf '[live-convert] booting installed disk for extraction\n'
qemu-system-x86_64 \
  -name fxroute-live-extract -accel kvm -cpu host -smp 4 -m 4096M \
  -drive "file=$DISK,format=qcow2,if=virtio" \
  -boot c,menu=off \
  -nic "user,model=virtio-net-pci,hostfwd=tcp::$SSH_PORT-:22" \
  -monitor "unix:$MONITOR,server=on,wait=off" \
  -device virtio-rng-pci -vga none -display none \
  -serial "file:$WORK_DIR/serial.log" \
  -pidfile "$PIDFILE" -daemonize
GUEST_PID="$(cat "$PIDFILE")"
cleanup_guest() { kill "$GUEST_PID" 2>/dev/null || true; }
trap cleanup_guest EXIT

printf '[live-convert] waiting for SSH\n'
for _ in $(seq 1 120); do
  ssh_run true >/dev/null 2>&1 && break
  kill -0 "$GUEST_PID" 2>/dev/null || die "guest exited while waiting for SSH"
  sleep 5
done
ssh_run true >/dev/null 2>&1 || die "SSH to installed guest failed"

# Extract, scrub, re-kernel, firmware-install and pack ALL as root in the
# container (the tree contains root-owned files throughout). The SSH tar
# stream feeds container stdin; outer SSH authenticates via askpass while
# the piped account password feeds remote sudo -S.
printf '[live-convert] extracting and converting inside container\n'
printf '%s\n' "$SSH_PASSWORD" | SSH_ASKPASS="$ROOT_DIR/iso/agama-askpass.sh" SSH_ASKPASS_REQUIRE=force DISPLAY=:0 \
  FXROUTE_ASKPASS_PASSWORD="$SSH_PASSWORD" setsid ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
  -o ConnectTimeout=8 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  -p "$SSH_PORT" "$SSH_USER@127.0.0.1" \
  "sudo -S tar -c --one-file-system --numeric-owner --same-permissions \
    --exclude=./proc/* --exclude=./sys/* --exclude=./dev/* \
    --exclude=./run/* --exclude=./tmp/* --exclude=./mnt/* \
    --exclude=./media/* --exclude=./var/tmp/* --directory=/ ." \
| docker run --rm -i \
  -v "$TREE:/t:z" \
  -v "$WORK_DIR/kernel-default.rpm:/tmp/k-default.rpm:ro,z" \
  -v "$WORK_DIR/kernel-default-extra.rpm:/tmp/k-extra.rpm:ro,z" \
  -v "$UDEV_RULE_SRC:/tmp/live-udev.rules:ro,z" \
  -v "$LIVE_INIT_SRC:/tmp/live-init.sh:ro,z" \
  -e ISO_KVER="$ISO_KVER" \
  -e BUILD_COMMIT="$BUILD_COMMIT" \
  -e LIVE_EPOCH="$LIVE_EPOCH" \
  "$DOCKER_IMAGE" bash -c '
    set -Eeuo pipefail
    command -v cpio >/dev/null 2>&1 || zypper --non-interactive install --no-recommends cpio >/dev/null 2>&1
    tar -x -C /t --numeric-owner
    [[ -f /t/etc/os-release ]] || { echo "[live-convert][error] extraction failed" >&2; exit 1; }
    export LIVE_TREE=/t UDEV_RULE_SRC=/tmp/live-udev.rules LIVE_INIT_SRC=/tmp/live-init.sh
    '"$(declare -f filter_fstab scrub_tree)"'
    scrub_tree
    for rpm in /tmp/k-default.rpm /tmp/k-extra.rpm; do
      name="$(rpm -qp --queryformat "%{NAME}" "$rpm")"
      case "$name" in kernel-default|kernel-default-extra) ;; *) echo "[live-convert][error] unexpected RPM: $name" >&2; exit 1;; esac
      (cd /t && rpm2cpio "$rpm" | cpio -idm --quiet "./usr/lib/modules/*" "./boot/*")
    done
    [[ -d "/t/usr/lib/modules/$ISO_KVER" ]] || { echo "[live-convert][error] ISO modules missing" >&2; exit 1; }
    for moddir in /t/usr/lib/modules/*; do
      [[ "$(basename "$moddir")" == "$ISO_KVER" ]] || rm -rf -- "$moddir"
    done
    rm -rf -- /t/boot
    mkdir -p /t/boot
    zypper --non-interactive --root /t refresh >/dev/null 2>&1 || true
    zypper --non-interactive install --no-recommends cpio kmod >/dev/null 2>&1 || true
    zypper --non-interactive --root /t install --no-recommends \
      kernel-firmware-iwlwifi kernel-firmware-ath10k kernel-firmware-ath11k \
      kernel-firmware-ath12k kernel-firmware-atheros kernel-firmware-brcm \
      kernel-firmware-mediatek kernel-firmware-realtek kernel-firmware-marvell \
      wireless-regdb sof-firmware kernel-firmware-sound kernel-firmware-intel \
      kernel-firmware-bluetooth
    depmod -a -b /t "$ISO_KVER"
    for mod in kernel/drivers/hid/usbhid/usbhid.ko kernel/drivers/net/wireless/intel/iwlwifi/iwlwifi.ko; do
      compgen -G "/t/usr/lib/modules/$ISO_KVER/$mod*" > /dev/null \
        || { echo "[live-convert][error] driver missing: $mod" >&2; exit 1; }
    done
  ' || die "extract/convert step failed"

printf '[live-convert] powering off guest\n'
printf '%s\n' "$SSH_PASSWORD" | ssh_run sudo -S -- systemctl poweroff >/dev/null 2>&1 || true
for _ in $(seq 1 30); do kill -0 "$GUEST_PID" 2>/dev/null || break; sleep 1; done
kill "$GUEST_PID" 2>/dev/null || true
trap - EXIT

printf '[live-convert] packing flat squash\n'
docker run --rm \
  -v "$TREE:/t:ro,z" \
  -v "$WORK_DIR:/o:z" \
  -e LIVE_EPOCH="$LIVE_EPOCH" \
  "$DOCKER_IMAGE" bash -c '
    set -Eeuo pipefail
    command -v mksquashfs >/dev/null 2>&1 || zypper --non-interactive install --no-recommends squashfs >/dev/null 2>&1
    env -u SOURCE_DATE_EPOCH mksquashfs /t /o/squashfs.img -comp xz -noappend \
      -mkfs-time "$LIVE_EPOCH" -all-time "$LIVE_EPOCH" -no-xattrs -no-progress >/dev/null
  ' || die "mksquashfs failed"
docker run --rm -v "$WORK_DIR:/o:z" "$DOCKER_IMAGE" chown "$HOST_UID:$HOST_GID" /o/squashfs.img >/dev/null 2>&1 || true
mv -- "$WORK_DIR/squashfs.img" "$OUTPUT"
printf '[live-convert] wrote %s\n' "$OUTPUT"
