#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the subwoofer controls columns.

At max-width:760px .effects-subwoofer-controls is intentionally reduced to a
single column; the 761-1100px tablet rule uses two columns, so:

  ≤760px -> 1 column, 761-1100px -> 2 columns,
  >1100px -> 2 cards (2.1) or 3 cards (2.2, Dual-Mono and Stereo) sharing
  one equal-width track each while hugging their own content height.

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

            # Mobile trim mirror (narrowest phone): Level + Align share one
            # row, Polarity sits centered below spanning the full width —
            # no full-row stacking per control, no overflow.
            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(80)
            trim = page.evaluate("""
                (() => {
                    const box = (el) => { const b = el.getBoundingClientRect();
                        return { x: Math.round(b.x), top: Math.round(b.top),
                                 right: Math.round(b.right), width: Math.round(b.width) }; };
                    const group = document.querySelector('.effects-subwoofer-sub1-group');
                    const lvl = box(document.querySelector('#effects-subwoofer-level').closest('.stepper-control'));
                    const aln = box(document.querySelector('#effects-subwoofer-delay').closest('.stepper-control'));
                    const pol = box(document.querySelector('#effects-subwoofer-polarity'));
                    const g = box(group);
                    const pcs = getComputedStyle(group.querySelector('.effects-subwoofer-polarity-field')).gridColumn;
                    return { lvl, aln, pol, g, pcs };
                })()
            """)
            check(f"[390px] Level + Align share one row ({trim})",
                  abs(trim["lvl"]["top"] - trim["aln"]["top"]) <= 2)
            check(f"[390px] real gap between the steppers ({trim})",
                  trim["aln"]["x"] - trim["lvl"]["right"] >= 4)
            check(f"[390px] Polarity spans the full row below ({trim})",
                  trim["pcs"] == "1 / -1" and trim["pol"]["top"] > trim["aln"]["top"])
            check(f"[390px] Polarity centered ({trim})",
                  abs((trim["g"]["x"] + trim["g"]["width"] / 2) - (trim["pol"]["x"] + trim["pol"]["width"] / 2)) <= 2)
            check(f"[390px] no control overflows the card ({trim})",
                  trim["lvl"]["x"] >= trim["g"]["x"] - 1
                  and trim["aln"]["right"] <= trim["g"]["right"] + 1
                  and trim["pol"]["right"] <= trim["g"]["right"] + 1
                  and trim["pol"]["x"] >= trim["g"]["x"] - 1)

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

            # 2.2 pins the third card: Dual-Mono and Stereo show Global + Sub 1
            # + Sub 2 in three equal-width columns on desktop. Cards hug
            # their own content instead of stretching to one height.
            three_col = page.evaluate("""
                (() => {
                    const card = document.querySelector('.effects-card-subwoofer');
                    card.classList.add('is-subwoofer-22');
                    // Without a catalog the Sub 2 group starts hidden; unhide
                    // it for the comparison so all three cards measure.
                    const hidden = [...document.querySelectorAll('.effects-subwoofer-sub2-field.hidden')];
                    hidden.forEach((el) => el.classList.remove('hidden'));
                    const n = getComputedStyle(document.querySelector('.effects-subwoofer-controls'))
                        .gridTemplateColumns.trim().split(/\\s+/).length;
                    const rects = [...document.querySelectorAll(
                        '.effects-subwoofer-global-group, .effects-subwoofer-sub1-group, .effects-subwoofer-sub2-group')]
                        .map((el) => el.getBoundingClientRect());
                    hidden.forEach((el) => el.classList.add('hidden'));
                    card.classList.remove('is-subwoofer-22');
                    return { n, widths: rects.map((r) => Math.round(r.width)),
                             heights: rects.map((r) => Math.round(r.height)) };
                })()
            """)
            check(f"2.2 desktop uses 3 columns ({three_col})", three_col["n"] == 3)
            check(f"2.2 cards share one width ({three_col})",
                  max(three_col["widths"]) - min(three_col["widths"]) <= 2)
            check(f"2.2 cards are not stretched to one height ({three_col})",
                  max(three_col["heights"]) - min(three_col["heights"]) > 4)

            # Desktop 2.2: the timing readout tucks under the sub cards
            # (columns 2-3, second row) with its bottom edge on Global's
            # bottom instead of costing another full row.
            timing = page.evaluate("""
                (() => {
                    const card = document.querySelector('.effects-card-subwoofer');
                    card.classList.add('is-subwoofer-22');
                    const delayed = document.querySelector('#effects-subwoofer-derived-delays');
                    const wasHidden = delayed.classList.contains('hidden');
                    if (wasHidden) delayed.classList.remove('hidden');
                    // Snapshot everything before restoring classes:
                    // the computed style is live and would re-resolve.
                    const style = getComputedStyle(delayed);
                    const column = style.gridColumn;
                    const row = style.gridRow;
                    const t = delayed.getBoundingClientRect();
                    const g = document.querySelector('.effects-subwoofer-global-group').getBoundingClientRect();
                    const s = document.querySelector('.effects-subwoofer-sub1-group').getBoundingClientRect();
                    if (wasHidden) delayed.classList.add('hidden');
                    card.classList.remove('is-subwoofer-22');
                    return { column, row,
                             bottomDelta: Math.round(t.bottom - g.bottom),
                             belowSubs: Math.round(t.top - s.bottom) };
                })()
            """)
            check(f"timing tucks under the sub cards ({timing})",
                  timing["column"] == "2 / -1" and str(timing["row"]).startswith("2"))
            check(f"timing bottom edge meets Global bottom ({timing})",
                  abs(timing["bottomDelta"]) <= 2)
            check(f"timing sits below the sub cards ({timing})",
                  timing["belowSubs"] >= 0)

            # Single shared crossover plus one block per side, and the L/R
            # link switch only for a true Stereo sub pair. The per-side
            # blocks and the link start hidden (no Stereo pair routed) and
            # each block carries Frequency, Type, Slope and Main highpass.
            # The tab row (Link plus, while unlinked, the Sub L / Sub R tabs)
            # sits above the graph like the speaker-way tabs; the side blocks
            # carry no extra side heading.
            layout = page.evaluate("""
                (() => {
                    const card = document.querySelector('.effects-card-subwoofer');
                    const shared = document.querySelector('#effects-subwoofer-shared-crossover');
                    const sides = ['left', 'right'].map((side) =>
                        document.querySelector(`#effects-subwoofer-${side}-crossover`));
                    const tabrow = document.querySelector('#effects-subwoofer-tabrow');
                    const link = document.querySelector('#effects-subwoofer-link-wrap');
                    const tabs = document.querySelector('#effects-subwoofer-side-tabs');
                    const preview = document.querySelector('#effects-subwoofer-preview');
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
                        tabrowHidden: tabrow.classList.contains('hidden'),
                        linkHidden: link.classList.contains('hidden'),
                        linkInTabrow: link.parentElement === tabrow,
                        tabsHidden: tabs.classList.contains('hidden'),
                        tabsInTabrow: tabs.parentElement === tabrow,
                        tabCount: tabs.querySelectorAll('[data-sub-side]').length,
                        tabrowAboveGraph: tabrow.compareDocumentPosition(preview)
                            & Node.DOCUMENT_POSITION_FOLLOWING ? true : false,
                        tabsBeforeLink: !!(tabs.compareDocumentPosition(link)
                            & Node.DOCUMENT_POSITION_FOLLOWING),
                        sideLabels: document.querySelectorAll(
                            '.effects-subwoofer-crossover-side-label').length,
                        linkInGlobalCard: !!document.querySelector(
                            '#effects-subwoofer-global-group #effects-subwoofer-link-wrap'),
                        subFieldLabels: [...document.querySelectorAll(
                            '.effects-subwoofer-sub1-group .field-group label, .effects-subwoofer-sub2-group .field-group label')]
                            .map((el) => el.textContent),
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
            check("the tab row starts hidden (no Stereo pair routed)",
                  layout["tabrowHidden"] is True)
            check("link and tabs live in the tab row above the graph",
                  layout["linkInTabrow"] is True and layout["tabsInTabrow"] is True
                  and layout["tabrowAboveGraph"] is True)
            check("tabs come before the link, like the speaker tab row",
                  layout["tabsBeforeLink"] is True)
            check("no side headings inside the crossover blocks and no link in the Global card",
                  layout["sideLabels"] == 0 and layout["linkInGlobalCard"] is False)
            check("sub cards use short Trim-style labels (name lives in the card header)",
                  layout["subFieldLabels"] == ["Level", "Align", "Polarity"] * 2)
            # Dropdown alignment: Type, Slope, Polarity and Main highpass
            # read left-aligned like every other app select.
            align = page.evaluate("""(() => {
                const out = {};
                for (const id of ['effects-subwoofer-family', 'effects-subwoofer-slope',
                                  'effects-subwoofer-main-highpass', 'effects-subwoofer-polarity']) {
                    out[id] = getComputedStyle(document.getElementById(id)).textAlign;
                }
                return out;
            })()""")
            check(f"sub selects left-aligned ({align})",
                  len(align) == 4 and all(v in ('left', 'start') for v in align.values()))

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
