# USB audio input (UAC2)

FXRoute can receive computer playback through a Linux USB device-controller
port. The computer sees **FXRoute USB Audio**; FXRoute sees an ordinary ALSA
capture card, usually named **UAC2 Gadget**.

## Enable and use

1. Open **Technical settings → Source → Input → USB Audio Gadget…**.
2. Choose **On**. The dialog reports whether the controller needs a restart.
3. Select the gadget under **External input**, or choose it as the **Device**
   in **Custom Mapping**. Selecting On does not change the current source.
4. To remove the gadget, choose **Off**. After controller setup, On/Off works
   without rebooting.

The supplied configuration is host-to-FXRoute stereo PCM, 48 kHz, 32-bit UAC2.
Its USB OUT endpoint becomes ALSA **capture** (`c_chmask=3`); the reverse
direction is disabled (`p_chmask=0`). A fixed USB rate avoids host-driven ALSA
rate changes. Existing PipeWire input routing and SRC feed the existing DSP
graph, including when the selected output uses another sample rate.
Discovery and Custom Mapping use the card's actual channels and also work
with multichannel gadget cards configured outside FXRoute.

## Requirements and detection

- A real entry in `/sys/class/udc`, plus kernel `libcomposite` and
  `usb_f_uac2` support. ConfigFS must be available.
- A UDC with suitable isochronous endpoints. Binding errors are reported;
  a failed initial activation is rolled back and does not persist On.
- A dedicated USB device/peripheral connector. The normal USB-A host ports
  are not reconfigured, host drivers are not unloaded, and other sound
  cards retain their routing.
- The root-owned helper installed by `install.sh` **only on supported hardware**.
  The installer uses the same UDC/kernel and supported Pi boot-setup probe as
  the application before creating the helper, sudoers rule or system service.
  Hosts without a suitable controller get none of these additions. Existing installations
  can install it from the checkout with:

  ```sh
  sudo bash scripts/install_usb_gadget.sh "$(id -un)"
  ```

  Read-only capability check: `bash scripts/install_usb_gadget.sh --check-support`
  (exit 0 for supported hardware, 1 otherwise). A helper installed by an older
  version on unsupported hardware is not silently deleted; remove that verified
  installation with `sudo bash scripts/install_usb_gadget.sh --remove "$(id -un)"`.

The configuration entry is hidden when no supported controller or supported
initial controller setup is detected. Detection is not restricted to ARM or
Raspberry Pi: other boards exposing a usable UDC follow the same path.

### Raspberry Pi 4/5 and Armbian

Use the dedicated USB-C device port, not a USB-A host port. The actual kernel
and board image determine whether it can operate as a gadget.

For Pi 4 Model B and Pi 5 Model B firmware layouts, initial setup is offered
only when the live device tree exposes the dedicated `usb` controller symbol,
its compatible controller type is known, the `dwc2` kernel driver is available,
and the image contains both `config.txt` and `overlays/dwc2.dtbo` under
`/boot/firmware` or `/boot`. A Pi model string alone is insufficient.

If no UDC is present, On backs up `config.txt` to
`config.txt.fxroute-usb-backup` and appends a marked block containing
`dtoverlay=dwc2,dr_mode=peripheral`. The setting is saved and the dialog requests
a restart. Conflicting `dwc2`/`dwc-otg` overlays or `otg_mode=1`, including in
included files, prevent automatic modification and require manual review.
Other Armbian boot layouts require board-specific peripheral-port setup first;
FXRoute does not guess an overlay or change an arbitrary dual-role host port.

Off removes the audio gadget, not the controller overlay. Keeping this initial
setup permits later hot toggling. Uninstallation also leaves this controller
setup and its backup available for deliberate administrator review.

### Power and cable state

An ordinary USB-C power supply can still power the board with the gadget
enabled. In that case the dialog reports that it is waiting for a USB host.
Do not expect a power-only cable to carry USB audio. When connected to a
computer, provide the power arrangement required by the board; software
cannot establish whether that computer can supply enough power.

The ALSA card remains usable for input configuration without a connected
computer. Its presence, or a UDC that is merely powered/addressed/suspended,
does not trigger Auto. The monitor checks the **live** UDC `configured` state
every tick, even when its input inventory is cached. Connected USB alone is
also insufficient: normal signal detection or an explicit presence report is
still required. Disconnection suppresses both capture probes and external
presence reports for that gadget.

## Ownership and persistence

- FXRoute creates only `/sys/kernel/config/usb_gadget/fxroute_audio`.
- It does not adopt an existing same-name gadget, replace other functions,
  or unbind another gadget. A free UDC can coexist with an occupied one.
  Multiple unclaimed UDCs require an unambiguous system configuration.
