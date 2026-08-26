#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser checks for the Measurement decision ladder and import labels."""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8216
VIEWPORTS = ((1440, 900), (390, 844), (320, 700))

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_measurement_ui_responsive.py (playwright not installed)")
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
const measurementJobs = new Map();
let measurementSequence = 0;
window.__measurementCalls = [];
window.__measurementControls = {
    completionDebugResolvers: [],
    delayCompletionDebug: false,
    completeLast() {
        const last = [...measurementJobs.values()].at(-1);
        if (last) {
            last.status = 'completed';
            last.message = 'Measurement finished.';
        }
    },
    releaseCompletionDebug() {
        const resolvers = this.completionDebugResolvers.splice(0);
        resolvers.forEach(resolve => resolve());
    },
};
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
    }));
    if (u.includes('/api/debug/21-runtime-state') && opts?.method === 'POST') {
        const payload = JSON.parse(String(opts.body || '{}'));
        if (payload.label === 'ui-directly-after-measurement-end' && window.__measurementControls.delayCompletionDebug) {
            return new Promise(resolve => window.__measurementControls.completionDebugResolvers.push(() => resolve(json({}))));
        }
        return json({});
    }
    const startJob = (kind, path) => {
        const body = opts?.body;
        const job = {
            id: `${kind}-${++measurementSequence}`,
            status: 'running',
            job_kind: kind,
            message: 'Measurement running…',
        };
        measurementJobs.set(job.id, job);
        window.__measurementCalls.push({ type: 'start', path, id: job.id, channel: body?.get?.('channel') || '' });
        return json({ job });
    };
    if (u.includes('/api/measurements/lr-repeat/start') && opts?.method === 'POST') {
        return startJob('lr-repeat', '/api/measurements/lr-repeat/start');
    }
    if (u.endsWith('/api/measurements/start') && opts?.method === 'POST') {
        return startJob('single', '/api/measurements/start');
    }
    if (u.includes('/api/measurements/jobs/')) {
        const parts = u.split('/');
        const finalPart = decodeURIComponent(parts.at(-1));
        if (finalPart === 'cancel') {
            const id = decodeURIComponent(parts.at(-2));
            const job = measurementJobs.get(id) || { id, job_kind: 'single' };
            job.status = 'cancelled';
            job.message = 'Measurement cancelled.';
            measurementJobs.set(id, job);
            window.__measurementCalls.push({ type: 'cancel', id });
            return json({ job });
        }
        const job = measurementJobs.get(finalPart) || { id: finalPart, status: 'running', job_kind: 'single', message: 'Measurement running…' };
        return json({ job });
    }
    if (u.includes('/api/audio/samplerate')) {
        return json({ available: true, active_rate: 44100, supported_rates: [44100, 48000] });
    }
    if (u.includes('/api/audio/outputs')) {
        return json({
            outputs: [{ key: 'default', name: 'Default', active: true }],
            selected_output: { key: 'default', name: 'Default', active: true },
            output_mode: { mode: 'stereo' },
        });
    }
    if (u.includes('/api/status')) return json({ source: 'local', status: 'Stopped' });
    if (u.includes('/api/dsp/presets')) return json({ available: true, preset_count: 0, active_preset: 'Neutral', presets: [] });
    if (u.includes('/api/measurements/settings')) {
        return json({ measurement_settings: { measurementSampleRate: 48000 } });
    }
    if (u.includes('/api/measurements/inputs')) {
        return json({
            inputs: [{ id: 'mic-1', label: 'Test microphone', channels: 2, persistent_id: 'mic-key', measurement_sample_rate: 48000 }],
            selection: { input_id: 'mic-1', persistent_id: 'mic-key', configured: true },
            capture_available: true,
        });
    }
    if (u.includes('/api/measurements')) return json({ measurements: [] });
    if (u.includes('/api/library/')) return json({ tracks: [], albums: [], folders: [] });
    if (u.includes('/api/streaming/providers')) return json({ providers: [] });
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    return realFetch(url, opts);
};
"""


def _open_measurement(page):
    page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
    page.wait_for_selector("#playback-bar", state="visible")
    page.evaluate("document.getElementById('offline-indicator')?.classList.add('hidden')")
    page.locator("#tab-btn-effects").click()
    page.locator("#effects-measure-open").click()
    page.locator("#measurement-panel").wait_for(state="visible")


def _wait_for_call(page, call_type, path=None, channel=None):
    page.wait_for_function(
        "([type, path, channel]) => window.__measurementCalls.some(call => "
        "call.type === type && (!path || call.path === path) && (!channel || call.channel === channel))",
        arg=[call_type, path, channel],
    )


def _cancel_from_sweep_button(page):
    assert page.locator("#measurement-sweep-toggle").inner_text() == "Cancel"
    page.locator("#measurement-sweep-toggle").click()
    _wait_for_call(page, "cancel")
    page.wait_for_function("() => document.getElementById('measurement-sweep-toggle')?.textContent === 'Start Sweep'")


def _run():
    server = _serve()
    checks = 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width, height in VIEWPORTS:
                page = browser.new_page(viewport={"width": width, "height": height})
                page.add_init_script(STUB)
                _open_measurement(page)

                assert page.locator(".measurement-workflow-label").all_text_contents()[:1] == ["Measurements"]
                assert page.locator("#measurement-sweep-toggle").inner_text() == "Start Sweep"
                assert not page.locator("#measurement-sweep-menu").is_visible()
                page.locator("#measurement-auto-sub-group").evaluate("element => element.classList.remove('hidden')")
                labels = page.locator(".measurement-workflow-label").all_text_contents()
                assert labels[:3] == ["Measurements", "Subwoofer", "Calibration"]
                checks += 4

                page.locator("#measurement-sweep-toggle").click()
                menu = page.locator("#measurement-sweep-menu")
                assert menu.is_visible()
                assert [
                    page.locator("[data-measurement-channel='left']").inner_text(),
                    page.locator("[data-measurement-channel='right']").inner_text(),
                    page.locator("[data-measurement-channel='stereo']").inner_text(),
                    page.locator("#measurement-repeat-start").inner_text(),
                    page.locator("#measurement-hybrid-open").inner_text(),
                ] == ["L", "R", "Stereo", "Start LR Repeat", "Advanced"]
                assert page.locator(".measurement-workflow-menu-choice").nth(1).inner_text() == (
                    "Start LR Repeat\nRun repeated left and right sweeps for improved precision."
                )
                assert page.locator(".measurement-workflow-menu-choice").nth(2).inner_text() == (
                    "Advanced\nCombined Speaker and Room Measurement"
                )
                menu_box = menu.bounding_box()
                assert menu_box is not None
                assert menu_box["x"] >= -1 and menu_box["x"] + menu_box["width"] <= width + 1
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
                checks += 3

                page.keyboard.press("Escape")
                assert not menu.is_visible()
                assert page.locator("#measurement-panel").is_visible()
                checks += 2

                page.locator("#measurement-sweep-toggle").click()
                page.locator("#measurement-hybrid-open").click()
                page.locator("#measurement-hybrid-panel").wait_for(state="visible")
                page.locator("#measurement-hybrid-close").click()
                assert page.evaluate("document.activeElement?.id") == "measurement-sweep-toggle"
                checks += 2

                for channel in ("left", "right", "stereo"):
                    page.locator("#measurement-sweep-toggle").click()
                    page.wait_for_function(f"() => !document.querySelector(\"[data-measurement-channel='{channel}']\")?.disabled")
                    page.locator(f"[data-measurement-channel='{channel}']").click()
                    _wait_for_call(page, "start", "/api/measurements/start", channel)
                    _cancel_from_sweep_button(page)
                checks += 6

                page.locator("#measurement-sweep-toggle").click()
                page.locator("#measurement-repeat-start").click()
                _wait_for_call(page, "start", "/api/measurements/lr-repeat/start")
                _cancel_from_sweep_button(page)
                checks += 2

                page.evaluate("window.__fxDebugRuntimeSnapshots = true; window.__measurementControls.delayCompletionDebug = true")
                page.locator("#measurement-sweep-toggle").click()
                page.locator("[data-measurement-channel='left']").click()
                _wait_for_call(page, "start", "/api/measurements/start", "left")
                page.evaluate("window.__measurementControls.completeLast()")
                page.wait_for_function("() => document.getElementById('measurement-sweep-toggle')?.textContent === 'Start Sweep'")
                page.wait_for_function("() => window.__measurementControls.completionDebugResolvers.length > 0")
                page.locator("#measurement-sweep-toggle").click()
                page.wait_for_function("() => !document.querySelector(\"[data-measurement-channel='right']\")?.disabled")
                page.locator("[data-measurement-channel='right']").click()
                _wait_for_call(page, "start", "/api/measurements/start", "right")
                assert page.locator("#measurement-sweep-toggle").inner_text() == "Cancel"
                page.evaluate("window.__measurementControls.releaseCompletionDebug()")
                page.wait_for_timeout(50)
                assert "Measurement running" in page.locator("#measurement-setup-status").inner_text()
                _cancel_from_sweep_button(page)
                page.evaluate("window.__measurementControls.delayCompletionDebug = false")
                checks += 5

                page.locator("#measurement-sweep-toggle").click()
                page.locator("#measurement-hybrid-open").click()
                page.locator("#measurement-hybrid-primary").click()
                _wait_for_call(page, "start", "/api/measurements/start")
                page.wait_for_function("() => document.getElementById('measurement-sweep-toggle')?.textContent === 'Cancel'")
                page.locator("#measurement-sweep-toggle").click()
                _wait_for_call(page, "cancel")
                page.wait_for_function("() => document.getElementById('measurement-sweep-toggle')?.textContent === 'Start Sweep'")
                page.locator("#measurement-hybrid-close").click()
                checks += 4

                page.locator("#measurement-close").click()
                _open_measurement(page)
                assert not page.locator("#measurement-sweep-menu").is_visible()
                checks += 1

                page.locator("#measurement-close").click()
                page.locator("#tab-btn-library").click()
                library_import = page.locator("#toggle-import")
                assert library_import.inner_text() == "Import"
                library_import.click()
                assert library_import.inner_text() == "Close Import"
                assert library_import.get_attribute("aria-expanded") == "true"
                assert page.locator("#library-import-panel").is_visible()
                assert "-" not in library_import.inner_text() and "x" not in library_import.inner_text().lower()
                library_box = library_import.bounding_box()
                assert library_box is not None and library_box["width"] >= 100
                checks += 4
                library_import.click()
                assert library_import.inner_text() == "Import"

                page.locator("#tab-btn-effects").click()
                effects_import = page.locator("#effects-toggle-import")
                effects_import.click()
                assert effects_import.inner_text() == "Close Import"
                assert effects_import.get_attribute("aria-expanded") == "true"
                assert page.locator("#effects-import-panel").is_visible()
                checks += 3
                effects_import.click()
                page.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"PASS  scripts/test_measurement_ui_responsive.py ({checks} checks)")


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:  # noqa: BLE001 - report browser failures clearly
        print("FAIL  scripts/test_measurement_ui_responsive.py")
        print(f"  {exc}")
        sys.exit(1)
