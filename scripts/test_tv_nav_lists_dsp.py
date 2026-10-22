#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Remote navigation in track lists (album and playlist detail) and DSP effect groups.

Track rows: Up/Down step between the tracks' Play buttons, Left/Right stay
inside one track (Play, Plus, heart), entering a list lands on the first
Play button and the detail's Back button never becomes a track stop. DSP:
Left/Right follow an effect group's control order, retrace the same path
backwards and never jump into another effect group (e.g. Loudness).
"""
import traceback

from test_tv_nav import STUB, _serve

FIXTURE = r"""
window.navRequests = [];
const listFetch = window.fetch;
const listTracks = Array.from({length: 4}, (_, i) => ({
    id: 'trk' + i, title: 'Track ' + i, artist: 'Artist', duration: 60 + i, favorite: false
}));
window.fetch = (url, opts = {}) => {
    const u = String(url);
    window.navRequests.push({url: u, method: opts.method || 'GET'});
    const json = data => Promise.resolve(new Response(JSON.stringify(data), {
        status: 200, headers: {'Content-Type': 'application/json'}
    }));
    if (/\/api\/tracks\/[^/]+\/favorite$/.test(u) && opts.method === 'POST') {
        // A slow answer keeps the heart disabled long enough to drop focus.
        const favorite = JSON.parse(opts.body).favorite;
        return new Promise(resolve => setTimeout(() => resolve(json({favorite})), 150));
    }
    if (u === '/api/tracks' || u === '/api/playlists') return json([]);
    if (/\/api\/albums\/[^/]+\/tracks/.test(u)) return json(listTracks);
    return listFetch(url, opts);
};
"""

LIBRARY_SETUP = """() => {
    switchTab('library');
    Object.assign(state.library, {viewMode:'albums', albumsLoaded:true,
        tracks:[], searchQuery:'', albumDetail:null, playlistDetail:null,
        albums:Array.from({length:4},(_,i)=>({id:'alb'+i,name:'Album '+i,artist:'Artist',favorite:false}))});
    FXRouteLibraryUI.renderAlbums();
}"""

# The upmixer row exists in Surround only; show it the way
# updateEffectsUpmixerUi does for an enabled HQ upmixer without Center.
UPMIXER_SETUP = """() => {
    switchTab('effects');
    const $ = id => document.getElementById(id);
    $('effects-upmixer-row').classList.remove('hidden');
    $('effects-upmixer-enabled').checked = true;
    $('effects-upmixer-details').classList.remove('hidden');
    $('effects-upmixer-mode').value = 'hq';
    $('effects-upmixer-fft-wrap').classList.remove('hidden');
    $('effects-upmixer-center-wrap').classList.add('hidden');
    $('effects-loudness-enabled').checked = true;
    $('effects-loudness-details').classList.remove('hidden');
}"""

ACTIVE = """() => {
    const a = document.activeElement;
    const row = a.closest('.track-item');
    return {
        id: a.id,
        cls: a.className,
        row: row ? row.dataset.trackId : null,
        group: a.closest('.effects-effect')?.id || null,
    };
}"""


def run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('SKIP  test_tv_nav_lists_dsp.py (playwright not installed)')
        return
    server = _serve(port=0)
    failures = []
    passed = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            def scenario(name, test):
                nonlocal passed
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                page.add_init_script(STUB + FIXTURE)
                page.goto(f'http://127.0.0.1:{server.server_port}/static/index.html')
                page.wait_for_timeout(400)
                try:
                    test(page)
                    passed += 1
                    print('PASS', name)
                except Exception as error:
                    traceback.print_exc()
                    failures.append(f'{name}: {error}')
                finally:
                    page.close()

            def active(page):
                return page.evaluate(ACTIVE)

            def press(page, key):
                page.keyboard.press(key)
                page.wait_for_timeout(30)
                return active(page)

            def open_album(page):
                page.evaluate(LIBRARY_SETUP)
                page.wait_for_selector('#albums-grid > :nth-child(4)')
                page.focus('#albums-grid > :nth-child(1)')
                page.keyboard.press('ArrowRight')
                page.keyboard.press('ArrowLeft')
                page.keyboard.press('Enter')
                page.wait_for_selector('#album-detail-tracks .track-item:nth-child(4) .track-play')

            def is_play(state, track):
                return 'track-play' in state['cls'] and state['row'] == track

            def album_rows(page):
                open_album(page)
                state = press(page, 'ArrowDown')
                assert is_play(state, 'trk0'), f'opened album must enter on the first Play: {state}'
                assert is_play(press(page, 'ArrowDown'), 'trk1')
                state = press(page, 'ArrowRight')
                assert 'track-add' in state['cls'] and state['row'] == 'trk1', state
                state = press(page, 'ArrowRight')
                assert 'track-fav' in state['cls'] and state['row'] == 'trk1', state
                state = press(page, 'ArrowRight')
                assert 'track-fav' in state['cls'] and state['row'] == 'trk1', f'Right left the track: {state}'
                assert is_play(press(page, 'ArrowDown'), 'trk2'), 'Down must land on the next Play'
                assert is_play(press(page, 'ArrowLeft'), 'trk2'), 'Left must stop at Play'
                assert is_play(press(page, 'ArrowUp'), 'trk1')
                assert is_play(press(page, 'ArrowUp'), 'trk0')
                state = press(page, 'ArrowUp')
                assert state['row'] is None and state['id'] == 'album-favorite-toggle', f'Up leaves to the header: {state}'

            def back_button(page):
                open_album(page)
                page.focus('#album-detail-back')
                assert is_play(press(page, 'ArrowDown'), 'trk0'), 'Down from Back must enter on the first Play'
                page.focus('#album-detail-back')
                state = press(page, 'ArrowLeft')
                assert state['row'] is None, f'Left from Back must not land in a track: {state}'
                assert state['id'] == 'album-favorite-toggle', state
                assert press(page, 'ArrowRight')['id'] == 'album-detail-back'
                # From the last control of a track, Right never reaches Back.
                page.focus('#album-detail-tracks .track-item:nth-child(1) .track-fav')
                assert press(page, 'ArrowRight')['row'] == 'trk0'

            def heart_keeps_focus(page):
                open_album(page)
                page.keyboard.press('ArrowDown')
                page.keyboard.press('ArrowDown')
                page.keyboard.press('ArrowRight')
                page.keyboard.press('ArrowRight')
                page.evaluate('window.navRequests=[]')
                page.keyboard.press('Enter')
                page.wait_for_timeout(400)
                posts = [r for r in page.evaluate('window.navRequests') if r['method'] == 'POST']
                assert len(posts) == 1 and posts[0]['url'].endswith('/trk1/favorite'), posts
                state = active(page)
                assert 'track-fav' in state['cls'] and state['row'] == 'trk1', f'heart lost focus: {state}'
                assert page.evaluate("document.activeElement.classList.contains('tv-nav-focus')")
                assert is_play(press(page, 'ArrowDown'), 'trk2')

            def playlist_detail_header(page):
                page.evaluate("""() => {
                    switchTab('library');
                    state.playlists = [{id: 'pl0', name: 'List', track_ids: ['trk0', 'trk1'], track_count: 2}];
                    Object.assign(state.library, {viewMode: 'playlists', albumsLoaded: true, albums: [],
                        tracks: ['trk0', 'trk1'].map(id => ({id, title: id, artist: 'Artist', duration: 60, favorite: false})),
                        searchQuery: '', albumDetail: null, playlistDetail: null});
                    FXRouteLibraryUI.openPlaylistDetail('pl0');
                }""")
                page.wait_for_selector('#playlist-detail-tracks .track-item:nth-child(2) .track-play')
                page.focus('#playlist-detail-back')
                assert is_play(press(page, 'ArrowDown'), 'trk0'), 'Down from Back must enter on the first Play'
                page.focus('#playlist-detail-back')
                assert press(page, 'ArrowLeft')['id'] == 'playlist-detail-download', 'Left from Back reaches the export'
                assert press(page, 'ArrowRight')['id'] == 'playlist-detail-back'
                page.focus('#playlist-detail-download')
                assert press(page, 'ArrowLeft')['id'] == 'playlist-favorite-toggle', 'Left from the export reaches the heart'
                # Backspace still means Back, not the export left of it.
                page.focus('#playlist-detail-tracks .track-item:nth-child(1) .track-play')
                page.keyboard.press('Backspace')
                page.wait_for_selector('#playlist-detail', state='hidden')

            UPMIXER_ORDER = ['effects-upmixer-enabled', 'effects-upmixer-mode', 'effects-upmixer-fft-size',
                             'minus', 'effects-upmixer-surround', 'plus']

            def control_name(page):
                return page.evaluate("""() => {
                    const a = document.activeElement;
                    if (a.id) return a.id;
                    if (a.closest('#effects-upmixer-surround-wrap') && a.matches('.stepper-btn')) {
                        return a === a.parentElement.querySelector('.stepper-btn') ? 'minus' : 'plus';
                    }
                    return a.className;
                }""")

            def upmixer_symmetry(page):
                page.evaluate(UPMIXER_SETUP)
                page.focus('#effects-upmixer-enabled')
                page.keyboard.press('Shift')
                forward = [control_name(page)]
                for _ in range(len(UPMIXER_ORDER)):
                    page.keyboard.press('ArrowRight')
                    forward.append(control_name(page))
                assert forward[:len(UPMIXER_ORDER)] == UPMIXER_ORDER, forward
                assert forward[-1] == 'plus', f'Right at the group end must stop: {forward}'
                backward = [control_name(page)]
                for _ in range(len(UPMIXER_ORDER) - 1):
                    page.keyboard.press('ArrowLeft')
                    backward.append(control_name(page))
                assert backward == UPMIXER_ORDER[::-1], f'Left must retrace Right: {backward}'

            def upmixer_no_group_jump(page):
                page.evaluate(UPMIXER_SETUP)
                page.focus('#effects-upmixer-fft-size')
                page.keyboard.press('Shift')
                seen = []
                for _ in range(6):
                    page.keyboard.press('ArrowLeft')
                    seen.append(active(page))
                groups = [state['group'] for state in seen]
                assert 'effects-loudness-row' not in groups, f'Left jumped into Loudness: {seen}'
                assert [state['id'] for state in seen[:2]] == ['effects-upmixer-mode', 'effects-upmixer-enabled'], seen
                # Leaving the extras card stays among the DSP cards.
                assert page.evaluate("!!document.activeElement.closest('.effects-card')"), seen

            def trail_across_cards(page):
                page.evaluate(UPMIXER_SETUP)
                # Two controls low in the A/B card: plain geometry would not
                # lead back from them to the Protection limiter at the top.
                page.evaluate("""() => document.querySelector('.effects-card-manage-order').insertAdjacentHTML(
                    'beforeend', '<div style="display:flex;gap:24px;margin-top:auto">'
                    + '<button id="ab-probe-1" type="button">1</button><button id="ab-probe-2" type="button">2</button></div>')""")
                page.focus('#effects-limiter-enabled')
                page.keyboard.press('Shift')
                path = [control_name(page)]
                for _ in range(2):
                    page.keyboard.press('ArrowLeft')
                    path.append(control_name(page))
                assert path == ['effects-limiter-enabled', 'ab-probe-2', 'ab-probe-1'], path
                back = [path[-1]]
                for _ in range(2):
                    page.keyboard.press('ArrowRight')
                    back.append(control_name(page))
                assert back == path[::-1], f'Right must retrace Left across cards: {path} / {back}'
                # Once retraced, Right follows the geometry again.
                page.focus('#ab-probe-2')
                page.keyboard.press('ArrowRight')
                assert control_name(page) != 'effects-limiter-enabled'

            scenario('album detail rows step on Play buttons', album_rows)
            scenario('detail Back stays out of the track navigation', back_button)
            scenario('track heart keeps focus through its request', heart_keeps_focus)
            scenario('playlist detail header: Back, export and heart', playlist_detail_header)
            scenario('upmixer Left retraces Right', upmixer_symmetry)
            scenario('upmixer Left never jumps into Loudness', upmixer_no_group_jump)
            scenario('DSP Left retraces Right across cards', trail_across_cards)
            browser.close()
    finally:
        server.shutdown()
    assert not failures, '\n'.join(failures)
    print(f'list and DSP navigation: {passed} scenarios passed')


if __name__ == '__main__':
    run()
