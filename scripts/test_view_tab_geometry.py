#!/usr/bin/env python3
"""Playwright geometry contract for the shared compact view tabs.

Library ``Albums / Tracks / Folders / Favorites`` and the TIDAL browse navigation
``Albums / Tracks / Artists / Playlists`` (one shared row) plus the search
result types must render through one shared ``.view-tab`` component: equal
visible height, equal horizontal padding and one identical mint active
state.  The old ``Favorites`` over-tab and its inner category chips are
gone — no second navigation level may remain.  Nothing may overflow or
shrink the touch targets at any checked width.

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
PORT = 8196

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_view_tab_geometry.py (playwright not installed)")
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
    if (u.includes('/api/streaming/tidal/search?q=')) return json({ tracks: [], artists: [], albums: [], playlists: [] });
    if (u.includes('/api/streaming/tidal/favorites/ids')) return json({ tracks: [], albums: [], artists: [], playlists: [] });
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
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(STUB)
            # Only static/index.html is served in production (the canonical
            # shell that loads the streaming module).
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_function("typeof window.FXRouteStreaming === 'object'", timeout=15000)
            page.wait_for_timeout(300)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: true, capabilities: { transport: false } });"
            )
            page.wait_for_timeout(400)

            def activate(tab):
                page.evaluate(f"document.querySelector('.tab-btn[data-tab=\\\"{tab}\\\"]').click()")
                page.wait_for_timeout(400)

            def heights(sel):
                return [el.bounding_box()["height"] for el in page.locator(sel).all()]

            def pads(sel):
                return page.evaluate(
                    "Array.from(document.querySelectorAll('" + sel + "')).map("
                    "(el) => { const s = getComputedStyle(el); return [parseFloat(s.paddingLeft), parseFloat(s.paddingRight)]; })"
                )

            def bg(sel):
                return page.evaluate("getComputedStyle(document.querySelector('" + sel + "')).backgroundColor")

            for width in (1440, 1024, 834):
                page.set_viewport_size({"width": width, "height": 800})
                page.wait_for_timeout(200)
                # Library view tabs.
                activate("library")
                lib_heights = heights("#tab-library .view-tab")
                check(f"[{width}px] library renders four view tabs", len(lib_heights) == 4)
                check(f"[{width}px] library view tabs share one height",
                      max(lib_heights) - min(lib_heights) <= 0.5)
                check(f"[{width}px] library view tabs keep a touchable height (>=36px)",
                      min(lib_heights) >= 36)
                lib_pads = pads("#tab-library .view-tab")
                check(f"[{width}px] library view tabs share horizontal padding",
                      len({tuple(p) for p in lib_pads}) == 1)

                # TIDAL browse navigation: one shared row of four view tabs.
                activate("tidal")
                browse_heights = heights(".tidal-subbar .view-tab[data-browse]")
                check(f"[{width}px] TIDAL browse renders four view tabs", len(browse_heights) == 4)
                check(f"[{width}px] TIDAL browse tabs share one height",
                      max(browse_heights) - min(browse_heights) <= 0.5)
                check(f"[{width}px] TIDAL browse tabs keep a touchable height (>=36px)",
                      min(browse_heights) >= 36)
                browse_pads = pads(".tidal-subbar .view-tab[data-browse]")
                check(f"[{width}px] TIDAL browse tabs share horizontal padding",
                      len({tuple(p) for p in browse_pads}) == 1)

                # The browse tabs render in one navigation row: the old
                # Favorites level and its inner category chips are gone.
                check(f"[{width}px] no second navigation row (#tidal-fav-types gone)",
                      page.locator("#tidal-fav-types").count() == 0)
                check(f"[{width}px] subbar carries exactly one view-tab group",
                      page.locator(".tidal-subbar .view-tabs").count() == 1)

                # Library and TIDAL browse tabs are the same component.
                check(f"[{width}px] library and TIDAL browse tabs are equally tall",
                      abs(min(lib_heights) - min(browse_heights)) <= 0.5)
                check(f"[{width}px] library and TIDAL browse tabs are equally padded",
                      tuple(lib_pads[0]) == tuple(browse_pads[0]))

                # Identical mint active state (default browse category Albums).
                lib_active = bg("#library-view-albums")
                browse_active = bg(".tidal-subbar .view-tab[data-browse].is-active")
                check(f"[{width}px] library and TIDAL active states match ({lib_active})",
                      lib_active == browse_active)

                # Search result type tabs use the same component.
                page.fill("#tidal-search-input", "daft punk")
                page.keyboard.press("Enter")
                page.wait_for_timeout(400)
                search_heights = heights("#tidal-search-result-types .view-tab")
                check(f"[{width}px] search result types render as view tabs", len(search_heights) == 4)
                check(f"[{width}px] search type tabs match the shared height",
                      abs(min(search_heights) - min(browse_heights)) <= 0.5)

                check(f"[{width}px] no horizontal overflow",
                      page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
                # Clear the executed search so the next width starts on Albums.
                page.fill("#tidal-search-input", "")
                page.wait_for_timeout(200)

            # Mobile: everything wraps/scrolls cleanly, no overflow, targets intact.
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            activate("library")
            lib_heights = heights("#tab-library .view-tab")
            check("[390px] library view tabs stay touchable on mobile", min(lib_heights) >= 36)
            check("[390px] library no horizontal overflow",
                  page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            activate("tidal")
            browse_heights = heights(".tidal-subbar .view-tab[data-browse]")
            check("[390px] TIDAL browse tabs stay touchable on mobile", min(browse_heights) >= 36)
            check("[390px] TIDAL browse navigation has no second level",
                  page.locator("#tidal-fav-types").count() == 0)
            check("[390px] TIDAL no horizontal overflow",
                  page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_view_tab_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_view_tab_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_view_tab_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
