# FXRoute Web-Demo

A static, simulated frontend demo of FXRoute. It reuses the real product
frontend from the same checkout — there is **no second UI source** — and
adds only a demo/simulation layer on top.

## What is in this worktree

The demo layer lives in the same checkout as the product frontend:

- `demo/` — simulation source (`state.js`, `routes.js`, `xhr.js`,
  `streaming.js`, `ws.js`, `boot.js`, `demo.css`), fixtures
  (`data/`), `README.md` and the built `dist/` snapshot.
- `static/demo/` — demo-owned artwork pool (not part of the product UI).
- `scripts/build_demo.py`, `scripts/serve_demo.py`,
  `scripts/test_demo_behavior.js` — the whole toolchain.

## Seeded system

The demo opens on the machine the development box is actually measured
against: an 18-channel Focusrite Scarlett 16i16 4th Gen Pro in
`stereo-sub` mode, a 2-way LR24 crossover at 3 kHz with two subs behind an
80 Hz bass-management split, and the captured 18-channel input with the
split `Electrical Ref L/R` on the line inputs 7/8.

That state is what makes the Crossover, Subwoofer and Speaker Auto
Alignment surfaces visible at all — the frontend gates them on a complete
crossover topology with the Global bank selected. Keep it consistent when
changing the seed: the alignment run below is only offered under those
conditions.

The same seed carries the **Surround** mode, so the third output mode is
explorable without a Surround source or a second machine: the stereo-sub
front end expanded to a fully routed 7.2 system on the same 18-channel
Scarlett — the 2-way LR24 front at 3 kHz, Center, Side L/R, Rear L/R and
both sub roles behind the 80 Hz bass-management split. Switching **Mode**
to Surround lands on that configured system: the routing grid offers the
Surround roles, the A/B card opens in its Easy view on the Global speaker
group pickers, and its **Expert** switch shows the bank selector — Global,
the way pairs (Front L/R once Crossover is off), Center, Side L/R, Rear L/R
and Sub 1 / Sub 2 — with the seeded **Ind. Banks** selected; the mode hint
names the LFE target, the Subwoofer card switches to the speaker groups
behind the **Link groups** switch, and Speaker Auto Alignment stays hidden
because Surround does not offer it. The mode state is held per mode, so exploring Surround never
touches the stereo-sub configuration; a half-routed Side or Rear pair and
Sub 2 without Sub 1 are saved but reported as inactive, exactly like the
backend.

`demo/data/alignment.js` holds the Speaker Auto Alignment fixture: the last
real alignment run on the test machine, with its `Before (planning)` and
`After (verification)` takes. 20 Hz to 1 kHz is the measurement verbatim,
and the blend region up to 2 kHz keeps the measured crossover dip, so the
trace still reads as a real 2-way. Above that the raw capture describes the
test rig rather than a speaker — a broad +8/+10 dB hump at 4.8 kHz and sharp
peaks and dips over 2–9 kHz that no delay-and-gain correction could have
caused — so both takes follow one tamed top end: a small presence rise easing
into a natural, slightly falling top end. The stored curves carry that shape
on its own; `topEndRipple()` adds the shallow structure a real 1/6-octave
sweep keeps on top of it (a broad rise around 2.7 kHz, a dip at 5.8 kHz, a
peak near 4.3 kHz, a few tenth-of-a-decibel wiggles up top), never more than
±0.55 dB off the shape and faded in over 2.05–2.7 kHz so the join with the
measured blend region stays smooth. The rig's own comb structure stays out.
Both takes get the same ripple, so the run's +1.55 dB before/after distance
is untouched by it. Committing a run really moves the way trim in the
Crossover card.

Each take also carries the run's **timing timeline**: its full-band IR and
every isolated way band on one time base anchored on the reference way, the
way the alignment leaves undelayed. So the Before lane shows every way at
minus its planned delay and the After lane at its residual — exactly what the
proposal and the verification report — and the IR view draws the pair as one
lane per take on a shared millisecond axis. A saved take keeps its timeline
too, so it comes back as a lane instead of a 0 ms preview. The slices follow
`measurement/speaker_verification.py`: one window from the earliest arrival
− 2 ms to the latest + 4 ms, on the take's own sample origin, each normalized
to its own peak inside that window. The way bands are damped sinusoids per
role (the low way of the seeded LR24 rings slowly, the high way within a
fraction of a millisecond), so the fixture stays reproducible.

The run's delay and gain are **derived**, not hand-written: the fixture
stores only the per-way band arrivals in samples and the per-way passband
levels, and the build computes `added_delay = max(arrival) - arrival` and
`added_gain = median(levels) - level` from them, exactly like
`measurement/speaker_align.py`. Arrivals are earliest-way-origin in whole
48 kHz samples, so one way always lands on 0.0 ms and a two-way crossover
over centimetres of path difference arrives within about one lobe width —
0.375 ms gross, corrected to one sample of residual, with isolation margins
near 15 dB rather than a comfortable 20. The build refuses to emit a run the
real backend would have rejected.

Next to the 2-way run, the fixture carries the last real saved 3-way run
on the test machine (right side, 2026-09-27; verified down to a 0.021 ms
residual). Its low/mid/high values 0.417/0.0/0.604 ms are relative band
arrivals from the earliest way (`arrival_ms`), not absolute delays; the
planned added delays derived from them are 0.188/0.604/0.0 ms. The run's
frequency traces, timing timelines, IR previews and analysis blocks are kept
verbatim, including the backend's unmeasured (null) verification isolation. Left
mirrors the right run's data, the same precedent as the 2-way shared
curves. The demo serves the 2-way run for a 2-way topology and the 3-way
run once the routed topology has three ways per side (re-route the outputs
to Low/Mid/High and give the mid ways their highpass); both pairs ship as
saved runs, and a commit moves the way trim of the side that ran.

