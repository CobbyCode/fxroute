# FXRoute Manual

FXRoute turns a small Linux audio PC into a browser-controlled music and DSP system.

Use a phone, tablet, or laptop on the local network to control playback, switch EasyEffects presets, compare DSP profiles, import filters, measure the room, and tune the result without using the desktop.

## 1. What FXRoute is for

FXRoute puts these tasks on one local hi-fi control system:

- play internet radio, Spotify, and local music
- browse local albums with artwork, artist context, and discovery hints
- control volume, queue, play/pause, and track position from the browser
- route audio through EasyEffects for live DSP
- switch room-correction, PEQ, convolver, and tone presets
- compare DSP presets quickly with A/B switching
- measure the room/speaker response and use it as a tuning guide
- expose the setup safely on the local network

FXRoute runs in a Linux desktop audio session. It is not intended for a fully headless server.

## 2. Opening FXRoute

Open FXRoute from a browser on the same network:

- `http://fxroute.local`
- `http://<host-ip>:8000`
- `http://localhost:8000` on the audio PC itself

If the optional local HTTPS proxy is enabled, use:

- `https://fxroute.local`
- `https://<host-ip>`

The top-left FXRoute logo opens **Technical settings**.

## 3. Basic listening workflow

A typical listening session:

1. Start music from **Radio**, **Spotify**, or **Library**.
2. Use the bottom playback bar for play/pause, volume, seek, and queue control.
3. Open **DSP** to choose or compare the sound profile.
4. If you want to tune the room, open **Measure** from the DSP page.
5. Save useful measurements, transfer correction ideas into a new PEQ preset, or use the visible measurements to create a Convolver preset.

EasyEffects handles the live audio processing. FXRoute controls, organizes, compares, and edits the presets.

## 4. Radio

Use **Radio** for internet radio playback.

You can:

- play stations from the curated catalog, grouped by genre with station
  artwork (SomaFM, Radio Paradise, FIP, KEXP, and more)
- search stations via Radio Browser by name, genre, or country, with
  low-quality streams filtered out
- add your own stream URLs to the personal catalog
- edit the name, URL, and artwork of personal stations
- delete personal stations you no longer use
- see live stream info (codec, bitrate, sample rate) in the playback bar
- open the cover detail card for the current station, with live metadata
  and artwork when the station provides them (Radio Paradise, FIP, SomaFM,
  and KEXP are enriched with dedicated providers)

Radio is a quick way to check playback, output selection, and DSP routing.

## 5. Spotify

Use **Spotify** to control the Spotify desktop app in the same Linux user session as FXRoute.

You can:

- play/pause
- previous/next track
- seek within a track
- control Spotify volume
- toggle shuffle and loop
- see cover art and track metadata

FXRoute refreshes Spotify metadata from local desktop events and lightweight polling, so automatic next-track changes should update title, artist, cover, duration, and position without needing a manual browser action.

FXRoute does not replace Spotify Connect. It controls the local Spotify client through the desktop session, so Spotify must be installed on and reachable from the audio PC.

The regular Spotify desktop client also supports Spotify Lossless for eligible Premium accounts. Enable **Lossless** in a current Spotify desktop client (version 1.2.67 or newer) to stream available music at up to 24-bit/44.1 kHz FLAC while FXRoute continues to provide remote playback control. FXRoute controls the client; it does not provide the Spotify stream itself.

On fresh installs, Spotify autostart is enabled by default when a local Spotify desktop client is available. Installer reruns preserve an existing `.env`, so an already configured system keeps its current setting.
Spotify may trigger a Linux keyring unlock prompt after login.
On XFCE, the graphical keyring tool may need to be installed first:

```bash
sudo apt install seahorse
seahorse
```

In Passwords and Keys, open Passwords → Login. Then change the password of the Login keyring and set a blank password by leaving the new password fields empty.

## 6. Library and network shares

Use **Library** for local and imported music.

You can:

