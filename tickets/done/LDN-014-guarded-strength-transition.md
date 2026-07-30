# Ticket: LDN-014

## Project
FXRoute

## Goal
Remove the remaining positive Loudness Strength transient while retaining
LDN-013 commit `b7d355551d5cdde4691a721721aab82d63fa714c`.

## Task
Determine whether EasyEffects can apply LSP Loudness `volume` and
`outputGain` atomically. If not, guard the live transition by attenuating the
Loudness output, applying the target volume, waiting for the audio path, and
softly restoring output gain to its exact target.

## Input
- EasyEffects 8.2.8 Local Server and source.
- LDN-012 direct runtime path.
- LDN-013 duplicate-save no-op path.

## Expected Output
- No positive intermediate gain in either direction.
- Only a brief attenuation is permitted.
- Exact final volume/output-gain targets and unchanged persistence.
- No preset reload, graph reconfigure, formula, level, or UI changes.
- Focused local tests and adjacent live transitions on `.104`.
- Separate local commit; no push or release.

## Target Path
`/home/pbclaw/ai/projects/fxroute`

## Notes
Preserve unrelated dirty AutoSub changes and project artifacts.

## Status
done

## Atomic Update Finding

EasyEffects 8.2.8 has no atomic Local Server command for multiple plugin
properties. The server parses one `set_property` line at a time and calls
`QObject::setProperty` individually. Loudness `volume` is independently bound
to the LSP LV2 control port, while `outputGain` updates the EasyEffects plugin
output multiplier through a separate signal. A successful property readback
therefore cannot confirm that both values took effect in the same audio block.

## Implementation

- Before every Strength transition, `outputGain` is lowered 18 dB below the
  lower of the old and new output-gain targets.
- The guard receives 60 ms to reach the audio path.
- LSP `volume` is set to its exact target and receives 100 ms to settle.
- `outputGain` is then restored monotonically to its exact target in steps no
  larger than 3 dB, spaced by 6 ms.
- Every intermediate analytical gain sum is below or equal to the final sum.
- Failure rollback keeps the guard active while restoring old volume, then
  ramps back to the old output gain.
- Persistence and the LDN-013 duplicate-save no-op path remain unchanged.

## Local Verification

- `python3 -m py_compile easyeffects.py main.py scripts/test_loudness_strength_runtime.py scripts/test_loudness_live_regressions.py`
- `python3 scripts/test_loudness_strength_runtime.py`
- `python3 scripts/test_loudness_live_regressions.py`
- `python3 scripts/test_loudness_integration.py`
- `git diff --check`

All passed. The Strength test covers every adjacent transition in both
directions, asserts the 18 dB guard, monotonic restoration, no intermediate
positive gain, exact final sum, persistence once, and guarded rollback.

## Deployment

The reviewed `easyeffects.py` was compiled and deployed to `.104`; FXRoute is
active. Loudness was already disabled with Strength Full and FFT 8192 after
restart, so no artificial live transition was performed and the user's state
was not changed. Acoustic review is pending.

## User Acceptance

Paul confirmed on `.104` that the guarded transition removed the audible
positive Strength transient. LDN-014 is accepted. The confirmed production
code commit is `911c542c27a5fcb01b0a5e728563874e3312320e`.
