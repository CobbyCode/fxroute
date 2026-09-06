# FXRoute Manual

FXRoute turns a small Linux audio PC into a browser-controlled music and DSP system. A phone, tablet, or laptop on the local network controls playback, switches DSP presets, compares profiles, imports filters, and runs room measurements — the audio machine itself needs no screen.

This is the short manual. The [README](README.md) covers the feature overview, installation, and the web demo; [docs/INSTALLER.md](docs/INSTALLER.md) covers the installer and streaming providers in depth.

## 1. What FXRoute is for

FXRoute assumes one audio machine running a Linux user session with an active PipeWire stack (desktop or headless CLI with user services enabled).

On that machine, FXRoute provides:

- a web interface for local music, internet radio, Spotify, Qobuz, TIDAL, and Bluetooth input
- a native DSP engine that all playback enters through (`fxroute_dsp_sink`)
- DSP presets, A/B compare, filter imports, and output-mode routing
- room and speaker measurement that feeds PEQ and FIR/convolver preset creation
- control of the whole setup from browsers on the local network

Spotify, Qobuz, and TIDAL integration is unofficial and builds on community backends and protocols.

FXRoute exposes its web UI on the LAN. Keep it on a trusted network.

## 2. Install and first start

Three supported ways to get FXRoute onto the audio machine:

- **Classic install.** `git clone https://github.com/CobbyCode/fxroute.git && cd fxroute && ./install.sh`. The installer prepares system packages, builds the native DSP engine, creates the Python virtualenv, and enables the `fxroute.service` user service. Run it as the audio user; from a root shell pass `--user <name>` when the host has several users.
- **ARM64 Armbian image.** Write the image, boot, and finish the web onboarding; first boot installs FXRoute and enables the `.local` name and HTTPS. See [docs/INSTALL-ARMBIAN.md](docs/INSTALL-ARMBIAN.md).
- **x86_64 installation ISO.** Write the ISO, boot, pick the `FXRoute Headless` or `FXRoute Desktop` profile, and complete the installer. See [docs/INSTALL-ISO.md](docs/INSTALL-ISO.md).

Streaming providers are optional and are managed later in **Technical settings → Providers**; the installer flags select the same backends, e.g. `./install.sh --providers spotifyd,qobuz,tidal`.

Open FXRoute from any browser on the same network:

- `http://fxroute.local:8000` (default name; change it in **Technical settings → Device Name**)
- `http://<host-ip>:8000`
- `http://localhost:8000` on the audio PC itself

With the optional local HTTPS proxy enabled, `https://<host-ip>` and `https://<device-name>.local` also work; HTTP on port 8000 stays reachable.

The service is `fxroute.service`. Quick host commands:

```bash
systemctl --user status fxroute
systemctl --user restart fxroute
journalctl --user -u fxroute -f
```

## 3. The interface

The main tabs are **Radio**, **Library**, and **DSP**, plus the provider tabs **Spotify**, **Qobuz**, and **TIDAL** once installed and shown. The FXRoute logo in the top-left opens **Technical settings**; the power menu next to it can **Suspend** or **Shut down** the audio PC when the host supports it.

The playback bar at the bottom is always visible:

- play/pause, previous/next, seek, and the current queue
- the **volume slider is the FXRoute master volume**, the main listening level
- the level badge shows the live peak against the DSP chain; radio and library rows show stream tech lines (codec, bitrate, sample rate)
- click the current cover to open the detail card with metadata, "Recently played" where the source provides it, and the queue

Source and app volumes (Spotify client, Qobuz app) stay separate from the FXRoute master volume.

## 4. Sources

### 4.1 Radio

Play from the curated catalog or from **My Stations** (your saved streams). Radio lets you:

- search Radio Browser by station, genre, or country (low-quality streams are filtered out)
- add, edit, and delete personal stations, with custom artwork
- export your station list
- see live metadata and artwork for stations with dedicated providers (Radio Paradise, FIP, SomaFM, KEXP) in the current-track card

Curated stations appear under **Station Catalog** until you add them to **My Stations**.

### 4.2 Library and network shares

**Library** plays music from the local folder or from a network share, in album, track, or folder views. It supports MP3, FLAC, WAV, OGG/Opus/WebM, M4A, M3U/M3U8 playlists, and album ZIP imports; uploads, URL imports, downloads, playlists, favorites, and multi-select are handled in the tab.

