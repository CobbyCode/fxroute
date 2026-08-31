# FXRoute Leap 16 Installation ISO

The ISO builder creates an x86_64 openSUSE Leap 16 installation medium from
the official offline installer. It adds exactly these FXRoute entries to the
normal Agama boot menu:

- `FXRoute Headless`: no graphical packages; FXRoute and its native DSP run as
  a lingering user service.
- `FXRoute Desktop`: a small KDE Plasma 6 / Wayland base with SDDM, automatic
  login as `fxroute`, and a normal Chrome window opening the FXRoute UI.

The desktop is not a kiosk. The Plasma desktop, shell, and keyboard shortcuts
remain available.

The automatic storage profile leaves target selection to Agama's normal
bootable-device selection instead of choosing a disk by size. The selected
target is repartitioned, so verify the target before starting an unattended
installation and disconnect disks that must not be touched. Profile and
source files use Agama's device-wide lookup, so the ISO can be written to
optical media or USB media without assuming `/dev/sr0`.

## Build

Install the host tools first. On openSUSE this is typically:

```bash
sudo zypper install git mkisofs mkmedia perl-JSON qemu-x86 squashfs
```

The base media is downloaded and verified against its pinned SHA-512 digest:

`https://download.opensuse.org/distribution/leap/16.0/offline/Leap-16.0-offline-installer-x86_64.install.iso`

The build requires a SHA-512 password hash for the `fxroute` user and an SSH
public key for root. They are rendered into the generated profiles only:

```bash
export FXROUTE_PASSWORD_HASH="$(openssl passwd -6 'change-this-password')"
export FXROUTE_SSH_PUBLIC_KEY="$(< ~/.ssh/id_ed25519.pub)"
export SOURCE_DATE_EPOCH=0
./iso/build-leap-16-iso.sh
```

Use `--base-iso`, `--output`, `--password-hash`, or
`--ssh-public-key-file` to override individual values. The source archive is
created from `git ls-files`, with normalized tar metadata, and is copied to
the installed system by Agama before the first boot.
`SOURCE_DATE_EPOCH` defaults to `0`; the builder uses reproducible `mkisofs` and
`isohybrid` shims, normalizes Rock Ridge change times and EFI volume serials,
and preserves normalized staging timestamps so repeated builds with the same
inputs and toolchain are byte-identical.

For schema validation, install `agama-cli` and `agama-common`. Build with
`--keep-work`, then validate the rendered profiles from the retained staging
directory:

```bash
agama config validate --local /path/to/work/stage/fxroute/profiles/headless.jsonnet
agama config validate --local /path/to/work/stage/fxroute/profiles/desktop.jsonnet
```

Do not publish an ISO built with test credentials. Generate a unique password
hash and use a deployment-specific SSH key for every real installation.

## First Boot

Both profiles install a small target-side systemd service. Agama copies the
first-boot script and service unit as files and enables the unit with a
chrooted post-install script; this avoids requiring an extra `agama-scripts`
RPM that is not present on the offline medium. The service extracts the source
archive and invokes the existing `install.sh`; it does not reimplement FXRoute
installation. Both profiles pass `--providers none`, so Spotify Desktop and
`spotifyd` remain independent installer selections. They can be installed
later with the existing `install.sh --spotify-desktop` or `install.sh
--spotifyd` options.

Both profiles install `/etc/ssh/sshd_config.d/90-fxroute-iso.conf` before the
first boot; the first-boot service reapplies and validates it before enabling
`sshd` for remote administration. It contains `PasswordAuthentication no` and
`KbdInteractiveAuthentication no`. Password and keyboard-interactive
authentication are disabled, and Root SSH access is restricted to the public
key supplied at build time (`PermitRootLogin prohibit-password`). The ISO still
sets the requested local `fxroute` password hash; the root password remains
unset. Use a unique hash and deployment-specific key, and disable `sshd` after
installation if remote administration is not required.

The Desktop profile installs Chrome from Google's official RPM repository:
`https://dl.google.com/linux/chrome/rpm/stable/x86_64`. It does not use a
Flatpak or a downloaded untrusted RPM.

Leap 16's current Agama schema does not accept the older `user.autologin`
profile property. The first-boot script therefore configures the supported
SDDM and openSUSE display-manager settings after the user exists, including
the `plasmawayland` session. It force-updates the `display-manager.service`
alias so an existing legacy display-manager selection cannot win. A failed
first-boot attempt remains recorded for diagnostics but is retried on the next
boot. An in-progress marker also makes an interrupted first boot recoverable
without leaving a partial install target behind.

## Updates

ISO installs do not ship a `.git` directory, so the first-boot setup
re-creates one after `install.sh` finishes: it inits a repo in
`~/fxroute`, adds the public GitHub `origin`, fetches `main`, and checks
out the commit the ISO was built from (recorded on the media as
`/opt/fxroute-iso-build-commit`). This makes the normal git-based update
flow work unchanged:

```bash
cd ~/fxroute && scripts/update_fxroute.sh
```

The fetch is best effort with three bounded attempts; if GitHub is
unreachable during first boot, FXRoute still installs and runs, and the
checkout can be prepared later with the same `git init`/`fetch`/`checkout`
steps. When the ISO was built from a commit that is not on GitHub
(development or test builds, `FXROUTE_ISO_ALLOW_UNPUSHED=1`), the first
boot never overwrites the installed tree with an older published
checkout; it records the installed state as a local commit on top of
`origin/main` instead, and the builder refuses unpushed sources for
normal (non-opt-in) builds. User state is not affected by updates:
presets, measurements, provider credentials and runtime state live in
`~/.config/fxroute` and `~/.env`-style configuration stays in place
(`.env`, `.venv`, `media/cache`, `native_dsp/build`, and `backups/` are
gitignored).

## QEMU Verification

The runner creates a new 40 GiB disk for each profile, boots the ISO, waits for
`/api/status`, and verifies the installed packages, user service, DSP binary,
default target, SDDM autologin configuration, and Chrome setup:

```bash
FXROUTE_SSH_KEY="$HOME/.ssh/id_ed25519_vm" \
  ./iso/test-leap-16-iso.sh all dist/fxroute-leap-16-x86_64.iso
```

Set `FXROUTE_KEEP_ISO_TEST=1` to retain serial logs and guest disks after a
test. The desktop check reboots the guest before validating SDDM, Wayland, and
Chrome. QEMU emulates Ethernet networking only; real WLAN hardware must be
checked separately.

The runner uses the host's default BIOS firmware. For a separate UEFI firmware
sanity check, use PFLASH code and a disposable writable variable store (do not
pass the OVMF code image with `-bios`):

```bash
uefi_work="$(mktemp -d)"
cp /usr/share/qemu/ovmf-x86_64-4m-vars.bin "$uefi_work/vars.fd"
qemu-system-x86_64 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/qemu/ovmf-x86_64-4m-code.bin \
  -drive if=pflash,format=raw,file="$uefi_work/vars.fd" \
  -cdrom dist/fxroute-leap-16-x86_64.iso \
  -boot once=d
rm -rf -- "$uefi_work"
```

Confirm that UEFI reaches the GRUB menu and that both `FXRoute Desktop` and
`FXRoute Headless` entries are present before selecting one.
