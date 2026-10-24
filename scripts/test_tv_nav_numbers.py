#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered numeric remote navigation and direct-entry regressions.

Runs the real frontend with the isolated demo transport, including saves.
Run: python3 scripts/test_tv_nav_numbers.py.
"""

from playwright.sync_api import sync_playwright

from demo_server_test_support import demo_server


def setup(page, url):
    page.goto(f"{url}/?fast", wait_until="networkidle")
    page.evaluate("toggleSettingsPanel(true)")
    page.locator("#settings-output-mode-select:not([disabled])").wait_for()

    def pick(selector, value):
        page.select_option(selector, value)
        page.wait_for_function("!state.outputSystem.busy")

    device = page.evaluate("""[...document.querySelectorAll('#settings-output-select option')]
        .find(option => option.textContent.includes('Scarlett'))?.value""")
    assert device, "multichannel demo device missing"
    pick("#settings-output-select", device)
    pick("#settings-output-mode-select", "stereo-sub")
    pick("#settings-crossover-select", "on")
    for index, role in enumerate(("left_low", "left_mid", "left_high", "right_low",
                                  "right_mid", "right_high", "sub1", "sub2"), 1):
        pick(f"#settings-routing-out-{index}", role)
    page.evaluate("toggleSettingsPanel(false)")
    page.click("#tab-btn-effects")
    page.locator("#effects-crossover-card").wait_for(state="visible")
    if page.is_checked("#effects-crossover-link"):
        page.uncheck("#effects-crossover-link")
    page.check("#effects-loudness-enabled")
    page.check("#effects-bass-enabled")
    page.wait_for_function("!state.outputSystem.busy && !state.crossover.busy")


def controls(page, field_id):
    field = page.locator(f"#{field_id}")
    group = field.locator("xpath=..")
    return field, group.locator('[data-stepper="dec"]'), group.locator('[data-stepper="inc"]')


def focused(locator):
    return locator.evaluate("el => el === document.activeElement")


def remote_field(page, field, minus, plus):
    minus.focus()
    page.keyboard.press("ArrowRight")
    assert focused(field), "Right must reach the value from minus"
    page.keyboard.press("ArrowRight")
    assert focused(plus), "Right must leave the value for plus"
    page.keyboard.press("ArrowLeft")
    assert focused(field), "Left must return to the value"


def hold_save_response(page, path, method="POST", count=1):
    page.evaluate("""([path, method, count]) => {
        const original = window.fetch;
        let held = 0;
        window.__numberSavesHeld = 0;
        window.__releaseNumberSave = null;
        window.__restoreNumberFetch = () => { window.fetch = original; };
        window.fetch = async (url, options) => {
            const response = await original(url, options);
            if (String(url) === path && (options?.method || 'GET') === method && held < count) {
                window.__numberSavesHeld = ++held;
                // Freeze demo response objects like a real HTTP body.
                const snapshot = new Response(JSON.stringify(await response.json()), {status: response.status});
                await new Promise(resolve => { window.__releaseNumberSave = resolve; });
                return snapshot;
            }
            return response;
        };
    }""", [path, method, count])


def release_save_response(page, restore=True):
    page.evaluate("restore => { if (restore) window.__restoreNumberFetch(); window.__releaseNumberSave(); }", restore)


def main():
    with demo_server() as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            setup(page, url)
            ids = page.locator(".stepper-input:visible:not([disabled])").evaluate_all("els => els.map(el => el.id)")
            assert {"effects-crossover-level", "effects-crossover-delay",
                    "effects-crossover-frequency-lowpass", "effects-subwoofer-level",
                    "effects-subwoofer-delay", "effects-subwoofer-sub2-delay",
                    "effects-loudness-strength", "effects-bass-amount"} <= set(ids), ids

            for field_id in ids:
                field, minus, plus = controls(page, field_id)
                remote_field(page, field, minus, plus)
                # Work away from the maximum so repeated increases are observable.
                start = field.evaluate("el => Number(el.value)")
                step = field.evaluate("el => Number(el.step)")
                if start + 3 * step > float(field.get_attribute("max")):
                    field.fill(str(start - 3 * step))
                    field.press("Tab")
                    page.wait_for_function("!state.outputSystem.busy && !state.crossover.busy")
                    start = float(field.input_value())
                plus.focus()
                for count in range(1, 4):
                    page.keyboard.press("Enter")
                    assert focused(plus), f"{field_id}: plus stole focus on press {count}"
                    assert abs(float(field.input_value()) - (start + count * step)) < 0.001, field_id
                    page.wait_for_function("!state.outputSystem.busy && !state.crossover.busy")
                page.keyboard.press("ArrowLeft")
                assert focused(field), f"{field_id}: cannot leave plus"
                page.keyboard.press("ArrowLeft")
                assert focused(minus), f"{field_id}: value trapped Left after stepping"
                for count in range(1, 4):
                    page.keyboard.press("Space")
                    assert focused(minus), f"{field_id}: minus stole focus"
                    assert abs(float(field.input_value()) - (start + (3 - count) * step)) < 0.001, field_id
                    page.wait_for_function("!state.outputSystem.busy && !state.crossover.busy")
                page.keyboard.press("ArrowRight")
                assert focused(field), field_id
                page.keyboard.press("ArrowRight")
                assert focused(plus), f"{field_id}: value trapped Right after stepping"
                print(f"PASS {field_id}: repeated +/- and bidirectional navigation")

            field, minus, plus = controls(page, "effects-subwoofer-delay")
            for exit_key in ("Enter", "Escape"):
                remote_field(page, field, minus, plus)
                page.keyboard.press("Enter")
                field.press("Control+A")
                page.keyboard.type("12.34")
                page.keyboard.press("ArrowLeft")
                assert focused(field), "editing must retain native caret handling"
                page.keyboard.press("Backspace")
                assert field.input_value() == "12.4", "native caret/backspace changed"
                page.keyboard.press(exit_key)
                assert focused(field), f"{exit_key} must leave editing on the same value stop"
                page.keyboard.press("ArrowRight")
                assert focused(plus), f"{exit_key} did not release numeric editing"
                page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
                stored = page.evaluate("state.outputSystem.catalog.modes['stereo-sub'].processing.sub1.alignment_ms")
                assert stored == 12.4, f"{exit_key} did not commit the value: {stored}"
                print(f"PASS numeric edit exit with {exit_key}")

            # Pointer and Tab entry must retain native typing until explicitly ended.
            for entry in ("pointer", "tab"):
                if entry == "pointer":
                    field.click()
                else:
                    minus.focus()
                    page.keyboard.press("Tab")
                assert focused(field)
                field.press("Control+A")
                page.keyboard.type("7.81")
                page.keyboard.press("ArrowLeft")
                page.keyboard.press("Backspace")
                assert field.input_value() == "7.1", entry
                page.keyboard.press("Enter")
                assert focused(field)
                page.keyboard.press("ArrowLeft")
                assert focused(minus), f"{entry}: Enter did not restore navigation"
                # A pointer step must focus its button, not retain the editor.
                field.click()
                plus.click()
                assert focused(plus), f"{entry}: pointer step stole focus"
                print(f"PASS direct {entry} entry and pointer step")

            # A programmatic value event on an unfocused field is not edit intent.
            remote_field(page, field, minus, plus)
            plus.focus()
            field.evaluate("el => el.dispatchEvent(new Event('input', {bubbles: true}))")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("ArrowLeft")
            assert focused(minus), "synthetic input armed an unfocused editor"
            print("PASS programmatic value events do not enter editing")

            # An earlier save must not roll back a sub after navigation leaves
            # its group: every later sub save also carries its neighbor's trim.
            page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            first, first_minus, first_plus = controls(page, "effects-subwoofer-level")
            second, second_minus, second_plus = controls(page, "effects-subwoofer-sub2-level")
            for control in (first, second):
                control.fill("0")
                control.press("Enter")
                page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            hold_save_response(page, "/api/audio/output-state/apply", count=2)
            first_minus.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("typeof window.__releaseNumberSave === 'function'")
            for _ in range(9):
                page.keyboard.press("Enter")
            second_minus.focus()
            page.keyboard.press("Enter")
            release_save_response(page, restore=False)
            page.wait_for_function("window.__numberSavesHeld === 2")
            assert float(first.input_value()) == -1, "older save rolled back Sub 1 while its newer save is in flight"
            # An additional neighbor step must not capture the old Sub 1 value.
            page.keyboard.press("Enter")
            release_save_response(page)
            page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            assert float(first.input_value()) == -1, "older save rolled back Sub 1 after navigation"
            assert float(second.input_value()) == -0.2, "older save rolled back Sub 2"
            page.focus("#effects-subwoofer-polarity")
            page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            page.evaluate("fetchOutputSystemCatalog(true)")
            levels = page.evaluate("""() => {
                const p = state.outputSystem.catalog.modes['stereo-sub'].processing;
                return [p.sub1.level_db, p.sub2.level_db];
            }""")
            assert levels == [-1, -0.2], f"navigation re-saved stale sub levels: {levels}"
            assert [float(first.input_value()), float(second.input_value())] == [-1, -0.2]
            print("PASS both sub levels survive delayed saves and cross-control navigation")

            # Keep a successful trim across a failed refresh AND a later failed
            # neighbor write. The following retry must not overwrite that trim.
            for control in (first, second):
                control.fill("0")
                control.press("Enter")
                page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            page.evaluate("""() => {
                const original = window.fetch;
                let posts = 0, failRefresh = false;
                window.__restoreSubFailureFetch = () => { window.fetch = original; };
                window.fetch = async (url, options) => {
                    if (String(url) === '/api/audio/output-state/apply' && options?.method === 'POST') {
                        if (++posts === 2) return new Response(JSON.stringify({detail:'test neighbor failure'}), {status:500});
                        const response = await original(url, options);
                        if (posts === 1) failRefresh = true;
                        return response;
                    }
                    if (String(url) === '/api/audio/output-state' && failRefresh) {
                        failRefresh = false;
                        return new Response('{}', {status:500});
                    }
                    return original(url, options);
                };
            }""")
            first.fill("-21")
            first.press("Enter")
            page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            second_minus.focus()
            page.evaluate("renderSubwooferPanel()")
            assert float(first.input_value()) == -21, "failed GET lost an acknowledged sub level"
            page.keyboard.press("Enter")
            page.evaluate("flushSubwooferSettingsBeforeMeasurement().catch(() => null)")
            page.evaluate("renderSubwooferPanel()")
            assert float(first.input_value()) == -21, "failed neighbor save discarded the acknowledged baseline"
            page.keyboard.press("Enter")
            page.evaluate("flushSubwooferSettingsBeforeMeasurement()")
            page.evaluate("window.__restoreSubFailureFetch()")
            page.evaluate("fetchOutputSystemCatalog(true)")
            saved_first = page.evaluate("state.outputSystem.catalog.modes['stereo-sub'].processing.sub1.level_db")
            assert saved_first == -21, "neighbor retry overwrote the successful trim after network failures"
            print("PASS acknowledged sub trim survives refresh/write failures and neighbor retry")

            # Server echoes must not overwrite a focused stepper's unsaved value.
            field, minus, plus = controls(page, "effects-bass-amount")
            plus.focus()
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("Enter")
            field.fill("3.5")
            plus.click()
            page.evaluate("FXRouteEffectsUI.applyEffectsExtras({...FXRouteEffectsUI.collectEffectsExtras(), bassAmount: -3})")
            assert float(field.input_value()) == 4, "save echo overwrote the stepped value"
            assert page.evaluate("_activeEditing.has(document.getElementById('effects-bass-amount'))")
            page.focus("#effects-bass-enabled")
            assert not page.evaluate("_activeEditing.has(document.getElementById('effects-bass-amount'))"), "stepper guard leaked"
            print("PASS stepper save-echo protection and guard cleanup")

            hold_save_response(page, "/api/dsp/extras")
            field.click()
            field.fill("2.5")
            plus.click()
            page.wait_for_function("typeof window.__releaseNumberSave === 'function'")
            page.evaluate("FXRouteEffectsUI.applyEffectsExtras({...FXRouteEffectsUI.collectEffectsExtras(), bassAmount: 2.5})")
            assert float(field.input_value()) == 3, "in-flight echo overwrote focused stepper"
            page.focus("#effects-bass-enabled")
            page.evaluate("FXRouteEffectsUI.fetchEffects()")
            assert float(field.input_value()) == 3, "poll overwrote unsaved step after leaving the group"
            release_save_response(page)
            page.wait_for_function("""async () => {
                const response = await fetch('/api/dsp/presets');
                return (await response.json()).global_extras.bass_enhancer.params.amount === 3;
            }""", timeout=5000)
            print("PASS delayed extras echo preserves the last step after leaving")

            hold_save_response(page, "/api/dsp/presets", "GET")
            page.evaluate("() => { window.__numberPollDone = false; void FXRouteEffectsUI.fetchEffects().then(() => { window.__numberPollDone = true; }); }")
            page.wait_for_function("typeof window.__releaseNumberSave === 'function'")
            field.click()
            field.fill("4.5")
            plus.click()
            page.focus("#effects-bass-enabled")
            page.wait_for_function("state.dsp.global_extras.bass_enhancer.params.amount === 5", timeout=5000)
            release_save_response(page)
            page.wait_for_function("window.__numberPollDone")
            assert float(field.input_value()) == 5, "late pre-save poll overwrote the committed step"
            assert page.evaluate("state.dsp.global_extras.bass_enhancer.params.amount") == 5
            print("PASS late GET cannot overwrite a newer completed extras save")

            for field_id in ("effects-crossover-frequency-lowpass", "effects-crossover-level", "effects-crossover-delay"):
                field, minus, plus = controls(page, field_id)
                plus.focus()
                old_value = field.input_value()
                pending_value = str(float(old_value) + float(field.get_attribute("step")))
                field.evaluate("(el, value) => { el.value = value; }", pending_value)
                page.evaluate("FXRouteCrossoverUI.renderCrossoverTile()")
                assert float(field.input_value()) == float(pending_value), f"{field_id}: render overwrote focused stepper"
                field.evaluate("(el, value) => { el.value = value; }", old_value)
            print("PASS crossover focused-stepper render protection")

            # Hold a real mutation response while more remote presses arrive.
            field, minus, plus = controls(page, "effects-crossover-level")
            start = float(field.input_value())
            hold_save_response(page, "/api/audio/output-state/apply")
            plus.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("typeof window.__releaseNumberSave === 'function'")
            page.keyboard.press("Enter")
            page.keyboard.press("Enter")
            assert focused(plus)
            assert abs(float(field.input_value()) - (start + 0.3)) < 0.001
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("ArrowLeft")
            page.keyboard.press("Enter")
            assert focused(minus)
            release_save_response(page)
            page.wait_for_function("""expected => {
                const s = state.outputSystem.catalog;
                return !state.outputSystem.busy && !state.crossover.busy
                    && Math.abs(s.modes[s.active_mode].processing[state.crossover.activeWay].level_db - expected) < 0.001;
            }""", arg=start + 0.2, timeout=5000)
            print("PASS rapid crossover steps persist through a delayed save")

            field, minus, plus = controls(page, "effects-crossover-level")
            delay, delay_minus, delay_plus = controls(page, "effects-crossover-delay")
            hold_save_response(page, "/api/audio/output-state/apply")
            plus.focus()
            page.keyboard.press("Enter")
            page.wait_for_function("typeof window.__releaseNumberSave === 'function'")
            delay_plus.focus()
            release_save_response(page)
            page.wait_for_function("!state.outputSystem.busy && !state.crossover.busy")
            assert delay.is_enabled(), "focused neighbor stepper stayed disabled after save"
            page.keyboard.press("ArrowLeft")
            assert focused(delay), "disabled value was skipped"
            page.keyboard.press("ArrowLeft")
            assert focused(delay_minus)
            print("PASS crossover neighbor stepper re-enables after delayed save")

            page.evaluate("""() => {
                toggleMeasurementPanel(true);
                FXRouteMeasurementPeqEditor.addMeasurementPeqFilter({freqHz: 1000, gainDb: 0, q: 1});
                renderMeasurementPanel();
            }""")
            for field_id, attr in (("measurement-peq-freq", "frequency"),
                                   ("measurement-peq-gain", "gain"), ("measurement-peq-q", "q")):
                field = page.locator(f"#{field_id}")
                minus = field.locator(f'xpath=../button[@data-measurement-peq-{attr}-step="-1"]')
                plus = field.locator(f'xpath=../button[@data-measurement-peq-{attr}-step="1"]')
                remote_field(page, field, minus, plus)
                page.keyboard.press("ArrowRight")
                for _ in range(3):
                    page.keyboard.press("Enter")
                    assert focused(plus), f"{field_id}: step moved focus to the panel"
                page.keyboard.press("ArrowLeft")
                page.keyboard.press("ArrowLeft")
                assert focused(minus), field_id
                page.keyboard.press("Enter")
                assert focused(minus), field_id
                page.keyboard.press("ArrowRight")
                page.keyboard.press("Enter")
                field.press("Control+A")
                page.keyboard.type("2" if attr != "frequency" else "1200")
                page.keyboard.press("Escape")
                assert page.locator("#measurement-panel").is_visible(), "Escape closed the dialog while editing"
                assert focused(field), f"{field_id}: edit commit lost the replacement field"
                page.keyboard.press("ArrowRight")
                assert focused(plus), field_id
                print(f"PASS {field_id}: stepping and modal numeric editing")

            field = page.locator("#measurement-peq-freq")
            minus = field.locator('xpath=../button[@data-measurement-peq-frequency-step="-1"]')
            plus = field.locator('xpath=../button[@data-measurement-peq-frequency-step="1"]')
            for entry in ("pointer", "tab"):
                for exit_key in ("Enter", "Escape"):
                    if entry == "pointer":
                        field.click()
                    else:
                        minus.click()
                        page.keyboard.press("Tab")
                    assert focused(field)
                    field.press("Control+A")
                    page.keyboard.type("1300" if exit_key == "Enter" else "1400")
                    page.keyboard.press(exit_key)
                    assert focused(field), f"PEQ {entry}/{exit_key} lost replacement field focus"
                    assert page.locator("#measurement-panel").is_visible()
                    page.keyboard.press("ArrowRight")
                    assert focused(plus), f"PEQ {entry}/{exit_key} did not restore navigation"
                    print(f"PASS PEQ direct {entry} entry and {exit_key} commit")
            assert not errors, errors
        finally:
            browser.close()


if __name__ == "__main__":
    main()
