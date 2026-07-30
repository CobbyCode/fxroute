# Ticket: LDN-017

## Project
FXRoute

## Goal
Allow Auto Gain and Loudness to run together while preserving the canonical
Volume and the existing safe Loudness runtime transitions.

## Task
- Remove only the UI/backend Auto Gain/Loudness mutex.
- Replace Auto Gain targets with -12/-15/-18/-23 LUFS.
- Extend the central Loudness formula with `AutoGainOffset = G + 23` while
  Auto Gain is enabled.
- Apply Auto Gain/Loudness state changes through a focused runtime path without
  preset reload, graph reconfigure, peak refresh, slider jump, or master jump.
- Preserve plugin order Auto Gain → Loudness → Limiter and all accepted
  Strength, Volume, SPL Calibration, and duplicate-save fixes.

## Expected Output
- Auto Gain only, Loudness only, and both together work.
- Offsets 11/8/5/0 dB are exact for targets -12/-15/-18/-23.
- Loudness volume plus output gain always equals canonical A.
- Target and both enable states persist across restart.
- Separate commit and deployment to `.104`; no push or release.
- If `.104` stores -9, perform only the requested one-time switch to -12.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Notes
Preserve unrelated dirty AutoSub work and project artifacts.

## Analysis

- Backend mutex: `EasyEffectsManager.normalize_effects_extras`.
- UI mutex: checkbox change handlers plus disabled-state logic.
- Existing plugin order already ends in
  `autogain#0 → loudness#0 → limiter#0`.
- Central work-point calculation: `_loudness_plugin_payload`.
- The general extras path reloaded presets for non-Strength changes; the
  accepted Strength path already provided the guarded live transition model.

## Implementation

- Removed only the UI/backend Auto Gain/Loudness mutex.
- Auto Gain targets are strictly `-12/-15/-18/-23 LUFS`; `-9` is rejected.
- `_loudness_plugin_payload` now derives `AutoGainOffset = target + 23` when
  Auto Gain is enabled and folds it into the existing Strength offset.
- Added a shared guarded Auto Gain/Loudness runtime path for enable states,
  target, FFT, Strength, Volume, and SPL calibration changes.
- Kept the existing Strength entry point as a compatibility wrapper around the
  shared guarded path.
- Volume and SPL saves persist through the active-preset scope they used
  previously, but no longer reload the active preset.
- UI target feedback now uses LUFS and has a cache-buster for the new script.

## Verification

- `python3 -m py_compile easyeffects.py main.py scripts/test_loudness_integration.py scripts/test_loudness_live_regressions.py scripts/test_loudness_strength_runtime.py scripts/test_spl_calibration.py`
- `node --check static/app.js`
- `python3 scripts/test_loudness_integration.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_spl_calibration.py`
- `git diff --check`

All passed. Coverage verifies joint enablement without mutex, exact
11/8/5/0-dB Auto Gain offsets, exact `LSP volume + output gain = A`, unchanged
chain order, strict target values, guarded transitions without intermediate
Loudness bypass for enabled→enabled changes, rollback, canonical Volume,
reload-/refresh-free API routing, SPL behavior, and persisted joint states and
target after reload.

The first `.104` live pass exposed EasyEffects' acknowledged lower bound of
`-36 dB` for runtime `outputGain`: the temporary safety guard requested
`-45.7 dB` for Min/-12 and EasyEffects clamped it. The guard is now bounded to
the real port minimum while remaining at or below both valid endpoint gains;
the target work point and formula are unchanged.

## Deployment Preflight

`.104` currently stores Auto Gain disabled at `-15 LUFS`, so the requested
one-time `-9 → -12` deployment correction is not needed.

## Deployment

Commit `155340a80d787bdc7bce5c19d68e8c20c2367a69` was deployed to `.104`.
The deployed `main.py`, `easyeffects.py`, `static/app.js`, and
`static/index.html` match the committed files byte for byte.

With playback stopped, live checks covered:

- Auto Gain alone: active at `-15`, Loudness bypassed.
- Loudness alone: active with Auto Gain bypassed.
- Combined targets `-12/-15/-18/-23`: runtime offsets `11/8/5/0 dB`.
- For every target, runtime `LSP volume + outputGain` equalled
  `A=-21.392839410828756 dB`.
- Visible Volume remained `44` throughout target changes; System-Master
  remained `100` throughout the combined target changes.
- Both enabled and target `-18` survived a service restart with the exact
  runtime work point.
- No preset-load, graph-reconfigure, or peak-refresh event occurred during the
  runtime changes. The only graph lifecycle events were the two deliberate
  persistence/restoration service restarts.

The original user state was restored and rechecked after a final restart:
Auto Gain off, target `-15`, Loudness off, Strength Min, visible Volume and
System-Master both `44`. FXRoute is active. No push or release.
