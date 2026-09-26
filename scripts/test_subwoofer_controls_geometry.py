#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright geometry contract for the subwoofer controls columns.

At max-width:760px .effects-subwoofer-controls is intentionally reduced to a
single column; the 761-1100px tablet rule uses two columns, so:

  ≤760px -> 1 column, 761-1100px -> 2 columns,
  >1100px -> 2 cards (2.1) or 3 cards (2.2, Dual-Mono and Stereo) sharing
  one equal-width track each while hugging their own content height.

At <=520px each card is a property list: label left, control right, and every
control of the card on one shared left edge and width.

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

            # Stacked cards: the split only starts where each card can hold
            # the trim row, so the single column runs up to ~800px. Below
            # that a card is ~290px of content, less than the two digit-safe
            # steppers need (~298px), and the split also made an 8px viewport
            # change halve the card.
            for width in (800, 760, 700, 600, 520):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                n = column_count()
                check(f"[{width}px] subwoofer controls 1 column (got {n})", n == 1)

            # Phone widths (<=520px): every group is one property list. Each
            # control sits on its own row right of its label, and all visible
            # controls of the card share one left edge and one width.
            def separated(a, b):
                return (a["right"] <= b["x"] + 1 or b["right"] <= a["x"] + 1
                        or a["bottom"] <= b["top"] + 1 or b["bottom"] <= a["top"] + 1)

            for width in (320, 390, 480):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
                rows = page.evaluate("""
                    (() => {
                        const box = (el) => { const b = el.getBoundingClientRect();
                            return { x: Math.round(b.x), top: Math.round(b.top),
                                     bottom: Math.round(b.bottom), right: Math.round(b.right),
                                     width: Math.round(b.width), mid: Math.round(b.top + b.height / 2) }; };
                        const card = document.querySelector('.effects-card-subwoofer');
                        const out = [];
                        for (const fg of card.querySelectorAll('.effects-subwoofer-control-group .field-group')) {
                            const ctrl = fg.querySelector('.stepper-control, select');
                            if (!ctrl || !ctrl.offsetParent) continue;
                            const group = fg.closest('.effects-subwoofer-control-group');
                            out.push({ id: (fg.querySelector('input, select') || {}).id,
                                       label: box(fg.querySelector('label')), ctrl: box(ctrl),
                                       group: box(group) });
                        }
                        const group = document.querySelector('.effects-subwoofer-sub1-group');
                        // A number input clips silently, so every value field has
                        // to be at least as wide as its own declared floor, and
                        // that floor has to hold the widest value the app can
                        // render ("-40.00" measures ~50px at this font size).
                        const valueOk = [...card.querySelectorAll('.stepper-input')]
                            .filter((i) => i.offsetParent).every((i) => {
                                const w = i.getBoundingClientRect().width;
                                return w + 0.5 >= parseFloat(getComputedStyle(i).minWidth) && w >= 60;
                            });
                        const btnOut = Math.max(0, ...[...card.querySelectorAll('.stepper-control')]
                            .filter((sc) => sc.offsetParent).map((sc) => {
                                const sbox = sc.getBoundingClientRect();
                                return Math.round(Math.max(...[...sc.children].map((c) => {
                                    const cb = c.getBoundingClientRect();
                                    return Math.max(cb.right - sbox.right, sbox.left - cb.left);
                                })));
                            }));
                        return { rows: out, valueOk, btnOut };
                    })()
                """)
                ctrls = [row["ctrl"] for row in rows["rows"]]
                check(f"[{width}px] property rows rendered ({len(ctrls)})", len(ctrls) >= 7)
                check(f"[{width}px] all controls share one left edge ({ctrls})",
                      max(c["x"] for c in ctrls) - min(c["x"] for c in ctrls) <= 1)
                check(f"[{width}px] all controls equally wide ({ctrls})",
                      max(c["width"] for c in ctrls) - min(c["width"] for c in ctrls) <= 1)
                check(f"[{width}px] controls stay inside their group ({rows})",
                      all(r["ctrl"]["x"] >= r["group"]["x"] and r["ctrl"]["right"] <= r["group"]["right"]
                          for r in rows["rows"]))
                check(f"[{width}px] each label sits left of its control, same row ({rows})",
                      all(r["label"]["right"] <= r["ctrl"]["x"] and r["label"]["x"] >= r["group"]["x"]
                          and abs(r["label"]["mid"] - r["ctrl"]["mid"]) <= 2 for r in rows["rows"]))
                check(f"[{width}px] no two controls overlap ({ctrls})",
                      all(separated(a, b) for i, a in enumerate(ctrls) for b in ctrls[i + 1:]))
                check(f"[{width}px] every stepper keeps its value field", rows["valueOk"])
                check(f"[{width}px] no stepper button leaves its own frame ({rows['btnOut']})",
                      rows["btnOut"] <= 0)

            # Above the phone range the two steppers share one row again.
            page.set_viewport_size({"width": 600, "height": 900})
            page.wait_for_timeout(80)
            wide = page.evaluate("""
                (() => {
                    const box = (el) => { const b = el.getBoundingClientRect();
                        return { x: Math.round(b.x), top: Math.round(b.top),
                                 right: Math.round(b.right), width: Math.round(b.width) }; };
                    return {
                        lvl: box(document.querySelector('#effects-subwoofer-level').closest('.stepper-control')),
                        aln: box(document.querySelector('#effects-subwoofer-delay').closest('.stepper-control')),
                    };
                })()
            """)
            check(f"[600px] Level + Align share one row ({wide})",
                  abs(wide["lvl"]["top"] - wide["aln"]["top"]) <= 2)
            check(f"[600px] both steppers equally wide ({wide})",
                  abs(wide["lvl"]["width"] - wide["aln"]["width"]) <= 1)
            check(f"[600px] real gap between the steppers ({wide})",
                  wide["aln"]["x"] - wide["lvl"]["right"] >= 4)
            page.set_viewport_size({"width": 390, "height": 900})
            page.wait_for_timeout(80)

            # Tablet 801-1100px: two columns.
            for width in (801, 900, 1100):
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
            # The tightest three-across split (1101px) and wide desktop.
            for width in (1101, 1440):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(80)
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
                        const align = getComputedStyle(document.querySelector('.effects-subwoofer-controls'))
                        .alignItems;
                    const groups = [...document.querySelectorAll(
                            '.effects-subwoofer-global-group, .effects-subwoofer-sub1-group, .effects-subwoofer-sub2-group')];
                        const rects = groups.map((el) => el.getBoundingClientRect());
                        // Label tops of the first and the last control row per card.
                        const rows = groups.map((group) => {
                            const tops = [...group.querySelectorAll('.field-group')]
                                .filter((fg) => fg.querySelector('.stepper-control, select')?.offsetParent)
                                .map((fg) => Math.round(fg.querySelector('label').getBoundingClientRect().top));
                            return [Math.min(...tops), Math.max(...tops)];
                        });
                        const titles = groups.map((group) => Math.round(
                            group.querySelector('.effects-subwoofer-group-label').getBoundingClientRect().left
                            - group.getBoundingClientRect().left));
                        const heightsOf = (sel) => [...card.querySelectorAll(sel)]
                            .filter((el) => el.offsetParent)
                            .map((el) => Math.round(el.getBoundingClientRect().height));
                        const controlHeights = heightsOf('.effects-subwoofer-controls .stepper-control, .effects-subwoofer-controls select');
                        hidden.forEach((el) => el.classList.add('hidden'));
                        card.classList.remove('is-subwoofer-22');
                        return { n, align, widths: rects.map((r) => Math.round(r.width)),
                                 heights: rects.map((r) => Math.round(r.height)), rows, titles,
                                 controlHeights: [...new Set(controlHeights)] };
                    })()
                """)
                check(f"[{width}px] 2.2 desktop uses 3 columns ({three_col})", three_col["n"] == 3)
                check(f"[{width}px] 2.2 cards share one width ({three_col})",
                      max(three_col["widths"]) - min(three_col["widths"]) <= 2)
                # Global, Sub 1 and Sub 2 share one vertical rhythm: Crossover /
                # Type sit on the Level / Align row, Slope / Main highpass on the
                # Polarity row, every control is equally tall, and the three
                # cards come out equally high while the grid still aligns them
                # to start (no stretching).
                check(f"[{width}px] 2.2 cards align to start and share one height ({three_col})",
                      three_col["align"] == "start"
                      and max(three_col["heights"]) - min(three_col["heights"]) <= 1)
                check(f"[{width}px] 2.2 first control rows line up ({three_col['rows']})",
                      max(r[0] for r in three_col["rows"]) - min(r[0] for r in three_col["rows"]) <= 1)
                check(f"[{width}px] 2.2 second control rows line up ({three_col['rows']})",
                      max(r[1] for r in three_col["rows"]) - min(r[1] for r in three_col["rows"]) <= 1)
                check(f"[{width}px] 2.2 card titles share one inset ({three_col['titles']})",
                      max(three_col["titles"]) - min(three_col["titles"]) <= 1)
                check(f"[{width}px] 2.2 controls share one height ({three_col['controlHeights']})",
                      len(three_col["controlHeights"]) == 1)

            # Desktop 2.2: the timing readout takes the second row under the
            # sub cards (columns 2-3) instead of costing another full row.
            # It used to end exactly on Global's bottom edge; the sub trims
            # are two rows now (the value fields keep the digit floor that
            # "-40.00" needs), so the readout follows them down instead. What
            # matters is that it shares the sub columns, sits below the sub
            # cards and never overlaps them.
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
            check(f"timing starts on the sub cards' bottom edge ({timing})",
                  0 <= timing["belowSubs"] <= 24)
            check(f"timing never overlaps the sub cards ({timing})",
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
                        bothTabText: document.querySelector('#effects-subwoofer-tab-both')?.textContent || '',
                        bothTabHidden: document.querySelector('#effects-subwoofer-tab-both')
                            ?.classList.contains('hidden') ?? true,
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
            check("the common Sub L/R tab exists for linked stereo",
                  layout["bothTabText"] == "Sub L/R")
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
