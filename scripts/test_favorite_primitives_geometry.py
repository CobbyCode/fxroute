#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the unified favorite primitives.

.track-row-favorite, .track-fav and .streaming-fav share one round favorite
button. The responsive contract is one geometry per viewport range:

  >700px            -> 34x34 (base primitive)
  641-700px         -> 28x28 (phone)
  ≤640px            -> 40x40 (small phone)

Each variant must be square (width == height) and all three must have the
same box within the same range. The playback-footer favorite
(.track-favorite-btn) keeps its own compact footer size and is out of scope.

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8203

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_favorite_primitives_geometry.py (playwright not installed)")
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

# Insert the three favorite variants into the library track list so each is
# measured in its real flex row context.
FAV_JS = """
(() => {
    switchTab('library');
    const loader = document.querySelector('.content-state--loading');
    if (loader) loader.style.display = 'none';
    const list = document.getElementById('tracks-list');
    list.classList.remove('hidden');
    list.innerHTML = `
        <div class="track-item" data-track-id="t1">
            <button class="track-play" data-track-id="t1" type="button">▶</button>
            <div class="track-info">
                <div class="track-title">Test Track</div>
                <div class="track-artist track-sub">Test Artist</div>
            </div>
            <button class="track-row-favorite" data-track-favorite="t1" type="button" aria-label="favorite">♡</button>
        </div>
        <div class="track-item" data-track-id="t2">
            <button class="track-fav" data-fav-type="track" data-fav-id="t2" type="button" aria-label="favorite">♡</button>
        </div>
        <div class="track-item" data-track-id="t3">
            <button class="streaming-fav" data-fav-type="track" data-fav-id="t3" type="button" aria-label="favorite">♡</button>
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
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(FAV_JS)
            page.wait_for_timeout(300)

            selectors = {
                "track-row-favorite": ".track-row-favorite",
                "track-fav": ".track-fav",
                "streaming-fav": ".streaming-fav",
            }
            for name, sel in selectors.items():
                check(f"{name} exists", page.locator(sel).count() >= 1)

            def boxes():
                out = {}
                for name, sel in selectors.items():
                    bb = page.locator(sel).first.bounding_box()
                    out[name] = bb
                return out

            def assert_range(widths, expected, label):
                for width in widths:
                    page.set_viewport_size({"width": width, "height": 900})
                    page.wait_for_timeout(80)
                    bs = boxes()
                    for name, bb in bs.items():
                        check(f"[{width}px] {name} has box", bb is not None)
                        assert bb is not None
                        check(
                            f"[{width}px] {name} square (w={bb['width']:.2f} h={bb['height']:.2f})",
                            abs(bb["width"] - bb["height"]) <= 1,
                        )
                        check(
                            f"[{width}px] {name} is {expected}px (got {bb['width']:.2f})",
                            abs(bb["width"] - expected) <= 1 and abs(bb["height"] - expected) <= 1,
                        )
                    sizes = {name: bb["width"] for name, bb in bs.items()}
                    check(
                        f"[{width}px] all three variants match ({label})",
                        max(sizes.values()) - min(sizes.values()) <= 1,
                    )

            assert_range((700, 680, 641), 28, "phone 28px")
            assert_range((640, 600, 390, 360, 320), 40, "small phone 40px")
            assert_range((761, 900, 1440), 34, "desktop 34px")

            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_favorite_primitives_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_favorite_primitives_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_favorite_primitives_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
