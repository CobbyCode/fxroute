#!/usr/bin/env bash
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="https://download.opensuse.org/distribution/leap/16.0/offline/Leap-16.0-offline-installer-x86_64.install.iso"
BASE_SHA512="94411793a1878b3558211c8bbe4f3823c3c5212cc2dd9f9e4532a18be7eddd3be14db5a3bb47a2f36dd957e190ea706cd45af16bce218dfc00f71e64a8469971"
BASE_ISO="${FXROUTE_BASE_ISO:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/Leap-16.0-offline-installer-x86_64.install.iso}"
OUTPUT="${FXROUTE_ISO_OUTPUT:-$ROOT_DIR/dist/fxroute-leap-16-x86_64.iso}"
LIVE_SQUASH="${FXROUTE_LIVE_SQUASH:-}"
SKIP_LIVE=0
KEEP_WORK=0
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"

usage() {
  cat <<EOF
Usage: $0 [options]

Build a bootable openSUSE Leap 16 x86_64 ISO with the FXRoute profiles.

The profiles contain no user passwords or SSH keys. The account, the
network/WLAN setup, the target disk, and locale/keyboard/timezone are
chosen interactively in Agama; the ISO itself ships no credentials.

The ISO also ships a non-persistent "Try FXRoute" live system
(/LiveFX/squashfs.img, RAM overlay, no Agama, no installer).

Options:
  --base-iso PATH          Use PATH instead of the cached/downloaded Leap ISO
  --output PATH            Write the resulting ISO to PATH
  --live-squash PATH       Use PATH as the prebuilt LiveFX squashfs image
                           (default: build via iso/scripts/build-live-root.sh)
  --no-live                Skip the Try FXRoute live system (dev/test only)
  --keep-work              Keep the temporary media staging directory
  -h, --help               Show this help
EOF
}

