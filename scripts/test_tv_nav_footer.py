#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered footer zone and cover control checks for the remote.

The playback footer must navigate the same way for radio, library and Spotify
playback: Left/Right stay in the row, Up/Down stay in the column, and only Up
from the top row leaves into the page above. The footer cover is a stop whenever
media is loaded, even without artwork, and opens the details.

Run directly:

    python3 scripts/test_tv_nav_footer.py
"""

import traceback

from test_tv_nav import STUB, _serve

RADIO_STATE = """() => {
    Object.assign(state.playback, {playing: true, paused: false, volume: 40, duration: 0, position: 0,
        current_track: {source: 'radio', id: 'groovesalad', title: 'Groove Salad', artwork_available: false}});
    FXRoutePlaybackUI.updatePlaybackUI();
}"""

LIBRARY_STATE = """() => {
    const tracks = [0, 1, 2].map(i => ({source: 'local', id: 'track' + i, title: 'Track ' + i,
        artist: 'Artist', album: 'Album', duration: 200, cover_available: false}));
    Object.assign(state.playback, {playing: true, paused: false, volume: 40, duration: 200, position: 20,
        current_track: tracks[1], queue: {active: true, index: 1, count: 3, tracks, mode: 'app_replace'}});
    FXRoutePlaybackUI.updatePlaybackUI();
}"""

# Spotify owns the footer through its own state; model its control set.
SPOTIFY_FOOTER = """() => {
    const bar = document.getElementById('playback-bar');
    bar.classList.add('has-media');
    for (const id of ['footer-shuffle', 'btn-previous', 'btn-next', 'footer-loop']) {
        const button = document.getElementById(id);
        button.classList.remove('hidden');
        button.disabled = false;
    }
    document.getElementById('btn-play-pause').disabled = false;
    document.querySelector('.seek-row').classList.remove('hidden');
    document.getElementById('seek-slider').disabled = false;
}"""

# Visible footer stops in reading order, per source.
ORDERS = {
    'radio': ['playback-cover', 'btn-play-pause', 'volume-slider'],
    'library': ['playback-cover', 'track-favorite-btn', 'footer-shuffle', 'btn-previous',
                'btn-play-pause', 'btn-next', 'footer-loop', 'btn-clear-queue', 'volume-slider'],
    'spotify': ['playback-cover', 'footer-shuffle', 'btn-previous', 'btn-play-pause', 'btn-next',
                'footer-loop', 'volume-slider'],
}

FOOTER_STOPS = """() => {
    const bar = document.getElementById('playback-bar');
    const selector = 'button, input, select, [tabindex="0"]';
    return Array.from(bar.querySelectorAll(selector))
        .filter(el => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length > 0
            && el.id !== 'seek-slider')
        .map(el => el.id);
}"""


def run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('SKIP  test_tv_nav_footer.py (playwright not installed)')
        return

    server = _serve(port=0)
    failures = []
    count = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            def scenario(name, test, viewport=(1920, 1080)):
                nonlocal count
                page = browser.new_page(viewport={'width': viewport[0], 'height': viewport[1]})
                page.add_init_script(STUB)
                page.goto(f'http://127.0.0.1:{server.server_port}/static/index.html')
                page.wait_for_timeout(500)
                try:
                    test(page)
                    count += 1
                    print(f'PASS {name}')
                except Exception as error:
                    traceback.print_exc()
                    failures.append(f'{name}: {error}')
                    print(f'FAIL {failures[-1]}')
                finally:
                    page.close()

            def active(page):
                return page.evaluate('document.activeElement.id')

            def prepare(page, kind):
                if kind == 'radio':
                    page.evaluate(RADIO_STATE)
                elif kind == 'library':
                    page.evaluate(LIBRARY_STATE)
                else:
                    page.evaluate(RADIO_STATE)
                    page.evaluate(SPOTIFY_FOOTER)
                page.wait_for_timeout(150)
                stops = page.evaluate(FOOTER_STOPS)
                assert stops[:len(ORDERS[kind])] == ORDERS[kind] or stops == ORDERS[kind], (kind, stops)
                # Keep the page on the tab strip so the remote mode is active.
                page.focus('#tab-btn-radio')
                page.keyboard.press('ArrowDown')

            def row_walk(kind):
                def test(page):
                    prepare(page, kind)
                    order = ORDERS[kind]
                    page.focus('#' + order[0])
                    page.keyboard.press('ArrowLeft')
                    assert active(page) == order[0], f'{kind}: Left left the footer for {active(page)}'
                    for expected in order[1:]:
                        page.keyboard.press('ArrowRight')
                        assert active(page) == expected, f'{kind}: Right expected {expected}, got {active(page)}'
                    page.keyboard.press('ArrowRight')
                    assert active(page) == order[-1], f'{kind}: Right left the footer for {active(page)}'
                    for expected in reversed(order[:-1]):
                        page.keyboard.press('ArrowLeft')
                        assert active(page) == expected, f'{kind}: Left expected {expected}, got {active(page)}'
                    assert page.evaluate("document.activeElement.classList.contains('tv-nav-focus')")
                return test

            def column_walk(kind):
                def test(page):
                    prepare(page, kind)
                    has_seek = page.evaluate("!document.querySelector('.seek-row').classList.contains('hidden')")
                    for stop in ('playback-cover', 'volume-slider'):
                        page.focus('#' + stop)
                        page.keyboard.press('ArrowDown')
                        assert active(page) == stop, f'{kind}: Down from {stop} left to {active(page)}'
                    page.focus('#btn-play-pause')
                    page.keyboard.press('ArrowDown')
                    if has_seek:
                        assert active(page) == 'seek-slider', f'{kind}: Down from play reached {active(page)}'
                        page.keyboard.press('ArrowDown')
                        assert active(page) == 'seek-slider', f'{kind}: Down from seek left the footer'
                        page.keyboard.press('ArrowLeft')
                        page.keyboard.press('ArrowRight')
                        assert active(page) == 'seek-slider', f'{kind}: Left/Right left the seek row'
                        page.keyboard.press('ArrowUp')
                        assert page.evaluate("!!document.activeElement.closest('.transport-controls')"), (
                            f'{kind}: Up from seek reached {active(page)}')
                    else:
                        assert active(page) == 'btn-play-pause', f'{kind}: Down from play left the footer'
                return test

            def leave_upwards(kind):
                def test(page):
                    prepare(page, kind)
                    for stop in ('playback-cover', 'btn-play-pause', 'volume-slider'):
                        page.focus('#' + stop)
                        page.keyboard.press('ArrowUp')
                        outside = page.evaluate("!document.activeElement.closest('#playback-bar')")
                        assert outside, f'{kind}: Up from {stop} stayed in the footer ({active(page)})'
                        assert page.evaluate("""() => {
                            const r = document.activeElement.getBoundingClientRect();
                            const bar = document.getElementById('playback-bar').getBoundingClientRect();
                            return r.bottom <= bar.top + 1;
                        }"""), f'{kind}: Up from {stop} landed behind the footer'
                return test

            def leave_to_visible_control(page):
                page.evaluate("""() => {
                    document.body.innerHTML = `
                      <button id="far" style="position:absolute;top:-300px;width:120px;height:44px">Far</button>
                      <button id="near" style="position:absolute;left:40px;top:300px;width:120px;height:44px">Near</button>
                      <footer class="playback-bar"><button id="play" style="width:60px;height:44px">Play</button></footer>`;
                    const play = document.getElementById('play').getBoundingClientRect();
                    document.getElementById('far').style.left = (play.left + play.width / 2 - 60) + 'px';
                    document.getElementById('play').focus();
                }""")
                page.keyboard.press('ArrowUp')
                assert active(page) == 'near', f'Up from the footer reached {active(page)} above the viewport'

            def page_before_footer(page):
                # The fixed footer is always close; Down must still walk the
                # rest of the page (DSP tiles below the Tone effect) first.
                page.evaluate("""() => {
                    document.body.innerHTML = `
                      <button id="upper" style="position:absolute;left:600px;top:200px;width:40px;height:40px">A</button>
                      <button id="lower" style="position:absolute;left:300px;top:1500px;width:300px;height:40px">B</button>
                      <div style="position:absolute;left:0;top:1900px;width:1px;height:1px"></div>
                      <footer class="playback-bar"><button id="play" style="width:60px;height:44px">Play</button></footer>`;
                    const play = document.getElementById('play').getBoundingClientRect();
                    document.getElementById('upper').style.left = (play.left + play.width / 2 - 20) + 'px';
                    document.getElementById('upper').focus();
                }""")
                page.keyboard.press('ArrowDown')
                assert active(page) == 'lower', f'Down skipped the page content for {active(page)}'
                page.keyboard.press('ArrowDown')
                assert active(page) == 'play', f'Down from the last page control reached {active(page)}'

            def vanished_control_keeps_footer_focus(page):
                prepare(page, 'library')
                page.focus('#btn-play-pause')
                page.keyboard.press('ArrowRight')
                page.keyboard.press('ArrowRight')
                page.keyboard.press('ArrowRight')
                assert active(page) == 'btn-clear-queue', active(page)
                page.evaluate("document.getElementById('btn-clear-queue').classList.add('hidden')")
                page.wait_for_timeout(100)
                assert page.evaluate("!!document.activeElement.closest('#playback-bar')"), (
                    f'a vanished footer control dropped the focus to {active(page)}')
                assert page.evaluate("document.activeElement.classList.contains('tv-nav-focus')")

            def disabled_control_keeps_footer_focus(page):
                prepare(page, 'library')
                page.focus('#btn-play-pause')
                page.keyboard.press('ArrowRight')
                assert active(page) == 'btn-next', active(page)
                page.evaluate("document.getElementById('btn-next').disabled = true")
                page.wait_for_timeout(100)
                if page.evaluate("document.activeElement === document.body"):
                    page.keyboard.press('ArrowLeft')
                assert page.evaluate("!!document.activeElement.closest('#playback-bar')"), (
                    f'a disabled footer control sent the next key to {active(page)}')

            def enter_footer_from_page(page):
                prepare(page, 'library')
                page.focus('#tab-btn-radio')
                reached = False
                for _ in range(14):
                    page.keyboard.press('ArrowDown')
                    if page.evaluate("!!document.activeElement.closest('#playback-bar')"):
                        reached = True
                        break
                assert reached, 'Down never reaches the footer'

            def cover_without_artwork(page):
                page.evaluate(RADIO_STATE)
                page.wait_for_timeout(150)
                assert page.evaluate("!document.getElementById('playback-cover').classList.contains('hidden')"), (
                    'footer cover is not a control without artwork')
                assert 'artwork-placeholder' in page.get_attribute('#playback-cover', 'src')
                page.focus('#btn-play-pause')
                page.keyboard.press('ArrowLeft')
                assert active(page) == 'playback-cover', f'Left from play reached {active(page)}'
                page.keyboard.press('Enter')
                page.wait_for_timeout(200)
                assert page.evaluate("!document.getElementById('cover-detail-card').classList.contains('hidden')"), (
                    'OK on the cover did not open the details')
                page.keyboard.press('Backspace')
                page.wait_for_timeout(200)
                assert page.evaluate("document.getElementById('cover-detail-card').classList.contains('hidden')")
                assert active(page) == 'playback-cover', 'closing the details lost the cover focus'

            def cover_idle(page):
                assert page.evaluate("document.getElementById('playback-cover').classList.contains('hidden')"), (
                    'idle footer offers an empty details view')

            def cover_artwork_error(page):
                page.evaluate("""() => {
                    Object.assign(state.playback, {playing: true, paused: false, volume: 40,
                        current_track: {source: 'radio', id: 'x', title: 'X', artwork_available: true,
                            artwork_url: '/missing-cover.png'}});
                    FXRoutePlaybackUI.updatePlaybackUI();
                }""")
                page.wait_for_function("document.getElementById('playback-cover').src.includes('artwork-placeholder')")
                assert page.evaluate("!document.getElementById('playback-cover').classList.contains('hidden')")

            for kind in ('radio', 'library', 'spotify'):
                scenario(f'footer row order and edges ({kind})', row_walk(kind))
                scenario(f'footer columns stay inside ({kind})', column_walk(kind))
                scenario(f'footer Up leaves above the bar ({kind})', leave_upwards(kind))
            scenario('footer row order at TV size', row_walk('radio'), viewport=(1280, 720))
            scenario('footer row order at TV size (library)', row_walk('library'), viewport=(1280, 720))
            scenario('footer Up prefers a control in view', leave_to_visible_control)
            scenario('Down walks the page before the footer', page_before_footer)
            scenario('footer keeps the focus when a control vanishes', vanished_control_keeps_footer_focus)
            scenario('footer keeps the focus when a control is disabled', disabled_control_keeps_footer_focus)
            scenario('footer is reachable from the page', enter_footer_from_page)
            scenario('cover without artwork opens the details', cover_without_artwork)
            scenario('idle footer has no cover control', cover_idle)
            scenario('failed artwork falls back to the placeholder control', cover_artwork_error)
            browser.close()
    finally:
        server.shutdown()
    assert not failures, '\n'.join(failures)
    print(f'tv nav footer: ok ({count} scenarios)')


if __name__ == '__main__':
    run()