## Uploads

Two upload surfaces drive the simulated backend with a real file body.

**Presets and IRs.** The create/import endpoints read the upload the browser
sent, so a preset is named and shaped the way `DSPManager` names it: the
uploaded file names it (`Measured Front LR.json` becomes `Measured Front
LR`, a form field never wins), a bundle keeps the name of its preset member
and falls back to the archive name for a `preset.json`, and a `.irs`/`.wav`
upload creates the convolver preset its `preset_name` names. The content is
validated first: invalid JSON, a JSON that is neither a native nor an
EasyEffects output preset, a file that is not a ZIP, a missing or empty IR,
an unsupported dual-import file and a malformed REW line all fail with the
backend's message, and the imported filters reach the preset entry the UI
renders (native and legacy EasyEffects equalizer chains, the REW PK/PEQ
parser, dual PEQ or dual convolver).

The bank binding is resolved **before** anything is written
(`dsp.api._resolve_bank_binding`): a client whose cached output-state
revision is stale is refused with the backend's 409
(`bank-assign-conflict`) and no preset exists afterwards. That is the one
place where the demo adds client bookkeeping of its own: after a committed
assignment the demo layer asks `FXRouteOutputSystemController` to refresh the
output state, the call the product itself makes after a convolver creation
but not after the JSON/bundle imports — without it the page's cached
revision would stay one behind and the next import would be refused as
stale.

**Library files.** The library upload of the product frontend posts through
`XMLHttpRequest`, not `fetch`, so `demo/xhr.js` replaces `window.XMLHttpRequest`
with a shim that routes every `/api/` request through the same simulated API
(and reports upload progress the way a real upload does); anything else falls
back to the real object. `POST /api/library/upload` answers like
`library/api.py`: an audio file lands in the active catalog's `Uploads`
folder, a `.zip` as its own album folder, a playlist file imports the library
tracks it names (and is refused when none match), a duplicate name is a 409,
an unsupported type a 400 — each with the backend's `status`/`kind`/
`message`/`track_count` fields so the UI reports the real wording.

The demo reads ZIP uploads only far enough to name them: signature plus the
first local entry of a bundle, no extraction.

## Auto Sub job discovery

The backend keeps its Auto Sub jobs in a process-wide map, so a reloaded page
asks `GET /api/measurements/auto-sub-optimize/current` and the measurement
panel reattaches the newest live or briefly retained run. The demo answers
with the same shape, including the 10 minute retention after a run finished
and the cancelled state, so a reloaded page reattaches its run instead of
losing it.

That needs the one thing the demo otherwise has none of: the job has to
survive the reload. The newest job is parked in `sessionStorage` (mode,
target key, start time, status); everything else stays in memory, and a fresh
session starts with no run at all.

## How the demo relates to the frontend

The product UI lives in this checkout (`FRONTEND_ROOT`, default: this
worktree). Both the live server and the build lift the UI from there, so
current frontend changes appear in the demo automatically. Set
`FXROUTE_FRONTEND_ROOT` to serve a different checkout.

- `scripts/serve_demo.py` — live development server (port 8765). Serves the
  frontend's `static/` directly and injects the demo transport layer on the
  fly (always current, nothing copied).
- `scripts/build_demo.py` — produces the self-contained static snapshot in
  `demo/dist/` (mirrors the whole `static/` tree automatically + the demo
  layer + the demo artwork pool). This snapshot is the commit-able,
  publishable build. New product JS/CSS/font/image assets land in the
  snapshot without touching the build script. The only deliberate
  exclusion (`STATIC_EXCLUDE` in `build_demo.py`): the page shell
  `index.html`, which is transformed and written separately.
- `scripts/test_demo_behavior.js` — Node behavior test asserting the demo
  contract (simulation intercepts the backend, power menu is simulated,
  transport/VU/radio/measurement render, current frontend wording).

The simulation layer:

- `demo/state.js`, `routes.js`, `xhr.js`, `streaming.js`, `ws.js`,
  `boot.js`, `demo.css` — the fake-backend/state/interceptor (and the
  XMLHttpRequest shim the library upload needs).
- `demo/data/` — fixtures (library, radio, measurements).
- `static/demo/` — demo-owned artwork pool.

## Build / serve / test

```sh
# Live development server (frontend pulled live from this checkout, no copy)
python3 scripts/serve_demo.py          # http://127.0.0.1:8765

# Reproducible public build into demo/dist/ (domain-root hosting)
python3 scripts/build_demo.py

# Reproducible subpath build, e.g. a GitHub Pages project site at /fxroute/
python3 scripts/build_demo.py --base-path /fxroute/ --out /tmp/publish

# Behavior contract test (reads demo/dist, so build first)
node scripts/test_demo_behavior.js
```

The build is reproducible: running it twice against the same frontend
checkout yields identical output. `demo/dist/` is committed so there is a
known publishable snapshot; it is generated output, not a second UI source.

`FXROUTE_FRONTEND_ROOT` overrides the frontend checkout path if the demo
needs to build against a different checkout.

### Base paths

`--base-path` defaults to `/` (domain-root hosting): the built page uses
absolute `/static/...` URLs. For subpath hosting (e.g. a GitHub Pages
project site under `/fxroute/`), pass `--base-path /fxroute/`: the build
prefixes every `/static/` reference in the page, the copied JS/CSS and the
web manifest with the base path, while the demo layer keeps its relative
`./demo/` references (they resolve under the subpath). `--out` selects the
output directory (default `demo/dist`) so a subpath build never overwrites
the committed root snapshot.
