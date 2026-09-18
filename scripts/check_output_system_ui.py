#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser smoke check for the Output System UI (settings, bank, tile).

Serves the live worktree demo (scripts/serve_demo.py) and drives it in
headless Chromium: opens settings, selects the multichannel device,
switches to Crossover, then verifies the bank selector and the Crossover
tile (tabs, graph, controls) on the DSP tab. Fails on uncaught page
errors or console errors from the new modules.

Run manually after output-system UI changes; not part of the test suite:

    python3 scripts/check_output_system_ui.py [--shots DIR]
"""

import argparse
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8765


def wait_for_server(timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("demo server did not come up")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", default="/tmp/opencode/fxroute-output-shots")
    args = parser.parse_args()
    shots = pathlib.Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    server = subprocess.Popen(
        [sys.executable, "scripts/serve_demo.py"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    problems: list[str] = []
    try:
        wait_for_server()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
            page.on("console", lambda message: problems.append(f"console.{message.type}: {message.text}")
                    if message.type == "error" else None)
            page.goto(f"http://localhost:{PORT}/", wait_until="networkidle")
            page.wait_for_selector("#playback-bar", state="visible", timeout=15000)

            def check(name: str, condition: bool) -> None:
                print(("ok   " if condition else "FAIL ") + name)
                if not condition:
                    failures.append(name)

            # One mode, independent crossover, one hardware routing editor.
            page.evaluate("toggleSettingsPanel(true)")
            page.wait_for_selector("#settings-output-mode-select:not([disabled])", state="visible", timeout=5000)
            check("one routing editor", page.locator(".settings-routing-grid").count() == 1)
            check("mode choices", page.locator("#settings-output-mode-select option").all_text_contents() == ['Stereo', 'Stereo + Sub'])
            check("independent crossover visible", page.locator("#settings-crossover-select").is_visible())

            # Multichannel device, then Crossover mode.
            # NOTE: Playwright select_option races the settings re-render
            # (rebuilt <option> nodes detach mid-action and the change is
            # lost); value+dispatch exercises the same app listener.
            def pick(selector, value):
                page.evaluate(
                    """([sel, val]) => {
                        const el = document.querySelector(sel);
                        el.value = val;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    [selector, value])

            scarlett_value = page.evaluate(
                """Array.from(document.querySelectorAll('#settings-output-select option'))
                    .find(o => o.textContent.includes('Scarlett'))?.value || ''""")
            check("scarlett device offered", bool(scarlett_value))
            if not scarlett_value:
                return 1
            pick("#settings-output-select", scarlett_value)
            page.wait_for_timeout(1500)
            pick("#settings-output-mode-select", "stereo-sub")
            page.wait_for_timeout(700)
            check("one row per hardware port", page.locator("#settings-routing-grid select").count() == 18)
            options = page.locator("#settings-routing-out-1 option").all_text_contents()
            check("full-band and sub roles", 'Main L' in options and 'Sub L' in options and 'Sub 2' in options and 'Low L' not in options)
            pick("#settings-crossover-select", "on")
            page.wait_for_timeout(700)
            options = page.locator("#settings-routing-out-1 option").all_text_contents()
            check("crossover replaces Main and keeps subs", 'Main L' not in options and 'Low L' in options and 'Low-Mid R' in options and 'Sub L' in options)
            for index, role in enumerate(['left_low', 'left_mid', 'left_high', 'right_low', 'right_mid', 'right_high', 'sub_l', 'sub_r'], 1):
                pick(f"#settings-routing-out-{index}", role)
                page.wait_for_timeout(250)
            page.wait_for_timeout(1500)
            topology = page.locator("#os-topology").inner_text()
            check(f"topology follows routing ({topology})", "3-Way" in topology and "Stereo subs" in topology)
            page.screenshot(path=str(shots / "os-settings.png"))

            # DSP tab: bank selector and crossover tile.
            page.evaluate("toggleSettingsPanel(false)")
            page.evaluate("document.getElementById('tab-btn-effects').click()")
            page.wait_for_selector("#effects-bank-select", state="visible", timeout=5000)
            bank_options = page.locator("#effects-bank-select option").all_inner_texts()
            check(f"bank selector lists ways ({len(bank_options)} options)", len(bank_options) >= 7)
            info = page.locator("#effects-bank-info").inner_text()
            check(f"bank info shows preset ({info})", "Listening" in info)
            card = page.locator("#effects-crossover-card")
            check("crossover card visible", card.is_visible())
            order = page.evaluate(
                """[...document.querySelectorAll('#tab-effects .effects-grid > section')]
                    .map(el => el.id || el.querySelector('h3')?.textContent)""")
            check(f"dsp card order ({order})",
                  order == ['effects-crossover-card', 'Subwoofer', 'A/B compare',
                            'Output extras', 'Combine', 'Create PEQ preset'])
            tops = page.evaluate(
                """Object.fromEntries([...document.querySelectorAll('#tab-effects .effects-grid > section')]
                    .map(el => [el.id || el.querySelector('h3')?.textContent, Math.round(el.getBoundingClientRect().top)]))""")
            check(f"crossover visually between create-preset and subwoofer ({tops})",
                  tops['A/B compare'] < tops['Combine'] < tops['effects-crossover-card'] < tops['Subwoofer'])
            span = page.evaluate(
                "getComputedStyle(document.getElementById('effects-crossover-card')).gridColumn")
            check(f"crossover spans full width ({span})", span == '1 / -1')
            page.locator("#effects-crossover-starter").click()
            page.wait_for_timeout(1200)
            tabs = page.locator("#effects-crossover-tabs button")
            check(f"six way tabs ({tabs.count()} found)", tabs.count() == 6)
            check("crossover graph is a canvas",
                  page.evaluate("document.getElementById('effects-crossover-graph')?.tagName") == 'CANVAS')
            graph_box = page.locator("#effects-crossover-graph").bounding_box()
            check(f"graph height compact ({graph_box['height']:.0f}px)", 120 <= graph_box['height'] <= 220)
            variance = page.evaluate(
                """(() => {
                    const c = document.getElementById('effects-crossover-graph');
                    if (!c || c.tagName !== 'CANVAS') return -1;
                    const x = c.getContext('2d');
                    const d = x.getImageData(0, 0, c.width, c.height).data;
                    let s = 0, n = 0;
                    for (let i = 0; i < d.length; i += 401 * 4) { s += (d[i] + d[i + 1] + d[i + 2]) / 3; n += 1; }
                    const mean = s / Math.max(1, n);
                    let v = 0;
                    for (let i = 0; i < d.length; i += 401 * 4) { const g = (d[i] + d[i + 1] + d[i + 2]) / 3; v += (g - mean) ** 2; }
                    return v / Math.max(1, n);
                })()""")
            check("crossover graph painted", variance > 0)
            backing = page.evaluate(
                """(() => { const c = document.getElementById('effects-crossover-graph');
                    const r = c.getBoundingClientRect();
                    return { w: c.width, cssW: Math.round(r.width), dpr: window.devicePixelRatio || 1 }; })()""")
            check(f"graph backing matches display ({backing})",
                  abs(backing['w'] - backing['cssW'] * backing['dpr']) <= 2)
            page.set_viewport_size({"width": 1000, "height": 900})
            page.wait_for_timeout(600)
            backing2 = page.evaluate(
                """(() => { const c = document.getElementById('effects-crossover-graph');
                    const r = c.getBoundingClientRect();
                    return { w: c.width, cssW: Math.round(r.width), dpr: window.devicePixelRatio || 1 }; })()""")
            check(f"graph follows viewport resize ({backing2})",
                  abs(backing2['w'] - backing2['cssW'] * backing2['dpr']) <= 2)
            page.set_viewport_size({"width": 1440, "height": 900})
            page.wait_for_timeout(600)
            check("group labels span full row",
                  page.evaluate("getComputedStyle(document.querySelector('#effects-crossover-card .crossover-trio-group > .effects-subwoofer-group-label')).flexBasis") == "100%")
            steppers = page.locator("#effects-crossover-card .crossover-control-groups .stepper-control")
            check(f"crossover uses sub-style steppers ({steppers.count()} found)", steppers.count() == 4)
            units = page.locator("#effects-crossover-card .crossover-control-groups .stepper-unit").all_text_contents()
            check(f"stepper units ({units})", units == ['Hz', 'Hz', 'dB', 'ms'])
            groups = page.locator("#effects-crossover-card .crossover-control-groups > div").all_text_contents()
            check(f"three control groups ({len(groups)} found)", len(groups) == 3)
            pol_cls = page.locator("#effects-crossover-polarity").get_attribute("class") or ""
            check("polarity select shares sub-tile style", "effects-subwoofer-polarity-select" in pol_cls)
            overflowing = page.evaluate(
                """(() => {
                    const card = document.getElementById('effects-crossover-card').getBoundingClientRect();
                    return [...document.querySelectorAll('#effects-crossover-card .crossover-control-groups .url-input')]
                        .filter((el) => el.getBoundingClientRect().width > 0)
                        .filter(el => { const r = el.getBoundingClientRect();
                            return r.left < card.left - 1 || r.right > card.right + 1; }).length;
                })()""")
            check(f"no control overflows the card ({overflowing} found)", overflowing == 0)
            check("family select populated",
                  page.locator("#effects-crossover-family option").count() >= 3)
            check("slope select populated",
                  page.locator("#effects-crossover-slope option").count() >= 2)
            page.screenshot(path=str(shots / "os-crossover.png"))

            # Switch the bank and the way tab; both must update without errors.
            pick("#effects-bank-select", "left_mid")
            page.wait_for_timeout(800)
            check("bank info follows selection",
                  "Room" in page.locator("#effects-bank-info").inner_text()
                  or "Neutral" in page.locator("#effects-bank-info").inner_text())
            page.locator("[data-crossover-way='right_high']").click()
            page.wait_for_timeout(400)
            check("way tab activates",
                  page.locator("[data-crossover-way='right_high']").get_attribute("class")
                  is not None
                  and "is-active" in (page.locator("[data-crossover-way='right_high']").get_attribute("class") or ""))

            # Clear one filter, then let the starter refill only the gap.
            page.evaluate(
                "applyOutputSystemMutation('set_processing',"
                " { mode: 'stereo-sub', role: 'left_mid', lowpass: null }, false, { quiet: true })")
            page.wait_for_timeout(1200)
            page.locator("#effects-crossover-starter").click()
            page.wait_for_timeout(1500)
            starter_back = page.evaluate(
                """fetch('/api/audio/output-state').then(r => r.json())
                    .then(j => JSON.stringify(j.modes[j.active_mode].processing.left_mid.lowpass))""")
            check(f"starter refills only the gap ({starter_back})",
                  '"frequency_hz":2500' in starter_back)

            # Editing a way control persists to the backend state.
            page.evaluate(
                """(() => {
                    const slope = document.getElementById('effects-crossover-slope');
                    slope.value = '48';
                    slope.dispatchEvent(new Event('change', { bubbles: true }));
                })()""")
            page.wait_for_timeout(1500)
            slope_back = page.evaluate(
                """fetch('/api/audio/output-state').then(r => r.json())
                    .then(j => {
                        const active = document.querySelector('.crossover-tab.is-active')
                            ?.dataset.crossoverWay || 'left_low';
                        const entry = j.modes[j.active_mode].processing[active];
                        return JSON.stringify((entry.lowpass || entry.highpass || {}).slope_db_oct);
                    })""")
            check(f"way slope edit persists ({slope_back})", slope_back == "48")

            # Cutoff changes must not move the layout: card width is identical
            # for a 3-digit and a 5-digit frequency.
            card_width = lambda: page.locator("#effects-crossover-card").bounding_box()["width"]
            width_before = card_width()
            page.evaluate(
                """(() => {
                    const input = document.getElementById('effects-crossover-frequency-lowpass');
                    input.value = '200'; input.dispatchEvent(new Event('change', { bubbles: true }));
                })()""")
            page.wait_for_timeout(1500)
            page.evaluate(
                """(() => {
                    const input = document.getElementById('effects-crossover-frequency-lowpass');
                    input.value = '2000'; input.dispatchEvent(new Event('change', { bubbles: true }));
                })()""")
            page.wait_for_timeout(1500)
            width_after = card_width()
            check(f"card width stable across cutoff change ({width_before} vs {width_after})",
                  width_before == width_after)

            # Existing sub tile edits the same role processing in v2.
            check("stereo sub labels", page.locator('.effects-card-subwoofer').is_visible())
            page.evaluate("""() => {
                const input = document.getElementById('effects-subwoofer-level');
                input.value = '-6'; input.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
            page.wait_for_timeout(1300)
            level = page.evaluate("fetch('/api/audio/output-state').then(r => r.json()).then(j => j.modes[j.active_mode].processing.sub_l.level_db)")
            check("sub tile saves routed Sub L", level == -6)

            page.evaluate("toggleSettingsPanel(true)")
            pick('#settings-crossover-select', 'off')
            page.wait_for_timeout(700)
            check("crossover Off removes way options", 'Low L' not in page.locator('#settings-routing-out-1 option').all_text_contents())
            pick('#settings-output-mode-select', 'stereo')
            page.wait_for_timeout(700)
            check("Stereo uses only stereo roles", page.locator('#settings-routing-out-1 option').all_text_contents() == ['Off', 'Main L', 'Main R'])
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(shots / 'output-mobile.png'))

            browser.close()
    finally:
        server.terminate()
    relevant = [p for p in problems if "output" in p.lower() or "crossover" in p.lower()
                or "bank" in p.lower() or "pageerror" in p.lower()]
    for problem in problems:
        print("console:", problem[:220])
    if relevant:
        print(f"FAILED: {len(relevant)} relevant page/console errors")
        return 1
    if failures:
        print(f"FAILED: {len(failures)} checks")
        return 1
    print("output-system UI browser check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
