# FXRoute Armbian Images

`armbian/build-image.sh` builds a headless ARM64 system image through the
official Armbian build framework. The framework checkout is pinned to:

`4a50e16e09222e00d3f57884b4dfbf8fdb4ce5dc`

The build wrapper uses Armbian's supported `config-*`,
`userpatches/customize-image.sh`, and read-only `userpatches/overlay`
interfaces. It does not fork or modify Armbian board files.

## Host Requirements

Install the normal Armbian build prerequisites, Git, GNU tar, and SHA-256
tools. The current Armbian build also needs a working Docker daemon or a host
that satisfies its native build prerequisites. Reserve at least 20 GiB for the
checkout, kernel sources, package cache, and image output.

QEMU verification additionally needs `qemu-system-aarch64`, `qemu-img`,
`curl`, `ssh`, and Python 3. Pi checks need `mcopy` (from `mtools`); generic
UEFI checks need the host AAVMF firmware files. A QEMU build check does not
replace a physical board check.

## Build

```bash
./armbian/build-image.sh \
  --board rpi4
```

The wrapper neither requests nor generates a builder password. No builder
credential, setup token, or setup environment file is embedded in the image.
End-user account credentials are entered only during first boot.
Because the temporary setup AP is open, perform onboarding only on a trusted
local connection.

The resulting raw image and checksum are written to `dist/` by default. The
same command with `--board rpi5` builds the current official Armbian target
for Pi 5:

```bash
./armbian/build-image.sh \
  --board rpi5
```

For Khadas VIM1S, the wrapper follows Armbian's official OOWOW target: it uses
`BOARD=khadas-vim1s`, defaults to `BRANCH=legacy`, and passes
`EXT=image-output-oowow`. Armbian's extension produces the OOWOW image itself;
the wrapper does not rename a normal `.img`:

```bash
./armbian/build-image.sh \
  --board khadas-vim1s
```

The default output is
`dist/fxroute-armbian-khadas-vim1s-trixie-legacy.oowow.img.xz` with its
`.sha256` checksum. A VIM1S output path must retain the `.oowow.img.xz`
suffix.

At the pinned Armbian revision, `rpi4`, `rpi4b`, `rpi5`, and `rpi5b` are
intentional aliases for `BOARD=rpi4b`. Armbian uses the shared `bcm2711`
configuration and identifies a Pi 5 at runtime. There is no separate current
`rpi5b.conf` to select.

Other Armbian board names can be passed with `--board`. The wrapper validates
that the pinned checkout contains that board file. For a generic board, use
`--kernel-ref commit:<sha>` when the kernel input must be reproducible; the
Pi aliases use the observed pinned Raspberry Pi Linux commit for `legacy`,
`current`, or `edge` automatically.

The separate `qemu-uboot-arm64` board emits a qcow2 disk and a U-Boot firmware
companion. With `--output dist/fxroute-qemu.img`, the wrapper writes
`dist/fxroute-qemu.img.qcow2` and `dist/fxroute-qemu.u-boot.bin`.

The pinned checkout also provides the generic UEFI arm64 target used for a
repeatable QEMU `virt` smoke test. Select a recorded commit from the kernel
source configured by Armbian, then build it as a raw image:

```bash
./armbian/build-image.sh \
  --board uefi-arm64 \
  --kernel-ref commit:<pinned-linux-stable-commit> \
  --output dist/fxroute-armbian-uefi-arm64-trixie-current.img
```

Useful overrides are:

```bash
./armbian/build-image.sh --help
./armbian/build-image.sh --release noble --branch current ...
./armbian/build-image.sh --armbian-source /path/to/pinned/armbian-build ...
./armbian/build-image.sh --cache-dir "$HOME/.cache/fxroute/armbian" ...
```

