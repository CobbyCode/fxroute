#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser checks for the Measurement → Saved runs block on narrow viewports.

The saved-run cards used to run out of the dialog: a long run name or an
unbroken ALSA device string pushed the grid track past the card, and the
separate Close button overflowed the toolbar on a phone. This drives the real
page with deliberately hostile saved runs and asserts that

* nothing inside `.measurement-saved-list` leaves the Saved runs card,
* the document, dialog, card and list all stay free of horizontal overflow,
* the title, the area badge, the date and the controls stay readable,
* the redundant Close control is gone and the summary is the only toggle,
* Select all, per-run selection, Delete and Merge still work.
"""

import http.server
import pathlib
import sys
import threading
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8236
VIEWPORTS = ((320, 700), (390, 844), (768, 1024), (1440, 900))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_measurement_saved_runs_responsive.py (playwright not installed)")
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


# Hostile but realistic saved runs: a hyphen-dense run name, an ALSA id with
# no spaces at all, a Global target (the widest badge) and a legacy result
# without a target.
STUB = """
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
    addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
    removeEventListener(type, handler) { this.listeners[type] = (this.listeners[type] || []).filter(item => item !== handler); }
    send() {} close() {}
};
const DEVICE = 'USB_PnP_Sound_Device_Audio_hwid:1,0_snd-usb-0d05:0d05-snd-usb-0-2';
let saved = [
    {
        id: 'long-name', name: 'Livingroom-Subwoofer-Calibration-2026-09-16_Final_FINAL_v2',
        created_at: '2026-09-16T10:00:00Z', channel: 'left', measurement_kind: 'single',
        input_device: { id: 'mic-1', label: DEVICE },
        input_channels: { mic: 12, electrical_reference: 18 },
        traces: [{ kind: 'sweep-response', role: 'trusted', points: [[20, 0], [1000, 6], [20000, 1]] }],
        measurement_target: { schema: 'fxroute.measurement-target', version: 1, mode: 'stereo',
            device_key: 'default', bank_id: 'global', preset: 'Neutral', revision: 2,
            processing_fingerprint: 'fp-1', sample_rate_hz: 48000, channels: 4,
            roles: ['global'], measured_roles: ['global'] },
    },
    {
        id: 'legacy', name: 'Legacy sweep', created_at: '2026-09-15T10:00:00Z',
        channel: 'stereo', measurement_kind: 'single',
        input_device: { id: 'mic-1', label: DEVICE },
        traces: [{ kind: 'sweep-response', role: 'trusted', points: [[20, 0], [1000, 6], [20000, 1]] }],
    },
];
window.__measurementCalls = [];
window.__savedRunCalls = [];
const json = (d, status = 200) => Promise.resolve(new Response(JSON.stringify(d), {
    status, headers: { 'Content-Type': 'application/json' },
}));
window.confirm = () => true;
window.prompt = () => 'Merged by the responsive check';
window.fetch = (url, opts) => {
    const u = String(url);
    if (u.includes('/api/measurements/merge') && opts?.method === 'POST') {
        window.__savedRunCalls.push({ type: 'merge' });
        const body = JSON.parse(String(opts.body || '{}'));
        const merged = { ...saved[0], id: 'merged-1', name: body.name };
        saved = [...saved, merged];
        return json({ measurement: merged });
    }
    if (u.includes('/api/measurements/') && opts?.method === 'DELETE') {
        const id = decodeURIComponent(u.split('/api/measurements/')[1].split('?')[0]);
        window.__savedRunCalls.push({ type: 'delete', id });
        saved = saved.filter(item => item.id !== id);
        return json({ ok: true });
    }
    if (u.includes('/api/measurements') && !opts?.method) return json({ measurements: saved });
    const outputState = { revision: 1, active_mode: 'stereo', selected_bank: 'global' };
    const bank = { id: 'global', label: 'Global', roles: ['global'], channel_mode: 'stereo',
        preset: 'Neutral', preset_a: 'Neutral', preset_b: null, active_side: 'A', can_a: true, can_b: false };
    if (u.includes('/api/audio/output-state')) {
        return json({ status: 'ok', revision: 1, active_mode: 'stereo',
            device: { key: 'default', channels: 4, routing: { stereo: ['main_l', 'main_r', 'sub1', 'sub1'], crossover: [] } },
            modes: { stereo: { selected_bank: 'global', banks: { global: bank }, all_banks: {},
                    processing: {}, bass_management: { frequency_hz: 80, main_highpass_enabled: true },
                    extras: {}, topology: { mode: 'stereo', roles: ['main_l', 'main_r', 'sub1'],
                        sub_roles: ['sub1'], sub_mode: 'mono', left_ways: [], right_ways: [],
                        way_count: null, issues: [] } },
                crossover: { selected_bank: 'global', banks: { global: bank }, all_banks: {},
                    processing: {}, extras: {}, topology: { mode: 'crossover', roles: [], sub_roles: [],
                        sub_mode: 'none', left_ways: [], right_ways: [], way_count: null, issues: [] } } },
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
    if (u.includes('/api/measurements/settings')) return json({ measurement_settings: { measurementSampleRate: 48000 } });
    if (u.includes('/api/measurements/inputs')) return json({
        inputs: [{ id: 'mic-1', label: DEVICE, channels: 18, persistent_id: 'mic-key', measurement_sample_rate: 48000 }],
        selection: { input_id: 'mic-1', persistent_id: 'mic-key', configured: true }, capture_available: true });
    if (u.includes('/api/library/')) return json({ tracks: [], albums: [], folders: [] });
    if (u.includes('/api/streaming/providers')) return json({ providers: [] });
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    return realFetch(url, opts);
};
void outputState;
"""

# Every element inside the saved list must stay within the card's padding box,
# and the containers must not scroll horizontally.
GEOMETRY = """
() => {
    const card = document.querySelector('.measurement-card-list');
    const list = document.querySelector('.measurement-saved-list');
    const group = document.querySelector('.measurement-saved-group');
    const dialog = document.querySelector('.measurement-dialog');
    const cardBox = card.getBoundingClientRect();
    const style = getComputedStyle(card);
    const inner = {
        left: cardBox.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft),
        right: cardBox.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight),
    };
    const offenders = [];
    for (const node of list.querySelectorAll('*')) {
        const box = node.getBoundingClientRect();
        if (!box.width) continue;
        const over = Math.max(box.right - inner.right, inner.left - box.left);
        if (over > 0.5) {
            offenders.push({
                over: Math.round(over * 10) / 10,
                sel: node.tagName.toLowerCase() + '.' + String(node.className || '').trim().split(/\\s+/).join('.'),
                text: (node.textContent || '').trim().slice(0, 40),
            });
        }
    }
    // A box may also be narrower than its own text (nowrap inside a shrunk flex
    // item), which hides the spill from scrollWidth. The run-name link is the
    // one deliberate exception: it truncates with an ellipsis.
    const spills = [];
    for (const node of list.querySelectorAll('*')) {
        if (getComputedStyle(node).textOverflow === 'ellipsis') continue;
        if (node.scrollWidth > node.clientWidth + 1 && node.clientWidth > 0) {
            spills.push({ sel: node.tagName.toLowerCase() + '.' + String(node.className || '').trim().split(/\\s+/).join('.'),
                          by: node.scrollWidth - node.clientWidth,
                          text: (node.textContent || '').trim().slice(0, 40) });
        }
    }
    return {
        docOverflow: document.documentElement.scrollWidth - window.innerWidth,
        dialogOverflow: dialog.scrollWidth - dialog.clientWidth,
        cardOverflow: card.scrollWidth - card.clientWidth,
        listOverflow: list.scrollWidth - list.clientWidth,
        groupOverflow: group.scrollWidth - group.clientWidth,
        offenders, spills,
    };
}
"""


def _open_measurement(page):
    page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
    page.wait_for_selector("#playback-bar", state="visible")
    page.locator("#tab-btn-effects").click()
    page.locator("#effects-measure-open").click()
    page.locator("#measurement-panel").wait_for(state="visible")


def _assert_inside(report, width):
    for key in ("docOverflow", "dialogOverflow", "cardOverflow", "listOverflow", "groupOverflow"):
        assert report[key] <= 1, f"{key} = {report[key]} at {width}px"
    assert not report["offenders"], f"elements outside the card at {width}px: {report['offenders']}"
    assert not report["spills"], f"text spilling its own box at {width}px: {report['spills']}"


def _run():
    server = _serve()
    checks = 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.context.new_cdp_session(page).send("Network.setCacheDisabled", {"cacheDisabled": True})
                page.add_init_script(STUB)
                _open_measurement(page)

                summary = page.locator(".measurement-saved-group > summary")
                assert summary.inner_text() == "Open saved (2)", summary.inner_text()
                # The redundant Close control is gone for good.
                assert page.locator("[data-measurement-close-saved]").count() == 0
                assert page.locator(".measurement-saved-close-action").count() == 0
                checks += 2

                summary.click()
                page.wait_for_selector("[data-measurement-toggle='long-name']")
                page.wait_for_function(
                    "() => document.querySelector('.measurement-saved-group > summary')?.textContent === 'Close saved (2)'")
                checks += 2

                # The Global badge (the widest one) sits next to the truncated
                # run name and stays whole.
                badge = page.locator("[data-measurement-toggle='long-name'] ~ .measurement-area-badge")
                assert badge.inner_text() == "Global"
                assert page.locator("[data-measurement-toggle='legacy'] ~ .measurement-area-badge").count() == 0
                title = page.locator("[data-measurement-toggle='long-name'] ~ .measurement-list-title a")
                assert title.get_attribute("title").startswith("Livingroom-Subwoofer")
                assert title.inner_text() != title.get_attribute("title"), "the long name must be truncated, not wrapped"
                checks += 4

                # Nothing leaves the card while the run is unselected.
                _assert_inside(page.evaluate(GEOMETRY), width)
                checks += 1

                # Select all, then both bulk actions become live; re-measure.
                page.locator("[data-measurement-select-all]").check()
                page.wait_for_function("() => document.querySelectorAll('[data-measurement-toggle]:checked').length === 2")
                assert not page.locator("[data-measurement-merge-selected]").is_disabled()
                assert not page.locator("[data-measurement-delete-selected]").is_disabled()
                _assert_inside(page.evaluate(GEOMETRY), width)
                checks += 3

                # The toolbar and its controls stay inside the saved list.
                assert page.evaluate("""() => {
                    const list = document.querySelector('.measurement-saved-list');
                    const box = list.getBoundingClientRect();
                    return ['.measurement-saved-toolbar', '.measurement-select-all-toggle',
                            '.measurement-saved-toolbar-selection', '.measurement-saved-delete-action',
                            '.measurement-saved-merge-action'].every(sel => {
                        const node = list.querySelector(sel);
                        const b = node.getBoundingClientRect();
                        return b.left >= box.left - 0.5 && b.right <= box.right + 0.5;
                    });
                }"""), "toolbar controls leave the saved-runs container"
                checks += 1

                # Per-run selection still toggles the compare trace.
                toggle = page.locator("[data-measurement-toggle='legacy']")
                toggle.click()
                assert not toggle.is_checked()
                toggle.click()
                assert toggle.is_checked()
                _assert_inside(page.evaluate(GEOMETRY), width)
                checks += 3

                # Merge of the two selected runs.
                page.locator("[data-measurement-merge-selected]").click()
                page.wait_for_function("() => window.__savedRunCalls.some(call => call.type === 'merge')")
                page.wait_for_selector("[data-measurement-toggle='merged-1']")
                _assert_inside(page.evaluate(GEOMETRY), width)
                checks += 2

                # Delete of the selected runs; the accordion only opens/closes
                # through its summary.
                page.locator(".measurement-saved-group > summary").click()
                page.wait_for_function(
                    "() => document.querySelector('.measurement-saved-group > summary')?.textContent === 'Open saved (3)'")
                page.locator(".measurement-saved-group > summary").click()
                page.wait_for_selector("[data-measurement-toggle='merged-1']")
                page.locator("[data-measurement-select-all]").check()
                page.locator("[data-measurement-delete-selected]").click()
                page.wait_for_function("""() => {
                    const deleted = window.__savedRunCalls.filter(call => call.type === 'delete').length;
                    return deleted >= 3 && !document.querySelector('[data-measurement-toggle]');
                }""")
                assert page.locator(".measurement-saved-group").count() == 0, (
                    "an empty saved list must collapse the whole group")
                checks += 3
                page.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"PASS  scripts/test_measurement_saved_runs_responsive.py ({checks} checks)")


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:  # noqa: BLE001 - report browser failures clearly
        print("FAIL  scripts/test_measurement_saved_runs_responsive.py")
        print(f"  {type(exc).__name__}: {exc}")
        traceback.print_exc()
        sys.exit(1)