- Desired state is stored atomically in
  `/var/lib/fxroute-usb-gadget/state.json`. The system service
  `fxroute-usb-gadget.service` restores it at boot, retrying boundedly while
  the kernel makes the controller available. A missing/busy saved controller
  never causes fallback to an unrelated port.
- The root helper is a standalone, isolated Python program at
  `/usr/local/sbin/fxroute-usb-gadget`. The application receives sudo
  permission for precisely its `on` and `off` actions. It cannot supply paths,
  shell commands, descriptors or boot configuration content.
- Changes are locked across processes. The installer records hashes of its
  installed helper, unit and sudoers rule, and refuses to replace externally
  modified files. `uninstall.sh` removes a verified installation after turning
  its gadget off.

Read-only status: `GET /api/audio/usb-gadget`. Configuration:
`POST /api/audio/usb-gadget` with `{"enabled": true}` or `{"enabled": false}`.
The input overview also includes the status under `usb_gadget`.

## Verification without physical hardware

Targeted automated checks:

```sh
python3 scripts/test_usb_gadget_system.py
python3 scripts/test_usb_gadget_installer.py
python3 scripts/test_usb_gadget_input.py
python3 scripts/test_usb_gadget_api.py
node scripts/test_usb_gadget_ui.js
python3 scripts/test_external_input_stereo_pairs.py
python3 scripts/test_custom_input_mapping.py
python3 scripts/test_input_auto_select.py
python3 scripts/test_external_input_unavailability.py
node scripts/test_custom_mapping_ui.js
```

The filesystem simulation covers generic UDCs, missing kernel support, Pi
boot prerequisites, foreign gadgets, ambiguous/busy controllers, binding
rollback, persistent On/Off, restart restoration and modified configuration
protection. Input tests cover actual channel-pair and mapping construction,
live USB states, cached inventories, presence feeds and ordinary USB inputs.

### QEMU results (2026-10-19)

- QEMU 11.1.2 `raspi4b` boots the available Raspberry Pi 4 DTB and
  `6.18.34+rpt-rpi-v8` kernel. The initial boot probe reached the root-device
  wait; this is a platform boot check, **not** a gadget transport test.
- A disposable generic ARM64 `virt` guest runs Debian kernel
  `6.12.101+deb13-arm64`. Controller simulations pass inside ARM64 userspace.
- Linux `dummy_hcd` rejects UAC2's isochronous endpoint allocation. The real
  ConfigFS bind failure is cleanly rolled back, leaving no gadget and no
  persisted On state.
- Linux `usbip-vudc` in that guest successfully binds the real UAC2 function
  and creates ALSA `UAC2Gadget` while disconnected. FXRoute correctly reports
  active configuration, no connected host, and a disconnected gadget capture
  card. This tests kernel ConfigFS/ALSA behavior rather than a filesystem mock.
- A virtual `vhci-hcd` host attaches through USB/IP and enumerates **FXRoute USB
  Audio** with the Linux USB-audio driver. The UDC becomes `configured` and
  FXRoute's connection reader changes from false to true. PipeWire discovers
  `alsa_input.platform-usbip-vudc.0.stereo-fallback` as 2-channel, 48 kHz capture.
  A PCM transfer attempt fails: the virtual host reports `Not yet implemented`
  for isochronous traffic and capture returns an I/O error. Enumeration is
  verified; virtual audio transport is **not** verified.
- The enabled state is restored after a guest reboot with the virtual UDC
  loaded at boot. Repeated On, Off and service restoration of Off succeed
  against real ConfigFS. The installer also accepts a verified reinstall.

### x86 regression verification (2026-10-19)

On an Intel N150 host without a UDC, the browser hides the gadget entry and
the API rejects activation without changing other USB devices. The existing
11 input entries and armed-input state are retained. Selecting Scarlett
input 9–10 succeeds, Custom Mapping lists its 18 capture channels, and the
native DSP remains active without a runtime error. The original App playback
selection is restored after this test. Served page/JavaScript assets match
the deployed source. Focused gadget, auto-select, mapping and stereo-pair
tests also pass on that host; no full suite is run.

**Hardware-unverified:** physical Pi 4/5 controller initialization under an
Armbian image, USB-C electrical/PD behavior and power margins, host enumeration
and interoperability on physical Windows/macOS/Linux computers, sustained
isochronous audio, clock drift, cable reconnects and simultaneous physical
USB DAC operation. Simulated controllers do not establish those properties.
