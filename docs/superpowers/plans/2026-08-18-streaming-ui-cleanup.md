# Streaming UI Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Spotify and Qobuz compact and centered, make Tidal browse/search the primary surface, fix Tidal search clearing, and reserve layout space for the existing global footer without changing playback ownership or backend behavior.

**Architecture:** Preserve the existing shared provider DOM and capability-driven rendering in `static/streaming.js`. Add a small Tidal-specific rendering distinction using classes/state already owned by the streaming module, then express the visual hierarchy in the existing `static/style.css`; only update visible navigation text and asset query versions in `static/index.html` if required. Add structural JavaScript/CSS regression checks beside the existing streaming tests.

**Tech Stack:** Existing vanilla JavaScript, HTML, CSS, Node syntax checks, Python structural regression tests.

**Spec:** User-provided Auftrag: Streaming-UI visuell aufräumen - Spotify/Qobuz kompakt, Tidal als Browser.

## Global Constraints

- Do not change playback, ownership, coordinator, routing, DSP, provider API, backend, sample-rate, installer, or volume-sync architecture.
- Only `static/index.html` is served; do not edit the stale repository-root `index.html`.
- Preserve the existing global footer and its playback/ownership logic.
- Keep Tidal track results compact with cover, track, artist, album, and duration.
- Tidal upper tabs are browse areas; result-type chips are subordinate search filters.
- Empty Tidal state must not create a large ghost player surface.
- The fixed footer requires a centralized safe-area layout value, not an arbitrary one-off margin.
- Use English in code comments and test messages.

---

### Task 1: Add failing UI contract tests

**Files:**
- Modify: `scripts/test_streaming_ui_actions.js`
- Modify or create: the existing focused CSS/structure regression test that covers `static/style.css` and `static/index.html`

**Interfaces:**
- Tests inspect source-level DOM/CSS contracts without requiring a browser.
- The tests must detect old visible `TIDAL` navigation text, stale Tidal search-clear behavior, large Tidal empty/player classes, missing compact-provider classes, and missing footer safe-area declarations.

- [ ] **Step 1: Add tests for the desired contracts**

Assert that `static/streaming.js` contains:
- visible provider metadata `tidal: { name: 'Tidal'`;
- Tidal search empty-query handling that clears the result container and returns to a neutral state;
- no large Tidal empty-player rendering path, while retaining the shared empty state for Spotify/Qobuz;
- a compact Tidal now-playing class/state and the browse-first content structure.

Assert that the served shell and styles contain:
- visible navigation text `Tidal` and no navigation label `TIDAL`;
- a centralized `--playback-footer-space` safe-area use in the body/layout and a provider-content bottom padding/margin based on it;
- compact centered provider layout rules and subordinate Tidal browse/search styling.

- [ ] **Step 2: Run the focused tests and confirm they fail for the missing contracts**

Run: `node scripts/test_streaming_ui_actions.js` and the focused CSS/structure test.
Expected: failures identify the old visible TIDAL label, missing empty-query reset, and missing layout contracts.

---

### Task 2: Fix provider rendering and Tidal search state

**Files:**
- Modify: `static/streaming.js`

**Interfaces:**
- Preserve `renderProvider(providerId, data)`, transport adapters, fetch endpoints, and all footer-facing callbacks.
- Add only presentation/state behavior local to Tidal rendering.

- [ ] **Step 1: Make the failing tests pass with minimal rendering changes**

Use `PROVIDER_META.tidal.name = 'Tidal'`. In the shared render path, apply a provider-specific compact class to Tidal now-playing and suppress the large shared empty player for Tidal while keeping its content visible when authenticated. Keep Tidal Browse as the primary content surface.

In `renderTidalSearch`, make `doSearch()` trim the value and, when empty, clear `#tidal-search-results`, clear the executed query state, remove any result heading/state, and render the neutral message without issuing a search request. Preserve old results while the user edits non-empty text. Keep Enter and the Search button on the same handler; optionally wire Escape to the same clear behavior.

- [ ] **Step 2: Run the focused JavaScript tests**

