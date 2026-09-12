#!/usr/bin/env bash
# FXRoute Live root builder (flat squashfs for dmsquash-live).
#
# Builds /LiveFX/squashfs.img as a FLAT squashfs: the squash root IS the
# live root filesystem (it contains /proc, /usr, /etc, ...). This matches
# the real dmsquash-live path in the Leap 16 initrd:
#   usr/sbin/dmsquash-live-root:
#     if [ -d /run/initramfs/squashfs/LiveOS ]; then FSIMG=.../rootfs.img
#     elif [ -d /run/initramfs/squashfs/proc ]; then FSIMG=$SQUASHED; overlayfs=required
# A nested outer squash with LiveOS/rootfs.img is the base ISO format and is
# NOT used here; flat is simpler (mksquashfs only, no mkfs.ext4) and supported.
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT=""
SOURCE_DIR="$ROOT_DIR"
BUILD_COMMIT=""
KERNEL_RPMS=()
DOCKER_IMAGE="${FXROUTE_LIVE_DOCKER_IMAGE:-registry.opensuse.org/opensuse/leap:16.0}"
MINIMAL=0
KEEP_WORK=0
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"

usage() {
  cat <<EOF
Usage: $0 --output PATH [options]

Build a flat LiveFX squashfs image (squash root = live rootfs).

Options:
  --output PATH        Write squashfs image to PATH (required)
  --source DIR         FXRoute source directory (default: repo root)
  --commit REV         FXRoute commit to bake in (default: HEAD)
  --kernel-rpm PATH    kernel RPM matching the ISO kernel; its modules are
                       installed so input/WLAN drivers can bind after
                       switch-root (repeatable: kernel-default plus
                       kernel-default-extra for ath11k/ath12k/mt76 WLAN
                       drivers; default: none, live drivers stay missing)
  --docker-image REF   Leap container image (default: $DOCKER_IMAGE)
  --minimal            Build a tiny placeholder image (GRUB/structure tests only,
                       not a bootable FXRoute desktop)
  --keep-work          Keep the temporary live-root directory
  -h, --help           Show this help
EOF
}