FXRoute treats local tags and cover files as authoritative and enriches albums opportunistically with cached MusicBrainz IDs, Cover Art Archive covers, artist summaries, and similar-artist suggestions. Scans stay fast because unchanged tracks are cached by path, mtime, and size.

The active library is selected in **Technical settings → Music Library**. FXRoute discovers accessible SMB shares; a share that is not found can be added manually as `smb://server/share`. The host needs the installer-provided CIFS support to mount it.

### 4.3 Spotify

Spotify pairs through Spotify Connect, not a login inside FXRoute: install the backend in **Technical settings → Providers**, open the Spotify app on a phone or computer, and select the FXRoute device. Two backends are supported:

- **Spotify Desktop** — the official client on an x86_64 desktop session, controlled through MPRIS. A current client (1.2.67+) can also deliver Lossless streams for eligible Premium accounts; FXRoute only provides the remote control, not the stream.
- **spotifyd** — a headless player for sessions without a desktop. The installer pins its Zeroconf port so phones find the FXRoute player reliably. Lossless is not available through spotifyd.

The tab offers play/pause, next/previous, seek, shuffle, loop, volume, cover art, and track metadata. Metadata follows the local player, so automatic track changes update without a manual refresh. When the backend is idle the tab says **Ready for Spotify Connect.**; when it is not running it explains how to start it. Spotify may trigger a Linux keyring unlock prompt after login (on XFCE, install and set up `seahorse` first).

### 4.4 Qobuz

The Qobuz tab controls a **Qobuz Connect** player (`qbzd`) on the audio PC. Connect the account in **Technical settings → Providers**: press **Connect**, open the shown sign-in link, paste the redirect URL back, and confirm. Playback starts from the Qobuz app by selecting the FXRoute device; the tab then offers the same transport controls as the other sources. Stream quality follows your Qobuz account and app settings. The Qobuz client runs at unity gain, so volume changes are handled by the FXRoute master slider.

### 4.5 TIDAL

TIDAL is a full catalog browser inside FXRoute. After installing the backend you can browse tracks, albums, artists, and playlists, search, favorite, create or extend playlists, and play natively — TIDAL audio runs through FXRoute's DSP chain and playback bar like local files.

Two login methods exist in the TIDAL tab:

- **Browser login (PKCE)** — copy the login link, sign in on any device, paste the redirect URL back. Only this method unlocks Lossless and Hi-Res playback.
- **Device login** — enter a code at link.tidal.com. Faster, but limited to AAC 320 kbps.

Disconnect in **Technical settings → Providers**; playback stops until you sign in again.

## 5. DSP

Open **DSP** to shape the sound. The **Measure** button on this page opens the measurement assistant (section 6).

### 5.1 Presets, compare, import

- **Presets** are selectable chains. **Direct** bypasses the whole processing chain including the global helpers; **Neutral** is a clean chain that keeps the helpers active and is the default. Both are built in and cannot be deleted.
- **A/B compare** switches between two presets while you listen. Preset A and B choose the slots; the header shows which side is active.
- **Combine** builds one preset from up to three presets in order.
- **Import filter** loads stereo or separate left/right corrections: FXRoute preset JSON, convolver `.irs`, WAV impulse responses, or pasted REW-style filter text.
- **Create PEQ preset** builds paired left/right bands and can add a channel gain trim and delay. The EQ engine runs IIR, FIR, FFT, or SPM modes.

### 5.2 Output helpers

**Output extras** apply automatically on top of every preset except **Direct**:

- **Protection limiter** — protects the final output from peaks
- **Headroom** — safety margin from −2 to −6 dB
- **Autogain** — drives the programme level toward −12, −15, −18, or −23 LUFS
- **Loudness** — calibrated contour that follows the playback level; **Strength** (1–10) and an **FFT** size set the contour. It works at its own DSP level and never moves the master volume; when Autogain is active, the contour accounts for the Autogain target too
- **Bass enhancer** — adjustable low-frequency enhancement
- **Tone effect** — **Crystalizer** or **Maximizer** modes

Autogain and Loudness can run together; the limiter stays the final stage.

### 5.3 Output modes and subwoofers

