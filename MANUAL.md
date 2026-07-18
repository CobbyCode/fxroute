# FXRoute Manual

FXRoute turns a small Linux audio PC into a browser-controlled music and DSP box.

Use it from a phone, tablet, or laptop on the local network to control playback, switch EasyEffects presets, compare DSP profiles, import filters, run practical room measurements, and tune the result without touching the desktop.

## 1. What FXRoute is for

FXRoute is useful when one local machine should act as a practical hi-fi control hub:

- play internet radio, Spotify, and local music
- browse local albums with artwork, artist context, and discovery hints
- control volume, queue, play/pause, and track position from the browser
- route audio through EasyEffects for live DSP
- switch room-correction, PEQ, convolver, and tone presets
- compare DSP presets quickly with A/B switching
- measure the room/speaker response and use it as a tuning guide
- expose the setup safely on the local network

FXRoute is designed for a Linux desktop-session audio box, not for a fully headless server.

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

A normal listening session looks like this:

1. Start music from **Radio**, **Spotify**, or **Library**.
2. Use the bottom playback bar for play/pause, volume, seek, and queue control.
3. Open **DSP** to choose or compare the sound profile.
4. If you want to tune the room, open **Measure** from the DSP page.
5. Save useful measurements, transfer correction ideas into a new PEQ preset, or use the visible measurements to create a Convolver preset.

EasyEffects does the live audio processing. FXRoute makes it easier to control, organize, compare, and edit presets.

## 4. Radio

Use **Radio** for simple internet radio playback.

You can:

- play built-in stations
- add custom stream URLs
- add or edit station artwork
- delete stations you no longer use

Radio is the quickest way to check that playback, output selection, and DSP routing are working.

## 5. Spotify

Use **Spotify** to control the Spotify desktop app running in the same Linux user session as FXRoute.

You can:

- play/pause
- previous/next track
- seek within a track
- control Spotify volume
- toggle shuffle and loop
- see cover art and track metadata

FXRoute refreshes Spotify metadata from local desktop events and lightweight polling, so automatic next-track changes should update title, artist, cover, duration, and position without needing a manual browser action.

FXRoute does not replace Spotify Connect. It controls the local Spotify client through the desktop session, so Spotify must be installed and reachable on the audio PC.

The regular Spotify desktop client also supports Spotify Lossless for eligible Premium accounts. Enable **Lossless** in a current Spotify desktop client (version 1.2.67 or newer) to stream available music at up to 24-bit/44.1 kHz FLAC while FXRoute continues to provide remote playback control. FXRoute controls the client; it does not provide the Spotify stream itself.

On fresh installs, Spotify autostart is enabled by default when a local Spotify desktop client is available. Installer reruns preserve an existing `.env`, so an already configured system keeps its current setting.
Spotify may trigger a Linux keyring unlock prompt after login.
On XFCE, the graphical keyring tool may need to be installed first:

