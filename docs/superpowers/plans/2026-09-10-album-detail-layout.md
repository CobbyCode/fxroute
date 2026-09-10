# Album detail layout implementation plan

**Goal:** Make Library and TIDAL album headers compact, consistently arranged, and readable without expanding the artist description.

**Architecture:** Keep the existing artwork-led visual language and shared metadata/about renderers. An album-specific header modifier controls the tighter composition without changing artist-page layout. Provider data remains provider-owned; optional fields disappear naturally.

**Tech stack:** Existing HTML, CSS and vanilla JavaScript; Python demo server; Node checks.

**Spec:** User brief in this session (2026-09-10): group metadata, show artist text directly, avoid expansion-driven resizing, accommodate different provider fields.

## Implementation

- [x] Update `static/app.js`: combine track count and release information in the live count renderer (including filtered counts); combine label and genres; render escaped About text as a directly visible, labelled section.
- [x] Update `static/streaming.js`: opt albums into the same compact header; combine track count/type/year/country/quality into a summary and label/genres into a second row; retain provider precedence and asynchronous request guards.
- [x] Update `static/index.html`, `static/css/_library.css`, and `static/css/_responsive.css`; regenerate `static/style.css` with `bash scripts/build-css.sh`: album-only modifier, smaller bounded cover, aligned metadata, back action without a wasted full-height column, subtle description divider; explicit phone layout and wrapping.
- [x] Update existing UI assertions that describe the old accordion/layout. Run `node --check` on both JavaScript files and the detail/enrichment/artwork UI checks.
- [x] Inspect real demo views at desktop, tablet, and phone widths, including absent metadata, long text, filtered local tracks, favorites and Back. Verified at 1440, 768, 601 and 390 pixels in the live demo, plus escaped descriptions and empty rows. CSS build and `python3 scripts/test_style_contract.py` pass.
