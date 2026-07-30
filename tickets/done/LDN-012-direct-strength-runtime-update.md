# Ticket: LDN-012

## Project
FXRoute

## Goal
Eliminate the positive transient during Loudness Strength changes without
reloading or reconfiguring the EasyEffects graph.

## Task
Implement a pure-Strength runtime path that updates the active LSP Loudness
`volume` and `output-gain` directly in a direction-safe order, then persists
the Strength state exactly once without applying the full preset.

## Input
Confirmed production baseline LDN-010 at commit
`4fbdcc027c64757b86c4f0c6f72628473b6d25e2`, deployed on `.104`.

Measured cause:

- EasyEffects preset loading applies `output-gain` before `volume`.
- Low-to-high Strength therefore creates a temporary positive gain of up to
  30 dB.
- Each Strength action currently causes two preset reload/reconfigure events.

## Expected Output

- Direct runtime parameter updates with no preset load or graph reconfigure.
- Low-to-high Strength: lower `volume` before raising `output-gain`.
- High-to-low Strength: lower `output-gain` before raising `volume`.
- Final gain sum remains exactly unchanged for every level.
- System master, bypass, limiter, Strength levels, formula, SPL calibration,
  toggle logic, and UI remain unchanged.
- One persistence operation and one UI update per Strength action.
- Focused automated checks plus live adjacent transitions in both directions
  on `.104`.
- Separate local commit; no push and no release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Do not reuse the failed LDN-011 preset-based transition. Preserve unrelated
dirty AutoSub files and project artifacts.

## Status
done

## Implementation

- Added direct EasyEffects 8.2.8 Local Server updates for the active
  `loudness#0` `volume` and `outputGain` properties.
- Each mutation is acknowledged through `get_property` before the second
  property is changed.
- Larger offset: `outputGain` first, then `volume`.
- Smaller offset: `volume` first, then `outputGain`.
- A failed second mutation rolls the first mutation back; persistence does
  not run after a failed runtime transition.
- The final extras are persisted once to global extras and preset JSON files
  without `load_preset`.
- The pure-Strength route emits one UI broadcast and does not schedule the
  peak-monitor refresh/reconfigure path.

## Local Verification

- `python3 -m py_compile easyeffects.py main.py scripts/test_loudness_strength_runtime.py scripts/test_loudness_live_regressions.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_loudness_integration.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `git diff --check`

All passed.

## Live Verification

- Deployed only `main.py` and `easyeffects.py` to `.104`; the remote files
  differed from the local files only by the reviewed LDN-012 hunks.
- All adjacent transitions passed in both directions:
  `Med→Full→Med→Light→Min→Light→Med`.
- Every final `volume + outputGain` sum remained exactly
  `-30.5182983699436 dB`.
- Loudness bypass remained `false`, Limiter input gain remained `0 dB`, and
  system master remained `100% / 0 dB`.
- All Main L/R and Sub 1/2 Stage-to-hardware links remained present.
- The service log contained only direct Strength update events; no preset
  load, graph reconfigure, or peak-monitor refresh occurred.
- Initial Strength `Med`, FFT 8192, Loudness enabled, and stored volume were
  restored.
- Existing dirty AutoSub changes and project artifacts were preserved.
- No push or release.
