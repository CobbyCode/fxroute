#!/usr/bin/env bash
# Build a bootable Ubuntu 26.04 Desktop ISO with the FXRoute payload.
# Standard-Ubuntu-Weg: offizielles Desktop-ISO + livefs-editor. Die
# Secure-Boot-Kette (Shim/signierter Kernel) bleibt unangetastet; der normale
# "Try or Install Ubuntu"-Eintrag bleibt Default. Neu ist nur der Eintrag
# "Install FXRoute" (derselbe Installer + `autoinstall` + Seed).
#
# livefs-edit braucht ein Loop-Device: wo das fehlt (rootless Desktop),
# laeuft derselbe Bau per --docker in einem privilegierten
# ubuntu:26.04-Container (gleiches Werkzeug, gleiche Aktionen).
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$HOME/.local/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UBUNTU_VERSION="26.04.1"
BASE_URL="https://releases.ubuntu.com/26.04/ubuntu-${UBUNTU_VERSION}-desktop-amd64.iso"
BASE_SHA256="601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f"
BASE_ISO="${FXROUTE_UBUNTU_BASE_ISO:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/ubuntu-${UBUNTU_VERSION}-desktop-amd64.iso}"
OUTPUT="${FXROUTE_UBUNTU_ISO_OUTPUT:-$ROOT_DIR/dist/fxroute-ubuntu-26.04-x86_64.iso}"
BUILDER="${FXROUTE_UBUNTU_BUILDER:-direct}"
TEST_SEED=0
INSIDE_DOCKER=0
KEEP_WORK=0

usage() {
  cat <<EOF
Usage: $0 [options]

Options:
  --base-iso PATH   Use PATH instead of the cached/downloaded Ubuntu ISO
  --output PATH     Write the resulting ISO to PATH
                    (docker mode: must be under the repo or the ISO cache dir)
  --docker          Run livefs-edit in a privileged ubuntu:26.04 container
                    (same tool, same actions; for hosts without loop rights)
  --test-seed       Stage the fully-automatic QEMU test seed instead of the
                    interactive product seed (dev/test ISOs only)
  --inside-docker   Internal: container-side build (set up deps, then build)
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
    --docker) BUILDER="docker"; shift ;;
    --test-seed) TEST_SEED=1; shift ;;
    --inside-docker) INSIDE_DOCKER=1; shift ;;
    --keep-work) KEEP_WORK=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

if [[ "$INSIDE_DOCKER" -eq 1 ]]; then
  printf '[ubuntu-iso] installing container build deps\n'
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends \
    xorriso squashfs-tools python3 python3-pip python3-yaml git curl ca-certificates \
    udev mount kmod initramfs-tools-core snapd zstd cpio xz-utils file \
    > /tmp/fxroute-apt.log 2>&1 || { tail -20 /tmp/fxroute-apt.log; die "apt setup failed"; }
  pip3 install --quiet --break-system-packages \
    "git+https://github.com/mwhudson/livefs-editor.git" \
    > /tmp/fxroute-pip.log 2>&1 || { tail -20 /tmp/fxroute-pip.log; die "livefs-editor install failed"; }
  command -v livefs-edit >/dev/null 2>&1 || die "livefs-edit install did not provide the binary"
fi

command -v git >/dev/null 2>&1 || die "git is required to create the source archive"
command -v tar >/dev/null 2>&1 || die "tar is required to create the source archive"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required to verify the Ubuntu ISO"
command -v curl >/dev/null 2>&1 || die "curl is required to download the Ubuntu ISO"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

if [[ ! -f "$BASE_ISO" ]]; then
  [[ "$INSIDE_DOCKER" -eq 0 ]] || die "base ISO is not mounted in the container: $BASE_ISO"
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

if [[ -n "${FXROUTE_UBUNTU_PRESTAGED:-}" ]]; then
  # Container run: payload was staged outside (git ownership); reuse it.
  STAGE_DIR="$FXROUTE_UBUNTU_PRESTAGED"
  [[ -f "$STAGE_DIR/fxroute-iso/source.tar" ]] || die "prestaged payload missing: $STAGE_DIR"
else
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
  if [[ "$TEST_SEED" -eq 1 ]]; then
    printf '[ubuntu-iso] warning: staging the TEST seed (dev/test ISO, never release this)\n'
    cp -- "$ROOT_DIR/ubuntu/autoinstall/user-data.test" "$STAGE_DIR/user-data"
    # QEMU-only kernel extras, baked into the Try entry at build time:
    # serial console for observability, live SSH test hook.
    export FXROUTE_GRUB_TEST_EXTRA="console=ttyS0 fxroute.live-password=test"
    # QEMU test SSH hook (unit + helper, enabled in the squashfs).
    export FXROUTE_TEST_SSH=1
    TEST_SSH_CPS="--cp $ROOT_DIR/ubuntu/autoinstall/fxroute-test-ssh.service \$LAYERS[0]/etc/systemd/system/fxroute-test-ssh.service --cp $ROOT_DIR/ubuntu/scripts/fxroute-test-ssh.sh \$LAYERS[0]/usr/local/libexec/fxroute-test-ssh.sh"
  else
    cp -- "$ROOT_DIR/ubuntu/autoinstall/user-data" "$STAGE_DIR/user-data"
    TEST_SSH_CPS=""
  fi
fi

