# FXRoute Ubuntu 26.04 x86 ISO — design notes

This tree builds the FXRoute x86_64 installation ISO from the official Ubuntu
26.04 LTS Desktop ISO. It is the only x86 installation medium; releases publish
it as `fxroute-<version>-x86_64-ubuntu26.04.iso` since 1.0-beta9. Using,
building and testing the ISO is described in
[docs/INSTALL-ISO.md](../docs/INSTALL-ISO.md); this file records how it is put
together and why.

## Build and installer path (standard Ubuntu tooling)

* **Base:** the official `ubuntu-26.04.1-desktop-amd64.iso` (latest 26.04
  point release, SHA-256 checked against `releases.ubuntu.com`).
* **ISO customization:** `livefs-editor` (`livefs-edit`, Canonical upstream,
  installed with `pipx` from its Git repository) — the tool Ubuntu documents
  for this purpose. No own ISO build, no mkmedia, no third-party tool such as
  Cubic. Reason: it is the intended path, it works directly on the signed
  original ISO (the Secure Boot chain of shim and signed kernel stays
  untouched), and its actions (`--python`, `--cp`, plus an own no-Recommends
  package installation) cover exactly what is needed.
* **Installer:** Subiquity autoinstall with `subiquity.autoinstallpath` on two
  own GRUB entries, `Install FXRoute Desktop` and `Install FXRoute Headless`,
  each with `autoinstall`. The path is absolute
  (`/cdrom/fxroute-seed/<profile>.yaml`) because Subiquity resolves it relative
  to its working directory, the live user's home. The `Try FXRoute Live` entry
  (the stock Ubuntu live session plus an FXRoute autostart) stays the default
  and has no seed access (no `autoinstall.yaml` in the ISO root, no cloud-init
  datasource): the normal Ubuntu live/installer model.
* **Interactive sections:** keyboard, network (Wi-Fi once in the installer) and
  identity (user, hostname, password) via `interactive-sections` in **both**
  profiles — the usual Ubuntu questions, no own dialogs. Curtin carries the
  installer's netplan over to the target by default, so Wi-Fi connects after
  the first boot without a second setup.
* **First boot:** `fxroute-first-boot.service` (oneshot, installed by the
  autoinstall `late-commands`) unpacks `source.tar`, runs `install.sh` and sets
  up the desktop stack (GDM autologin, Firefox kiosk, GNOME power saving off).
* **Live (`Try FXRoute Live`):** Casper live session. The FXRoute dependencies
  are already in the squashfs without Recommends; an autostart runs
  `install.sh` in the RAM session (only after the network and the apt lock,
  `apt-get update` with retry) and opens the Firefox kiosk. **Reaching the
  FXRoute interface can take considerably longer (the DSP engine is compiled
  in the RAM session); that is known and not a fault as long as the system then
  starts normally.** If the start fails, a note is left on the desktop and no
  dead kiosk opens; a `FXRoute` icon on the desktop restarts the kiosk (needed
  for example after Alt+F4). The live root and the installed root are
  deliberately not identical.
* **Headless target (`Install FXRoute Headless`):** the same
  `ubuntu-desktop-minimal` setup, but the Curtin late-command
  `prepare-headless-target.sh` explicitly removes desktop, GDM and the Firefox
  deb from the target (purge list without autoremove, fail-closed on collateral
  removals; the Firefox snap seed entry is pruned offline) and sets
  `multi-user.target` before the first boot. The Firefox snap itself is removed
  at first boot with snapd running (after `snap wait system seed.loaded`).
  Operation is through the browser on the LAN (FXRoute HTTP) and SSH;
  `install.sh` takes care of linger and the user manager.

## Reused unchanged

* `install.sh` detects `apt` by capability probe, and every package list has an
  `apt` branch. Verified on Ubuntu 26.04 (resolute, QEMU VM and real hardware):
  every `apt` package name resolves, PipeWire is 1.6.2 (≥ 1.4.9, no backport
  needed), `caddy` 2.6.2 is in the standard repositories, and Firefox is the
  stock snap. Providers resolve their current stable upstream releases
  (spotifyd, `tidalapi` from PyPI, Qobuz from the official upstream with a
  compatible fork as fallback).
* The whole FXRoute application: DSP, routing, providers, measurement, library.
* `fxroute.service` (user unit), the `source.tar`/`build-commit` pattern, the
  device-name derivation (`fxroute-<machine-id>`) and the SSH defaults.

## Layout

