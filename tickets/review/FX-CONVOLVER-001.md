# FX-CONVOLVER-001 — Implement Convolver Assist v1 in measurement view

## Status
review

## Goal
Add an experimental automatic Convolver Assist workflow beside the existing PEQ Assist workflow in the FXRoute measurement view.

## Context
Paul pushed the current stable state and explicitly marked the next phase as experimental. Protect the existing PEQ Assist behavior when PEQ mode is active.

## Requirements

### UI
- Add a PEQ / Convolver mode toggle next to Reset in the measurement view.
- Add a Target Curve dropdown near the mode toggle / Reset controls.
- PEQ mode keeps existing PEQ Assist behavior.
- Convolver mode replaces the PEQ Assist panel under the graph with a Convolver Assist panel.

### Target curves
All curves must be defined full-range from 20 Hz to 20 kHz. Only selected correction range is used for calculation.

- Neutral: 0 dB from 20 Hz to 20 kHz.
- Bass Shelf:
  - 20 +4, 30 +4, 50 +3, 80 +2, 120 +1, 200 0, 1000 0, 20000 0 dB
- Harman-style:
  - 20 +5, 30 +4.5, 50 +4, 80 +3, 120 +2, 200 +1, 500 +0.5, 1000 0, 2000 -1, 5000 -2.5, 10000 -4, 20000 -5 dB
- Bruel & Kjaer-style:
  - 20 +2, 50 +2, 100 +1.5, 200 +1, 500 +0.5, 1000 0, 2000 -0.5, 5000 -1.5, 10000 -2.5, 20000 -3.5 dB

### Correction range
- Transparent draggable range overlay on measurement graph; measurement lines remain visible.
- Default: 20 Hz to 250 Hz.
- Overlay moves as block; left/right edges resize.
- Synced numeric fields: Range Start Hz, Range End Hz.

### Convolver Assist panel
- Target Curve dropdown
- Range Start / End fields
- Max Boost dropdown
- Sample Rate dropdown
- Quality dropdown
- Take L, Take R, Take Both

### Max Boost dropdown
- Cuts only / 0 dB
- Safe / +1.5 dB
- Normal / +3 dB default
- Strong / +6 dB

### Internal defaults
- Max Boost: +3 dB
- Max Cut: -9 dB
- Safety Margin: 1 dB
- Auto Gain: enabled
- Smoothing: automatic
- Phase Mode: Minimum Phase
- IR Length: Auto

### Sample Rate dropdown
- Auto default
- 44.1, 48, 88.2, 96, 176.4, 192 kHz

### Quality / advanced options
- Main UI quality: Auto default.
- Advanced options may expose:
  - Phase Mode: Minimum Phase default/recommended, Linear Phase advanced
  - IR Length: Auto default, Normal, High
  - Max Cut
  - Safety Margin
  - Smoothing
- Do not implement Mixed Phase in v1.

### Calculation
For each frequency bin inside selected range:

```text
correctionDb = targetCurveDb - measuredDb
```

Then:
- apply smoothing
- apply max boost limit
- apply max cut limit
- apply deep bass safety if needed
- generate FIR correction IR
- convert to minimum phase by default
- calculate auto gain

Auto gain:
```text
autoGainDb = -(maxPositiveCorrectionDb + safetyMarginDb)
```
Round to nearest 0.5 dB.

For v1:
- Bake negative auto gain into generated IR.
- Store `autoGainDb` in metadata.
- Include auto gain in visible filter name.

Example names:
- `Conv Neutral 20-250Hz -4dB`
- `Conv Harman 20-300Hz -5dB`
- `Conv BK 20-500Hz -3.5dB`
- `Conv LR Neutral 20-250Hz -4dB`

### Take behavior
- Take L uses left measurement.
- Take R uses right measurement.
- Take Both creates stereo convolver from both measurements.
- Take Both uses the stronger required auto gain as shared displayed gain.
- Add generated convolver to same assist stack as PEQ filters.
- Convolver stack items visually distinct from PEQ items.
- PEQ and Convolver items apply in click order.

### Preset creation
- Keep existing preset creation flow.
- Final preset may contain PEQ and Convolver items in order.

### Warnings
Show when relevant:
- Range end > 1000 Hz: `Wide-range correction can change speaker tonality and may correct reflections instead of the speaker response.`
- `autoGainDb < -6 dB`: `This correction needs high headroom. Consider reducing Max Boost or narrowing the correction range.`
- Boost below 40 Hz: `Low bass boost can increase amplifier load and speaker excursion.`

## Validation
- Inspect existing implementation and choose the smallest coherent integration path.
- Run static syntax checks for changed JS/Python files.
- If feasible, run a local smoke check or direct UI/asset inspection.
- Return changed files, validation commands, and any v1 limitations explicitly.

## Deliverable
Implementation in `/home/pbclaw/ai/projects/fxroute-public`, ready for Main review.

## Implementation Notes

### Changed files
- `static/index.html`
- `static/app.js`
- `static/style.css`
- `tickets/review/FX-CONVOLVER-001.md`

