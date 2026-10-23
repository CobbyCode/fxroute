# FXRoute Ubuntu 26.04 — hardware test plan

Goal: verify the ISO that passed the full QEMU acceptance on real hardware,
using the product ISO (the published release ISO, or a build without
`--test-seed`; menu: `Try FXRoute Live` / `Install FXRoute Desktop` /
`Install FXRoute Headless`).
The four blocks build on each other; a failing block means stop and keep the
logs. The software checks can be run over SSH; this plan lists the manual steps
and the expectations.

One-time preparation:

* Target PC: UEFI mode (no CSM), leave Secure Boot **enabled** in the setup.
* USB stick written with the product ISO (for example
  `sudo dd if=fxroute-<version>-x86_64-ubuntu26.04.iso of=/dev/sdX bs=4M oflag=direct status=progress`; at least as large as the ISO).
* Ethernet optional (not required — Wi-Fi is configured in the installer),
  Scarlett connected over USB, speakers/monitor on the main output.
* After the first boot the device name is `fxroute-XXXXXX` (machine-id
  suffix); the SSH password is the user's installer password.

## Block A — Secure Boot (signed Ubuntu chain only)

1. Select the ISO/stick in the UEFI boot menu; the shim must load (no MOK
   enrolment screen, no "Security Violation").
2. GRUB → `Try FXRoute Live` starts normally (Secure Boot on).
3. GRUB → `Install FXRoute Desktop` (or Headless) → let the installation run.
4. The installed system boots with Secure Boot on (no MOK, no
   "Verification failed").

Checks (on the host after the first boot):

```bash
mokutil --sb-state                 # expected: SecureBoot enabled
sudo efibootmgr -v                 # ubuntu + fxroute entries present
bootctl status 2>/dev/null | head  # Secure Boot: yes (if systemd-efi)
```

Passed when: all three steps run without Secure Boot errors; `mokutil`
reports "SecureBoot enabled"; no own kernel/MOK was needed.

## Block B — Wi-Fi: once in the installer, automatic afterwards

1. In the installer (`Install FXRoute Desktop`/`Headless`, Subiquity network
   page) select the target Wi-Fi, enter the password, **continue** (no other
   network step).
2. Finish the installation, remove the stick, let it reboot.

Checks after the first boot (no second setup):

```bash
nmcli -f GENERAL,IP4 dev show wlan0   # state: connected (activated)
nmcli -g GENERAL.STATE dev wlan0      # "connected"
nmcli -g 802-11-wireless.ssid connection show "$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2==\"802-11-wireless\"{print \$1}')"
ip -4 addr show wlan0 | grep inet
```

Passed when: after the first boot, without any user action, an active Wi-Fi
connection with an IPv4 address exists (netplan/Curtin carried over the
connection set in the installer).

## Block C — first boot / autologin / kiosk

1. After the installer reboot: no login screen; GDM logs the installer user
   in automatically (the GNOME desktop appears).
2. Shortly afterwards Firefox starts **full screen (kiosk)** with FXRoute.

Checks:

```bash
test -f /var/lib/fxroute-iso/install-complete && echo marker-ok
systemctl --user is-active fxroute.service
loginctl list-sessions --no-legend        # the user's seat0 session
pgrep -af "firefox --kiosk"
curl -fsS http://127.0.0.1:8000/api/status | head -c 200
```

Also by hand: the desktop link "FXRoute" exists, Firefox shows the FXRoute
interface; after 5 minutes idle there is **no** lock or dimming.

## Block D — Scarlett (18→14→10→18) including playback/VU/AUX links

Preparation: Scarlett (18i20 or similar) connected over USB; `pactl list cards
short` or `pactl list cards | grep -A2 scarlett` shows the device with its
profiles. FXRoute was installed with `--providers none` → DSP chain through
PipeWire.

Short script (one command per step, all output for the log):

```bash
pactl list cards short                        # Scarlett card visible
pactl set-card-profile <scarlett-card> output:analogue-output-surround-71 ...
```

Scenario (each step by hand on the device plus a logged software check):

1. **18 inputs/outputs active** (analog profile, all channels visible).
2. **→ 14 channels:** reduce the channel count in the Scarlett hardware panel
   or the FXRoute UI; `pactl list sinks` shows fewer channels; running playback
   survives the change or restarts cleanly.
3. **→ 10 channels:** likewise; check that AUX/routing links in the FXRoute UI
   do not get stuck (check the AUX link state per channel).
4. **→ back to 18:** all channels are back; playback/VU show values again.

Check continuously during 2–4:

* **Playback:** test tone/stream runs without dropouts; sample rate per
  `pactl list sinks | grep -E "Sample|Channel"` as expected.
* **VU/peak:** FXRoute `/api/status` → `output_peak_warning.detected` is `true`
  with a loud signal; `vu_db_l/r` move.
* **AUX links:** AUX outputs follow the main routing (links active in the UI,
  no orphaned links after a channel-count change).
* **USB log:** `sudo dmesg -w | grep -iE "usb|scarlett"` shows clean
  enumeration/re-enumeration without `cannot set freq` errors;
  `journalctl -b -u fxroute` without DSP errors.

Passed when: every channel-count change is survived without a restart,
playback stays audible and running, VU values are live, AUX links are intact,
and the USB log shows no errors.

## Stop criteria / follow-up

* A fails → Secure Boot with a shim error: save the boot log, open an issue.
* B fails: save `networkctl status wlan0` and `/etc/netplan/*.yaml`.
* C fails: check `journalctl -u fxroute-first-boot -b` and the desktop file
  `/home/<user>/Desktop/FXRoute-NOT-STARTED.txt`.
* D fails: save a `dmesg` excerpt and `pactl list cards`; if needed, handle the
  Scarlett profile change through `alsa-ucm-conf`.
