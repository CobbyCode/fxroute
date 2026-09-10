# FXRoute Web-Demo

A static, simulated frontend demo of FXRoute. It reuses the real product
frontend from the same checkout — there is **no second UI source** — and
adds only a demo/simulation layer on top.

## What is in this worktree

The demo layer lives in the same checkout as the product frontend:

- `demo/` — simulation source (`state.js`, `routes.js`,
  `streaming.js`, `ws.js`, `boot.js`, `demo.css`), fixtures
  (`data/`), `README.md` and the built `dist/` snapshot.
- `static/demo/` — demo-owned artwork pool (not part of the product UI).
- `scripts/build_demo.py`, `scripts/serve_demo.py`,
  `scripts/test_demo_behavior.js` — the whole toolchain.

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

- `demo/state.js`, `routes.js`, `streaming.js`, `ws.js`, `boot.js`,
  `demo.css` — the fake-backend/state/interceptor.
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
