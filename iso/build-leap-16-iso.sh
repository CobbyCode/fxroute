#!/usr/bin/env bash
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="https://download.opensuse.org/distribution/leap/16.0/offline/Leap-16.0-offline-installer-x86_64.install.iso"
BASE_SHA512="94411793a1878b3558211c8bbe4f3823c3c5212cc2dd9f9e4532a18be7eddd3be14db5a3bb47a2f36dd957e190ea706cd45af16bce218dfc00f71e64a8469971"
BASE_ISO="${FXROUTE_BASE_ISO:-${XDG_CACHE_HOME:-$HOME/.cache}/fxroute/Leap-16.0-offline-installer-x86_64.install.iso}"
OUTPUT="${FXROUTE_ISO_OUTPUT:-$ROOT_DIR/dist/fxroute-leap-16-x86_64.iso}"
PASSWORD_HASH="${FXROUTE_PASSWORD_HASH:-}"
SSH_PUBLIC_KEY="${FXROUTE_SSH_PUBLIC_KEY:-}"
SSH_PUBLIC_KEY_FILE=""
KEEP_WORK=0
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"

usage() {
  cat <<EOF
Usage: $0 [options]

Build a bootable openSUSE Leap 16 x86_64 ISO with the FXRoute profiles.

Options:
  --base-iso PATH          Use PATH instead of the cached/downloaded Leap ISO
  --output PATH            Write the resulting ISO to PATH
  --password-hash HASH     SHA-512 crypt hash for the fxroute user
  --ssh-public-key-file P  Install P as the root SSH public key
  --keep-work              Keep the temporary media staging directory
  -h, --help               Show this help

The same values can be supplied with FXROUTE_PASSWORD_HASH and
FXROUTE_SSH_PUBLIC_KEY. The password hash and public key are rendered only
into the generated ISO and are never written to the repository.
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
    --password-hash)
      [[ $# -ge 2 ]] || die "--password-hash requires a value"
      PASSWORD_HASH="$2"
      shift 2
      ;;
    --ssh-public-key-file)
      [[ $# -ge 2 ]] || die "--ssh-public-key-file requires a path"
      SSH_PUBLIC_KEY_FILE="$2"
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

if [[ -n "$SSH_PUBLIC_KEY_FILE" ]]; then
  [[ -f "$SSH_PUBLIC_KEY_FILE" ]] || die "SSH public key file does not exist: $SSH_PUBLIC_KEY_FILE"
  SSH_PUBLIC_KEY="$(<"$SSH_PUBLIC_KEY_FILE")"
  SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY%$'\n'}"
  SSH_PUBLIC_KEY="${SSH_PUBLIC_KEY%$'\r'}"
fi

[[ "$PASSWORD_HASH" =~ ^\$6\$[A-Za-z0-9./]+\$[A-Za-z0-9./]+$ ]] \
  || die "Provide FXROUTE_PASSWORD_HASH as a SHA-512 crypt value (for example from openssl passwd -6)"
[[ "$SSH_PUBLIC_KEY" != *$'\n'* ]] \
  || die "The SSH public key must be one line"
[[ "$SSH_PUBLIC_KEY" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))[[:space:]][A-Za-z0-9+/=]+([[:space:]].*)?$ ]] \
  || die "Provide a supported OpenSSH public key in FXROUTE_SSH_PUBLIC_KEY"

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
mkdir -p "$STAGE_DIR/fxroute/profiles" "$STAGE_DIR/fxroute/scripts"

printf '[iso] creating deterministic source.tar\n'
git -C "$ROOT_DIR" ls-files -z \
  | tar --directory="$ROOT_DIR" --create --file="$SOURCE_ARCHIVE" \
      --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 \
      --numeric-owner --pax-option=delete=atime,delete=ctime \
      --no-recursion --null --verbatim-files-from \
      --files-from=-
tar --list --file="$SOURCE_ARCHIVE" >/dev/null || die "Could not read generated source archive"