- play local tracks from the music folder
- switch between track, folder, and album views
- open album pages with cover art and album-level play/add-to-queue actions
- view cached album facts and artist information when metadata is available
- browse similar-artist discovery suggestions
- search by title, artist, album, genre, or year
- shuffle or loop the current queue
- select multiple tracks
- save selected tracks as a playlist
- upload audio files or album ZIPs
- import from a media URL when supported by the installed tools
- download or delete selected tracks

Typical supported formats include MP3, FLAC, WAV, OGG/Opus/WebM, M4A, M3U/M3U8 playlists, and ZIP album imports. Exact support depends on the host tools installed by the installer.

FXRoute treats local tags and cover files as authoritative. It can add cached MusicBrainz IDs, Cover Art Archive fallback covers, album facts, Wikipedia/Wikidata artist summaries, and ListenBrainz suggestions. It caches unchanged tracks by relative path, modification time, and size, which keeps rescans fast.

The **Library** selector in **Technical settings** switches between the local music folder and network libraries. FXRoute discovers accessible disk shares on configured or nearby SMB hosts. Select a discovered share to use it as the active library.

To add a share that was not discovered, choose **Add network share manually…** and enter one share URL, for example:

```text
smb://server/share
```

FXRoute checks guest access when discovering shares, mounts the selected share when needed, and then scans it like the local library. The host needs `smbclient`, CIFS/GVFS support, and permission to mount the share. The installer provides the required packages and the CIFS mount helper on supported distributions. A share must expose one disk share; administrative and printer shares are ignored.

## 7. EasyEffects installation

The installer prefers Flatpak EasyEffects when it installs EasyEffects. Recent Flatpak versions expose the control socket FXRoute uses for fast preset switching and recovery.

If EasyEffects already exists on the system, FXRoute can use that installation. Native/package-manager versions use the CLI fallback when no control socket is available. Preset switching can still work, but socket-based control and recovery may be limited.

For a manual Flatpak installation:

```bash
flatpak install --user flathub com.github.wwmm.easyeffects
```

## 8. DSP and EasyEffects

Use **DSP** to shape and correct the sound.

Main tools:

- **A/B compare** — switch between two presets while listening.
- **Combine** — build a new preset from up to three existing presets.
- **Import filter** — import stereo or separate left/right filters.
- **Create PEQ preset** — sketch up to 12 temporary filters, stage them for
  Left, Right, or both channels, and create a left/right parametric EQ preset.
- **Custom House Curve** — edit up to 8 frequency/gain points on the graph or
  numerically, then create a reusable target curve.
- **Output extras** — configure the global helpers shared by all presets.

Typical DSP files:

- EasyEffects preset JSON
- convolver `.irs` files
- WAV impulse responses
- REW text filters for left/right PEQ-style correction

Use A/B compare while music is playing. Switching between presets is usually more useful than comparing their numbers.

### Global helpers

Global helpers affect the active DSP setup:

- **Protection Limiter** protects the final output from peaks.
- **Headroom** adds a controlled safety margin before the output stage.
- **Auto Gain** adjusts programme level toward the selected loudness target.
- **Loudness** applies a calibrated contour that follows the playback level. **Strength** controls its intensity; when Auto Gain is active, the contour also accounts for the selected Auto Gain target.
- **Bass Enhancer** adds adjustable low-frequency enhancement.
- **Tone Effect** provides broad tonal shaping and contains the Loudness control.

Each helper can be enabled or adjusted from **Output extras**. Auto Gain and
Loudness can be used independently or together, while the Protection Limiter
remains the final stage.

## 9. Measurement assistant

Open **Measure** from the DSP page.

Use the measurement assistant to tune the room and speakers:

- choose left, right, or stereo measurement
- run a same-position L/R Repeat when you want a more reliable stereo pair
- select a host microphone
- optionally load a microphone calibration file
- run a sweep
- view the frequency response from 20 Hz to 20 kHz
- switch between frequency response and impulse-response preview when preview data is available
- switch graph smoothing: raw, 1/6 octave, 1/3 octave, or 1 octave
- save useful runs
- inspect a measurement curve and create a PEQ correction from it
- sketch up to 12 temporary PEQ filters and transfer them to a new preset
- create a custom House Curve with up to 8 editable frequency/gain points
- transfer visible L/R measurements into the Convolver assistant
- turn the result into a PEQ or FIR/Convolver preset
- export imported or created calibration and House Curve files

