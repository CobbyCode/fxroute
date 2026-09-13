#!/usr/bin/env python3
"""Library empty-state contract while a library scan is running.

A scan fills the library cache over seconds. The Albums and the
Tracks/Playlists surfaces must not report "No albums found" / "No tracks yet"
while it runs: they show the shared library scan status (same source and
progress line the tracks view already used) and only fall back to the empty
message once the scan finished and the library really has no content. An
existing playlist is content, so an empty track list must not wipe it.

Runs the real rendered page (static server + stubbed library API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8211

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_scan_empty_state.py (playwright not installed)")
    sys.exit(0)


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


STUB = """
const realFetch = window.fetch.bind(window);
// Mutable so the test can end the scan: the app polls /api/library/status
// while it runs and re-renders the active library view.
window.__libstub = Object.assign({
    scanning: true,
    tracks_found: 12,
    files_seen: 40,
    current_dir: '/music/Scan Test',
    playlists: [{ id: 'pl-1', name: 'Scan Mix', track_count: 2 }],
}, window.__libstubSeed || {});
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), {
        status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    const s = window.__libstub;
    if (u.includes('/api/library/status')) {
        return json({
            scanning: s.scanning,
            tracks_found: s.tracks_found,
            files_seen: s.files_seen,
            current_dir: s.current_dir,
        });
    }
    if (u.includes('/api/tracks')) return json([]);
    if (u.includes('/api/albums')) return json([]);
    if (u.includes('/api/playlists')) return json(s.playlists);
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    if (u.includes('/api/')) return json({});
    return realFetch(url, opts);
};
"""


def _run():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    passed = 0

    def check(name, condition):
        nonlocal passed
        assert condition, name
        passed += 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_function("typeof window.FXRouteContentState === 'object'", timeout=15000)
            page.wait_for_timeout(500)

            def activate(tab):
                page.evaluate(
                    f"document.querySelector('.tab-btn[data-tab=\"{tab}\"]').click()"
                )
                page.wait_for_timeout(400)

            def content_state():
                return page.evaluate(
                    "(() => { const el = document.querySelector('#tab-library .content-state');"
                    " return { text: (el.textContent || '').trim(), cls: el.className }; })()"
                )

            activate("library")

            # --- during the scan: albums shows the shared scan status
            page.evaluate("document.getElementById('library-view-albums').click()")
            page.wait_for_timeout(1600)  # one scan poll
            albums = content_state()
            check(
                "scanning: albums view shows the shared scan status",
                "Scanning library" in albums["text"] and "12 audio tracks found" in albums["text"],
            )
            check(
                "scanning: albums view is a loading state, not an empty state",
                "content-state--loading" in albums["cls"] and "content-state--empty" not in albums["cls"],
            )
            check(
                "scanning: albums view never claims there are no albums",
                "No albums found" not in albums["text"],
            )

            # --- during the scan: tracks keep the scan status and the playlist
            page.evaluate("document.getElementById('library-view-tracks').click()")
            page.wait_for_timeout(1600)
            tracks = content_state()
            check("scanning: tracks view keeps the shared scan status", "Scanning library" in tracks["text"])
            check(
                "scanning: an empty track list does not wipe the existing playlist",
                page.locator('#tracks-list .playlist-item[data-playlist-id="pl-1"]').count() == 1,
            )
            check(
                "scanning: tracks view does not claim the library is empty",
                "No tracks yet" not in tracks["text"],
            )

            # --- scan finished, library really empty: now the empty state is due
            page.evaluate("document.getElementById('library-view-albums').click()")
            page.evaluate("window.__libstub.scanning = false; window.__libstub.tracks_found = 0;")
            page.wait_for_function(
                "document.querySelector('#tab-library .content-state')"
                ".className.includes('content-state--empty')",
                timeout=10000,
            )
            finished = content_state()
            check(
                "finished: albums view falls back to the empty message",
                "No albums found. Import music with album tags." in finished["text"],
            )
            browser.close()

        # --- finished scan on an empty library, no playlists: empty states stay
        with sync_playwright() as p2:
            browser2 = p2.chromium.launch()
            page2 = browser2.new_page(viewport={"width": 1440, "height": 900})
            page2.add_init_script(
                "window.__libstubSeed = { scanning: false, tracks_found: 0, files_seen: 0, playlists: [] };"
            )
            page2.add_init_script(STUB)
            page2.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page2.wait_for_function("typeof window.FXRouteContentState === 'object'", timeout=15000)
            page2.wait_for_timeout(800)
            page2.evaluate("document.querySelector('.tab-btn[data-tab=\"library\"]').click()")
            page2.wait_for_timeout(400)
            page2.evaluate("document.getElementById('library-view-tracks').click()")
            page2.wait_for_timeout(600)
            idle_tracks = page2.evaluate(
                "document.querySelector('#tab-library .content-state').textContent.trim()"
            )
            check(
                "finished: tracks view falls back to the empty message",
                "No tracks yet. Import a file or URL to get started." in idle_tracks,
            )
            browser2.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_library_scan_empty_state.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_library_scan_empty_state.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print("FAIL  scripts/test_library_scan_empty_state.py")
        print(f"  {exc}")
        sys.exit(1)
