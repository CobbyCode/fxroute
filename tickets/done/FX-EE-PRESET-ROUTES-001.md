# Ticket: FX-EE-PRESET-ROUTES-001 — Entdupliziere EasyEffects-Preset-Routen in `main.py`

## Status
done

## Owner
Codex

## Context
`main.py` enthält mehrere EasyEffects-Preset-Routen mit wiederholtem Ablauf:

- EasyEffects-Manager prüfen
- Extras aus JSON/Form einsammeln und normalisieren
- Preset erzeugen/importieren/kombinieren
- optional Preset laden
- Status holen
- WebSocket broadcast
- Peak-Monitor refresh schedulen
- Exceptions auf HTTP status mappen

Betroffene Gegend grob: `main.py` EasyEffects-Extras/Preset-Routen, insbesondere um:

- `/api/easyeffects/presets/combine`
- `/api/easyeffects/presets/load`
- `/api/easyeffects/presets/create-convolver`
- `/api/easyeffects/presets/import-json`
- `/api/easyeffects/presets/import-bundle`
- `/api/easyeffects/presets/create-with-ir`
- `/api/easyeffects/presets/create-peq`
- `/api/easyeffects/presets/import-rew-peq`
- `/api/easyeffects/presets/import-filter-dual`
- `/api/easyeffects/presets/delete`

## Goal
Reduce local duplication in the EasyEffects preset route code while preserving endpoint behavior.

This is a maintainability refactor, not a feature change.

## Non-goals / safety rails
- Do **not** rewrite fragile audio, PipeWire, Spotify, sample-rate recovery, source-transition, or EasyEffects socket recovery logic.
- Do **not** change public API request/response shapes unless unavoidable and explicitly documented.
- Do **not** change preset generation/import semantics in `easyeffects.py`.
- Do **not** introduce broad framework abstractions or move large unrelated code blocks.
- Keep the change small to medium and reviewable.

## Suggested approach
Prefer a small set of local helpers near the existing EasyEffects helpers, for example:

- `_require_easyeffects_manager()` returning manager or raising `503`
- a helper for extras-from-form dict construction if it removes repeated literal blocks safely
- a helper for post-mutation status/broadcast/peak refresh, with refresh reason passed explicitly
- a helper for optional load-after-create that preserves current behavior and active preset response
- a helper/decorator/function to map `FileNotFoundError`, `ValueError`, `RuntimeError`, `UnicodeDecodeError` only where semantics match

Be conservative: if one route has unique behavior, leave that route partially explicit.

## Acceptance criteria
- EasyEffects preset endpoints retain behavior and response fields.
- Duplication is materially reduced in `main.py` around the preset routes.
- Existing special cases are preserved:
  - `/load` updates compare active side when loading preset A/B.
  - Import bundle still validates zip members, imports matching IR files, reports missing kernels, and cleans temp zip.
  - Create/import with uploads still cleans temp files.
  - Dual filter import still handles dual convolver and dual PEQ variants.
  - Peak refresh reasons remain meaningful per route.
- No unrelated audio handoff logic touched.
- Tests/checks listed below pass.

## Required validation
Run at minimum:

```bash
python3 -m py_compile main.py config.py hardware_controller.py measurement.py easyeffects.py
node --check static/app.js
node --check static/measurement_dsp.js
node --check scripts/measurement_dsp_smoke.js
node scripts/measurement_dsp_smoke.js
git diff --check
```

Also inspect changed diff carefully for behavior drift.

If feasible, add a lightweight local test or script for helper behavior without requiring live EasyEffects. Avoid brittle integration tests that need the `.104` runtime unless Main explicitly asks.

## Expected output
- Modified code in `main.py` only unless a very small test/helper file is clearly justified.
- Ticket moved to `tickets/review/FX-EE-PRESET-ROUTES-001.md` with:
  - summary of refactor
  - list of changed helpers/routes
  - validation commands and results
  - any behavior intentionally left duplicated and why

