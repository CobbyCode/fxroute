# Basic Surround path

Surround is an output mode in the existing Output System. The mode is remembered for each logical input, including each external stereo pair. Stereo and Stereo + Sub keep their routing and DSP state, but now share automatic input recognition with Surround.

## Routing and processing

- Program PCM uses the fixed position bus `FL, FR, FC, LFE, SL, SR, BL, BR`. Absent positions are zero, not duplicated or guessed.
- Front L/R reuse the existing Main L/R roles and banks. Center is mono; Side L/R and Rear L/R are independent stereo banks. Side and Rear are routed as complete L/R pairs.
- A program position without its own speaker folds down instead of going silent: Center to Front L/R at -3 dB each, Side to Rear (or Rear to Side) at unity, and to Front L/R at -3 dB when neither surround pair is routed. Sweeps excite only each speaker's own position, never its fold-down sends.
- The independent Crossover switch splits Front L/R through the existing speaker ways. Center, Side, Rear and Sub routing remain available.
- Surround permits Sub 1 and optional Sub 2, both fed by the same mono bass bus. Sub L/Sub R are not valid Surround roles.
- A Surround Global measurement is scoped to one speaker group, chosen only in the Measure sweep menu, and covers that group's speakers plus every sub. The other groups are muted at the outputs; groups other than Front also silence the front pair's sink feed for the take, because its bass would otherwise arrive through the shared bass bus. All Banks is measurable as the whole system as listened (it owns no bank, so its result is never importable). Advanced follows the selected speaker area, including a single Center + Sub model for Center, and is hidden for All Banks and Sub.
- Full-range speaker bass is low-passed into the common sub bus. LFE is a separate program input: it bypasses that bass-management low-pass, then shares the sub bank, physical gain/delay/polarity and guard mute. With no sub configured, LFE feeds Front L/R at 0.5 per side, through any configured front speaker crossover.
- Bass management uses the sub tile's link scheme per speaker group (Front L/R, Center, Side L/R, Rear L/R). Linked, every group shares one crossover and the speaker high-pass switch. Unlinked, each configured group has its own crossover type, slope, frequency and high-pass switch; each program position's bass is low-passed at the crossover of the group that plays it, so Sub 1 and Sub 2 still receive one identical mono bass bus.
- Global exists in Surround as one whole-mode preset whose PEQ and convolver plugins carry the speaker group they correct (Front L/R, Center, Side L/R, Rear L/R). Global A/B switches the entire preset across every routed group; there is no A/B Speaker selection. The preset is a filter set: all groups' filters run at once, and a filter created for one group replaces only that group's filters of the listened preset in a new preset. Each A/B slot lists its filters per group. A tagged plugin runs on its group's effect pairs ahead of the output matrix and the bass-management split, so it covers the speaker and its share of the mono bass bus; LFE is no group. A group without a routed speaker folds into another one and its plugin stays out of the chain; a delay band is refused because it would shift every group. Area banks, All Banks, and the global helpers are unchanged.
- Level Matching stores one mode-wide calibration family over logical speakers; crossover ways stay together. Individual speaker banks keep staged matching against the Front reference, while Global/All Banks/Sub can measure all speaker groups in one run. Subs remain owned by Auto Sub Optimize. Polarity Check diagnoses wiring only in individual speaker banks, never Global/All Banks/Sub.
- Sub 1/Sub 2 use the existing mono/dual-mono adjustment and optimization paths. No parallel Surround optimizer or DSP engine is created.

## Digital ingress

The selected capture pair feeds `fxroute_surround_input` before stereo ingress, gain, remix or SRC in all three output modes. Detection uses samples, not a sound-card name or a manual digital/analog flag. Its realtime callback only exchanges bounded PCM/carrier buffers; a separate worker recognizes IEC-61937, validates/deframes AC-3 and DTS Core, decodes with libavcodec and projects actual decoder positions onto the shared bus. Split stereo pairs can feed the two carrier ports from different capture nodes. Explicit custom PCM mappings retain their direct routing.

Surround links the eight discrete output positions. Stereo and Stereo + Sub instead link `output_stereo_FL/FR` to the two DSP inputs: Front at unity, Center and the corresponding Side/Rear at −3 dB, LFE at half gain per side, all scaled by one normalization derived from the actual decoded layout. This retains every channel and the stereo image without changing the discrete Surround bus. PCM and encoded stereo keep unity gain. The downmix precedes the existing stereo global effects and bass management, so Stereo + Sub distributes its bass and LFE through the configured crossover rather than creating a second sub path.

