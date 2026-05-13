# FX-MEASUREMENT-DSP-001 — Extract measurement DSP helpers from app.js

## Status
review

## Goal
Refactor the pure Measurement/Convolver DSP/math code out of `static/app.js` into a separate browser-loadable JS helper module/file, without changing UI behavior.

## Scope
- Create a small standalone file, preferred: `static/measurement_dsp.js`.
- No build system.
- Expose helpers through a simple global, e.g. `window.FXRouteMeasurementDsp`.
- Move only pure/math-ish helpers first. Suggested candidates:
  - curve interpolation / target curve db helpers if practical
  - convolver correction-bin / dip-guard logic
  - FFT / inverse FFT helpers
  - linear/minimum phase impulse generation helpers
  - WAV writer helpers
  - PEQ magnitude helper if it can be moved cleanly
- Keep UI state, DOM, Canvas, event handlers, fetch/API calls in `static/app.js`.
- Update `static/index.html` to load the new helper before `app.js` if needed.
- Add lightweight Node-based smoke tests if practical without introducing a test framework.

## Constraints
- Conservative refactor only: preserve behavior and function signatures as much as possible.
- Do not touch samplerate/Spotify/Peak-Monitor handoff logic.
- Do not do a broad rewrite of Measurement UI.
- Do not commit unless explicitly instructed by Main after review.

## Expected output
- Modified files ready for review.
- Short summary of moved helpers and why.
- Validation commands run and results.
- Any risks or follow-up suggestions.

## Validation minimum
- `node --check static/app.js`
- `node --check static/measurement_dsp.js` if created
- `python3 -m py_compile main.py config.py hardware_controller.py measurement.py easyeffects.py`
- `git diff --check`
- If tests are added: run them and report command.

## Result
- Added `static/measurement_dsp.js` as browser-loadable/no-build helper namespace `window.FXRouteMeasurementDsp` with CommonJS export for smoke tests.
- Updated `static/index.html` to load `measurement_dsp.js` before `app.js`.
- Kept UI/state/DOM/fetch logic in `static/app.js`; existing app function names now delegate to the helper where behavior was moved.
- Added `scripts/measurement_dsp_smoke.js` for lightweight Node validation.

## Extracted helpers
- Measurement trace smoothing and graph range/coordinate math.
- Measurement convolver curve interpolation, correction analysis, adaptive/gentle dip guard, FIR magnitude bins, FFT/IFFT, linear/minimum phase impulse generation, and WAV writer.
- Measurement PEQ clamp helpers and biquad magnitude response helper.

## Validation run
- `node --check static/app.js`
- `node --check static/measurement_dsp.js`
- `node --check scripts/measurement_dsp_smoke.js`
- `node scripts/measurement_dsp_smoke.js`
- `python3 -m py_compile main.py config.py hardware_controller.py measurement.py easyeffects.py`
- `git diff --check`

All commands passed.

## Risks / follow-up
- Browser runtime behavior was not exercised with Playwright/live UI; validation is syntax plus pure-DSP smoke coverage.
- `static/app.js` intentionally keeps thin compatibility wrappers to minimize call-site churn; a later refactor can remove those wrappers once review confirms the split is stable.

## Main review
Accepted.

Additional review changes:
- Bumped static asset cache token to `0.6.1-measurement-dsp-split` for both `measurement_dsp.js` and `app.js`.
- Added a neutral changelog bullet for the DSP helper split.

Review validation:
- `node --check static/app.js`
- `node --check static/measurement_dsp.js`
- `node --check scripts/measurement_dsp_smoke.js`
- `node scripts/measurement_dsp_smoke.js`
- `python3 -m py_compile main.py config.py hardware_controller.py measurement.py easyeffects.py`
- `git diff --check`
