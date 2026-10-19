#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Install the fixed-action root helper; never run application imports as root.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HELPER=/usr/local/sbin/fxroute-usb-gadget
UNIT=/etc/systemd/system/fxroute-usb-gadget.service
SUDOERS=/etc/sudoers.d/fxroute-usb-gadget
STATE=/var/lib/fxroute-usb-gadget
MANIFEST="$STATE/install.sha256"
REMOVE=0
if [[ "${1:-}" == --remove ]]; then REMOVE=1; shift; fi
USER_NAME="${1:-${SUDO_USER:-}}"
[[ $# -le 1 && "$EUID" -eq 0 && "$USER_NAME" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || {
    echo 'Usage: sudo bash scripts/install_usb_gadget.sh [--remove] <FXRoute user>' >&2
    exit 2
}
id "$USER_NAME" >/dev/null

for path in "$STATE" "$MANIFEST" "$HELPER" "$UNIT" "$SUDOERS"; do
    [[ ! -L "$path" ]] || { echo "Refusing symlink: $path" >&2; exit 1; }
    if [[ -e "$path" ]]; then
        [[ "$(stat -c %u "$path")" == 0 ]] || { echo "Untrusted owner: $path" >&2; exit 1; }
        mode="$(stat -c %a "$path")"
        (( (8#$mode & 0022) == 0 )) || { echo "Untrusted permissions: $path" >&2; exit 1; }
    fi
done

if [[ -f "$MANIFEST" ]]; then
    # Check the manifest's exact path set before letting sha256sum consume it.
    [[ "$(awk '{print $2}' "$MANIFEST")" == "$(printf '%s\n' "$HELPER" "$UNIT" "$SUDOERS")" ]] || {
        echo 'Unexpected USB gadget installation manifest' >&2; exit 1;
    }
    sha256sum --status -c "$MANIFEST" || {
        echo 'USB gadget installation was modified; leaving it untouched' >&2; exit 1;
    }
    grep -Fxq "$USER_NAME ALL=(root) NOPASSWD: $HELPER on, $HELPER off" "$SUDOERS" || {
        echo 'USB gadget belongs to another installation user' >&2; exit 1;
    }
elif [[ -e "$HELPER" || -e "$UNIT" || -e "$SUDOERS" ]]; then
    echo 'Unowned USB gadget installation; leaving it untouched' >&2
    exit 1
elif [[ "$REMOVE" -eq 1 ]]; then
    exit 0
fi

if [[ "$REMOVE" -eq 1 ]]; then
    "$HELPER" off
    systemctl disable --now fxroute-usb-gadget.service
    rm -- "$HELPER" "$UNIT" "$SUDOERS" "$MANIFEST"
    systemctl daemon-reload
    exit 0
fi

stage="$(mktemp -d /var/tmp/fxroute-usb-install.XXXXXX)"
trap 'rm -rf -- "$stage"' EXIT
install -m 755 "$SOURCE_ROOT/audio/usb_gadget_system.py" "$stage/helper"
install -m 644 "$SOURCE_ROOT/scripts/fxroute-usb-gadget.service" "$stage/unit"
printf '%s ALL=(root) NOPASSWD: %s on, %s off\n' "$USER_NAME" "$HELPER" "$HELPER" > "$stage/sudoers"
visudo -cf "$stage/sudoers" >/dev/null
python3 -I "$stage/helper" status >/dev/null
install -d -m 755 "$STATE" /usr/local/sbin /etc/sudoers.d
install -m 755 "$stage/helper" "$HELPER"
install -m 644 "$stage/unit" "$UNIT"
install -m 440 "$stage/sudoers" "$SUDOERS"
sha256sum "$HELPER" "$UNIT" "$SUDOERS" > "$MANIFEST"
chmod 644 "$MANIFEST"
systemctl daemon-reload
systemctl enable fxroute-usb-gadget.service
systemctl restart fxroute-usb-gadget.service
echo 'USB audio gadget system helper installed.'