`SOURCE_DATE_EPOCH` defaults to `0` and is applied to the deterministic
FXRoute source archive. Byte-identical complete images also require pinned
Armbian package repositories, firmware, Docker build image, and all other
external inputs; the wrapper records the Armbian and Pi kernel source pins and
does not pretend that a moving distribution mirror is a lockfile.
The runtime fake-clock seed intentionally uses current UTC instead of this
reproducibility epoch, so an RTC-less Pi can establish HTTPS before NTP settles.

## First Boot

The image is headless and completes its initial setup through the local web
page:

- The end user selects the FXRoute user name, sets its password, and may add an SSH public key under Advanced. The account is created with the required sudo, audio, and wheel access; SSH accepts key authentication only. Without a key, SSH key authentication is unavailable until one is added locally.
- Root is locked and SSH password and keyboard-interactive authentication are disabled.
- Armbian's noninteractive `armbian-firstrun.service` remains enabled for host-key regeneration and board setup.
- Armbian's first-login marker is retained until provisioning completes. With no wired Ethernet carrier, the image starts a temporary open `hostname-armbiansetup` access point at `http://10.42.0.1`; enter the account, optional SSH key, and Wi-Fi credentials there. With a wired Ethernet carrier, the image waits briefly for DHCP, then serves `https://<dhcp-address>`; accept its self-signed setup certificate. HTTP GET redirects to HTTPS and HTTP POST is rejected before credentials are read; no access point is started while wired networking is usable.
- The Wi-Fi credentials are saved as Armbian-style Netplan configuration for the existing `systemd-networkd` stack. Ethernet has route metric 100 and Wi-Fi has route metric 600. The setup service is not enabled again after successful onboarding.
- The FXRoute first-boot service waits for the network and Armbian first-run work, extracts the source archive, and invokes the existing `install.sh` with `--user`, `--providers none`, and `--yes`.
- The installer creates the persistent systemd user session, PipeWire graph, native DSP engine, and `fxroute.service` as the target user.
- Source and onboarding metadata are removed only after the installer completes successfully. Failure leaves markers and retries on a later boot.

Spotify Desktop is never included. On ARM, install `spotifyd` later through
the existing installer path if required:

```bash
./install.sh --spotifyd
```

The existing Raspberry Pi OS installation path and the completed x86_64 ISO
path are unchanged.

## QEMU Check

QEMU 11 provides a `raspi4b` machine but no Raspberry Pi 5 machine. After
building an image, run the shared ARM64 boot check:

```bash
./armbian/test-image.sh \
  --machine raspi4b \
  dist/fxroute-armbian-rpi4-trixie-current.img
```

For the generic UEFI image, use the `virt` machine. The runner boots the
image through the host's AAVMF firmware rather than borrowing Pi boot files:

```bash
./armbian/test-image.sh \
  --machine virt \
  dist/fxroute-armbian-uefi-arm64-trixie-current.img
```

The generic `virt` runner completes the first-boot form with an ephemeral test
account and key. The runner then waits for `/api/status`. It checks the AArch64
userspace, target-user systemd services and runtime sockets, and the
`fxroute_dsp_sink`. QEMU does not provide physical ALSA output, and a Pi 5
must still be tested on Pi 5 hardware for boot firmware, storage, Ethernet,
USB, thermal, GPU, and audio behavior.

The first boot installs FXRoute inside the guest. Under QEMU's
software emulation this takes roughly one to two hours, so pass an explicit
generous timeout, for example `--timeout 7200`; the default of two hours
matches that first-boot budget, while shorter values abort a healthy but
slow install.

## Local Browser Preview

The production page is rendered by `armbian/armbian-web-config.py`, so it is
not a standalone HTML file: Wi-Fi scanning and submission use the same service
endpoints. Run its loopback-only preview directly from the checkout:

```bash
python3 armbian/armbian-web-config.py --preview --port 8765
```

Open `http://127.0.0.1:8765/` in the browser. Preview mode uses a small fixed
nearby-network list, accepts valid form submissions for iteration, and does not
create accounts or write network, SSH, or onboarding state on the host. Stop it
with `Ctrl-C`.
