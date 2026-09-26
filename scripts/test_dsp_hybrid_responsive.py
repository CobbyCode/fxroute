#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser contract for the DSP / hybrid responsive fixes.

Covers the three reported defects and the digit-safety rule that keeps them
from coming back:

1. the Advanced (hybrid) measurement wizard is cut off at 320px and its
   Start button leaves the panel,
2. the PEQ Add button leaves its row on a phone,
3. the sub/crossover Level and Alignment steppers must be flush, exactly as
   wide as each other and without overhanging their field,
4. no stepper value field may be narrower than the widest value the app can
   render ("-40.00", "20000"), because a number input clips silently.

Runs the real rendered page (static server + stubbed audio API) in headless
Chromium and skips cleanly when playwright is unavailable.
"""

import http.server
import pathlib
import re
import sys
import threading
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8241
WIDTHS = (320, 360, 390, 440, 480, 600, 768, 1024, 1440)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_dsp_hybrid_responsive.py (playwright not installed)")
    sys.exit(0)


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


STUB = """
(() => {
    const realFetch = window.fetch.bind(window);
    window.WebSocket = class {
        static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
        constructor() {
            this.readyState = 1; this.listeners = {};
            setTimeout(() => {
                if (typeof this.onopen === 'function') this.onopen({});
                (this.listeners.open || []).forEach(handler => handler({}));
            }, 0);
        }
        addEventListener(t, h) { (this.listeners[t] = this.listeners[t] || []).push(h); }
        removeEventListener(t, h) { this.listeners[t] = (this.listeners[t] || []).filter(i => i !== h); }
        send() {} close() {}
    };
    // A stereo system with two subs, so the sub cards and the 2.2 layout both
    // render, and device labels long enough to matter.
    const bank = (id, label, roles, mode) => ({ id, label, roles, channel_mode: mode,
        preset: 'Neutral', preset_a: 'Neutral', preset_b: null,
        active_side: 'A', can_a: true, can_b: false });
    const modeCatalog = (mode) => ({
        selected_bank: 'global',
        banks: {
            global: bank('global', 'Global', ['global'], 'stereo'),
            main: bank('main', 'Main L/R', ['main_l', 'main_r'], 'stereo'),
            sub1: bank('sub1', 'Sub 1', ['sub1'], 'mono'),
        },
        all_banks: { preset: null, preset_a: 'Neutral', preset_b: null, active_side: null, can_a: false, can_b: false },
        processing: {},
        bass_management: { frequency_hz: 80, main_highpass_enabled: true },
        extras: {},
        topology: {
            mode,
            roles: mode === 'stereo' ? ['main_l', 'main_r', 'sub1', 'sub2'] : [],
            sub_roles: mode === 'stereo' ? ['sub1', 'sub2'] : [],
            sub_mode: mode === 'stereo' ? 'stereo' : 'none',
            left_ways: [], right_ways: [], way_count: null, issues: [],
        },
    });
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), {
        status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    window.fetch = (url, opts) => {
        const u = String(url);
        if (u.includes('/api/audio/output-state')) {
            return json({ status: 'ok', revision: 1, active_mode: 'stereo',
                device: { key: 'default', channels: 6, routing: { stereo: ['main_l', 'main_r', 'sub_l', 'sub_r', 'sub1', 'sub2'], crossover: [] } },
                modes: { stereo: modeCatalog('stereo'), crossover: modeCatalog('crossover') },
                capabilities: { modes: ['stereo', 'crossover'],
                    roles: { stereo: ['main_l', 'main_r', 'sub_l', 'sub_r', 'sub1', 'sub2'],
                        crossover: ['left_low', 'left_mid', 'left_high', 'right_low', 'right_mid', 'right_high'] },
                    filter_families: { 'linkwitz-riley': [12, 24, 36, 48, 60, 72],
                        butterworth: [6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72],
                        bessel: [6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72] },
                    max_slope_db_oct: 72, max_biquads_per_output: 32 } });
        }
        if (u.includes('/api/audio/samplerate')) return json({ available: true, active_rate: 48000, supported_rates: [48000] });
        if (u.includes('/api/audio/outputs')) return json({ outputs: [{ key: 'default', name: 'Default', active: true }],
            selected_output: { key: 'default', name: 'Default', active: true }, output_mode: { mode: 'stereo' } });
        if (u.includes('/api/status')) return json({ source: 'local', status: 'Stopped' });
        if (u.includes('/api/dsp/presets')) return json({ available: true, preset_count: 0, active_preset: 'Neutral', presets: [] });
        if (u.includes('/api/measurements')) return json({ measurements: [] });
        if (u.includes('/api/measurements/inputs')) return json({ inputs: [], selection: {},
            capture_available: false });
        if (u.includes('/api/library/')) return json({ tracks: [], albums: [], folders: [] });
        if (u.includes('/api/streaming/providers')) return json({ providers: [] });
        if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
        return realFetch(url, opts);
    };
})();
"""

# Worst-case values per field, taken from the real min/max/step attributes:
# crossover 20..20000 step 1, way trim -80..24 step 0.1, sub level -24..12
# step 0.1, sub alignment -40..40 step 0.1. The app renders two decimals.
WORST_CASE = {
    "effects-crossover-frequency-highpass": "20000",
    "effects-crossover-frequency-lowpass": "20000",
    "effects-crossover-level": "-80.00",
    "effects-crossover-delay": "-40.00",
    "effects-subwoofer-level": "-24.00",
    "effects-subwoofer-sub2-level": "-24.00",
    "effects-subwoofer-delay": "-40.00",
    "effects-subwoofer-sub2-delay": "-40.00",
    "effects-subwoofer-frequency-number": "200",
}

AUDIT = """
(values) => {
    const problems = {clipped: [], overhang: [], offCard: [], squeezed: []};
    const card = document.querySelector('.effects-card-subwoofer');
    const cross = document.querySelector('.effects-card-crossover');
    const cs = getComputedStyle(card);
    const box = card.getBoundingClientRect();
    const inner = {left: box.left + parseFloat(cs.borderLeftWidth) + parseFloat(cs.paddingLeft),
                   right: box.right - parseFloat(cs.borderRightWidth) - parseFloat(cs.paddingRight)};

    for (const [id, value] of Object.entries(values)) {
        for (const input of document.querySelectorAll('#' + CSS.escape(id))) {
            if (!input.offsetParent) continue;
            input.value = value;
        }
    }
    for (const host of [card, cross]) {
        if (!host) continue;
        for (const group of host.querySelectorAll('.stepper-control')) {
            if (!group.offsetParent) continue;
            const gb = group.getBoundingClientRect();
            // Any child (button, value, unit) outside the stepper's own frame.
            for (const child of group.children) {
                const cb = child.getBoundingClientRect();
                const out = Math.max(cb.right - gb.right, gb.left - cb.left);
                if (out > 0.5) {
                    problems.overhang.push({host: host.className.slice(0, 24),
                        cls: String(child.className).slice(0, 20), out: Math.round(out)});
                }
            }
            const input = group.querySelector('.stepper-input');
            if (input && input.scrollWidth > input.clientWidth + 1) {
                problems.clipped.push({id: input.id || '(no id)', value: input.value,
                    field: Math.round(input.clientWidth), text: input.scrollWidth});
            }
        }
        for (const fg of host.querySelectorAll('.field-group')) {
            const ctrl = fg.querySelector('.stepper-control, select');
            if (!ctrl || !ctrl.offsetParent) continue;
            // A phone property row has no box of its own (display: contents);
            // its control then answers to the group it sits in.
            const frame = getComputedStyle(fg).display === 'contents'
                ? fg.closest('.effects-subwoofer-control-group') : fg;
            const fb = frame.getBoundingClientRect();
            const cb = ctrl.getBoundingClientRect();
            const out = Math.max(cb.right - fb.right, fb.left - cb.left);
            if (out > 0.5) {
                problems.offCard.push({host: host.className.slice(0, 24),
                    label: (fg.querySelector('label') || {}).textContent?.trim().slice(0, 14),
                    out: Math.round(out)});
            }
            // A control narrower than the text it must show.
            const prev = ctrl.style.cssText;
            ctrl.style.width = 'min-content';
            const need = ctrl.getBoundingClientRect().width;
            ctrl.style.cssText = prev;
            if (cb.width < need - 0.5) {
                problems.squeezed.push({host: host.className.slice(0, 24),
                    label: (fg.querySelector('label') || {}).textContent?.trim().slice(0, 14),
                    got: Math.round(cb.width), need: Math.round(need)});
            }
            const outOfCard = Math.max(cb.right - inner.right, inner.left - cb.left);
            if (outOfCard > 0.5) {
                problems.offCard.push({host: host.className.slice(0, 24), label: 'leaves the card',
                    out: Math.round(outOfCard)});
            }
        }
    }

    // Level and Alignment must match exactly, wherever they live.
    const pairs = [];
    for (const sel of ['.effects-subwoofer-channel-group', '#effects-crossover-trim-group']) {
        for (const group of document.querySelectorAll(sel)) {
            if (!group.offsetParent) continue;
            const widths = [...group.querySelectorAll('.field-group')]
                .map((fg) => ({label: (fg.querySelector('label') || {}).textContent?.trim().slice(0, 8),
                               w: Math.round(fg.querySelector('.stepper-control, select')
                                   ?.getBoundingClientRect().width || 0)}));
            const level = widths.find((x) => x.label === 'Level');
            const align = widths.find((x) => x.label === 'Align');
            if (level && align) pairs.push({group: sel, level: level.w, align: align.w});
        }
    }
    return {problems, pairs};
}
"""

HYBRID = """
() => {
    const panel = document.getElementById('measurement-hybrid-panel');
    const dialog = panel.querySelector('.hybrid-measurement-dialog');
    const out = {docOverflow: document.documentElement.scrollWidth - window.innerWidth,
                 panelOverflow: panel.scrollWidth - panel.clientWidth,
                 dialogOverflow: dialog.scrollWidth - dialog.clientWidth, escapes: []};
    for (const sel of ['.hybrid-wizard-grid', '.hybrid-room-view', '.hybrid-step-card',
                       '.hybrid-action-row', '.hybrid-action-row button']) {
        for (const node of panel.querySelectorAll(sel)) {
            if (!node.offsetParent && getComputedStyle(node).position !== 'sticky') continue;
            const b = node.getBoundingClientRect();
            const out_ = Math.max(b.right - window.innerWidth, -b.left);
            if (out_ > 0.5) {
                out.escapes.push({sel, out: Math.round(out_), text: (node.textContent || '')
                    .trim().replace(/\\s+/g, ' ').slice(0, 24)});
            }
        }
    }
    const primary = panel.querySelector('.hybrid-action-row button:last-child');
    if (primary) {
        const b = primary.getBoundingClientRect();
        out.primary = {right: Math.round(b.right), width: Math.round(b.width),
                       fullyVisible: b.right <= window.innerWidth + 0.5 && b.left >= -0.5};
    }
    return out;
}
"""

PEQ_ROW = """
() => {
    const row = document.querySelector('.effects-peq-add-row');
    const field = row.querySelector('.effects-peq-mode-field');
    const select = field.querySelector('select');
    const button = row.querySelector('.btn-inline');
    const rb = row.getBoundingClientRect();
    const fb = field.getBoundingClientRect();
    const bb = button.getBoundingClientRect();
    const sb = select.getBoundingClientRect();
    const overlap = !(fb.right <= bb.left + 1 || bb.right <= fb.left + 1
                      || fb.bottom <= bb.top + 1 || bb.bottom <= fb.top + 1);
    return {rowOverflow: row.scrollWidth - row.clientWidth,
            fieldOverRow: Math.max(fb.right - rb.right, rb.left - fb.left),
            buttonOverRow: Math.max(bb.right - rb.right, rb.left - bb.left),
            selectClipped: select.scrollWidth > select.clientWidth + 1,
            overlap};
}
"""


def _show_effects(page):
    page.evaluate("() => { switchTab('effects'); }")
    page.wait_for_selector(".effects-card-subwoofer .stepper-control", state="visible")
    page.wait_for_timeout(200)


def _check_css_contract(check):
    """Guard the declarations that caused these defects, cheaply.

    The browser pass above proves the behaviour; these assertions fail with a
    readable message when a single declaration regresses, instead of only
    showing up as a few clipped pixels at one width.
    """
    effects = (ROOT / "static" / "css" / "_effects.css").read_text()
    measurement = (ROOT / "static" / "css" / "_measurement.css").read_text()
    responsive = (ROOT / "static" / "css" / "_responsive.css").read_text()

    # A bare 1fr track is floored at min-content; the wizard stacked on a
    # bare 1fr and dragged the room view and action row out of the panel.
    check("hybrid wizard stacks on minmax(0, 1fr), never a bare 1fr",
          not re.search(r"\.hybrid-wizard-grid\s*\{[^}]*grid-template-columns:\s*1fr\s*;",
                        responsive),
          responsive)
    check("hybrid action row tracks may shrink",
          re.search(r"\.hybrid-action-row\s*\{[^}]*minmax\(0,\s*0\.35fr\)"
                    r"[^}]*minmax\(0,\s*1fr\)", measurement))
    # Every value field keeps the digit floor, and the floor is declared
    # after the base rule so it is not silently overridden.
    check("stepper value field declares a digit floor",
          re.search(r"\.stepper-control \.stepper-input\s*\{[^}]*min-width:\s*4rem", effects))
    check("stepper value field may grow with its control",
          re.search(r"\.stepper-control \.stepper-input\s*\{[^}]*flex:\s*1 1 auto", effects))
    check("no value field returns to min-width: 0", "min-width: 0" not in
          re.search(r"#tab-effects \.stepper-control \.stepper-input\s*\{[^}]*\}", effects).group(0))
    # The five-digit frequency override must come after the base rule.
    base_at = effects.index("#tab-effects .stepper-control .stepper-input {")
    check("no dead override before the base stepper rule",
          ".stepper-input-freq" not in effects[:base_at])
    check("crossover type select fits its widest option",
          re.search(r"effects-crossover-type-select\s*\{[^}]*min\(100%,\s*9\.5rem\)", effects))
    check("sub trim stepper fills its field and stays flush",
          re.search(r"\.effects-subwoofer-control-group \.stepper-control\s*\{[^}]*width:\s*100%", effects))
    check("unit block has one width so neighbouring steppers match",
          re.search(r"\.stepper-unit\s*\{[^}]*min-width:\s*1\.5rem", effects))
    check("PEQ add row wraps instead of overrunning",
          re.search(r"\.effects-peq-add-row\s*\{[^}]*flex-wrap:\s*wrap", effects))
    check("sub cards stay stacked until the split fits",
          re.search(r"@media \(min-width: 761px\) and \(max-width: 800px\)"
                    r"\s*\{[^}]*\.effects-subwoofer-controls", responsive))


def _run():
    server = _serve()
    passed = 0

    def check(name, condition, detail=""):
        assert condition, f"{name} {detail}"
        nonlocal passed
        passed += 1

    _check_css_contract(check)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width in WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.add_init_script(STUB)
                page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
                page.wait_for_selector("#playback-bar", state="visible")
                page.wait_for_timeout(400)
                _show_effects(page)
                data = page.evaluate(AUDIT, WORST_CASE)
                p = data["problems"]

                check(f"[{width}px] no clipped stepper value", not p["clipped"], p["clipped"])
                check(f"[{width}px] no stepper child leaves its frame", not p["overhang"], p["overhang"])
                check(f"[{width}px] no control overhangs its field or card", not p["offCard"], p["offCard"])
                check(f"[{width}px] no control squeezed below its content", not p["squeezed"], p["squeezed"])
                for pair in data["pairs"]:
                    check(f"[{width}px] Level/Alignment equally wide in {pair['group']}",
                          abs(pair["level"] - pair["align"]) <= 1, pair)

                # The PEQ add row: no overflow, no overlap, intact mode select.
                page.evaluate("""() => document.querySelectorAll('.effects-disclosure-body')
                    .forEach(n => n.classList.remove('hidden'))""")
                page.wait_for_timeout(150)
                peq = page.evaluate(PEQ_ROW)
                check(f"[{width}px] PEQ add row has no horizontal overflow",
                      peq["rowOverflow"] <= 1, peq)
                check(f"[{width}px] PEQ mode field and Add button stay in the row",
                      peq["fieldOverRow"] <= 1 and peq["buttonOverRow"] <= 1, peq)
                check(f"[{width}px] PEQ mode select shows its options",
                      not peq["selectClipped"], peq)
                check(f"[{width}px] PEQ field and Add button never overlap",
                      not peq["overlap"], peq)

                # The hybrid wizard.
                page.click("#effects-measure-open")
                page.wait_for_selector("#measurement-panel", state="visible")
                page.click("#measurement-sweep-toggle")
                page.wait_for_timeout(150)
                page.click("#measurement-hybrid-open")
                page.wait_for_selector("#measurement-hybrid-panel", state="visible")
                page.wait_for_timeout(300)
                hyb = page.evaluate(HYBRID)
                check(f"[{width}px] hybrid panel has no horizontal overflow",
                      hyb["panelOverflow"] <= 1 and hyb["dialogOverflow"] <= 1, hyb)
                check(f"[{width}px] no hybrid element leaves the viewport",
                      not hyb["escapes"], hyb)
                check(f"[{width}px] hybrid Start button fully visible",
                      hyb["primary"]["fullyVisible"], hyb["primary"])
                page.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"PASS  scripts/test_dsp_hybrid_responsive.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:  # noqa: BLE001 - report browser failures clearly
        print("FAIL  scripts/test_dsp_hybrid_responsive.py")
        print(f"  {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