Run: `node scripts/test_streaming_ui_actions.js`.
Expected: PASS with no playback-contract regressions.

- [ ] **Step 3: Refactor only duplicated Tidal presentation helpers**

Keep the shared provider DOM and extract no new abstraction unless the implementation repeats a concrete Tidal-only operation. Re-run the focused tests after any cleanup.

---

### Task 3: Rework existing CSS hierarchy

**Files:**
- Modify: `static/style.css`

**Interfaces:**
- Preserve existing global tokens, footer dimensions, tab behavior, and mobile breakpoints.
- Add selectors to the existing streaming CSS section rather than an independent parallel style layer.

- [ ] **Step 1: Add compact centered Spotify/Qobuz rules**

Constrain `.streaming-now-playing` for Spotify/Qobuz to a sensible desktop max width and center it. Use a compact grid/flex composition so cover, metadata, transport, and progress read as one unit. Keep mobile layouts stacked and full width within the existing content padding.

- [ ] **Step 2: De-emphasize status lines and Tidal now playing**

Style `.streaming-status-line` as a small secondary line near the provider content. Style the Tidal compact state as a short horizontal summary with no duplicate full transport/progress controls. Make Tidal Browse tabs visually stronger than result chips.

- [ ] **Step 3: Remove large idle Tidal whitespace and protect the footer safe area**

Use the existing `--playback-footer-space` token for the content's bottom padding. Ensure the last result can scroll above the fixed footer on desktop and narrow mobile widths, without introducing a hardcoded one-off `180px` margin.

- [ ] **Step 4: Run CSS/structure tests and inspect the diff**

Run: focused CSS/structure tests and `git diff --check`.
Expected: PASS and no unrelated CSS layer or backend changes.

---

### Task 4: Update served navigation text and version references

**Files:**
- Modify: `static/index.html`
- Modify: `static/app.js` only if rendering requires it

**Interfaces:**
- Provider/API identifiers remain `tidal`.
- Only visible UI copy changes from `TIDAL` to `Tidal`; no provider endpoint or ownership changes.

- [ ] **Step 1: Change only the served navigation label**

Update the main navigation label to `Tidal`, retain `data-tab="tidal"`, and leave the stale root `index.html` untouched.

- [ ] **Step 2: Bump only changed cached static assets if required**

If `static/streaming.js` or `static/style.css` query strings are present and changed, update their independent versions in `static/index.html`. Confirm tests do not hardcode old versions.

- [ ] **Step 3: Run `node --check` for changed JavaScript**

Run: `node --check static/streaming.js` and `node --check static/app.js` if touched.
Expected: exit 0.

---

### Task 5: Verify all requested behavior

**Files:**
- No new production files.

- [ ] **Step 1: Run focused streaming tests**

Run the relevant `scripts/test_streaming_*.js`, `scripts/test_streaming_*.py`, footer/owner tests, and playback transport contract tests.

- [ ] **Step 2: Run required cheap checks**

Run: `node --check static/streaming.js`; `python3 -m py_compile scripts/test_*.py`; `git diff --check`; `python3 scripts/check_router_structure.py`.

- [ ] **Step 3: Run the full test suite once**

Run: `scripts/run_tests.sh`.
Expected: pass, with only the documented local native-DSP dependency skips.

- [ ] **Step 4: Deploy only after local verification, using the required rsync dry run first**

Count changed files with `rsync -an --checksum --itemize-changes`, back up corresponding `.104` files, transfer only the verified working-tree changes, restart `fxroute.service` if needed, and verify status/HTTP/static assets.

- [ ] **Step 5: Perform desktop and mobile live acceptance on `.104`**

Verify Spotify/Qobuz compact centering, Tidal browse-first hierarchy, compact Tidal now playing, query search, clear+Enter, favorites/playlists, track playback, footer clearance, and a narrow viewport.

- [ ] **Step 6: Review the final diff and create one focused local commit only after successful verification**

Do not push. Leave `.104` in the verified state and leave no backup/scratch files in the repository.
