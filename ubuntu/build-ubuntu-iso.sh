#!/usr/bin/env bash
# Build a bootable Ubuntu 26.04 Desktop ISO with the FXRoute payload.
# Standard-Ubuntu-Weg: offizielles Desktop-ISO + livefs-editor. Die
# Secure-Boot-Kette (Shim/signierter Kernel) bleibt unangetastet; der normale
# "Try or Install Ubuntu"-Eintrag bleibt Default. Neu ist nur der Eintrag
# "Install FXRoute" (derselbe Installer + `autoinstall` + Seed).
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UBUNTU_VERSION="26.04.1"
BASE_URL="https://releases.ubuntu.com/26.04/ubuntu-${UBUNTU_VERSION}-desktop-amd64.iso"
BASE_SHA256="601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f"
BASE_ISO="${FXROUTE_UBUNTU_BASE_ISO:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/ubuntu-${UBUNTU_VERSION}-desktop-amd64.iso}"
OUTPUT="${FXROUTE_UBUNTU_ISO_OUTPUT:-$ROOT_DIR/dist/fxroute-ubuntu-26.04-x86_64.iso}"
KEEP_WORK=0

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --base-iso PATH   Use PATH instead of the cached/downloaded Ubuntu ISO
  --output PATH     Write the resulting ISO to PATH
  --keep-work       Keep the temporary staging directory
  -h, --help        Show this help
EOF
}

die() {
  printf '[ubuntu-iso][error] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-iso) BASE_ISO="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    --keep-work) KEEP_WORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required to create the source archive"
command -v tar >/dev/null 2>&1 || die "tar is required to create the source archive"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required to verify the Ubuntu ISO"
command -v curl >/dev/null 2>&1 || die "curl is required to download the Ubuntu ISO"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
command -v livefs-edit >/dev/null 2>&1 || die "livefs-edit is required (pipx install git+https://github.com/mwhudson/livefs-editor.git)"
command -v xorriso >/dev/null 2>&1 || die "xorriso is required for livefs-edit repacking"

if [[ ! -f "$BASE_ISO" ]]; then
  mkdir -p "$(dirname "$BASE_ISO")"
  partial="${BASE_ISO}.part"
  printf '[ubuntu-iso] downloading %s\n' "$BASE_URL"
  curl --fail --location --retry 3 --continue-at - --output "$partial" "$BASE_URL"
  mv -- "$partial" "$BASE_ISO"
fi

actual_sha256="$(sha256sum "$BASE_ISO" | awk '{print $1}')"
[[ "$actual_sha256" == "$BASE_SHA256" ]] \
  || die "Ubuntu ISO checksum mismatch for $BASE_ISO (expected $BASE_SHA256, got $actual_sha256)"

OUTPUT="$(realpath -m "$OUTPUT")"
BASE_ISO="$(realpath -m "$BASE_ISO")"
mkdir -p "$(dirname "$OUTPUT")"
[[ "$OUTPUT" != "$BASE_ISO" ]] || die "The output path must not overwrite the base ISO"

BUILD_TMP_DIR="${FXROUTE_UBUNTU_TMPDIR:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/build}"
mkdir -p "$BUILD_TMP_DIR"
WORK_DIR="$(mktemp -d "$BUILD_TMP_DIR/fxroute-ubuntu-iso.XXXXXX")"
cleanup() {
  if [[ "$KEEP_WORK" -eq 0 ]]; then
    rm -rf -- "$WORK_DIR"
  else
    printf '[ubuntu-iso] keeping work directory: %s\n' "$WORK_DIR"
  fi
}
trap cleanup EXIT

STAGE_DIR="$WORK_DIR/stage"
mkdir -p "$STAGE_DIR/fxroute-iso/scripts"

printf '[ubuntu-iso] creating deterministic source.tar\n'
git -C "$ROOT_DIR" ls-files -z \
  | tar --directory="$ROOT_DIR" --create --file="$STAGE_DIR/fxroute-iso/source.tar" \
      --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 \
      --numeric-owner --pax-option=delete=atime,delete=ctime \
      --no-recursion --null --verbatim-files-from \
      --files-from=-
tar --list --file="$STAGE_DIR/fxroute-iso/source.tar" >/dev/null || die "Could not read generated source archive"

git -C "$ROOT_DIR" rev-parse HEAD > "$STAGE_DIR/fxroute-iso/build-commit"
cp -- "$ROOT_DIR/ubuntu/scripts/first-boot-install-ubuntu.sh" \
  "$ROOT_DIR/ubuntu/scripts/fxroute-live-autostart.sh" \
  "$ROOT_DIR/ubuntu/scripts/fxroute-ubuntu-launcher.sh" \
  "$STAGE_DIR/fxroute-iso/scripts/"
cp -- "$ROOT_DIR/ubuntu/autoinstall/first-boot.service" "$STAGE_DIR/fxroute-iso/first-boot.service"
chmod 755 "$STAGE_DIR/fxroute-iso/scripts/"*.sh
cp -- "$ROOT_DIR/ubuntu/autoinstall/user-data" "$STAGE_DIR/user-data"

# FXRoute runtime deps, pre-baked into the live squashfs so the Try session
# and the installed system start fast. Same set install.sh installs via apt
# (core+audio, SMB, bluetooth agent, avahi/.local, qobuz runtime, spotify
# keyring, native DSP build deps). No third-party repos, no PPA.
read -r -a LIVE_PACKAGES <<< \
  "python3 python3-pip python3-venv mpv ffmpeg playerctl bluez wireplumber pipewire-bin pipewire-pulse pulseaudio-utils libspa-0.2-bluetooth rtkit curl git socat tar dbus dbus-bin smbclient cifs-utils libglib2.0-bin gvfs gvfs-backends gvfs-fuse python3-dbus python3-gi gir1.2-glib-2.0 avahi-daemon libavahi-client3 libnss-mdns pipewire-alsa gnome-keyring libsecret-1-0 libpam-gnome-keyring openssh-server gcc libc6-dev pkg-config libpipewire-0.3-dev libspa-0.2-dev liblilv-dev lilv-utils lv2-dev lsp-plugins-lv2 zam-plugins calf-plugins libebur128-dev libsamplerate0-dev libspeexdsp-dev"

printf '[ubuntu-iso] editing live ISO with livefs-edit\n'
livefs-edit "$BASE_ISO" "$OUTPUT" \
  --install-packages "${LIVE_PACKAGES[@]}" \
  --cp "$STAGE_DIR/fxroute-iso" 'new/iso/fxroute-iso' \
  --cp "$STAGE_DIR/user-data" '$LAYERS[0]/var/lib/cloud/seed/nocloud/user-data' \
  --cp "$ROOT_DIR/ubuntu/scripts/fxroute-live-autostart.sh" '$LAYERS[0]/usr/local/libexec/fxroute-live-autostart.sh' \
  --cp "$ROOT_DIR/ubuntu/scripts/fxroute-ubuntu-launcher.sh" '$LAYERS[0]/usr/local/bin/fxroute-desktop-launcher' \
  --python "$ROOT_DIR/ubuntu/livefs-actions/add_install_entry.py"

printf '[ubuntu-iso] wrote %s\n' "$OUTPUT"
printf '[ubuntu-iso] sha256 %s\n' "$(sha256sum "$OUTPUT" | awk '{print $1}')"
