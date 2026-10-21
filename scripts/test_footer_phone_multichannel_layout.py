#!/usr/bin/env python3
"""Playwright geometry contract for the phone footer with the multichannel meter.

At <=390px the level meter is hidden by design and the grid areas carry no
"meter" cell.  The multichannel meter sets ``display: flex`` at higher
specificity; if it stays visible it falls into implicit grid tracks (extra
column and row), squeezing transport and volume to the left and growing the
footer.  This runs the real rendered page in headless Chromium and skips
cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8199

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_footer_phone_multichannel_layout.py (playwright not installed)")
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
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    if (u.startsWith('/api/') || u.includes('/api/')) return json({});
    return realFetch(url, opts);
};
"""

# App playback with media: transport buttons and seek row revealed, meter in
# multichannel mode (as in Surround output modes).
PLAYING_JS = """
(() => {
    const bar = document.getElementById('playback-bar');
    bar.className = 'playback-bar has-media is-playing has-cover';
    for (const id of ['btn-previous', 'btn-next', 'footer-shuffle', 'footer-loop']) {
        document.getElementById(id).classList.remove('hidden');
    }
    bar.querySelector('.seek-row').classList.remove('hidden');
    document.getElementById('playback-meter').classList.add('is-multichannel');
    document.getElementById('meter-multi').innerHTML = '<div class="meter-col"><div class="meter-bar"><i></i></div></div>';
})();
"""

MEASURE_JS = """
(() => {
    const rect = (el) => { const r = el.getBoundingClientRect(); return { x: r.x, right: r.right, y: r.y, bottom: r.bottom, w: r.width, h: r.height }; };
    const bar = document.getElementById('playback-bar');
    const meter = document.getElementById('playback-meter');
    const style = getComputedStyle(bar);
    return {
        bar: rect(bar),
        columns: style.gridTemplateColumns.split(' ').length,
        rows: style.gridTemplateRows.split(' ').length,
        meterDisplay: getComputedStyle(meter).display,
        meter: rect(meter),
        center: rect(bar.querySelector('.playback-center')),
        controls: rect(bar.querySelector('.controls')),
        track: rect(bar.querySelector('.track-info')),
        overflowX: document.documentElement.scrollWidth - window.innerWidth,
    };
})();
"""


def _run():
    server = _serve()
    passed = 0

    def check(name, condition, detail=""):
        assert condition, f"{name} {detail}".strip()
        nonlocal passed
        passed += 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width, height in ((320, 568), (360, 640), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(STUB)
                page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
                page.wait_for_timeout(600)
                page.add_style_tag(content="*,*::before,*::after{transition:none!important;animation:none!important}")
                page.evaluate(PLAYING_JS)
                page.wait_for_timeout(200)
                m = page.evaluate(MEASURE_JS)
                label = f"[{width}px multichannel]"

                check(f"{label} meter is hidden", m["meterDisplay"] == "none", m["meterDisplay"])
                check(f"{label} grid has only the two explicit columns", m["columns"] == 2, m["columns"])
                check(f"{label} grid has no implicit row", m["rows"] == 3, m["rows"])
                # Transport and volume use the full inner width of the bar.
                check(
                    f"{label} transport spans the bar width",
                    abs(m["center"]["x"] - m["track"]["x"]) <= 1
                    and abs(m["center"]["right"] - (m["bar"]["right"] - (m["track"]["x"] - m["bar"]["x"]))) <= 1,
                    m["center"],
                )
                check(
                    f"{label} volume spans the bar width",
                    abs(m["controls"]["right"] - m["center"]["right"]) <= 1,
                    (m["controls"], m["center"]),
                )
                check(f"{label} footer stays compact", m["bar"]["h"] <= 200, m["bar"]["h"])
                check(f"{label} no horizontal overflow", m["overflowX"] <= 0, m["overflowX"])
                page.close()

            # Line source: switcher and volume only, no implicit tracks.
            page = browser.new_page(viewport={"width": 360, "height": 640})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_timeout(600)
            page.add_style_tag(content="*,*::before,*::after{transition:none!important;animation:none!important}")
            page.evaluate(PLAYING_JS)
            page.evaluate(
                "document.getElementById('playback-bar').classList.add('source-mode');"
                "document.getElementById('source-switcher').classList.remove('hidden');"
            )
            page.wait_for_timeout(200)
            m = page.evaluate(MEASURE_JS)
            check("[360px source-mode] meter is hidden", m["meterDisplay"] == "none", m["meterDisplay"])
            check("[360px source-mode] grid has two columns and two rows", (m["columns"], m["rows"]) == (2, 2), (m["columns"], m["rows"]))
            check("[360px source-mode] no horizontal overflow", m["overflowX"] <= 0, m["overflowX"])
            page.close()

            # One pixel wider than the cut-off the meter shares the row with
            # the track info again.
            page = browser.new_page(viewport={"width": 391, "height": 844})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_timeout(600)
            page.add_style_tag(content="*,*::before,*::after{transition:none!important;animation:none!important}")
            page.evaluate(PLAYING_JS)
            page.wait_for_timeout(200)
            m = page.evaluate(MEASURE_JS)
            check("[391px multichannel] meter stays visible", m["meterDisplay"] == "flex", m["meterDisplay"])
            check("[391px multichannel] meter sits beside the track info", m["meter"]["x"] >= m["track"]["right"] - 1, m["meter"])
            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"OK  scripts/test_footer_phone_multichannel_layout.py ({passed} checks)")


if __name__ == "__main__":
    _run()
