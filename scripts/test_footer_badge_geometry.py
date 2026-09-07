#!/usr/bin/env python3
"""Playwright geometry contract for the footer format badge.

The format badge (``#samplerate-status``) must have its left outer edge on
the same axis as the track texts (Artist/Track title/Album title), with the
badge text sitting cleanly inside the pill.  The queue badge keeps its own
position to the right as a text-only pill (no leading icon).

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium.  Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8198

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_footer_badge_geometry.py (playwright not installed)")
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


# Keep the samplerate poll healthy so renderSamplerateUI keeps the badge
# visible with a real rate instead of hiding it on a failed fetch.
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

STATE_JS = """
(() => {
    const sr = document.getElementById('samplerate-status');
    sr.classList.remove('hidden');
    sr.textContent = '44.1kHz';
    const qs = document.getElementById('queue-status');
    qs.classList.remove('hidden');
    qs.textContent = '1 / 1';
})();
"""

FALLBACK_BRANCH_JS = """
(() => {
    const title = document.getElementById('track-title');
    title.classList.remove('placeholder');
    title.textContent = 'Groove Is in the Heart';
    title.style.display = '';
    const artist = document.getElementById('track-artist');
    artist.textContent = 'Deee-Lite';
    artist.style.display = '';
    const scTitle = document.getElementById('sc-title');
    scTitle.textContent = '';
    const scArtist = document.getElementById('sc-artist');
    scArtist.textContent = '';
    const scAlbum = document.getElementById('sc-album');
    scAlbum.textContent = '';
    scAlbum.style.display = '';
})();
"""

SONG_INFO_BRANCH_JS = """
(() => {
    const title = document.getElementById('track-title');
    title.textContent = '';
    title.style.display = 'none';
    const artist = document.getElementById('track-artist');
    artist.textContent = '';
    artist.style.display = 'none';
    const scTitle = document.getElementById('sc-title');
    scTitle.textContent = 'Groove Is in the Heart';
    const scArtist = document.getElementById('sc-artist');
    scArtist.textContent = 'Deee-Lite';
    const scAlbum = document.getElementById('sc-album');
    scAlbum.textContent = 'World Clique';
    scAlbum.style.display = '';
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
            for width, height in ((1440, 900), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(STUB)
                # Only static/index.html is served in production; the
                # repository-root index.html is stale.
                page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
                page.wait_for_timeout(600)

                for branch, extra_js, ref_selector in (
                    ("fallback", FALLBACK_BRANCH_JS, "#track-title"),
                    ("song-info", SONG_INFO_BRANCH_JS, ".sc-title"),
                ):
                    page.evaluate(STATE_JS)
                    page.evaluate(extra_js)
                    # Let one samplerate poll cycle run; the stub keeps the
                    # badge visible, proving the layout survives real polling.
                    page.wait_for_timeout(1600)

                    badge = page.locator("#samplerate-status").bounding_box()
                    ref = page.locator(ref_selector).bounding_box()
                    artist = page.locator("#track-artist" if branch == "fallback" else ".sc-artist").bounding_box()
                    album = page.locator(".sc-album").bounding_box()
                    queue = page.locator("#queue-status").bounding_box()
                    text_left = page.evaluate(
                        "document.getElementById('samplerate-status').getBoundingClientRect().left"
                    )
                    badge_text_left = page.evaluate(
                        "(() => { const r = document.createRange();"
                        " r.selectNodeContents(document.getElementById('samplerate-status'));"
                        " return r.getBoundingClientRect().left; })()"
                    )

                    check(
                        f"[{width}px {branch}] format badge left edge == {ref_selector} left edge",
                        abs(badge["x"] - ref["x"]) <= 1,
                    )
                    check(
                        f"[{width}px {branch}] format badge left edge == artist left edge",
                        abs(badge["x"] - artist["x"]) <= 1,
                    )
                    if album and album["width"] > 0:
                        check(
                            f"[{width}px {branch}] format badge left edge == album left edge",
                            abs(badge["x"] - album["x"]) <= 1,
                        )
                    check(
                        f"[{width}px {branch}] badge text sits inside the pill",
                        badge_text_left > badge["x"] + 2 and badge_text_left < badge["x"] + badge["width"] - 2,
                    )
                    check(
                        f"[{width}px {branch}] queue badge sits right of the format badge",
                        queue["x"] > badge["x"] + badge["width"],
                    )
                    check(
                        f"[{width}px {branch}] no horizontal overflow",
                        page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
                    )
                    check(
                        f"[{width}px {branch}] badge text is visible",
                        page.locator("#samplerate-status").is_visible(),
                    )
                    check(f"[{width}px {branch}] text_left reports the box axis", abs(text_left - badge["x"]) <= 1)

                page.close()

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_footer_badge_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_footer_badge_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_footer_badge_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
