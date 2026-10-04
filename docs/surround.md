# Basic Surround path

Surround is an output mode in the existing Output System. The mode is remembered for each logical input, including each external stereo pair. Stereo and Stereo + Sub keep their existing routing and DSP state.

## Routing and processing

- Program PCM uses the fixed position bus `FL, FR, FC, LFE, SL, SR, BL, BR`. Absent positions are zero, not duplicated or guessed.
- Front L/R reuse the existing Main L/R roles and banks. Center is mono; Side L/R and Rear L/R are independent stereo banks. Side and Rear are routed as complete L/R pairs.
- A program position without its own speaker folds down instead of going silent: Center to Front L/R at -3 dB each, Side to Rear (or Rear to Side) at unity, and to Front L/R at -3 dB when neither surround pair is routed. Sweeps excite only each speaker's own position, never its fold-down sends.
- The independent Crossover switch splits Front L/R through the existing speaker ways. Center, Side, Rear and Sub routing remain available.
- Surround permits Sub 1 and optional Sub 2, both fed by the same mono bass bus. Sub L/Sub R are not valid Surround roles.
- Full-range speaker bass is low-passed into the common sub bus. LFE is a separate program input: it bypasses that bass-management low-pass, then shares the sub bank, physical gain/delay/polarity and guard mute. With no sub configured, LFE feeds Front L/R at 0.5 per side, through any configured front speaker crossover.
- Bass management uses the sub tile's link scheme per speaker group (Front L/R, Center, Side L/R, Rear L/R). Linked, every group shares one crossover and the speaker high-pass switch. Unlinked, each configured group has its own crossover type, slope, frequency and high-pass switch; each program position's bass is low-passed at the crossover of the group that plays it, so Sub 1 and Sub 2 still receive one identical mono bass bus.
- Surround offers no Global bank: its whole-system chain stays Neutral, while the global helpers still apply. Area banks and All Banks remain selectable.
- Sub 1/Sub 2 use the existing mono/dual-mono adjustment and optimization paths. No parallel Surround optimizer or DSP engine is created.

## Digital ingress

The selected capture pair feeds `fxroute_surround_input` before stereo ingress, gain, remix or SRC. Its realtime callback only exchanges bounded PCM/carrier buffers; a separate worker recognizes IEC-61937, validates/deframes AC-3 and DTS Core, decodes with libavcodec and projects actual decoder positions onto the shared bus.

Stereo PCM and AC-3 stereo remain stereo. AC-3 or DTS identity alone never implies 5.1. Null/pause, unsupported carriers and decoder errors must not pass as PCM. A mute latch ends with digital silence, a valid burst, or 12288 frames without any IEC preamble or confirmed raw-DTS sync; raw DTS needs its sync to recur at a constant frame spacing, since single sync patterns occur in ordinary PCM. DTS-HD Master Audio is not a supported full-resolution S/PDIF transport; the TV may deliver DTS Core or PCM instead.

The initial carrier adapter supports the measured 48 kHz carrier. Selecting external Surround temporarily borrows that graph rate through the existing transition coordinator without overwriting the user's Auto/Fixed playback policy. Leaving it restores the playback policy. Hardware clock-source selection is device-specific and is not guessed or changed by FXRoute; configure the interface to follow the digital source when required. A lock indication alone does not prove bit-exact reception.

`GET /api/audio/surround` exposes helper liveness, decoder format/layout, errors and carrier ownership. A separate transport can feed the same position-tagged PCM bus without changing output banks or routing.

## Building and verification

Build `native_surround/build.sh` with a C compiler and pkg-config packages `libpipewire-0.3`, `libavcodec`, `libavutil`, `libswresample`. On openSUSE with FFmpeg 8 these headers are supplied by `pipewire-devel`, `ffmpeg-8-libavcodec-devel`, `ffmpeg-8-libavutil-devel`, `ffmpeg-8-libswresample-devel`.

Focused checks:

```sh
python3 scripts/test_surround.py
python3 scripts/test_surround_source.py
python3 scripts/test_surround_native.py
python3 scripts/test_surround_ingress.py
node scripts/test_surround_ui.js
```

The native checks require the corresponding development libraries. Live acceptance also needs clock-stability, actual speaker wiring/identification, physical LFE calibration, and extended format-switching tests. MPEG IEC-61937 has not been verified; the measured MPEG TV source was stereo PCM.
