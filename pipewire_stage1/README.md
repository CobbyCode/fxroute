# FXRoute PipeWire 2.1 native helper

PipeWire-native helper used by the staged 2.1 subwoofer runtime.

## Purpose

A minimal C/libpipewire helper that sits in the PipeWire graph between
EasyEffects monitor output and the selected multichannel hardware sink. It
provides the fixed 4-channel routing that the FXRoute 2.1 runtime needs.

## Port structure

Six explicit mono DSP ports, exposed as a `pw_filter` node:

| Port        | Routing                           |
|-------------|-----------------------------------|
| `input_L`   | —                                  |
| `input_R`   | —                                  |
| `output_1`  | input L                           |
| `output_2`  | input R                           |
| `output_3`  | `(L + R) * 0.5` (mono sum)       |
| `output_4`  | `(L + R) * 0.5` (mono sum)       |

Outputs 1/2 carry the stereo main pair and outputs 3/4 carry the mono
subwoofer feed. FXRoute connects these ports to the matching hardware
playback channels at runtime.

Port audio channel metadata is set explicitly:

- inputs: `FL`, `FR`
- outputs: `FL`, `FR`, `RL`, `RR`

## Architecture

- Single `pw_filter` process callback as the timing model. The filter exposes
  six explicit mono DSP ports; there are no independent capture or playback
  `pw_stream` objects.
- No autoconnect (`PW_KEY_NODE_AUTOCONNECT` is not set). FXRoute manages all
  graph links explicitly.
- Frame count is taken from `spa_io_position->clock.duration`, not from output
  chunk size.
- The callback dequeues all required port buffers before routing. If the graph
  cannot provide a complete set, already dequeued buffers are queued back.
- DSP buffer access uses `pw_filter_get_dsp_buffer()`.
- The helper follows explicit `--rate` and `--lowpass-hz` values; it does not
  set PipeWire force-rate.

## Build

```bash
./pipewire_stage1/build.sh
```

Requires PipeWire development packages:

- `libpipewire-0.3`
- `libspa-0.2`

If either module is missing, the build script reports the dependency and exits
without installing packages.