```bash
sudo apt install seahorse
seahorse

In Passwords and Keys / Passwörter und Schlüssel, open Passwords / Passwörter → Login. Then change the password of the Login keyring and set a blank password by leaving the new password fields empty.

## 6. Library

Use **Library** for local files and imported music.

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

FXRoute keeps local tags and local cover files first. It can enrich albums with cached MusicBrainz IDs, Cover Art Archive fallback covers, compact album facts, Wikipedia/Wikidata artist summaries, and ListenBrainz discovery suggestions. Unchanged tracks are cached by relative path, modification time, and size so rescans stay fast.

A NAS can be used as the library by mounting its SMB/Samba share locally, then setting `MUSIC_ROOT` in `.env` to that mount path, for example `/mnt/music`.

## 7. DSP and EasyEffects

Use **DSP** for sound shaping and correction.

Main tools:

- **A/B compare** — switch between two presets while listening.
- **Combine** — build a new preset from up to three existing presets.
- **Import filter** — import stereo or separate left/right filters.
- **Create PEQ preset** — build left/right parametric EQ bands.
- **Output extras** — add global helpers like protection limiter, headroom, autogain, bass enhancement, or tone effect.

Typical DSP files:

- EasyEffects preset JSON
- convolver `.irs` files
- WAV impulse responses
- REW text filters for left/right PEQ-style correction

Tip: use A/B compare while real music is playing. It is usually easier to judge a preset by switching quickly than by staring at numbers.

### EasyEffects installation mode

The installer prefers Flatpak EasyEffects when it installs EasyEffects itself. Flatpak is usually the best-supported path because recent EasyEffects versions expose the control socket FXRoute can use for faster preset switching.

If EasyEffects is already installed by the user, FXRoute may use that existing installation instead. Native/package-manager EasyEffects can still work through the CLI fallback, but older native versions may not expose the EasyEffects control socket. In that case preset switching can still work, but socket-based control and recovery may be less capable than with the Flatpak version.

For the most reproducible setup, use the Flatpak package:

```bash
flatpak install --user flathub com.github.wwmm.easyeffects
```

### Maintenance updates

Open **Technical settings → Maintenance** to see the installed version, check for and run an update, and view the update log. For safety, FXRoute blocks an update when the local installation contains uncommitted changes. After a successful update, the page reports completion and whether a reload is needed.

### Home Assistant / external automation

FXRoute exposes `GET /api/power/state` as a read-only hint for amplifier power automation. `amp_should_be_on` is true when playback is active or when the Measurement Assistant is open. External automation systems can use that value to control an amplifier smart plug or power socket. No MQTT broker is required, and FXRoute does not control the plug directly.

Minimal Home Assistant example:

```yaml
rest:
  - resource: "http://fxroute.local:8000/api/power/state"  # Adapt host/port if needed.
    scan_interval: 5
    binary_sensor:
      - name: "FXRoute Amp Should Be On"
        value_template: "{{ value_json.amp_should_be_on }}"
    sensor:
      - name: "FXRoute Amp Reason"
        value_template: "{{ value_json.reason }}"

automation:
  - alias: "FXRoute amp on"
    trigger:
      - platform: state
        entity_id: binary_sensor.fxroute_amp_should_be_on
        to: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.verstaerker_steckdose  # Adapt to your smart plug entity.

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
          entity_id: switch.verstaerker_steckdose  # Adapt to your smart plug entity.
