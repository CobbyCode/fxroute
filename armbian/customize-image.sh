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

for required in source.tar build-commit first-boot-install.sh fxroute-armbian-first-boot.service armbian-web-config.py armbian-web-config.service; do
  [[ -f "$OVERLAY_DIR/$required" && ! -L "$OVERLAY_DIR/$required" ]] \
    || { printf 'Missing Armbian overlay file: %s\n' "$OVERLAY_DIR/$required" >&2; exit 1; }
done

install -d -m 755 "$IMAGE_DIR"
install -m 644 "$OVERLAY_DIR/source.tar" "$IMAGE_DIR/source.tar"
install -m 644 "$OVERLAY_DIR/build-commit" "$IMAGE_DIR/build-commit"
install -d -m 755 /usr/local/libexec
install -m 755 "$OVERLAY_DIR/first-boot-install.sh" \
  /usr/local/libexec/fxroute-armbian-first-boot.sh
install -m 644 "$OVERLAY_DIR/fxroute-armbian-first-boot.service" \
  /etc/systemd/system/fxroute-armbian-first-boot.service
install -m 755 "$OVERLAY_DIR/armbian-web-config.py" \
  /usr/local/libexec/armbian-web-config.py
install -m 644 "$OVERLAY_DIR/armbian-web-config.service" \
  /etc/systemd/system/armbian-web-config.service

# Keep Armbian's first-login marker and profile hook. The marker gates the
# temporary web setup service and is removed after FXRoute provisioning.
passwd -l root >/dev/null 2>&1
cat > /etc/profile.d/armbian-check-first-login.sh <<'EOF'
#!/bin/sh
if [ -w /root/ ] && [ -f /root/.not_logged_in_yet ] &&
   ! systemctl is-active --quiet armbian-web-config.service 2>/dev/null &&
   [ ! -d /opt/fxroute-armbian ]; then
  bash /usr/lib/armbian/armbian-firstlogin
fi
EOF
chmod 755 /etc/profile.d/armbian-check-first-login.sh

install -d -m 755 /etc/netplan
cat > /etc/netplan/20-fxroute-ethernet-priority.yaml <<'EOF'
network:
  version: 2
  renderer: networkd
  ethernets:
    all-eth-interfaces:
      match:
        name: "e*"
      dhcp4-overrides:
        route-metric: 100
      dhcp6-overrides:
        route-metric: 100
    all-lan-interfaces:
      match:
        name: "lan*"
      dhcp4-overrides:
        route-metric: 100
      dhcp6-overrides:
        route-metric: 100
    all-wan-interfaces:
      match:
        name: "wan*"
      dhcp4-overrides:
        route-metric: 100
      dhcp6-overrides:
        route-metric: 100
EOF
chmod 600 /etc/netplan/20-fxroute-ethernet-priority.yaml

install -d -m 755 /etc/ssh/sshd_config.d
# Never ship host keys shared by every copy of a public image. They are
# generated on first boot before SSH is enabled for the configured account.
rm -f /etc/ssh/ssh_host_*
# Restrictive default before onboarding. The web setup and first-boot script
# replace this file conditionally: password SSH stays open only when no SSH
# key was provided, otherwise the account stays key-only. Root stays denied.
cat > /etc/ssh/sshd_config.d/90-fxroute-armbian.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
EOF
chmod 644 /etc/ssh/sshd_config.d/90-fxroute-armbian.conf

# Armbian's firstrun unit is Type=simple in the pinned release. Make its
# completion observable so the web service sees the final Raspberry Pi host
# name before it publishes the setup SSID.
install -d -m 755 /etc/systemd/system/armbian-firstrun.service.d
cat > /etc/systemd/system/armbian-firstrun.service.d/10-fxroute-wait.conf <<'EOF'
[Service]
Type=oneshot
EOF
chmod 644 /etc/systemd/system/armbian-firstrun.service.d/10-fxroute-wait.conf

install -d -m 755 /etc/systemd/system/multi-user.target.wants
ln -srf /etc/systemd/system/fxroute-armbian-first-boot.service \
  /etc/systemd/system/multi-user.target.wants/fxroute-armbian-first-boot.service
ln -srf /etc/systemd/system/armbian-web-config.service \
  /etc/systemd/system/multi-user.target.wants/armbian-web-config.service
