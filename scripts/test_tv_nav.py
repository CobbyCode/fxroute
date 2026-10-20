#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered behaviour tests for the device-independent remote navigation.

Boots the real page (static server + API stub, page JS active) in headless
Chromium and drives it with real keyboard events: spatial focus movement,
PageUp/PageDown tab switching, Backspace dialog handling, BrowserBack for
settings, ContextMenu for the input picker and the native text/slider
exceptions.

Run directly:

    python3 scripts/test_tv_nav.py
"""

import http.server
import pathlib
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8231

STUB = """
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), {
        status: 200, headers: { 'Content-Type': 'application/json' } }));
    if (u.includes('/api/status')) return json({ source: 'radio', status: 'Stopped',
        artist: '', title: '', album: '', output_peak_warning: false });
    if (u.includes('/api/audio/samplerate')) return json({ available: true,
        active_rate: 44100, supported_rates: [44100, 48000] });
    if (u.includes('/api/audio/outputs')) return json({ outputs: [
        { key: 'default', name: 'Default', active: true, channels: 2 }],
        active_key: 'default', output_mode: { mode: 'stereo' } });
    if (u.includes('/api/audio/output-state')) return json({ revision: 1,
        active_mode: 'stereo',
        capabilities: { filter_families: { 'linkwitz-riley': [12, 24] } },
        modes: { stereo: { enabled: true, crossover_enabled: false,
            topology: { channels: 2 }, processing: {} } } });
    if (u.includes('/api/audio/sources')) return json({ mode: 'app-playback', inputs: [] });
    if (u.includes('/api/radio/stations')) return json({ stations: [] });
    if (u.includes('/api/dsp/presets')) return json({ presets: [], presets_available: [] });
    if (u.includes('/api/library/status')) return json({ ready: true, folders: [] });
    if (u.includes('/api/measurements')) return json({ measurements: [] });
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    if (u.includes('/api/spotify/status')) return json({ available: false, installed: false,
        capabilities: {}, status: 'Stopped' });
    if (u.includes('/api/system/power')) return json({ available: false });
    if (u.includes('/api/settings')) return json({});
    return json({});
};
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _focus(page):
    return page.evaluate("""() => {
        const el = document.activeElement;
        if (!el) return null;
        return {
            id: el.id || '',
            tag: el.tagName,
            cls: String(el.className || ''),
            text: String(el.textContent || '').trim().slice(0, 40),
            ring: el.classList.contains('tv-nav-focus'),
        };
    }""")


def _browser_back(page):
    # Chromium's Playwright key table has no BrowserBack entry; dispatch the
    # event the remote's key produces in Firefox.
    page.evaluate("""() => {
        const target = document.activeElement || document.body;
        target.dispatchEvent(new KeyboardEvent('keydown', {
            key: 'BrowserBack', code: 'BrowserBack', bubbles: true, cancelable: true }));
    }""")


