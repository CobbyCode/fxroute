# FXRoute .104 Reference State — Consolidation Baseline

Reference for the upcoming consolidation.  The .104 test machine currently
runs this exact state and it is live-verified (real backend, no stubs).
Keep this record until the consolidation is merged and re-verified.

## Status

- Secured: 2026-08-21
- Service active, HTTP 200, playback and DSP verified against the real backend.

## Source commits

### Canonical checkout (branch `main`)

Backend and JS assets come from this line:

- `1de6eac` fix: stop applying Loudness volumeDb as in-graph master gain
  (parent: `5f0547d` fix: decouple global master from Loudness work point,
  detect spotifyd instance)

### CSS worktree (branch `refactor/css-cleanup`)

`static/index.html`, `static/style.css`, `static/css/*.css` and
`scripts/build-css.sh` come from this line:

- `8b7cf96` fix: align app.js cache tag with the refactored asset in the served shell
- `1c781c9` test: cover 280/300px widths in TIDAL subbar overflow contract
- `3f389ac` fix: let TIDAL subbar navigation shrink at small phone widths
- `501f16d` test: add Playwright UI walkthrough over the built CSS states
- `35c9071` refactor: sort CSS partials semantically, keep cascade contract
- `7d972cd` fix: restore small-phone brand size, one-column subwoofer controls,
  unified favorite geometry

## .104 file identity (md5, verified 2026-08-21)

| File | md5 |
|---|---|
| dsp/manager.py | 38ee1304dc42a195d623683763428f6e |
| audio/volume_contract.py | 9997c4daf71996b1d37184a9d1da13d1 |
| static/index.html | 99fcbc26c0d3b32a84ad74892d6ebcac |
| static/style.css | ec3d9f6592d0639a88f314e7e5a33f25 |
| static/app.js | 586e4e60367d696ea4d354b86fd06a35 |
| static/radio.js | 61aec69266c414132e45f91d786ecd76 |
| static/streaming.js | c0a6f8d9764e901ee19ceba91410ec7f |
| static/measurement_dsp.js | bae1e1568911a6a80164bd97694d7a39 |
| static/hybrid_measurement.js | 2bff3d64843ae69213e4fbe128a7b7a7 |
| VERSION | 661f033885da6ba3a942be9cb07690be (0.9.13) |
| scripts/build-css.sh | 37bd797995079a9369c994ee4528c6b2 |
| static/css/_base.css | a75128bd8114960ca74aad66a04343a9 |
| static/css/_tokens.css | f2d9a12040bcb2efc9a25e17952fa9df |

All 11 `static/css/_*.css` partials are deployed (built `static/style.css`
matches the worktree build).  `static/index.html` references
`style.css?v=0.9.99` and `app.js?v=0.9.52`.

## Consistency verification (2026-08-21)

- rsync `--checksum` dry-run, canonical checkout → .104 (backend + JS assets):
  0 content differences (mtime-only drift on 8 files).
- rsync `--checksum` dry-run, CSS worktree → .104 (CSS-owned files):
  0 content differences (mtime-only drift on 2 files).

## Live-verified on .104

- CSS: brand 35x35 px ≤ 600 px; subwoofer controls one column ≤ 760 px;
  favorite primitives unified per breakpoint (28x28 / 40x40); TIDAL subbar
  navigation no overflow at 280/300/320 px.
- Loudness: in-graph master gain 0 dB (previously `volumeDb`, −34.118 dB on
  .104, which muted playback); loudness on/off never moves the master;
  master slider works with loudness enabled; playback audible (confirmed by
  ear on 2026-08-21).
- Streaming tabs (Spotify/Qobuz/TIDAL) render live content, no console errors.

## Deploy backups on .104 (restore points, under `backups/`)

- `css-deploy-20260820-234504/`
- `tidal-subbar-fix-20260820-235812/`
- `tidal-subbar-index-20260821-000549/`
- `appjs-tag-consistency-20260821-010107/`
- `loudness-fix-20260821-010134/`

## Notes / divergence

- The git repo inside the .104 application directory is a frozen snapshot
  (HEAD `72064f8` "Freeze deployed test state at 0.9.12", 2026-08-16) whose
  working tree was overwritten by direct deploys.  It is not a source of
  truth for this reference.
- The canonical checkout's own `static/style.css`, `static/css/` and
  `scripts/build-css.sh` are the pre-CSS-build state; two CSS tests
  (`scripts/test_style_contract.py`, `scripts/test_streaming_ui_structure.js`)
  fail there against the stale build.  The CSS worktree is the CSS source of
  truth; the canonical checkout is the backend/JS source of truth.
- Native DSP engine binaries are built on .104 and are not tracked in git;
  they are not part of this reference.
