# FXRoute x86_64 installation ISO (Ubuntu 26.04)

The x86_64 installation medium is the official Ubuntu 26.04 Desktop ISO with
FXRoute added. It boots into a live session, installs a desktop or a headless
FXRoute appliance, and keeps Ubuntu's signed boot chain. Design notes and the
build history are in [`ubuntu/README.md`](../ubuntu/README.md).

## Download and write

Each release lists its ISO in the [release notes](https://github.com/CobbyCode/fxroute/releases)
(`fxroute-<version>-x86_64-ubuntu26.04.iso` on SourceForge, with a `.sha256`
file). Compare `sha256sum fxroute-<version>-x86_64-ubuntu26.04.iso` with the
published checksum, then write the ISO to a USB stick at least as large as the
ISO:

```bash
sudo dd if=fxroute-<version>-x86_64-ubuntu26.04.iso of=/dev/sdX bs=4M oflag=direct status=progress
```

Boot the target PC from the stick in UEFI mode. Ubuntu's shim and signed
kernel are left untouched, so the ISO needs no MOK enrolment with Secure Boot
enabled.

## Boot menu

| Entry | What it does |
| --- | --- |
| **Try FXRoute Live** (default) | Ubuntu live session that sets up FXRoute in RAM and opens it in a Firefox kiosk. Nothing is written to disk. |
| **Install FXRoute Desktop** | Installs Ubuntu with GNOME; the installed user is logged in automatically and FXRoute opens in a Firefox kiosk. |
| **Install FXRoute Headless** | Installs the same base without a desktop; FXRoute is operated from browsers on the LAN, the machine over SSH. |

## Installing

The installer (Subiquity) asks only for the keyboard, the network, and the
user (name, hostname, password). Configure Wi-Fi here once; the connection is
carried over to the installed system. Everything else runs automatically:

- **The disk is erased.** Storage uses Ubuntu's automatic LVM layout on the
  largest disk; there is no partitioning step.
- Locale `en_US.UTF-8`, keyboard default `us`, `openssh-server` enabled.
- After the reboot, `fxroute-first-boot.service` unpacks the FXRoute source
  shipped on the ISO and runs `install.sh` once. This needs an internet
  connection and takes a while (the native DSP engine is compiled). Its log is
  `journalctl -u fxroute-first-boot -b`.

**Desktop** logs the installed user in through GDM, starts Firefox in kiosk
mode with FXRoute, and switches off screen lock, dimming and automatic
suspend. A **FXRoute** icon on the desktop reopens the kiosk (for example
after Alt+F4).

**Headless** removes the desktop, GDM and Firefox from the target (the Firefox
snap during the first boot) and boots to `multi-user.target`. Open FXRoute from another device
at `http://fxroute-<id>.local:8000` or `http://<host-ip>:8000`; SSH uses the
password set in the installer.

## Live session

**Try FXRoute Live** installs FXRoute into the RAM session after the network
and apt are ready, then opens the kiosk. Reaching the FXRoute interface takes
noticeably longer than on an installed system because the DSP engine is
compiled in the session; that is expected. If the start fails, a note is left
on the desktop instead of an empty kiosk. FXRoute shows a **Live Mode**
banner: changes and logins are lost on reboot.

## Building the ISO yourself

```bash
ubuntu/build-ubuntu-iso.sh            # direct mode, needs loop devices (usually root)
ubuntu/build-ubuntu-iso.sh --docker   # same build in a privileged ubuntu:26.04 container
```

| Option / variable | Meaning |
| --- | --- |
| `--base-iso PATH`, `FXROUTE_UBUNTU_BASE_ISO` | Ubuntu base ISO; by default `ubuntu-26.04.1-desktop-amd64.iso` is downloaded to `~/.cache/fxroute/` and checked against its pinned SHA-256 |
| `--output PATH`, `FXROUTE_UBUNTU_ISO_OUTPUT` | result, default `dist/fxroute-ubuntu-26.04-x86_64.iso` |
| `--docker` | run `livefs-edit` in a container where loop devices are missing (rootless desktop) |
| `--test-seed` | stage the fully automatic QEMU test seed instead of the product seeds |
| `--keep-work` | keep the temporary staging directory |

Both modes need `git`, `tar`, `sha256sum`, `curl` and `python3` on the host.
Direct mode also needs `xorriso` and `livefs-edit`
(`pipx install git+https://github.com/mwhudson/livefs-editor.git`); `--docker`
needs Docker and installs both inside the container. The build customizes the base ISO with
livefs-editor: it adds the FXRoute dependencies to the live squashfs without
Recommends, copies the payload, writes the two installer seeds, replaces the
GRUB menu with the three entries above and refreshes `md5sum.txt`.

The payload `source.tar` holds every tracked file of the checkout **as it is
in the working tree**, and `build-commit` records `HEAD`. Build a release ISO
from a clean checkout of the release commit.

## Testing in QEMU

```bash
ubuntu/build-ubuntu-iso.sh --test-seed --output dist/fxroute-ubuntu-26.04-test.iso
ubuntu/test-ubuntu-iso.sh all dist/fxroute-ubuntu-26.04-test.iso
```

The test runs three phases, also separately (`live`, `install`, `appliance`):
the live session must serve `/api/status`, the installer entry must complete
unattended with the test seed, and the installed disk must boot without the
ISO into a running `fxroute.service` (desktop: autologin and kiosk).
`FXROUTE_UBUNTU_TEST_PROFILE=headless` tests the headless entry instead of the
desktop one. It needs `qemu-system-x86_64` with KVM, OVMF, `curl` and `ssh`.
The unit tests beside the scripts cover the seeds, the desktop defaults, the
headless target preparation, the live autostart and the test harness; they are
not part of `scripts/run_tests.sh`:

```bash
python3 -m unittest discover -s ubuntu -p 'test_*.py'
```

Checks that need real hardware (Secure Boot, Wi-Fi carried over from the
installer, a multichannel USB interface) are listed in
[`ubuntu/HARDWARE-TEST-PLAN.md`](../ubuntu/HARDWARE-TEST-PLAN.md).
