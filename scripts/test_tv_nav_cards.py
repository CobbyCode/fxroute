#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered card/heart routing, isolated activation and focus-contrast checks."""

import traceback
from test_tv_nav import STUB, _serve


FIXTURE = r"""
window.navRequests = [];
const navFetch = window.fetch;
const navItems = Array.from({length: 8}, (_, i) => ({
    id: 'nav' + i, name: 'Album ' + i, title: 'Album ' + i,
    artist: 'Artist', favorite: false, track_count: 1
}));
const navRemovedIds = new Set();
window.fetch = (url, opts = {}) => {
    const u = String(url);
    window.navRequests.push({url: u, method: opts.method || 'GET'});
    const json = data => Promise.resolve(new Response(JSON.stringify(data), {
        status: 200, headers: {'Content-Type': 'application/json'}
    }));
    if (u.endsWith('/favorite') && opts.method === 'POST') {
        const favorite = JSON.parse(opts.body).favorite;
        if (u.includes('/tidal/') && !favorite) navRemovedIds.add(u.split('/').at(-2));
        return json({favorite});
    }
    if (u === '/api/tracks' || u === '/api/playlists') return json([]);
    if (/\/api\/albums\/[^/]+\/tracks/.test(u)) return json([{id: 'track0', title: 'Track', duration: 60}]);
    if (u.includes('/api/streaming/providers/discovery')) return json({providers: window.navTidalEnabled ? [{id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, enabled: true}] : []});
    if (u.includes('/api/streaming/tidal/library/snapshot')) return json({});
    if (u.includes('/api/streaming/tidal/favorites/ids')) return json({albums: navItems.filter(i=>!navRemovedIds.has(i.id)).map(i=>i.id), artists: [], playlists: [], tracks: []});
    if (u.includes('/api/streaming/tidal/favorites?')) return json(navItems.filter(i=>!navRemovedIds.has(i.id)));
    if (u.endsWith('/api/streaming/tidal/playlists')) return json(navItems);
    if (/\/api\/streaming\/tidal\/(albums|playlists)\/nav\d+$/.test(u)) return json({title:'Album',tracks:[]});
    if (u.includes('/api/streaming/tidal/status')) return json({installed:true,available:true,authenticated:true,capabilities:{},status:'Stopped'});
    return navFetch(url, opts);
};
"""

LIBRARY_SETUP = """() => {
    switchTab('library');
    Object.assign(state.library, {viewMode:'albums', albumsLoaded:true,
        tracks:[], searchQuery:'', albumDetail:null, playlistDetail:null,
        albums:Array.from({length:8},(_,i)=>({id:'nav'+i,name:'Album '+i,artist:'Artist',favorite:false}))});
    FXRouteLibraryUI.renderAlbums();
}"""

# TIDAL favorite albums listed in the Library Albums view (TIDAL has no tab).
TIDAL_SETUP = """async () => {
    switchTab('library');
    Object.assign(state.library, {viewMode:'albums', albumsLoaded:true,
        tracks:[], searchQuery:'', albumDetail:null, playlistDetail:null, albums:[]});
    window.navTidalEnabled = true;
    await FXRouteStreaming.refreshEnabledFlags();
    FXRouteStreaming.renderProvider('tidal', {installed:true, available:true, authenticated:true, capabilities:{}, status:'Stopped', user:{id:'7'}});
    await FXRouteStreaming.tidal.ensureLibrary(true);
    FXRouteLibraryUI.renderAlbums();
    // Let the Library finish its own TIDAL-driven re-renders before the test
    // styles and focuses the grid.
    await new Promise(resolve => setTimeout(resolve, 300));
}"""

RADIO_SETUP = """() => {
    switchTab('radio');
    radioModule.setStations(Array.from({length:8},(_,i)=>({id:'nav'+i,title:'Station '+i,stream_url:'https://example.invalid/'+i})));
}"""


def contrast(a, b):
    def luminance(rgb):
        channels = [n / 255 for n in rgb]
        channels = [n / 12.92 if n <= .04045 else ((n + .055) / 1.055) ** 2.4 for n in channels]
        return sum(n * weight for n, weight in zip(channels, (.2126, .7152, .0722)))
    light, dark = sorted((luminance(a), luminance(b)), reverse=True)
    return (light + .05) / (dark + .05)