**Technical settings → Output Mode** offers:

- **Stereo** — mains only
- **2.1 Subwoofer** — mains plus one mono subwoofer (Out 3/4)
- **2.2 Subwoofer** — mains plus two mono subwoofers (Out 3 = Sub 1, Out 4 = Sub 2), configured independently
- **2.2 Stereo Bass** — mains plus a left sub (Out 3) and a right sub (Out 4) driven from their own channels

The **Crossover / Subwoofer** card on the DSP page shows the active routing, a crossover preview, the crossover frequency (40–200 Hz, LR24), the main highpass, and per-sub level, alignment, and polarity. In the 2.2 modes, Sub 1 and Sub 2 are set separately; the derived Main/Sub delays are shown for reference. Subwoofer modes need an output device with at least four channels.

### 5.4 Sample rate

**Sample Rate** defaults to **Auto**: the PipeWire graph and DAC follow the effective playback rate of the current source (local files, radio, Spotify, Bluetooth can differ). A fixed rate forces one clock; FXRoute rejects rates the selected output does not support and caps processing at 384 kHz. Switching policy can restart the audio path, so stop playback first and re-check the output afterwards. Use Auto when sources with different native rates play together; use a fixed rate when the DAC, DSP chain, or external hardware needs one clock.

## 6. Measurement assistant

Open **Measure** on the DSP page. The assistant plays sweeps through the active DSP chain, records the response from a host microphone, and turns saved measurements into PEQ or FIR/convolver corrections. A microphone input is selected in **Setup**, where you can also load a calibration file and choose an electrical-reference input.

### 6.1 Sweeps and workflow

- **Start Sweep → L, R, or Stereo** runs one measurement. The status line shows the input level (`Peak … dBFS`, `Peak < -90 dBFS`, or `CLIP`).
- **Start LR Repeat** measures left and right three times each at one fixed microphone position and shows one combined result. **Save current** stores the pair as `<name> · L` and `<name> · R`; the internal repeats are never added to the saved list. Use the mode when you want a more dependable L/R pair for correction or aligned FIR modes.
- The graph shows the frequency response from 20 Hz to 20 kHz with smoothing (raw, 1/6, 1/3, or 1 octave); the **IR** toggle shows a compact impulse-response preview where available. The preview is a timing/reflection sanity check, not a full IR export.
- **Save current** keeps a run under a name of your choice. Saved runs appear in the list; checkboxes show/hide runs on the graph, and individual runs can be exported or deleted.

### 6.2 Correction tools on the graph

- **PEQ** — sketch up to 12 temporary filters (F1–F12), edit frequency, gain, type, and Q, and use **Take L / Take R / Take Both** to build a new PEQ preset.
- **Custom House Curve** — create a target curve from up to 8 frequency/gain points on the graph (P1–P8), then use it as the Convolver target curve. House-curve and calibration files can be uploaded in **Setup** and exported again.
- Target-curve choices: **Neutral**, **Bass Shelf**, **Harman-style**, **Bruel & Kjaer-style**, or a custom curve.

### 6.3 SPL calibration

SPL Calibration plays **−23-LUFS pink noise** (83 dB SPL target) to level-tune the output profile. With a UMIK-1, UMIK-2, or Dayton UMM-6, FXRoute measures the SPL automatically; otherwise enter a C-weighted, Slow meter reading manually. Autogain and Loudness are temporarily neutralized during the run and restored exactly when it stops, saves, or fails.

### 6.4 Advanced workflow

**Advanced** guides a multi-position measurement: direct response ~1 m from each speaker, left/right at the main listening position and 20–30 cm to each side, and an integration step when subwoofer routing is active. FXRoute validates position, L/R timing, and the summed response and asks you to repeat a step after correcting it. Keep the microphone at ear height and move it only when a step asks. Use the electrical reference input when available — it makes the timing analysis measurably more precise than acoustic-only captures.

### 6.5 Auto Sub Optimize

**Auto Sub Optimize** scans subwoofer alignment candidates around the currently configured values, applies the verified delay, polarity, and gain for the active mode, and works per mode:

- **2.1** — one shared alignment for a mono sub, checked against both mains
- **2.2** — Sub 1/Sub 2 alignment combinations evaluated as one dual-sub system
- **2.2 Stereo Bass** — left and right sub/main branches optimized separately

