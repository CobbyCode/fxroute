# Digital Surround ingress

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
`output_SL`, `output_SR`, `output_BL`, `output_BR`, in that order. Link these
directly to the eight DSP inputs; do not route them through a stereo sink.

The caller owns the capture source, graph clock, and DSP links. The helper
requests 48 kHz but does not force a hardware clock. Any other graph rate
produces silence. Capture must reach the helper without SRC, mixing or gain.
Carrier words are recovered with `lroundf(sample * 32768)`.

Both 32768-frame SPSC queues are bounded. The realtime callback only moves
samples and atomic queue indexes. A separate thread recognizes bursts, decodes
with persistent per-codec contexts, converts sample format without SRC or
rematrixing, projects positions, and writes status. Late output is discarded;
underruns are silence. Overruns, lost inputs and wrong clocks fail closed.

Recognition retains 4096 carrier frames before releasing PCM. Live output uses
an 8192-frame delay (170.67 ms at 48 kHz), including worker scheduling margin.
Uncompressed PCM occupies FL/FR only. AC-3 and DTS Core IEC 61937 types 1 and
11/12/13 decode automatically, including same-codec layout changes. Null,
pause, unsupported and corrupt bursts are muted. Payloads of rejected bursts
are not scanned as nested IEC streams. Unknown/raw-DTS input stays muted until
a valid supported burst or 8192 consecutive exact-zero stereo frames. Short
encoded gaps never become PCM. Raw DTS, including 14-bit forms, is recognized
for suppression only; it is not decoded.

Channel positions come from the decoded frame, not its channel count. For
example, `5.1(side)` occupies SL/SR while a true back layout occupies BL/BR.
LFE has the decoder's original PCM scale: no separate boost, attenuation,
fold-down, crossover, or headroom processing happens here. FFmpeg's decoder
options are retained across codec flushes.

## Status and Python lifecycle

`--status PATH` atomically replaces a small JSON file, normally about every
100 ms. Fields are `codec`, `sample_rate`, `layout`, `positions` (decoder order),
`bus_positions` (fixed output order), `errors`, `decoded_bursts`,
`lookahead_frames`, `state`, and `mute_reason`. `state` is `pcm`, `encoded` or
`muted`. A corrupt burst after valid decoding retains the last valid codec and
layout, with `state=muted` and its reason; this is not a successful current
decode. A discontinuity clears current layout metadata.

`audio.digital_input.DigitalInput` exposes:

- `await start(source_name, left, right, rate=48000)`
- `await stop()`
- `snapshot()` (bounded metadata plus running/PID/source/error/stderr readback)
- `await links_present()` and `await repair_links()`
- `output_ports`, `carrier_ports`, and `node_name`

Channel names such as `AUX2` resolve only within the explicitly selected node
to `capture_AUX2` or `output_AUX2`; explicit port names are also accepted.
Repeated starts for the same selection verify/repair links and preserve the
PID and decoder state. A changed selection restarts the helper. The adapter
creates only capture-to-carrier links and checks all ten helper ports. It
rejects duplicate nodes, missing binaries and missing selected ports, drains a
bounded 16 KiB stderr tail, periodically repairs capture links, and terminates
and reaps its owned process group even on cancellation. DSP links remain the
runtime's responsibility.

## Offline verification

```sh
native_surround/build/fxroute-surround-input --offline INPUT.s32 OUTPUT.f32 --status STATUS.json
python3 scripts/test_surround_ingress.py
python3 native_surround/test_captures.py
python3 native_surround/test_live.py
```

Offline input is little-endian interleaved stereo S32: IEC carrier words occupy
the upper 16 bits; regular PCM retains its full sample scale. Output is
interleaved eight-channel float32 in bus order, with exactly the input frame
count. Recognition delay is removed for offline output, and the last incomplete
burst is muted without discarding prior valid decoded audio. `--chunk FRAMES`
varies input boundaries for regression tests.

The capture verifier uses `SURROUND_CAPTURE_ROOT` (default `/tmp/opencode`) and
skips captures that are absent. The live verifier starts and stops its own
private hardware-free PipeWire server; it never changes the normal server,
hardware, product service or graph rate.

Limitations: only 48 kHz decoded audio is accepted, raw DTS is suppressed rather
than decoded, and arbitrary compressed data lacking any recognizable sync
cannot be distinguished reliably from PCM. After a detected unknown stream,
silence or a valid supported burst is required to leave the mute latch.
