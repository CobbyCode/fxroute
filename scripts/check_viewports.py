#!/usr/bin/env python3
"""Viewport verification for the CSS cleanup.

Loads static/index.html at the required widths and asserts the FXRoute
layout invariants: footer fixed and fully visible, no horizontal overflow,
seek/volume sliders centered with matching gutters, no zone overlap.
Run manually after CSS changes; not part of the test suite.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8199

VIEWPORTS = [
    (1440, 900),
    (1200, 900),
    (1024, 900),
    (900, 900),
    (834, 1112),
    (820, 1180),
    (768, 1024),
    (390, 844),
    (360, 800),
    (320, 700),
]

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
    document.getElementById('playback-bar').classList.add('has-media');
    const sr = document.querySelector('.seek-row');
    sr.classList.remove('hidden');
    sr.style.display = '';
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
    scAlbum.style.display = 'none';
})();
"""


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


def _run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP  check_viewports.py (playwright not installed)")
        return 0

    server = _serve()
    checks = {"n": 0}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(STUB)
                page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
                page.wait_for_selector("#playback-bar", state="visible")
                page.evaluate(STATE_JS)
                page.wait_for_timeout(600)

                def check(name, cond):
                    assert cond, f"[{width}px] {name}"
                    checks["n"] += 1

                # Footer: fixed, fully inside the viewport horizontally
                bar = page.locator("#playback-bar").bounding_box()
                check("playback-bar has box", bar is not None)
                assert bar is not None
                check("playback-bar left >= 0", bar["x"] >= -0.5)
                check("playback-bar right <= viewport", bar["x"] + bar["width"] <= width + 0.5)
                check("playback-bar bottom within viewport", bar["y"] + bar["height"] <= height + 0.5)
                check("playback-bar is fixed", page.evaluate(
                    "getComputedStyle(document.getElementById('playback-bar')).position === 'fixed'"
                ))

                # No horizontal overflow
                check("no horizontal overflow", page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                ))

                # Seek row and volume control share the same outer gutters on
                # phones (both full-width rows); on wider layouts they live in
                # different grid zones by design.
                seek = page.locator(".seek-row").bounding_box()
                vol = page.locator(".volume-control").bounding_box()
                check("seek-row has box", seek is not None)
                check("volume-control has box", vol is not None)
                assert seek is not None and vol is not None
                if width <= 700:
                    check("seek/volume left gutters match", abs(seek["x"] - vol["x"]) <= 1)
                    check("seek/volume right gutters match", abs((seek["x"] + seek["width"]) - (vol["x"] + vol["width"])) <= 1)

                # The actual slider tracks (not just their rows) must share the
                # same horizontal center on phones: the volume track sits in a
                # row with icon/display side columns and would drift off-center
                # if those columns were asymmetric against the seek row.
                if width in (390, 360, 320):
                    seek_slider = page.locator(".seek-slider").bounding_box()
                    vol_slider = page.locator(".volume-slider").bounding_box()
                    check("seek-slider has box", seek_slider is not None)
                    check("volume-slider has box", vol_slider is not None)
                    assert seek_slider is not None and vol_slider is not None
                    seek_mid = seek_slider["x"] + seek_slider["width"] / 2
                    vol_mid = vol_slider["x"] + vol_slider["width"] / 2
                    check("seek/volume track centers match", abs(vol_mid - seek_mid) <= 1)

                # No overlap between track, transport, meter and volume zones
                def zone(sel):
                    return page.locator(sel).first.bounding_box()

                zones = {
                    "track": ".track-info",
                    "transport": ".playback-center",
                    "volume": ".controls",
                }
                boxes = {}
                for name, sel in zones.items():
                    b = zone(sel)
                    check(f"zone {name} has box", b is not None)
                    assert b is not None
                    boxes[name] = b

                # The meter is hidden by design on phones <=390px.
                meter_box = zone(".playback-meter")
                if width <= 390:
                    check("phone: meter hidden", meter_box is None or meter_box["width"] == 0)
                else:
                    check("meter has box", meter_box is not None)
                    assert meter_box is not None
                    boxes["meter"] = meter_box

                def overlaps(a, b):
                    return not (a["x"] + a["width"] <= b["x"] + 0.5 or b["x"] + b["width"] <= a["x"] + 0.5
                                or a["y"] + a["height"] <= b["y"] + 0.5 or b["y"] + b["height"] <= a["y"] + 0.5)

                if width > 390:
                    check("meter/transport no overlap", not overlaps(boxes["meter"], boxes["transport"]))
                check("track/transport no overlap", not overlaps(boxes["track"], boxes["transport"]))
                check("transport/volume no overlap", not overlaps(boxes["transport"], boxes["volume"]))

                page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  check_viewports.py ({checks['n']} checks)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_run())
    except AssertionError as exc:
        print(f"FAIL  check_viewports.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"FAIL  check_viewports.py")
        print(f"  type={type(exc).__name__} repr={exc!r}")
        traceback.print_exc()
        sys.exit(1)