```
ubuntu/
  README.md                  this file
  HARDWARE-TEST-PLAN.md      checks that need real hardware
  build-ubuntu-iso.sh        ISO build (downloads/checks the base ISO, livefs-edit)
  test-ubuntu-iso.sh         QEMU acceptance (live → install → appliance)
  test-askpass.sh            SSH_ASKPASS helper for the QEMU tests
  generate-seeds.py          renders fxroute-seed/{desktop,headless}.yaml
  test_*.py                  unit tests (seeds, profiles, desktop defaults,
                             headless target, live autostart, harness)
  autoinstall/
    user-data                Subiquity autoinstall base (en_US/us, rest default)
    user-data.test           test-seed overrides (QEMU only: autologin test/test)
    first-boot.service       oneshot unit for the installed system
    fxroute-live.desktop     autostart entry of the live session
    fxroute-test-ssh.service QEMU SSH hook as a unit (test seed only)
  livefs-actions/            livefs-edit --python actions (chroot prep, packages
                             without Recommends, payload, GRUB entries, checksums)
  scripts/
    first-boot-install-ubuntu.sh   first-boot setup on the installed system
    prepare-headless-target.sh     headless purge inside the target (late-command)
    fxroute-live-autostart.sh      live-session entry (RAM overlay only)
    fxroute-ubuntu-launcher.sh     desktop launcher (appliance and live session)
    fxroute-test-ssh.sh            QEMU SSH hook (test seed only)
```

## QEMU acceptance (UEFI/OVMF, Secure Boot off)

First passed on 2026-09-17 with the test ISO `dist/fxroute-ubuntu-26.04-test.iso`:

* **Live phase:** the default entry `Try FXRoute Live` boots the GNOME live
  session; the autostart script sets up FXRoute and the kiosk opens.
  `/api/status` is reachable through the QEMU port forward (full status JSON).
* **Install phase:** the install entry (with `autoinstall` on the command line,
  verified over SSH) runs Subiquity fully unattended: LVM layout, test user,
  late-commands (payload to `/opt/fxroute-iso`, first-boot unit enabled). The
  reboot lands directly in the installed system (Subiquity changes the EFI boot
  order; no second GRUB menu appears).
* **Appliance phase:** the installed disk boots without the ISO.
  `fxroute-first-boot.service` runs once successfully and sets
  `install-complete`. `fxroute.service` (user unit) is active, HTTP
  `/api/status` answers on 127.0.0.1:8000, GDM autologin as the installed user
  is active, and Firefox runs in `--kiosk` mode.

Learned and fixed during the QEMU runs (details in the Git history):

* The stock desktop command line has no `boot=casper`, so the live guard checks
  the Casper mount (`/cdrom/casper`) instead of the kernel flag.
* Casper stacks squashfs layers with `.live` on top; the placeholders
  `/var/lib/cloud/seed/nocloud/*` in the `.live` layer are empty and hide seed
  files in lower layers. The seeds therefore live as files on the ISO
  (`/fxroute-seed/<profile>.yaml`) and are selected per GRUB entry with an
  absolute `subiquity.autoinstallpath=/cdrom/...` (Subiquity resolves relative
  paths against its working directory, the live user's home). There is no
  `autoinstall.yaml` in the ISO root and no cloud-init datasource, so the stock
  Try entry stays unaffected.
* `md5sum.txt` must be rewritten after the squashfs rebuild (pre-repack hook
  `final_checksums.py`); xorriso creates `/boot.catalog` only afterwards, so the
  catalog is excluded as in the Ubuntu original and the payload stays fully
  covered.
* The autoinstall reboot shows no second GRUB (Subiquity changes the EFI order);
  the harness recognizes the installed system over SSH (root filesystem,
  command line, marker) instead of a GRUB banner.
* dconf system overrides require `/etc/dconf/profile/user` with
  `system-db:local`; Ubuntu does not create it. uint32 keys need the
  `idle-delay=uint32 0` notation (a bare `0` is int32 and ignored). Both are
  unit-tested (`test_desktop_defaults.py`).

## Hardware checks

The checks that QEMU cannot cover are in
[HARDWARE-TEST-PLAN.md](HARDWARE-TEST-PLAN.md):

* Wi-Fi (Intel 8265 / 8086:24fd / iwlwifi): configured in the installer,
  connected after the first boot without a second setup.
* Secure Boot on real hardware (signed Ubuntu path only, no MOK).
* A Scarlett audio interface on the target system.
