# FXRoute Manual

FXRoute turns a small Linux audio PC into a browser-controlled music and DSP box.

Use it from a phone, tablet, or laptop on the local network to control playback, switch EasyEffects stereo presets, compare DSP profiles, import filters, run practical room measurements, and tune the result without touching the desktop.

## 1. What FXRoute is for

FXRoute is useful when one local machine should act as a practical hi-fi control hub:

- play internet radio, Spotify, and local music
- control volume, queue, play/pause, and track position from the browser
- route audio through EasyEffects stereo DSP for live DSP
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

The top-left FXRoute logo opens **Technical settings**. Optional USB amplifier-controller support is documented in [`docs/HARDWARE_CONTROLLER.md`](docs/HARDWARE_CONTROLLER.md).

## 3. Basic listening workflow

A normal listening session looks like this:

1. Start music from **Radio**, **Spotify**, or **Library**.
2. Use the bottom playback bar for play/pause, volume, seek, and queue control.
3. Open **DSP** to choose or compare the sound profile.
4. If you want to tune the room, open **Measure** from the DSP page.
5. Save useful measurements, transfer rough PEQ ideas into a new preset, or create a convolver/FIR correction preset.

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

FXRoute does not replace Spotify Connect. It controls the local Spotify client through the desktop session, so Spotify must be installed and reachable on the audio PC.

## 6. Library

Use **Library** for local files and imported music.

You can:

- play local tracks from the music folder
- browse by folder, album, or use the full track list
- search by folder, title, artist, album, or album artist
- shuffle or loop the current queue
- select multiple tracks
- save selected tracks as a playlist
- upload audio files or album ZIPs
- import from a media URL when supported by the installed tools
- download or delete selected tracks
- discover similar artists for the current album via ListenBrainz

Typical supported formats include MP3, FLAC, WAV, OGG/Opus/WebM, M4A, M3U/M3U8 playlists, and ZIP album imports. Exact support depends on the host tools installed by the installer.

FXRoute keeps library and playlist lists text-only for speed. When a local track starts, the Now Playing cue can show available folder or embedded cover art. For files without metadata, names like `Artist - Title.ext` are used as a conservative fallback for display.

A NAS can be used as the library by mounting its SMB/Samba share locally, then setting `MUSIC_ROOT` in `.env` to that mount path, for example `/mnt/music`.

## 7. Optional amplifier controller

If an RP2040/ESP32-style USB CDC controller is connected, FXRoute can show and override amplifier input-selector state from **Technical settings → Amplifier Controller**.

The feature is optional. Without the MCU, FXRoute should behave normally and the card simply reports that no controller is detected.

The current protocol, API routes, config key, and later hardware-test checklist are documented in [`docs/HARDWARE_CONTROLLER.md`](docs/HARDWARE_CONTROLLER.md).

## 8. DSP and EasyEffects

Use **DSP** for sound shaping and correction.

Main tools:

- **A/B compare** — switch between two presets while listening.
- **Combine** — build a new preset from up to three existing presets.
- **Import filters** — import stereo filters, separate left/right filters, exported FXRoute filters, and compatible correction files.
- **Create PEQ preset** — build left/right parametric EQ bands.
- **Output extras** — add global helpers like protection limiter, headroom, autogain, bass enhancement, or tone effect.

Typical import formats:

- stereo `.irs` or `.wav` impulse responses
- separate left/right `.irs` or `.wav` impulse responses
- REW text filters for PEQ-style correction
- exported FXRoute filter ZIPs for reimport on the same or another FXRoute system
- compatible EasyEffects preset exports

Tip: use A/B compare while real music is playing. It is usually easier to judge a preset by switching quickly than by staring at numbers.

For level checks, FXRoute includes a 30-second 1 kHz stereo FLAC tone at `-12 dBFS` peak: `/static/audio/level-tone-1khz-minus12dbfs-48k.flac`. It is intentionally not a 0 dBFS full-scale tone.

When a convolver filter is exported from FXRoute, the download also includes a `.wav` copy of the impulse so it can be inspected in tools such as REW. Import the exported ZIP again through **DSP → Import filters** to restore the ready-to-use filter.

### EasyEffects installation mode

The installer prefers Flatpak EasyEffects when it installs EasyEffects itself. Flatpak is usually the best-supported path because recent EasyEffects versions expose the control socket FXRoute can use for faster preset switching.

If EasyEffects is already installed by the user, FXRoute may use that existing installation instead. Native/package-manager EasyEffects can still work through the CLI fallback, but older native versions may not expose the EasyEffects control socket. In that case preset switching can still work, but socket-based control and recovery may be less capable than with the Flatpak version.

For the most reproducible setup, use the Flatpak package:

```bash
flatpak install --user flathub com.github.wwmm.easyeffects
```

## 9. Measurement assistant

Open **Measure** from the DSP page.

The measurement assistant is meant for practical room-tuning work:

- choose left, right, or stereo measurement
- select a host microphone
- optionally load a microphone calibration file
- optionally load a REW-style house curve text file as a target curve
- run a sweep
- view the frequency response from 20 Hz to 20 kHz
- switch graph smoothing: raw, 1/6 octave, 1/3 octave, or 1 octave
- save useful runs
- use the PEQ assistant to sketch a few correction filters
- use the convolver assistant to create FIR correction presets from saved measurements
- transfer draft filters into **Create PEQ preset**

Think of it as a tuning assistant for broad room and speaker decisions: bass problems, channel differences, correction direction, and sanity checks.

House curve files use simple REW-compatible frequency/dB pairs, one point per line, with frequency first. Spaces, tabs, or commas are accepted. Frequencies must be strictly increasing, and at least two points are required.

## 10. Technical settings

Click the FXRoute logo to open **Technical settings**.

Useful settings:

- choose the audio output device
- check the current source mode
- see Bluetooth input status when the host supports it
- use normal PipeWire/Pulse inputs created by tools such as `shairport-sync` / AirPlay or Scream LAN audio
- download the local HTTPS certificate when the optional HTTPS proxy is enabled

FXRoute does not need special integration for every LAN-audio tool. If another service creates a normal system audio input, it should appear like any other input in Technical settings.

Use this area when audio comes from the wrong output, the source mode looks wrong, Bluetooth input needs checking, or a client device needs the local HTTPS certificate.

## 11. Local HTTPS certificate

When the optional local HTTPS proxy is enabled, FXRoute creates a local certificate authority for the audio PC.

Install the downloaded certificate only on devices you trust on your own LAN. Import it into the operating system or browser trust store as a trusted certificate authority. If the FXRoute Caddy certificate authority is regenerated, client devices may need the new certificate again.

## 12. Good first checks

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

## 12. Updating

To update FXRoute, re-run the installer on the host:

```bash
cd ~/fxroute
./install.sh
```

The installer overwrites application files and restarts the service.
Your configuration (`.env`) and data are preserved.

## 13. What FXRoute expects

FXRoute is designed for:

- a Linux desktop-session audio machine
- PipeWire
- EasyEffects in the same user session
- local network browser control
- a DAC, amp, active speakers, headphones, or similar listening setup

The audio desktop session is part of the design. Fully headless operation is not the primary target.
