#!/usr/bin/env python3
"""Browser regression checks for the debounced TIDAL direct search."""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8198

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_tidal_direct_search.py (playwright not installed)")
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
window.__tidalSearchFetches = [];
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (data) => Promise.resolve(new Response(JSON.stringify(data), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
    }));
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
    if (u.includes('/api/streaming/tidal/favorites/ids')) {
        return json({ tracks: [], albums: [], artists: [], playlists: [] });
    }
    if (u.includes('/api/streaming/tidal/favorites?')) return json([]);
    if (u.includes('/api/streaming/tidal/search?q=')) {
        const parsed = new URL(u, window.location.href);
        const query = parsed.searchParams.get('q') || '';
        const type = parsed.searchParams.get('types') || 'tracks';
        window.__tidalSearchFetches.push({ query, type });
        const title = query === 'queen' ? 'Queen result' : query === 'que' ? 'Que result' : query + ' result';
        const data = {
            tracks: type === 'tracks' ? [{ id: query, title, artist: 'Test artist', album: 'Test album', duration: 180 }] : [],
            artists: [],
            albums: [],
            playlists: [],
        };
        const delay = query === 'que' ? 450 : 20;
        return new Promise((resolve) => setTimeout(() => resolve(new Response(JSON.stringify(data), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
        })), delay));
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
            page = browser.new_page(viewport={"width": 1024, "height": 800})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_function("typeof window.FXRouteStreaming === 'object'", timeout=15000)
            page.wait_for_timeout(300)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: true, capabilities: { transport: false } });"
            )
            page.locator("#tab-btn-tidal").click()
            page.wait_for_selector("#tidal-search-input")

            check("search button is removed", page.locator("#tidal-search-btn").count() == 0)

            page.locator("#tidal-search-input").click()
            page.keyboard.type("que", delay=20)
            page.wait_for_timeout(100)
            check("typing does not request immediately", len(page.evaluate("window.__tidalSearchFetches")) == 0)
            page.wait_for_timeout(260)
            page.wait_for_function("window.__tidalSearchFetches.length === 1", timeout=1000)

            page.keyboard.type("en", delay=20)
            page.wait_for_function("window.__tidalSearchFetches.length === 2", timeout=1500)
            page.wait_for_timeout(550)
            fetches = page.evaluate("window.__tidalSearchFetches")
            body_text = page.locator("#tidal-browse-body").inner_text()
            check("both debounced queries are requested", [item["query"] for item in fetches] == ["que", "queen"])
            check("newer results remain visible after stale response", "Search results for \"queen\"" in body_text)
            check("stale result does not overwrite newer result", "Queen result" in body_text and "Que result" not in body_text)

            page.fill("#tidal-search-input", "")
            page.wait_for_selector("#tidal-fav-results")
            page.wait_for_timeout(200)
            body_text = page.locator("#tidal-browse-body").inner_text()
            check("clearing returns to the existing browse surface", page.locator("#tidal-fav-results").count() == 1)
            check("clearing removes search results", "Search results for" not in body_text and page.locator("#tidal-search-items").count() == 0)
            check("clearing does not issue another search", len(page.evaluate("window.__tidalSearchFetches")) == 2)

            page.fill("#tidal-search-input", "reconnect")
            page.wait_for_timeout(100)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: false, capabilities: { transport: false } });"
            )
            page.wait_for_timeout(350)
            page.evaluate(
                "window.FXRouteStreaming.renderProvider('tidal', "
                "{ installed: true, available: true, authenticated: true, capabilities: { transport: false } });"
            )
            page.wait_for_selector('#tidal-search-items', timeout=1500)
            check("a search pending across re-authentication is resumed", "Search results for \"reconnect\"" in page.locator("#tidal-browse-body").inner_text())
            page.fill("#tidal-search-input", "")
            page.wait_for_selector("#tidal-fav-results")

            page.evaluate("window.__tidalSearchFetches.length = 0")
            page.fill("#tidal-search-input", "que")
            page.wait_for_function("window.__tidalSearchFetches.length === 1", timeout=1000)
            page.fill("#tidal-search-input", "")
            page.wait_for_selector("#tidal-fav-results")
            page.wait_for_timeout(500)
            check("clearing an in-flight search blocks its late result", "Que result" not in page.locator("#tidal-browse-body").inner_text())

            page.evaluate("window.__tidalSearchFetches.length = 0")
            page.fill("#tidal-search-input", "refresh")
            page.wait_for_timeout(100)
            page.click("#tidal-refresh-btn")
            page.wait_for_timeout(700)
            refresh_fetches = [
                item for item in page.evaluate("window.__tidalSearchFetches")
                if item["query"] == "refresh"
            ]
            check("refresh does not duplicate a pending debounced search", len(refresh_fetches) == 1)

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_tidal_direct_search.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_tidal_direct_search.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report browser setup failures
        print("FAIL  scripts/test_tidal_direct_search.py")
        print(f"  {exc}")
        sys.exit(1)
