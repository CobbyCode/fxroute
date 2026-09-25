#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser check for crossover trim precision, stepping, and narrow layouts.

Run against the local demo server: python3 scripts/serve_demo.py
Then: python3 scripts/check_crossover_trim_ui.py
"""

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto("http://localhost:8765/", wait_until="networkidle")
            page.evaluate("toggleSettingsPanel(true)")
            page.locator("#settings-output-mode-select:not([disabled])").wait_for()

            def pick(selector, value):
                page.evaluate(
                    """([selector, value]) => {
                        const el = document.querySelector(selector);
                        el.value = value;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""", [selector, value])

            device = page.evaluate("""[...document.querySelectorAll('#settings-output-select option')]
                .find(option => option.textContent.includes('Scarlett'))?.value""")
            assert device, "multichannel demo device missing"
            pick("#settings-output-select", device)
            page.wait_for_timeout(1500)
            pick("#settings-output-mode-select", "stereo-sub")
            page.wait_for_timeout(700)
            pick("#settings-crossover-select", "on")
            page.wait_for_timeout(700)
            roles = ["left_low", "left_mid", "left_high", "right_low", "right_mid", "right_high"]
            for index, role in enumerate(roles, 1):
                pick(f"#settings-routing-out-{index}", role)
                page.wait_for_timeout(250)
            page.evaluate("toggleSettingsPanel(false)")
            page.locator("#tab-btn-effects").click()
            page.locator("#effects-crossover-card").wait_for(state="visible")
            assert page.locator("#effects-crossover-tabs button").count() == 6
            assert page.locator("#effects-crossover-level").get_attribute("step") == "0.1"
            assert page.locator("#effects-crossover-delay").get_attribute("step") == "0.1"
            for role in roles:
                page.evaluate("""async role => {
                    await applyOutputSystemMutation('set_processing', {
                        mode: 'stereo-sub', role, level_db: -79.9996,
                        alignment_ms: -39.9996,
                    }, false, { quiet: true });
                }""", role)
            for width in (1440, 1000, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                for role in roles:
                    page.locator(f"[data-crossover-way='{role}']").click()
                    page.wait_for_function("""() => document.querySelector('#effects-crossover-level').value === '-80.00'
                        && document.querySelector('#effects-crossover-delay').value === '-40.00'""")
                    dimensions = page.evaluate("""() => ['effects-crossover-level', 'effects-crossover-delay'].map(id => {
                        const input = document.getElementById(id);
                        const control = input.closest('.stepper-control');
                        const group = document.getElementById('effects-crossover-trim-group');
                        const card = document.getElementById('effects-crossover-card');
                        const unit = control.querySelector('.stepper-unit');
                        const text = document.createElement('canvas').getContext('2d');
                        const style = getComputedStyle(input);
                        text.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
                        const available = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                        const box = control.getBoundingClientRect();
                        const bounds = group.getBoundingClientRect();
                        const cardBounds = card.getBoundingClientRect();
                        const unitBox = unit.getBoundingClientRect();
                        return { value: input.value, textWidth: text.measureText(input.value).width,
                            available, inside: box.left >= bounds.left - 1 && box.right <= bounds.right + 1,
                            groupInside: bounds.left >= cardBounds.left - 1 && bounds.right <= cardBounds.right + 1,
                            unitInside: unitBox.right <= box.right - 25 };
                    })""")
                    assert all(d["textWidth"] <= d["available"] and d["inside"] and d["groupInside"]
                               and d["unitInside"]
                               for d in dimensions), f"{width}px {role}: {dimensions}"
                print(f"{width}px: six way tabs fit")
            page.set_viewport_size({"width": 1440, "height": 900})
            page.locator("[data-crossover-way='left_low']").click()
            level = page.locator("#effects-crossover-level")
            level.fill("-4.47049")
            level.press("Tab")
            assert level.input_value() == "-4.47"
            page.locator("#effects-crossover-level:not([disabled])").wait_for()
            def stored_trim():
                return page.evaluate("""async () => {
                    const response = await fetch('/api/audio/output-state');
                    const state = await response.json();
                    return state.modes['stereo-sub'].processing.left_low;
                }""")

            assert stored_trim()["level_db"] == -4.47049
            level.locator("xpath=../button[@data-stepper='inc']").click()
            assert level.input_value() == "-4.37", f"stepper level: {level.input_value()}"
            page.locator("#effects-crossover-level:not([disabled])").wait_for()
            assert stored_trim()["level_db"] == -4.37049
            page.locator("#effects-crossover-level:not([disabled])").wait_for()
            level.locator("xpath=../button[@data-stepper='dec']").click()
            assert level.input_value() == "-4.47"
            delay = page.locator("#effects-crossover-delay")
            delay.fill("7.8125")
            delay.press("Tab")
            assert delay.input_value() == "7.81"
            page.locator("#effects-crossover-delay:not([disabled])").wait_for()
            assert stored_trim()["alignment_ms"] == 7.8125
            pick("#effects-crossover-polarity", "invert")
            page.locator("#effects-crossover-polarity:not([disabled])").wait_for()
            assert stored_trim()["alignment_ms"] == 7.8125
            assert stored_trim()["level_db"] == -4.47049
            delay.locator("xpath=../button[@data-stepper='inc']").click()
            assert delay.input_value() == "7.91"
            page.locator("#effects-crossover-delay:not([disabled])").wait_for()
            assert stored_trim()["alignment_ms"] == 7.9125
            delay.locator("xpath=../button[@data-stepper='dec']").click()
            assert delay.input_value() == "7.81"
            page.locator("#effects-crossover-delay:not([disabled])").wait_for()
            assert stored_trim()["alignment_ms"] == 7.8125
            print("typed and +/-0.1 stepper values compact after changes")
            assert not errors, errors
        finally:
            browser.close()


if __name__ == "__main__":
    main()
