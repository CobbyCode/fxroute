# Automatic PCM and digital ingress

Build with `sh native_surround/build.sh`. Dependencies are `pkg-config`, a C11
compiler, PipeWire development headers, and FFmpeg 5.1+ development headers for
`libavcodec`, `libavutil` and `libswresample`. The binary is
`native_surround/build/fxroute-surround-input`.

## Live interface

```sh
native_surround/build/fxroute-surround-input --status /path/to/status.json
```

The float DSP filter is named `fxroute_surround_input`. Inputs are `carrier_L`
and `carrier_R`. Outputs are `output_FL`, `output_FR`, `output_FC`, `output_LFE`,
`output_SL`, `output_SR`, `output_BL`, `output_BR`, in that order, followed by
`output_stereo_FL`, `output_stereo_FR`. Surround links the eight discrete ports
directly to its DSP inputs. Stereo and Stereo + Sub link the two stereo ports
to their DSP inputs, ahead of the existing stereo effects and bass management.
Never link raw carrier samples to a stereo sink while detection starts.

The caller owns the capture source, graph clock, and DSP links. The helper
requests 48 kHz but does not force a hardware clock. Any other graph rate
produces silence. Capture must reach the helper without SRC, mixing or gain.
Carrier words are recovered with `lroundf(sample * 32768)`.

Both 32768-frame SPSC queues are bounded. The realtime callback only moves
samples and atomic queue indexes. A separate thread recognizes bursts, decodes
with persistent per-codec contexts, converts sample format without SRC or
rematrixing on the discrete bus, derives the stereo fold-down, and writes status. Late output is discarded;
underruns are silence. Overruns, lost inputs and wrong clocks fail closed.

Recognition retains 2050 carrier frames before releasing PCM. Live output uses
a 2818-frame delay (63.98 ms at 48 kHz), including 768 frames of worker margin,
for PCM and encoded audio alike on all ten outputs. AV Sync credits this
existing input delay independently of the codec and output mode.
Uncompressed PCM occupies FL/FR and the unity stereo pair only. AC-3 and DTS Core IEC 61937 types 1 and
11/12/13 decode automatically, including same-codec layout changes. Null,
pause, unsupported and corrupt bursts are muted. Payloads of rejected bursts
are not scanned as nested IEC streams. Unknown/raw-DTS input stays muted until
a valid supported burst, 8192 consecutive exact-zero stereo frames, or more than 6656
frames without any IEC preamble (confirmed raw DTS: 8704 frames without sync). Every IEC stream
repeats its preamble at least every 6144 frames, so a running compressed stream
never becomes PCM, while a latch never outlives it (including after
discontinuities). Raw DTS, including 14-bit forms, is recognized for
suppression only, once its sync recurs three times at one constant frame
spacing; a lone sync pattern is ordinary PCM (TV PCM hits -225/+232 about once
a minute). It is not decoded.

An IEC transition clears all still-pending audio from the earliest
unemitted frame, including a possible unframed payload prefix after a switch
gap. Already emitted audio cannot be retracted; the retractable window is the
2050-frame lookahead (bounded by the 2818-frame live delay for other safety
mutes). Up to 42.71 ms of genuine PCM immediately before a transition is lost
by design; earlier PCM stays unity and valid decoded audio after the preamble
is unchanged. Broad safety muting also publishes an
atomic absolute-frame watermark to the output consumer. Queued audio below it
is silenced, but future valid decoded frames are not. Worker and RT never edit
each other's queue slots. The retractable window is bounded by the 2818-frame
live delay; already pulled raw-DTS prefixes cannot be undone. Unrecognizable
payload before a first sync later than the recognition window remains
indistinguishable from PCM. Confirmed raw DTS has an 8704-frame timeout, above
its longest accepted sync interval (8192). Each IEC preamble clears raw-DTS
history so IEC recovery keeps its shorter timeout.
The confirmed raw-DTS latch is separate from candidate repeat counts. Plausible
syncs preserve suppression during spacing/packing requalification; only IEC,
safe PCM return or a discontinuity reset clears it.

Non-silent PCM after AC-3 resumes at last preamble +6658 frames: 5122 frames
after the 1536-frame burst ends (106.71 ms). DTS-512 leaves 6146 frames (128.04 ms).
Silence reset is unchanged pending a real TV idle-carrier measurement.
Quanta above 768 frames fail closed because the recognition decision would
miss some same-callback pulls. The helper forces 768-frame callbacks while active (restored on stop).

Channel positions come from the decoded frame, not its channel count. For
example, `5.1(side)` occupies SL/SR while a true back layout occupies BL/BR.
The discrete bus retains the decoder's original PCM scale, including LFE.
The additional stereo pair mixes Front at unity, Center and each matching
Side/Rear at -3 dB, and LFE at 0.5 per side, using one shared normalization
`1 / max(1, sum(left weights), sum(right weights))` over the actual input layout.
PCM and encoded stereo stay unity. Stereo + Sub processes this full downmix
through its normal crossover, including the folded LFE. Decoder options are
retained across codec flushes. No input-type or device-name hint is used.

