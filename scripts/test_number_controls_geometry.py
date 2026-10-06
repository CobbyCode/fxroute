#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright contract for the numeric controls of Tone, Crossover and Subwoofer.

Every stepper value field takes at most two decimals; Center, Surround and
Loudness Strength take whole numbers only. Minus and plus share one box that
is a little larger than before, and inside each parameter area the controls of
a column start at the same x (labels left-aligned with them) above the phone
range, where the cards keep their one-column property list.

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium. Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8204

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_number_controls_geometry.py (playwright not installed)")
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

# Without a catalog the cards start hidden: show Tone (all four effects on), Crossover and the 2.2 Subwoofer.
SHOW_JS = """
(() => {
    switchTab('effects');
    for (const selector of ['#effects-crossover-card', '.effects-card-subwoofer']) {
        document.querySelector(selector).classList.remove('hidden');
    }
    document.querySelector('.effects-card-subwoofer').classList.add('is-subwoofer-22');
    document.querySelectorAll('.effects-subwoofer-sub2-field.hidden').forEach((el) => el.classList.remove('hidden'));
    for (const key of ['upmixer', 'loudness', 'bass', 'tone-effect']) {
        const box = document.getElementById('effects-' + key + '-enabled');
        if (!box.checked) box.click();
    }
    document.getElementById('effects-upmixer-mode').value = 'hq';
    document.getElementById('effects-upmixer-row').classList.remove('hidden');
    document.getElementById('effects-upmixer-center-wrap').classList.remove('hidden');
    document.getElementById('effects-upmixer-fft-wrap').classList.remove('hidden');
})();
"""

MEASURE_JS = """
() => {
    const box = (el) => { const b = el.getBoundingClientRect(); return { left: b.left, right: b.right, top: b.top, width: b.width, height: b.height }; };
    // Where the label text itself starts (the label box may be wider than its text).
    const textLeft = (el) => { const range = document.createRange(); range.selectNodeContents(el); return range.getBoundingClientRect().left; };
    const visible = (el) => el.offsetParent !== null;
    const toneGroup = document.getElementById('effects-loudness-enabled').closest('section.effects-extras-group');
    const steppers = [...document.querySelectorAll('.stepper-control')].filter(visible).map((control) => {
        const [dec, inc] = [control.querySelector('[data-stepper="dec"]'), control.querySelector('[data-stepper="inc"]')];
        return { id: control.querySelector('input').id, control: box(control), dec: box(dec), inc: box(inc) };
    });
    // Controls (stepper or select) with the label that heads or precedes them.
    const controlOf = (host) => host.querySelector('.stepper-control, select');
    const tone = [...toneGroup.querySelectorAll('.effects-effect-line .effects-inline-control')].filter(visible)
        .map((pair) => ({ id: (pair.querySelector('input, select') || {}).id, control: box(controlOf(pair)),
                          label: box(pair.querySelector('.effects-inline-control-label')) }));
    const groups = (selector) => [...document.querySelectorAll(selector)].filter(visible).map((group) => ({
        id: group.id || group.className,
        box: box(group),
        fields: [...group.querySelectorAll('.field-group')].filter(visible).filter((fg) => controlOf(fg) && visible(controlOf(fg)))
            .map((fg) => ({ id: (fg.querySelector('input, select') || {}).id, control: box(controlOf(fg)),
                          label: { ...box(fg.querySelector('label')), textLeft: textLeft(fg.querySelector('label')) } })),
    }));
    return {
        steppers, tone, toneGroup: box(toneGroup),
        crossover: groups('#effects-crossover-card .crossover-trio-group'),
        sub: groups('.effects-card-subwoofer .effects-subwoofer-control-group'),
        docFits: document.documentElement.scrollWidth <= window.innerWidth + 1,
    };
}
"""


