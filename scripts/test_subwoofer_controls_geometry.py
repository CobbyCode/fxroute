#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the subwoofer controls columns.

At max-width:760px .effects-subwoofer-controls is intentionally reduced to a
single column; the 761-1100px tablet rule uses two columns, so:

  ≤760px -> 1 column, 761-1100px -> 2 columns,
  >1100px -> 2 equal cards (2.1) or 3 equal cards (2.2, Dual-Mono and Stereo).

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8202

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_subwoofer_controls_geometry.py (playwright not installed)")
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

SHOW_JS = """
(() => {
    switchTab('effects');
    const card = document.querySelector('.effects-card-subwoofer');
    if (card) card.classList.remove('hidden');
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
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(SHOW_JS)
            page.wait_for_timeout(300)

            controls = page.locator(".effects-subwoofer-controls")
            check("subwoofer controls exist", controls.count() >= 1)

            def column_count():
                return page.evaluate(
                    "getComputedStyle(document.querySelector('.effects-subwoofer-controls'))"
                    ".gridTemplateColumns.trim().split(/\\s+/).length"
                )

            # Mobile ≤760px: exactly one column.
            for width in (760, 700, 600, 520):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                n = column_count()
                check(f"[{width}px] subwoofer controls 1 column (got {n})", n == 1)

            # Tablet 761-1100px: two columns unchanged.
            for width in (761, 900, 1100):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                n = column_count()
                check(f"[{width}px] subwoofer controls 2 columns (got {n})", n == 2)

            # Desktop >1100px: compact equal cards — 2.1 shows Global + Sub
            # (2 columns), 2.2 adds the third card (3 columns).
            for width in (1101, 1200, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                n = column_count()
                check(f"[{width}px] subwoofer controls 2 columns without 2.2 (got {n})", n == 2)
                geometry = page.evaluate("""
                    (() => {
                        const group = document.querySelector('.effects-subwoofer-global-group');
                        const field = document.querySelector('.effects-subwoofer-highpass-field');
                        const select = document.querySelector('#effects-subwoofer-main-highpass');
                        const groupRect = group.getBoundingClientRect();
                        const fieldRect = field.getBoundingClientRect();
                        const selectRect = select.getBoundingClientRect();
                        return {
                            clientWidth: group.clientWidth,
                            scrollWidth: group.scrollWidth,
                            groupRight: groupRect.right,
                            fieldRight: fieldRect.right,
                            selectRight: selectRect.right,
                        };
                    })()
                """)
                check(
                    f"[{width}px] Global group has no hidden horizontal overflow ({geometry})",
                    geometry["scrollWidth"] <= geometry["clientWidth"] + 1,
                )
                check(
                    f"[{width}px] Main highpass field stays inside Global group ({geometry})",
                    geometry["fieldRight"] <= geometry["groupRight"] + 1,
                )
                check(
                    f"[{width}px] Main highpass select stays inside Global group ({geometry})",
                    geometry["selectRight"] <= geometry["groupRight"] + 1,
                )

            # 2.2 pins the third equal card: Dual-Mono and Stereo show
            # Global + Sub 1 + Sub 2 in three equal columns on desktop.
            three_col = page.evaluate("""
                (() => {
                    const card = document.querySelector('.effects-card-subwoofer');
                    card.classList.add('is-subwoofer-22');
                    // Without a catalog the Sub 2 group starts hidden; unhide
                    // it for the width comparison so all three cards measure.
                    const hidden = [...document.querySelectorAll('.effects-subwoofer-sub2-field.hidden')];
                    hidden.forEach((el) => el.classList.remove('hidden'));
                    const n = getComputedStyle(document.querySelector('.effects-subwoofer-controls'))
                        .gridTemplateColumns.trim().split(/\\s+/).length;
                    const groups = [...document.querySelectorAll(
                        '.effects-subwoofer-global-group, .effects-subwoofer-sub1-group, .effects-subwoofer-sub2-group')]
                        .map((el) => Math.round(el.getBoundingClientRect().width));
                    hidden.forEach((el) => el.classList.add('hidden'));
                    card.classList.remove('is-subwoofer-22');
                    return { n, groups };
                })()
            """)
            check(f"2.2 desktop uses 3 equal cards ({three_col})", three_col["n"] == 3)
            check(f"2.2 cards share one width ({three_col})",
                  max(three_col["groups"]) - min(three_col["groups"]) <= 2)

            # Timing row spans the full width below the cards.
            timing = page.evaluate("""
                (() => {
                    const card = document.querySelector('.effects-card-subwoofer');
                    card.classList.add('is-subwoofer-22');
                    const style = getComputedStyle(document.querySelector('#effects-subwoofer-derived-delays'));
                    card.classList.remove('is-subwoofer-22');
                    return { column: style.gridColumn };
                })()
            """)
            check(f"timing row spans the full width ({timing})",
                  timing["column"] == "1 / -1")

            # Single shared crossover plus one block per side, and the L/R
            # link switch only for a true Stereo sub pair. The per-side
            # blocks and the link start hidden (no Stereo pair routed) and
            # each block carries Frequency, Type, Slope and Main highpass.
            # Unlinked Stereo serves one side at a time through the Sub L /
            # Sub R tabs, so the tabs start hidden as well.
            layout = page.evaluate("""
                (() => {
                    const card = document.querySelector('.effects-card-subwoofer');
                    const shared = document.querySelector('#effects-subwoofer-shared-crossover');
                    const sides = ['left', 'right'].map((side) =>
                        document.querySelector(`#effects-subwoofer-${side}-crossover`));
                    const link = document.querySelector('#effects-subwoofer-link-wrap');
                    const tabs = document.querySelector('#effects-subwoofer-side-tabs');
                    const cardRect = card.getBoundingClientRect();
                    const fields = (row) => row.querySelectorAll('input[type=number], select').length;
                    return {
                        sharedHidden: shared.classList.contains('hidden'),
                        sharedFields: fields(shared),
                        sharedRight: shared.getBoundingClientRect().right,
                        sideHidden: sides.map((row) => row.classList.contains('hidden')),
                        sideFields: sides.map(fields),
                        sideRight: sides.map((row) => row.getBoundingClientRect().right),
                        cardRight: cardRect.right,
                        linkHidden: link.classList.contains('hidden'),
                        tabsHidden: tabs.classList.contains('hidden'),
                        tabCount: tabs.querySelectorAll('[data-sub-side]').length,
                    };
                })()
            """)
            check(f"shared crossover row is visible and complete ({layout})",
                  layout["sharedHidden"] is False and layout["sharedFields"] == 4)
            check("the shared row stays inside the card",
                  layout["sharedRight"] <= layout["cardRight"] + 1)
            check("both per-side rows exist, complete and start hidden",
                  layout["sideFields"] == [4, 4] and layout["sideHidden"] == [True, True])
            check("per-side rows stay inside the card",
                  all(right <= layout["cardRight"] + 1 for right in layout["sideRight"]))
            check("the L/R link switch starts hidden", layout["linkHidden"] is True)
            check("the Sub L / Sub R tabs exist and start hidden",
                  layout["tabsHidden"] is True and layout["tabCount"] == 2)

            page.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_subwoofer_controls_geometry.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_subwoofer_controls_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_subwoofer_controls_geometry.py")
        print(f"  {exc}")
        sys.exit(1)
