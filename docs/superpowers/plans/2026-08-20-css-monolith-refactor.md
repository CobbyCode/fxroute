# CSS Monolith Refactor — single-file → layered tokens + components Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split `static/style.css` (~7771 lines, ~550 selectors, ~120 media blocks) into a maintainable layered architecture with unified primitives and consolidated breakpoints, while preserving the live site on `static/index.html` via a single built output and `?v=` bump — one `yes` to run, no write-loop.

**Architecture:** Keep `static/style.css` as the single served file (AGENTS.md: "Only `static/index.html` is served" + `static/app.js`/`style.css?v=` cache busting). Introduce a `static/css/` source tree (`tokens`/`base`/`components`/`playback`/`responsive`) concatenated by a tiny build step into `static/style.css`; unify 5× favorite-heart + 4× track-row + 2× play-button aliases into primitives `.btn-fav` / `.track-row` / `.track-play`; collapse 9× playback-footer media blocks and scattered 600/700/760/900/980/1100/1180 breakpoints into 3 canonical ranges.

**Tech Stack:** Existing vanilla CSS, `cat`/tiny Node or shell concat, `python3 -m py_compile` + `git diff --check` + `scripts/run_tests.sh` + `node --check`, `.104` rsync dry-run → transfer → verify.

**Spec:** docs/codex analysis of `static/style.css` 2026-08-20: 7771 lines single-file monolith; duplicated primitives; sprawling media queries; `color-scheme: dark`; `backdrop-filter` stacking; `prefers-reduced-motion` present; no `@layer`.

## Global Constraints

- Do not change playback, ownership, coordinator, routing, DSP, provider API, backend, sample-rate, installer, or volume-sync architecture.
- Only `static/index.html` is served; do not edit the stale repository-root `index.html`.
- `main.py` is the FastAPI entrypoint; `dsp/api.py` hosts native DSP endpoints — do not touch for a CSS refactor.
- When changing cached static assets (`static/style.css`, `static/app.js`, `static/radio.js`, etc.) update their independent `?v=` query strings in `static/index.html` — do not sync versions.
- No `push`/`release`/`force-push` without explicit user request; `deploy` means local working-tree → `.104` via `rsync` + `systemctl restart`.
- Use English in code comments/log messages.
- Tests: `scripts/run_tests.sh` is the full suite; skips of 10 native DSP tests locally are expected.

---

## File Structure

**Create:**
- `static/css/_tokens.css` — `:root` vars, `color-scheme: dark`, `--font-*`, `--radius-*`, `--ease`
- `static/css/_base.css` — `*`/global scrollbar/`html`/`body`/selection + reset
- `static/css/_layout.css` — header / tabs / `.tab-content` / `.content-state`
- `static/css/_library.css` — library toolbar/search/tracks/folders
- `static/css/_radio.css` — `.radio-panel`, `.station-card`, `.stations-grid`, catalog
- `static/css/_streaming.css` — `.streaming-*`, TIDAL browse/detail, `.cover-detail-*`
- `static/css/_effects.css` — `.effects-*`, subwoofer, PEQ, convolver
- `static/css/_measurement.css` — `.measurement-*`, `.hybrid-*`, `.spl-*`
- `static/css/_playback.css` — `.playback-bar` desktop base + `.controls` / `.meter` / `.track-info`
- `static/css/_overlays.css` — manage dialogs, toasts, `.skip-link`, `.offline-indicator`
- `static/css/_responsive.css` — consolidated 3 playback ranges + shared component breakpoints
- `scripts/build-css.sh` — deterministic `cat _tokens.css _base.css ... _responsive.css > static/style.css` (order matters)
- `scripts/test_style_contract.py` (or `.js`) — structural regression guard for the built output

**Modify:**
- `static/index.html:1316-...` — only `style.css?v=` bump when `static/style.css` bytes change
- `static/style.css` — becomes the built artifact; no hand-edits after Task 2 (guard enforces this)

### Task 1: Add failing structural guards (no prod CSS change)

**Files:**
- Create: `scripts/test_style_contract.py`
- Create: `scripts/check_style_structure.sh` (optional helper)
- Modify: none in `static/`

**Interfaces:**
- Consumes: `static/style.css` bytes, `static/css/` sources after Task 2
- Produces: guard used by Tasks 2-5 and CI; must run via `python3 scripts/test_style_contract.py`

- [ ] **Step 1: Write the failing guard**

