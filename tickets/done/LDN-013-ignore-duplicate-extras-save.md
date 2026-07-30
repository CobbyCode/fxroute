# Ticket: LDN-013

## Project
FXRoute

## Goal
Prevent the duplicate unchanged Extras request after a Strength action from
reloading the active EasyEffects preset and reconfiguring the audio graph.

## Task
Treat a normalized Extras payload identical to the persisted state as a no-op.
It must not rewrite presets, reload EasyEffects, broadcast another UI update,
or refresh the peak monitor.

## Input
LDN-012 commit `f68321c755558a77744dbc08d12c8a7a8d55d620`.
Live trace on `.104`: the direct Min→Light update completed at
07:04:07.171, followed 851 ms later by an unchanged full Extras save, two
preset loads, and a peak-monitor reconfigure.

## Expected Output
- Pure Strength transition remains direction-safe.
- Duplicate identical Extras POST returns success without side effects.
- No subsequent preset reload, graph reconfigure, or peak-monitor restart.
- Adjacent Strength transitions verified live in both directions.
- Separate local commit; no push or release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Preserve unrelated dirty AutoSub changes and project artifacts.

## Status
review

## Implementation

- The normalized request payload is compared with the persisted normalized
  Extras state before any runtime or persistence side effect.
- An identical payload returns success with zero updated presets.
- The no-op path does not write presets, load the active preset, broadcast a
  second UI state, or schedule a peak-monitor refresh.
- The LDN-012 direct runtime path is unchanged.

## Local Verification

- `python3 -m py_compile main.py scripts/test_loudness_live_regressions.py scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `git diff --check`

All passed. The regression test reproduces the duplicate identical Strength
save and verifies that it emits no side effects.

## Live Verification

- Deployed only the reviewed `main.py` no-op hunk to `.104`.
- Tested all adjacent transitions in both directions:
  `Light→Med→Full→Med→Light→Min→Light`.
- After every real change, a duplicate identical request was sent.
- Each real transition produced exactly one direct Strength event; every
  duplicate produced exactly one `Ignored unchanged` event.
- No preset-load, graph-reconfigure, global-extras refresh, or peak-monitor
  restart event occurred.
- Gain sum remained `-30.5182983699436 dB`; Loudness bypass remained `false`,
  Limiter input gain remained `0 dB`, and system master remained `1.00`.
- Main and Sub output links remained present.
- Restored initial live state: Strength Light, Loudness enabled, FFT 8192.
- No push or release.
