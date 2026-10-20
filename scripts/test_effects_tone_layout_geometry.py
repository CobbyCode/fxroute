#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the Tone effects layout.

Upmixer, Loudness, Bass enhancer and Tone effect share one pattern: a compact
header (checkbox + name) and one details block below it. A disabled effect
leaves nothing but its header (no gap); an enabled one shows all parameters in
the details, side by side where the card allows it and wrapping inside the
card where it does not.

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
    print("SKIP  scripts/test_effects_tone_layout_geometry.py (playwright not installed)")
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

KEYS = ("upmixer", "loudness", "bass", "tone-effect")

# Switch every effect to the wanted state through its checkbox (the real handlers run), then
# reveal what only the Surround catalog would: the row itself, the Center control and, for HQ, the FFT size.
SET_STATE_JS = """
(on) => {
    for (const key of %s) {
        const box = document.getElementById('effects-' + key + '-enabled');
        if (box.checked !== on) box.click();
    }
    const mode = document.getElementById('effects-upmixer-mode');
    mode.value = 'hq';
    document.getElementById('effects-upmixer-row').classList.remove('hidden');
    document.getElementById('effects-upmixer-center-wrap').classList.remove('hidden');
    document.getElementById('effects-upmixer-fft-wrap').classList.remove('hidden');
}
""" % list(KEYS)

MEASURE_JS = """
() => {
    const box = (el) => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, bottom: b.bottom, height: b.height }; };
    const group = document.getElementById('effects-loudness-enabled').closest('section.effects-extras-group');
    const effects = {};
    for (const key of %s) {
        const row = document.getElementById(key === 'upmixer' ? 'effects-upmixer-row' : 'effects-' + key + '-row');
        const header = row.querySelector('.effects-effect-header');
        const details = row.querySelector('.effects-effect-details');
        const controls = [...details.querySelectorAll('.effects-inline-control')].filter((el) => el.offsetParent !== null)
            .map((el) => ({ id: (el.querySelector('input, select') || {}).id, ...box(el),
                children: [...el.querySelectorAll('select, .effects-inline-control-label')].map(box) }));
        const style = getComputedStyle(row);
        // The separator above every effect after the first visible one (padding + border on top) is not leftover space.
        const chrome = ['paddingTop', 'borderTopWidth'].reduce((sum, name) => sum + parseFloat(style[name] || 0), 0);
        effects[key] = { row: box(row), header: box(header), chrome, details: getComputedStyle(details).display, controls };
    }
    return { group: box(group), effects, docFits: document.documentElement.scrollWidth <= window.innerWidth + 1 };
}
""" % list(KEYS)


def _run():
    server = _serve()
    passed = 0

    def check(name, condition):
        assert condition, name
        nonlocal passed
        passed += 1

    def same_line(controls, first, second):
        a = next(c for c in controls if c["id"] == first)
        b = next(c for c in controls if c["id"] == second)
        return abs(a["top"] - b["top"]) <= 2

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate("switchTab('effects')")
            page.wait_for_timeout(300)

            for width in (1440, 1100, 900, 800, 600, 480, 390, 320):
                page.set_viewport_size({"width": width, "height": 1000})
                page.wait_for_timeout(80)

                # Everything off: only the headers remain, no gap is left behind.
                page.evaluate(SET_STATE_JS, False)
                page.wait_for_timeout(80)
                off = page.evaluate(MEASURE_JS)
                for key, effect in off["effects"].items():
                    check(f"[{width}px] {key}: details gone when off", effect["details"] == "none")
                    check(f"[{width}px] {key}: row is just its header when off ({effect['row']['height']:.0f} vs {effect['header']['height']:.0f} + {effect['chrome']:.0f})",
                          effect["row"]["height"] <= effect["header"]["height"] + effect["chrome"] + 1)

                # Everything on: the details sit below their header and inside the card.
                page.evaluate(SET_STATE_JS, True)
                page.wait_for_timeout(80)
                on = page.evaluate(MEASURE_JS)
                group = on["group"]
                check(f"[{width}px] no horizontal page overflow", on["docFits"])
                for key, effect in on["effects"].items():
                    check(f"[{width}px] {key}: details shown when on", effect["details"] != "none")
                    check(f"[{width}px] {key}: parameters sit below the header", all(c["top"] >= effect["header"]["bottom"] - 1 for c in effect["controls"]))
                    for control in effect["controls"]:
                        check(f"[{width}px] {key}/{control['id']} stays inside the card",
                               control["left"] >= group["left"] - 1 and control["right"] <= group["right"] + 1)
                        check(f"[{width}px] {key}/{control['id']} children fit their reserved cell",
                              all(c['left'] >= control['left'] - 1 and c['right'] <= control['right'] + 1 for c in control['children']))
                        for other in effect['controls']:
                            if other is control:
                                continue
                            check(f"[{width}px] {control['id']} does not paint over {other['id']}",
                                  all(c['right'] <= other['left'] + 1 or c['left'] >= other['right'] - 1
                                      or c['bottom'] <= other['top'] + 1 or c['top'] >= other['bottom'] - 1
                                      for c in control['children']))
                    check(f"[{width}px] {key}: the details belong to the row", effect["controls"] and effect["row"]["bottom"] >= max(c["bottom"] for c in effect["controls"]) - 1)

                # Where the details are wide enough, the pairs share a line.
                if width >= 390:
                    up = on["effects"]["upmixer"]["controls"]
                    check(f"[{width}px] Mode and FFT share a line", same_line(up, "effects-upmixer-mode", "effects-upmixer-fft-size"))
                    check(f"[{width}px] Center and Surround share a line", same_line(up, "effects-upmixer-center", "effects-upmixer-surround"))
                    check(f"[{width}px] Center line sits below the Mode line",
                          next(c for c in up if c["id"] == "effects-upmixer-center")["top"] > next(c for c in up if c["id"] == "effects-upmixer-mode")["bottom"] - 1)
                    check(f"[{width}px] Strength and FFT share a line",
                          same_line(on["effects"]["loudness"]["controls"], "effects-loudness-strength", "effects-loudness-fft-size"))

                # Switching one effect on opens only its own details.
                page.evaluate(SET_STATE_JS, False)
                page.evaluate("document.getElementById('effects-bass-enabled').click()")
                page.wait_for_timeout(80)
                one = page.evaluate(MEASURE_JS)
                for key, effect in one["effects"].items():
                    check(f"[{width}px] only Bass enhancer opened ({key})", (effect["details"] != "none") == (key == "bass"))

            # Enlarged text can cross container-query boundaries independently
            # of the viewport. All child paint boxes must still fit their cells.
            for font_size in (20, 24):
                page.evaluate('(size) => document.documentElement.style.fontSize = size + "px"', font_size)
                for width in (1920, 1280, 960, 600, 390):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    page.evaluate(SET_STATE_JS, True)
                    measured = page.evaluate(MEASURE_JS)
                    check(f'[{width}px/{font_size}px font] page fits', measured['docFits'])
                    for key, effect in measured['effects'].items():
                        check(f'[{width}px/{font_size}px font] {key} reserves actual child widths',
                              all(child['left'] >= control['left'] - 1 and child['right'] <= control['right'] + 1
                                  and control['left'] >= measured['group']['left'] - 1
                                  and control['right'] <= measured['group']['right'] + 1
                                  for control in effect['controls'] for child in control['children']))
            browser.close()
    finally:
        server.shutdown()
    print(f"effects tone layout geometry: {passed} checks passed")


if __name__ == "__main__":
    _run()
