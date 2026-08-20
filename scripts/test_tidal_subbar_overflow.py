#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the TIDAL subbar at small phone widths.

.tidal-subbar .view-tabs (Tracks/Albums/Artists/Playlists/Search chips) used
flex: 0 0 auto, so the navigation could not shrink inside the wrapping
subbar: at 320px it rendered 337px wide inside a 288px container and pushed
the document 33px past the viewport. It now shrinks (flex: 0 1 auto,
min-width: 0) so the chips wrap.

Contract: no horizontal document overflow at any tested width while the
TIDAL tab is active. Skips cleanly when playwright is unavailable.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8204

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_tidal_subbar_overflow.py (playwright not installed)")
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
    if (u.includes('/api/streaming/providers')) {
        return json({ providers: [
            { id: 'spotify', name: 'Spotify', installed: false, available: false, capabilities: {} },
            { id: 'qobuz', name: 'Qobuz', installed: false, available: false, capabilities: {} },
            { id: 'tidal', name: 'Tidal', installed: true, available: true, capabilities: { transport: false } },
        ] });
    }
    if (u.includes('/api/streaming/tidal/status')) {
        return json({ installed: true, available: true, authenticated: true, capabilities: { transport: false } });
    }
    if (u.includes('/api/streaming/tidal/search?q=')) {
        return json({ tracks: [], artists: [], albums: [], playlists: [] });
    }
    if (u.includes('/api/streaming/tidal/favorites/ids')) {
        return json({ tracks: [], albums: [], artists: [], playlists: [] });
    }
    if (u.includes('/api/streaming/tidal/')) return json({});
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
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_function("typeof window.FXRouteStreaming === 'object'", timeout=15000)
            page.wait_for_timeout(300)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: true, capabilities: { transport: false } });"
            )
            page.wait_for_timeout(300)
            page.evaluate("document.querySelector('.tab-btn[data-tab=\\\"tidal\\\"]').click()")
            page.wait_for_timeout(400)

            # Subbar must exist and the navigation must be renderable.
            tabs_box = page.locator(".tidal-subbar .view-tabs").bounding_box()
            check("tidal subbar navigation has box", tabs_box is not None)
            assert tabs_box is not None

            # All widths from small phone to desktop: no horizontal overflow
            # while the TIDAL tab is active, and the subbar stays in the viewport.
            for width in (320, 360, 390, 520, 600, 700, 760, 900, 1440):
                page.set_viewport_size({"width": width, "height": 844})
                page.wait_for_timeout(200)
                overflow = page.evaluate(
                    "document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                check(f"[{width}px] no horizontal overflow (got {overflow}px)", overflow <= 1)

                box = page.locator(".tidal-subbar .view-tabs").bounding_box()
                check(f"[{width}px] subbar navigation has box", box is not None)
                assert box is not None
                check(
                    f"[{width}px] subbar navigation right edge inside viewport "
                    f"(right={box['x'] + box['width']:.1f})",
                    box["x"] + box["width"] <= width + 1,
                )

            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_tidal_subbar_overflow.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_tidal_subbar_overflow.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_tidal_subbar_overflow.py")
        print(f"  {exc}")
        sys.exit(1)
