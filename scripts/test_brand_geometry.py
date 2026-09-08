#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the brand mark size.

Approved route-mark branding: the mark is a fixed 32x32px SVG at every
viewport (proposal composition). Earlier responsive scaling (35/38/37px
ladder) belonged to the retired monogram branding and must not return.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8201

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_brand_geometry.py (playwright not installed)")
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
            page.wait_for_selector(".brand-mark", state="visible")
            page.wait_for_timeout(300)

            mark = page.locator(".brand-mark")
            check("brand-mark exists", mark.count() >= 1)

            # Approved route-mark branding: fixed 32x32 at every width.
            for width in (1440, 900, 761, 760, 700, 601, 600, 599, 520, 500, 390, 360, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                bb = mark.first.bounding_box()
                check(f"[{width}px] brand-mark has box", bb is not None)
                assert bb is not None
                check(
                    f"[{width}px] brand-mark is 32x32 (w={bb['width']:.2f} h={bb['height']:.2f})",
                    abs(bb["width"] - 32) <= 1 and abs(bb["height"] - 32) <= 1,
                )

            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_brand_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_brand_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_brand_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
