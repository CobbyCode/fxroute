#!/usr/bin/env python3
"""Playwright geometry contract for the TIDAL browse header.

The TIDAL toolbar must follow the established one-row header pattern
(Library/Radio/DSP): provider title left, search group right, vertically
aligned; the ``Tidal · Connected`` status small and right-aligned below on
the same content edge.  On the <=760px breakpoint the row stacks like the
other tab headers instead of overflowing.

Runs the real rendered page (static server + stubbed streaming API) in
headless Chromium.  Skips cleanly when playwright or a browser is not
available on the host.
"""

import http.server
import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8197

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_tidal_header_geometry.py (playwright not installed)")
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
    if (u.includes('/api/streaming/tidal/')) return json({});
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    return realFetch(url, opts);
};
"""


def _center(box):
    return box["y"] + box["height"] / 2


def _right_edge(box):
    return box["x"] + box["width"]


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
            # Only static/index.html is served in production; the repository-
            # root index.html is stale and does not load the streaming module.
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_function("typeof window.FXRouteStreaming === 'object'", timeout=15000)
            page.wait_for_timeout(300)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: true, capabilities: { transport: false } });"
            )
            page.wait_for_timeout(300)

            def activate():
                page.evaluate("document.querySelector('.tab-btn[data-tab=\"tidal\"]').click()")
                page.wait_for_timeout(400)

            # Desktop/tablet widths: title + search on one row, status shares
            # the search right edge, no overflow, footer intact.
            for width in (1440, 1024, 834):
                page.set_viewport_size({"width": width, "height": 800})
                page.wait_for_timeout(200)
                activate()

                title_box = page.locator(".tidal-toolbar-title").bounding_box()
                search_box = page.locator(".tidal-toolbar .streaming-search").bounding_box()
                status_box = page.locator(".tidal-toolbar > .streaming-status-line").bounding_box()
                title_text = page.locator(".tidal-toolbar-title").inner_text().strip()
                status_text = page.locator(".tidal-toolbar > .streaming-status-line").inner_text()

                check(f"[{width}px] Tidal title text is 'Tidal'", title_text == "Tidal")
                check(f"[{width}px] title and search are vertically aligned",
                      abs(_center(title_box) - _center(search_box)) <= 2)
                check(f"[{width}px] status right edge matches search right edge",
                      abs(_right_edge(status_box) - _right_edge(search_box)) <= 1.5)
                check(f"[{width}px] status shows provider + connected", "Connected" in status_text)
                check(f"[{width}px] no horizontal overflow",
                      page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
                check(f"[{width}px] footer present", page.locator("#playback-bar").count() == 1)

            # Mobile: the title stacks above the search row instead of
            # overflowing; status stays on the same right edge.
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            activate()

            title_box = page.locator(".tidal-toolbar-title").bounding_box()
            search_box = page.locator(".tidal-toolbar .streaming-search").bounding_box()
            status_box = page.locator(".tidal-toolbar > .streaming-status-line").bounding_box()

            check("[390px] mobile stacks the title above the search row",
                  _center(search_box) - _center(title_box) > 8)
            check("[390px] status right edge matches search right edge on mobile",
                  abs(_right_edge(status_box) - _right_edge(search_box)) <= 1.5)
            check("[390px] no horizontal overflow on mobile",
                  page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            check("[390px] footer present on mobile", page.locator("#playback-bar").count() == 1)

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_tidal_header_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_tidal_header_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_tidal_header_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