die() {
  printf '[live-root][error] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output)
      [[ $# -ge 2 ]] || die "--output requires a path"
      OUTPUT="$2"
      shift 2
      ;;
    --source)
      [[ $# -ge 2 ]] || die "--source requires a directory"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --commit)
      [[ $# -ge 2 ]] || die "--commit requires a revision"
      BUILD_COMMIT="$2"
      shift 2
      ;;
    --kernel-rpm)
      [[ $# -ge 2 ]] || die "--kernel-rpm requires a path"
      KERNEL_RPMS+=("$2")
      shift 2
      ;;
    --docker-image)
      [[ $# -ge 2 ]] || die "--docker-image requires a reference"
      DOCKER_IMAGE="$2"
      shift 2
      ;;
    --minimal)
      MINIMAL=1
      shift
      ;;
    --keep-work)
      KEEP_WORK=1
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

[[ -n "$OUTPUT" ]] || die "--output is required"
[[ -d "$SOURCE_DIR" ]] || die "source directory not found: $SOURCE_DIR"
[[ -f "$SOURCE_DIR/main.py" && -f "$SOURCE_DIR/requirements.txt" ]] \
  || die "source directory does not look like FXRoute: $SOURCE_DIR"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH must be a non-negative integer"
if [[ "${#KERNEL_RPMS[@]}" -gt 0 ]]; then
  for i in "${!KERNEL_RPMS[@]}"; do
    [[ -f "${KERNEL_RPMS[$i]}" ]] || die "kernel RPM not found: ${KERNEL_RPMS[$i]}"
    KERNEL_RPMS[$i]="$(realpath -m "${KERNEL_RPMS[$i]}")"
  done
  # The package identity (kernel-default/-extra) is verified inside the
  # container via rpm -qp; the staged file name is not significant.
fi
# Epoch-zero files are treated as unmodified by SDDM's config loader, which
# then falls back to /etc/X11/xdm/Xsession (absent on Leap 16). sysusers also
# uses this epoch for shadow's last-change DAY; day zero means expired.
# Keep reproducibility, but use at least day one for the bootable filesystem.
LIVE_EPOCH="$SOURCE_DATE_EPOCH"
if [[ "$LIVE_EPOCH" -lt 86400 ]]; then
  LIVE_EPOCH=86400
fi
command -v mksquashfs >/dev/null 2>&1 || die "mksquashfs is required"

if [[ -z "$BUILD_COMMIT" ]]; then
  BUILD_COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
  [[ -n "$BUILD_COMMIT" ]] || die "could not determine build commit"
fi

OUTPUT="$(realpath -m "$OUTPUT")"
mkdir -p "$(dirname "$OUTPUT")"

BUILD_TMP_BASE="${FXROUTE_LIVE_TMPDIR:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/live}"
mkdir -p "$BUILD_TMP_BASE"
WORK_DIR="$(mktemp -d "$BUILD_TMP_BASE/live-root.XXXXXX")"
LIVE_ROOT="$WORK_DIR/root"
CONTAINER_NAME="fxroute-live-build-$$"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"
cleanup() {
  if [[ "$KEEP_WORK" -eq 1 ]]; then
    printf '[live-root] keeping work directory: %s\n' "$WORK_DIR"
  else
    # Remove the exported root with container privileges; never change its
    # ownership (even --keep-work must retain the image's real uid/gid/modes).
    rm -rf -- "$WORK_DIR" 2>/dev/null || \
      docker run --rm -v "$BUILD_TMP_BASE:/w:z" "$DOCKER_IMAGE" \
        rm -rf "/w/$(basename "$WORK_DIR")" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ "$MINIMAL" -eq 1 ]]; then
  printf '[live-root] building minimal placeholder image\n'
  mkdir -p "$LIVE_ROOT/proc" "$LIVE_ROOT/sys" "$LIVE_ROOT/dev" \
    "$LIVE_ROOT/etc" "$LIVE_ROOT/boot"
  printf 'fxroute-live\n' > "$LIVE_ROOT/etc/hostname"
  printf 'fxroute-live placeholder (structure test only)\n' > "$LIVE_ROOT/etc/fxroute-live"
  printf '%s\n' "$BUILD_COMMIT" > "$LIVE_ROOT/etc/fxroute-live-commit"
  # Flat-format marker: squash root must contain /proc so dmsquash-live
  # takes the FSIMG=SQUASHED + overlayfs=required path.
  # NOTE: mksquashfs 4.6+ refuses explicit -mkfs-time/-all-time when
  # $SOURCE_DATE_EPOCH is exported, so unset it for the call.
  find "$LIVE_ROOT" -exec touch -h --date="@$SOURCE_DATE_EPOCH" {} + 2>/dev/null || true
  env -u SOURCE_DATE_EPOCH mksquashfs "$LIVE_ROOT" "$OUTPUT" -comp xz -noappend \
    -mkfs-time "$SOURCE_DATE_EPOCH" -all-time "$SOURCE_DATE_EPOCH" \
    -no-xattrs >/dev/null
  printf '[live-root] wrote %s\n' "$OUTPUT"
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "docker is required for the full live build (or use --minimal)"

printf '[live-root] building full FXRoute live root from %s @ %s\n' "$SOURCE_DIR" "$BUILD_COMMIT"
mkdir -p "$LIVE_ROOT"

# Source snapshot for the container (deterministic file list).
SOURCE_SNAP="$WORK_DIR/source.tar"
git -C "$ROOT_DIR" ls-files -z \
  | tar --directory="$ROOT_DIR" --create --file="$SOURCE_SNAP" \
      --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 \
      --numeric-owner --pax-option=delete=atime,delete=ctime \
      --no-recursion --null --verbatim-files-from \
      --files-from=-

SETUP_SCRIPT="$WORK_DIR/live-setup.sh"
cat > "$SETUP_SCRIPT" <<'SETUP_EOF'
set -Eeuo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export DEBIAN_FRONTEND=noninteractive

LIVE_USER="fxroute"
LIVE_HOME="/home/$LIVE_USER"
BUILD_COMMIT_FILE="/etc/fxroute-live-commit"

# Base desktop + FXRoute runtime packages (mirrors iso/profiles/desktop.jsonnet
# plus install.sh zypper core/audio/support/smb sets). Leap 16.0 OSS names:
# pattern kde_plasma via -t pattern, ffmpeg binary via ffmpeg-7, kwriteconfig6
# via kf6-kconfig, wallpaper tool via plasma6-workspace, squash tools via
# squashfs. Groups are best-effort (|| true); critical binaries verified below.
zypper --non-interactive refresh || true
zypper --non-interactive install --no-recommends -t pattern kde_plasma || true
zypper --non-interactive install --no-recommends \
  plasma6-session plasma6-session-x11 sddm-qt6 plasma6-pa plasma6-nm qt6-wayland \
  plasma6-workspace kf6-kconfig konsole \
  MozillaFirefox \
  python3 python313-pip tar ca-certificates iproute2 openssh-server git \
  curl socat mpv playerctl \
  bluez wireplumber pipewire-tools pipewire-pulseaudio pulseaudio-utils \
  pipewire-spa-plugins-0_2 rtkit \
  samba-client cifs-utils glib2-tools gvfs gvfs-backend-samba gvfs-fuse \
  dbus-1-tools avahi caddy firewalld sudo \
  alsa-utils xdg-user-dirs squashfs || true
# ffmpeg binary (Leap splits it as ffmpeg-7/ffmpeg-4, Tumbleweed as ffmpeg).
if ! command -v ffmpeg >/dev/null 2>&1; then
  zypper --non-interactive install --no-recommends ffmpeg-7 || true
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  zypper --non-interactive install --no-recommends ffmpeg-4 || true
fi
# DSP build toolchain (runtime libs stay, toolchain could be removed later).
zypper --non-interactive install --no-recommends \
  gcc gcc-c++ cmake pkgconf-pkg-config make \
  pipewire-devel lilv liblilv-0-devel lv2-devel lv2-lsp-plugins lv2-zam-plugins \
  libebur128-devel libsamplerate-devel speexdsp-devel libexpat-devel fluidsynth-devel || true
# Kernel modules matching the ISO kernel. The container image ships none
# and repo kernels no longer match the booted ISO kernel, so the exact
# kernel RPMs are injected via --kernel-rpm (kernel-default plus
# kernel-default-extra, which carries the ath11k/ath12k/mt76 WLAN drivers).
# Without these, pointer and WLAN drivers can never load after switch-root:
# USB mice/trackpads stay dead while the AT keyboard works, and Plasma
# shows no WLANs. Firmware comes from the distro packages below.
if compgen -G "/tmp/kernel-rpm-*.rpm" > /dev/null; then
  command -v depmod >/dev/null 2>&1 || zypper --non-interactive install --no-recommends kmod || true
  for kernel_rpm in /tmp/kernel-rpm-*.rpm; do
    RPM_NAME="$(rpm -qp --queryformat '%{NAME}' "$kernel_rpm" 2>/dev/null || true)"
    case "$RPM_NAME" in
      kernel-default|kernel-default-extra) ;;
      *) echo "[live-root][error] expected a kernel-default RPM, got '${RPM_NAME:-unknown}' ($kernel_rpm)" >&2; exit 1 ;;
    esac
    rpm -i --nodeps --noscripts "$kernel_rpm"
  done
  KVER=""
  for d in /usr/lib/modules/*-default; do
    if [[ -d "$d" ]]; then KVER="$(basename "$d")"; break; fi
  done
  [[ -n "$KVER" ]] || { echo "[live-root][error] no kernel modules after kernel RPM install" >&2; exit 1; }
  depmod -a "$KVER"
  # Module files may be compressed (.ko.zst on Leap 16); match any suffix.
  # psmouse is intentionally absent: SUSE builds it into the kernel.
  for mod in kernel/drivers/hid/usbhid/usbhid.ko kernel/drivers/hid/i2c-hid/i2c-hid.ko kernel/drivers/hid/hid-multitouch.ko kernel/drivers/net/wireless/intel/iwlwifi/iwlwifi.ko kernel/drivers/net/wireless/ath/ath11k/ath11k.ko kernel/drivers/net/wireless/mediatek/mt76/mt7921/mt7921e.ko; do
    compgen -G "/usr/lib/modules/$KVER/$mod*" > /dev/null || { echo "[live-root][error] driver module missing: $mod" >&2; exit 1; }
  done
fi
# WLAN firmware for the usual notebook adapters (version-independent, hence
# from the repos) plus the regulatory database. Without these the drivers
# above probe but never associate, and Plasma lists no WLANs.
# Speaker firmware for notebook audio (Intel SOF, amp/DSP firmware, legacy
# Intel SST, Bluetooth): without it the sound card never appears and radio
# playback stays silent.
zypper --non-interactive install --no-recommends \
  kernel-firmware-iwlwifi kernel-firmware-ath10k kernel-firmware-ath11k \
  kernel-firmware-ath12k kernel-firmware-atheros kernel-firmware-brcm \
  kernel-firmware-mediatek kernel-firmware-realtek kernel-firmware-marvell \
  wireless-regdb sof-firmware kernel-firmware-sound kernel-firmware-intel \
  kernel-firmware-bluetooth || true
for cmd in python3 git mpv playerctl wpctl pactl firefox sddm; do
  command -v "$cmd" >/dev/null 2>&1 || echo "[live-root][warn] expected command missing after package install: $cmd"
done
command -v python3 >/dev/null 2>&1 || { echo "[live-root][error] python3 is required" >&2; exit 1; }
command -v firefox >/dev/null 2>&1 || { echo "[live-root][error] firefox is required" >&2; exit 1; }

# Live user (fixed name, autologin, no password needed locally).
if ! id -u "$LIVE_USER" >/dev/null 2>&1; then
  useradd -m -U -s /bin/bash "$LIVE_USER"
fi
passwd -d "$LIVE_USER" || true
# audio/video for ALSA+display, input for pointer devices via logind seat
# ACLs, systemd-journal for qbzd volume bridge. Each group best-effort so a
# missing group never drops the others (wheel may not exist in minimal Leap
# containers; sudoers drop-in below grants live sudo independently).
getent group input >/dev/null 2>&1 || groupadd -r input 2>/dev/null || true
for grp in audio video input systemd-journal; do
  usermod -aG "$grp" "$LIVE_USER" 2>/dev/null || true
done
# Volatile live sudo (physical autologin session; sshd stays disabled without
# fxroute.live-password). Passwordless sudo matches common live media and keeps
# provider/CIFS helpers usable without persisting credentials.
if ! rpm -q sudo >/dev/null 2>&1; then
  zypper --non-interactive install --no-recommends sudo || true
fi
mkdir -p /etc/sudoers.d
printf '%s ALL=(ALL) NOPASSWD:ALL\n' "$LIVE_USER" > /etc/sudoers.d/99-fxroute-live
chmod 440 /etc/sudoers.d/99-fxroute-live
visudo -cf /etc/sudoers.d/99-fxroute-live >/dev/null 2>&1 || true

# Live firewall: firewalld is enabled by preset; open the FXRoute LAN ports
# offline (no running daemon in the container). Without 8000/tcp the QEMU
# user-forward and LAN browsers cannot reach the live Web UI.
if command -v firewall-offline-cmd >/dev/null 2>&1; then
  firewall-offline-cmd --zone=public --add-port=8000/tcp || true
  firewall-offline-cmd --zone=public --add-port=80/tcp || true
  firewall-offline-cmd --zone=public --add-port=443/tcp || true
  firewall-offline-cmd --zone=public --add-port=5353/udp || true
  firewall-offline-cmd --zone=public --add-port=4444/tcp || true
  firewall-offline-cmd --zone=public --add-service=ssh || true
fi

# FXRoute checkout on the build commit (no git fetch at live boot).
rm -rf -- "$LIVE_HOME/fxroute"
mkdir -p -- "$LIVE_HOME/fxroute"
tar --extract --file /tmp/fxroute-source.tar --directory "$LIVE_HOME/fxroute" --no-same-owner
chown -R "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/fxroute"
chmod 755 "$LIVE_HOME/fxroute/install.sh"
if [[ -f /etc/fxroute-live-commit ]]; then
  LIVE_COMMIT="$(cat /etc/fxroute-live-commit)"
else
  LIVE_COMMIT="unknown"
fi
# Best effort: make the tree match the build commit when git is available.
if command -v git >/dev/null 2>&1; then
  su "$LIVE_USER" -c "cd ~/fxroute && git init -q -b main 2>/dev/null || true"
  su "$LIVE_USER" -c "cd ~/fxroute && git add -A 2>/dev/null && git -c user.name='FXRoute Live' -c user.email='live@fxroute.local' commit -qm 'FXRoute live snapshot $LIVE_COMMIT' 2>/dev/null || true"
fi

# Python venv + dependencies (same as install.sh, but without starting systemd units).
# Providers stay uninstalled like install.sh --providers none on the
# installed path: TIDAL/Spotify/Qobuz install on demand from Settings.
# (The TIDAL pip list ships in the source tree for that on-demand install.)
su "$LIVE_USER" -c "cd ~/fxroute && python3 -m venv .venv"
su "$LIVE_USER" -c "cd ~/fxroute && .venv/bin/pip install -q --upgrade pip"
su "$LIVE_USER" -c "cd ~/fxroute && .venv/bin/pip install -q -r requirements.txt"

# Native DSP build.
su "$LIVE_USER" -c "cd ~/fxroute && bash native_dsp/build.sh || bash -c 'cmake -S native_dsp -B native_dsp/build && cmake --build native_dsp/build --parallel'"

# Calf LV2 exactly like install.sh (pinned version + checksum): the DSP
# helper chain always instantiates the Calf BassEnhancer, so a missing
# bundle fails every playback transition on every device. User-local
# bundle like the installer; the build fails loudly if it is incomplete.
CALF_VERSION="0.90.9"
CALF_SHA256="2d304eed88e87438b2b8857a2f4480046bf4003bce2e17a042abdbbf7d59122f"
curl -fL --retry 3 -o /tmp/calf.tar.gz "https://github.com/calf-studio-gear/calf/archive/$CALF_VERSION.tar.gz"
printf '%s  %s\n' "$CALF_SHA256" /tmp/calf.tar.gz | sha256sum -c -
rm -rf /tmp/calf-src /tmp/calf-build /tmp/calf-stage
mkdir -p /tmp/calf-src
tar -xzf /tmp/calf.tar.gz -C /tmp/calf-src
cmake -S "/tmp/calf-src/calf-$CALF_VERSION" -B /tmp/calf-build \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr \
  -DLV2DIR=/usr/lib64/lv2 -DWANT_GUI=OFF -DWANT_JACK=OFF \
  -DWANT_LASH=OFF -DWANT_SORDI=OFF
cmake --build /tmp/calf-build --parallel
env DESTDIR=/tmp/calf-stage cmake --install /tmp/calf-build
[[ -d /tmp/calf-stage/usr/lib64/lv2/calf.lv2 && -f /tmp/calf-stage/usr/lib64/calf/libcalf.so ]] \
  || { echo "[live-root][error] Calf LV2 source build did not produce the expected bundle" >&2; exit 1; }
rm -f /tmp/calf-stage/usr/lib64/lv2/calf.lv2/calf.so
cp -- /tmp/calf-stage/usr/lib64/calf/libcalf.so /tmp/calf-stage/usr/lib64/lv2/calf.lv2/calf.so
su "$LIVE_USER" -c "mkdir -p ~/.lv2 && rm -rf ~/.lv2/calf.lv2"
cp -a /tmp/calf-stage/usr/lib64/lv2/calf.lv2 "$LIVE_HOME/.lv2/calf.lv2"
chown -R "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/.lv2/calf.lv2"
rm -rf /tmp/calf.tar.gz /tmp/calf-src /tmp/calf-build /tmp/calf-stage

# Minimal .env for live (Music dir in RAM home).
if [[ ! -f "$LIVE_HOME/fxroute/.env" ]]; then
  printf 'MUSIC_ROOT=%s/Music\nDOWNLOADS_SUBDIR=incoming\nLOG_LEVEL=INFO\nHOST=0.0.0.0\nPORT=8000\n' "$LIVE_HOME" > "$LIVE_HOME/fxroute/.env"
  chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/fxroute/.env"
fi
su "$LIVE_USER" -c "mkdir -p ~/Music ~/fxroute/media/cache/covers ~/.config/fxroute/measurements"

# PipeWire DSP sink config for the live user.
su "$LIVE_USER" -c "mkdir -p ~/.config/pipewire/pipewire-pulse.conf.d ~/.config/pipewire/pipewire.conf.d ~/.config/wireplumber/wireplumber.conf.d ~/.config/systemd/user"
cat > "$LIVE_HOME/.config/pipewire/pipewire-pulse.conf.d/50-fxroute-dsp-sink.conf" <<'EOF'
pulse.cmd = [
  { cmd = "load-module" args = "module-null-sink sink_name=fxroute_dsp_sink sink_properties=device.description=FXRoute_DSP_Ingress" flags = [ ] }
]
EOF
chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/.config/pipewire/pipewire-pulse.conf.d/50-fxroute-dsp-sink.conf"

# fxroute.service user unit (same Exec as install.sh) + enable via symlink.
LIVE_UID="$(id -u "$LIVE_USER")"
cat > "$LIVE_HOME/.config/systemd/user/fxroute.service" <<EOF
[Unit]
Description=FXRoute
After=default.target
Documentation=file://$LIVE_HOME/fxroute/README.md

[Service]
Type=simple
WorkingDirectory=$LIVE_HOME/fxroute
EnvironmentFile=$LIVE_HOME/fxroute/.env
ExecStart=$LIVE_HOME/fxroute/.venv/bin/python3 $LIVE_HOME/fxroute/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/.config/systemd/user/fxroute.service"
su "$LIVE_USER" -c "mkdir -p ~/.config/systemd/user/default.target.wants && ln -sf ../fxroute.service ~/.config/systemd/user/default.target.wants/fxroute.service"

# Appliance identity: hostname + live marker + transient machine-id.
printf 'fxroute-live\n' > /etc/hostname
printf 'fxroute-live\n' > /etc/fxroute-live
printf '%s\n' "$LIVE_COMMIT" > /etc/fxroute-live-commit
: > /etc/machine-id
chmod 444 /etc/machine-id || true

# SDDM autologin as live user (X11 default.desktop like first-boot; the
# plasma6-session-x11 package provides it, plasmawayland stays available).
mkdir -p /etc/sddm.conf.d
cat > /etc/sddm.conf.d/10-fxroute-autologin.conf <<EOF
[Autologin]
User=$LIVE_USER
Session=default.desktop
Relogin=false
EOF
chmod 644 /etc/sddm.conf.d/10-fxroute-autologin.conf
if [[ ! -e /usr/share/xsessions/default.desktop ]]; then
  echo "[live-root][error] X11 default.desktop session is missing" >&2
  exit 1
fi
# SUSE's SDDM patch reads this setting after the SDDM drop-ins. An empty
# DISPLAYMANAGER_AUTOLOGIN overrides User= above (same as installed desktop).
if grep -q '^DISPLAYMANAGER_AUTOLOGIN=' /etc/sysconfig/displaymanager; then
  sed -i "s|^DISPLAYMANAGER_AUTOLOGIN=.*|DISPLAYMANAGER_AUTOLOGIN=\"$LIVE_USER\"|" /etc/sysconfig/displaymanager
else
  printf 'DISPLAYMANAGER_AUTOLOGIN="%s"\n' "$LIVE_USER" >> /etc/sysconfig/displaymanager
fi
test -x /usr/etc/X11/xdm/Xsession
systemctl set-default graphical.target
# The xdm package symlinks display-manager.service to display-manager-legacy;
# force SDDM so the live autologin config actually takes effect.
ln -sf /usr/lib/systemd/system/sddm.service /etc/systemd/system/display-manager.service
systemctl enable sddm.service
# NetworkManager is enabled by the installer on installed systems, not by
# package presets; the live root must do the same or no interface (wired
# or WLAN) ever comes up and Plasma lists no networks.
systemctl enable NetworkManager.service

# Appliance defaults (mirrors first-boot-install.sh desktop stack, live edition).
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/10-fxroute-appliance.conf <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
HandlePowerKey=poweroff
EOF
chmod 644 /etc/systemd/logind.conf.d/10-fxroute-appliance.conf

# Live Firefox: homepage = local FXRoute.
for candidate in /usr/lib64/firefox/distribution /usr/lib/firefox/distribution; do
  if mkdir -p "$candidate" 2>/dev/null; then
    cat > "$candidate/policies.json" <<'EOF'
{
  "policies": {
    "Homepage": {
      "URL": "http://127.0.0.1:8000/",
      "StartPage": "homepage"
    },
    "DontCheckDefaultBrowser": true,
    "DisableFirefoxStudies": true,
    "DisablePocket": true
  }
}
EOF
    chmod 644 "$candidate/policies.json"
    break
  fi
done

# Appliance session helper, icon and wallpaper: same files the installed
# desktop uses (first-boot-install.sh). The helper ensures the FXRoute and
# Spotify desktop links at graphical login, honoring localized folders.
if [[ -f /tmp/fxroute-session-init.sh ]]; then
  mkdir -p /usr/local/libexec
  cp -- /tmp/fxroute-session-init.sh /usr/local/libexec/fxroute-appliance-session-init.sh
  chmod 755 /usr/local/libexec/fxroute-appliance-session-init.sh
fi
if [[ -f "$LIVE_HOME/fxroute/static/favicon.svg" ]]; then
  mkdir -p /usr/share/pixmaps
  cp -- "$LIVE_HOME/fxroute/static/favicon.svg" /usr/share/pixmaps/fxroute.svg
  chmod 644 /usr/share/pixmaps/fxroute.svg
fi
if [[ -f "$LIVE_HOME/fxroute/assets/fxroute-wallpaper.png" ]]; then
  mkdir -p /usr/share/wallpapers
  cp -- "$LIVE_HOME/fxroute/assets/fxroute-wallpaper.png" /usr/share/wallpapers/fxroute-wallpaper.png
  chmod 644 /usr/share/wallpapers/fxroute-wallpaper.png
fi

# Audio/power parity with the installed path (install.sh): WirePlumber
# Bluetooth monitor without seat gate, canonical PipeWire clock rates, and
# the narrow polkit rule for the UI power menu (rendered for fxroute).
su "$LIVE_USER" -c "mkdir -p ~/.config/wireplumber/wireplumber.conf.d ~/.config/pipewire/pipewire.conf.d"
cat > "$LIVE_HOME/.config/wireplumber/wireplumber.conf.d/50-fxroute-bluetooth.conf" <<'EOF'
# Managed by FXRoute. Headless appliances (linger, no login session) never
# activate a logind seat; with seat-monitoring enabled WirePlumber never
# creates the BlueZ monitor, no A2DP endpoints are registered and Bluetooth
# input stays unavailable. Disable the seat gate so the monitor always runs.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
cat > "$LIVE_HOME/.config/pipewire/pipewire.conf.d/90-fxroute-clock-rate.conf" <<'EOF'
# Managed by FXRoute. Changes take effect after restarting PipeWire/session or rebooting.
context.properties = {
    default.clock.rate = 44100
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
}
EOF
chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/.config/wireplumber/wireplumber.conf.d/50-fxroute-bluetooth.conf" "$LIVE_HOME/.config/pipewire/pipewire.conf.d/90-fxroute-clock-rate.conf"
chmod 644 "$LIVE_HOME/.config/wireplumber/wireplumber.conf.d/50-fxroute-bluetooth.conf" "$LIVE_HOME/.config/pipewire/pipewire.conf.d/90-fxroute-clock-rate.conf"
if [[ -f "$LIVE_HOME/fxroute/assets/polkit/50-fxroute-power.rules" ]]; then
  mkdir -p /etc/polkit-1/rules.d
  sed -e "s/INSTALL_USER_PLACEHOLDER/$LIVE_USER/g" "$LIVE_HOME/fxroute/assets/polkit/50-fxroute-power.rules" > /etc/polkit-1/rules.d/50-fxroute-power.rules
  chmod 644 /etc/polkit-1/rules.d/50-fxroute-power.rules
fi

# Privileged helpers the app calls via passwordless sudo (same paths as
# install.sh): CIFS mount helper for SMB libraries and the provider
# helper for provider login/network checks. Live sudo already covers
# NOPASSWD, so only the root-owned binaries are needed here.
for helper in fxroute-cifs-mount fxroute-provider-privileged; do
  [[ -f "$LIVE_HOME/fxroute/scripts/$helper" ]] || { echo "[live-root][error] helper missing: scripts/$helper" >&2; exit 1; }
  install -o 0 -g 0 -m 755 "$LIVE_HOME/fxroute/scripts/$helper" /usr/local/sbin/$helper
done

# Desktop links at build time too (same content as the installed desktop;
# the session helper re-ensures them at login for localized folders).
su "$LIVE_USER" -c "mkdir -p ~/Desktop"
cat > "$LIVE_HOME/Desktop/FXRoute.desktop" <<'EOF'
[Desktop Entry]
Type=Link
Name=FXRoute
Comment=Open the FXRoute control surface
URL=http://127.0.0.1:8000/
Icon=/usr/share/pixmaps/fxroute.svg
EOF
cat > "$LIVE_HOME/Desktop/Spotify Download.desktop" <<'EOF'
[Desktop Entry]
Type=Link
Name=Spotify
Comment=Official Spotify download page for Linux
URL=https://www.spotify.com/download/linux/
Icon=internet-web-browser
EOF
chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/Desktop/FXRoute.desktop" "$LIVE_HOME/Desktop/Spotify Download.desktop"
chmod 644 "$LIVE_HOME/Desktop/FXRoute.desktop" "$LIVE_HOME/Desktop/Spotify Download.desktop"

# Live desktop launcher (waits bounded for /api/status, then kiosk; a backend
# that never comes up produces a visible error state instead of a dead desktop).
cat > /usr/local/bin/fxroute-desktop-launcher <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
# Wait for the FXRoute backend, but never unbounded: a backend that never
# comes up must still produce a visible desktop state instead of leaving the
# user on a seemingly dead desktop with no kiosk and no clue. 180s covers the
# cold-media backend start (mpv probes, venv import) plus several service
# restart attempts (RestartSec=10); healthy boots leave the loop in seconds.
status_url="http://127.0.0.1:8000/api/status"
backend_ready=0
for _ in $(seq 1 180); do
  if curl --fail --silent --show-error --connect-timeout 5 --max-time 30 \
      "$status_url" >/dev/null; then
    backend_ready=1
    break
  fi
  sleep 1
done
if [[ "$backend_ready" != 1 ]]; then
  logger -t fxroute-desktop-launcher \
    "FXRoute backend not reachable after 180s; showing the error state" || true
  # Visible failure state: a note on the Desktop plus the kiosk window with
  # the browser's own "unable to connect" page instead of a bare desktop.
  mkdir -p "$HOME/Desktop" 2>/dev/null || true
  printf '%s\n' \
    'FXRoute could not start: the backend did not come up on 127.0.0.1:8000.' \
    'Diagnosis: journalctl --user -u fxroute -n 50' \
    > "$HOME/Desktop/FXRoute-NOT-STARTED.txt" 2>/dev/null || true
  exec firefox --kiosk http://127.0.0.1:8000/
fi
# First graphical login only: desktop links, wallpaper, Firefox bookmark
# (same helper as the installed desktop).
if [[ ! -f "$HOME/.local/share/fxroute/appliance-ready" ]]; then
  if [[ -x /usr/local/libexec/fxroute-appliance-session-init.sh ]]; then
    /usr/local/libexec/fxroute-appliance-session-init.sh || true
  fi
  mkdir -p "$HOME/.local/share/fxroute"
  touch "$HOME/.local/share/fxroute/appliance-ready"
fi
exec firefox --kiosk http://127.0.0.1:8000/
EOF
chmod 755 /usr/local/bin/fxroute-desktop-launcher
su "$LIVE_USER" -c "mkdir -p ~/.config/autostart"
cat > "$LIVE_HOME/.config/autostart/fxroute.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=FXRoute
Comment=Open the FXRoute control surface
Exec=/usr/local/bin/fxroute-desktop-launcher
TryExec=firefox
OnlyShowIn=KDE;
X-GNOME-Autostart-enabled=true
EOF
chown "$LIVE_USER:$(id -gn "$LIVE_USER")" "$LIVE_HOME/.config/autostart/fxroute.desktop"
chmod 644 "$LIVE_HOME/.config/autostart/fxroute.desktop"

# Live KWallet/welcome/power defaults for the live user.
LIVE_GROUP="$(id -gn "$LIVE_USER")"
printf '[Wallet]\nEnabled=false\n' > "$LIVE_HOME/.config/kwalletrc"
printf '[Daemon]\nAutolock=false\nLockOnResume=false\n' > "$LIVE_HOME/.config/kscreenlockerrc"
cat > "$LIVE_HOME/.config/powerdevilrc" <<'EOF'
[AC][SuspendAndShutdown]
AutoSuspendAction=0
[AC][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
[Battery][SuspendAndShutdown]
AutoSuspendAction=0
[Battery][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
[LowBattery][SuspendAndShutdown]
AutoSuspendAction=0
[LowBattery][Display]
DimDisplayWhenIdle=false
TurnOffDisplayWhenIdle=false
EOF
chown "$LIVE_USER:$LIVE_GROUP" "$LIVE_HOME/.config/kwalletrc" "$LIVE_HOME/.config/kscreenlockerrc" "$LIVE_HOME/.config/powerdevilrc"
chmod 600 "$LIVE_HOME/.config/kwalletrc" "$LIVE_HOME/.config/kscreenlockerrc" "$LIVE_HOME/.config/powerdevilrc"
su "$LIVE_USER" -c "mkdir -p ~/.local/share/opensuse-welcome && echo 1 > ~/.local/share/opensuse-welcome/launched"
su "$LIVE_USER" -c "mkdir -p ~/.config/autostart && printf '[Desktop Entry]\nHidden=true\n' > ~/.config/autostart/org.opensuse.opensuse_welcome_launcher.desktop"

# Live notice on the desktop.
cat > "$LIVE_HOME/Desktop-LIVE-README.txt" <<'EOF'
FXRoute Live Mode — changes and logins are not saved and will be lost after reboot.

Try FXRoute directly from this USB/ISO medium. Network, audio, DSP,
measurements and providers work in this session, but nothing is stored.
Internal drives are not automatically mounted or changed.
EOF
chown "$LIVE_USER:$LIVE_GROUP" "$LIVE_HOME/Desktop-LIVE-README.txt" || true
su "$LIVE_USER" -c "mkdir -p ~/Desktop && cp -f ~/Desktop-LIVE-README.txt ~/Desktop/LIVE-MODE-README.txt || true"

# udisks protection is installed as a file (see live-udev-nomount.rules); copy it.
if [[ -f /tmp/fxroute-live-udev.rules ]]; then
  mkdir -p /etc/udev/rules.d
  cp -- /tmp/fxroute-live-udev.rules /etc/udev/rules.d/99-fxroute-live-nomount.rules
  chmod 644 /etc/udev/rules.d/99-fxroute-live-nomount.rules
fi
if [[ -f /tmp/fxroute-live-init.sh ]]; then
  mkdir -p /usr/local/libexec
  cp -- /tmp/fxroute-live-init.sh /usr/local/libexec/fxroute-live-init.sh
  chmod 755 /usr/local/libexec/fxroute-live-init.sh
  mkdir -p /etc/systemd/system
  cat > /etc/systemd/system/fxroute-live-init.service <<EOF2
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
EOF2
  systemctl enable fxroute-live-init.service || true
fi

# Live must not start the installer or sshd by default.
systemctl disable fxroute-first-boot.service 2>/dev/null || true
systemctl mask fxroute-first-boot.service 2>/dev/null || true
systemctl disable sshd.service 2>/dev/null || true

# Ensure flat-squash contract: /proc must exist in the squash root.
mkdir -p /proc /sys /dev /run /tmp
chmod 755 /proc /sys || true
chmod 1777 /tmp

SETUP_EOF
chmod 755 "$SETUP_SCRIPT"

UDEV_RULE_SRC="$ROOT_DIR/iso/scripts/live-udev-nomount.rules"
[[ -f "$UDEV_RULE_SRC" ]] || die "udev rule is missing: $UDEV_RULE_SRC"
LIVE_INIT_SRC="$ROOT_DIR/iso/scripts/fxroute-live-init.sh"
[[ -f "$LIVE_INIT_SRC" ]] || die "live init script is missing: $LIVE_INIT_SRC"
SESSION_INIT_SRC="$ROOT_DIR/iso/scripts/fxroute-appliance-session-init.sh"
[[ -f "$SESSION_INIT_SRC" ]] || die "session init script is missing: $SESSION_INIT_SRC"

printf '[live-root] pulling %s\n' "$DOCKER_IMAGE"
docker pull "$DOCKER_IMAGE" >/dev/null

printf '[live-root] running live setup in container\n'
KERNEL_RPM_MOUNT=()
for i in "${!KERNEL_RPMS[@]}"; do
  KERNEL_RPM_MOUNT+=(-v "${KERNEL_RPMS[$i]}:/tmp/kernel-rpm-$i.rpm:ro,z")
done
docker run --rm --name "$CONTAINER_NAME" \
  -v "$SOURCE_SNAP:/tmp/fxroute-source.tar:ro,z" \
  -v "$SETUP_SCRIPT:/tmp/live-setup-inner.sh:ro,z" \
  -v "$UDEV_RULE_SRC:/tmp/fxroute-live-udev.rules:ro,z" \
  -v "$LIVE_INIT_SRC:/tmp/fxroute-live-init.sh:ro,z" \
  -v "$SESSION_INIT_SRC:/tmp/fxroute-session-init.sh:ro,z" \
  "${KERNEL_RPM_MOUNT[@]}" \
  -v "$LIVE_ROOT:/live-root:z" \
  -v "$WORK_DIR:/live-output:z" \
  -e SOURCE_DATE_EPOCH="$LIVE_EPOCH" \
  "$DOCKER_IMAGE" bash -c "
    set -Eeuo pipefail
    printf '%s\n' '$BUILD_COMMIT' > /etc/fxroute-live-commit
    bash /tmp/live-setup-inner.sh
    # Export container root to /live-root (exclude kernel pseudo-fs and docker metadata).
    tar --create --one-file-system --numeric-owner --preserve-permissions \
      --exclude=./live-root --exclude=./live-output --exclude=./.dockerenv \
      --exclude=./proc/* --exclude=./sys/* --exclude=./dev/* \
      --exclude=./run/* --exclude=./tmp/* \
      --directory=/ . | tar --extract --directory=/live-root \
        --numeric-owner --same-owner --same-permissions
    # Re-create pseudo-fs mountpoints inside the exported tree.
    mkdir -p /live-root/proc /live-root/sys /live-root/dev /live-root/run /live-root/tmp
    chmod 1777 /live-root/tmp
    # Build commit marker survives the tar round-trip.
    printf '%s\n' '$BUILD_COMMIT' > /live-root/etc/fxroute-live-commit
    printf 'fxroute-live\n' > /live-root/etc/hostname
    printf 'fxroute-live\n' > /live-root/etc/fxroute-live
    : > /live-root/etc/machine-id
    # Pack while the root still has its original ownership and setuid bits.
    # Only the finished artifact is handed to the invoking host user.
    env -u SOURCE_DATE_EPOCH mksquashfs /live-root /live-output/squashfs.img \
      -comp xz -noappend -mkfs-time '$LIVE_EPOCH' -all-time '$LIVE_EPOCH' \
      -no-xattrs -no-progress >/dev/null
    chown '$HOST_UID:$HOST_GID' /live-output/squashfs.img
  "

mv -- "$WORK_DIR/squashfs.img" "$OUTPUT"
printf '[live-root] wrote %s\n' "$OUTPUT"
