#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the A/B compare card next to Output extras.

In the two-column grid (768px and up) the A/B card takes the height of the
Output extras card, which grows with every enabled effect. The compare tile
fills the card and spends the spare height evenly between its sections
(status, preset pair, actions, info line); the preset slots keep their natural
height. Below 768px the card keeps its natural height and the plain gaps.

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8205

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_compare_card_fill_geometry.py (playwright not installed)")
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

EXTRAS = ["limiter", "headroom", "autogain", "loudness", "bass", "tone-effect", "upmixer"]

# The compare tile starts hidden until presets exist: show it with its texts. The extras are switched
# through their checkboxes (the real handlers run); the Upmixer row only exists for a Surround catalog.
PREPARE_JS = """
(() => {
    switchTab('effects');
    document.getElementById('effects-compare-row').style.display = 'flex';
    document.getElementById('effects-compare-active').textContent = 'Listening: A · Neutral';
    document.getElementById('effects-compare-chain').textContent = 'Chain: Single preset';
    document.getElementById('effects-info').textContent = '20 presets';
    document.getElementById('effects-status').innerHTML = '';
    document.getElementById('effects-upmixer-row').classList.remove('hidden');
    document.getElementById('effects-upmixer-center-wrap').classList.remove('hidden');
    document.getElementById('effects-upmixer-fft-wrap').classList.remove('hidden');
})();
"""

SET_EXTRAS_JS = """
([ids, on]) => {
    for (const id of ids) {
        const box = document.getElementById('effects-' + id + '-enabled');
        if (box.checked !== on) box.click();
    }
    // The checkboxes re-hide what only a Surround catalog would show; reveal it again.
    document.getElementById('effects-upmixer-mode').value = 'hq';
    document.getElementById('effects-upmixer-row').classList.remove('hidden');
    document.getElementById('effects-upmixer-center-wrap').classList.remove('hidden');
    document.getElementById('effects-upmixer-fft-wrap').classList.remove('hidden');
}
"""

MEASURE_JS = """
() => {
    const box = (el) => { const b = el.getBoundingClientRect(); return { top: b.top, bottom: b.bottom, height: b.height, left: b.left, right: b.right }; };
    const card = document.querySelector('.effects-card-manage-order');
    const extras = document.querySelector('.effects-card-extras');
    const tile = document.getElementById('effects-compare-row');
    const tileStyle = getComputedStyle(tile);
    const blocks = [...tile.children].filter((el) => el.offsetParent !== null && el.getBoundingClientRect().height > 0).map((el) => ({ id: el.id || el.className, ...box(el) }));
    const slots = [...tile.querySelectorAll('.effects-compare-slot')].map(box);
    return {
        card: box(card), extras: box(extras), tile: box(tile), blocks, slots,
        cardPaddingBottom: parseFloat(getComputedStyle(card).paddingBottom),
        tilePaddingBottom: parseFloat(tileStyle.paddingBottom), tileBorderBottom: parseFloat(tileStyle.borderBottomWidth),
        columns: getComputedStyle(document.querySelector('.effects-grid')).gridTemplateColumns.trim().split(/\\s+/).length,
        docFits: document.documentElement.scrollWidth <= window.innerWidth + 1,
    };
}
"""


def gaps(blocks):
    return [b["top"] - a["bottom"] for a, b in zip(blocks, blocks[1:])]


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
            page = browser.new_page(viewport={"width": 1440, "height": 1300})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(PREPARE_JS)
            page.wait_for_timeout(300)

            for width in (1440, 1180, 1000, 800, 768):
                page.set_viewport_size({"width": width, "height": 1300})
                states = {}
                # Nothing enabled, the common extras, and everything including the Upmixer.
                for label, ids in (("off", []), ("some", EXTRAS[:6]), ("all", EXTRAS)):
                    page.evaluate(SET_EXTRAS_JS, [EXTRAS, False])
                    page.evaluate(SET_EXTRAS_JS, [ids, True])
                    page.wait_for_timeout(120)
                    m = page.evaluate(MEASURE_JS)
                    states[label] = m
                    tag = f"[{width}px/{label}]"
                    check(f"{tag} two columns", m["columns"] == 2)
                    check(f"{tag} no horizontal page overflow", m["docFits"])
                    check(f"{tag} A/B card takes the extras height ({m['card']['height']:.0f} vs {m['extras']['height']:.0f})",
                          abs(m["card"]["height"] - m["extras"]["height"]) <= 1)
                    check(f"{tag} tile fills the card",
                          abs(m["tile"]["bottom"] - (m["card"]["bottom"] - m["cardPaddingBottom"])) <= 1.5)
                    names = [b["id"] for b in m["blocks"]]
                    check(f"{tag} four sections in the tile ({names})", len(m["blocks"]) == 4)
                    check(f"{tag} info line sits at the tile bottom",
                          abs(m["blocks"][-1]["bottom"] - (m["tile"]["bottom"] - m["tilePaddingBottom"] - m["tileBorderBottom"])) <= 1.5)
                    g = gaps(m["blocks"])
                    check(f"{tag} the spare height is spread evenly between the sections ({[round(x, 1) for x in g]})",
                          max(g) - min(g) <= 1.5)
                    check(f"{tag} the sections never touch ({min(g):.1f})", min(g) >= 14)
                    check(f"{tag} preset slots keep one natural height ({[round(s['height']) for s in m['slots']]})",
                          len({round(s["height"]) for s in m["slots"]}) == 1 and m["slots"][0]["height"] < 130)
                # More enabled effects: a taller neighbour makes the gaps grow, not a hole under the actions.
                check(f"[{width}px] gaps grow with the extras height",
                      gaps(states["off"]["blocks"])[0] < gaps(states["some"]["blocks"])[0] < gaps(states["all"]["blocks"])[0])
                check(f"[{width}px] no hole larger than the other gaps under the actions",
                      all(x <= gaps(states["all"]["blocks"])[0] + 1.5 for x in gaps(states["all"]["blocks"])))

            # A status message (e.g. a failed save) is a fifth section and takes its share of the gaps.
            page.set_viewport_size({"width": 1440, "height": 1300})
            page.evaluate(SET_EXTRAS_JS, [EXTRAS, True])
            page.evaluate("document.getElementById('effects-status').textContent = 'Saved'")
            page.wait_for_timeout(120)
            m = page.evaluate(MEASURE_JS)
            g = gaps(m["blocks"])
            check(f"[1440px/status] five sections ({len(m['blocks'])})", len(m["blocks"]) == 5)
            check(f"[1440px/status] gaps stay even ({[round(x, 1) for x in g]})", max(g) - min(g) <= 1.5)
            page.evaluate("document.getElementById('effects-status').innerHTML = ''")

            # One column: natural height, plain gaps, no stretch.
            for width in (767, 600, 390):
                page.set_viewport_size({"width": width, "height": 1300})
                page.evaluate(SET_EXTRAS_JS, [EXTRAS, True])
                page.wait_for_timeout(120)
                m = page.evaluate(MEASURE_JS)
                g = gaps(m["blocks"])
                check(f"[{width}px] one column", m["columns"] == 1)
                check(f"[{width}px] natural gaps between the sections ({[round(x, 1) for x in g]})",
                      all(abs(x - 14.4) <= 1.5 for x in g))
                check(f"[{width}px] no horizontal page overflow", m["docFits"])
            browser.close()
    finally:
        server.shutdown()
    print(f"compare card fill geometry: {passed} checks passed")


if __name__ == "__main__":
    _run()