```

## 8. Measurement assistant

Open **Measure** from the DSP page.

The measurement assistant is meant for practical room-tuning work:

- choose left, right, or stereo measurement
- run a same-position L/R Repeat when you want a more reliable stereo pair
- select a host microphone
- optionally load a microphone calibration file
- run a sweep
- view the frequency response from 20 Hz to 20 kHz
- switch between frequency response and impulse-response preview when preview data is available
- switch graph smoothing: raw, 1/6 octave, 1/3 octave, or 1 octave
- save useful runs
- inspect a measurement curve and create a PEQ correction draft from it
- transfer visible L/R measurements into the Convolver assistant
- turn the result into a PEQ or FIR/Convolver preset

Use it as a practical measurement and correction workspace: inspect room and speaker response, compare channels, identify correction needs, and turn visible measurements directly into PEQ or Convolver drafts. Review the result before applying it; measurement conditions and correction choices still matter.

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
- you want a cleaner input for PEQ or convolver drafting
- you care about L/R timing for aligned FIR modes
- a single sweep looks suspicious and you want repeat confirmation

Keep the microphone fixed during the whole repeat. Moving the microphone between the internal sweeps defeats the purpose of the mode.

#### Auto Sub Optimize

Auto Sub Optimize measures candidates around the selected crossover frequency and applies the best verified delay, polarity, and subwoofer gain configuration for the active mode:

- **2.1** — optimizes one mono subwoofer. One shared alignment is evaluated against both main channels.
- **2.2 Mono** — optimizes two mono subwoofers as one dual-sub system. A matrix scan evaluates the Sub 1/Sub 2 alignment combinations against both main channels.
- **2.2 Stereo** — optimizes the left sub/main branch and right sub/main branch separately.

The scan is centered around the alignment values currently configured for the active mode. If you already know or suspect useful starting delays — for example from a subwoofer manual that lists internal DSP latency — enter them first. Auto Sub Optimize then scans around those starting values instead of assuming 0 ms.

The optimizer does not directly measure the subwoofer’s internal latency. It optimizes the practical sub/main integration at the microphone position, including the subwoofer, crossover, room, and listening position.

Where the active mode uses a fine scan, FXRoute checks additional candidates around the best coarse delay region. In 2.2 Mono, the matrix scan evaluates the combined dual-sub result. Treat the selected values as a practical optimum for the measured crossover, room, and microphone position rather than as universally exact latency figures.

In **2.1** and **2.2 Mono**, candidates are evaluated against both left and right main channels so a weak result on one side affects the combined choice. In **2.2 Stereo**, the left and right sub/main branches are evaluated and optimized separately. The active polarity is protected unless another measured setting is clearly better. AutoGain then makes bounded gain steps against the selected target curve, verifies them with fresh sweeps, and restores gain changes that do not improve the result. PEQ, target curves, and room-correction filters are not changed.

**Recommended order with EQ or Convolver:**

1. Set the crossover, sub levels, polarity, and initial alignment values roughly as desired for the active mode.
2. Create and enable the EQ or Convolver preset you intend to use.
3. Run **Auto Sub Optimize** once with that preset active.
4. Check the result with a normal measurement in the same output mode.

There is no routine need to run Auto Sub Optimize both before and after creating the correction preset. Run it again only after a relevant change to the crossover, routing, polarity, or active correction setup—not as part of an adjustment loop.

For best results:

- keep the microphone fixed during the scan
- avoid moving around the room during the measurements
- set crossover, sub level, polarity, and an initial alignment roughly before starting
- check the result with a normal measurement in the same output mode afterwards

### Frequency and IR graph views

The Measurement graph has two local views:

- **Freq** shows the normal frequency response from 20 Hz to 20 kHz. Smoothing, PEQ drafting, and Convolver range editing are available in this view.
- **IR** shows a compact impulse-response preview from -2 ms to +30 ms for visible measurements that include preview data. The preview is normalized for inspection and is intended as a timing/reflection sanity check, not as a full impulse-response export.

New measurements include the compact IR preview when analysis can produce it. Older saved runs may not have preview data and will stay hidden in **IR** view.

### Timing and Electrical Reference

L/R Repeat compares repeated L/R timing relationships and rejects unstable pairs. For timing-sensitive work, an Electrical Reference input is recommended: record a line-level reference from the playback signal alongside the acoustic microphone signal. Acoustic-only timing remains available, but it is less precise and may reject more pairs.

### Convolver handoff

Measurements are independent from the Convolver settings. The Convolver assistant uses the visible saved measurement selection when saved runs are selected. If no saved run is selected, it can use the current measurement.

**Take L / Take R / Take Both** — one visible Left measurement enables **Take L**, one visible Right measurement enables **Take R**, and one visible Left plus one visible Right enables **Take Both**. Saved L/R Repeat results can be used like any other saved left/right pair. Hide or deselect unrelated saved runs before taking measurements into the Convolver draft.

### Phase modes

- **Linear Phase** creates symmetric FIR correction.
- **Minimum Phase** is the practical default for normal room and speaker correction.
- **Minimum Phase aligned** is a stereo variant of **Minimum Phase**. It uses the measured L/R direct-arrival timing from separate saved left/right measurements and delays the earlier FIR channel for better time alignment.
- **Hybrid aligned** blends minimum-phase bass correction into zero-delay linear-style upper correction. In stereo mode it uses the same L/R direct-arrival timing safety gate as **Minimum Phase aligned**.

The aligned modes require single saved L/R measurements with valid direct-arrival timing data. Merged measurements are not supported for aligned timing correction.

FXRoute blocks aligned filter creation when the measured signed L/R timing offset exceeds the safety limit in either direction. The timing summary is shown as one arrival relation, for example `L arrives 5.27 ms later than R`.

## 9. Technical settings

Click the FXRoute logo to open **Technical settings**.

Useful settings:

- choose the audio output device
- check the current source mode
- see Bluetooth input status when the host supports it
- download the local HTTPS certificate when the optional HTTPS proxy is enabled

Use this area when audio comes from the wrong output, the source mode looks wrong, Bluetooth input needs checking, or a client device needs the local HTTPS certificate.

## 10. Local HTTPS certificate

When the optional local HTTPS proxy is enabled, FXRoute creates a local certificate authority for the audio PC.

Install the downloaded certificate only on devices you trust on your own LAN. Import it into the operating system or browser trust store as a trusted certificate authority. If the FXRoute Caddy certificate authority is regenerated, client devices may need the new certificate again.

## 11. Good first checks

If something does not play:

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

## 12. What FXRoute expects

FXRoute is designed for:

- a Linux desktop-session audio machine
- PipeWire
- EasyEffects in the same user session
- local network browser control
- a DAC, amp, active speakers, headphones, or similar listening setup

The audio desktop session is part of the design. Fully headless operation is not the primary target.