## Status and Python lifecycle

`--status PATH` atomically replaces a small JSON file, normally about every
100 ms. Fields are `codec`, `sample_rate`, `layout`, `positions` (decoder order),
`bus_positions` (fixed output order), `errors`, `decoded_bursts`,
`lookahead_frames`, `live_latency_frames`, `worker_margin_frames`, `state`, and `mute_reason`. `state` is `pcm`, `encoded` or
`muted`. A corrupt burst after valid decoding retains the last valid codec and
layout, with `state=muted` and its reason; this is not a successful current
decode. A discontinuity clears current layout metadata.

Live JSON additionally reports lifetime extrema/counters:

- `worker_quantum_frames`: most recent callback size.
- `worker_delay_max_us`: maximum input callback-to-worker completion time,
  including queue wait and decode/emission work.
- `worker_reserve_min_us`: minimum time remaining until the quantum-rounded
  output pull deadline; `null` before any output decision. This is a measured
  estimate assuming the current graph period, not an OS scheduling guarantee.
- `input_queue_peak_frames`: largest backlog observed by the worker.
- `late_frames`: output discarded after its pull deadline, never replayed.
- `underrun_frames`: missing output at a pull, excluding pre-input startup.
- `queued_muted_frames`: queued output suppressed by the safety watermark.
- `transport_faults`: queue, link and unsupported clock/quantum faults.

`audio.digital_input.DigitalInput` exposes:

- `await start(source_name, left, right, rate=48000, right_source=None)`
- `await stop()`
- `snapshot()` (bounded metadata plus running/PID/source/error/stderr readback)
- `await links_present()` and `await repair_links()`
- `output_ports`, `carrier_ports`, and `node_name`

Channel names such as `AUX2` resolve only within the explicitly selected node
to `capture_AUX2` or `output_AUX2`; explicit port names are also accepted.
`right_source` can select another capture node for the right channel, including
two distinct `MONO` ports. Repeated starts for the same selection verify/repair links and preserve the
PID and decoder state. A changed selection restarts the helper. The adapter
creates only capture-to-carrier links and checks all twelve helper ports. It
rejects duplicate nodes, missing binaries and missing selected ports, drains a
bounded 16 KiB stderr tail, periodically repairs capture links, and terminates
and reaps its owned process group even on cancellation. DSP links remain the
runtime's responsibility.

## Offline verification

```sh
native_surround/build/fxroute-surround-input --offline INPUT.s32 OUTPUT.f32 --status STATUS.json
python3 scripts/test_surround_ingress.py
python3 scripts/test_surround_live_queue.py
python3 native_surround/test_captures.py
python3 native_surround/test_live.py
```

Offline input is little-endian interleaved stereo S32: IEC carrier words occupy
the upper 16 bits; regular PCM retains its full sample scale. Output is
interleaved eight-channel float32 in bus order, with exactly the input frame
count. `--stereo` writes the two-channel fold-down instead of the discrete bus.
Recognition delay is removed for offline output, and the last incomplete
burst is muted without discarding prior valid decoded audio. `--chunk FRAMES`
varies input boundaries for regression tests.

The capture verifier uses `SURROUND_CAPTURE_ROOT` (default `/tmp/opencode`) and
skips captures that are absent. The live verifier starts and stops its own
private hardware-free PipeWire server; it never changes the normal server,
hardware, product service or graph rate.
It exercises PCM, AC-3 and DTS on both discrete and stereo taps. Raw input
and decoded output are recorded together and compared frame-for-frame with
the offline reference: this 256-frame-quantum graph observes 3074 helper frames
plus one 256-frame filter scheduling hop. That graph transport is separate
from the algorithmic AV Sync credit; the test is not a physical end-to-end
latency measurement.

`SURROUND_STRESS=1 python3 native_surround/test_live.py` also runs six CPU busy
workers with rapid PCM/AC-3/DTS/layout changes at quantum 1024. All ten outputs
are checked, including expected silence; late/underrun/fault/error counters must
not grow and worker reserve must stay positive. Whole-block observer-hop changes
are distinguished from helper underruns. `SURROUND_LIVE_SECONDS` extends the
switch run and `SURROUND_LIVE_REPORT` retains raw recordings and status samples.
Production injection is opt-in (`SURROUND_PRODUCTION=1`, run only the two
rapid-switch methods), requires a quiet existing carrier, and never restarts or
reconfigures the product helper. Only test-owned record/source/load processes
are terminated; their temporary links disappear with their nodes.

Limitations: only 48 kHz decoded audio is accepted, raw DTS is suppressed rather
than decoded, and arbitrary compressed data lacking any recognizable sync
cannot be distinguished reliably from PCM. After a detected unknown stream,
silence, a valid supported burst or the sync timeout leaves the mute latch.
