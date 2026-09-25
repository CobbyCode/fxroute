#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Check Sub 1/2 trim formatting, stepping and control fit in the local demo.

Run python3 scripts/serve_demo.py, then python3 scripts/check_subwoofer_trim_ui.py.
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
                page.evaluate("""([selector, value]) => {
                    const input = document.querySelector(selector);
                    input.value = value;
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                }""", [selector, value])

            device = page.evaluate("""[...document.querySelectorAll('#settings-output-select option')]
                .find(option => option.textContent.includes('Scarlett'))?.value""")
            assert device, "multichannel demo device missing"
            pick("#settings-output-select", device)
            page.wait_for_timeout(1500)
            pick("#settings-output-mode-select", "stereo-sub")
            page.wait_for_timeout(700)
            for index, role in enumerate(("main_l", "main_r", "sub1", "sub2"), 1):
                pick(f"#settings-routing-out-{index}", role)
                page.wait_for_timeout(250)
            page.evaluate("toggleSettingsPanel(false)")
            page.locator("#tab-btn-effects").click()
            page.locator(".effects-card-subwoofer").wait_for(state="visible")
            assert page.locator("#effects-subwoofer-sub2-level").is_visible()
            ids = ["effects-subwoofer-level", "effects-subwoofer-delay",
                   "effects-subwoofer-sub2-level", "effects-subwoofer-sub2-delay"]
            for field_id in ids:
                assert page.locator(f"#{field_id}").get_attribute("step") == "0.1", field_id
            for role, level, alignment in (("sub1", -4.4, 7.81), ("sub2", -80, -39.99)):
                page.evaluate("""async ([role, level, alignment]) => {
                    await applyOutputSystemMutation('set_processing', {
                        mode: 'stereo-sub', role, level_db: level, alignment_ms: alignment,
                    }, false, { quiet: true });
                }""", [role, level, alignment])
            for width in (1440, 1000, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                page.wait_for_function("""() => document.getElementById('effects-subwoofer-level').value === '-4.40'
                    && document.getElementById('effects-subwoofer-delay').value === '7.81'
                    && document.getElementById('effects-subwoofer-sub2-level').value === '-80.00'
                    && document.getElementById('effects-subwoofer-sub2-delay').value === '-39.99'""")
                dimensions = page.evaluate("""ids => ids.map(id => {
                    const input = document.getElementById(id);
                    const control = input.closest('.stepper-control');
                    const group = input.closest('.effects-subwoofer-channel-group');
                    const card = document.querySelector('.effects-card-subwoofer');
                    const style = getComputedStyle(input);
                    const context = document.createElement('canvas').getContext('2d');
                    context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
                    const textWidth = context.measureText(input.value).width;
                    const available = input.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
                    const box = control.getBoundingClientRect();
                    const bounds = group.getBoundingClientRect();
                    const cardBounds = card.getBoundingClientRect();
                    return { id, textWidth, available,
                        inside: box.left >= bounds.left - 1 && box.right <= bounds.right + 1,
                        groupInside: bounds.left >= cardBounds.left - 1 && bounds.right <= cardBounds.right + 1 };
                })""", ids)
                assert all(d["textWidth"] <= d["available"] and d["inside"] and d["groupInside"]
                           for d in dimensions), f"{width}px: {dimensions}"
                print(f"{width}px: Sub 1/2 controls fit")

            page.set_viewport_size({"width": 1440, "height": 900})

            def stored(role, key):
                return page.evaluate("""async ([role, key]) => {
                    const response = await fetch('/api/audio/output-state');
                    const catalog = await response.json();
                    return catalog.modes['stereo-sub'].processing[role][key];
                }""", [role, key])

            for field_id, role, key, typed, shown, stepped in (
                ("effects-subwoofer-level", "sub1", "level_db", "-4.6", "-4.60", "-4.50"),
                ("effects-subwoofer-delay", "sub1", "alignment_ms", "7.83", "7.83", "7.93"),
                ("effects-subwoofer-sub2-level", "sub2", "level_db", "-23.8", "-23.80", "-23.70"),
                ("effects-subwoofer-sub2-delay", "sub2", "alignment_ms", "-39.89", "-39.89", "-39.79"),
            ):
                field = page.locator(f"#{field_id}")
                field.fill(typed)
                field.press("Tab")
                assert field.input_value() == shown, (field_id, field.input_value())
                page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
                page.wait_for_function("""async ([role, key, expected]) => {
                    const response = await fetch('/api/audio/output-state');
                    const catalog = await response.json();
                    return catalog.modes['stereo-sub'].processing[role][key] === Number(expected);
                }""", arg=[role, key, typed])
                field.locator("xpath=../button[@data-stepper='inc']").click()
                assert field.input_value() == stepped, (field_id, field.input_value())
                page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
                page.wait_for_function("""async ([role, key, expected]) => {
                    const response = await fetch('/api/audio/output-state');
                    const catalog = await response.json();
                    return catalog.modes['stereo-sub'].processing[role][key] === Number(expected);
                }""", arg=[role, key, stepped])
                actual = stored(role, key)
                assert actual == float(stepped), (field_id, actual, stepped)
                field.locator("xpath=../button[@data-stepper='dec']").click()
                assert field.input_value() == shown, (field_id, field.input_value())
                page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
                assert stored(role, key) == float(typed), field_id
            assert not errors, errors
            print("Sub 1/2 input and +/-0.1 stepping persist")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
