#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ARMBIAN_REPOSITORY="https://github.com/armbian/build.git"
ARMBIAN_BUILD_REF="4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc"
REQUESTED_BOARD="rpi4"
RELEASE="trixie"
BRANCH="current"
WIFI_SETUP_PASSWORD="${FXROUTE_WIFI_SETUP_PASSWORD:-}"
ARMBIAN_SOURCE_DIR="${FXROUTE_ARMBIAN_SOURCE:-}"
ARMBIAN_CACHE_DIR="${FXROUTE_ARMBIAN_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/armbian}"
OUTPUT="${FXROUTE_ARMBIAN_OUTPUT:-}"
KERNEL_REF="${FXROUTE_ARMBIAN_KERNEL_REF:-}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
export SOURCE_DATE_EPOCH
E2FSPROGS_FAKE_TIME="${E2FSPROGS_FAKE_TIME:-$SOURCE_DATE_EPOCH}"
export E2FSPROGS_FAKE_TIME
KEEP_WORK=0

usage() {
  cat <<EOF
Usage: $0 [options]

Build a headless FXRoute ARM64 image with the pinned official Armbian build
framework. The default Raspberry Pi target is the current shared rpi4b board
configuration, which is also the official target for Pi 5 at this revision.

Options:
  --board <board>              Raspberry alias (rpi4/rpi5) or Armbian board name
  --release <release>          Armbian userspace release (default: $RELEASE)
  --branch <branch>            Armbian kernel branch (default: $BRANCH)
  --kernel-ref <ref>           Pin kernel ref, e.g. commit:<sha> or branch:<name>
  --wifi-setup-password <pass> Password authorizing the temporary setup form
  --output <path>              Write the primary image to this path
  --armbian-source <path>      Use an existing Armbian checkout at the pinned ref
  --cache-dir <path>           Cache the pinned Armbian checkout here
  --keep-work                  Keep the temporary Armbian build checkout
  -h, --help                   Show this help

The source and optional temporary setup password can be supplied through
FXROUTE_ARMBIAN_SOURCE and FXROUTE_WIFI_SETUP_PASSWORD. If no Wi-Fi setup
password is supplied, a random one is generated and printed once during the
build. The end user creates the FXRoute account and SSH key during first boot;
no builder credentials are written into the image.
For qemu-uboot-arm64, the qcow2 image and U-Boot companion are written beside
the requested output path.
EOF
}

