# Ticket: LDN-022

## Project
FXRoute

## Goal
Run SPL Calibration pink noise through neutral Auto Gain and Loudness states,
independent of the previously active Auto-Gain target and Loudness correction.

## Task
Extend only the existing SPL Calibration runtime snapshot/restore lifecycle:
remember Auto Gain bypass and target, bypass Auto Gain without changing its
target, and restore Auto Gain plus Loudness exactly on every exit path.

## Input
Confirmed neutral-Loudness SPL path from LDN-015 and current production code
through LDN-021.

## Expected Output
- SPL pink noise is not raised from −23 LUFS by Auto Gain.
- Auto Gain target is not changed during calibration.
- Auto Gain and Loudness runtime state is restored on stop, save, cancel,
  automatic completion, and failure.
- Normal sweeps, AutoSub, all other DSP stages, targets, formulas, and gain
  structure remain unchanged.
- No preset reload or graph reconfigure.
- Focused tests, separate commit, deployment to `.104`; no push or release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Preserve unrelated dirty AutoSub changes and project artifacts.

## Status
review

## Implementation

- SPL Calibration snapshots live Auto Gain `bypass` and `target` together with
  the existing Loudness and system-master state.
- Auto Gain is bypassed through the EasyEffects runtime property before Pink
  Noise starts; its target is never changed during calibration.
- The shared stop path restores the exact Auto Gain target and bypass state,
  then restores the existing Loudness state and system master.
- Manual stop, save, cancel/close, automatic completion, playback/generation
  failure, and neutralization failure continue to use the shared restore path.
- No preset load, graph reconfigure, target, formula, gain-structure, sweep, or
  AutoSub path was changed.

## Verification

- `python3 -m py_compile main.py scripts/test_spl_calibration.py`
- `python3 scripts/test_spl_calibration.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `git diff --check`

All passed. Tests verify Auto Gain bypass before playback, unchanged target,
exact Auto Gain/Loudness restoration, error rollback, and absence of preset
application.

## Deployment

Committed `main.py` from `d362b2b82b383dda133d060a98ec23bf514f8ce2`
was deployed byte-identically to `.104`. Remote compilation passed, FXRoute
restarted active, and the SPL Calibration endpoint returned HTTP 200. Pink
Noise was not started automatically. No push or release.
