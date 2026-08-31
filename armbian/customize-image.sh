#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
set -Eeuo pipefail

# Armbian runs this script inside the target rootfs. The host-side overlay is
# intentionally read-only and is copied into the image explicitly.
RELEASE="${1:-}"
LINUXFAMILY="${2:-}"
BOARD="${3:-}"
BUILD_DESKTOP="${4:-}"
ARCH="${5:-}"
OVERLAY_DIR="/tmp/overlay"
IMAGE_DIR="/opt/fxroute-armbian"

[[ "$BUILD_DESKTOP" == "no" ]] \
  || { printf '%s\n' "FXRoute Armbian images must not contain a desktop stack" >&2; exit 1; }
[[ "$ARCH" == "arm64" ]] \
  || { printf 'FXRoute Armbian images require arm64, got %s\n' "$ARCH" >&2; exit 1; }
[[ -n "$RELEASE" && -n "$LINUXFAMILY" && -n "$BOARD" ]] \
  || { printf '%s\n' "Armbian customization arguments are incomplete" >&2; exit 1; }

for required in source.tar provision.env first-boot-install.sh fxroute-armbian-first-boot.service; do
  [[ -f "$OVERLAY_DIR/$required" && ! -L "$OVERLAY_DIR/$required" ]] \
    || { printf 'Missing Armbian overlay file: %s\n' "$OVERLAY_DIR/$required" >&2; exit 1; }
done

install -d -m 755 "$IMAGE_DIR"
install -m 644 "$OVERLAY_DIR/source.tar" "$IMAGE_DIR/source.tar"
install -m 600 "$OVERLAY_DIR/provision.env" "$IMAGE_DIR/provision.env"
install -d -m 755 /usr/local/libexec
install -m 755 "$OVERLAY_DIR/first-boot-install.sh" \
  /usr/local/libexec/fxroute-armbian-first-boot.sh
install -m 644 "$OVERLAY_DIR/fxroute-armbian-first-boot.service" \
  /etc/systemd/system/fxroute-armbian-first-boot.service

# Armbian's interactive root first-login helper is not part of a headless
# appliance. Keep armbian-firstrun.service: it regenerates host keys and does
# noninteractive board setup before the FXRoute handoff runs.
rm -f -- /root/.not_logged_in_yet /etc/profile.d/armbian-check-first-login.sh
passwd -l root >/dev/null 2>&1

install -d -m 755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-fxroute-armbian.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
chmod 644 /etc/ssh/sshd_config.d/90-fxroute-armbian.conf

install -d -m 755 /etc/systemd/system/multi-user.target.wants
ln -srf /etc/systemd/system/fxroute-armbian-first-boot.service \
  /etc/systemd/system/multi-user.target.wants/fxroute-armbian-first-boot.service