Create `scripts/test_style_contract.py` that asserts:
- `static/style.css` exists and is non-empty, served via `static/index.html` query string present
- no hand-divergence: if `static/css/` sources exist, `cat` order in `scripts/build-css.sh` reproduces `static/style.css` (hash check)
- no `!important` drift beyond allowlist (currently 2 sensitive-width overrides in `.effects-subwoofer-*-select`; otherwise zero)
- unified primitives exist in the built CSS: `.btn-fav`, `.track-row`, `.track-play`
- media sanity: at most 3 playback ranges (`min-width:1181` desktop + `701-1180` tablet + `max-width:700` phone) — fail if the built CSS contains >3 `@media` blocks that touch `.playback-bar`

```python
# snippet: scripts/test_style_contract.py
import re, hashlib, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
css = (ROOT/"static/style.css").read_text()
assert "@media (min-width: 1181px)" in css
assert re.search(r"@media \(max-width:\s*700px\)", css)
# playback range cap — tighten after consolidation
assert css.count(".playback-bar") >= 1
```

- [ ] **Step 2: Run guard to confirm it fails on current monolith**

Run: `python3 scripts/test_style_contract.py -v`; `python3 -m py_compile scripts/test_style_contract.py`
Expected: FAIL — e.g. "`!important` drift" and "playback range cap" fail, primitive aliases `.track-row-favorite` still present without `.btn-fav`.

- [ ] **Step 3: Commit guards**

```bash
git add scripts/test_style_contract.py
git commit -m "test: add CSS layered-build contract guard

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>"
```

### Task 2: Extract source tree + deterministic build (zero visual change)

**Files:**
- Create: `static/css/_tokens.css`, `_base.css`, `_layout.css`, `_library.css`, `_radio.css`, `_streaming.css`, `_effects.css`, `_measurement.css`, `_playback.css`, `_overlays.css`, `_responsive.css`
- Create: `scripts/build-css.sh`
- Modify: none in `static/style.css` yet (built artifact regenerated)

**Interfaces:**
- Consumes: current `static/style.css` line ranges (use `sed -n '1,35p'` for tokens, `36,250` for base, etc. — verify via `grep -n "^@media\|^\."`)
- Produces: `scripts/build-css.sh` that reproduces byte-identical `static/style.css`; `static/css/*.css` are the new source of truth

- [ ] **Step 1: Choose line splits via grep**

Run:
```bash
grep -n "^\.header\|^\.tabs\|^\.radio-panel\|^\.streaming-\|^\.effects-\|^\.measurement-\|^\.playback-bar\|^\.manage-" static/style.css | head -n 30
wc -l static/style.css
```
Allocate contiguous ranges to each `_*.css` preserving original order (tokens → base → overlays → layout → radio → library → streaming → effects → measurement → playback → responsive). No reordering of declarations.

- [ ] **Step 2: Create build script**

```bash
cat > scripts/build-css.sh <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/static/style.css"
cat "$ROOT/static/css/_tokens.css" \
    "$ROOT/static/css/_base.css" \
    "$ROOT/static/css/_layout.css" \
    "$ROOT/static/css/_overlays.css" \
    "$ROOT/static/css/_library.css" \
    "$ROOT/static/css/_radio.css" \
    "$ROOT/static/css/_streaming.css" \
    "$ROOT/static/css/_effects.css" \
    "$ROOT/static/css/_measurement.css" \
    "$ROOT/static/css/_playback.css" \
    "$ROOT/static/css/_responsive.css" >"$OUT"
echo "built $OUT ($(wc -l <"$OUT") lines)"
EOS
chmod +x scripts/build-css.sh
```

- [ ] **Step 3: Populate source files (verbatim slices)**

Extract each file by copying the corresponding lines from the current `static/style.css` into its `_*.css` bucket — do not reformat or dedupe yet. Keep `/* SPDX */` only in `_tokens.css`.

- [ ] **Step 4: Rebuild and verify byte-identity before any cleanup**

Run:
```bash
cp static/style.css /tmp/style.before.css
scripts/build-css.sh
diff -u /tmp/style.before.css static/style.css | head -n 40
python3 -m py_compile scripts/test_style_contract.py
python3 scripts/test_style_contract.py 2>&1 | head -n 40
git diff --check
```
Expected: `diff` empty (or only trailing newline normalization). Guard still fails on the same primitives / `!important` — intended, signals Task 3 scope.

- [ ] **Step 5: Commit extraction**

```bash
git add static/css/ scripts/build-css.sh
git commit -m "refactor: extract CSS source tree with deterministic build

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>"
```