Inspect the room and speaker response, compare channels, identify correction needs, and turn visible measurements into PEQ or Convolver filters. Review the result before applying it. Measurement conditions and correction choices affect the result.

### PEQ and Custom House Curve editing

The Measurement graph provides two temporary editing assistants:

- **PEQ** shows slots **F1–F12**. Add or select a filter, edit its frequency,
  gain, type, and Q, and use **Take L**, **Take R**, or **Take Both** to stage
  the filters for a new PEQ preset. Empty slots are allowed; a thirteenth
  temporary filter is rejected.
- **Custom House Curve** shows slots **P1–P8**. Click an empty graph area to
  add a point, drag a point to adjust frequency and gain, or edit the selected
  point numerically. The graph uses logarithmic frequency spacing, matching
  the target-curve interpolation used by the Convolver assistant.

Choose **Create Custom House Curve…** from the target-curve selector, enter a
name, and press **Create Target Curve**. The resulting file is immediately
available as a target curve. Switching back to PEQ or Convolver keeps the
custom draft available for later editing.

In **Setup**, select an imported calibration or House Curve file and press
**Export** to download the managed file with its original content. Built-in
target curves are not exportable files.

### Advanced Measurement

**Advanced Measurement** is a guided multi-position workflow for speaker characterisation and system integration. It combines direct speaker measurements with measurements at the main listening position and two nearby listening positions.

The workflow guides you through:

- a direct measurement about 1 m from the left speaker
- a direct measurement about 1 m from the right speaker
- left and right measurements at the main listening position
- left and right measurements 20–30 cm to either side of the main position
- a final stereo system-integration check at the main position when subwoofer routing is active

Follow the on-screen instructions and move the microphone only when the current step asks for a new position. Keep the microphone at ear height for listening-position measurements. The workflow checks direct-response quality, spatial consistency, L/R timing, and the summed system response. It can reject a result when the microphone position, routing, or timing is inconsistent; repeat the affected step after correcting the setup.

Use the electrical reference input in **Setup** when available. It gives the timing analysis a line-level playback reference alongside the acoustic microphone signal. Acoustic-only measurements remain supported but provide less precise timing information.

### SPL Calibration

SPL Calibration plays calibrated **−23-LUFS pink noise** and calculates the
level adjustment for the current output profile. With a configured UMIK-1,
UMIK-2, or Dayton UMM-6, FXRoute can capture the microphone signal and
determine SPL automatically.
When automatic microphone measurement is unavailable, enter the reading from a
C-weighted, Slow SPL meter manually.

Only during SPL Calibration, Auto Gain and Loudness are temporarily neutral so
their previous target, Strength, and compensation do not alter the reference
noise. Their exact prior states are restored when calibration stops, is saved,
is cancelled, or fails. Normal sweeps and Auto Sub Optimize continue to
measure the active processing chain.

### Single Sweep and L/R Repeat

Use **Start Single Sweep** when you want one quick measurement of the selected speaker:

- **L** measures the left speaker.
- **R** measures the right speaker.
- **Stereo** measures both playback channels together for a broad overall check.

While a sweep is running, the status line shows the simple input-level indicator, for example `Peak -42 dBFS`, `Peak < -90 dBFS`, or `CLIP`.

Use **Start L/R Repeat** when you want a more dependable left/right measurement pair at one microphone position. Put the microphone in place, do not move it, then start the repeat. FXRoute measures left and right three times each, alternating L/R internally.

While L/R Repeat is running, the status keeps the current repeat step and total progress, and adds the same simple input-level indicator used by Single Sweep.

After the repeat finishes, FXRoute shows one combined result for review:

- `<name> · L`
- `<name> · R`

The intermediate repeat sweeps are processed internally and are not added to **Saved runs**. Review the combined result, edit the base name if needed, then press **Save current**. Both L and R summaries are saved together.

L/R Repeat is useful when:

- you are comparing speaker balance at the same listening position
- you want a cleaner input for PEQ or Convolver filter creation
- you care about L/R timing for aligned FIR modes
- a single sweep looks suspicious and you want repeat confirmation

