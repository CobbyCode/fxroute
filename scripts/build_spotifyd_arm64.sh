#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

SPOTIFYD_VERSION="0.4.2"
SPOTIFYD_ARTIFACT_VERSION="1"
SPOTIFYD_SOURCE_SHA512="03a28037db2389a9415cde985dbf8c639c38d8000da5985178833fc5250249da5798f426e9b6e89c18762ff2d05b1331590905ef818f1430fe1c7e2507020898"
RUSTUP_VERSION="1.28.2"
RUSTUP_SHA512="b4adcc9d587b05b261cdae9bec589d262b7d5326bfad8d100bc8066f29ffdcb332053ae018566a5e8f3b2ee177b9eb1595e2c3712fb0a6e935da49c89a56ca59"
RUST_TOOLCHAIN_VERSION="1.88.0"
UBUNTU_IMAGE="ubuntu:22.04@sha256:8c71efb5d8170edf0965b2ac5e867cc70d3d8f73d1c9c0573d690d6203fc5866"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/dist/spotifyd-arm64}"
ARCHIVE="spotifyd-${SPOTIFYD_VERSION}-linux-aarch64-fxroute-${SPOTIFYD_ARTIFACT_VERSION}.tar.gz"

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required to build the ARM64 artifact\n' >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd -P)"

docker run --rm --interactive --platform linux/arm64 \
  --volume "$OUTPUT_DIR:/out:Z" \
  --env "SPOTIFYD_VERSION=$SPOTIFYD_VERSION" \
  --env "SPOTIFYD_ARTIFACT_VERSION=$SPOTIFYD_ARTIFACT_VERSION" \
  --env "SPOTIFYD_SOURCE_SHA512=$SPOTIFYD_SOURCE_SHA512" \
  --env "RUSTUP_VERSION=$RUSTUP_VERSION" \
  --env "RUSTUP_SHA512=$RUSTUP_SHA512" \
  --env "RUST_TOOLCHAIN_VERSION=$RUST_TOOLCHAIN_VERSION" \
  --env "ARCHIVE=$ARCHIVE" \
  "$UBUNTU_IMAGE" bash -s <<'CONTAINER'
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
export TZ=UTC

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  build-essential \
  pkg-config \
  libssl-dev \
  libdbus-1-dev \
  libasound2-dev \
  libpulse-dev \
  file \
  binutils \
  tar \
  gzip

work="$(mktemp -d -t fxroute-spotifyd-arm64.XXXXXX)"
cleanup() {
  rm -rf -- "$work"
}
trap cleanup EXIT

curl -fL --retry 3 \
  -o "$work/spotifyd-source.tar.gz" \
  "https://github.com/Spotifyd/spotifyd/archive/refs/tags/v${SPOTIFYD_VERSION}.tar.gz"
printf '%s  %s\n' "$SPOTIFYD_SOURCE_SHA512" "$work/spotifyd-source.tar.gz" \
  | sha512sum -c -
tar -xzf "$work/spotifyd-source.tar.gz" -C "$work"
source_dir="$work/spotifyd-${SPOTIFYD_VERSION}"

curl -fL --retry 3 \
  -o "$work/rustup-init" \
  "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/aarch64-unknown-linux-gnu/rustup-init"
printf '%s  %s\n' "$RUSTUP_SHA512" "$work/rustup-init" | sha512sum -c -
chmod 755 "$work/rustup-init"
"$work/rustup-init" -y --profile minimal --default-toolchain "$RUST_TOOLCHAIN_VERSION" \
  >/dev/null
export PATH="/root/.cargo/bin:$PATH"
test "$(rustc --version | awk '{print $2}')" = "$RUST_TOOLCHAIN_VERSION"

cd "$source_dir"
RUSTFLAGS="-C target-cpu=generic" cargo build --release --locked --jobs 1 \
  --config profile.release.lto=false \
  --no-default-features \
  --features alsa_backend,pulseaudio_backend,dbus_mpris

binary="$source_dir/target/release/spotifyd"
test -f "$binary"
test -x "$binary"
file -b "$binary" | grep -Eq '^ELF 64-bit LSB pie executable, ARM aarch64(,|$)'

stage="$work/stage"
mkdir -p "$stage"
install -m 755 "$binary" "$stage/spotifyd"

provenance="/out/${ARCHIVE%.tar.gz}.provenance.txt"
printf 'source version: spotifyd v%s\n' "$SPOTIFYD_VERSION" > "$provenance"
printf 'source archive sha512: %s\n' "$SPOTIFYD_SOURCE_SHA512" >> "$provenance"
printf 'build image: %s\n' "ubuntu:22.04@sha256:8c71efb5d8170edf0965b2ac5e867cc70d3d8f73d1c9c0573d690d6203fc5866" >> "$provenance"
printf 'build architecture: aarch64\n' >> "$provenance"
printf 'Rust toolchain: %s\n' "$RUST_TOOLCHAIN_VERSION" >> "$provenance"
printf 'Cargo features: alsa_backend,pulseaudio_backend,dbus_mpris\n' >> "$provenance"
printf 'Cargo flags: --locked --no-default-features --jobs 1 --config profile.release.lto=false\n' >> "$provenance"
printf 'RUSTFLAGS: -C target-cpu=generic\n' >> "$provenance"
printf '\nRuntime dependencies:\n' >> "$provenance"
ldd "$binary" | tee -a "$provenance"
if ldd "$binary" | grep -q 'not found'; then
  printf 'built spotifyd has unresolved runtime dependencies\n' >&2
  exit 1
fi

tar_file="$work/${ARCHIVE%.gz}"
tar --sort=name --numeric-owner --owner=0 --group=0 \
  --mtime='UTC 1970-01-01' -cf "$tar_file" -C "$stage" spotifyd
gzip -n -f "$tar_file"
mv -f "$tar_file.gz" "/out/$ARCHIVE"
(
  cd /out
  sha256sum "$ARCHIVE" | tee "$ARCHIVE.sha256"
  sha512sum "$ARCHIVE" | tee "$ARCHIVE.sha512"
)
(
  cd "$stage"
  sha256sum spotifyd | tee "/out/${ARCHIVE%.tar.gz}.binary.sha256"
)
CONTAINER

printf 'ARM64 artifact written to %s/%s\n' "$OUTPUT_DIR" "$ARCHIVE"