Stereo PCM and AC-3 stereo remain stereo. AC-3 or DTS identity alone never implies 5.1. Null/pause, unsupported carriers and decoder errors must not pass as PCM. A mute latch ends with 8192 exact-zero frames, a valid burst, or more than 6656 frames without an IEC preamble. This exceeds the maximum E-AC-3 period of 6144 frames. Confirmed raw DTS uses a separate 8704-frame timeout covering accepted sync intervals up to 8192 frames; it still requires three syncs at a constant spacing, and a lone sync pattern remains PCM. An IEC preamble clears raw-DTS history and returns to the shorter IEC fallback. DTS-HD Master Audio is not a supported full-resolution S/PDIF transport; the TV may deliver DTS Core or PCM instead.

The carrier adapter supports a 48 kHz graph. Selecting a normal external pair in any output mode temporarily borrows that graph rate through the existing transition coordinator without overwriting the user's Auto/Fixed playback policy. Leaving it restores the playback policy. Where the capture card exposes a clock selector, FXRoute follows a locked digital source and returns to Internal on release; this generic clock handling is separate from sample-driven format detection. Custom PCM mappings outside Surround only borrow the carrier when their card needs it. A lock indication alone does not prove bit-exact reception.

Both discrete and stereo helper outputs have the same 2818-frame live offset (58.71 ms), including PCM. AV Sync credits the actual routed helper regardless of codec or output mode and reduces the common AV delay accordingly. If the target is below the remaining input/DSP latency, no negative delay can restore it; the UI reports the achievable minimum. The 2050-frame recognition lookahead is included in the live offset, not added again. The remaining 768 frames are worker margin; the helper forces 768-frame callbacks while active (restored on stop); larger quanta and non-48-kHz clocks fail closed.

A newly recognised IEC preamble suppresses the whole still-pending prefix from the earliest unemitted frame. Up to 42.71 ms of genuine PCM immediately before a transition is lost by design; earlier PCM stays unity and valid decoded audio after the preamble is unchanged. The initial prefix remains unclassified until the first recognition window completes: a preamble during that window suppresses a possible partial startup payload, including a very short initial PCM prefix that cannot yet be distinguished from it. Pure PCM retains unity from frame zero. Codec/layout changes preserve all decoded frames. Bitstream-to-non-silent-PCM fallback retains a necessary safe gap: 5122 frames after AC-3 (106.71 ms), 6146 after DTS-512 (128.04 ms). Safety muting publishes an atomic watermark covering already queued outputs not yet pulled, without cross-thread edits to queue slots. Already pulled frames cannot be retracted; raw DTS recognized only after widely spaced syncs can still have an audible unrecognized prefix. See `docs/SPDIF-LATENCY-ANALYSIS.md` for the measured reserve and limits.

`GET /api/audio/surround` exposes helper liveness, decoder format/layout, errors and carrier ownership, plus `lookahead_frames`, `live_latency_frames`, `worker_margin_frames`, `worker_quantum_frames`, `worker_delay_max_us`, `worker_reserve_min_us`, `input_queue_peak_frames`, `late_frames`, `underrun_frames`, `queued_muted_frames` and `transport_faults`. Counters and extrema cover the helper's lifetime. Reserve is measured against the quantum-rounded pull deadline, including scheduling and decode time, not merely inferred from queue capacity. A separate transport can feed the same position-tagged PCM bus without changing output banks or routing.

Once raw DTS is confirmed, its safety latch survives spacing and packing changes; plausible syncs maintain suppression while the new pattern requalifies. Candidate-history changes alone never reopen PCM. IEC recognition, safe PCM return or discontinuity reset clears that latch.

## Building and verification

Build `native_surround/build.sh` with a C compiler and pkg-config packages `libpipewire-0.3`, `libavcodec`, `libavutil`, `libswresample`. On openSUSE with FFmpeg 8 these headers are supplied by `pipewire-devel`, `ffmpeg-8-libavcodec-devel`, `ffmpeg-8-libavutil-devel`, `ffmpeg-8-libswresample-devel`.

Focused checks:

```sh
python3 scripts/test_surround.py
python3 scripts/test_surround_source.py
python3 scripts/test_digital_input_modes.py
python3 scripts/test_surround_native.py
python3 scripts/test_surround_ingress.py
python3 scripts/test_measurement_global_group.py
python3 scripts/test_measurement_all_banks.py
node scripts/test_surround_ui.js
node scripts/test_hybrid_scope_flow.js
node scripts/test_all_banks_import_block.js
```

The native checks require the corresponding development libraries. Live acceptance also needs clock-stability, actual speaker wiring/identification, physical LFE calibration, and extended format-switching tests. MPEG IEC-61937 has not been verified; the measured MPEG TV source was stereo PCM.

Measurement teardown (diagnosed and fixed): input syncs that arrive while a measurement session owns the audio graph are dropped on purpose (they muted the surround ingress and released the digital carrier), but the session release only restored rates, so the ingress stayed dropped until the next source change or service restart. The swallowed syncs now mark the routing stale and the digital guard loop re-asserts it within one guard interval (5 s) after the release. Covered by `python3 scripts/test_input_ingress_resync.py`.