cp -- "$SOURCE_ARCHIVE" "$STAGE_DIR/fxroute/source.tar"
cp -- "$ROOT_DIR/iso/scripts/first-boot-install.sh" "$STAGE_DIR/fxroute/scripts/first-boot-install.sh"
chmod 755 "$STAGE_DIR/fxroute/scripts/first-boot-install.sh"

render_profile() {
  local template="$1"
  local output="$2"
  python3 - "$template" "$output" "$PASSWORD_HASH" "$SSH_PUBLIC_KEY" <<'PY'
import json
import pathlib
import sys

template, output, password_hash, ssh_public_key = sys.argv[1:]
text = pathlib.Path(template).read_text()
text = text.replace("__FXROUTE_PASSWORD_HASH__", json.dumps(password_hash)[1:-1])
text = text.replace("__FXROUTE_SSH_PUBLIC_KEY__", json.dumps(ssh_public_key)[1:-1])
json.loads(text)
pathlib.Path(output).write_text(text)
PY
}

render_profile "$ROOT_DIR/iso/profiles/headless.jsonnet" "$STAGE_DIR/fxroute/profiles/headless.jsonnet"
render_profile "$ROOT_DIR/iso/profiles/desktop.jsonnet" "$STAGE_DIR/fxroute/profiles/desktop.jsonnet"

HEADLESS_ISO="$WORK_DIR/headless.iso"
FINAL_ISO="$WORK_DIR/fxroute-leap-16-x86_64.iso"
export SOURCE_DATE_EPOCH

# Keep files added to the base image independent of the build clock. The
# source archive already has normalized tar metadata; this also covers the
# profile and first-boot files as seen by mkmedia.
find "$STAGE_DIR" -exec touch -h --date="@$SOURCE_DATE_EPOCH" {} +

mkmedia_args=(--mkisofs --mbr --no-mount-iso --no-sign --no-check --no-digest --tmp-dir "$MKMEDIA_TMP")

printf '[iso] adding FXRoute Headless boot entry\n'
"$MKMEDIA" \
  "${mkmedia_args[@]}" \
  --create "$HEADLESS_ISO" \
  --add-entry "FXRoute Headless" \
  --boot "inst.auto=device:/fxroute/profiles/headless.jsonnet inst.install=1 inst.finish=reboot" \
  "$BASE_ISO" "$STAGE_DIR"

printf '[iso] adding FXRoute Desktop boot entry\n'
"$MKMEDIA" \
  "${mkmedia_args[@]}" \
  --create "$FINAL_ISO" \
  --add-entry "FXRoute Desktop" \
  --boot "inst.auto=device:/fxroute/profiles/desktop.jsonnet inst.install=1 inst.finish=reboot" \
  "$HEADLESS_ISO" "$STAGE_DIR"

isoinfo -i "$FINAL_ISO" -R -x /boot/grub2/grub.cfg > "$WORK_DIR/final-grub.cfg"
grep -Fq 'menuentry "FXRoute Desktop"' "$WORK_DIR/final-grub.cfg" \
  || die "final ISO is missing the FXRoute Desktop boot label"
grep -Fq 'menuentry "FXRoute Headless"' "$WORK_DIR/final-grub.cfg" \
  || die "final ISO is missing the FXRoute Headless boot label"
if grep -Fq ' - FXRoute Desktop"' "$WORK_DIR/final-grub.cfg" ||
   grep -Fq ' - FXRoute Headless"' "$WORK_DIR/final-grub.cfg"; then
  die "final ISO still contains the unnormalized FXRoute boot labels"
fi
[[ "$(od -An -tx1 -j 510 -N 2 "$FINAL_ISO" | tr -d '[:space:]')" == "55aa" ]] \
  || die "final ISO is missing the hybrid MBR"

mv -- "$FINAL_ISO" "$OUTPUT"
printf '[iso] wrote %s\n' "$OUTPUT"
printf '[iso] sha512 %s\n' "$(sha512sum "$OUTPUT" | awk '{print $1}')"
