#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the subwoofer controls columns.

At max-width:760px .effects-subwoofer-controls is intentionally reduced to a
single column; a later max-width:1100px block used to override it back to two
columns at 600/700px. The 1100px tablet rule is now scoped to 761-1100px, so:

  ≤760px -> 1 column, 761-1100px -> 2 columns, >1100px -> base (8 tracks).

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8202

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_subwoofer_controls_geometry.py (playwright not installed)")
    sys.exit(0)


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


STUB = """
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    if (u.includes('/api/audio/samplerate')) {
        return json({ available: true, active_rate: 44100, supported_rates: [44100, 48000] });
    }
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    return realFetch(url, opts);
};
"""

SHOW_JS = """
(() => {
    switchTab('effects');
    const card = document.querySelector('.effects-card-subwoofer');
    if (card) card.classList.remove('hidden');
})();
"""


def _run():
    server = _serve()
    passed = 0

    def check(name, condition):
        assert condition, name
        nonlocal passed
        passed += 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(SHOW_JS)
            page.wait_for_timeout(300)

            controls = page.locator(".effects-subwoofer-controls")
            check("subwoofer controls exist", controls.count() >= 1)

            def column_count():
                return page.evaluate(
                    "getComputedStyle(document.querySelector('.effects-subwoofer-controls'))"
                    ".gridTemplateColumns.trim().split(/\\s+/).length"
                )

            # Mobile ≤760px: exactly one column.
            for width in (760, 700, 600, 520):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                n = column_count()
                check(f"[{width}px] subwoofer controls 1 column (got {n})", n == 1)

            # Tablet 761-1100px: two columns unchanged.
            for width in (761, 900, 1100):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                n = column_count()
                check(f"[{width}px] subwoofer controls 2 columns (got {n})", n == 2)

            # Desktop >1100px: base multi-track layout unchanged.
            page.set_viewport_size({"width": 1200, "height": 900})
            page.wait_for_timeout(80)
            n = column_count()
            check(f"[1200px] subwoofer controls 8 columns (got {n})", n == 8)

            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_subwoofer_controls_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_subwoofer_controls_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_subwoofer_controls_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
