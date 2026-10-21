#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered behaviour tests for the device-independent remote navigation.

Boots the real page (static server + API stub, page JS active) in headless
Chromium and drives it with real keyboard events: spatial focus movement,
PageUp (next tab) / PageDown (previous tab) switching, Backspace dialog
handling, BrowserBack for settings, ContextMenu for the input picker and the native text/slider
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


def _serve(port=PORT):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
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

    def check(name, condition, detail=None):
        assert condition, f"{name} [{detail!r}]" if detail is not None else name
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

            # PageUp goes right to the next tab, PageDown left to the previous.
            page.keyboard.press("PageUp")
            page.wait_for_timeout(120)
            check("PageUp activates the next tab",
                  page.evaluate("document.getElementById('tab-btn-library').getAttribute('aria-selected')") == "true")
            check("PageUp focuses the new tab",
                  page.evaluate("document.activeElement.id") == "tab-btn-library")
            check("focus ring class is applied", _focus(page)["ring"])
            check("focus ring is visible",
                  page.evaluate("getComputedStyle(document.activeElement).outlineWidth") == "2px")

            page.keyboard.press("PageDown")
            page.wait_for_timeout(120)
            check("PageDown returns to the previous tab",
                  page.evaluate("document.getElementById('tab-btn-radio').getAttribute('aria-selected')") == "true")
            check("PageDown focuses the previous tab",
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

            # A row that already handles Enter must not be clicked a second
            # time by the generic activation.
            page.evaluate("""() => {
                const own = document.createElement('div');
                own.id = 'tv-nav-own-enter';
                own.tabIndex = 0;
                own.textContent = 'Own Enter';
                own.addEventListener('keydown', (event) => {
                    if (event.key !== 'Enter') return;
                    event.preventDefault();
                    window.__ownEnter = (window.__ownEnter || 0) + 1;
                });
                own.addEventListener('click', () => {
                    window.__ownClicks = (window.__ownClicks || 0) + 1;
                });
                document.getElementById('tab-radio').append(own);
            }""")
            page.focus("#tv-nav-own-enter")
            page.keyboard.press("Enter")
            check("a row with its own Enter handler runs once",
                  page.evaluate("window.__ownEnter") == 1)
            check("the generic activation does not click that row again",
                  page.evaluate("window.__ownClicks || 0") == 0)

            # Enter must also activate focusable rows without an ARIA role and
            # toggle checkboxes, which do not activate on Enter natively.
            page.evaluate("""() => {
                const row = document.createElement('div');
                row.id = 'tv-nav-row';
                row.tabIndex = 0;
                row.textContent = 'Row';
                row.addEventListener('click', () => {
                    window.__rowClicks = (window.__rowClicks || 0) + 1;
                });
                const box = document.createElement('input');
                box.type = 'checkbox';
                box.id = 'tv-nav-checkbox';
                document.getElementById('tab-radio').append(row, box);
            }""")
            page.focus("#tv-nav-row")
            page.keyboard.press("Enter")
            check("Enter activates a focusable row without a role",
                  page.evaluate("window.__rowClicks") == 1)
            page.focus("#tv-nav-checkbox")
            page.keyboard.press("Enter")
            check("Enter toggles a checkbox like a click",
                  page.evaluate("document.getElementById('tv-nav-checkbox').checked"))

            # A closed select must not own the arrows: otherwise the remote
            # can never leave e.g. "Preset 3 optional".
            page.evaluate("""() => {
                const select = document.createElement('select');
                select.id = 'tv-nav-select';
                select.innerHTML = '<option value="a">A</option>'
                    + '<option value="b">B</option>';
                select.value = 'a';
                document.getElementById('tab-radio').append(select);
            }""")
            page.focus("#tv-nav-select")
            page.keyboard.press("ArrowUp")
            page.wait_for_timeout(120)
            check("ArrowUp leaves a select instead of trapping the remote",
                  page.evaluate("document.activeElement.id") != "tv-nav-select")
            check("leaving a select does not change its value",
                  page.evaluate("document.getElementById('tv-nav-select').value") == "a")

            # Backspace uses the visible back control of a detail view, not a
            # hidden back button of another tab that comes first in the DOM.
            page.evaluate("""() => {
                const back = document.createElement('button');
                back.id = 'tv-nav-test-back';
                back.className = 'album-detail-back';
                back.textContent = 'Back';
                back.addEventListener('click', () => {
                    window.__backClicks = (window.__backClicks || 0) + 1;
                });
                document.getElementById('tab-radio').appendChild(back);
            }""")
            page.focus("#tv-nav-native")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(120)
            check("Backspace activates the visible back control",
                  page.evaluate("window.__backClicks") == 1)
            page.evaluate("document.getElementById('tv-nav-test-back').remove()")

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

            # Text fields own the arrows, so Escape is the exit.
            page.keyboard.press("Escape")
            page.wait_for_timeout(120)
            check("Escape leaves a text field",
                  page.evaluate("document.activeElement.id") != "station-search")
            check("the control reached by Escape carries the focus ring",
                  _focus(page)["ring"])
            check("Escape keeps the typed value",
                  page.input_value("#station-search") == "ab")

            # A slider reached by the remote is a navigation stop until OK
            # arms it; then Left/Right adjust, and OK, Back or Up/Down end it.
            page.focus("#volume-slider")
            page.evaluate("document.getElementById('volume-slider').value = 50")
            before = page.input_value("#volume-slider")
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(120)
            check("ArrowRight leaves an unarmed slider unchanged", page.input_value("#volume-slider") == before)
            check("ArrowRight stops at the footer edge instead of adjusting",
                  page.evaluate("document.activeElement.id") == "volume-slider"
                  and not page.evaluate("document.getElementById('volume-slider').classList.contains('tv-nav-editing')"))
            page.focus("#volume-slider")
            page.keyboard.press("Enter")
            check("OK arms the slider",
                  page.evaluate("document.getElementById('volume-slider').classList.contains('tv-nav-editing')"))
            page.keyboard.press("ArrowRight")
            page.wait_for_timeout(120)
            check("ArrowRight adjusts the armed slider", page.input_value("#volume-slider") != before)
            check("slider keeps focus while adjusting",
                  page.evaluate("document.activeElement.id") == "volume-slider")
            page.keyboard.press("Enter")
            check("OK again releases the slider",
                  not page.evaluate("document.getElementById('volume-slider').classList.contains('tv-nav-editing')"))
            held = page.input_value("#volume-slider")
            page.keyboard.press("ArrowLeft")
            page.wait_for_timeout(120)
            check("a released slider no longer changes", page.input_value("#volume-slider") == held)
            page.focus("#volume-slider")
            page.keyboard.press("Enter")
            page.keyboard.press("Backspace")
            check("Back releases the slider and keeps the focus on it",
                  page.evaluate("document.activeElement.id") == "volume-slider"
                  and not page.evaluate("document.getElementById('volume-slider').classList.contains('tv-nav-editing')"))
            page.keyboard.press("Enter")
            page.keyboard.press("ArrowUp")
            page.wait_for_timeout(120)
            check("ArrowUp moves the focus off the armed slider",
                  page.evaluate("document.activeElement.id") != "volume-slider")
            check("an armed slider is released when the focus leaves it",
                  not page.evaluate("document.getElementById('volume-slider').classList.contains('tv-nav-editing')"))
            check("the control reached from the slider has the ring", _focus(page)["ring"])
            # Tab focus and the mouse keep native slider keys.
            page.keyboard.press("Tab")
            page.focus("#volume-slider")
            page.evaluate("document.getElementById('volume-slider').value = 50")
            page.keyboard.press("ArrowRight")
            check("a Tab-focused slider keeps native Left/Right",
                  page.input_value("#volume-slider") != "50")

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

            # Alt+ArrowLeft is the desktop back shortcut some remotes send;
            # it must open settings instead of navigating the browser back.
            page.keyboard.down("Alt")
            page.keyboard.press("ArrowLeft")
            page.keyboard.up("Alt")
            page.wait_for_timeout(150)
            check("Alt+ArrowLeft opens settings instead of navigating back",
                  page.evaluate("!document.getElementById('settings-panel').classList.contains('hidden')"))
            check("Alt+ArrowLeft keeps the URL",
                  page.evaluate("location.pathname") == "/static/index.html")
            page.focus("#close-settings")
            page.keyboard.press("Backspace")
            page.wait_for_timeout(150)

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

            # A status-driven re-render can replace the focused row. The
            # navigation keeps the focus on the nearest remaining control
            # instead of dropping it to <body>.
            page.evaluate("document.activeElement.remove()")
            page.wait_for_timeout(150)
            check("focus survives a list re-render",
                  page.evaluate("document.getElementById('source-picker').contains(document.activeElement)"))
            check("focus moves to the nearest remaining row", _focus(page)["text"] == "App", _focus(page))
            check("the restored row carries the focus ring", _focus(page)["ring"])

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

            # Escape and a backdrop click also close the picker.
            page.keyboard.press("ContextMenu")
            page.wait_for_timeout(120)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            check("Escape closes the picker",
                  page.evaluate("document.getElementById('source-picker').classList.contains('hidden') && !FXRouteModal.topRoot()"))
            page.keyboard.press("ContextMenu")
            page.wait_for_timeout(120)
            page.evaluate("FXRouteModal.topRoot().querySelector('.manage-overlay-backdrop').click()")
            page.wait_for_timeout(150)
            check("a backdrop click closes the picker",
                  page.evaluate("document.getElementById('source-picker').classList.contains('hidden') && !FXRouteModal.topRoot()"))

            # A keyboard context menu (Menu/Compose key, Shift+F10) can arrive
            # only as a contextmenu event under Wayland. It must be suppressed
            # and open the picker; a mouse right-click stays native.
            page.wait_for_timeout(800)
            # The status poll may have hidden the line-input switcher again.
            page.evaluate("document.getElementById('source-switcher').classList.remove('hidden')")
            prevented = page.evaluate("""() => {
                const event = new MouseEvent('contextmenu', {
                    button: 0, bubbles: true, cancelable: true });
                document.body.dispatchEvent(event);
                return event.defaultPrevented;
            }""")
            page.wait_for_timeout(150)
            check("a keyboard context menu is suppressed", prevented)
            check("a keyboard context menu opens the picker",
                  page.evaluate("!document.getElementById('source-picker').classList.contains('hidden')"),
                  page.evaluate("({top: FXRouteModal.topRoot()?.id, source: document.getElementById('source-select').outerHTML, switcher: document.getElementById('source-switcher').className})"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            page.wait_for_timeout(800)
            right_clicked = page.evaluate("""() => {
                const event = new MouseEvent('contextmenu', {
                    button: 2, bubbles: true, cancelable: true });
                document.body.dispatchEvent(event);
                return event.defaultPrevented;
            }""")
            page.wait_for_timeout(150)
            check("a mouse right-click keeps the native context menu", not right_clicked)
            check("a mouse right-click does not open the picker",
                  page.evaluate("document.getElementById('source-picker').classList.contains('hidden')"))

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