def _run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP  test_tv_nav.py (playwright not installed)")
        return 0

    server = _serve()
    checks = {"count": 0}

    def check(name, condition):
        assert condition, name
        checks["count"] += 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#tab-btn-radio")
            page.wait_for_timeout(700)

            radio = page.locator("#tab-btn-radio")
            check("radio tab starts active",
                  page.evaluate("document.getElementById('tab-btn-radio').getAttribute('aria-selected')") == "true")

            # PageDown / PageUp switch the main tab strip.
            page.keyboard.press("PageDown")
            page.wait_for_timeout(120)
            check("PageDown activates the next tab",
                  page.evaluate("document.getElementById('tab-btn-library').getAttribute('aria-selected')") == "true")
            check("PageDown focuses the new tab",
                  page.evaluate("document.activeElement.id") == "tab-btn-library")
            check("focus ring class is applied", _focus(page)["ring"])
            check("focus ring is visible",
                  page.evaluate("getComputedStyle(document.activeElement).outlineWidth") == "3px")

            page.keyboard.press("PageUp")
            page.wait_for_timeout(120)
            check("PageUp returns to the previous tab",
                  page.evaluate("document.getElementById('tab-btn-radio').getAttribute('aria-selected')") == "true")
            check("PageUp focuses the previous tab",
                  page.evaluate("document.activeElement.id") == "tab-btn-radio")

            # Arrow keys keep the existing tab-strip handling (Left/Right)
            # and use spatial navigation everywhere else.
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(120)
            check("ArrowRight still activates the next tab",
                  page.evaluate("document.getElementById('tab-btn-library').getAttribute('aria-selected')") == "true")
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(120)

            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(120)
            check("ArrowDown moves into the active panel",
                  page.evaluate("document.getElementById('tab-radio').contains(document.activeElement)"))
            check("ArrowDown target carries the focus ring", _focus(page)["ring"])
            check("ArrowDown target is a real control",
                  page.evaluate("document.activeElement.matches('input, button, select, textarea, [tabindex]')"))

            # Custom ARIA controls are activated by Enter/Space; native
            # buttons are not clicked twice.
            page.evaluate("""() => {
                const custom = document.createElement('div');
                custom.id = 'tv-nav-custom';
                custom.setAttribute('role', 'button');
                custom.tabIndex = 0;
                custom.textContent = 'Custom';
                custom.addEventListener('click', () => {
                    window.__customClicks = (window.__customClicks || 0) + 1;
                });
                const native = document.createElement('button');
                native.id = 'tv-nav-native';
                native.textContent = 'Native';
                native.addEventListener('click', () => {
                    window.__nativeClicks = (window.__nativeClicks || 0) + 1;
                });
                document.getElementById('tab-radio').append(custom, native);
            }""")
            page.focus("#tv-nav-custom")
            page.keyboard.press("Enter")
            check("Enter activates a custom ARIA control",
                  page.evaluate("window.__customClicks") == 1)
            page.keyboard.press("Space")
            check("Space activates a custom ARIA control",
                  page.evaluate("window.__customClicks") == 2)
            page.focus("#tv-nav-native")
            page.keyboard.press("Enter")
            check("Enter activates a native button once",
                  page.evaluate("window.__nativeClicks") == 1)

            # Text entry keeps every native key, including Backspace.
            page.focus("#station-search")
            page.keyboard.type("ab")
            page.keyboard.press("ArrowLeft")
            page.keyboard.type("X")
            check("ArrowLeft moves the caret in a text field",
                  page.input_value("#station-search") == "aXb")
            page.keyboard.press("Backspace")
            check("Backspace deletes text instead of closing UI",
                  page.input_value("#station-search") == "ab")

            # Sliders keep their left/right adjustment; up/down leaves them
            # (a TV remote has no Tab key to escape a focused slider).
            page.focus("#volume-slider")
            page.evaluate("document.getElementById('volume-slider').value = 50")
            before = page.input_value("#volume-slider")
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(120)
            check("ArrowRight adjusts the volume slider", page.input_value("#volume-slider") != before)
            check("slider keeps focus while adjusting",
                  page.evaluate("document.activeElement.id") == "volume-slider")
            page.keyboard.press("ArrowUp")
            page.wait_for_timeout(120)
            check("ArrowUp moves the focus off the slider",
                  page.evaluate("document.activeElement.id") != "volume-slider")
            check("the control reached from the slider has the ring", _focus(page)["ring"])

            # BrowserBack opens Settings, Backspace closes it.
            _browser_back(page)
            page.wait_for_timeout(150)
            check("BrowserBack opens settings",
                  page.evaluate("!document.getElementById('settings-panel').classList.contains('hidden')"))
            check("settings focus lands inside the dialog",
                  page.evaluate("document.getElementById('settings-panel').contains(document.activeElement)"))
            first = _focus(page)
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(120)
            check("arrows navigate inside the settings dialog",
                  page.evaluate("document.getElementById('settings-panel').contains(document.activeElement)"))
            check("arrow navigation changes the focused control", _focus(page)["id"] != first["id"])

            # Backspace in a settings text field edits the text, not the dialog.
            page.focus("#settings-device-name-input")
            page.fill("#settings-device-name-input", "abc")
            page.keyboard.press("Backspace")
            check("Backspace edits the settings text field",
                  page.input_value("#settings-device-name-input") == "ab")
            check("settings stays open while editing",
                  page.evaluate("!document.getElementById('settings-panel').classList.contains('hidden')"))

            page.focus("#close-settings")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(150)
            check("Backspace closes the settings dialog",
                  page.evaluate("document.getElementById('settings-panel').classList.contains('hidden')"))

            # BrowserBack toggles settings closed again.
            _browser_back(page)
            page.wait_for_timeout(120)
            _browser_back(page)
            page.wait_for_timeout(150)
            check("BrowserBack closes settings again",
                  page.evaluate("document.getElementById('settings-panel').classList.contains('hidden')"))

            # ContextMenu opens the input picker built from the footer select.
            page.evaluate("""() => {
                const switcher = document.getElementById('source-switcher');
                switcher.classList.remove('hidden');
                const select = document.getElementById('source-select');
                select.disabled = false;
                select.innerHTML = '<option value="app">App</option>'
                    + '<option value="optical">Optical</option>'
                    + '<option value="usb" disabled>USB</option>';
                select.value = 'app';
                window.__sourceChange = null;
                select.addEventListener('change', (event) => { window.__sourceChange = event.target.value; });
                document.getElementById('tab-btn-radio').click();
            }""")
            page.wait_for_timeout(120)
            page.keyboard.press("ContextMenu")
            page.wait_for_timeout(150)
            check("ContextMenu opens the input picker",
                  page.evaluate("!document.getElementById('source-picker').classList.contains('hidden')"))
            check("picker lists every source",
                  page.evaluate("document.getElementById('source-picker-list').children.length") == 3)
            check("picker starts on the active source",
                  page.evaluate("document.activeElement.textContent") == "App")

            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(120)
            check("arrows move to the next input",
                  page.evaluate("document.activeElement.textContent") == "Optical")

            # ContextMenu closes the picker again; Backspace also closes it.
            page.keyboard.press("ContextMenu")
            page.wait_for_timeout(120)
            check("ContextMenu closes the picker",
                  page.evaluate("document.getElementById('source-picker').classList.contains('hidden')"))
            page.keyboard.press("ContextMenu")
            page.wait_for_timeout(120)
            check("ContextMenu reopens the picker",
                  page.evaluate("!document.getElementById('source-picker').classList.contains('hidden')"))
            page.keyboard.press("Backspace")
            page.wait_for_timeout(120)
            check("Backspace closes the picker",
                  page.evaluate("document.getElementById('source-picker').classList.contains('hidden')"))

            # Enter selects the highlighted input and closes the picker.
            page.keyboard.press("ContextMenu")
            page.wait_for_timeout(120)
            page.keyboard.press("ArrowDown")
            page.wait_for_timeout(120)
            page.keyboard.press("Enter")
            page.wait_for_timeout(150)
            check("Enter selects the input and closes the picker",
                  page.evaluate("document.getElementById('source-picker').classList.contains('hidden')"))
            check("selection is forwarded to the footer select",
                  page.evaluate("document.getElementById('source-select').value") == "optical")
            check("the existing change handler ran",
                  page.evaluate("window.__sourceChange") == "optical")

            # No browser navigation was triggered by the navigation keys.
            check("navigation keys never change the URL",
                  page.evaluate("location.pathname") == "/static/index.html")
            check("no navigation module errors",
                  not [error for error in page_errors if "tv_nav" in error.lower()])

            browser.close()
    finally:
        server.shutdown()

    print(f"tv nav browser tests: ok ({checks['count']} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
