# spotifyd ARM64 Build

FXRoute ships a small, versioned ARM64 prebuilt for spotifyd v0.4.2. The
upstream ARM64 release is not suitable for Debian 13/Trixie because it links
against OpenSSL 1.1. This artifact is built against the OpenSSL 3 and glibc 2.35
runtime supplied by Ubuntu 22.04 ARM64.

## Build Baseline

Run `scripts/build_spotifyd_arm64.sh` on a machine with Docker and ARM64
binfmt support:

```bash
scripts/build_spotifyd_arm64.sh /tmp/fxroute-spotifyd-arm64
```

The recipe pins all inputs that affect the portable binary:

- source version: spotifyd `0.4.2`
- source archive SHA-512:
  `03a28037db2389a9415cde985dbf8c639c38d8000da5985178833fc5250249da5798f426e9b6e89c18762ff2d05b1331590905ef818f1430fe1c7e2507020898`
- build image: `ubuntu:22.04@sha256:8c71efb5d8170edf0965b2ac5e867cc70d3d8f73d1c9c0573d690d6203fc5866`
- Rust toolchain: `1.88.0`
- Cargo lockfile: `--locked`
- Cargo features: `alsa_backend`, `pulseaudio_backend`, `dbus_mpris`
- Cargo feature selection: `--no-default-features`
- CPU baseline: `-C target-cpu=generic` for portable aarch64 code

The generated archive contains only the executable. The script also writes a
SHA-256 checksum, a SHA-512 checksum, a binary checksum, and a provenance
manifest containing the runtime dependencies. The expected runtime is
OpenSSL 3, PipeWire-Pulse through libpulse, ALSA, and the session D-Bus.

Check the archive before hosting it:

```bash
sha256sum -c spotifyd-0.4.2-linux-aarch64-fxroute-1.tar.gz.sha256
tar -tzf spotifyd-0.4.2-linux-aarch64-fxroute-1.tar.gz
```

## Hosting

Host the archive, both checksum files, and the provenance manifest as assets
of the dedicated FXRoute GitHub release/tag `spotifyd-arm64-v1`. Do not
replace an existing asset under the same version; the installer pins the
archive SHA-256. A release maintainer can upload the files with:

```bash
gh release create spotifyd-arm64-v1 \
  --title "spotifyd ARM64 v1" \
  --notes-file spotifyd-0.4.2-linux-aarch64-fxroute-1.provenance.txt \
  spotifyd-0.4.2-linux-aarch64-fxroute-1.tar.gz \
  spotifyd-0.4.2-linux-aarch64-fxroute-1.tar.gz.sha256 \
  spotifyd-0.4.2-linux-aarch64-fxroute-1.tar.gz.sha512 \
  spotifyd-0.4.2-linux-aarch64-fxroute-1.provenance.txt
```

The installer URL and checksum are deliberately fixed in `install.sh`; an
unverified mirror or a source build is not a fallback.

## Runtime Verification

The artifact is intended for ARM64 hosts including a Raspberry Pi 4 and
Khadas Debian 13 systems. Verify the extracted binary on each target with:

```bash
file "$HOME/.local/bin/spotifyd"
ldd "$HOME/.local/bin/spotifyd"
```

The `ldd` output must not contain `not found`, must resolve `libssl.so.3` and
`libcrypto.so.3`, and must show the host's glibc. Then run the installer with
`--spotifyd`, confirm the user service, and select FXRoute from Spotify
Connect. The generated config keeps `volume_controller = "none"`, so the
FXRoute master remains the playback volume control.