die() {
  printf '[armbian][error] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --board)
      [[ $# -ge 2 ]] || die "--board requires a board name"
      REQUESTED_BOARD="$2"
      shift 2
      ;;
    --release)
      [[ $# -ge 2 ]] || die "--release requires a value"
      RELEASE="$2"
      shift 2
      ;;
    --branch)
      [[ $# -ge 2 ]] || die "--branch requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --kernel-ref)
      [[ $# -ge 2 ]] || die "--kernel-ref requires a value"
      KERNEL_REF="$2"
      shift 2
      ;;
    --wifi-setup-password)
      [[ $# -ge 2 ]] || die "--wifi-setup-password requires a value"
      WIFI_SETUP_PASSWORD="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || die "--output requires a path"
      OUTPUT="$2"
      shift 2
      ;;
    --armbian-source)
      [[ $# -ge 2 ]] || die "--armbian-source requires a path"
      ARMBIAN_SOURCE_DIR="$2"
      shift 2
      ;;
    --cache-dir)
      [[ $# -ge 2 ]] || die "--cache-dir requires a path"
      ARMBIAN_CACHE_DIR="$2"
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
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

[[ "$REQUESTED_BOARD" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
  || die "Invalid Armbian board name: $REQUESTED_BOARD"
[[ "$RELEASE" =~ ^[a-z0-9][a-z0-9._-]*$ ]] \
  || die "Invalid Armbian release: $RELEASE"
[[ "$BRANCH" =~ ^[a-z0-9][a-z0-9._-]*$ ]] \
  || die "Invalid Armbian branch: $BRANCH"
[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] \
  || die "SOURCE_DATE_EPOCH must be a non-negative integer"

command -v git >/dev/null 2>&1 || die "git is required"
command -v tar >/dev/null 2>&1 || die "tar is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v head >/dev/null 2>&1 || die "head is required"
command -v tr >/dev/null 2>&1 || die "tr is required"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

generate_wifi_setup_password() {
  local value=""
  while [[ ${#value} -lt 16 ]]; do
    value="$(head -c 32 /dev/urandom | tr -dc 'A-Za-z0-9')"
  done
  printf '%s' "${value:0:16}"
}

generate_setup_metadata() {
  # Store only a salted verifier. The image must not contain the password or
  # an equivalent network credential in recoverable form.
  # shellcheck disable=SC2016
  printf '%s' "$WIFI_SETUP_PASSWORD" |
    python3 -c '
import base64
import hashlib
import secrets
import sys

password = sys.stdin.read()
iterations = 600000
salt = secrets.token_bytes(16)
digest = hashlib.pbkdf2_hmac(
    "sha256", password.encode("ascii"), salt, iterations
)
verifier = f"pbkdf2-sha256${iterations}${salt.hex()}${digest.hex()}"
print(base64.b64encode(verifier.encode("ascii")).decode("ascii"))
'
}

if [[ -z "$WIFI_SETUP_PASSWORD" ]]; then
  WIFI_SETUP_PASSWORD="$(generate_wifi_setup_password)"
fi
[[ "$WIFI_SETUP_PASSWORD" =~ ^[A-Za-z0-9._-]{8,63}$ ]] \
  || die "The Wi-Fi setup password must be 8-63 letters, numbers, dots, underscores, or hyphens"
printf '[armbian] temporary Wi-Fi setup password: %s\n' "$WIFI_SETUP_PASSWORD"

board="$REQUESTED_BOARD"
requested_board="$board"
build_board="$requested_board"
case "$requested_board" in
  rpi4|rpi4b|rpi5|rpi5b)
    # Armbian currently uses one bcm2711 board file for Pi 4 and Pi 5.
    build_board="rpi4b"
    ;;
esac

AP_PASSWORD_VERIFIER_B64="$(generate_setup_metadata)"
[[ "$AP_PASSWORD_VERIFIER_B64" =~ ^[A-Za-z0-9+/=]+$ ]] || die "Could not derive a setup password verifier"

if [[ -z "$OUTPUT" ]]; then
  OUTPUT="$ROOT_DIR/dist/fxroute-armbian-${requested_board}-${RELEASE}-${BRANCH}.img"
fi
OUTPUT="$(realpath -m "$OUTPUT")"
mkdir -p "$(dirname "$OUTPUT")"
ARMBIAN_CACHE_DIR="$(realpath -m "$ARMBIAN_CACHE_DIR")"
mkdir -p "$ARMBIAN_CACHE_DIR"

BUILD_TMP_DIR="${FXROUTE_ARMBIAN_TMPDIR:-$ARMBIAN_CACHE_DIR/tmp}"
mkdir -p "$BUILD_TMP_DIR"
WORK_DIR="$(mktemp -d "$BUILD_TMP_DIR/image.XXXXXX")"
armbian_dir="$WORK_DIR/armbian-build"
SOURCE_ARCHIVE="$WORK_DIR/source.tar"
WORK_TOKEN="${WORK_DIR##*.}"
cleanup() {
  if [[ "$KEEP_WORK" -eq 0 ]]; then
    # A Docker build may leave root-owned files in the work directory that
    # an unprivileged cleanup cannot remove; that must not fail the build.
    if ! rm -rf -- "$WORK_DIR"; then
      printf '[armbian][warn] could not remove build work directory (root-owned leftovers may remain): %s\n' "$WORK_DIR" >&2
    fi
  else
    printf '[armbian] keeping work directory: %s\n' "$WORK_DIR"
  fi
}
trap cleanup EXIT

prepare_armbian_checkout() {
  local repository="$ARMBIAN_SOURCE_DIR"
  local cache_repo="$ARMBIAN_CACHE_DIR/build"
  local use_cache=0

  if [[ -z "$repository" ]]; then
    if [[ ! -d "$cache_repo/.git" ]]; then
      printf '[armbian] cloning %s\n' "$ARMBIAN_REPOSITORY"
      git clone --no-checkout "$ARMBIAN_REPOSITORY" "$cache_repo"
    fi
    repository="$cache_repo"
    use_cache=1
  fi

  repository="$(realpath -m "$repository")"
  [[ -d "$repository/.git" ]] || die "Armbian source is not a Git checkout: $repository"

  local shallow_repository=""
  local partial_repository=0
  shallow_repository="$(git -C "$repository" rev-parse --is-shallow-repository 2>/dev/null || true)"
  if git -C "$repository" config --get extensions.partialClone >/dev/null 2>&1 \
    || git -C "$repository" config --get remote.origin.promisor >/dev/null 2>&1; then
    partial_repository=1
  fi
  if [[ "$use_cache" -eq 1 ]]; then
    # Older managed caches were shallow or blob-filtered. Hydrate them once
    # before cloning so the build checkout always has a complete history/tree.
    if [[ "$shallow_repository" == "true" ]]; then
      git -C "$repository" fetch --unshallow origin
    fi
    if [[ "$partial_repository" -eq 1 ]]; then
      git -C "$repository" fetch --refetch --no-filter origin "$ARMBIAN_BUILD_REF"
      git -C "$repository" config --unset-all extensions.partialClone || true
      git -C "$repository" config --unset-all remote.origin.promisor || true
      git -C "$repository" config --unset-all remote.origin.partialclonefilter || true
    fi
    shallow_repository="$(git -C "$repository" rev-parse --is-shallow-repository 2>/dev/null || true)"
    partial_repository=0
    if git -C "$repository" config --get extensions.partialClone >/dev/null 2>&1 \
      || git -C "$repository" config --get remote.origin.promisor >/dev/null 2>&1; then
      partial_repository=1
    fi
  fi
  [[ "$shallow_repository" != "true" ]] \
    || die "Armbian source must not be a shallow checkout: $repository"
  [[ "$partial_repository" -eq 0 ]] \
    || die "Armbian source must not be a partial checkout: $repository"

  if ! git -C "$repository" cat-file -e "${ARMBIAN_BUILD_REF}^{commit}" 2>/dev/null; then
    [[ "$repository" == "$cache_repo" ]] \
      || die "Armbian source does not contain pinned commit $ARMBIAN_BUILD_REF: $repository"
    git -C "$repository" fetch --depth=1 origin "$ARMBIAN_BUILD_REF"
  fi
  git -C "$repository" cat-file -e "${ARMBIAN_BUILD_REF}^{commit}" \
    || die "Pinned Armbian commit is unavailable: $ARMBIAN_BUILD_REF"

  # A blob-filtered cache has the commit and tree objects but no working tree
  # blobs. Hydrate it before making a local clone; local clones cannot fetch
  # omitted promisor blobs from the source repository.
  if [[ "$use_cache" -eq 1 ]]; then
    git -C "$repository" checkout --detach "$ARMBIAN_BUILD_REF"
  fi

  git clone --local --no-hardlinks "$repository" "$armbian_dir"
  (
    cd "$armbian_dir"
    git checkout --detach "$ARMBIAN_BUILD_REF"
  )
  [[ "$(git -C "$armbian_dir" rev-parse HEAD)" == "$ARMBIAN_BUILD_REF" ]] \
    || die "Armbian checkout is not at the pinned commit"
}

create_source_archive() {
  printf '[armbian] creating deterministic FXRoute source archive\n'
  git -C "$ROOT_DIR" ls-files -z \
    | tar --directory="$ROOT_DIR" --create --file="$SOURCE_ARCHIVE" \
        --sort=name --mtime="@$SOURCE_DATE_EPOCH" --owner=0 --group=0 \
        --numeric-owner --pax-option=delete=atime,delete=ctime \
        --no-recursion --null --verbatim-files-from --files-from=-
  tar --list --file="$SOURCE_ARCHIVE" >/dev/null \
    || die "Could not read generated FXRoute source archive"
}

board_config_path() {
  local candidate=""
  local board_type=""
  local -a board_types=("conf" "wip" "csc" "eos" "tvb")
  # Search config/boards/$build_board.conf along with the other board formats.
  for board_type in "${board_types[@]}"; do
    candidate="$armbian_dir/config/boards/$build_board.$board_type"
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

board_family() {
  local config_path="$1"
  awk -F= '
    /^[[:space:]]*(declare[[:space:]]+-g[[:space:]]+)?(export[[:space:]]+)?BOARDFAMILY[[:space:]]*=/ {
      value = $2
      sub(/[[:space:]]+#.*/, "", value)
      gsub(/[[:space:];"]/, "", value)
      gsub(/\047/, "", value)
      print value
      exit
    }
  ' "$config_path"
}

kernel_ref_for_rpi() {
  case "$BRANCH" in
    legacy) printf 'commit:1138716fb8a796625e519982e53a7b3c89e76ca4\n' ;;
    current) printf 'commit:b3aefe19d14cf15f2e41dfd269fa0ca6198dacd2\n' ;;
    edge) printf 'commit:6668e5a5b241b7418d5bc97595f891be20d5c578\n' ;;
    *) return 1 ;;
  esac
}

write_build_configuration() {
  local userpatches="$armbian_dir/userpatches"
  local config_path=""
  local family=""

  config_path="$(board_config_path || true)"
  [[ -n "$config_path" ]] || die "No Armbian board config found for $build_board"
  mkdir -p "$userpatches/overlay"

  cat > "$userpatches/config-fxroute.conf" <<'EOF'
# FXRoute requires a minimal, headless, systemd user session.
BUILD_MINIMAL=yes
BUILD_DESKTOP=no
# Fixed 8 GiB image: the first boot installs FXRoute (apt, venv, pip, native
# DSP build) on top of the base rootfs; the derived size left no headroom.
FIXED_IMAGE_SIZE=8192
NETWORKING_STACK=systemd-networkd
export E2FSPROGS_FAKE_TIME="${E2FSPROGS_FAKE_TIME:-${SOURCE_DATE_EPOCH:-0}}"
CONSOLE_AUTOLOGIN=no
DESKTOP_AUTOLOGIN=no
KERNEL_CONFIGURE=no
KERNEL_BTF=no
BETA=no
ROOTPWD="!"
BOOT_LOGO=no
COMPRESS_OUTPUTIMAGE=none
add_packages_to_image ca-certificates curl dbus-user-session dnsmasq-base hostapd iw openssl openssh-server python3 sudo tar wpasupplicant

function prepare_root_device__fxroute_reproducible_rootfs() {
  [[ "${ROOTFS_TYPE}" == "ext4" ]] || return 0
  mkopts[ext4]+=" -E hash_seed=01234567-89ab-cdef-0123-456789abcdef"
  mkopts[ext4]+=" -U 01234567-89ab-cdef-0123-456789abcdef"
}

function render_bootscript_template() { (
  typeset BOOTSCRIPT_TEMPLATE__CREATE_DATE
  typeset SHELL_FORMAT

  BOOTSCRIPT_TEMPLATE__CREATE_DATE="$(date -u -d "@${SOURCE_DATE_EPOCH:-0}" -Ru)"

  bootscript_export_display_console
  bootscript_export_serial_console

  SHELL_FORMAT="$(set | sed -En '/^BOOTSCRIPT_TEMPLATE__/ { s/=.*$//; s/^/$/; p; }')"
  display_alert "Bootscript template variables to be rendered" "${SHELL_FORMAT:-N/A}" "debug"

  export $(set | sed -En '/^BOOTSCRIPT_TEMPLATE__/s/=.*$//p')
  envsubst "'${SHELL_FORMAT}'"
); }

function config_post_debootstrap_tweaks() {
  local wrapper_dir="${SRC}/.tmp/fxroute-rsync"
  local rsync_binary=""
  local mkfs_fat_binary=""

  # The pinned Armbian image copier applies -X to the ext4 /boot tree even
  # when /boot/firmware is a nested FAT partition.
  rsync_binary="$(type -P rsync || true)"
  [[ -n "$rsync_binary" ]] || {
    printf '%s\n' "Could not locate rsync for the Armbian image-copy wrapper" >&2
    return 1
  }
  mkdir -p "$wrapper_dir"
  export FXROUTE_RSYNC_BINARY="$rsync_binary"
  cat > "$wrapper_dir/rsync" <<'RSYNC_WRAPPER'
#!/usr/bin/env bash
boot_copy=0
for arg in "$@"; do
  case "$arg" in
    -*) ;;
    */boot|*/boot/) boot_copy=1 ;;
  esac
done
if [[ "$boot_copy" -eq 1 ]]; then
  exec "$FXROUTE_RSYNC_BINARY" --filter='-x security.*' --filter='-x system.*' "$@"
fi
exec "$FXROUTE_RSYNC_BINARY" "$@"
RSYNC_WRAPPER
  chmod 755 "$wrapper_dir/rsync"
  PATH="$wrapper_dir:$PATH"
  export PATH

  mkfs_fat_binary="$(type -P mkfs.fat || true)"
  if [[ -n "$mkfs_fat_binary" ]]; then
    export FXROUTE_MKFS_FAT_BINARY="$mkfs_fat_binary"
    cat > "$wrapper_dir/mkfs.fat" <<'MKFS_FAT_WRAPPER'
#!/usr/bin/env bash
exec "$FXROUTE_MKFS_FAT_BINARY" --invariant "$@"
MKFS_FAT_WRAPPER
    chmod 755 "$wrapper_dir/mkfs.fat"
    ln -sfn -- mkfs.fat "$wrapper_dir/mkfs.vfat"
  fi

  mkdir -p "${SDCARD}/etc"
  # A Pi without an RTC needs a valid initial clock for HTTPS package
  # downloads; keep runtime clock data separate from reproducible file times.
  date -u '+%Y-%m-%d %H:%M:%S' > "${SDCARD}/etc/fake-hwclock.data"
}
EOF

  family="$(board_family "$config_path")"
  [[ -n "$family" ]] || die "Could not determine Armbian family for $build_board"
  if [[ -z "$KERNEL_REF" && "$build_board" == "rpi4b" ]]; then
    KERNEL_REF="$(kernel_ref_for_rpi || true)"
  fi
  [[ -z "$KERNEL_REF" || "$KERNEL_REF" =~ ^(branch|tag|commit):[A-Za-z0-9._/+~=-]+$ ]] \
    || die "Kernel ref must use branch:, tag:, or commit: syntax"
  mkdir -p "$userpatches/config/sources/families"
  {
    printf '%s\n' '# FXRoute keeps the image headless and reproducible after the board family is sourced.'
    printf '%s\n' 'declare -g NETWORKING_STACK="systemd-networkd"'
    if [[ -n "$KERNEL_REF" ]]; then
      printf 'declare -g KERNELBRANCH="%s"\n' "$KERNEL_REF"
    fi
  } > "$userpatches/config/sources/families/$family.conf"

  cp -- "$ROOT_DIR/armbian/customize-image.sh" "$userpatches/customize-image.sh"
  chmod 755 "$userpatches/customize-image.sh"
  cp -- "$SOURCE_ARCHIVE" "$userpatches/overlay/source.tar"
  cp -- "$ROOT_DIR/armbian/first-boot-install.sh" "$userpatches/overlay/first-boot-install.sh"
  cp -- "$ROOT_DIR/armbian/fxroute-armbian-first-boot.service" \
    "$userpatches/overlay/fxroute-armbian-first-boot.service"
  cp -- "$ROOT_DIR/armbian/armbian-web-config.py" \
    "$userpatches/overlay/armbian-web-config.py"
  cp -- "$ROOT_DIR/armbian/armbian-web-config.service" \
    "$userpatches/overlay/armbian-web-config.service"
  printf 'ARMBIAN_WEB_CONFIG_AP_PASSWORD_VERIFIER_B64=%s\n' \
    "$AP_PASSWORD_VERIFIER_B64" \
    > "$userpatches/overlay/armbian-web-config.env"
  chmod 755 "$userpatches/overlay/first-boot-install.sh"
  chmod 755 "$userpatches/overlay/armbian-web-config.py"
  chmod 644 "$userpatches/overlay/fxroute-armbian-first-boot.service"
  chmod 644 "$userpatches/overlay/armbian-web-config.service"
  chmod 600 "$userpatches/overlay/armbian-web-config.env"
}

copy_image_output() {
  local image_dir="$armbian_dir/output/images"
  local image=""
  local disk_output="$OUTPUT"
  local artifact_stem=""
  local uboot=""
  local images=()

  while IFS= read -r -d '' image; do
    images+=("$image")
  done < <(find "$image_dir" -maxdepth 1 -type f \( -name '*.img' -o -name '*.img.xz' -o -name '*.img.qcow2' \) -print0 2>/dev/null | sort -z)
  [[ ${#images[@]} -eq 1 ]] \
    || die "Expected exactly one Armbian disk image in $image_dir, found ${#images[@]}"
  image="${images[0]}"

  case "$image" in
    *.img) cp -- "$image" "$disk_output" ;;
    *.img.xz) xz --decompress --stdout "$image" > "$disk_output" ;;
    *.img.qcow2)
      case "$OUTPUT" in
        *.img.qcow2) disk_output="$OUTPUT" ;;
        *.img) disk_output="$OUTPUT.qcow2" ;;
        *) disk_output="$OUTPUT.img.qcow2" ;;
      esac
      cp -- "$image" "$disk_output"
      ;;
    *) die "Unsupported Armbian image output: $image" ;;
  esac
  case "$disk_output" in
    *.img.qcow2) artifact_stem="${disk_output%.img.qcow2}" ;;
    *.img.xz) artifact_stem="${disk_output%.img.xz}" ;;
    *.img) artifact_stem="${disk_output%.img}" ;;
    *) artifact_stem="$disk_output" ;;
  esac
  while IFS= read -r -d '' uboot; do
    cp -- "$uboot" "${artifact_stem}.u-boot.bin"
  done < <(find "$image_dir" -maxdepth 1 -type f -name '*.u-boot.bin' -print0 2>/dev/null | sort -z)
  sha256sum "$disk_output" > "$disk_output.sha256"
  printf '[armbian] wrote %s\n' "$disk_output"
  printf '[armbian] sha256 %s\n' "$(awk '{print $1}' "$disk_output.sha256")"
}

prepare_armbian_checkout
create_source_archive
write_build_configuration

printf '[armbian] building requested board %s as %s (%s/%s)\n' \
  "$requested_board" "$build_board" "$RELEASE" "$BRANCH"
(
  cd "$armbian_dir"
  export SOURCE_DATE_EPOCH
  export TERM="${TERM:-xterm-256color}"
  export ARMBIAN_BUILD_UUID="fxroute-${requested_board}-${RELEASE}-${BRANCH}-${WORK_TOKEN}"
  "$armbian_dir/compile.sh" fxroute \
    "BOARD=$build_board" \
    "BRANCH=$BRANCH" \
    "RELEASE=$RELEASE" \
    "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" \
    "E2FSPROGS_FAKE_TIME=$E2FSPROGS_FAKE_TIME"
)

copy_image_output