## Completion summary
- Added local EasyEffects route helpers in `main.py`:
  - `_require_easyeffects_manager()`
  - `_effects_extras_from_form(...)`
  - `_finish_easyeffects_preset_mutation(...)`
  - `_raise_easyeffects_http_error(...)`
- Reused the helpers across EasyEffects preset/extras/compare routes where behavior matched.
- Preserved route response fields, load-after-create behavior, compare active-side update on `/load`, bundle validation/import flow, upload temp-file cleanup, and per-route peak refresh reasons.
- Left specialized import parsing/cleanup logic explicit in bundle, upload, REW, and dual-filter routes to avoid broad behavior changes.

## Changed helpers/routes
- Helpers added near existing EasyEffects extras helpers.
- Routes touched:
  - `/api/easyeffects/extras`
  - `/api/easyeffects/presets`
  - `/api/easyeffects/compare`
  - `/api/easyeffects/presets/combine`
  - `/api/easyeffects/presets/load`
  - `/api/easyeffects/irs/upload`
  - `/api/easyeffects/presets/create-convolver`
  - `/api/easyeffects/presets/import-json`
  - `/api/easyeffects/presets/import-bundle`
  - `/api/easyeffects/presets/create-with-ir`
  - `/api/easyeffects/presets/create-peq`
  - `/api/easyeffects/presets/import-rew-peq`
  - `/api/easyeffects/presets/import-filter-dual`
  - `/api/easyeffects/presets/delete`

## Main review and live validation
- Main reviewed `main.py` diff manually after Codex completion. Changes are limited to EasyEffects route helpers/routes in `main.py`; no PipeWire, Spotify, sample-rate, source-transition, or EasyEffects socket-recovery logic changed.
- Deployed `main.py` only to `.104` `/home/paul/fxroute` after creating a timestamped remote backup.
- Remote compile and `fxroute.service` restart succeeded.
- Live API smoke checks on `.104` passed for `/api/status`, `/api/easyeffects/presets`, `/api/easyeffects/extras`, direct `/api/easyeffects/presets/load`, `/api/easyeffects/presets/create-peq`, `/api/easyeffects/presets/import-rew-peq`, `/api/easyeffects/presets/import-filter-dual`, `/api/easyeffects/presets/import-json`, `/api/easyeffects/presets/create-convolver`, and `/api/easyeffects/presets/delete`. Test presets were deleted afterwards; final preset count delta was 0 and active preset remained unchanged.
- Browser UI loaded `http://192.168.178.104:8000/`, DSP tab opened, and browser console had no errors.
- Additional browser UI upload/download validation passed on `.104`: JSON preset upload/import and ZIP bundle upload/import via the Import panel; preset count rose `30 → 32`; browser-triggered downloads produced valid exported files in `/tmp/openclaw/downloads` (active convolver ZIP with `preset.json`, `.irs`, `.wav`, `manifest.json`; uploaded JSON and bundle-imported presets as valid JSON); cleanup returned preset count to `30`, left no `fxroute-ui-upload-*` presets, kept active preset unchanged, and `fxroute.service` remained active.

## Validation results
- `python3 -m py_compile main.py config.py hardware_controller.py measurement.py easyeffects.py` passed.
- `node --check static/app.js` passed.
- `node --check static/measurement_dsp.js` passed.
- `node --check scripts/measurement_dsp_smoke.js` passed.
- `node scripts/measurement_dsp_smoke.js` passed: `measurement_dsp smoke ok`.
- `git diff --check` passed.
- Reviewed the changed diff for behavior drift around `loadAfterCreate`, active compare side, bundle IR handling, temp-file cleanup, HTTP error mapping, and peak refresh reasons.

## Intentional duplication left
- `/api/easyeffects/presets/load` keeps its compare active-side update explicit because that behavior is unique to direct preset load.
- Bundle import keeps ZIP member selection, safe-path validation, IR selection/import, missing-kernel reporting, and temp ZIP cleanup explicit.
- Upload/dual-filter temp-file handling remains route-local because each route has different file lifetime and response semantics.