### Task 3: Unify duplicated primitives + fix tokens (one yes to run)

**Files:**
- Modify: `static/css/_tokens.css` (add missing `--text-muted` coverage, `--scrollbar` if reused)
- Modify: `static/css/_base.css` (normalize scrollbar: keep `*::-webkit-scrollbar{width:8px}` once)
- Modify: `static/css/_library.css`, `_streaming.css` — dedupe track-row + fav aliases
- Modify: `static/css/_effects.css` — remove `!important` width overrides via narrower selectors
- Modify: `static/css/_playback.css` — normalize `.control-btn*` + `.meter-*` once
- Modify: `scripts/build-css.sh` — no change (order fixed)

**Interfaces:**
- Consumes: Task 2 source files
- Produces: smaller, token-consistent output; `static/style.css` rebuilt; guard must turn more green; visual diff must be zero at 360/700/1180px

- [ ] **Step 1: Token sweep — replace raw rgba with vars**

In `_tokens.css` add (if absent):
```css
--scrollbar: var(--bg-hover);
```
Replace in all `_*.css`:
- `rgba(255,255,255,0.06)` → `var(--border)` where used as border
- `rgba(255,255,255,0.10)` → `var(--border-strong)` where used as strong border
- `rgba(110,231,183,0.12)` → `var(--accent-dim)`
Keep visual identical; only substitution.

- [ ] **Step 2: Unify favorite primitive**

Create once (choose `_base.css` or new `_primitives.css` — if new, insert in build order after `_base.css`):
```css
.btn-fav{width:34px;height:34px;min-height:34px;border-radius:50%;border:1px solid var(--border);background:transparent;color:var(--text-muted);display:inline-flex;align-items:center;justify-content:center;transition:color .12s,border-color .12s,transform .12s}
.btn-fav:hover,.btn-fav.active{color:var(--accent);border-color:var(--accent)}
.btn-fav:active{transform:scale(.92)}
```
Keep aliases as single-line forwards for one release so `grep` guards pass, then add `/* TODO remove alias */`:
```css
.track-row-favorite,.track-fav,.streaming-fav,.album-favorite-toggle,.track-favorite-btn{composes: btn-fav} /* or copy-in once */
```

- [ ] **Step 3: Unify track-row + play primitives**

Define:
```css
.track-row{display:flex;align-items:center;gap:.7rem;padding:.45rem .6rem;border-radius:var(--radius-md);background:var(--bg-surface);border:1px solid var(--border)}
.track-row:hover{background:var(--bg-hover);border-color:var(--border-strong)}
.track-play{width:34px;height:34px;border-radius:50%;border:1px solid var(--border);background:transparent;color:var(--text-secondary);display:inline-flex;align-items:center;justify-content:center}
```

- [ ] **Step 4: Remove `!important` drift**

Replace the two subwoofer width fixes:
```css
/* was: width:min(100%,5.6rem) !important */
#tab-effects .effects-subwoofer-polarity-select{width:min(100%,5.6rem);min-width:0}
```
Use parent-qualified selector to win without `!important`. Rebuild and confirm `grep -c "!important" static/style.css` drops from 2 to 0 (or to the documented allowlist).

- [ ] **Step 5: Rebuild + guard + compile checks**

Run:
```bash
scripts/build-css.sh
python3 scripts/test_style_contract.py -v
python3 -m py_compile main.py 2>&1 | head
git diff --check | head -n 30
```

- [ ] **Step 6: Manual visual spot-check (local, no .104 yet)**

Open `static/index.html` at 360 / 700 / 1180 widths; verify no layout shift on Library/Radio/TIDAL/Effects/FX footer. Snapshot optional (`scripts/test_style_contract.py` captures).

- [ ] **Step 7: Commit primitive sweep**

```bash
git add static/css/ static/style.css
git commit -m "refactor: unify CSS primitives and token leaks

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>"
```

### Task 4: Consolidate responsive layer (playback footer focus)

**Files:**
- Modify: `static/css/_responsive.css` (primary), lightly `_playback.css` (desktop base stays there)
- Modify: `static/css/_layout.css` / `_library.css` / `_effects.css` — move only their `@media` blocks into `_responsive.css` grouped by range
- Modify: `static/style.css` via rebuild

**Interfaces:**
- Consumes: Tasks 2-3
- Produces: `@media` hygiene — 3 playback ranges + up to 5 shared component ranges, no functional CSS change

- [ ] **Step 1: Inventory current media map**

