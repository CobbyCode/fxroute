# Ticket: LDN-015

## Project
FXRoute

## Goal
Run SPL Calibration pink noise through a defined neutral Loudness state,
independent of the previously active Loudness calibration and Strength.

## Task
At SPL Calibration start, snapshot the live Loudness bypass, volume, and
output-gain, bypass Loudness, and set its runtime output gain to 0 dB. Restore
the exact live state on stop, save, cancel, or failure without loading a preset
or reconfiguring the graph.

## Input
Confirmed LDN-014 production code
`911c542c27a5fcb01b0a5e728563874e3312320e`.

## Expected Output
- Pink noise does not receive stored T, Strength, or compensation gain.
- All other filters, convolver, headroom, routing, and crossover stages remain
  unchanged.
- Exact Loudness runtime state and system master restoration on every exit.
- Normal sweeps and AutoSub remain unchanged.
- No preset reload, graph reconfigure, formula, gain-structure, or Strength
  changes.
- Focused tests, separate commit, deployment to `.104`; no push or release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Preserve unrelated dirty AutoSub changes and project artifacts.

## Status
done

## Implementation

- Added acknowledged runtime reads for EasyEffects plugin properties.
- SPL Calibration snapshots live Loudness `bypass`, `volume`, and
  `outputGain`, plus system master.
- Before pink noise starts, Loudness is bypassed and its runtime
  `outputGain` is set to 0 dB; a 100 ms propagation window is observed.
- Stored calibration T, Strength, and Loudness volume are never rewritten or
  applied during the neutral session.
- The common stop path restores output gain and volume while still bypassed,
  waits 100 ms when Loudness was originally active, restores the exact bypass
  state last, and restores system master.
- Manual stop, automatic completion/failure, save, cancel/close, noise
  generation failure, and playback-start failure all use this common restore.
- Removed the previous SPL-only preset apply/load cycle.
- Normal sweeps, AutoSub, other plugins, routing, and graph lifecycle are
  untouched.

## Verification

- `python3 -m py_compile easyeffects.py main.py scripts/test_spl_calibration.py scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_spl_calibration.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `git diff --check`

All passed. Tests verify neutral entry, exact stop restoration, no preset
application, unchanged SPL metadata persistence, and exact restoration after a
neutralization failure.

## Deployment

The reviewed `main.py` and `easyeffects.py` were compiled and deployed to
`.104`; FXRoute is active and the SPL Calibration endpoint is healthy.
Runtime inspection after deployment matched the existing disabled Loudness
state (`bypass=true`, `volume=-18.06179973983887`, `outputGain=0`). No pink
noise was automatically played. No push or release.

## Acceptance

Paul performed the SPL Calibration on `.104` and confirmed on 2026-07-30 that
the neutral Loudness calibration mode works as intended. The deployed
production implementation remains
`64a0c5e0635d056c1d176a245dcf31c01707183e`. No push or release.
