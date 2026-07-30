# Ticket: LDN-016

## Project
FXRoute

## Goal
Prevent a Loudness Strength change from restoring a stale Volume/System-Master
value.

## Task
Trace the Strength request, response, and persistence path; keep Volume as one
canonical global value and make Strength mutate only its offset. Derive the
runtime Loudness values from the currently effective A.

## Expected Output
- Strength requests cannot overwrite canonical Volume from stale UI/API state.
- The visible Volume slider and System-Master remain unchanged.
- Existing guarded runtime transition remains preset-reload-free.
- Strength levels, SPL Calibration, and the gain formula remain unchanged.
- Focused regression tests, separate commit, deployment to `.104`.
- No push or release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Status
done

## Notes
Preserve all unrelated dirty AutoSub work and artifacts.

## Root Cause

`/api/volume` persisted the new canonical Loudness `volumeDb`, but the browser's
EasyEffects state retained the previous value. The next extras request sent
that stale `loudnessVolumeDb` together with the new Strength. The backend
accepted it, so the request ceased to be a pure Strength change and restored
the old Volume through the general preset path.

## Implementation

- Extras requests no longer mutate Loudness `volumeDb`; only `/api/volume`
  owns that canonical value.
- Extras payloads no longer send cached `loudnessVolumeDb`.
- `/api/volume` returns the persisted canonical `loudnessVolumeDb`, and the UI
  synchronizes its cached EasyEffects state from that acknowledged value.
- The existing guarded runtime Strength transition remains unchanged.

## Verification

- `python3 -m py_compile main.py easyeffects.py scripts/test_loudness_live_regressions.py scripts/test_loudness_strength_runtime.py scripts/test_spl_calibration.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_spl_calibration.py`
- `git diff --check`

All passed. The regression sends a deliberately stale `loudnessVolumeDb` with
a Min→Full request and verifies that the runtime-only Strength path is used,
canonical A remains `-42 dB`, and no preset load, system-volume write, or peak
refresh occurs.

## Deployment

Commit `856038cf32fe5b5a7efd52e97c43db0ef47dea6f` was deployed to `.104`;
deployed `main.py` and `static/app.js` matched the committed files byte for
byte and FXRoute restarted active. A state-neutral live request resent the
current Strength `light` with a deliberately stale `volumeDb=-3`. It was
handled as a no-op (`updated_presets=0`): canonical `volumeDb` remained
`-19.674128523856954`, visible Volume remained `47`, and Strength remained
`light`. No push or release.

## Acceptance

Paul reproduced the original Min → reduce Volume → Light/Med/Full workflow on
`.104` and confirmed on 2026-07-30 that the fix works. Accepted production code:
`856038cf32fe5b5a7efd52e97c43db0ef47dea6f`. No push or release.
