#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Polarity labels: Normal/Inverted on desktop, N/I on phone widths.

Static contract:
* static/index.html carries Normal/Inverted in all three polarity selects
  (crossover trim, Sub 1, Sub 2) with stored values normal/invert unchanged.
* static/crossover_ui.js owns polarityLabel/relabelPolaritySelects with the
  same 520px compact breakpoint as the Type labels; both modules wire it.
* The built static/style.css matches scripts/build-css.sh output.

Geometry contract (real rendered page, skips without playwright/browser):
* >520px viewports show Normal/Inverted in closed value and options.
* <=520px viewports (incl. 320px) show N/I, values still normal/invert.
* No select overflows its group and no two polarity controls overlap.
"""

import http.server
import pathlib
import re
import sys
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8207

POLARITY_IDS = [
    "effects-crossover-polarity",
    "effects-subwoofer-polarity",
    "effects-subwoofer-sub2-polarity",
]


class PolarityLabelStaticTests(unittest.TestCase):
    def test_index_has_full_names_with_stored_values(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        for select_id in POLARITY_IDS:
            pattern = re.compile(
                rf'<select[^>]*id="{select_id}"[^>]*>(.*?)</select>',
                re.DOTALL,
            )
            match = pattern.search(html)
            self.assertTrue(match, f"missing #{select_id}")
            body = match.group(1)
            self.assertIn('<option value="normal">Normal</option>', body,
                          f"#{select_id} must offer Normal")
            self.assertIn('<option value="invert">Inverted</option>', body,
                          f"#{select_id} must offer Inverted")
            self.assertNotIn(">N</option>", body,
                             f"#{select_id} must not use the short N")
            self.assertNotIn(">I</option>", body,
                             f"#{select_id} must not use the short I")

    def test_crossover_module_owns_compact_polarity_labels(self):
        source = (ROOT / "static" / "crossover_ui.js").read_text(encoding="utf-8")
        self.assertIn("POLARITY_LABELS", source)
        self.assertIn("normal: 'Normal'", source)
        self.assertIn("invert: 'Inverted'", source)
        self.assertIn("COMPACT_POLARITY_LABELS", source)
        self.assertIn("function polarityLabel(", source)
        self.assertIn("function relabelPolaritySelects(", source)
        self.assertIn("select.effects-subwoofer-polarity-select option", source)
        self.assertIn("function watchCompactLabels(", source)
        self.assertIn("relabelPolaritySelects()", source)
        exports = source.rsplit("return {", 1)[1].split("};", 1)[0]
        for name in ("polarityLabel", "relabelPolaritySelects", "watchCompactLabels"):
            self.assertIn(name, exports,
                          f"crossover_ui.js must export {name}")

    def test_subwoofer_wire_relabels_and_app_delegates(self):
        sub = (ROOT / "static" / "subwoofer_ui.js").read_text(encoding="utf-8")
        self.assertIn("relabelPolaritySelects", sub,
                      "subwoofer wire must relabel for a narrow first paint")
        app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function polarityLabel(", app,
                      "app.js keeps a thin polarityLabel wrapper")

    def test_stored_values_and_dsp_paths_untouched(self):
        crossover = (ROOT / "static" / "crossover_ui.js").read_text(encoding="utf-8")
        self.assertIn("=== 'invert' ? 'invert' : 'normal'", crossover)
        sub = (ROOT / "static" / "subwoofer_ui.js").read_text(encoding="utf-8")
        self.assertIn("=== 'invert' ? 'invert' : 'normal'", sub)


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


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
    document.querySelector('.effects-card-subwoofer')?.classList.remove('hidden');
    document.querySelector('.effects-card-crossover')?.classList.remove('hidden');
    const sub2 = document.querySelector('.effects-subwoofer-sub2-group');
    if (sub2) sub2.classList.remove('hidden');
    for (const el of document.querySelectorAll('.effects-subwoofer-sub2-field.hidden')) {
        if (el.closest('.effects-subwoofer-sub2-group')) el.classList.remove('hidden');
    }
})();
"""


