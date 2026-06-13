# FXRoute 2.1 Stage-1 Graph Link Plan

This is a review plan for the first controlled linked-audio test of the
PipeWire-native 2.1 helper. It is not runtime integration.

## Scope

Goal: prove that the native helper can sit in the graph between
`easyeffects_sink.monitor` and the selected 4-channel hardware sink.

Stage-1 routing remains fixed:

- helper `output_1` = input L
- helper `output_2` = input R
- helper `output_3` = `(L + R) * 0.5`
- helper `output_4` = `(L + R) * 0.5`

Out of scope:

- LR24/crossover DSP
- delay, level, polarity
- FXRoute runtime integration
- periodic repair loop
- sample-rate policy changes
- Python/stdin/stdout audio transport
- `parec`/`pw-play` transport

## Known `.104` Ports

Read-only observations from 2026-06-11:

- PipeWire: `1.6.5`
- selected output:
  `alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40`
- hardware playback ports:
  - `alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40:playback_FL`
  - `alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40:playback_FR`
  - `alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40:playback_RL`
  - `alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40:playback_RR`
- EasyEffects monitor source:
  - `easyeffects_sink:monitor_FL`
  - `easyeffects_sink:monitor_FR`
- current source links observed while paused:
  - `mpv:output_FL -> easyeffects_sink:playback_FL`
  - `mpv:output_FR -> easyeffects_sink:playback_FR`
  - `audio-src:output_FL -> easyeffects_sink:playback_FL`
  - `audio-src:output_FR -> easyeffects_sink:playback_FR`

## Preflight

Run only after Paul approves an actual linked test.

Required state:

- FXRoute service is active.
- Playback is stopped or paused before graph mutation.
- Output mode is `subwoofer-2.1`.
- Native helper runtime is still inactive/pending.
- Selected output has at least 4 channels.
- No old `parec`, `pw-play`, or `fxroute_21` runtime process exists.

Read-only checks:

```bash
curl -fsS http://127.0.0.1:8000/api/status | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/api/audio/outputs | python3 -m json.tool
pgrep -af 'parec|pw-play|fxroute_21' || true
pw-link -io
pw-link -l
```

Capture the current graph before mutation:

```bash
mkdir -p /tmp/fxroute-21-stage1
pw-link -l > /tmp/fxroute-21-stage1/links.before.txt
pw-link -io > /tmp/fxroute-21-stage1/ports.before.txt
```

## Helper Build Boundary

Preferred for the linked test:

1. Build on `.104` against its local PipeWire `1.6.5` headers if development
   packages are already present.
2. If headers are missing, do not install packages without separate approval.
   Use the same reversible local-RPM/sysroot method used for
   `AUDIO-21-ENGINE-002C`, or stop and report the blocker.
3. Place the temporary binary under `/tmp/fxroute-21-stage1/`, not in the live
   FXRoute runtime path.

The helper must be launched with the effective output rate reported by
`/api/audio/outputs`. It must not set PipeWire force-rate.

Example:

```bash
/tmp/fxroute-21-stage1/fxroute_21_passthrough \
  --node-name fxroute_21_stage1 \
  --rate "$EFFECTIVE_RATE" \
  --quantum 1024
```

## Link Mapping

Helper input links:

```text
easyeffects_sink:monitor_FL -> fxroute_21_stage1:input_L
easyeffects_sink:monitor_FR -> fxroute_21_stage1:input_R
```

Helper output links:

```text
fxroute_21_stage1:output_1 -> BEHRINGER playback_FL
fxroute_21_stage1:output_2 -> BEHRINGER playback_FR
fxroute_21_stage1:output_3 -> BEHRINGER playback_RL
fxroute_21_stage1:output_4 -> BEHRINGER playback_RR
```

Expanded commands:

```bash
HW=alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40

pw-link easyeffects_sink:monitor_FL fxroute_21_stage1:input_L
pw-link easyeffects_sink:monitor_FR fxroute_21_stage1:input_R

pw-link fxroute_21_stage1:output_1 "$HW:playback_FL"
pw-link fxroute_21_stage1:output_2 "$HW:playback_FR"
pw-link fxroute_21_stage1:output_3 "$HW:playback_RL"
pw-link fxroute_21_stage1:output_4 "$HW:playback_RR"
```