### Implemented
- Added `measurement-assist-mode` PEQ / Convolver toggle beside Reset.
- Added `measurement-target-curve` Target Curve dropdown near Reset.
- Added `measurement-convolver-panel` with Target Curve, Range Start Hz, Range End Hz, Max Boost, Sample Rate, Quality, Take L, Take R, Take Both, summary, warnings, and feedback elements.
- Added full-range target curve definitions for `Neutral`, `Bass Shelf`, `Harman-style`, and `Bruel & Kjaer-style`.
- Added Convolver mode graph overlay for the correction range, defaulting to `20 Hz`–`250 Hz`; the block can be dragged, and left/right edges resize.
- Synced the range overlay with numeric fields `measurement-convolver-range-start` and `measurement-convolver-range-end`.
- Preserved PEQ Assist behavior in PEQ mode, including existing transfer into Create PEQ Preset and existing `/api/easyeffects/presets/create-peq` preset creation flow.
- Added experimental in-memory `state.easyeffects.assistStack` entries for PEQ and Convolver take actions, preserving click order while avoiding backend preset rewrites.
- Convolver take actions compute smoothed per-bin correction metadata inside the selected range, apply max boost / max cut, calculate rounded auto gain, name staged items with visible gain, and show required warnings.

### Validation commands run
- `git diff --check`
- `node --check static/app.js`
- `python3 -m py_compile main.py easyeffects.py measurement.py models.py`
- DOM/API identifier smoke check for the new measurement convolver IDs, `/api/easyeffects/presets/create-peq`, and `/api/easyeffects/presets/create-convolver`.

### Known v1 limits / open points
- FIR generation, minimum-phase conversion, IR file export/upload, and actual Convolver preset insertion are intentionally not wired in this MVP; existing backend `/api/easyeffects/presets/create-convolver` requires a pre-existing uploaded IR filename.
- Final preset creation still uses the existing PEQ preset flow; staged Convolver Assist items are metadata-only and not yet applied into EasyEffects presets.
- Convolver stack items are represented in `state.easyeffects.assistStack` and summarized in the Convolver panel; there is no full visual reorder/delete stack UI yet.
- Deep bass safety is currently warning-only plus max-boost limiting; no additional dynamic limiter is applied to generated correction metadata.

## Implementation notes
- Added PEQ / Convolver mode toggle and target curve selectors in the measurement graph header.
- Added Convolver Assist panel with target curve, correction range, max boost, sample rate, quality, and Take L / Take R / Take Both actions.
- Added transparent draggable correction range overlay with edge resize and block move behavior; numeric range fields stay synchronized.
- Added full-range target curves for Neutral, Bass Shelf, Harman-style, and Bruel & Kjaer-style.
- Take actions analyze selected measurement traces, calculate correction bins against the target curve, apply boost/cut limits and shared auto-gain, generate WAV FIR files, and create EasyEffects convolver presets through existing backend import endpoints.
- Existing PEQ Assist remains gated to PEQ mode and still writes the existing PEQ draft path.

## Validation
- `node --check static/app.js`
- `python3 -m py_compile main.py measurement.py easyeffects.py`
- `git diff --check`

## v1 limitations
- FIR generation is intentionally conservative and browser-side for this experimental v1.
- Minimum-phase mode is represented in metadata, but generated WAVs currently use a straightforward frequency-sampled FIR kernel rather than a full dedicated minimum-phase transform.

## Main review addendum
- Verified the local implementation against the actual repository state rather than relying on delegated notes.
- Confirmed backend routes and helper paths are present for `/api/easyeffects/presets/create-convolver`, `/api/easyeffects/presets/import-json`, `/api/easyeffects/presets/create-with-ir`, and `/api/easyeffects/presets/import-filter-dual`.
- Fixed Convolver measurement selection so `Take L` / `Take R` no longer silently fall back to the wrong opposite-channel measurement; they use the requested side or a stereo measurement only.
- Fixed Measurement `Reset` in Convolver mode to restore the full Convolver Assist draft state, not only the range endpoints.
- Fixed Reset button availability so Convolver-mode draft changes can be reset even when no current measurement trace is loaded.
- Re-ran validation after review fixes.

## Main validation
- `git diff --check`
- `node --check static/app.js`
- `python3 -m py_compile main.py measurement.py easyeffects.py`

## Live test note — 2026-05-09
- Paul tested the Convolver and aligned Measurement PEQ draft workflows on `.104`.
- Current assessment: the new workflow already works very well and should be kept as the current experimental baseline.
- Further polish is still needed, but no further changes should be forced before explicitly deciding the next refinement pass.
- Measurement PEQ was aligned with the Convolver pattern: `Take L/R/Both` stages a local draft, editable preset name is available, and `Create PEQ Preset` in the Measurement PEQ panel creates directly without using the old global Create PEQ Preset detour.
- PEQ mode intentionally allows multiple visible/selected measurement curves.
- Mobile polish after live feedback: long measurement names and active A/B preset names now render as a compact readable prefix (~24 chars) with ellipsis while preserving the full file link/title. Deployed to `.104` with cache token `0.5.2-mobile-name-v2`.
