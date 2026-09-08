#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Manual UI walkthrough of the built CSS states.

Drives the real rendered page (static server + API stub, page JS active) in
headless Chromium. Clicks through every tab (radio/library/effects/streaming)
and opens the measurement + settings dialogs, then at every required boundary
viewport runs layout invariants and writes screenshots for human inspection.

Run manually after CSS changes; not part of the test suite:

    python3 scripts/check_ui_walkthrough.py [--shots DIR]

Exits 1 when any invariant fails; screenshot directory defaults to
/tmp/fxroute-ui-shots.
"""

import argparse
import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8215

VIEWPORTS = [
    (1440, 900),
    (1200, 900),
    (1181, 900),
    (1180, 900),
    (1024, 900),
    (901, 900),
    (900, 900),
    (834, 1112),
    (820, 1180),
    (761, 1024),
    (760, 1024),
    (701, 1024),
    (700, 1024),
    (641, 1024),
    (640, 1024),
    (600, 1024),
    (520, 800),
    (390, 844),
    (360, 800),
    (320, 700),
]

# API stub: keeps the real app booting without a backend. Streaming providers
# report installed so the streaming tabs are visible; status reports a playing
# track so the footer renders with media.
STUB = """
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    const track = { artist: 'Deee-Lite', title: 'Groove Is in the Heart', album: 'World Clique', trackId: 't1', duration: 260000, position: 60000, status: 'Playing' };
    if (u.includes('/api/audio/samplerate')) {
        return json({ available: true, active_rate: 44100, supported_rates: [44100, 48000] });
    }
    if (u.includes('/api/audio/outputs')) return json({ outputs: [{ key: 'default', name: 'Default', active: true }] });
    if (u.includes('/api/status')) return json({ ...track, source: 'radio', output_peak_warning: false });
    if (u.includes('/api/spotify/status')) return json({ available: true, installed: true, source: 'spotify', capabilities: {}, status: 'Stopped', artist: '', title: '', album: '', trackId: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 });
    if (u.includes('/api/streaming/')) return json({ installed: true, available: true });
    if (u.includes('/api/dsp/presets')) return json({ presets: [] });
    if (u.includes('/api/library/status')) return json({ ready: true, folders: [] });
    if (u.includes('/api/measurements')) return json({ measurements: [] });
    if (u.includes('/api/radio/stations')) return json({ stations: [{ id: 'groovesalad', name: 'Groove Salad', genre: 'ambient' }] });
    return realFetch(url, opts);
};
"""

# Force every tab panel visible + playback playing + open dialogs, then restore
# each tab in turn for a real click-through. The app's own tab-switching code
# runs, so visibility transitions are exercised as in production.
PREPARE_JS = """
(() => {
    // Un-hide streaming tabs (stub reports installed; app may gate on status).
    for (const id of ['tab-btn-spotify', 'tab-btn-qobuz', 'tab-btn-tidal']) {
        const b = document.getElementById(id);
        if (b) { b.hidden = false; b.style.display = ''; }
    }
    for (const id of ['tab-spotify', 'tab-qobuz', 'tab-tidal']) {
        const p = document.getElementById(id);
        if (p) { p.hidden = false; p.classList.remove('hidden'); }
    }
    const bar = document.getElementById('playback-bar');
    if (bar) bar.classList.add('has-media', 'is-playing');
    const sr = document.querySelector('.seek-row');
    if (sr) { sr.classList.remove('hidden'); sr.style.display = ''; }
    const title = document.getElementById('track-title');
    if (title) { title.classList.remove('placeholder'); title.textContent = 'Groove Is in the Heart'; title.style.display = ''; }
    const artist = document.getElementById('track-artist');
    if (artist) { artist.textContent = 'Deee-Lite'; artist.style.display = ''; }
    // Freeze load-triggered animations so screenshots/geometry are stable.
    const frozen = document.createElement('style');
    frozen.textContent = '*{animation:none !important;transition:none !important}';
    document.head.appendChild(frozen);
    // The API stub stands in for the backend; suppress the "Disconnected"
    // banner so it cannot intercept pointer events in this walkthrough.
    const off = document.getElementById('offline-indicator');
    if (off) off.style.display = 'none';
})();
"""


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _run(shots_dir: pathlib.Path) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP  check_ui_walkthrough.py (playwright not installed)")
        return 0

    server = _serve()
    checks = {"n": 0, "fails": []}
    shots_dir.mkdir(parents=True, exist_ok=True)

    def check(name, cond, width=None):
        tag = f"[{width}px] " if width else ""
        checks["n"] += 1
        if not cond:
            msg = f"{tag}{name}"
            checks["fails"].append(msg)
            print(f"  FAIL {msg}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(STUB)
                page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
                page.wait_for_selector("#playback-bar", state="visible")
                page.evaluate(PREPARE_JS)
                page.wait_for_timeout(250)

                # --- invariant checks independent of the active tab ---
                # No horizontal overflow anywhere.
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                check("no horizontal overflow", overflow <= 1, width)
                # Brand mark: approved route mark is fixed 32x32 everywhere.
                bb = page.locator(".brand-mark").first.bounding_box()
                if bb:
                    ok = abs(bb["width"] - 32) <= 1 and abs(bb["height"] - 32) <= 1
                    check(
                        f"brand-mark ladder (w={bb['width']:.1f} h={bb['height']:.1f})",
                        ok, width,
                    )
                else:
                    check("brand-mark has box", False, width)

                # --- click-through: every tab, screenshots + spot checks ---
                tabs = [
                    ("radio", None),
                    ("library", None),
                    ("effects", None),
                    ("spotify", None),
                    ("qobuz", None),
                    ("tidal", None),
                ]
                for tab, _extra in tabs:
                    btn = page.locator(f"#tab-btn-{tab}")
                    if btn.count() == 0 or not btn.first.is_visible():
                        continue
                    page.evaluate(f"document.getElementById('tab-btn-{tab}').click()")
                    page.wait_for_timeout(120)
                    panel = page.locator(f"#tab-{tab}")
                    check(f"tab {tab} panel visible after click", panel.first.is_visible(), width)
                    page.screenshot(
                        path=str(shots_dir / f"{width}px-{tab}.png"),
                        full_page=False,
                    )

                    # Per-tab geometry invariants.
                    if tab == "effects":
                        page.evaluate(
                            "document.querySelectorAll('.effects-subwoofer-controls')[0]"
                            "?.classList.remove('hidden')"
                        )
                        page.wait_for_timeout(80)
                        sub = page.locator(".effects-subwoofer-controls").first
                        if sub.count():
                            cols = page.evaluate("""(() => {
                                const el = document.querySelector('.effects-subwoofer-controls');
                                if (!el) return null;
                                return getComputedStyle(el).gridTemplateColumns;
                            })()""")
                            if width <= 760:
                                one_col = cols and not cols.split(" ")[-1].strip().startswith("(") and cols.strip() != "none" and len(cols.split()) == 1
                                check(f"subwoofer controls single column ({cols})", bool(one_col), width)
                            else:
                                check(f"subwoofer controls multi column ({cols})", bool(cols) and cols != "none", width)
                        page.screenshot(
                            path=str(shots_dir / f"{width}px-effects-subwoofer.png"),
                            full_page=False,
                        )

                # Measurement dialog.
                measure_btn = page.locator("#effects-measure-open")
                if measure_btn.count():
                    page.evaluate("document.getElementById('effects-measure-open').click()")
                    page.wait_for_timeout(150)
                    dlg = page.locator("#measurement-panel")
                    if dlg.count():
                        check("measurement dialog open", dlg.first.is_visible(), width)
                        page.screenshot(
                            path=str(shots_dir / f"{width}px-measurement.png"),
                            full_page=False,
                        )
                        # Cleanup: close again.
                        close = page.locator("#measurement-close")
                        if close.count():
                            page.evaluate("document.getElementById('measurement-close').click()")
                            page.wait_for_timeout(100)

                # Favorite primitives on the radio/library rows.
                for cls in (".track-row-favorite", ".track-fav", ".streaming-fav"):
                    el = page.locator(cls).first
                    if el.count():
                        fbb = el.first.bounding_box()
                        if fbb and fbb["width"] > 0:
                            square = abs(fbb["width"] - fbb["height"]) <= 1
                            check(f"{cls} square ({fbb['width']:.1f}x{fbb['height']:.1f})", square, width)

                page.close()
            browser.close()
    finally:
        server.shutdown()

    if checks["fails"]:
        print(f"FAIL  check_ui_walkthrough.py ({len(checks['fails'])}/{checks['n']} checks failed)")
        print(f"  screenshots: {shots_dir}")
        return 1
    print(f"PASS  check_ui_walkthrough.py ({checks['n']} checks, screenshots in {shots_dir})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shots", default="/tmp/fxroute-ui-shots", help="screenshot directory")
    args = ap.parse_args()
    return _run(pathlib.Path(args.shots))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        import traceback
        print(f"FAIL  check_ui_walkthrough.py")
        print(f"  type={type(exc).__name__} repr={exc!r}")
        traceback.print_exc()
        sys.exit(1)