Keep the microphone fixed during the whole repeat. Moving the microphone between the internal sweeps defeats the purpose of the mode.

#### Auto Sub Optimize

Auto Sub Optimize tests candidates around the selected crossover frequency and applies the verified delay, polarity, and subwoofer gain for the active mode:

- **2.1** — optimizes one mono subwoofer. One shared alignment is evaluated against both main channels.
- **2.2 Mono** — optimizes two mono subwoofers as one dual-sub system. A matrix scan evaluates the Sub 1/Sub 2 alignment combinations against both main channels.
- **2.2 Stereo** — optimizes the left sub/main branch and right sub/main branch separately.

The scan is centered around the alignment values currently configured for the active mode. If you already know or suspect useful starting delays — for example from a subwoofer manual that lists internal DSP latency — enter them first. Auto Sub Optimize then scans around those starting values instead of assuming 0 ms.

The optimizer does not measure the subwoofer's internal latency directly. It optimizes sub/main integration at the microphone position, including the subwoofer, crossover, room, and listening position.

Where the active mode uses a fine scan, FXRoute checks additional candidates around the best coarse delay region. In 2.2 Mono, the matrix scan evaluates the combined dual-sub result. The selected values apply to the measured crossover, room, and microphone position; they are not universal latency figures.

In **2.1** and **2.2 Mono**, candidates are evaluated against both left and right main channels so a weak result on one side affects the combined choice. In **2.2 Stereo**, the left and right sub/main branches are evaluated and optimized separately. The active polarity is protected unless another measured setting is clearly better. AutoGain then makes measured gain steps of up to ±6 dB against the selected target curve, verifies them with fresh sweeps, and restores gain changes that do not improve the result. Before and during those gain sweeps, FXRoute checks all four final Stage outputs and stops an unsafe candidate before it can exceed −1 dBFS. PEQ, target curves, and room-correction filters are not changed.

**Recommended order with EQ or Convolver:**

1. Set the crossover, sub levels, polarity, and initial alignment values roughly as desired for the active mode.
2. Run **Auto Sub Optimize**.
3. Run a normal measurement with the optimized alignment.
4. Create and enable the EQ or Convolver correction from that state.
5. Verify the result with a final normal measurement.

Repeat **Auto Sub Optimize** only if the active correction materially changes phase or delay around the crossover.

For best results:

- keep the microphone fixed during the scan
- avoid moving around the room during the measurements
- run the final verification measurement in the same output mode

### Frequency and IR graph views

The Measurement graph has two local views:

- **Freq** shows the normal frequency response from 20 Hz to 20 kHz. Smoothing, PEQ correction, and Convolver range editing are available in this view.
- **IR** shows a compact impulse-response preview from -2 ms to +30 ms for visible measurements that include preview data. The preview is normalized for inspection and is intended as a timing/reflection sanity check, not as a full impulse-response export.

New measurements include the compact IR preview when analysis can produce it. Older saved runs may not have preview data and will stay hidden in **IR** view.

### Timing and Electrical Reference

L/R Repeat compares repeated L/R timing relationships and rejects unstable pairs. For timing-sensitive work, an Electrical Reference input is recommended: record a line-level reference from the playback signal alongside the acoustic microphone signal. Acoustic-only timing remains available, but it is less precise and may reject more pairs.

### Convolver handoff

Measurements are independent from the Convolver settings. The Convolver assistant uses the visible saved measurement selection when saved runs are selected. If no saved run is selected, it can use the current measurement.

**Take L / Take R / Take Both** — one visible Left measurement enables **Take L**, one visible Right measurement enables **Take R**, and one visible Left plus one visible Right enables **Take Both**. Saved L/R Repeat results can be used like any other saved left/right pair. Hide or deselect unrelated saved runs before taking measurements into the Convolver filter.

### Phase modes

