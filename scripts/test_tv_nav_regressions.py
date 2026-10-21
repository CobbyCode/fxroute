#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Focused rendered regressions for remote navigation and scroll boundaries."""

from test_tv_nav import STUB, _browser_back, _serve
import traceback


def run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('SKIP  test_tv_nav_regressions.py (playwright not installed)')
        return

    server = _serve(port=0)
    failures = []
    count = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            def scenario(name, test):
                nonlocal count
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                page.add_init_script(STUB.replace(
                    "presets: [], presets_available: []",
                    "available: true, mode: 'native', presets: [{name: 'b', bank: 'global', mode: 'stereo'}, {name: 'd', bank: 'global', mode: 'stereo'}]"))
                page.goto(f"http://127.0.0.1:{server.server_port}/static/index.html")
                page.wait_for_timeout(500)
                try:
                    test(page)
                    count += 1
                    print(f"PASS {name}")
                except Exception as error:
                    traceback.print_exc()
                    failures.append(f"{name}: {error}")
                    print(f"FAIL {failures[-1]}")
                finally:
                    page.close()

            def search_exit(page):
                page.focus('#tab-btn-radio')
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') == 'station-search'
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') != 'station-search', 'search traps D-pad'
                # Direct keyboard focus still owns its caret and text editing.
                page.focus('#station-search')
                page.keyboard.type('ab')
                page.keyboard.press('ArrowLeft')
                page.keyboard.type('X')
                assert page.input_value('#station-search') == 'aXb'

            def combine_exit(page):
                page.click('#tab-btn-effects')
                page.focus('#effects-combine-preset-3')
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') == 'effects-combine-preset-name'
                page.keyboard.press('ArrowUp')
                assert page.evaluate('document.activeElement.id') != 'effects-combine-preset-name', 'name traps D-pad'

            def catalog(page):
                page.focus('#tab-btn-radio')
                reached = False
                for _ in range(12):
                    page.keyboard.press('ArrowDown')
                    if page.evaluate("document.activeElement.matches('.station-catalog-summary')"):
                        reached = True
                        break
                assert reached, 'catalog summary unreachable'
                page.keyboard.press('Enter')
                assert page.evaluate("document.getElementById('station-catalog-section').open")
                # Nested closed details must not expose their summaries.
                page.evaluate("""() => {
                    const d = document.createElement('details');
                    d.innerHTML = '<summary>Outer</summary><details><summary id="nested-summary">Inner</summary></details>';
                    document.querySelector('#tab-radio').append(d);
                }""")
                page.focus('#nested-summary')
                assert page.evaluate('document.activeElement.id') != 'nested-summary'

            def select_interaction(page):
                page.click('#tab-btn-effects')
                page.evaluate("""() => {
                    const s = document.querySelector('#effects-combine-preset-3');
                    s.innerHTML = '<option value="">Optional</option><optgroup label="Unavailable" disabled><option value="bad">Bad</option></optgroup><option value="b">B</option><option value="c" disabled>C</option><option value="d">D</option>';
                    window.selectChanges = 0;
                    s.addEventListener('change', () => window.selectChanges++);
                }""")
                page.focus('#effects-combine-preset-3')
                page.keyboard.press('Enter')
                assert page.locator('#tv-select-picker').is_visible(), 'OK did not open remote choices'
                page.keyboard.press('ArrowDown')
                page.keyboard.press('Enter')
                assert page.input_value('#effects-combine-preset-3') == 'b'
                assert page.evaluate('window.selectChanges') == 1
                assert page.evaluate('document.activeElement.id') == 'effects-combine-preset-3'
                page.keyboard.press('Enter')
                page.keyboard.press('ArrowDown')
                page.keyboard.press('Escape')
                assert page.input_value('#effects-combine-preset-3') == 'b', 'cancel changed value'
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') != 'effects-combine-preset-3'

            def scroll_boundaries(page):
                page.evaluate("""() => {
                    document.body.innerHTML = `<div id="outer" style="position:fixed;left:80px;top:40px;width:600px;height:280px;overflow:auto">
                      <div style="height:350px"></div>
                      <div style="display:flex;gap:20px">
                        <div id="left" style="width:240px;height:180px;overflow:auto;flex-shrink:0">
                          <button id="left-top">Top</button><div style="height:600px"></div><button id="left-bottom">Bottom</button>
                        </div>
                        <div id="right" style="width:240px;height:180px;overflow:auto;flex-shrink:0">
                          <button id="right-top">Other top</button><div style="height:600px"></div><button id="right-bottom">Other bottom</button>
                        </div>
                      </div><div style="height:300px"></div></div>`;
                    document.querySelector('#outer').scrollTop = 350;
                    document.querySelector('#left').scrollTop = 1000;
                    document.querySelector('#left-bottom').focus();
                }""")
                page.keyboard.press('ArrowRight')
                assert page.evaluate('document.activeElement.id') == 'right-top', 'cannot leave bottom of independently scrolled tile'
                page.keyboard.press('ArrowLeft')
                assert page.evaluate('document.activeElement.id') == 'left-bottom', 'cannot return to scrolled tile'
                page.keyboard.press('ArrowUp')
                assert page.evaluate('document.activeElement.id') == 'left-top'
                assert page.evaluate("document.querySelector('#left').scrollTop") == 0

            def footer_visibility(page):
                page.evaluate("""() => {
                    const host = document.createElement('div');
                    host.style.cssText = 'margin-top:1000px;height:1100px';
                    host.innerHTML = '<button id="reveal-before">Before</button><div style="height:400px"></div><button id="reveal-target">Target</button>';
                    document.querySelector('#tab-radio').append(host);
                    document.querySelector('#reveal-before').focus();
                }""")
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') == 'reveal-target'
                assert page.evaluate("""() => {
                    const r = document.activeElement.getBoundingClientRect();
                    const footer = document.querySelector('.playback-bar').getBoundingClientRect();
                    return r.top >= 0 && r.bottom <= footer.top - 4;
                }"""), 'focus hidden behind playback footer'

            def tabs_from_text(page):
                page.click('#tab-btn-effects')
                page.focus('#effects-combine-preset-3')
                page.keyboard.press('ArrowDown')
                page.keyboard.press('PageDown')
                assert page.get_attribute('#tab-btn-library', 'aria-selected') == 'true', 'text focus swallowed PageDown'
                page.keyboard.press('PageUp')
                assert page.get_attribute('#tab-btn-effects', 'aria-selected') == 'true'

            def settings_select(page):
                page.keyboard.press('Alt+ArrowLeft')
                page.wait_for_function("!document.getElementById('settings-source-select').disabled")
                page.focus('#settings-source-select')
                page.keyboard.press('Enter')
                assert page.locator('#tv-select-picker').is_visible()
                page.keyboard.press('Backspace')
                assert page.locator('#tv-select-picker').count() == 0
                assert page.locator('#settings-panel').is_visible(), 'cancel closed parent dialog'
                assert page.evaluate('document.activeElement.id') == 'settings-source-select'
                page.keyboard.press('Alt+ArrowLeft')
                assert not page.locator('#settings-panel').is_visible()
                assert page.evaluate('!document.querySelector("#tab-content").inert')

            def app_input_picker(page):
                page.keyboard.press('ContextMenu')
                assert page.locator('#tv-select-picker').is_visible(), 'no input choices during app playback'
                assert page.locator('#tv-select-picker [role="option"]').count() >= 2
                page.keyboard.press('ContextMenu')
                assert page.locator('#tv-select-picker').count() == 0
                assert page.evaluate('document.activeElement !== document.body'), 'input picker lost opener focus'

            def keyboard_tab(page):
                page.keyboard.press('PageUp')
                page.keyboard.press('PageDown')
                page.keyboard.press('Tab')
                page.keyboard.press('Tab')
                page.keyboard.press('Tab')
                assert page.evaluate('document.activeElement.id') == 'station-search'
                page.keyboard.press('ArrowLeft')
                assert page.evaluate('document.activeElement.id') == 'station-search', 'Tab focus was treated as D-pad focus'

            def select_poll(page):
                page.keyboard.press('Alt+ArrowLeft')
                page.wait_for_function("!document.getElementById('settings-source-select').disabled")
                page.focus('#settings-source-select')
                page.keyboard.press('Enter')
                # The real settings poll replaces option nodes while a picker
                # stays open. Model that boundary without waiting on a timer.
                page.evaluate("""() => {
                    const s = document.getElementById('settings-source-select');
                    s.innerHTML = s.innerHTML;
                }""")
                page.keyboard.press('ArrowDown')
                page.keyboard.press('Enter')
                assert page.locator('#tv-select-picker').count() == 0
                assert page.locator('#custom-mapping-panel').is_visible(), 'poll invalidated selected option'

            def back_from_text(page):
                page.focus('#tab-btn-radio')
                page.keyboard.press('ArrowDown')
                page.keyboard.press('Alt+ArrowLeft')
                assert page.locator('#settings-panel').is_visible(), 'text focus leaks browser Back shortcut'

            def typed_text_keeps_keys(page):
                # A field the user types into keeps Option/Alt+Left and PageUp/PageDown.
                page.focus('#station-search')
                page.keyboard.type('ab')
                page.keyboard.press('Alt+ArrowLeft')
                page.keyboard.press('PageUp')
                page.keyboard.press('PageDown')
                assert page.locator('#settings-panel').is_hidden(), 'Alt+Left in typed text opened settings'
                assert page.get_attribute('#tab-btn-radio', 'aria-selected') == 'true', 'PageUp/PageDown in typed text switched the tab'
                assert page.evaluate('document.activeElement.id') == 'station-search'
                # Once the remote armed the field with OK it is typed text too.
                page.focus('#tab-btn-radio')
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') == 'station-search'
                page.keyboard.press('Enter')
                page.keyboard.press('PageUp')
                assert page.get_attribute('#tab-btn-radio', 'aria-selected') == 'true', 'PageUp while editing switched the tab'
                # The remote's own BrowserBack key still navigates from a field.
                _browser_back(page)
                assert page.locator('#settings-panel').is_visible(), 'BrowserBack from a text field stopped navigating'

            def space_without_focus(page):
                page.evaluate("document.activeElement.blur()")
                scrolled_by_default = page.evaluate("""() => {
                    const event = new KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true });
                    document.body.dispatchEvent(event);
                    return !event.defaultPrevented;
                }""")
                assert scrolled_by_default, 'Space on the bare page was swallowed'
                # A focused click-only row still activates with Space.
                page.evaluate("""() => {
                    const row = document.createElement('div');
                    row.id = 'space-row';
                    row.tabIndex = 0;
                    row.addEventListener('click', () => { window.__rowClicks = (window.__rowClicks || 0) + 1; });
                    document.getElementById('tab-radio').appendChild(row);
                }""")
                page.focus('#space-row')
                page.keyboard.press(' ')
                assert page.evaluate('window.__rowClicks') == 1, 'Space no longer activates a focused row'

            def own_arrow_rows(page):
                # The tab strip handles Left/Right itself: no second move off the strip.
                first = page.evaluate("Array.from(document.querySelectorAll('.tab-btn')).find(b => !b.hidden && b.offsetParent).id")
                page.focus('#' + first)
                page.keyboard.press('ArrowLeft')
                assert page.evaluate('document.activeElement.id') == first, 'Left on the first tab left the strip'
                page.keyboard.press('ArrowRight')
                second = page.evaluate('document.activeElement.id')
                assert second != first and page.evaluate('document.activeElement.classList.contains("tab-btn")')
                assert page.get_attribute('#' + second, 'aria-selected') == 'true'
                # A chip row bound by the panel UI moves one chip per key press.
                page.evaluate("""() => {
                    const row = document.createElement('div');
                    row.id = 'chip-row';
                    row.innerHTML = [0, 1, 2, 3].map(i => `<button id="chip-${i}" type="button">${i}</button>`).join('');
                    document.body.appendChild(row);
                    window.FXRouteMeasurementPanelUI.bindChipArrowKeyNavigation(row, 'button');
                }""")
                page.focus('#chip-0')
                page.keyboard.press('ArrowRight')
                assert page.evaluate('document.activeElement.id') == 'chip-1', 'chip row skipped a chip'
                page.keyboard.press('ArrowLeft')
                page.keyboard.press('ArrowLeft')
                assert page.evaluate('document.activeElement.id') == 'chip-3', 'chip row lost its wrap-around'
                # Up/Down still belong to the spatial navigation.
                page.keyboard.press('ArrowUp')
                assert page.evaluate('document.activeElement.id') != 'chip-3'

            def initial_context_focus(page):
                page.evaluate("""() => {
                    document.querySelector('#source-switcher').classList.remove('hidden');
                    const s = document.querySelector('#source-select');
                    s.innerHTML = Array.from({length:30}, (_,i)=>`<option value="${i}">Input ${i}</option>`).join('');
                    s.value = '29';
                    document.body.dispatchEvent(new MouseEvent('contextmenu', {button:-1,bubbles:true,cancelable:true}));
                }""")
                assert page.locator('#source-picker').is_visible(), 'keyboard contextmenu button -1 was ignored'
                assert page.evaluate("""() => {
                    const r = document.activeElement.getBoundingClientRect();
                    return document.activeElement.textContent==='Input 29'
                        && document.activeElement.classList.contains('tv-nav-focus')
                        && r.top>=0 && r.bottom<=innerHeight;
                }"""), 'initial context-menu focus is clipped or has no ring'

            def combine_tall_screen(page):
                page.set_viewport_size({'width':1280, 'height':887})
                page.focus('#tab-btn-effects')
                page.keyboard.press('Enter')
                page.keyboard.press('ArrowDown')
                page.focus('#effects-combine-preset-3')
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') == 'effects-combine-preset-name', 'footer steals next focus inside Combine'

            def tile_before_footer(page):
                page.evaluate("""() => {
                    document.body.innerHTML = `<section class="effects-card" style="position:absolute;left:80px;top:100px;width:600px;height:700px">
                      <button id="origin" style="position:absolute;left:344px;top:350px;width:160px;height:46px">Optional</button>
                      <input id="target" style="position:absolute;left:0;top:412px;width:294px;height:46px">
                    </section><footer class="playback-bar" style="top:620px;bottom:auto;height:98px;display:block">
                      <button id="footer-control" style="position:absolute;left:414px;top:0;width:160px;height:46px">Previous</button>
                    </footer>`;
                    document.querySelector('#origin').focus();
                }""")
                page.keyboard.press('ArrowDown')
                assert page.evaluate('document.activeElement.id') == 'target', 'aligned fixed footer beats the next field in the tile'

            def trusted_context_menu(page):
                page.evaluate("""() => {
                    document.addEventListener('contextmenu', e => { window.contextPrevented = e.defaultPrevented; }, true);
                }""")
                page.keyboard.press('Shift+F10')
                assert page.evaluate('window.contextPrevented'), 'native keyboard menu was not prevented'
                assert page.locator('#tv-select-picker').is_visible()

            def home_closes_layers(page):
                # Home arrives as BrowserBack and steps back one tier per press.
                _browser_back(page)
                page.wait_for_function("!document.getElementById('settings-source-select').disabled")
                assert page.locator('#settings-panel').is_visible(), 'Home did not open the settings'
                page.focus('#settings-source-select')
                page.keyboard.press('Enter')
                assert page.locator('#tv-select-picker').is_visible()
                _browser_back(page)
                assert page.locator('#tv-select-picker').count() == 0, 'Home left the select picker open'
                assert page.locator('#settings-panel').is_visible(), 'Home skipped the settings main view'
                _browser_back(page)
                assert not page.locator('#settings-panel').is_visible(), 'Home left the settings open'
                assert page.get_attribute('#tab-btn-radio', 'aria-selected') == 'true', 'Home changed the tab'
                page.evaluate('''() => {
                    const view = document.createElement('div');
                    view.id = 'fake-detail';
                    view.innerHTML = '<button class="album-detail-back" type="button">Back</button>';
                    view.querySelector('button').addEventListener('click', () => view.remove());
                    document.querySelector('#tab-radio').append(view);
                }''')
                _browser_back(page)
                assert page.locator('#fake-detail').count() == 0, 'Home did not leave the detail view'
                assert not page.locator('#settings-panel').is_visible(), 'Home opened the settings from a detail view'
                _browser_back(page)
                assert page.locator('#settings-panel').is_visible(), 'Home with nothing to close did not open the settings'

            def home_in_measurement(page):
                # Inside the assistant Home collapses the setup, then closes the assistant.
                page.click('#tab-btn-effects')
                page.click('#effects-measure-open')
                page.wait_for_selector('#measurement-panel', state='visible')
                page.click('#measurement-setup-toggle')
                assert page.locator('#measurement-setup-card').is_visible(), 'setup did not open'
                _browser_back(page)
                assert page.locator('#measurement-panel').is_visible(), 'Home left the assistant from its setup'
                assert not page.locator('#measurement-setup-card').is_visible(), 'Home kept the setup open'
                page.click('#measurement-sweep-toggle')
                assert page.locator('#measurement-sweep-menu').is_visible()
                _browser_back(page)
                assert page.locator('#measurement-panel').is_visible(), 'Home left the assistant from its menu'
                assert not page.locator('#measurement-sweep-menu').is_visible()
                _browser_back(page)
                assert not page.locator('#measurement-panel').is_visible(), 'Home did not leave the assistant'
                assert page.get_attribute('#tab-btn-effects', 'aria-selected') == 'true'
                assert not page.locator('#settings-panel').is_visible()

            def home_closes_import(page):
                # The stub answers /api/tracks with an object; the Library close path needs a list.
                page.evaluate('state.library.tracks = []')
                for tab, toggle, panel in (('library', '#toggle-import', '#library-import-panel'),
                                           ('effects', '#effects-toggle-import', '#effects-import-panel')):
                    # The offline banner of the stubbed page can cover the tab strip.
                    page.evaluate(f"document.querySelector('#tab-btn-{tab}').click()")
                    page.evaluate(f"document.querySelector('{toggle}').click()")
                    assert page.locator(panel).is_visible(), f'{tab} import did not open'
                    _browser_back(page)
                    assert not page.locator(panel).is_visible(), f'Home left the {tab} import open'
                    assert not page.locator('#settings-panel').is_visible(), f'Home opened the settings over the {tab} import'
                    assert page.get_attribute(toggle, 'aria-expanded') == 'false'

            def library_search_row(page):
                page.click('#tab-btn-library')
                page.wait_for_selector('#library-view-favorites')
                page.focus('#tab-btn-library')
                page.keyboard.press('ArrowDown')
                page.focus('#library-view-favorites')
                page.keyboard.press('ArrowRight')
                assert page.evaluate('document.activeElement.id') == 'library-search', (
                    f"Right from the view tabs left the row for {page.evaluate('document.activeElement.id')}")
                page.keyboard.press('ArrowLeft')
                assert page.evaluate('document.activeElement.id') == 'library-view-favorites'

            def home_stays_on_tab(page):
                page.click('#tab-btn-library')
                _browser_back(page)
                _browser_back(page)
                assert page.get_attribute('#tab-btn-library', 'aria-selected') == 'true', 'Home left the Library tab'

            def history_guard(page):
                # Firefox runs history Back for BrowserBack even when the page
                # cancels the key; the guard keeps the document in place.
                page.evaluate('window.__stillHere = 1')
                page.go_back()
                page.wait_for_timeout(300)
                assert page.evaluate('window.__stillHere') == 1, 'Back left the app'
                assert page.evaluate("location.pathname") == '/static/index.html'
                assert page.evaluate("!!history.state && history.state.fxrouteBackGuard"), 'guard entry was not restored'
                page.go_back()
                page.wait_for_timeout(300)
                assert page.evaluate('window.__stillHere') == 1, 'second Back left the app'
                page.reload()
                page.wait_for_selector('#tab-btn-radio')
                length = page.evaluate('history.length')
                page.reload()
                page.wait_for_selector('#tab-btn-radio')
                assert page.evaluate('history.length') == length, 'reload piled up guard entries'

            for name, test in [
                ('Home closes dialogs and detail views, then opens settings', home_closes_layers),
                ('Home collapses the measurement assistant in steps', home_in_measurement),
                ('Home closes the open Import panels', home_closes_import),
                ('Right from the Library tabs reaches the search field', library_search_row),
                ('Home stays on the current tab', home_stays_on_tab),
                ('history guard keeps Back inside the app', history_guard),
                ('search exit and native typing', search_exit),
                ('typed text keeps Alt+Left and page keys', typed_text_keeps_keys),
                ('Space on the bare page keeps scrolling', space_without_focus),
                ('rows with their own arrow keys move once', own_arrow_rows),
                ('combine name exit', combine_exit),
                ('catalog D-pad access', catalog),
                ('select choose and cancel', select_interaction),
                ('nested and sibling scroll containers', scroll_boundaries),
                ('focus clears fixed playback footer', footer_visibility),
                ('tabs after scroll and text focus', tabs_from_text),
                ('select dialog inside settings', settings_select),
                ('input picker during app playback', app_input_picker),
                ('Tab keeps native text navigation', keyboard_tab),
                ('select survives option replacement', select_poll),
                ('Back shortcut from remote text focus', back_from_text),
                ('initial keyboard context-menu focus', initial_context_focus),
                ('Combine before fixed footer on tall screens', combine_tall_screen),
                ('tile field wins over aligned footer', tile_before_footer),
                ('trusted keyboard context-menu event', trusted_context_menu),
            ]:
                scenario(name, test)
            # The public web demo keeps the browser's own Back.
            demo = browser.new_page(viewport={"width": 1280, "height": 720})
            demo.add_init_script("window.FXROUTE_DEMO_BOOTED = true;")
            demo.add_init_script(STUB)
            demo.goto(f"http://127.0.0.1:{server.server_port}/static/index.html")
            demo.wait_for_selector('#tab-btn-radio')
            assert demo.evaluate('history.state') is None, 'the demo got a history guard'
            count += 1
            print('PASS demo keeps native Back')
            demo.close()
            browser.close()
    finally:
        server.shutdown()
    assert not failures, '\n'.join(failures)
    print(f'tv nav regressions: ok ({count} scenarios)')


if __name__ == '__main__':
    run()