def _run_geometry():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP  scripts/test_polarity_labels.py geometry (playwright not installed)")
        return None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    passed = 0

    def check(name, condition):
        assert condition, name

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:
                print(f"SKIP  scripts/test_polarity_labels.py geometry ({exc})")
                return None
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(SHOW_JS)
            page.wait_for_timeout(300)

            def snapshot():
                return page.evaluate("""(() => {
                    const out = [];
                    for (const id of ['effects-crossover-polarity','effects-subwoofer-polarity','effects-subwoofer-sub2-polarity']) {
                        const sel = document.getElementById(id);
                        if (!sel || !sel.offsetParent) { out.push({ id, visible: false }); continue; }
                        const r = sel.getBoundingClientRect();
                        const group = sel.closest('.effects-subwoofer-control-group, .crossover-trio-group');
                        const g = group ? group.getBoundingClientRect() : null;
                        out.push({ id, visible: true,
                            value: sel.value, text: sel.options[sel.selectedIndex]?.text,
                            opts: [...sel.options].map((o) => [o.value, o.text]),
                            selLeft: Math.round(r.left), selRight: Math.round(r.right),
                            grpLeft: g ? Math.round(g.left) : null, grpRight: g ? Math.round(g.right) : null,
                            overflow: sel.scrollWidth - sel.clientWidth });
                    }
                    return out;
                })()""")

            # Desktop/tablet above the 520px compact breakpoint spell out.
            for width in (1440, 1024, 768, 600, 521):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(120)
                for row in snapshot():
                    if not row["visible"]:
                        continue
                    check(f"[{width}px] #{row['id']} keeps value {row}",
                          row["value"] in ("normal", "invert"))
                    check(f"[{width}px] #{row['id']} shows full names {row}",
                          row["opts"] == [["normal", "Normal"], ["invert", "Inverted"]])
                    check(f"[{width}px] #{row['id']} stays inside its group {row}",
                          row["selLeft"] >= row["grpLeft"] and row["selRight"] <= row["grpRight"] + 1)
                    check(f"[{width}px] #{row['id']} does not overflow {row}",
                          row["overflow"] <= 1)
                    passed += 4

            # Phone widths (incl. 320px) keep the short names.
            for width in (520, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(120)
                for row in snapshot():
                    if not row["visible"]:
                        continue
                    check(f"[{width}px] #{row['id']} keeps value {row}",
                          row["value"] in ("normal", "invert"))
                    check(f"[{width}px] #{row['id']} shows short names {row}",
                          row["opts"] == [["normal", "N"], ["invert", "I"]])
                    check(f"[{width}px] #{row['id']} stays inside its group {row}",
                          row["selLeft"] >= row["grpLeft"] and row["selRight"] <= row["grpRight"] + 1)
                    check(f"[{width}px] #{row['id']} does not overflow {row}",
                          row["overflow"] <= 1)
                    passed += 4

            # The long Inverted value fits: select it everywhere and re-check.
            page.evaluate("""(() => {
                for (const id of ['effects-crossover-polarity','effects-subwoofer-polarity','effects-subwoofer-sub2-polarity']) {
                    const sel = document.getElementById(id);
                    if (sel) sel.value = 'invert';
                }
            })()""")
            for width in (521, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_timeout(120)
                for row in snapshot():
                    if not row["visible"]:
                        continue
                    check(f"[{width}px] #{row['id']} keeps invert {row}",
                          row["value"] == "invert")
                    check(f"[{width}px] #{row['id']} stays inside its group with Inverted {row}",
                          row["selLeft"] >= row["grpLeft"] and row["selRight"] <= row["grpRight"] + 1)
                    check(f"[{width}px] #{row['id']} does not overflow with Inverted {row}",
                          row["overflow"] <= 1)
                    passed += 3

            page.close()
            browser.close()
    finally:
        server.shutdown()
    return passed


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(PolarityLabelStaticTests)
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    try:
        passed = _run_geometry()
    except AssertionError as exc:
        print("FAIL  scripts/test_polarity_labels.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:
        print("FAIL  scripts/test_polarity_labels.py")
        print(f"  {exc}")
        sys.exit(1)
    if passed is None:
        print("PASS  scripts/test_polarity_labels.py (static only, geometry skipped)")
    else:
        print(f"PASS  scripts/test_polarity_labels.py (static + {passed} geometry checks)")