die() {
  printf '[iso][error] %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base-iso)
      [[ $# -ge 2 ]] || die "--base-iso requires a path"
      BASE_ISO="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || die "--output requires a path"
      OUTPUT="$2"
      shift 2
      ;;
    --live-squash)
      [[ $# -ge 2 ]] || die "--live-squash requires a path"
      LIVE_SQUASH="$2"
      shift 2
      ;;
    --no-live)
      SKIP_LIVE=1
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

command -v git >/dev/null 2>&1 || die "git is required to create the source archive"
command -v tar >/dev/null 2>&1 || die "tar is required to create the source archive"
command -v sha512sum >/dev/null 2>&1 || die "sha512sum is required to verify the Leap ISO"
command -v curl >/dev/null 2>&1 || die "curl is required to download the Leap ISO"
command -v python3 >/dev/null 2>&1 || die "python3 is required to render the profiles"
command -v isoinfo >/dev/null 2>&1 || die "isoinfo is required to validate the boot labels"
MKMEDIA="${MKMEDIA:-}"
if [[ -z "$MKMEDIA" ]]; then
  MKMEDIA="$(command -v mkmedia || command -v mksusecd || true)"
fi
if [[ "$MKMEDIA" != */* ]]; then
  MKMEDIA="$(command -v "$MKMEDIA" || true)"
fi
[[ -n "$MKMEDIA" && -x "$MKMEDIA" ]] \
  || die "mkmedia is required (install the mkmedia package; mksusecd is accepted as a compatibility name)"

REAL_MKISOFS="${FXROUTE_REAL_MKISOFS:-$(command -v mkisofs || true)}"
if [[ -n "${FXROUTE_REAL_ISOHYBRID:-}" ]]; then
  REAL_ISOHYBRID="$FXROUTE_REAL_ISOHYBRID"
else
  MKMEDIA_ROOT="$(cd -- "$(dirname -- "$MKMEDIA")/.." && pwd)"
  MKMEDIA_ISOHYBRID="$MKMEDIA_ROOT/libexec/mkmedia/isohybrid"
  if [[ -x "$MKMEDIA_ISOHYBRID" ]]; then
    REAL_ISOHYBRID="$MKMEDIA_ISOHYBRID"
  else
    REAL_ISOHYBRID="$(command -v isohybrid || true)"
  fi
fi
[[ -n "$REAL_MKISOFS" && -x "$REAL_MKISOFS" ]] || die "mkisofs is required"
[[ -n "$REAL_ISOHYBRID" && -x "$REAL_ISOHYBRID" ]] || die "isohybrid is required"

if [[ ! -f "$BASE_ISO" ]]; then
  mkdir -p "$(dirname "$BASE_ISO")"
  partial="${BASE_ISO}.part"
  printf '[iso] downloading %s\n' "$BASE_URL"
  curl --fail --location --retry 3 --retry-delay 2 --output "$partial" "$BASE_URL"
  mv -- "$partial" "$BASE_ISO"
fi

actual_sha512="$(sha512sum "$BASE_ISO" | awk '{print $1}')"
[[ "$actual_sha512" == "$BASE_SHA512" ]] \
  || die "Leap ISO checksum mismatch for $BASE_ISO (expected $BASE_SHA512, got $actual_sha512)"

OUTPUT="$(realpath -m "$OUTPUT")"
BASE_ISO="$(realpath -m "$BASE_ISO")"
mkdir -p "$(dirname "$OUTPUT")"
[[ "$OUTPUT" != "$BASE_ISO" ]] || die "The output path must not overwrite the base ISO"

[[ "$SOURCE_DATE_EPOCH" =~ ^[0-9]+$ ]] || die "SOURCE_DATE_EPOCH must be a non-negative integer"
BUILD_TMP_DIR="${FXROUTE_ISO_TMPDIR:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/build}"
mkdir -p "$BUILD_TMP_DIR"
WORK_DIR="$(mktemp -d "$BUILD_TMP_DIR/fxroute-leap16-iso.XXXXXX")"
MKMEDIA_TMP="$WORK_DIR/mkmedia-tmp"
cleanup() {
  if [[ "$KEEP_WORK" -eq 0 ]]; then
    rm -rf -- "$WORK_DIR"
  else
    printf '[iso] keeping work directory: %s\n' "$WORK_DIR"
  fi
}
trap cleanup EXIT
mkdir -p "$MKMEDIA_TMP"

MKMEDIA_PATCH="$ROOT_DIR/iso/mkmedia.patch"
MKMEDIA_PATCHED="$WORK_DIR/mkmedia"
MKMEDIA_TOOL_DIR="$WORK_DIR/mkmedia-tools"
[[ -f "$MKMEDIA_PATCH" ]] || die "mkmedia patch is missing: $MKMEDIA_PATCH"
mkdir -p "$MKMEDIA_TOOL_DIR"
cp -- "$MKMEDIA" "$MKMEDIA_PATCHED"
cp -- "$ROOT_DIR/iso/mkisofs-reproducible.sh" "$MKMEDIA_TOOL_DIR/mkisofs"
cp -- "$ROOT_DIR/iso/isohybrid-reproducible.sh" "$MKMEDIA_TOOL_DIR/isohybrid"
chmod 755 "$MKMEDIA_PATCHED" "$MKMEDIA_TOOL_DIR/mkisofs" "$MKMEDIA_TOOL_DIR/isohybrid"
if command -v patch >/dev/null 2>&1; then
  patch --batch --forward "$MKMEDIA_PATCHED" < "$MKMEDIA_PATCH" >/dev/null \
    || die "mkmedia patch does not apply to $MKMEDIA"
else
  python3 - "$MKMEDIA_PATCHED" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
text = path.read_text()
replacements = (
    (
        '$ENV{PATH} = "$LIBEXECDIR/mkmedia:/usr/bin:/bin:/usr/sbin:/sbin";',
        '$ENV{PATH} = ($ENV{FXROUTE_MKMEDIA_TOOL_DIR} ? "$ENV{FXROUTE_MKMEDIA_TOOL_DIR}:" : "") .\n'
        '  "$LIBEXECDIR/mkmedia:/usr/bin:/bin:/usr/sbin:/sbin";',
    ),
    (
        '      system "mkdir -p \'$tmp_new/$1\'; cp \'$f\' \'$tmp_new/$1\'";',
        '      system "mkdir -p \'$tmp_new/$1\'; cp -p \'$f\' \'$tmp_new/$1\'";',
    ),
    (
        '      system "cp \'$f\' \'$tmp_new\'";',
        '      system "cp -p \'$f\' \'$tmp_new\'";',
    ),
    (
        '          $ent =~ s/$inst_regexp/$1$2 - $opt_new_boot_entry$1/;',
        '          $ent =~ s/$inst_regexp/$1$opt_new_boot_entry$1/;',
    ),
)
for old, new in replacements:
    if text.count(old) != 1:
        raise SystemExit(f"mkmedia patch context is missing or ambiguous: {old}")
    text = text.replace(old, new)
path.write_text(text)
PY
fi
MKMEDIA="$MKMEDIA_PATCHED"
FXROUTE_MKMEDIA_TOOL_DIR="$MKMEDIA_TOOL_DIR"
export FXROUTE_MKMEDIA_TOOL_DIR
export FXROUTE_REAL_MKISOFS="$REAL_MKISOFS"
export FXROUTE_REAL_ISOHYBRID="$REAL_ISOHYBRID"

SOURCE_ARCHIVE="$WORK_DIR/source.tar"
STAGE_DIR="$WORK_DIR/stage"
mkdir -p "$STAGE_DIR/fxroute/profiles" "$STAGE_DIR/fxroute/scripts" "$STAGE_DIR/LiveFX"

printf '[iso] creating deterministic source.tar\n'
git -C "$ROOT_DIR" ls-files -z \
  | tar --directory="$ROOT_DIR" --create --file="$SOURCE_ARCHIVE" \
      --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 \
      --numeric-owner --pax-option=delete=atime,delete=ctime \
      --no-recursion --null --verbatim-files-from \
      --files-from=-
tar --list --file="$SOURCE_ARCHIVE" >/dev/null || die "Could not read generated source archive"

cp -- "$SOURCE_ARCHIVE" "$STAGE_DIR/fxroute/source.tar"
# Release ISOs must be built from a pushed commit: the first-boot setup
# fetches origin/main and checks out the built commit; if it is not on the
# remote the guest would otherwise fall back to an older release.  Dev/test
# ISOs may opt out explicitly.
if ! git -C "$ROOT_DIR" merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  if [[ "${FXROUTE_ISO_ALLOW_UNPUSHED:-0}" != "1" ]]; then
    die "HEAD is not an ancestor of origin/main; push the ISO source commit first (or set FXROUTE_ISO_ALLOW_UNPUSHED=1 for a dev/test ISO)."
  fi
  printf '[iso] warning: building from an unpushed commit; first-boot updates will use a local snapshot commit\n'
fi
git -C "$ROOT_DIR" rev-parse HEAD > "$STAGE_DIR/fxroute/build-commit"
cp -- "$ROOT_DIR/iso/scripts/first-boot-install.sh" "$STAGE_DIR/fxroute/scripts/first-boot-install.sh"
chmod 755 "$STAGE_DIR/fxroute/scripts/first-boot-install.sh"

stage_profile() {
  local template="$1"
  local output="$2"
  python3 - "$template" "$output" <<'PY'
import json
import pathlib
import sys

template, output = sys.argv[1:]
text = pathlib.Path(template).read_text()
if "__FXROUTE_PASSWORD_HASH__" in text or "__FXROUTE_SSH_PUBLIC_KEY__" in text:
    raise SystemExit("profile must not contain credential placeholders")
json.loads(text)
pathlib.Path(output).write_text(text)
PY
}

stage_profile "$ROOT_DIR/iso/profiles/headless.jsonnet" "$STAGE_DIR/fxroute/profiles/headless.jsonnet"
stage_profile "$ROOT_DIR/iso/profiles/desktop.jsonnet" "$STAGE_DIR/fxroute/profiles/desktop.jsonnet"

LIVE_BOOT_OPTIONS_FILE="$ROOT_DIR/iso/scripts/live-boot-options.txt"
[[ -f "$LIVE_BOOT_OPTIONS_FILE" ]] || die "live boot options are missing: $LIVE_BOOT_OPTIONS_FILE"
LIVE_BOOT_OPTIONS="$(tr '\n' ' ' < "$LIVE_BOOT_OPTIONS_FILE")"
[[ "$LIVE_BOOT_OPTIONS" == *"rd.live.dir=LiveFX"* ]] || die "live boot options must select LiveFX"
[[ "$LIVE_BOOT_OPTIONS" != *"inst.auto"* ]] || die "live boot options must not contain inst.auto"

if [[ "$SKIP_LIVE" -eq 1 ]]; then
  printf '[iso] skipping Try FXRoute live system (--no-live)\n'
else
  if [[ -n "$LIVE_SQUASH" ]]; then
    [[ -f "$LIVE_SQUASH" ]] || die "live squash image not found: $LIVE_SQUASH"
    printf '[iso] staging prebuilt live squash: %s\n' "$LIVE_SQUASH"
    cp -- "$LIVE_SQUASH" "$STAGE_DIR/LiveFX/squashfs.img"
  else
    printf '[iso] building LiveFX squashfs image\n'
    # Version-exact kernel modules for the live root: it boots this ISO's
    # kernel, so pointer/input drivers must come from the kernel-default
    # RPM on this very ISO (repo kernels no longer match). Without them
    # USB mice/trackpads stay dead after switch-root. Fail fast if absent.
    # NOTE: materialize the listing first (see final-grub.cfg note below).
    isoinfo -R -i "$BASE_ISO" -f > "$WORK_DIR/base-isol.txt" 2>/dev/null \
      || die "could not list base ISO contents"
    KERNEL_RPM_ISO_PATH="$(grep -E '/kernel-default-[0-9][^/]*\.x86_64\.rpm$' "$WORK_DIR/base-isol.txt" | grep -v -E -- '-extra-|-optional-' | head -n 1 || true)"
    [[ -n "$KERNEL_RPM_ISO_PATH" ]] || die "kernel-default RPM not found on base ISO ($BASE_ISO)"
    printf '[iso] extracting live kernel modules: %s\n' "$KERNEL_RPM_ISO_PATH"
    isoinfo -R -i "$BASE_ISO" -x "$KERNEL_RPM_ISO_PATH" > "$WORK_DIR/kernel-default.rpm" \
      || die "could not extract kernel-default RPM from base ISO"
    "$ROOT_DIR/iso/scripts/build-live-root.sh" --output "$STAGE_DIR/LiveFX/squashfs.img" \
      --kernel-rpm "$WORK_DIR/kernel-default.rpm"
  fi
  [[ -f "$STAGE_DIR/LiveFX/squashfs.img" ]] || die "live squash staging failed"
fi

TRY_ISO="$WORK_DIR/try.iso"
HEADLESS_ISO="$WORK_DIR/headless.iso"
FINAL_ISO="$WORK_DIR/fxroute-leap-16-x86_64.iso"
export SOURCE_DATE_EPOCH

# Keep files added to the base image independent of the build clock. The
# source archive already has normalized tar metadata; this also covers the
# profile and first-boot files as seen by mkmedia.
find "$STAGE_DIR" -exec touch -h --date="@$SOURCE_DATE_EPOCH" {} +

mkmedia_args=(--mkisofs --mbr --no-mount-iso --no-sign --no-check --no-digest --tmp-dir "$MKMEDIA_TMP")

if [[ "$SKIP_LIVE" -eq 1 ]]; then
  LIVE_BASE_ISO="$BASE_ISO"
else
  printf '[iso] adding Try FXRoute live boot entry\n'
  "$MKMEDIA" \
    "${mkmedia_args[@]}" \
    --create "$TRY_ISO" \
    --add-entry "Try FXRoute" \
    --boot "$LIVE_BOOT_OPTIONS" \
    "$BASE_ISO" "$STAGE_DIR"
  LIVE_BASE_ISO="$TRY_ISO"
fi

printf '[iso] adding FXRoute Headless boot entry\n'
"$MKMEDIA" \
  "${mkmedia_args[@]}" \
  --create "$HEADLESS_ISO" \
  --add-entry "FXRoute Headless" \
  --boot "inst.auto=device:/fxroute/profiles/headless.jsonnet inst.install=0 inst.finish=reboot" \
  "$LIVE_BASE_ISO" "$STAGE_DIR"

printf '[iso] adding FXRoute Desktop boot entry\n'
"$MKMEDIA" \
  "${mkmedia_args[@]}" \
  --create "$FINAL_ISO" \
  --add-entry "FXRoute Desktop" \
  --boot "inst.auto=device:/fxroute/profiles/desktop.jsonnet inst.install=0 inst.finish=reboot" \
  "$HEADLESS_ISO" "$STAGE_DIR"

isoinfo -i "$FINAL_ISO" -R -x /boot/grub2/grub.cfg > "$WORK_DIR/final-grub.cfg"
grep -Fq 'menuentry "FXRoute Desktop"' "$WORK_DIR/final-grub.cfg" \
  || die "final ISO is missing the FXRoute Desktop boot label"
grep -Fq 'menuentry "FXRoute Headless"' "$WORK_DIR/final-grub.cfg" \
  || die "final ISO is missing the FXRoute Headless boot label"
if [[ "$SKIP_LIVE" -eq 0 ]]; then
  grep -Fq 'menuentry "Try FXRoute"' "$WORK_DIR/final-grub.cfg" \
    || die "final ISO is missing the Try FXRoute boot label"
  # Try entry must boot the LiveFX image with RAM overlay, without Agama/installer.
  grep -Fq 'rd.live.dir=LiveFX' "$WORK_DIR/final-grub.cfg" \
    || die "Try FXRoute entry is missing rd.live.dir=LiveFX"
  grep -Fq 'rd.live.overlay.overlayfs=1' "$WORK_DIR/final-grub.cfg" \
    || die "Try FXRoute entry is missing the RAM overlay flag"
  grep -Fq 'fxroute.live=1' "$WORK_DIR/final-grub.cfg" \
    || die "Try FXRoute entry is missing fxroute.live=1"
  if grep -A6 'menuentry "Try FXRoute"' "$WORK_DIR/final-grub.cfg" | grep -Fq 'inst.auto'; then
    die "Try FXRoute entry must not contain inst.auto"
  fi
  # NOTE: avoid `isoinfo | grep -q` under `set -o pipefail` (isoinfo exits 141
  # on SIGPIPE when grep quits early); materialize the listing first.
  isoinfo -i "$FINAL_ISO" -R -l > "$WORK_DIR/final-isol.txt" 2>/dev/null || true
  grep -Fq "LiveFX" "$WORK_DIR/final-isol.txt" \
    || die "final ISO is missing the LiveFX directory"
  grep -Fq "squashfs.img" "$WORK_DIR/final-isol.txt" \
    || die "final ISO is missing the LiveFX squashfs image"
fi
if grep -Fq ' - FXRoute Desktop"' "$WORK_DIR/final-grub.cfg" ||
   grep -Fq ' - FXRoute Headless"' "$WORK_DIR/final-grub.cfg" ||
   grep -Fq ' - Try FXRoute"' "$WORK_DIR/final-grub.cfg"; then
  die "final ISO still contains the unnormalized FXRoute boot labels"
fi
[[ "$(od -An -tx1 -j 510 -N 2 "$FINAL_ISO" | tr -d '[:space:]')" == "55aa" ]] \
  || die "final ISO is missing the hybrid MBR"

mv -- "$FINAL_ISO" "$OUTPUT"
printf '[iso] wrote %s\n' "$OUTPUT"
printf '[iso] sha512 %s\n' "$(sha512sum "$OUTPUT" | awk '{print $1}')"