Run: `grep -n "@media" static/style.css | sort | uniq -c`
Classify into:
- playback: ≥1181 (desktop), 701–1180 (tablet), ≤700 (phone)
- shared: 600/760/900/980/1100 (component-specific)
Merge 900/980/1100 overlaps into the playback tablet range where possible.

- [ ] **Step 2: Consolidate playback footer**

Rebuild `_responsive.css` so all `.playback-bar`, `.playback-meter`, `.volume-control`, `.control-btn*` responsive overrides live under exactly 3 blocks:
```css
@media (min-width: 1181px){ /* v5/v6/v7 consolidated */ }
@media (min-width: 701px) and (max-width: 1180px){ /* tablet */ }
@media (max-width: 700px){ /* phone incl. 360/390 nests */ }
```
Remove 6-7 duplicate `@media (max-width:1180px)` / `(max-width:700px)` repetitions; keep nested cost rules only once. Desktop keeps `bottom:5px; min-height:98px; padding-block:.55rem` and `grid-template-areas` from the consolidated ≥1181 block; do not re-add hardcoded `180px` margins.

- [ ] **Step 3: Consolidate shared component queries**

Group remaining component queries by range, not by component — one `@media (max-width:600px)` that covers library/radio/streaming/effects, not 18 copies of the same query across files. Preserve `prefers-reduced-motion: reduce` at the end.

- [ ] **Step 4: Rebuild + full verification**

Run:
```bash
scripts/build-css.sh
python3 scripts/test_style_contract.py -v
python3 -m py_compile scripts/test_style_contract.py scripts/build-css.sh 2>&1 | head
git diff --check
node --check static/streaming.js 2>&1 | head
scripts/run_tests.sh 2>&1 | tail -n 40
```
Expected: suite passes with only documented local native-DSP skips.

- [ ] **Step 5: Commit responsive consolidation**

```bash
git add static/css/ static/style.css
git commit -m "refactor: consolidate responsive layers into canonical ranges

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>"
```

### Task 5: Version, verify, and deploy (one yes)

**Files:**
- Modify: `static/index.html` — only `style.css?v=` bump if `static/style.css` bytes changed
- No push

**Interfaces:**
- Consumes: Tasks 1-4 built artifact + `.104` host `paul@192.168.178.104:/home/paul/fxroute`
- Produces: verified `.104` running the new layered CSS; git working tree is the source of truth

- [ ] **Step 1: Bump served asset iff changed**

```bash
grep -oE 'style\.css\?v=[^"]+' static/index.html
# if static/style.css changed: increment patch v (0.9.95 → 0.9.96)
```

- [ ] **Step 2: Cheap checks**

Run: `python3 -m py_compile main.py; git diff --check | head -n 30`

- [ ] **Step 3: Deploy dry-run, backup, transfer, verify**

Run exactly:
```bash
rsync -an --checksum --itemize-changes --exclude='/index.html' --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.commandcode/' --exclude='node_modules/' --exclude='.ua/' --exclude='shoot.log' --exclude='skills-lock.json' --exclude='AGENTS.md' ./ paul@192.168.178.104:/home/paul/fxroute/ | head -n 80
ssh paul@192.168.178.104 "mkdir -p /tmp/fxroute-backup-\$(date +%Y%m%d-%H%M%S) && cp -a /home/paul/fxroute/static/style.css /tmp/fxroute-backup-\$(date +%Y%m%d-%H%M%S)/"
rsync -a --checksum --exclude='/index.html' --exclude='.git/' --exclude='__pycache__/' --exclude='*.pyc' --exclude='.commandcode/' --exclude='node_modules/' --exclude='.ua/' --exclude='shoot.log' --exclude='skills-lock.json' --exclude='AGENTS.md' ./ paul@192.168.178.104:/home/paul/fxroute/ | tail -n 20
ssh paul@192.168.178.104 "systemctl --user restart fxroute.service; sleep 3; systemctl --user is-active fxroute.service; curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/status | head -c 20"
ssh paul@192.168.178.104 "journalctl --user -u fxroute.service --no-pager -n 30 | tail -n 30"
```
Expected: `active`, HTTP `200`, no CSS-related errors.

- [ ] **Step 4: Single focused local commit of version bump only if needed**

```bash
git add static/index.html
git commit -m "chore: bump style.css cache version

Co-authored-by: CommandCodeBot <noreply@commandcode.ai>"  # only if bumped
```

- [ ] **Step 5: Leave `.104` in verified state, no push, no scratch files**

