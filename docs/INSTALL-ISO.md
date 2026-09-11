# FXRoute Leap 16 Installation ISO

The ISO builder creates an x86_64 openSUSE Leap 16 installation medium from
the official offline installer. It adds exactly these FXRoute entries to the
normal Agama boot menu:

- `FXRoute Headless`: no graphical packages; FXRoute and its native DSP run as
  a lingering user service.
- `FXRoute Desktop`: a KDE Plasma 6 / Wayland appliance with SDDM and
  automatic login as the account created during installation. Firefox starts
  the FXRoute UI fullscreen after login; the Plasma shell, panel, and
  keyboard shortcuts remain available (closing the window returns to the
  normal desktop).
- `Try FXRoute`: non-persistent live mode. Boots a prebuilt FXRoute desktop
  from `/LiveFX/squashfs.img` with a RAM overlay (`rd.live.overlay.overlayfs=1`),
  `graphical.target`, no Agama (`systemd.mask=agama*`), no `inst.auto`, and no
  `fxroute-first-boot.service`. Hostname `fxroute-live`, volatile machine-id,
  Caddy state in RAM only. Live Mode — changes and logins are not saved and
  will be lost after reboot. Internal ATA/NVMe drives are not auto-mounted
  (udev `UDISKS_IGNORE`, Agama masked); the install paths are unchanged.

## Installation flow

Each entry preloads its profile with `inst.auto` and then stops for review
(`inst.install=0`), so Agama shows its normal interactive overview. Only the
machine-specific decisions that cannot be sensibly preseeded are asked there:

- Network/WLAN: select and configure the connection in Agama. Agama copies
  the installer network setup to the installed system.
- Target disk: explicitly select the target disk in Agama's storage section
  and confirm the destructive installation. The preloaded proposal only
  describes the default partition layout; it never silently wipes the first
  SSD. Verify the target before starting the installation and disconnect
  disks that must not be touched.
- Locale, keyboard, and timezone: choose them through Agama's normal
  mechanism. The profiles carry no fixed locale and no `Europe/Berlin`
  default.
- Account/password: create the end-user account and its password in Agama
  (for example the account `fxroute`). The release image ships no user
  passwords and requires no SSH keys.

After these decisions, press Install in Agama; the installation then runs
through largely automatically and reboots (`inst.finish=reboot`).

Profile and source files use Agama's device-wide lookup, so the ISO can be
written to optical media or USB media without assuming `/dev/sr0`.

## Build

Install the host tools first. On openSUSE this is typically:

```bash
sudo zypper install git mkisofs mkmedia perl-JSON qemu-x86 squashfs
```

The base media is downloaded and verified against its pinned SHA-512 digest:

`https://download.opensuse.org/distribution/leap/16.0/offline/Leap-16.0-offline-installer-x86_64.install.iso`

No credentials are needed to build. The profiles intentionally contain no
user passwords and no SSH public keys:

```bash
export SOURCE_DATE_EPOCH=0
./iso/build-leap-16-iso.sh
```

Use `--base-iso` or `--output` to override individual values. Use
`--live-squash PATH` to reuse a prebuilt LiveFX image and `--no-live` to
skip the Try system for dev/test (release ISOs always ship it).
`iso/scripts/build-live-root.sh --output PATH` builds the flat
`/LiveFX/squashfs.img` (squash root = live rootfs with `/proc`, no nested
`LiveOS/rootfs.img`) via Docker Leap 16.0 from the desktop profile packages
plus the `install.sh` core/audio sets, with FXRoute checkout, venv, native
DSP, `fxroute.service`, SDDM autologin (`fxroute`), and the appliance
defaults prebaked; `--minimal` emits a tiny structure-test image.
The bootable live filesystem uses a minimum epoch of `86400` (1970-01-02),
even with `SOURCE_DATE_EPOCH=0`. SDDM ignores epoch-zero configuration,
including Leap's Xsession path, and sysusers interprets shadow last-change
day zero as requiring a password change. The ISO metadata still uses the
requested epoch. SquashFS is packed inside the build container so system
owners, service-account directories and setuid bits survive export; only
the completed image file is assigned to the host user. Container identity
markers and temporary runtime files are excluded.
The source archive is created from `git ls-files`, with normalized tar metadata, and is
copied to the installed system by Agama before the first boot.
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

A profile without credentials still validates: Agama only reports the missing
authentication as an installation issue in its overview until the account is
created interactively.

## First Boot

Both profiles install a small target-side systemd service. Agama copies the
first-boot script and service unit as files and enables the unit with a
chrooted post-install script; this avoids requiring an extra `agama-scripts`
RPM that is not present on the offline medium. The service extracts the source
archive and invokes the existing `install.sh`; it does not reimplement FXRoute
installation. Both profiles pass `--providers none`, so Spotify Desktop and
`spotifyd` remain independent installer selections. They can be installed
later with the existing `install.sh --spotify-desktop` or `install.sh
--spotifyd` options. Provider credentials are never asked in the x86
installer; they are managed later in the FXRoute Settings.

