# FXRoute Leap 16 Installation ISO

The ISO builder creates an x86_64 openSUSE Leap 16 installation medium from
the official offline installer. It adds exactly these FXRoute entries to the
normal Agama boot menu:

- `FXRoute Headless`: no graphical packages; FXRoute and its native DSP run as
  a lingering user service.
- `FXRoute Desktop`: a small KDE Plasma 6 / Wayland base with SDDM, automatic
  login as the account created during installation, and a normal Chrome
  window opening the FXRoute UI.

The desktop is not a kiosk. The Plasma desktop, shell, and keyboard shortcuts
remain available.

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

### Build record 2026-09-06 (HEAD `be2ef71`)

- Built from clean canonical `main` at `be2ef71292edb0f39629df790b8b18d740c9d44b`
  (VERSION `0.9.17`), the same versioned tree as the same-day VIM1S OOWOW
  image build (`docs/ARMBIAN-IMAGE-BUILDS.md`); no version or code changes
  were made for the ISO builds. History: the first same-day ISO (built from
  `6f72c15`, sha512 `55508d93…`) was deleted locally by accident and rebuilt
  from the unchanged tree including the follow-up docs commit; only the
  docs-only delta separates the two builds.
- Pre-build checks: cached base ISO present and verified against the pinned
  SHA-512 digest, `scripts/test_install_iso.py` 37/37 OK (first build), all
  builder tools (`mkmedia`, `mkisofs`, `isoinfo`, patched
  `mkmedia`/`isohybrid` shims) available. No extra acceptance or full-suite
  run was started for these builds; the device smoke gate was satisfied by
  the verified VIM1S build-level checks.
- Command: `SOURCE_DATE_EPOCH=0 FXROUTE_ISO_ALLOW_UNPUSHED=1
  ./iso/build-leap-16-iso.sh`. The opt-in flag is required because `origin/main`
  is the stale public mirror; first-boot updates on ISO installs then record
  the installed state as a local snapshot commit instead of fetching the built
  commit from GitHub.
- Build: ~110 s each, detached via `setsid nohup` (logs
  `iso-leap16-build-2026-09-06-6f72c15.log` (misnamed `…-d40e801.log`) and
  `iso-leap16-build-2026-09-06-be2ef71.log`, git-ignored, local only).
  Successful: `[iso] wrote` + `[iso] sha512` markers, no `[iso][error]`.
- Result: `dist/fxroute-leap-16-x86_64.iso` (4577034240 bytes), sha512
  `5ffa71d0d10c662fe426e5ce28bfa26dfc5d689bc0f6e3291a4afb54befd5dd70c030b419d892997defbae4cf6ded794b819568bda66a812438d54ac4e09fc67`.
- In-media verification: both `FXRoute Headless` and `FXRoute Desktop` boot
  labels present in the final GRUB config; `/fxroute/build-commit` contains
  `be2ef71…`; the embedded `/fxroute/source.tar` is byte-identical with the
  deterministic archive from HEAD (sha256 `8f96e91b335c0d9d781f494765d7233117fb01685e75c6d862c6d64d287a0bfa`,
  719 entries, contains VERSION `0.9.17`). Note: this archive uses the ISO
  builder's `--mtime='UTC 1970-01-01'` form and therefore differs byte-wise
  from the Armbian image's `--mtime=@0` archive of the same tree.
- Kein Push, kein Release.

Use `--base-iso` or `--output` to override individual values. The source
archive is created from `git ls-files`, with normalized tar metadata, and is
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

`sshd` is enabled with the distribution default configuration, so the account
password created in Agama also works for remote administration. The image
carries no key-only SSH hardening and no preinstalled keys; disable `sshd`
after installation if remote administration is not required.

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

The runner creates fresh disks for each profile, boots the ISO, drives the
interactive Agama decisions through the installer's own Agama CLI
(account/password, locale/keyboard/timezone, explicit target-disk
selection, install start), waits for `/api/status`, and verifies the
installed packages, user service, DSP binary, default target, SDDM
autologin configuration, and Chrome setup. SSH into the installed
system uses the account password created in Agama:

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

Set `FXROUTE_KEEP_ISO_TEST=1` to retain serial logs and guest disks after a
test. The desktop check reboots the guest before validating SDDM, Wayland, and
Chrome. QEMU emulates Ethernet networking only; the WLAN selection itself is
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

Confirm that UEFI reaches the GRUB menu and that both `FXRoute Desktop` and
`FXRoute Headless` entries are present before selecting one.
