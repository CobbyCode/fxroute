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
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
    }));
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
        return json({ inputs: [], selection: {}, capture_available: false });
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
                    page.locator("#measurement-start").inner_text(),
                    page.locator("#measurement-repeat-start").inner_text(),
                    page.locator("#measurement-hybrid-open").inner_text(),
                ] == ["LR Stereo", "Start LR Repeat", "System Calibration"]
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
