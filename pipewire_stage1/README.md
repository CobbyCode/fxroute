# FXRoute PipeWire 2.1 native helper

This directory contains the PipeWire-native 2.1 helper used by the staged
FXRoute runtime replacement.

## Scope

- Minimal C/libpipewire helper source for native 2.1 output.
- PipeWire filter/client-style node with explicit mono DSP ports.
- Current Stage-2 routing:
  - Out 1 = input L
  - Out 2 = input R
  - Out 3 = LR24 lowpassed `(L + R) * 0.5`
  - Out 4 = LR24 lowpassed `(L + R) * 0.5`
- Logical ports are explicit:
  - `input_L`
  - `input_R`
  - `output_1`
  - `output_2`
  - `output_3`
  - `output_4`
- FXRoute must connect these graph ports explicitly later. This helper does not
  request PipeWire autoconnect and has no capture target or output target option.
- The helper follows explicit `--rate` and `--lowpass-hz` values supplied by
  FXRoute; it does not set PipeWire `force-rate`.
- No main highpass, delay, level, polarity, Measurement, UI, sample-rate
  switching, Python/stdin/stdout audio transport, queue/ringbuffer fixes, or
  periodic 5s `pw-link` repair are added here.

## Timing and buffer model

The corrected skeleton uses one PipeWire `pw_filter` process callback as the
single audio timing model. The filter exposes six explicit mono DSP ports rather
than two independent `pw_stream`s.

The process callback dequeues all required input and output port buffers before
routing any samples. If the graph cannot provide a complete set, already
dequeued buffers are queued back. This is not a capture-driven output-buffer
queue and does not silently drop an input cycle because a separate output buffer
is unavailable.

Frame count is taken from `spa_io_position->clock.duration`. The code does not
derive output capacity or frame count from a freshly dequeued output
`chunk->size`.

## Build

```bash
cd /home/pbclaw/ai/projects/fxroute
./pipewire_stage1/build.sh
```

The build script intentionally checks for the PipeWire development pkg-config
modules before compiling:

- `libpipewire-0.3`
- `libspa-0.2`

If either module is missing, the script reports the exact missing dependency and
does not install packages.

## Compile validation status

The base host does not have `pipewire-devel` installed, so plain
`pkg-config --modversion libpipewire-0.3` and
`pkg-config --modversion libspa-0.2` still fail.

For `AUDIO-21-ENGINE-002C`, the openSUSE `pipewire-devel` and
`libpipewire-0_3-0` RPMs were downloaded into a project-local temporary cache,
extracted into a local sysroot, and used via `PKG_CONFIG_PATH` plus
`PKG_CONFIG_SYSROOT_DIR`. With those headers/libraries, the helper compiled
successfully against PipeWire 1.6.6.

Compile-validation fixes from that pass:

- define `_GNU_SOURCE` before including PipeWire/SPA headers so SPA locale
  helpers expose `locale_t`, `newlocale`, and `uselocale`;
- include `<spa/buffer/buffer.h>` instead of the invalid `<spa/buffer.h>`;
- compare `pw_loop_add_signal(...)` against `NULL`, because it returns a source
  pointer rather than a negative status code.
- later linked-test debugging replaced the manual `pw_filter_dequeue_buffer(...)`
  path with `pw_filter_get_dsp_buffer(...)`, which is the correct DSP filter
  buffer API for this helper shape;
- explicit `audio.channel` metadata is now set on the six ports:
  - inputs: `FL`, `FR`
  - outputs: `FL`, `FR`, `RL`, `RR`

Temporary RPM/sysroot/build artifacts are validation-only and should not be
committed.

## Local node dry-run status

For `AUDIO-21-ENGINE-002D`, the helper was rebuilt through the same temporary
local sysroot and briefly started on the local development PipeWire session as
`fxroute_21_dry_run`.

Observed ports:

- `fxroute_21_dry_run:input_L`
- `fxroute_21_dry_run:input_R`
- `fxroute_21_dry_run:output_1`
- `fxroute_21_dry_run:output_2`
- `fxroute_21_dry_run:output_3`
- `fxroute_21_dry_run:output_4`

`pw-link -l` showed no active links involving the helper. The helper was stopped
after inspection and the node disappeared from `pw-link -io`.

## First linked-test result

For `AUDIO-21-ENGINE-002F`, Paul approved a controlled linked-audio test on
`.104`. The helper was copied only to `/tmp/fxroute-21-stage1`, manually linked,
tested briefly with normal FXRoute radio playback, then rolled back.

Confirmed:

- helper starts as `fxroute_21_stage1`;
- helper ports appear and can be linked manually;
- `easyeffects_sink:monitor_FL/FR` carry real L/R signal;
- helper `output_1/2` produce front signal when linked to BEHRINGER FL/FR;
- helper `output_3/4` produce mono signal when temporarily linked to BEHRINGER
  FL/FR for diagnosis;
- rollback removes helper ports, links, and process;
- no FXRoute runtime integration, deploy, service restart, or old subprocess
  transport was introduced.

Not accepted yet:

- with the intended mapping `output_3/4 -> BEHRINGER playback_RL/RR`, the
  hardware monitor capture still measured `RL/RR = 0`;
- adding explicit `audio.channel=RL/RR` on the helper output ports did not
  change that result.

Interpretation:

- the helper DSP path is no longer the primary suspect for mono generation,
  because `output_3/4` produce signal when routed to front ports;
- the remaining issue is likely in rear-port negotiation/linking, BEHRINGER
  surround profile behavior, or the chosen rear-channel monitor-capture method.

## Graph link plan

The next controlled test plan is documented in:

- `pipewire_stage1/STAGE1_GRAPH_LINK_PLAN.md`

The plan covers preflight checks, exact links from `easyeffects_sink.monitor`
to helper inputs, exact helper output links to the selected BEHRINGER 4-channel
hardware ports, direct EasyEffects hardware-path removal, validation, abort
conditions, and rollback. It remains a plan only; no live graph mutation or
runtime integration is performed by this document.

## Review notes

This pass intentionally addresses the rejected two-stream proposal:

- removed the capture stream / playback stream split;
- removed `PW_KEY_NODE_AUTOCONNECT` and `PW_STREAM_FLAG_AUTOCONNECT`;
- removed capture-target and output-target transport assumptions;
- replaced the helper with a `pw_filter` model and explicit ports;
- moved routing into the single filter process callback;
- stopped using output `chunk->size` to infer output frames.
