#!/usr/bin/env python3
"""Playwright geometry contract for the TIDAL browse header.

The TIDAL header follows the library pattern: row 1 is the page title with
the shared ``Connected`` pill + refresh button on the right, row 2 is the
single Tracks/Albums/Artists/Playlists navigation with the search group on
the right.  Both right-aligned groups share one right edge with the detail
Back button, so browse <-> detail never shifts horizontally.  On the
<=760px breakpoint the second row wraps (search below the navigation)
without overflowing.

Also verifies the refresh interaction: clicking the refresh button re-fetches
the authoritative favorites ids and the provider status without logging out,
and the browse surface (and its search) keeps working.

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
window.__tidalFetches = [];
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    window.__tidalFetches.push(u);
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
            # Only static/index.html is served in production (the canonical
            # shell that loads the streaming module).
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_function("typeof window.FXRouteStreaming === 'object'", timeout=15000)
            page.wait_for_timeout(300)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: true, capabilities: { transport: false } });"
            )
            page.wait_for_timeout(300)

            def activate():
                page.evaluate("document.querySelector('.tab-btn[data-tab=\\\"tidal\\\"]').click()")
                page.wait_for_timeout(400)

            # Desktop/tablet widths: title + Connected/refresh on row 1,
            # navigation + search on row 2, shared right edge, no overflow.
            for width in (1440, 1024, 834, 768):
                page.set_viewport_size({"width": width, "height": 800})
                page.wait_for_timeout(200)
                activate()

                title_box = page.locator(".tidal-toolbar-title").bounding_box()
                actions_box = page.locator(".tidal-toolbar-actions").bounding_box()
                status_box = page.locator(".tidal-toolbar-actions .streaming-status").bounding_box()
                tabs_box = page.locator(".tidal-subbar .view-tabs").bounding_box()
                search_box = page.locator(".tidal-subbar .streaming-search").bounding_box()
                title_text = page.locator(".tidal-toolbar-title").inner_text().strip()
                status_text = page.locator(".tidal-toolbar-actions .streaming-status").inner_text()
                placeholder = page.locator("#tidal-search-input").get_attribute("placeholder")

                check(f"[{width}px] Tidal title text is 'Tidal'", title_text == "Tidal")
                check(f"[{width}px] title and actions share one header row",
                      abs(_center(title_box) - _center(actions_box)) <= 2)
                check(f"[{width}px] status shows the shared Connected label", status_text == "Connected")
                check(f"[{width}px] refresh button sits in the actions group",
                      page.locator("#tidal-refresh-btn").is_visible())
                check(f"[{width}px] actions right edge matches search right edge",
                      abs(_right_edge(actions_box) - _right_edge(search_box)) <= 1.5)
                check(f"[{width}px] navigation and search share one second row",
                      abs(_center(tabs_box) - _center(search_box)) <= 2)
                check(f"[{width}px] search has the full placeholder",
                      placeholder == "Search albums, tracks, artists, playlists…")
                if width <= 1024:
                    check(f"[{width}px] tablet search is compact",
                          search_box["width"] < 430)
                check(f"[{width}px] single navigation row (no Favorites level)",
                      page.locator("#tidal-fav-types").count() == 0)
                check(f"[{width}px] second row sits below the title row",
                      _center(tabs_box) - _center(title_box) > 4)
                check(f"[{width}px] no horizontal overflow",
                      page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
                check(f"[{width}px] footer present", page.locator("#playback-bar").count() == 1)

            # Library <-> TIDAL cross-view alignment: switching tabs must
            # not move the shared header rhythm (gap, top, toggle center
            # and right edge), so neither edges nor controls visibly jump.
            page.evaluate("document.querySelector('.tab-btn[data-tab=\"library\"]').click()")
            page.wait_for_timeout(400)
            lib = page.evaluate("""(() => {
                const gapOf = (rs, ts) => {
                    const r = document.querySelector(rs).getBoundingClientRect();
                    const t = document.querySelector(ts).getBoundingClientRect();
                    return (t.x) - (r.x + r.width);
                };
                const row = document.querySelector('.library-header').getBoundingClientRect();
                const tog = document.querySelector('#library-view-mode-toggle').getBoundingClientRect();
                return { gap: gapOf('#refresh-library', '#library-view-mode-toggle'),
                         rowTop: row.y, togCy: tog.y + tog.height / 2,
                         togRight: tog.x + tog.width };
            })()""")
            activate()
            tid = page.evaluate("""(() => {
                const gapOf = (rs, ts) => {
                    const r = document.querySelector(rs).getBoundingClientRect();
                    const t = document.querySelector(ts).getBoundingClientRect();
                    return (t.x) - (r.x + r.width);
                };
                const row = document.querySelector('.tidal-toolbar').getBoundingClientRect();
                const tog = document.querySelector('#tidal-view-mode-toggle').getBoundingClientRect();
                return { gap: gapOf('#tidal-refresh-btn', '#tidal-view-mode-toggle'),
                         rowTop: row.y, togCy: tog.y + tog.height / 2,
                         togRight: tog.x + tog.width };
            })()""")
            check("[align] refresh/toggle gaps match", abs(lib["gap"] - tid["gap"]) <= 0.5)
            check("[align] header rows share one top", abs(lib["rowTop"] - tid["rowTop"]) <= 1)
            check("[align] toggles share one center", abs(lib["togCy"] - tid["togCy"]) <= 1)
            check("[align] toggles share one right edge", abs(lib["togRight"] - tid["togRight"]) <= 1)

            # Enter still executes the current query immediately.
            activate()
            page.fill("#tidal-search-input", "daft punk")
            page.keyboard.press("Enter")
            page.wait_for_timeout(400)
            check("[search] executing a search shows the result state",
                  page.locator("#tidal-browse-body").inner_text().find("No results") != -1)

            # Refresh reloads favorite ids + status without logging out and
            # keeps the browse surface alive.
            page.wait_for_timeout(200)
            page.evaluate("window.__tidalFetches.length = 0")
            page.click("#tidal-refresh-btn")
            page.wait_for_timeout(500)
            fetches = page.evaluate("window.__tidalFetches")
            ids_fetches = [u for u in fetches if u.endswith("/api/streaming/tidal/favorites/ids")]
            status_fetches = [u for u in fetches if u.endswith("/api/streaming/tidal/status")]
            logout_fetches = [u for u in fetches if "auth/logout" in u]
            check("[refresh] forces a fresh favorites/ids load", len(ids_fetches) >= 1)
            check("[refresh] reloads the provider status", len(status_fetches) >= 1)
            check("[refresh] never logs out", len(logout_fetches) == 0)
            check("[refresh] keeps the browse surface",
                  page.locator(".streaming-browse").count() == 1 and
                  page.locator(".tidal-toolbar-title").inner_text().strip() == "Tidal")

            # Mobile: the title row stays on one line; the second row wraps so
            # the search lands below the navigation, without overflowing.
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_timeout(200)
            activate()

            title_box = page.locator(".tidal-toolbar-title").bounding_box()
            actions_box = page.locator(".tidal-toolbar-actions").bounding_box()
            tabs_box = page.locator(".tidal-subbar .view-tabs").bounding_box()
            search_box = page.locator(".tidal-subbar .streaming-search").bounding_box()

            check("[390px] title and actions stay on one row",
                  abs(_center(title_box) - _center(actions_box)) <= 2)
            check("[390px] search wraps below the navigation",
                  _center(search_box) - _center(tabs_box) > 8)
            check("[390px] search uses the compact placeholder",
                  page.locator("#tidal-search-input").get_attribute("placeholder") == "Search…")
            check("[390px] refresh button visible",
                  page.locator("#tidal-refresh-btn").is_visible())
            check("[390px] no horizontal overflow on mobile",
                  page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            # The four library tabs wrap inside the panel instead of pushing
            # past its right edge (same as the TIDAL tabs row).
            page.evaluate("document.querySelector('.tab-btn[data-tab=\"library\"]').click()")
            page.wait_for_timeout(300)
            check("[390px] library tabs stay inside the panel", page.evaluate("""(() => {
                const pr = document.querySelector('#tab-library').getBoundingClientRect();
                return [...document.querySelectorAll('#tab-library *')].every((e) => {
                    const r = e.getBoundingClientRect();
                    return r.width < 5 || r.height < 5 || r.x + r.width <= pr.x + pr.width + 1.5;
                });
            })()"""))
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