- **Linear Phase** creates symmetric FIR correction.
- **Minimum Phase** is the default for normal room and speaker correction.
- **Minimum Phase aligned** is a stereo variant of **Minimum Phase**. It uses the measured L/R direct-arrival timing from separate saved left/right measurements and delays the earlier FIR channel for better time alignment.
- **Hybrid aligned** blends minimum-phase bass correction into zero-delay linear-style upper correction. In stereo mode it uses the same L/R direct-arrival timing safety gate as **Minimum Phase aligned**.

The aligned modes require single saved L/R measurements with valid direct-arrival timing data. Merged measurements are not supported for aligned timing correction.

FXRoute blocks aligned filter creation when the measured signed L/R timing offset exceeds the safety limit in either direction. The timing summary is shown as one arrival relation, for example `L arrives 5.27 ms later than R`.

## 10. Technical settings

Click the FXRoute logo to open **Technical settings**.

Useful settings:

- choose the audio output device
- choose Stereo, 2.1 Subwoofer, or 2.2 Subwoofer output mode
- follow the playback sample rate or use a fixed sample rate
- check the current source mode
- see Bluetooth input status when the host supports it
- download the local HTTPS certificate when the optional HTTPS proxy is enabled

Use this area to fix the output device, check the source mode or Bluetooth input, or download the local HTTPS certificate for a client device.

### Output modes

Select **Stereo**, **2.1 Subwoofer**, or **2.2 Subwoofer** under **Output Mode**. The available subwoofer controls and measurement workflows depend on the selected mode. Set the crossover, levels, polarity, and alignment in the DSP output controls before running **Auto Sub Optimize**.

### Fixed Sample Rate

**Sample Rate** is set to **Auto** by default. In Auto mode, FXRoute follows the effective playback rate of the current source and output path. Local files, radio streams, Spotify, and Bluetooth can therefore use different rates.

Select a supported rate instead of **Auto** to fix the PipeWire playback graph and hardware output to that rate. The current rate appears in the playback bar. FXRoute rejects a fixed rate that the selected output does not support. Changing the policy can restart the audio path, so stop playback first when possible and check the output after the change.

Use a fixed rate when the DAC, DSP chain, or external hardware needs one clock. Use Auto when sources with different native rates should play without forcing conversion to one rate.

### Maintenance updates

Open **Technical settings → Maintenance** to view the installed version, check
for updates, run an update, and inspect the update log. FXRoute blocks updates
when the installation contains uncommitted changes and reports when a
successful update requires a reload.

### Home Assistant / external automation

FXRoute exposes `GET /api/power/state` as a read-only hint for amplifier power
automation. `amp_should_be_on` is true while playback is active or the
Measurement Assistant is open. Home Assistant or another automation system can
use this value to control a smart plug; FXRoute does not control the plug
directly and does not require MQTT.

Minimal Home Assistant example:

```yaml
rest:
  - resource: "http://fxroute.local:8000/api/power/state"  # Adapt host/port if needed.
    scan_interval: 5
    binary_sensor:
      - name: "FXRoute Amp Should Be On"
        value_template: "{{ value_json.amp_should_be_on }}"
```

## 11. Local HTTPS certificate

When the optional local HTTPS proxy is enabled, FXRoute creates a local certificate authority for the audio PC.

Install the downloaded certificate only on devices you trust on your own LAN. Import it into the operating system or browser trust store as a trusted certificate authority. If the FXRoute Caddy certificate authority is regenerated, client devices may need the new certificate again.

## 12. Good first checks

If playback fails:

1. Try **Radio** first. It is the simplest playback source.
2. Check the bottom playback bar: does it show a track?
3. Open **Technical settings** and confirm the output device.
4. Check that EasyEffects is running if DSP presets are missing.
5. Restart FXRoute if the browser says it is disconnected.

Useful host commands:

```bash
systemctl --user status fxroute
systemctl --user restart fxroute
journalctl --user -u fxroute -f
```

If EasyEffects is the suspected problem, also check:

```bash
flatpak list --app | grep easyeffects
pgrep -af easyeffects
```

## 13. What FXRoute expects

FXRoute is designed for:

- a Linux desktop-session audio machine
- PipeWire
- EasyEffects in the same user session
- local network browser control
- a DAC, amp, active speakers, headphones, or similar listening setup

FXRoute depends on the audio desktop session and does not target fully headless operation.