The first-boot service uses the account created in Agama: it prefers the
`fxroute` account and otherwise uses the single regular user on the system.
It then installs FXRoute with `--with-lan-name` and `--with-caddy` under an
automatically derived unique device name (`fxroute-<machine-id>`), so Caddy
and HTTPS are set up while plain HTTP stays reachable.

`sshd` runs by default with the appliance SSH policy: the account created in
Agama can log in over the LAN with its password, SSH keys may be added later,
and root login stays disabled. The image never switches to key-only SSH and
ships no preinstalled keys; disable `sshd` after installation if remote
administration is not required.

The Desktop profile gets Firefox (`MozillaFirefox`) from the Leap
repositories; no third-party browser repository or signing key is added and
Chrome is not part of the image. Firefox is the default browser, opens the
FXRoute control surface (`http://127.0.0.1:8000/`) as its fixed start
page, and the desktop shows an FXRoute link plus a link to the official
Spotify download page (`https://www.spotify.com/download/linux/`).

The Desktop profile also ships these appliance defaults (no FXRoute,
Provider, or DSP behavior is changed):

- Automatic login without a password prompt via SDDM (`plasmawayland`).
- The keyboard layout selected in Agama is adopted by Plasma (`kxkbrc`).
- Automatic suspend/sleep/hibernate and screen locking are off by default
  (systemd-logind lid/idle drop-in plus PowerDevil and KScreenLocker
  config); the explicit FXRoute suspend/shutdown menu still works.
- "Welcome to openSUSE Leap" first-run and KWallet/keyring onboarding are
  suppressed. KWallet is disabled; the Spotify Desktop client keeps its
  credentials in its own profile and is unaffected.
- An FXRoute wallpaper in the existing brand style is the default desktop
  background (applied on the first graphical login).

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

The runner creates fresh disks for each profile, boots the ISO, drives the
interactive Agama decisions through the installer's own Agama CLI
(account/password, locale/keyboard/timezone, explicit target-disk
selection, install start), waits for `/api/status`, and verifies the
installed packages, user service, DSP binary, default target, SDDM
autologin configuration, Firefox fullscreen autostart, and the appliance
defaults. SSH into the installed system uses the account password created
in Agama:

```bash
FXROUTE_ISO_LIVE_PASSWORD="..." \
  FXROUTE_ISO_USER_PASSWORD="..." \
  FXROUTE_ISO_LOCALE="de_DE.UTF-8" \
  FXROUTE_ISO_KEYMAP="de" \
  FXROUTE_ISO_TIMEZONE="Europe/Vienna" \
  ./iso/test-leap-16-iso.sh all dist/fxroute-leap-16-x86_64.iso
```

`FXROUTE_ISO_LIVE_PASSWORD` is the installer live password (append
`live.password=...` to the boot entry for automated runs, or read the
generated password from the installer console for manual runs).
`FXROUTE_ISO_USER_PASSWORD` becomes the password of the account created in
Agama. The runner additionally forwards the Agama web ports so the same
decisions can be made manually in a browser via `https://agama.local` or
the forwarded ports.

Try verification boots `Try FXRoute` without Agama and checks live:true,
`fxroute_dsp_sink`, RAM overlay, reboot volatility (home probe lost), and
that internal disks stay unmounted:

```bash
FXROUTE_ISO_TRY_PASSWORD="..." \
  ./iso/test-leap-16-iso.sh try dist/fxroute-leap-16-x86_64.iso
```

`FXROUTE_ISO_TRY_PASSWORD` is passed as `fxroute.live-password=...` on the
Try kernel cmdline to enable live SSH for the test; release boots omit it
and keep `sshd` disabled with a locked live password.

For the live desktop-start packaging regression, inspect the actual image:

```bash
FXROUTE_TEST_LIVE_SQUASH=/path/to/squashfs.img \
  python3 -m unittest scripts.test_live_root_image
```

This checks system/SDDM ownership, sudo's setuid bit, nonzero SDDM config
timestamps, the vendor session wrapper, SUSE's sysconfig autologin override,
and removal of the Docker identity marker. The reproduced failure was a
successful login followed by `Session started false`: SDDM fell back to
the absent `/etc/X11/xdm/Xsession` instead of the packaged
`/usr/etc/X11/xdm/Xsession`. Changing only the config mtime from zero to a
positive value restored the vendor configuration. Hardware acceptance is
separate from these artifact and targeted VM checks.

Set `FXROUTE_KEEP_ISO_TEST=1` to retain serial logs and guest disks after a
test. The desktop check reboots the guest before validating SDDM, Wayland,
Firefox fullscreen autostart, and the appliance defaults. QEMU emulates
Ethernet networking only; the WLAN selection itself is
covered by the Agama contract checks (no fixed network profile, interactive
network section), while real WLAN hardware must be checked separately.

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

Confirm that UEFI reaches the GRUB menu and that `FXRoute Desktop`,
`FXRoute Headless`, and `Try FXRoute` entries are present before selecting one.
