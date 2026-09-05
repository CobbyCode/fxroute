# FXRoute

FXRoute is a browser-based control surface for a Linux hi-fi audio box. One small PC or ARM board with a PipeWire user session becomes the source and DSP hub for local music, internet radio, Spotify, Qobuz, and TIDAL — controlled from any phone, tablet, or laptop on the local network.

Radio, the music library, the streaming providers, the native DSP engine, and the room-measurement tools all live in one interface. You browse and play from the couch; FXRoute owns the audio session, the DSP chain, and the output routing on the audio machine.

## Web demo

Try the interface without any audio hardware. The demo is the real FXRoute frontend — same checkout, same UI — with a simulated backend for playback, DSP, radio, and measurement, so every view is explorable in a normal browser.

A stable demo link will be added here once the demo is published. Until then, run it locally:

```bash
git clone https://github.com/CobbyCode/fxroute.git
cd fxroute
python3 scripts/serve_demo.py     # then open http://127.0.0.1:8765
```

The demo page carries a small notice saying the backend is simulated. A static, publishable snapshot can be built with `python3 scripts/build_demo.py` (see `demo/README.md` for subpath hosting). The demo reuses the live frontend from this checkout, so it always reflects the current UI — there is no second UI source to keep in sync.

<p align="center">
  <img src="media/screenshots/radio-overview.png" width="32%" alt="FXRoute radio catalog with a station playing">
  <img src="media/screenshots/dsp.png" width="32%" alt="FXRoute DSP page with A/B compare and subwoofer controls">
  <img src="media/screenshots/measurement.png" width="32%" alt="FXRoute measurement assistant with a live sweep">
</p>

## What FXRoute does

**Sources**

- Local music library with album browsing, playlists, favorites, uploads, album ZIP imports, media-URL imports, and downloads
- Internet radio with a curated station catalog, personal stations, and Radio Browser search; live metadata and artwork for Radio Paradise, FIP, SomaFM, and KEXP
- Spotify control for a local desktop client or spotifyd (Spotify Connect pairing, no FXRoute login), including Lossless-aware playback through a current desktop client
- Qobuz Connect player control and a full TIDAL catalog browser with native playback through FXRoute's audio engine
- Bluetooth input visibility and control when the host audio stack supports it

**DSP and output**

- A native DSP engine (`fxroute_dsp_sink`) built from source, in the same PipeWire session as playback
- Presets with A/B compare, preset combining, and filter imports; two always-available presets: **Direct** (bypasses everything) and **Neutral** (clean chain, the default)
- Global output helpers that apply on top of any preset: protection limiter, headroom, autogain, loudness contouring, bass enhancer, and tone effect
- Stereo, 2.1, 2.2, and 2.2 Stereo Bass output modes with crossover, level, polarity, and alignment controls
- Auto or fixed sample rate (up to 384 kHz, device permitting)

**Measure and correct**

- Host-microphone room and speaker measurement: single L/R/Stereo sweeps, same-position L/R Repeat, and a guided multi-position Advanced workflow
- SPL calibration with UMIK-1/UMIK-2/Dayton UMM-6 support or a manual meter
- Auto Sub Optimize for subwoofer alignment in subwoofer output modes
- Correction tools on the measurement graph: a 12-filter PEQ sketchpad, custom House Curves, and FIR convolver preset creation in linear, minimum-phase, and aligned modes

**System**

- Control from any browser on the LAN; SMB music shares in addition to the local folder
- Optional local HTTPS via Caddy with a downloadable certificate
- `GET /api/power/state` as a read-only hint for amplifier smart-plug automation
- In-app maintenance updates, plus ready-made installers (see below)

## Install

**Classic install** on a Linux PC or board with PipeWire:

```bash
git clone https://github.com/CobbyCode/fxroute.git
cd fxroute
./install.sh
```

The installer prepares the system packages, builds the native DSP engine, creates the Python virtualenv, and enables the `fxroute.service` user service. Run it as the audio user; from a root shell on a host with several users, pass that user explicitly with `./install.sh --user <name>`. Custom install targets must be dedicated directories named `fxroute` unless `--local-project` is used.

**Ready-made images**

- ARM64 Armbian image with web onboarding — [docs/INSTALL-ARMBIAN.md](docs/INSTALL-ARMBIAN.md)
- x86_64 openSUSE Leap installation ISO — [docs/INSTALL-ISO.md](docs/INSTALL-ISO.md)

Both images start with no streaming providers. Providers are added later in **Technical settings → Providers**, or at install time with installer flags:

```bash
./install.sh --providers spotify-desktop,spotifyd,qobuz,tidal
```

Supported package managers are apt, dnf, zypper, and pacman. The provider matrix, first-run authentication, and uninstall behavior are covered in [docs/INSTALLER.md](docs/INSTALLER.md).

## First start

- `systemctl --user status fxroute` — the service is `fxroute.service`.
- Open the UI from any browser on the network:
  - `http://fxroute.local:8000` (mDNS; the name is set in **Technical settings → Device Name**)
  - `http://<host-ip>:8000`
  - `http://localhost:8000` on the audio PC itself
  - `https://<host-ip>` or `https://<device-name>.local` when the optional HTTPS proxy is enabled (HTTP stays reachable)
- Play something from **Radio** or **Library** to confirm audio and DSP routing.
- The music folder is configured in `.env` (`MUSIC_ROOT`) or selected in **Technical settings → Music Library**, which also lists discovered SMB shares.

FXRoute runs in a Linux user session with an active PipeWire audio stack — desktop or headless CLI with user services enabled. It is not intended to run as a system daemon. Playback applications enter the processing graph through the `fxroute_dsp_sink` Pulse/PipeWire sink.

## Home Assistant

`GET /api/power/state` is a read-only amplifier power hint for external automation: `amp_should_be_on` is true while local or Spotify playback is active or the Measurement Assistant is open. FXRoute does not require MQTT and never controls the plug itself.

```yaml
rest:
  - resource: "http://fxroute.local:8000/api/power/state"  # Adapt host/port if needed.
    scan_interval: 5
    binary_sensor:
      - name: "FXRoute amp should be on"
        value_template: "{{ value_json.amp_should_be_on }}"

automation:
  - alias: "FXRoute amp on"
    trigger:
      - platform: state
        entity_id: binary_sensor.fxroute_amp_should_be_on
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.verstaerker_steckdose  # Adapt to your smart plug.

  - alias: "FXRoute amp off after idle"
    trigger:
      - platform: state
        entity_id: binary_sensor.fxroute_amp_should_be_on
        to: "off"
        for:
          minutes: 20
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.verstaerker_steckdose  # Adapt to your smart plug.
```

## Documentation

- [MANUAL.md](MANUAL.md) — the short user manual
- [docs/INSTALLER.md](docs/INSTALLER.md) — installer, providers, and uninstall details
- [docs/INSTALL-ARMBIAN.md](docs/INSTALL-ARMBIAN.md) and [docs/INSTALL-ISO.md](docs/INSTALL-ISO.md) — ready-made images
- [CHANGELOG.md](CHANGELOG.md) — release history

## License

AGPL-3.0 — see [LICENSE](LICENSE).