Gain candidates stay within ±6 dB and are verified against the selected target curve before they stick; the four final stage outputs are checked at the DAC against the full-scale limit. If you know a sensible starting delay (for example from a subwoofer manual), enter it first — the scan centers on it. Keep the microphone fixed and stay quiet during the run.

**Recommended order with EQ or convolver correction:** set crossover and levels roughly, run Auto Sub Optimize, verify with a normal measurement, create and enable the correction from that state, then run a final verification measurement. Repeat the optimizer only if a new correction materially changes phase or delay around the crossover.

### 6.6 Convolver presets

The Convolver assistant turns saved L/R measurements into a FIR preset: choose the visible saved runs (exactly one Left and one Right for stereo), select the target curve, correction range, sample rate, and tap length, then create the preset. Phase modes:

- **Linear phase** — symmetric FIR
- **Minimum phase** — default for broad room/speaker correction
- **Minimum phase aligned** — minimum-phase correction with measured L/R direct-arrival alignment
- **Hybrid aligned** — minimum-phase bass blended into zero-delay linear-style upper correction

The aligned modes need separate saved L/R measurements with valid direct-arrival timing. FXRoute blocks filter creation when the measured L/R timing offset is unsafe; the graph shows the arrival relation (for example `L arrives 5.27 ms later than R`).

## 7. Technical settings

The FXRoute logo opens **Technical settings**:

- **Providers** — install or remove each streaming backend, show/hide its tab, and connect or disconnect accounts
- **Audio Output** — output device, **Output Mode** (Stereo, 2.1, 2.2, 2.2 Stereo Bass), and **Sample Rate** policy
- **Music Library** — local folder or a discovered/entered SMB share
- **Source** — active input mode and Bluetooth status
- **Device Name** — the `<name>.local` address on the LAN
- **Amplifier Controller** — status and controls when a supported USB amplifier controller is connected (RCA/XLR input, Press Input, Auto On/Off); the section just says none was detected otherwise
- **Maintenance** — installed version, update check, update, and update log
- **HTTPS certificate** — download link when the local HTTPS proxy is enabled

## 8. Maintenance

**Technical settings → Maintenance** shows the installed version, checks for updates, and runs them. Updates are blocked while the checkout contains uncommitted changes; **Restore to Public Release** then saves those changes as a patch and resets the checkout — use it only when you no longer need the local changes.

## 9. Local HTTPS certificate

When the optional HTTPS proxy is enabled, FXRoute runs a local certificate authority for the audio PC. Download its certificate from **Technical settings → HTTPS certificate** and import it as a trusted CA only on devices you control on your own LAN. If the CA is regenerated, client devices need the new certificate again.

## 10. Home Assistant / external automation

FXRoute exposes `GET /api/power/state` as a read-only power hint: `amp_should_be_on` is true while playback is active or the Measurement Assistant is open. A Home Assistant or similar automation can use it to switch an amplifier smart plug; FXRoute needs no MQTT broker and never controls the plug itself.

A minimal configuration polls the endpoint as a binary sensor and switches the plug on when playback starts and off after an idle period:

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

## 11. If something fails

1. Start with **Radio** — it is the simplest source and proves output and DSP routing.
2. Check the playback bar: does it show a track and a level?
3. Open **Technical settings** and confirm the output device and mode.
4. Restart the service and watch its log:

```bash
systemctl --user restart fxroute
journalctl --user -u fxroute -f
```

5. If the native DSP graph is the suspected problem:

```bash
wpctl status
pw-cli ls Node | grep fxroute_dsp
```

6. Reload the browser when it reports a disconnect after a service restart.

## 12. What FXRoute expects

- a Linux user-session audio machine (desktop, or headless with user services enabled)
- PipeWire running in that session, with the FXRoute DSP engine in the same graph
- browsers on the local network as the control surface
- a DAC, amp, active speakers, headphones, or similar listening setup
- optionally a Spotify client/spotifyd, qbzd, or TIDAL backend for provider playback
- a host microphone input for measurement; a calibrated USB measurement mic enables automatic SPL calibration

FXRoute does not run as a system daemon and does not replace Spotify Connect or Qobuz Connect — it controls the local players through the user session.
