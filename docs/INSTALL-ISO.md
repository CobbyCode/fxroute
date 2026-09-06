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

### Build record 2026-09-07 (HEAD `ebf2b09`)

- Built from clean canonical `main` at `ebf2b09889e2fad3d947c8b3770cbd5c58190632`
  (VERSION `0.9.17`, unchanged), on top of the previous ISO build record
  (`6edc851`). This build carries exactly one fix since the previous ISO:
  the desktop first-boot no longer deadlocks on `systemctl restart
  display-manager-legacy.service` (the unit is ordered
  `Before=display-manager.service`, resolved to `display-manager-legacy`;
  the synchronous restart waited on the display-manager job, which waits
  for first-boot — 30+ min hang on .129). Both restart/start now use
  `--no-block`, so first-boot exits after the install and the manager
  starts right after it (verified live on .129).
- Old artifact (`dist/fxroute-leap-16-x86_64.iso`, 4579131392 bytes) and the
  previous `iso-leap16-build-2026-09-06-6edc851.log` were removed before
  the build to save space.
- Command: `SOURCE_DATE_EPOCH=0 FXROUTE_ISO_ALLOW_UNPUSHED=1
  ./iso/build-leap-16-iso.sh` (same opt-in flag as before: `origin/main`
  is the stale public mirror). No version change, no further code changes,
  no full suite and no additional checksum/unpack or QEMU verification
  (as requested); the builder's own output markers are the completion gate.
- Build: single background run, log `iso-leap16-build-2026-09-06-ebf2b09.log`
  (git-ignored, local only). Successful: `[iso] wrote` + `[iso] sha512`
  markers, no `[iso][error]`, exit status 0.
- Result: `dist/fxroute-leap-16-x86_64.iso` (4579131392 bytes), sha512
  `3314d54f517310fcde2843beeb85087a20dfa5481ccd08407ce14718add6c8ec9e7ee45224dbb234b81bd951903f28da0ea1dec90d882c6fe741e036724f233a`.
- Kein Push, kein Release.

### Build record 2026-09-06 (HEAD `6edc851`)

- Built from clean canonical `main` at `6edc8517c63080109cb323e7f46f48ea83dc5de6`
  (VERSION `0.9.17`, unchanged), on top of the previous ISO build record
  (`e3b1fde`). This build carries three appliance/installer fixes: SDDM
  autologin uses the default Plasma (X11) session and keeps
  display-manager-legacy in charge, the desktop links resolve localized
  folders (Schreibtisch) with a first-login ensure, the session-init
  bookmark seeding crash is fixed and its wallpaper step is time-boxed
  with a config fallback, the logind default powers off on the hardware
  power button, and the PipeWire/DSP validation polls up to 120 s so a
  slow cold boot no longer records a false `install-failed`.
- Old artifact (`dist/fxroute-leap-16-x86_64.iso`, 4579131392 bytes, sha512
  `3b57fc5d…`) and all four previous `iso-leap16-build-*.log` files were
  removed before the build to save space.
- Command: `SOURCE_DATE_EPOCH=0 FXROUTE_ISO_ALLOW_UNPUSHED=1
  ./iso/build-leap-16-iso.sh` (same opt-in flag as before: `origin/main`
  is the stale public mirror). No version change, no further code changes,
  no full suite and no additional checksum/unpack or QEMU verification
  (as requested); the builder's own output markers are the completion gate.
- Build: single background run, log `iso-leap16-build-2026-09-06-6edc851.log`
  (git-ignored, local only). Successful: `[iso] wrote` + `[iso] sha512`
  markers, no `[iso][error]`, exit status 0.
- Result: `dist/fxroute-leap-16-x86_64.iso` (4579131392 bytes), sha512
  `f30cd23a5524980d51f2f65a85aaba8861eaf3a3c0456c41ff6f1f15d3179c3517d29d6c265b107d7209dc2db8f3718c50b0a7344be4301ff5d8d30c7cc32fa1`.
- Kein Push, kein Release.

### Build record 2026-09-06 (HEAD `e3b1fde`)

- Built from clean canonical `main` at `e3b1fdeded50902436bdbd3869dd6b5596f27560`
  (VERSION `0.9.17`, unchanged), on top of the previous ISO build record
  (`be2ef71`). This build carries the desktop-appliance completion commit:
  the Desktop profile now uses Firefox from the Leap repositories instead of
  Chrome (fullscreen FXRoute start at login, fixed start page, desktop
  links), ships the appliance defaults (autologin, Agama keyboard layout in
  Plasma, no automatic suspend/hibernate/lock, suppressed Welcome-to-Leap
  and KWallet onboarding, FXRoute wallpaper), and writes the explicit
  `90-fxroute-iso.conf` sshd default (password auth on, key login allowed,
  root login off). No FXRoute/Provider/DSP code changed.
- Pre-build checks: cached base ISO present and verified against the pinned
  SHA-512 digest, focused contract check `scripts/test_install_iso.py` 37/37
  OK at `e3b1fde`, builder tools (`mkmedia`, `mkisofs`, `isoinfo`, patched
  `mkmedia`/`isohybrid` shims) available. No full suite and no additional
  checksum/unpack or QEMU verification was run (as requested); the builder's
  own output markers are the completion gate.
- Command: `SOURCE_DATE_EPOCH=0 FXROUTE_ISO_ALLOW_UNPUSHED=1
  ./iso/build-leap-16-iso.sh`. The opt-in flag is required because `origin/main`
  is the stale public mirror; first-boot updates on ISO installs then record
  the installed state as a local snapshot commit instead of fetching the built
  commit from GitHub.
- Build: single foreground run, log `iso-leap16-build-2026-09-06-e3b1fde.log`
  (git-ignored, local only). Successful: `[iso] wrote` + `[iso] sha512`
  markers, no `[iso][error]`.
- Result: `dist/fxroute-leap-16-x86_64.iso` (4579131392 bytes), sha512
  `3b57fc5d05368b91ad6bf8f0b160f56da70036465058767c181412a5fb6d861c68cc7bde6d43a4217a2670ada46888a65d79b53c7e1937c2af90115a41913800`.
- Kein Push, kein Release.

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

Confirm that UEFI reaches the GRUB menu and that both `FXRoute Desktop` and
`FXRoute Headless` entries are present before selecting one.