## Direct Hardware Path Guard

Before linking helper outputs to hardware, remove any direct EasyEffects output
path to the same hardware playback ports, if present, to avoid double playback:

```bash
HW=alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40

pw-link -d ee_soe_output_level:output_FL "$HW:playback_FL" 2>/dev/null || true
pw-link -d ee_soe_output_level:output_FR "$HW:playback_FR" 2>/dev/null || true
```

Do not run a periodic repair loop. This is a one-shot graph setup for the test.

## Validation During Linked Test

After links are created:

```bash
pw-link -l | grep -E 'fxroute_21_stage1|easyeffects_sink|playback_(FL|FR|RL|RR)'
```

Expected helper links:

- `easyeffects_sink:monitor_FL -> fxroute_21_stage1:input_L`
- `easyeffects_sink:monitor_FR -> fxroute_21_stage1:input_R`
- `fxroute_21_stage1:output_1 -> ...:playback_FL`
- `fxroute_21_stage1:output_2 -> ...:playback_FR`
- `fxroute_21_stage1:output_3 -> ...:playback_RL`
- `fxroute_21_stage1:output_4 -> ...:playback_RR`

Expected non-links:

- no `ee_soe_output_level:output_FL -> ...:playback_FL`
- no `ee_soe_output_level:output_FR -> ...:playback_FR`
- no `parec`
- no `pw-play`

Audio validation should start with low volume and short playback. Use
FXRoute's normal playback controls so the source path remains
`mpv/audio-src -> easyeffects_sink`.

Optional measurement-only validation may capture the hardware monitor briefly
with PipeWire tooling. This is for measurement only, not runtime transport.

## Abort Conditions

Abort immediately if any of these happen:

- helper exits unexpectedly;
- helper ports do not appear;
- any `pw-link` command fails;
- direct EasyEffects hardware links remain alongside helper outputs;
- playback double-feeds FL/FR;
- FXRoute status shows sample-rate churn or ownership changes;
- audible stutter, runaway volume, or unstable graph behavior appears.

## Rollback

Stop playback first:

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/stop >/dev/null || true
```

Disconnect helper links:

```bash
HW=alsa_output.usb-BEHRINGER_UMC204HD_192k-00.analog-surround-40

pw-link -d easyeffects_sink:monitor_FL fxroute_21_stage1:input_L 2>/dev/null || true
pw-link -d easyeffects_sink:monitor_FR fxroute_21_stage1:input_R 2>/dev/null || true

pw-link -d fxroute_21_stage1:output_1 "$HW:playback_FL" 2>/dev/null || true
pw-link -d fxroute_21_stage1:output_2 "$HW:playback_FR" 2>/dev/null || true
pw-link -d fxroute_21_stage1:output_3 "$HW:playback_RL" 2>/dev/null || true
pw-link -d fxroute_21_stage1:output_4 "$HW:playback_RR" 2>/dev/null || true
```

Stop the helper process:

```bash
pkill -f 'fxroute_21_passthrough.*fxroute_21_stage1' || true
```

Confirm cleanup:

```bash
pw-link -io | grep fxroute_21_stage1 || true
pw-link -l | grep fxroute_21_stage1 || true
pgrep -af 'fxroute_21_passthrough|parec|pw-play' || true
```

If the pre-test graph contained direct EasyEffects hardware links, restore only
those links captured in `links.before.txt`. Do not guess new links.

## Acceptance For The Linked Test

The later linked test is acceptable only if:

- helper starts only for the test and stops cleanly;
- helper ports are explicit and manually linked;
- no autoconnect is used;
- no direct EasyEffects FL/FR hardware path remains during helper playback;
- Stage-1 produces audible/signal output on FL/FR/RL/RR;
- no `parec`, `pw-play`, queue, ringbuffer, or Python audio pipe exists;
- no periodic 5s graph repair loop is used;
- FXRoute sample-rate owner logic is untouched;
- rollback leaves no helper node, helper links, or helper process behind.
