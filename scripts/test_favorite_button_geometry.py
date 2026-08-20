#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the favorite button shape.

The .track-row-favorite primitive is a round 34x34 button. The phone media
query shrinks it to 28x28; between 641 and 700px the primitive min-height of
34px used to survive, making the button 28x34 (oval). The button must be
square (width == height) across the whole phone range.

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8197

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_favorite_button_geometry.py (playwright not installed)")
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

# Insert a library track row (same markup renderLibraryView produces) so the
# favorite button is measured in its real flex context. The library tab needs
# to be active and the loading placeholder hidden for the row to be visible.
FAV_JS = """
(() => {
    switchTab('library');
    const loader = document.querySelector('.content-state--loading');
    if (loader) loader.style.display = 'none';
    const list = document.getElementById('tracks-list');
    list.classList.remove('hidden');
    list.innerHTML = `
        <div class="track-item" data-track-id="t1">
            <label class="track-select">
                <input type="checkbox" class="track-checkbox" data-track-id="t1">
                <span class="track-select-box"></span>
            </label>
            <button class="track-play" data-track-id="t1" type="button">▶</button>
            <div class="track-info">
                <div class="track-title">Test Track</div>
                <div class="track-artist track-sub">Test Artist</div>
            </div>
            <button class="track-row-favorite" data-track-favorite="t1" type="button" aria-pressed="false" aria-label="Add track to favorites">♡</button>
        </div>`;
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
            page = browser.new_page(viewport={"width": 700, "height": 900})
            page.add_init_script(STUB)
            # Only static/index.html is served in production; the
            # repository-root index.html is stale.
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(FAV_JS)
            page.wait_for_timeout(300)

            fav = page.locator(".track-row-favorite")
            check("favorite button exists", fav.count() >= 1)

            for width in (700, 680, 650, 641, 640, 601, 540, 480, 390, 360, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                bb = fav.first.bounding_box()
                check(f"[{width}px] favorite button has box", bb is not None)
                assert bb is not None
                check(
                    f"[{width}px] favorite button is square (w={bb['width']:.2f} h={bb['height']:.2f})",
                    abs(bb["width"] - bb["height"]) <= 1,
                )
                # Round shape implies the 28px phone size between 641-700px.
                if 641 <= width <= 700:
                    check(f"[{width}px] favorite button is 28px", abs(bb["width"] - 28) <= 1 and abs(bb["height"] - 28) <= 1)

            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_favorite_button_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_favorite_button_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_favorite_button_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