# FXRoute runtime deps, pre-baked into the live squashfs so the Try session
# and the installed system start fast. Same set install.sh installs via apt
# (core+audio, SMB, bluetooth agent, avahi/.local, qobuz runtime, spotify
# keyring, native DSP build deps). Verified present on resolute; pipewire
# 1.6.2 needs no backports. No third-party repos, no PPA.
read -r -a LIVE_PACKAGES <<< \
  "python3 python3-pip python3-venv mpv ffmpeg playerctl bluez wireplumber pipewire-bin pipewire-pulse pulseaudio-utils libspa-0.2-bluetooth rtkit curl git socat tar dbus-bin smbclient cifs-utils libglib2.0-bin gvfs gvfs-backends gvfs-fuse python3-dbus python3-gi gir1.2-glib-2.0 avahi-daemon libavahi-client3 libnss-mdns pipewire-alsa gnome-keyring libsecret-1-0 libpam-gnome-keyring openssh-server gcc libc6-dev pkg-config libpipewire-0.3-dev libspa-0.2-dev liblilv-dev lilv-utils lv2-dev lsp-plugins-lv2 zam-plugins calf-plugins libebur128-dev libsamplerate0-dev libspeexdsp-dev"

export FXROUTE_UBUNTU_STAGE_DIR="$STAGE_DIR"

if [[ "$BUILDER" == "docker" && "$INSIDE_DOCKER" -eq 0 ]]; then
  command -v docker >/dev/null 2>&1 || die "docker is required for --docker"
  case "$OUTPUT" in
    "$ROOT_DIR"/*|"$(dirname "$BASE_ISO")"/*) ;;
    *) die "docker mode: --output must be under the repo or the ISO cache dir" ;;
  esac
  printf '[ubuntu-iso] re-running the build inside a privileged container\n'
  # livefs-edit stacks overlay mounts: upper/work must live on a real
  # filesystem, not on the container's own overlay /tmp. Bind a host dir.
  mkdir -p "$WORK_DIR/ctmp"
  docker run --rm --privileged \
    -v "$ROOT_DIR:$ROOT_DIR" \
    -v "$(dirname "$BASE_ISO"):$(dirname "$BASE_ISO")" \
    -v "$WORK_DIR:$WORK_DIR" \
    -v "$WORK_DIR/ctmp:/ctmp" \
    -e "TMPDIR=/ctmp" \
    -e "FXROUTE_UBUNTU_STAGE_DIR=$STAGE_DIR" \
    -e "FXROUTE_GRUB_TEST_EXTRA=${FXROUTE_GRUB_TEST_EXTRA:-}" \
    -e "FXROUTE_TEST_SSH=${FXROUTE_TEST_SSH:-}" \
    -e "FXROUTE_UBUNTU_BASE_ISO=$BASE_ISO" \
    -e "FXROUTE_UBUNTU_ISO_OUTPUT=$OUTPUT" \
    -e "FXROUTE_UBUNTU_BUILDER=direct" \
    -e "FXROUTE_UBUNTU_PRESTAGED=$STAGE_DIR" \
    ubuntu:26.04 "$ROOT_DIR/ubuntu/build-ubuntu-iso.sh" --inside-docker --keep-work
  rc=$?
  # Container runs as root: hand the work dir back to the invoking user.
  docker run --rm \
    -v "$WORK_DIR:$WORK_DIR" \
    ubuntu:26.04 chown -R "$(id -u):$(id -g)" "$WORK_DIR" >/dev/null 2>&1 || true
  [[ $rc -eq 0 ]] || die "container build failed (rc=$rc)"
  printf '[ubuntu-iso] wrote %s\n' "$OUTPUT"
  printf '[ubuntu-iso] sha256 %s\n' "$(sha256sum "$OUTPUT" | awk '{print $1}')"
  exit 0
fi

command -v livefs-edit >/dev/null 2>&1 || die "livefs-edit is required (direct mode: pipx install git+https://github.com/mwhudson/livefs-editor.git)"
command -v xorriso >/dev/null 2>&1 || die "xorriso is required for livefs-edit repacking"

printf '[ubuntu-iso] editing live ISO with livefs-edit\n'
# NOTE: --python takes code, not a path; the .py files stay the maintained
# source and are inlined here (they contain no backticks/`$`, safe to inline).
livefs-edit "$BASE_ISO" "$OUTPUT" \
  --python "$(cat "$ROOT_DIR/ubuntu/livefs-actions/remove_cdrom_source.py")" \
  --python "$(cat "$ROOT_DIR/ubuntu/livefs-actions/prep_chroot.py")" \
  --install-packages "${LIVE_PACKAGES[@]}" \
  --python "$(cat "$ROOT_DIR/ubuntu/livefs-actions/cleanup_chroot.py")" \
  --python "$(cat "$ROOT_DIR/ubuntu/livefs-actions/cp_payload.py")" \
  --cp "$STAGE_DIR/user-data" '$LAYERS[0]/var/lib/cloud/seed/nocloud/user-data' \
  --cp "$ROOT_DIR/ubuntu/scripts/fxroute-live-autostart.sh" '$LAYERS[0]/usr/local/libexec/fxroute-live-autostart.sh' \
  --cp "$ROOT_DIR/ubuntu/autoinstall/fxroute-live.desktop" '$LAYERS[0]/etc/xdg/autostart/fxroute-live.desktop' \
  --cp "$ROOT_DIR/ubuntu/scripts/fxroute-ubuntu-launcher.sh" '$LAYERS[0]/usr/local/bin/fxroute-desktop-launcher' \
  $TEST_SSH_CPS \
  --python "$(cat "$ROOT_DIR/ubuntu/livefs-actions/test_ssh.py")" \
  --python "$(cat "$ROOT_DIR/ubuntu/livefs-actions/add_install_entry.py")"

printf '[ubuntu-iso] wrote %s\n' "$OUTPUT"
printf '[ubuntu-iso] sha256 %s\n' "$(sha256sum "$OUTPUT" | awk '{print $1}')"