def run():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('SKIP  test_tv_nav_cards.py (playwright not installed)')
        return
    server = _serve(port=0)
    failures = []
    passed = 0
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()

            def scenario(name, test):
                nonlocal passed
                page = browser.new_page(viewport={'width':1280,'height':900})
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

            def setup(page, surface, layout='grid'):
                if surface == 'library':
                    page.evaluate(LIBRARY_SETUP)
                    grid = '#albums-grid'
                elif surface == 'radio':
                    page.evaluate(RADIO_SETUP)
                    grid = '#stations-grid'
                else:
                    page.evaluate(TIDAL_SETUP)
                    grid = '#albums-grid'
                page.wait_for_selector(grid + ' > :nth-child(8)')
                page.eval_on_selector(grid, "(e, list) => {e.classList.toggle('is-list',list); if(!list) {e.style.gridTemplateColumns='repeat(3, 208px)';e.style.justifyContent='start';}}", layout == 'list')
                return grid

            def card_path(page, surface, layout='grid'):
                grid = setup(page, surface, layout)
                card = grid + ' > :nth-child(2)'
                page.focus(card)
                page.keyboard.press('ArrowUp')
                assert page.evaluate("document.activeElement.matches('.album-card-fav,.station-card-fav,.streaming-fav')"), 'Up must enter own heart'
                assert page.locator(card).evaluate('(e)=>e.contains(document.activeElement)')
                page.keyboard.press('ArrowDown')
                assert page.locator(card).evaluate('(e)=>e===document.activeElement'), 'Down must return to own card'
                if layout == 'grid':
                    page.keyboard.press('ArrowRight')
                    assert page.locator(grid + ' > :nth-child(3)').evaluate('(e)=>e===document.activeElement'), 'Right must skip neighboring hearts'
                    page.keyboard.press('ArrowLeft')
                    assert page.locator(card).evaluate('(e)=>e===document.activeElement')
                page.keyboard.press('ArrowDown')
                next_card = grid + (' > :nth-child(3)' if layout == 'list' else ' > :nth-child(5)')
                assert page.locator(next_card).evaluate('(e)=>e===document.activeElement'), 'Down must stay in column'
                page.keyboard.press('ArrowUp')
                assert page.locator(next_card).evaluate('(e)=>e.contains(document.activeElement) && e!==document.activeElement')
                page.keyboard.press('ArrowUp')
                assert page.locator(card).evaluate('(e)=>e===document.activeElement'), 'second Up must use card geometry, not heart offset'

            def favorite_activation(page, surface, key):
                grid = setup(page, surface)
                heart = grid + ' > :nth-child(2) button'
                page.focus(heart)
                page.evaluate('window.navRequests=[]')
                page.keyboard.press(key)
                page.wait_for_timeout(100)
                requests = page.evaluate('window.navRequests')
                favorites = [r for r in requests if r['url'].endswith('/favorite') and r['method'] == 'POST']
                assert len(favorites) == 1, f'heart must toggle once, not open the card: {requests}'
                assert not any('/nav1/tracks' in r['url'] or r['url'].endswith('/albums/nav1') or r['url'].endswith('/playlists/nav1') for r in requests), requests

            def card_activation(page):
                grid = setup(page, 'library')
                page.focus(grid + ' > :nth-child(2)')
                page.evaluate('window.navRequests=[]')
                page.keyboard.press('Enter')
                page.wait_for_timeout(100)
                requests = page.evaluate('window.navRequests')
                assert sum(r['url'].endswith('/nav1/tracks') for r in requests) == 1
                assert not any(r['url'].endswith('/favorite') for r in requests)

            def entry_and_disabled(page):
                grid = setup(page, 'library')
                page.focus('#tab-btn-library')
                for _ in range(12):
                    page.keyboard.press('ArrowDown')
                    if page.evaluate("!!document.activeElement.closest('.album-card')"):
                        break
                assert page.evaluate("document.activeElement.matches('.album-card')"), 'entry must land on a card, not a heart'
                page.focus(grid + ' > :nth-child(5)')
                page.eval_on_selector(grid + ' > :nth-child(5) button', '(e)=>e.disabled=true')
                page.keyboard.press('ArrowUp')
                assert page.locator(grid + ' > :nth-child(2)').evaluate('(e)=>e===document.activeElement'), 'disabled heart must not trap Up'

            def focus_contrast(page):
                page.evaluate("document.querySelector('#tab-radio').insertAdjacentHTML('afterbegin','<button id=green class=btn-primary>Green</button><button id=red class=btn-danger>Red</button>')")
                for selector in ('#green','#red'):
                    page.focus(selector)
                    page.keyboard.press('Enter')
                    page.evaluate("document.activeElement.classList.add('tv-nav-focus')")
                    paint = page.eval_on_selector(selector, "e=>{const s=getComputedStyle(e);const rgb=v=>v.match(/[\\d.]+/g).slice(0,3).map(Number);return {outline:rgb(s.outlineColor),background:rgb(s.backgroundColor),shadow:s.boxShadow}}")
                    # A two-tone frame must have a 3:1 edge against both the
                    # filled control and the dark surrounding surface.
                    import re
                    colors = [paint['outline']] + [[float(v) for v in rgb.split(',')[:3]] for rgb in re.findall(r'rgba?\(([^)]+)\)',paint['shadow'])]
                    assert max(contrast(c,paint['background']) for c in colors) >= 3, paint
                    assert max(contrast(c,(22,22,25)) for c in colors) >= 3, paint

            def brand_clearance(page):
                page.focus('#open-settings')
                page.keyboard.press('ArrowLeft')
                page.focus('#open-settings')
                result = page.eval_on_selector('#open-settings', "e=>{e.classList.add('tv-nav-focus');const s=getComputedStyle(e);return {offset:parseFloat(s.outlineOffset),width:parseFloat(s.outlineWidth)}}")
                assert result['offset'] >= 2, 'inset ring crosses the unpadded wordmark'

            def row_edges(page):
                grid = setup(page, 'library')
                for index, direction in ((3,'ArrowRight'),(7,'ArrowLeft'),(8,'ArrowRight')):
                    card = grid + f' > :nth-child({index})'
                    page.focus(card)
                    page.keyboard.press(direction)
                    assert page.locator(card).evaluate('(e)=>e===document.activeElement'), 'row edge jumped diagonally'
                # The natural desktop grid has an incomplete last row too.
                page.eval_on_selector(grid, '(e)=>e.removeAttribute("style")')
                page.focus(grid + ' > :nth-child(8)')
                page.keyboard.press('ArrowRight')
                assert page.locator(grid + ' > :nth-child(8)').evaluate('(e)=>e===document.activeElement'), 'natural grid jumped to preceding row'

            def favorite_refresh(page, surface):
                grid = setup(page, surface)
                if surface == 'library':
                    page.evaluate("state.playlists=[]; state.library.viewMode='favorites'; state.library.albums.forEach(a=>a.favorite=true); FXRouteLibraryUI.renderFavorites()")
                    card = '[data-album-id="nav1"]'
                    next_card = '[data-album-id="nav2"]'
                else:
                    card = grid + ' > :nth-child(2)'
                    next_card = '#albums-grid .album-card:has([data-fav-id="nav2"])'
                page.focus(card)
                page.keyboard.press('ArrowUp')
                page.keyboard.press('Enter')
                page.wait_for_timeout(200)
                assert page.locator(next_card).evaluate('(e)=>e===document.activeElement'), 'removed favorite lost focus instead of selecting adjacent card'
                assert page.evaluate("document.activeElement.classList.contains('tv-nav-focus')")
                page.keyboard.press('ArrowUp')
                page.keyboard.press('ArrowDown')
                assert page.locator(next_card).evaluate('(e)=>e===document.activeElement'), 'refreshed favorite path traps D-pad'

            for surface in ('library','radio','tidal'):
                scenario(surface + ' grid routing', lambda page, s=surface: card_path(page,s))
                if surface != 'radio':
                    scenario(surface + ' list routing', lambda page, s=surface: card_path(page,s,'list'))
                    for key in ('Enter','Space'):
                        scenario(surface + ' heart ' + key, lambda page,s=surface,k=key: favorite_activation(page,s,k))
            scenario('card OK activates only the album',card_activation)
            scenario('grid entry and disabled heart',entry_and_disabled)
            scenario('filled button focus contrast',focus_contrast)
            scenario('brand ring clears wordmark',brand_clearance)
            scenario('horizontal row edges never jump diagonally',row_edges)
            scenario('Library favorite removal keeps adjacent focus',lambda page: favorite_refresh(page,'library'))
            scenario('Library TIDAL favorite removal keeps adjacent focus',lambda page: favorite_refresh(page,'tidal'))
            browser.close()
    finally:
        server.shutdown()
    assert not failures, '\n'.join(failures)
    print(f'card navigation: {passed} scenarios passed')


if __name__ == '__main__':
    run()
