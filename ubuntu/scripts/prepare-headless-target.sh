#!/usr/bin/env bash
# Run only inside the installed target, from a Subiquity late-command:
# curtin in-target --target=/target -- /opt/fxroute-iso/scripts/prepare-headless-target.sh
# The desktop-minimal source still contains a desktop. Remove it before reboot,
# without autoremove: NetworkManager/Wi-Fi and PipeWire must survive unchanged.
set -Eeuo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH
export LC_ALL=C DEBIAN_FRONTEND=noninteractive

die() {
  printf '[headless-target] %s\n' "$*" >&2
  exit 1
}

[[ "$(id -u)" -eq 0 ]] || die "Target preparation must run as root"
PROFILE_FILE="/etc/fxroute-iso-profile"
[[ -f "$PROFILE_FILE" && ! -L "$PROFILE_FILE" ]] \
  || die "Missing or invalid FXRoute profile marker: $PROFILE_FILE"
[[ "$(<"$PROFILE_FILE")" == headless ]] \
  || die "Target preparation requires the headless profile"

# Exact package names only. GNOME libraries/keyring are not the desktop and are
# retained for the shared installer (including provider credentials and GVfs).
# No wildcard purge and no dependency garbage collection.
# The last three entries are not top-level dependencies of the metapackages
# but apt removes them as part of the same transaction (verified against a
# real ubuntu-desktop-minimal target install): the Ubuntu shell-extensions
# metapackage, the default gsettings overrides, and the GNOME portal backend,
# which is useless without a graphical session. Each stays covered by the
# fail-closed simulation check below.
desktop_packages=(
  ubuntu-desktop ubuntu-desktop-minimal ubuntu-desktop-default-settings
  gdm3 gnome-shell gnome-shell-common gnome-session gnome-session-bin
  gnome-session-common ubuntu-session ubuntu-settings gnome-settings-daemon
  gnome-settings-daemon-common gnome-control-center gnome-control-center-data
  gnome-control-center-faces gnome-shell-extension-appindicator
  gnome-shell-extension-desktop-icons-ng gnome-shell-extension-ubuntu-dock
  gnome-shell-extension-ubuntu-tiling-assistant gnome-shell-ubuntu-extensions
  gnome-initial-setup xdg-desktop-portal-gnome
  gnome-remote-desktop nautilus nautilus-data firefox
)
declare -A allowed_packages=()
for package in "${desktop_packages[@]}"; do
  allowed_packages["$package"]=1
done

packages_to_purge=()
package_inventory="$(dpkg-query -W -f='${binary:Package}\t${db:Status-Status}\n')"
while IFS=$'\t' read -r package status; do
  [[ -n "$package" ]] || continue
  package_name="${package%%:*}"
  if [[ -n "${allowed_packages[$package_name]:-}" && "$status" != not-installed ]]; then
    packages_to_purge+=("$package")
  fi
done <<< "$package_inventory"

if [[ ${#packages_to_purge[@]} -gt 0 ]]; then
  # Fail closed for ANY collateral removal, not just today's list of audio and
  # network essentials. Keep the simulation and real transaction flags equal.
  apt_options=(-y -o APT::Get::AutomaticRemove=false)
  removal_plan="$(apt-get --simulate "${apt_options[@]}" purge "${packages_to_purge[@]}")" \
    || die "APT purge simulation failed; target was not changed"
  printf '%s\n' "$removal_plan"
  while read -r action package remainder; do
    case "$action" in
      Remv|Purg)
        [[ -n "${allowed_packages[${package%%:*}]:-}" ]] \
          || die "Refusing collateral package removal: $package"
        ;;
    esac
  done <<< "$removal_plan"
  apt-get "${apt_options[@]}" purge "${packages_to_purge[@]}"
fi

# Installed snaps cannot be removed here: no snap daemon runs inside the install
# target, and offline surgery on snapd state is unsupported. The Firefox snap
# itself is removed on first boot with a running snapd (see
# first-boot-install-ubuntu.sh). What is offline-safe stays here: remove
# Firefox from the seed so it cannot reappear, otherwise it would be reseeded.
# python3-yaml comes with Netplan on the Ubuntu desktop source. Preserve every
# other snap and shared assertion.
python3 - <<'PY'
from pathlib import Path

seed = Path('/var/lib/snapd/seed')
manifest = seed / 'seed.yaml'
if manifest.exists():
    import yaml
    data = yaml.safe_load(manifest.read_text())
    snaps = data.get('snaps', [])
    firefox = [snap for snap in snaps if snap.get('name') == 'firefox']
    if firefox:
        for snap in firefox:
            filename = snap['file']
            if Path(filename).name != filename or not filename.startswith('firefox_') or not filename.endswith('.snap'):
                raise SystemExit('Unexpected Firefox seed filename: ' + filename)
        data['snaps'] = [snap for snap in snaps if snap.get('name') != 'firefox']
        temporary = manifest.with_suffix('.yaml.fxroute-tmp')
        temporary.write_text(yaml.safe_dump(data, sort_keys=False))
        temporary.chmod(manifest.stat().st_mode & 0o777)
        temporary.replace(manifest)
        for snap in firefox:
            (seed / 'snaps' / snap['file']).unlink(missing_ok=True)
PY

package_inventory="$(dpkg-query -W -f='${binary:Package}\t${db:Status-Status}\n')"
while IFS=$'\t' read -r package status; do
  [[ -n "$package" ]] || continue
  package_name="${package%%:*}"
  if [[ -n "${allowed_packages[$package_name]:-}" && "$status" != not-installed ]]; then
    die "Desktop package remains after purge: $package ($status)"
  fi
done <<< "$package_inventory"

# This is an offline configuration operation, not an attempt to start/stop
# services in the installer. The very first target boot must be non-graphical.
systemctl set-default multi-user.target
printf '%s\n' '[headless-target] Desktop removed; first boot will use multi-user.target'
