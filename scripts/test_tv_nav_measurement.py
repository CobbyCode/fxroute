#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered Measurement Assistant remote navigation regressions.

Run: python3 scripts/test_tv_nav_measurement.py
Uses real frontend code and the isolated demo transport, never live audio.
"""

from collections import deque
import traceback

from playwright.sync_api import sync_playwright

from demo_server_test_support import demo_server


def focused(page):
    return page.evaluate("document.activeElement.outerHTML.slice(0, 250)")


def expect_focus(page, selector):
    assert page.locator(selector).evaluate("el => el === document.activeElement"), focused(page)


def open_assistant(page, url, with_measurement=True):
    page.goto(f"{url}/?fast", wait_until="networkidle")
    page.evaluate("toggleMeasurementPanel(true)")
    page.wait_for_function("state.measurement.measurements.length > 0")
    if with_measurement:
        page.evaluate("""() => {
            const m = state.measurement;
            const source = m.measurements.find(item => item.id === 'sweep-20260905-033530-97465f');
            if (!source) throw new Error('Missing demo frequency measurement');
            m.currentMeasurement = structuredClone(source);
            m.currentMeasurement.id = 'remote-test-current';
            m.currentMeasurementSaved = false;
            m.measurements = [source];
            m.visibilityById = {};
            renderMeasurementPanel();
        }""")
        page.locator('[data-measurement-peq-auto]:not([disabled])').wait_for()


def smoothing_exit(page):
    page.focus('[data-measurement-smoothing="raw"]')
    for value in ("1/6-oct", "1/3-oct", "1/1-oct"):
        page.keyboard.press("ArrowRight")
        expect_focus(page, f'[data-measurement-smoothing="{value}"]')
    page.keyboard.press("ArrowRight")
    expect_focus(page, "#measurement-name")
    page.keyboard.press("ArrowLeft")
    expect_focus(page, '[data-measurement-smoothing="1/1-oct"]')


def filter_auto_exit(page):
    page.focus('[data-measurement-peq-slot="0"]')
    for slot in range(1, 12):
        page.keyboard.press("ArrowRight")
        expect_focus(page, f'[data-measurement-peq-slot="{slot}"]')
    page.keyboard.press("ArrowRight")
    expect_focus(page, "[data-measurement-peq-auto]")
    page.keyboard.press("ArrowLeft")
    expect_focus(page, '[data-measurement-peq-slot="11"]')


def graph_view_exit(page):
    page.focus('[data-measurement-view="freq"]')
    page.keyboard.press('ArrowRight')
    expect_focus(page, '[data-measurement-view="ir"]')
    page.keyboard.press('ArrowRight')
    expect_focus(page, '#measurement-assist-mode')
    page.keyboard.press('ArrowLeft')
    expect_focus(page, '[data-measurement-view="ir"]')
    page.keyboard.press('Enter')
    assert page.locator('#measurement-peq-panel').is_hidden()
    page.keyboard.press('ArrowLeft')
    page.keyboard.press('Enter')
    assert page.locator('#measurement-peq-panel').is_visible()


def disabled_auto_edge(page):
    assert page.locator('[data-measurement-peq-auto]').is_disabled()
    page.focus('[data-measurement-peq-slot="11"]')
    page.keyboard.press("ArrowRight")
    expect_focus(page, '[data-measurement-peq-slot="11"]')
    page.keyboard.press("ArrowDown")
    assert not page.locator('[data-measurement-peq-slot="11"]').evaluate(
        "el => el === document.activeElement"), "Down must leave the row"


def wrapped_geometry(page):
    chips = page.locator('#measurement-peq-chips button:not([disabled])')
    rects = chips.evaluate_all("els => els.map(el => el.getBoundingClientRect().toJSON())")
    # Test a real CSS wrap, not a DOM-order assumption about F1-F12.
    index = next(i for i in range(len(rects) - 1) if rects[i + 1]['top'] > rects[i]['top'] + 10)
    chips.nth(index).focus()
    page.keyboard.press("ArrowRight")
    assert chips.nth(index).evaluate("el => el === document.activeElement"), focused(page)
    before_top = chips.nth(index).evaluate(
        "el => el.getBoundingClientRect().top - el.closest('.measurement-card').getBoundingClientRect().top")
    page.keyboard.press("ArrowDown")
    target_top = page.evaluate("""() => {
        const el = document.activeElement;
        return el.getBoundingClientRect().top - el.closest('.measurement-card').getBoundingClientRect().top;
    }""")
    assert target_top > before_top, focused(page)
    page.keyboard.press("ArrowUp")
    assert page.evaluate("""() => {
        const el = document.activeElement;
        return el.getBoundingClientRect().top - el.closest('.measurement-card').getBoundingClientRect().top;
    }""") < target_top


def activation_and_values(page):
    page.focus('[data-measurement-smoothing="raw"]')
    page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    assert page.evaluate("state.measurement.displaySmoothing") == "1/6-oct"
    expect_focus(page, '[data-measurement-smoothing="1/6-oct"]')
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    expect_focus(page, "#measurement-name")
    page.keyboard.press("Enter")
    page.keyboard.type("Remote name")
    page.keyboard.press("Enter")
    page.keyboard.press("ArrowRight")
    expect_focus(page, "#measurement-save")
    assert page.input_value('#measurement-name') == 'Remote name'

    page.focus('[data-measurement-peq-slot="0"]')
    page.keyboard.press("Enter")
    page.wait_for_selector('#measurement-peq-freq')
    assert page.evaluate('state.measurement.peqAssistant.filters.length') == 1
    minus = page.locator('[data-measurement-peq-frequency-step="-1"]')
    plus = page.locator('[data-measurement-peq-frequency-step="1"]')
    minus.focus()
    page.keyboard.press("ArrowRight")
    expect_focus(page, '#measurement-peq-freq')
    page.keyboard.press("ArrowRight")
    assert plus.evaluate('el => el === document.activeElement'), focused(page)
    before = float(page.input_value('#measurement-peq-freq'))
    page.keyboard.press("Enter")
    assert float(page.input_value('#measurement-peq-freq')) == before + 1
    page.keyboard.press("ArrowLeft")
    expect_focus(page, '#measurement-peq-freq')
    page.keyboard.press("Enter")
    page.keyboard.press("Control+A")
    page.keyboard.type("1234")
    page.keyboard.press("Enter")
    expect_focus(page, '#measurement-peq-freq')
    page.keyboard.press("ArrowRight")
    assert plus.evaluate('el => el === document.activeElement'), focused(page)
    assert page.evaluate('state.measurement.peqAssistant.filters[0].freqHz') == 1234

    page.focus('[data-measurement-peq-frequency-step="-1"]')
    page.keyboard.press("ArrowLeft")
    expect_focus(page, '#measurement-peq-type')
    page.keyboard.press("Enter")
    page.locator('#tv-select-picker').wait_for()
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    assert page.locator('#tv-select-picker').count() == 0
    assert page.input_value('#measurement-peq-type') == 'low_shelf'


def all_controls_reachable(page):
    page.click('[data-measurement-peq-slot="0"]')
    page.click('#measurement-peq-take-both')
    page.locator('#measurement-list summary').click()
    check_reachability(page, minimum=40)


def setup_reachable(page):
    page.click('#measurement-setup-toggle')
    check_reachability(page, minimum=10)


def convolver_reachable(page):
    page.select_option('#measurement-assist-mode', 'convolver')
    check_reachability(page, minimum=20)


def house_curve_reachable(page):
    page.select_option('#measurement-target-curve', 'create-custom-house-curve')
    page.click('[data-custom-house-curve-slot="0"]')
    check_reachability(page, minimum=20)


def check_reachability(page, minimum):
    page.evaluate("""() => {
        let index = 0;
        for (const el of document.querySelectorAll(`#measurement-panel button, #measurement-panel input,
            #measurement-panel select, #measurement-panel textarea, #measurement-panel summary,
            #measurement-panel a[href], #measurement-panel [tabindex]`)) {
            if (el.tabIndex >= 0 && !el.matches(':disabled') && el.checkVisibility()
                && el.getClientRects().length) el.dataset.remoteTest = String(index++);
        }
    }""")
    ids = page.locator('[data-remote-test]').evaluate_all('els => els.map(el => el.dataset.remoteTest)')
    assert len(ids) >= minimum, f'Incomplete rendered fixture: {len(ids)} controls'
    start = ids[0]
    page.focus(f'[data-remote-test="{start}"]')
    page.keyboard.press('ArrowDown')  # Establish remote, rather than native editor, ownership.
    queue = deque([start])
    visited = {start}
    predecessors = {}
    while queue:
        current = queue.popleft()
        for key in ('ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'):
            page.focus(f'[data-remote-test="{current}"]')
            page.keyboard.press(key)
            target = page.evaluate('document.activeElement.dataset.remoteTest')
            if target is not None:
                predecessors.setdefault(target, set()).add(current)
            if target is not None and target not in visited:
                visited.add(target)
                queue.append(target)
    missing = page.locator('[data-remote-test]').evaluate_all(
        '(els, visited) => els.filter(el => !visited.includes(el.dataset.remoteTest)).map(el => el.outerHTML.slice(0, 250))',
        list(visited))
    assert not missing, f'Controls unreachable by arrows: {missing}'
    can_return = {start}
    queue = deque([start])
    while queue:
        for previous in predecessors.get(queue.popleft(), set()) - can_return:
            can_return.add(previous)
            queue.append(previous)
    assert can_return == visited, f'Arrow navigation traps: {visited - can_return}'
    print(f'  Arrow-only reachability: {len(visited)} enabled controls')


def main():
    failures = []
    scenarios = [
        ('smoothing exits to name and returns', smoothing_exit, 1440, True),
        ('F1-F12 continue to Auto and return', filter_auto_exit, 1440, True),
        ('graph-view chips exit to assist mode and activate with OK', graph_view_exit, 1440, True),
        ('disabled Auto does not jump to graph header', disabled_auto_edge, 1440, False),
        ('wrapped chips follow rendered rows', wrapped_geometry, 800, True),
        ('OK selection and numeric editing remain usable', activation_and_values, 1440, True),
        ('all expanded assistant controls are arrow-reachable', all_controls_reachable, 1440, True),
        ('setup controls are arrow-reachable', setup_reachable, 1440, True),
        ('convolver controls are arrow-reachable', convolver_reachable, 1440, True),
        ('custom house-curve controls are arrow-reachable', house_curve_reachable, 1440, True),
        ('narrow PEQ and saved controls are arrow-reachable', all_controls_reachable, 375, True),
        ('narrow setup controls are arrow-reachable', setup_reachable, 375, True),
        ('narrow convolver controls are arrow-reachable', convolver_reachable, 375, True),
        ('narrow custom house-curve controls are arrow-reachable', house_curve_reachable, 375, True),
    ]
    with demo_server() as url, sync_playwright() as p:
        browser = p.chromium.launch()
        for name, run, width, with_measurement in scenarios:
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                open_assistant(page, url, with_measurement)
                run(page)
                assert not errors, errors
                print(f'PASS {name}')
            except Exception as error:
                traceback.print_exc()
                failures.append(f'{name}: {error}')
            finally:
                page.close()
        browser.close()
    assert not failures, '\n'.join(failures)
    print(f'Measurement remote navigation: {len(scenarios)} scenarios passed')


if __name__ == '__main__':
    main()
