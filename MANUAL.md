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
5. Save useful measurements or transfer rough PEQ ideas into a new preset.

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

On fresh installs, Spotify autostart is enabled by default when a local Spotify desktop client is available. Installer reruns preserve an existing `.env`, so an already configured system keeps its current setting.

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

Open **Technical settings → Maintenance** to check the installed version against GitHub, run a safe update, and see the update log. Updates are handled by the backend through the installer-owned update script, not by frontend shell commands.

FXRoute blocks updates when the local git checkout has uncommitted changes. A successful update uses fast-forward-only git logic, refreshes dependencies only when needed, validates/builds the app, restarts the configured FXRoute user service, and then reports when reload/restart is complete.

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

The measurement assistant is a tuning aid for broad room and speaker decisions: bass problems, channel differences, correction direction, and sanity checks.

### 1. What Measure is for

- practical room and speaker measurement
- **Single Sweep**: quick L, R, or Stereo sweep
- **L/R Repeat**: same-position repeated sweeps for a cleaner stereo pair
- **PEQ draft**: sketch a few correction filters, transfer into **Create PEQ preset**
- **Convolver draft**: draft FIR filters from saved runs

### 2. Sweep modes

**Start Single Sweep** — one quick measurement of the selected speaker:

- **L** measures the left speaker.
- **R** measures the right speaker.
- **Stereo** measures both playback channels together for a broad overall check.

While running, the status line shows the input-level indicator, for example `Peak -42 dBFS`, `Peak < -90 dBFS`, or `CLIP`.

**Start L/R Repeat** — a more dependable left/right measurement pair at one microphone position. Put the microphone in place, do not move it, then start the repeat. FXRoute measures left and right three times each, alternating L/R internally.

While the repeat is running the status shows the current repeat step, total progress, and the same dBFS/CLIP indicator. After the repeat finishes, FXRoute presents a combined result for review (`<name> · L` and `<name> · R`). The intermediate sweeps are processed internally and are not added to **Saved runs**. Review the combined result, edit the base name if needed, then press **Save current** — both L and R summaries are saved together.

L/R Repeat is useful for:

- comparing speaker balance at the same listening position
- cleaner input for PEQ or convolver drafting
- L/R timing required for aligned FIR modes
- confirming a suspicious single sweep

Keep the microphone fixed during the whole repeat. Moving it between internal sweeps defeats the purpose.

### 3. Graph views and saved runs

The measurement graph has two local views:

- **Freq** shows the normal frequency response from 20 Hz to 20 kHz. Smoothing (raw, 1/6, 1/3, 1 octave), PEQ drafting, and Convolver range editing are available here.
- **IR** shows a compact impulse-response preview from -2 ms to +30 ms for measurements that include preview data. It is a timing/reflection sanity check, not a full impulse-response export.

New measurements include the IR preview when analysis can produce it. Older saved runs may not have preview data and will stay hidden in **IR** view.

### 4. Timing and Electrical Reference

L/R Repeat does not blindly average. It compares repeated L/R timing relationships, clusters the pair deltas, and accepts the best stable cluster. Inconsistent pairs are rejected; only accepted pairs are averaged.

Saved repeat summaries include timing metadata: repeat count, accepted/rejected run count, timing method, L/R delta centre and spread, whether Electrical Reference was used, and whether the result is stable.

**Electrical Reference** — for timing-critical measurements, a line-level reference from the playback signal is recorded alongside the acoustic microphone signal.

Example:

- **Input 1** = measurement microphone
- **Input 2** = line-level reference from the playback signal

Select microphone and reference channels from the same capture device in **Setup**. Keep the reference level below clipping.

With Electrical Reference, Repeat can align the reference captures before deconvolution and average captures in the time domain — usually the most stable timing result.

If Electrical Reference is not connected or is disabled, Repeat still works with acoustic-only timing, but acoustic timing is normally less precise and may reject more pairs.

> ⚠️ An unstable repeat summary should not be used for timing-sensitive L/R alignment. Rerun with the microphone fixed, check the selected speaker and input channels, and use Electrical Reference if available.

### 5. Convolver handoff

Measurements are independent from the Convolver settings. The Convolver settings (target curve, correction range, phase mode, sample rate, tap length) are only applied when a saved run is imported.

**Take L / Take R / Take Both** — the Convolver assistant uses the visible saved measurement selection:

- one visible Left measurement enables **Take L**
- one visible Right measurement enables **Take R**
- one visible Left plus one visible Right enables **Take Both**

To take L and R separately, keep only the left run visible before **Take L**, then only the right run visible before **Take R**. If exactly one L and one R are visible, use **Take Both**.

Ambiguous multi-selections disable the Take buttons (`Select one measurement or one L/R selection.`). Hide or deselect unrelated saved runs before taking measurements into the Convolver draft.

For a stereo correction preset:

- **Separate L/R**: measure L → save → select L → set Convolver settings → Take L → measure R → save → select R → Take R → Create Convolver Preset. Settings are carried forward; check before the second Take.
- **Same settings for both channels**: select L and R together → set Convolver settings → **Take Both** → Create Convolver Preset.
- **L/R Repeat result**: save the repeat summary and use it like separate saved L/R measurements.

### 6. Phase modes

- **Linear Phase** — symmetric FIR correction.
- **Minimum Phase** — practical default for normal room and speaker correction.
- **Minimum Phase aligned** — stereo Minimum Phase that uses measured L/R direct-arrival timing from separate saved left/right measurements and delays the earlier FIR channel for better time alignment.
- **Hybrid aligned** blends minimum-phase bass correction into zero-delay linear-style upper correction. In stereo it uses the same L/R direct-arrival timing safety gate as Minimum Phase aligned.

The aligned modes require single saved L/R measurements with valid direct-arrival timing data. Merged measurements are not supported.

FXRoute blocks aligned filter creation when the measured signed L/R timing offset exceeds the safety limit. The timing summary is shown as one arrival relation, for example `L arrives 5.27 ms later than R`.

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