def clusters(values, tolerance=1.0):
    """Distinct x positions: values within the tolerance of each other count as one."""
    out = []
    for value in sorted(values):
        if not out or value - out[-1] > tolerance:
            out.append(value)
    return out


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
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(SHOW_JS)
            page.wait_for_timeout(300)

            # ---- Behaviour: decimals, whole numbers, symmetric steps ----
            def type_into(selector, text):
                field = page.locator(selector)
                field.click()
                field.press("Control+A")
                field.press_sequentially(text)
                return field

            def value(selector):
                return page.locator(selector).input_value()

            for selector, typed, expected in (
                ("#effects-bass-amount", "-1.2345", "-1.23"),
                ("#effects-crossover-level", "-4.47049", "-4.47"),
                ("#effects-crossover-delay", "7.8125", "7.81"),
                ("#effects-subwoofer-level", "-4.678", "-4.67"),
                ("#effects-subwoofer-delay", "-39.999", "-39.99"),
                ("#effects-subwoofer-sub2-delay", "0.005", "0.00"),
                ("#effects-crossover-frequency-lowpass", "3000.125", "3000.12"),
            ):
                type_into(selector, typed)
                check(f"{selector}: typed {typed} is held at two decimals ({value(selector)})", value(selector) == expected)

            for selector in ("#effects-upmixer-center", "#effects-upmixer-surround", "#effects-loudness-strength"):
                type_into(selector, "3.5")
                check(f"{selector}: no decimal point is taken ({value(selector)})", "." not in value(selector) and "," not in value(selector))
                type_into(selector, "-2")
                check(f"{selector}: no sign on a 0..10 field ({value(selector)})", "-" not in value(selector))
                type_into(selector, "1e1")
                check(f"{selector}: no exponent ({value(selector)})", "e" not in value(selector).lower())
                page.locator(selector).fill("7")
                page.locator(selector).locator("xpath=../button[@data-stepper='inc']").click()
                check(f"{selector}: plus moves one whole step ({value(selector)})", value(selector) == "8")
                page.locator(selector).locator("xpath=../button[@data-stepper='dec']").click()
                page.locator(selector).locator("xpath=../button[@data-stepper='dec']").click()
                check(f"{selector}: minus moves one whole step ({value(selector)})", value(selector) == "6")
            # A value written by script (autofill, paste) is cut the same way.
            check("a pasted fraction is cut to a whole number", page.evaluate("""() => {
                const field = document.getElementById('effects-loudness-strength');
                field.value = '2.75';
                field.dispatchEvent(new Event('input', { bubbles: true }));
                return field.value;
            }""") == "2")

            page.locator("#effects-bass-amount").fill("1.23")
            for _ in range(3):
                page.locator("#effects-bass-amount").locator("xpath=../button[@data-stepper='inc']").click()
            for _ in range(3):
                page.locator("#effects-bass-amount").locator("xpath=../button[@data-stepper='dec']").click()
            check(f"plus and minus are symmetric ({value('#effects-bass-amount')})", value("#effects-bass-amount") == "1.23")
            page.locator("#effects-subwoofer-delay").fill("0.3")
            page.locator("#effects-subwoofer-delay").locator("xpath=../button[@data-stepper='inc']").click()
            check(f"0.3 + 0.1 has no float residue ({value('#effects-subwoofer-delay')})", value("#effects-subwoofer-delay") == "0.40")

            # ---- Geometry ----
            for width in (1440, 1200, 1100, 900, 800, 600, 560, 540, 520, 480, 390, 320):
                page.set_viewport_size({"width": width, "height": 1000})
                page.wait_for_timeout(80)
                m = page.evaluate(MEASURE_JS)
                phone = width <= 520
                check(f"[{width}px] no horizontal page overflow", m["docFits"])

                check(f"[{width}px] steppers rendered ({len(m['steppers'])})", len(m["steppers"]) >= 12)
                for s in m["steppers"]:
                    dec, inc, control = s["dec"], s["inc"], s["control"]
                    check(f"[{width}px] {s['id']}: minus and plus are the same size",
                          abs(dec["width"] - inc["width"]) <= 0.5 and abs(dec["height"] - inc["height"]) <= 0.5)
                    check(f"[{width}px] {s['id']}: minus and plus sit symmetrically in the frame",
                          abs((dec["left"] - control["left"]) - (control["right"] - inc["right"])) <= 1.5)
                    # 26 px (34 px on phones) before. The buttons only give way where a card is tight:
                    # the three Subwoofer cards side by side below 1200px and the narrowest phones.
                    tight = (1101 <= width < 1200 and s["id"].startswith("effects-subwoofer")) or width < 390
                    wanted = 24 if tight else (34 if width <= 520 else 32)
                    check(f"[{width}px] {s['id']}: button hit area {dec['width']:.0f}x{dec['height']:.0f} (wanted {wanted}+)",
                          dec["height"] >= 44 and dec["width"] >= wanted)

                # Tone: the controls of a column start at one x, in every line of every effect.
                tone_lefts = clusters([t["control"]["left"] for t in m["tone"]])
                check(f"[{width}px] Tone controls sit on at most two columns ({tone_lefts})", len(tone_lefts) <= 2)
                check(f"[{width}px] Tone controls stay inside the card",
                      all(t["control"]["left"] >= m["toneGroup"]["left"] - 1 and t["control"]["right"] <= m["toneGroup"]["right"] + 1 for t in m["tone"]))
                first_column = [t for t in m["tone"] if abs(t["control"]["left"] - tone_lefts[0]) <= 1]
                check(f"[{width}px] Tone first column holds Mode, Center, Strength, Amount and Tone effect ({len(first_column)})", len(first_column) >= 5)
                if width >= 600:
                    # Labels left of their controls: one label column as well.
                    label_lefts = clusters([t["label"]["left"] for t in m["tone"]])
                    check(f"[{width}px] Tone labels sit on at most two columns ({label_lefts})", len(label_lefts) <= 2)

                if phone:
                    continue
                # Crossover and Subwoofer above the phone range: a control and its label start at
                # the same x, and the controls of one column share a width.
                for kind in ("crossover", "sub"):
                    for group in m[kind]:
                        for field in group["fields"]:
                            check(f"[{width}px] {kind}/{field['id']}: label text starts where its control starts",
                                  abs(field["label"]["textLeft"] - field["control"]["left"]) <= 1)
                            check(f"[{width}px] {kind}/{field['id']}: control stays inside its group",
                                  field["control"]["left"] >= group["box"]["left"] - 1 and field["control"]["right"] <= group["box"]["right"] + 1)
                        columns = clusters([f["control"]["left"] for f in group["fields"]])
                        if kind == "crossover":
                            # The group uses as many columns as its width holds (field + gap each, inside the group padding).
                            field_w = group["fields"][0]["control"]["width"]
                            fit = int((group["box"]["width"] - 24.4 + 12.8) // (field_w + 12.8))
                            check(f"[{width}px] crossover/{group['id']}: {len(columns)} columns for {len(group['fields'])} fields where {fit} fit",
                                  len(columns) == min(len(group["fields"]), fit))
                            if len(clusters([f["control"]["top"] for f in group["fields"]])) > 1:
                                check(f"[{width}px] crossover/{group['id']}: a field that wraps continues under a column",
                                      len(columns) < len(group["fields"]))
                        for column in columns:
                            widths = [f["control"]["width"] for f in group["fields"] if abs(f["control"]["left"] - column) <= 1]
                            check(f"[{width}px] {kind}/{group['id']}: column at {column:.0f} has one control width ({widths})",
                                  max(widths) - min(widths) <= 1)
                for group in m["crossover"]:
                    widths = [f["control"]["width"] for f in group["fields"]]
                    check(f"[{width}px] crossover/{group['id']}: all three fields have one width ({widths})", max(widths) - min(widths) <= 1)
                # Crossover groups stacked on each other keep their columns on the same x.
                for a in m["crossover"]:
                    for b in m["crossover"]:
                        if a is not b and abs(a["box"]["left"] - b["box"]["left"]) <= 1 and abs(a["box"]["width"] - b["box"]["width"]) <= 1:
                            check(f"[{width}px] stacked crossover groups share their columns",
                                  all(abs(x["control"]["left"] - y["control"]["left"]) <= 1
                                      for x, y in zip(a["fields"], b["fields"])))
            browser.close()
    finally:
        server.shutdown()
    print(f"number controls geometry: {passed} checks passed")


if __name__ == "__main__":
    _run()
