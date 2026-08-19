#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Functional checks for the shared streaming UI.
//
// Unlike test_streaming_ui_structure.js (string/static checks), this test
// actually executes static/streaming.js against a minimal DOM shim and
// asserts that:
//
//   1. each provider's transport buttons are bound to a real action (Spotify
//      -> app.js playerctl adapter, Qobuz -> /api/streaming/qobuz/..., TIDAL
//      -> /api/playback/...), and
//   2. a status refresh does not rebuild TIDAL browse content (which used to
//      wipe search results and the login input on every poll).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');

// --- minimal DOM shim -------------------------------------------------------

function makeEl() {
    const listeners = {};
    const childEls = {};
    const el = {
        style: {},
        classList: {
            _set: new Set(),
            add(...c) { c.forEach((x) => el.classList._set.add(x)); },
            remove(...c) { c.forEach((x) => el.classList._set.delete(x)); },
            toggle(c, force) {
                const on = force === undefined ? !el.classList._set.has(c) : !!force;
                if (on) el.classList._set.add(c); else el.classList._set.delete(c);
                return on;
            },
            contains(c) { return el.classList._set.has(c); },
        },
        dataset: {},
        hidden: false,
        textContent: '',
        innerHTML: '',
        title: '',
        value: '',
        disabled: false,
        className: '',
        type: '',
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        dispatch(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev || {})); },
        click() {
            el.dispatch('click', {
                target: el,
                preventDefault() {},
                stopPropagation() {},
            });
        },
        setAttribute(name, value) { el[name] = value; },
        getAttribute(name) { return el[name] != null ? String(el[name]) : null; },
        querySelector(sel) {
            if (!childEls[sel]) childEls[sel] = makeEl();
            return childEls[sel];
        },
        querySelectorAll(sel) {
            // Favorite hearts are rendered inside row innerHTML; surface the
            // button (with its dataset) so bindTidalFavoriteButtons and the
            // tests can exercise the write-back click path.
            if (sel === '.streaming-fav, .track-fav') {
                if (!el._favBtn) {
                    const m = el.innerHTML.match(/data-fav-type="([^"]+)" data-fav-id="([^"]+)"/);
                    const btn = makeEl();
                    btn.className = 'streaming-fav';
                    btn.dataset.favType = m ? m[1] : '';
                    btn.dataset.favId = m ? m[2] : '';
                    el._favBtn = btn;
                }
                return [el._favBtn];
            }
            return [];
        },
        appendChild() {},
        closest() { return null; },
    };
    return el;
}

function shellEl(providerId) {
    const el = makeEl();
    el['data-provider'] = providerId;
    const memo = {};
    // Browse tabs are real interactive elements in the shim so sections and
    // detail views can be entered by clicking them.
    const tabs = ['favorites', 'playlists'].map((name) => {
        const t = makeEl();
        t.dataset.browse = name;
        return t;
    });
    el.querySelector = (sel) => {
        if (!memo[sel]) {
            memo[sel] = makeEl();
            memo[sel].querySelectorAll = (inner) => {
                if (inner === '.streaming-browse-tab') return tabs;
                return [];
            };
            memo[sel].querySelector = (inner) => {
                const key = sel + ' ' + inner;
                if (!memo[key]) memo[key] = makeEl();
                return memo[key];
            };
        }
        return memo[sel];
    };
    return el;
}

function buildDom() {
    const providers = ['spotify', 'qobuz', 'tidal'];
    const shells = {};
    const tabButtons = {};
    const tabPanels = {};
    const byId = {};
    const createdEls = [];
    const tidalSearchTypeButtons = {};
    for (const type of ['artists', 'tracks', 'albums', 'playlists']) {
        const button = makeEl();
        button.dataset.searchType = type;
        tidalSearchTypeButtons[type] = button;
    }
    const tidalFavoriteTypeButtons = {};
    for (const type of ['tracks', 'albums', 'artists']) {
        const button = makeEl();
        button.dataset.type = type;
        tidalFavoriteTypeButtons[type] = button;
    }
    const tidalBrowseBody = makeEl();
    const tidalBrowseBodyEls = {};
    tidalBrowseBody.querySelectorAll = (sel) => {
        if (sel === '#tidal-search-result-types .streaming-chip') return Object.values(tidalSearchTypeButtons);
        if (sel === '#tidal-fav-types .streaming-chip') return Object.values(tidalFavoriteTypeButtons);
        return [];
    };
    tidalBrowseBody.querySelector = (sel) => {
        const typeMatch = sel.match(/^#tidal-search-type-(artists|tracks|albums|playlists)$/);
        if (typeMatch) return tidalSearchTypeButtons[typeMatch[1]];
        if (!tidalBrowseBodyEls[sel]) tidalBrowseBodyEls[sel] = makeEl();
        return tidalBrowseBodyEls[sel];
    };
    for (const pid of providers) {
        shells[pid] = shellEl(pid);
        tabButtons[pid] = makeEl();
        tabPanels[pid] = makeEl();
    }
    const document = {
        hidden: false,
        querySelectorAll(sel) {
            if (sel === '.streaming-shell[data-provider]') return providers.map((p) => shells[p]);
            return [];
        },
        querySelector(sel) {
            const m = sel.match(/\.tab-btn\[data-tab="(\w+)"\]/);
            return m ? tabButtons[m[1]] : null;
        },
        getElementById(id) {
            const m = id.match(/^tab-(\w+)$/);
            if (m) return tabPanels[m[1]];
            if (id === 'tidal-browse-body') return tidalBrowseBody;
            if (!byId[id]) byId[id] = makeEl();
            return byId[id];
        },
        createElement() {
            const el = makeEl();
            createdEls.push(el);
            return el;
        },
    };
    return { document, shells, tabButtons, tabPanels, createdEls };
}

function runStreaming(options = {}) {
    const { document, shells, createdEls } = buildDom();
    const fetchCalls = [];
    const spotifyCommandCalls = [];
    const spotifySeekCalls = [];
    const tidalFavoriteTracks = options.tidalFavoriteTracks || [];

    const sandbox = {
        document,
        window: {
            __visibleTab: 'radio',
            setInterval: () => 0,
            clearInterval: () => {},
        },
        setInterval: () => 0,
        clearInterval: () => {},
        fetch: async (url, opts) => {
            fetchCalls.push({ url: String(url), opts: opts || {} });
            const u = String(url);
            let body = {};
            if (u === '/api/streaming/tidal/playlists') {
                body = [{ id: 'pl-1', name: 'Test Playlist', art_url: '', track_count: 2 }];
            } else if (u === '/api/streaming/tidal/playlists/pl-1/tracks') {
                body = [
                    { id: 'p1', title: 'Playlist One', artist: 'Found Artist', duration: 10 },
                    { id: 'p2', title: 'Playlist Two', artist: 'Found Artist', duration: 12 },
                ];
            } else if (u.includes('/api/streaming/tidal/search?q=')) {
                if (u.includes('types=artists')) body = { artists: [{ id: 'a1', name: 'Found Artist', art_url: '' }] };
                else if (u.includes('types=albums')) body = { albums: [{ id: 'al1', title: 'Found Album', artist: 'Found Artist', art_url: '' }] };
                else if (u.includes('types=playlists')) body = { playlists: [{ id: 'p1', name: 'Found Playlist', art_url: '' }] };
                else body = { tracks: [
                    { id: 's1', title: 'Found Song', artist: 'Found Artist', album: 'Found Album', art_url: '', duration: 10 },
                    { id: 's2', title: 'Second Song', artist: 'Found Artist', album: 'Found Album', art_url: '', duration: 12 },
                ] };
            } else if (u === '/api/streaming/tidal/artists/a1') {
                body = {
                    id: 'a1', name: 'Found Artist', art_url: '',
                    albums: [{ id: 'al1', title: 'Found Album', artist: 'Found Artist', art_url: '' }],
                    top_tracks: [{ id: 's1', title: 'Found Song', artist: 'Found Artist', album: 'Found Album', art_url: '', duration: 10 }],
                };
            } else if (u === '/api/streaming/tidal/albums/al1') {
                body = { id: 'al1', title: 'Found Album', artist: 'Found Artist', year: 1996, audio_quality: 'LOSSLESS', num_tracks: 1 };
            } else if (u === '/api/streaming/tidal/albums/al1/tracks') {
                body = [{ id: 's1', title: 'Found Song', artist: 'Found Artist', album: 'Found Album', art_url: '', duration: 10 }];
            } else if (u === '/api/streaming/tidal/favorites/ids') {
                body = { tracks: [], albums: [], artists: [], playlists: [] };
            } else if (u.startsWith('/api/streaming/tidal/favorites?type=')) {
                if (u.includes('type=tracks')) body = tidalFavoriteTracks;
                else if (u.includes('type=artists')) body = [{ id: 'a1', name: 'Found Artist', art_url: '' }];
            } else if (u.startsWith('/api/streaming/tidal/') && u.endsWith('/favorite')) {
                body = { favorite: !!JSON.parse((opts && opts.body) || '{}').favorite };
            }
            return { ok: true, json: async () => body };
        },
        console,
        Date,
        String,
        Number,
        Object,
        JSON,
        RegExp,
        encodeURIComponent,
        navigator: undefined,
    };
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);

    const api = {
        showToast() {},
        escapeHtml: (v) => String(v),
        formatTime: () => '0:00',
        formatRateKhz: (v) => String(v),
        trackRowHtml: ({ index, title, sub, favoriteButton, duration }) =>
            `<span class="track-index">${index}</span><button class="track-play">▶</button>` +
            `<div class="track-info"><div class="track-title">${title}</div>` +
            (sub ? `<div class="track-sub">${sub}</div>` : '') + `</div>` +
            favoriteButton + (duration ? `<span class="track-duration">${duration}</span>` : ''),
        spotifyCommand: (...a) => { spotifyCommandCalls.push(a); return Promise.resolve(); },
        spotifySeek: (...a) => { spotifySeekCalls.push(a); return Promise.resolve(); },
    };
    sandbox.window.FXRouteStreaming.init(api);
    return { sandbox, shells, fetchCalls, spotifyCommandCalls, spotifySeekCalls, createdEls };
}

const baseCaps = {
    transport: true, seek: true, shuffle: true, loop: true, progress: true, cover: true,
    audio_format: true, bit_depth: true, sample_rate: true,
};

async function main() {
// --- 1. transport buttons are bound to real actions -------------------------

{
    const { sandbox, shells, fetchCalls, spotifyCommandCalls } = runStreaming();
    const spotifyData = { installed: true, available: true, authenticated: null, capabilities: baseCaps, status: 'Paused', title: 't', artist: 'a', album: 'b', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 100 };

    sandbox.window.FXRouteStreaming.renderProvider('spotify', spotifyData);
    shells.spotify.querySelector('[data-action="toggle"]').click();
    assert.deepEqual(spotifyCommandCalls, [['toggle']], 'Spotify toggle must dispatch to the app.js playerctl adapter');
    spotifyCommandCalls.length = 0;
    shells.spotify.querySelector('[data-action="next"]').click();
    assert.deepEqual(spotifyCommandCalls, [['next']], 'Spotify next must dispatch to the app.js playerctl adapter');

    // Qobuz toggle -> remote endpoint
    const qobuzData = { ...spotifyData, installed: true, available: true, authenticated: true, capabilities: baseCaps };
    sandbox.window.FXRouteStreaming.renderProvider('qobuz', qobuzData);
    fetchCalls.length = 0;
    shells.qobuz.querySelector('[data-action="toggle"]').click();
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/qobuz/toggle' && c.opts.method === 'POST'), 'Qobuz toggle must POST /api/streaming/qobuz/toggle');

    // TIDAL toggle -> native playback endpoint
    const tidalData = { ...spotifyData, installed: true, available: true, authenticated: true, capabilities: baseCaps };
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    fetchCalls.length = 0;
    shells.tidal.querySelector('[data-action="toggle"]').click();
    assert.ok(fetchCalls.some((c) => c.url === '/api/playback/toggle' && c.opts.method === 'POST'), 'TIDAL toggle must POST /api/playback/toggle');
}

// --- 2. status refresh does not wipe TIDAL browse content -------------------

{
    const { sandbox, shells } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    // First render builds the browse surface.
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    assert.ok(content.innerHTML.includes('streaming-browse'), 'first render must build the TIDAL browse surface');

    // Simulate the user's search results being written into the browse body.
    content.innerHTML = '<div class="streaming-results">25 tracks</div>';

    // A subsequent status refresh (poll/transport/nudge) must leave it intact.
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    assert.equal(content.innerHTML, '<div class="streaming-results">25 tracks</div>',
        'status refresh must not rebuild (and wipe) TIDAL browse content');

    // A real mode change (login -> browse) must still rebuild.
    content.innerHTML = '<div class="streaming-results">25 tracks</div>';
    sandbox.window.FXRouteStreaming.renderProvider('tidal', { ...tidalData, authenticated: false });
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    assert.ok(content.innerHTML.includes('streaming-browse'), 'auth change must rebuild the browse surface');
}

// --- 3. empty state and now-playing card are mutually exclusive ------------

{
    const { sandbox, shells } = runStreaming();
    const playing = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Playing', title: 't', artist: 'a', album: 'b', artUrl: 'http://x/c.jpg', shuffle: false, loop: 'none', position: 0, duration: 100 };
    sandbox.window.FXRouteStreaming.renderProvider('qobuz', playing);
    assert.equal(shells.qobuz.querySelector('.streaming-empty').hidden, true,
        'now-playing render must hide the empty state');
    assert.equal(shells.qobuz.querySelector('.streaming-now-playing').hidden, false,
        'now-playing render must show the now-playing card');

    const stopped = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
    sandbox.window.FXRouteStreaming.renderProvider('tidal', stopped);
    assert.equal(shells.tidal.querySelector('.streaming-empty').hidden, true,
        'idle TIDAL must not show a large empty player card');
    assert.equal(shells.tidal.querySelector('.streaming-now-playing').hidden, true,
        'idle TIDAL must hide the now-playing card (no ghost transport)');
    assert.ok(shells.tidal.querySelector('.streaming-content').innerHTML.includes('streaming-browse'),
        'idle TIDAL must keep Browse immediately available');

    // Playing TIDAL must still not render a second in-tab player: the global
    // footer is the authoritative player, browse stays the main content.
    const tidalPlaying = { ...stopped, status: 'Playing', title: 'One More Time', artist: 'Daft Punk', album: 'Discovery', artUrl: 'http://x/c.jpg', duration: 100 };
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalPlaying);
    assert.equal(shells.tidal.querySelector('.streaming-now-playing').hidden, true,
        'playing TIDAL must not show a duplicate in-tab player (the footer is the player)');
    assert.equal(shells.tidal.querySelector('.streaming-empty').hidden, true,
        'playing TIDAL must not show an empty-state card');
    assert.ok(shells.tidal.querySelector('.streaming-content').innerHTML.includes('streaming-browse'),
        'playing TIDAL must keep Browse as the main content');
}

// --- 4. Tidal detail views hide the standalone status line ----------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const statusLine = shells.tidal.querySelector('.streaming-status-line');
    const content = shells.tidal.querySelector('.streaming-content');
    assert.equal(statusLine.hidden, false, 'main browse surface must show the status line');
    assert.equal(statusLine.textContent, 'Tidal · Connected', 'status line must show the provider + Connected');

    // Open the Playlists tab and click a playlist row to enter a detail view.
    const tabs = content.querySelectorAll('.streaming-browse-tab');
    tabs.find((t) => t.dataset.browse === 'playlists').click();
    // renderTidalPlaylists fetches asynchronously; let the microtasks run.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const playlistRow = createdEls.find((el) => el.className === 'streaming-result');
    assert.ok(playlistRow, 'a playlist row must be created for the playlists tab');
    playlistRow.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(content.innerHTML.includes('tidal-detail'), 'playlist detail view must render');
    assert.equal(statusLine.hidden, true, 'detail view must hide the standalone status line');
    const playlistTrackRows = createdEls.filter((el) => el.className === 'streaming-result');
    playlistTrackRows.at(-1).querySelector('.track-play').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const playlistPlayCalls = fetchCalls.filter((c) => c.url === '/api/play');
    assert.equal(playlistPlayCalls.length, 1, 'a detail play button must dispatch exactly one playback request');
    const playlistPlayCall = playlistPlayCalls[0];
    assert.deepEqual(JSON.parse(playlistPlayCall.opts.body), {
        source: 'tidal', track_id: 'p2', queue_track_ids: ['p1', 'p2'],
    }, 'playlist detail playback must keep its complete playlist queue');

    // Leaving the detail view restores the status line immediately.
    content.querySelector('#tidal-detail-back').click();
    assert.equal(statusLine.hidden, false, 'back navigation must restore the status line');
}

// --- 5. TIDAL browse: Favorites default + persistent search bar -----------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');

    // First authenticated render lands on Favorites, not an empty Search screen.
    assert.ok(content.innerHTML.includes('streaming-browse'), 'browse surface must render');
    assert.ok(content.innerHTML.includes('id="tidal-search-input"'), 'search bar must be part of the browse surface');
    assert.ok(content.innerHTML.indexOf('tidal-search-input') < content.innerHTML.indexOf('streaming-browse-tabs'),
        'search bar must sit above the browse navigation');
    assert.ok(!content.innerHTML.includes('data-browse="search"'), 'Search must not be a browse tab');
    assert.ok(content.innerHTML.includes('data-browse="favorites"') && content.innerHTML.includes('data-browse="playlists"'),
        'browse navigation must be Favorites and Playlists');
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-types'),
        'first authenticated render must show Favorites content');

    // Keep the last favorite category while switching into and out of search.
    body.querySelectorAll('#tidal-fav-types .streaming-chip').find((tab) => tab.dataset.type === 'albums').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Typing alone must not execute a search.
    const input = content.querySelector('#tidal-search-input');
    input.value = 'daft punk';
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(fetchCalls.filter((c) => c.url.startsWith('/api/streaming/tidal/search?')).length, 0,
        'typing must not trigger live TIDAL search');

    // Executing a search replaces the browse body with results.
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/search?q=daft%20punk')),
        'search must hit the TIDAL search endpoint');
    const resultCount = createdEls.filter((el) => el.className === 'streaming-result').length;
    assert.equal(resultCount, 2, 'search must render all track result rows');
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('Search results for'),
        'search must switch to the dedicated result state');
    assert.ok(!sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-types'),
        'favorite categories must not be visible in search results');
    assert.ok(!body.innerHTML.includes('class="tidal-track-select"'),
        'normal track search results must not show permanent checkboxes');
    assert.ok(!body.innerHTML.includes('tidal-select-all'),
        'selection actions must stay hidden until selection mode is activated');

    // A normal track click uses all currently displayed tracks as a temporary queue.
    const resultRows = createdEls.filter((el) => el.className === 'streaming-result');
    resultRows[1].click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const playCall = fetchCalls.find((c) => c.url === '/api/play');
    assert.ok(playCall, 'clicking a search track must start playback');
    assert.deepEqual(JSON.parse(playCall.opts.body), {
        source: 'tidal', track_id: 's2', queue_track_ids: ['s1', 's2'],
    }, 'search track playback must queue every visible search track');

    // Selection mode is opt-in and can start a queue containing only checked tracks.
    const selectToggle = body.querySelector('#tidal-track-selection-toggle');
    selectToggle.click();
    const selectedRows = createdEls.filter((el) => el.className === 'streaming-result').slice(-2);
    const firstCheckbox = selectedRows[0].querySelector('.tidal-track-select');
    firstCheckbox.dispatch('change', { target: { checked: true } });
    body.querySelector('#tidal-play-selected').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const selectionPlayCalls = fetchCalls.filter((c) => c.url === '/api/play');
    assert.deepEqual(JSON.parse(selectionPlayCalls.at(-1).opts.body), {
        source: 'tidal', track_id: 's1', queue_track_ids: ['s1'],
    }, 'Play selected must queue only checked search tracks');

    // Switching result types reuses the executed query without a second Search click.
    for (const type of ['artists', 'tracks', 'albums', 'playlists']) {
        body.querySelector('#tidal-search-type-' + type).click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/search?q=daft%20punk&types=' + type + '&limit=25'),
            'switching to ' + type + ' must automatically search the saved query');
    }

    // Returning from a detail opened by search restores the executed search view.
    body.querySelector('#tidal-search-type-albums').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    createdEls.filter((el) => el.className === 'streaming-result').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    content.querySelector('#tidal-detail-back').click();
    assert.ok(body.innerHTML.includes('Search results for'),
        'back from a search detail must restore the search result state');

    // A status refresh must not wipe the results (contentKey guard).
    const searchBodyBeforeRefresh = body.innerHTML;
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    assert.equal(body.innerHTML, searchBodyBeforeRefresh,
        'status refresh must not re-render away the search results');

    // Clearing the field itself returns to Favorites without a search request.
    input.value = '';
    input.dispatch('input');
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(body.innerHTML.includes('tidal-fav-types') &&
        body.innerHTML.includes('class="streaming-chip is-active" data-type="albums"'),
        'clearing the search must return to Favorites');

    // Escape resets the search back to the browse section too.
    input.value = 'daft punk';
    content.querySelector('#tidal-search-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    input.dispatch('keydown', { key: 'Escape', preventDefault() {} });
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-types'),
        'Escape must clear the search back to the browse section');

    // Favorites <-> Playlists switching works.
    const tabs = content.querySelectorAll('.streaming-browse-tab');
    tabs.find((t) => t.dataset.browse === 'playlists').click();
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-playlists-results'),
        'Playlists tab must render the playlists section');
    tabs.find((t) => t.dataset.browse === 'favorites').click();
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-types'),
        'Favorites tab must render the favorites section');
}

// --- 6. TIDAL Favorites -> Tracks uses all visible tracks as a queue --------

{
    const favoriteTracks = [
        { id: 'f1', title: 'Favorite One', artist: 'Artist', duration: 10 },
        { id: 'f2', title: 'Favorite Two', artist: 'Artist', duration: 11 },
        { id: 'f3', title: 'Favorite Three', artist: 'Artist', duration: 12 },
        { id: 'f4', title: 'Favorite Four', artist: 'Artist', duration: 13 },
    ];
    const { sandbox, fetchCalls, createdEls } = runStreaming({ tidalFavoriteTracks: favoriteTracks });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));

    const favoriteRows = createdEls.filter((el) => el.className === 'streaming-result');
    assert.equal(favoriteRows.length, 4, 'Favorites Tracks must render all currently displayed tracks');
    favoriteRows[2].click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    const playCalls = fetchCalls.filter((c) => c.url === '/api/play');
    assert.equal(playCalls.length, 1, 'clicking a favorite track must start playback once');
    assert.deepEqual(JSON.parse(playCalls[0].opts.body), {
        source: 'tidal', track_id: 'f3', queue_track_ids: ['f1', 'f2', 'f3', 'f4'],
    }, 'favorite track playback must queue every visible favorite in order at the clicked track');
}

// --- 7. TIDAL artist detail: click from search and favorites ----------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');
    const input = content.querySelector('#tidal-search-input');

    // Search -> artists -> click the artist row: detail must open with the id.
    input.value = 'aphrodite';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    body.querySelector('#tidal-search-type-artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    createdEls.filter((el) => el.className === 'streaming-result').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/artists/a1'),
        'clicking an artist must fetch the artist detail with the artist id');
    assert.ok(content.innerHTML.includes('tidal-detail') && content.innerHTML.includes('tidal-artist-facts'),
        'artist click must open the artist detail view');
    assert.ok(content.innerHTML.includes('Found Artist'), 'artist detail must show the artist name');
    const headings = createdEls.filter((el) => el.className === 'streaming-results-heading');
    assert.ok(headings.some((el) => el.textContent === 'Top Tracks'),
        'artist detail must list top tracks');
    assert.ok(headings.some((el) => el.textContent === 'Albums'),
        'artist detail must list albums');

    // Track click from the artist detail uses the existing queue logic (the
    // album row is rendered last, so the top track is the second-to-last row).
    createdEls.filter((el) => el.className === 'streaming-result').at(-2).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const artistTrackPlay = fetchCalls.filter((c) => c.url === '/api/play').at(-1);
    assert.deepEqual(JSON.parse(artistTrackPlay.opts.body), {
        source: 'tidal', track_id: 's1', queue_track_ids: ['s1'],
    }, 'artist top-track click must play through the native queue');

    // Album click from the artist detail opens the existing album detail
    // (the album row is the last row rendered after the top tracks).
    const albumRow = createdEls.filter((el) => el.className === 'streaming-result').at(-1);
    albumRow.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/albums/al1'),
        'album click from the artist detail must open the album detail');

    // Back from the album restores the artist detail with the artist's own
    // id (nested back state) — never the album id.
    content.querySelector('#tidal-detail-back').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const artistRefetchCount = fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1').length;
    assert.equal(artistRefetchCount, 2, 'back from the album must restore the artist detail');
    assert.ok(!fetchCalls.some((c) => c.url === '/api/streaming/tidal/artists/al1'),
        'the restored artist detail must keep the artist id, not the album id');
}

// --- 8. artist back returns to the previous search --------------------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');
    const input = content.querySelector('#tidal-search-input');

    input.value = 'aphrodite';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    body.querySelector('#tidal-search-type-artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    createdEls.filter((el) => el.className === 'streaming-result').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Back from the artist returns exactly to the executed search results.
    content.querySelector('#tidal-detail-back').click();
    assert.ok(body.innerHTML.includes('Search results for'),
        'back from the artist must restore the previous artist search');
}

// --- 9. same artist detail path from Favorites -> Artists -------------------

{
    const favRun = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
    favRun.sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favBody = favRun.sandbox.document.getElementById('tidal-browse-body');
    const favContent = favRun.shells.tidal.querySelector('.streaming-content');
    favBody.querySelectorAll('#tidal-fav-types .streaming-chip').find((tab) => tab.dataset.type === 'artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    favRun.createdEls.filter((el) => el.className === 'streaming-result').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(favRun.fetchCalls.some((c) => c.url === '/api/streaming/tidal/artists/a1'),
        'clicking a favorited artist must open the same artist detail');
    assert.ok(favContent.innerHTML.includes('tidal-artist-facts'),
        'favorited artist must open the same artist detail view');
    favContent.querySelector('#tidal-detail-back').click();
    assert.ok(favBody.innerHTML.includes('tidal-fav-types') &&
        favBody.innerHTML.includes('data-type="artists"'),
        'back from a favorited artist must restore Favorites -> Artists');
}

// --- 10. artist row fav heart toggles through the write-back endpoint -------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');
    const input = content.querySelector('#tidal-search-input');

    // Search -> Artists: the row heart must carry the artist type + id.
    input.value = 'aphrodite';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    body.querySelector('#tidal-search-type-artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    const artistRow = createdEls.filter((el) => el.className === 'streaming-result').at(-1);
    const heart = artistRow.querySelectorAll('.streaming-fav, .track-fav')[0];
    assert.ok(heart, 'artist rows must render a favorite heart');
    assert.equal(heart.dataset.favType, 'artists', 'the artist heart must carry the artists favorite type');
    assert.equal(heart.dataset.favId, 'a1', 'the artist heart must carry the artist id');

    heart.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    let favCalls = fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1/favorite');
    assert.equal(favCalls.length, 1, 'the artist heart must dispatch exactly one write-back request');
    assert.deepEqual(JSON.parse(favCalls[0].opts.body), { favorite: true },
        'the first artist heart click must add the artist');
    assert.equal(fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1').length, 0,
        'the artist heart click must not open the artist detail (stopPropagation)');

    heart.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    favCalls = fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1/favorite');
    assert.equal(favCalls.length, 2, 'the second artist heart click must write back again');
    assert.deepEqual(JSON.parse(favCalls[1].opts.body), { favorite: false },
        'the second artist heart click must remove the artist');

    // Favorites -> Artists: same heart, same write-back endpoint.
    const favRun = runStreaming();
    favRun.sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favBody = favRun.sandbox.document.getElementById('tidal-browse-body');
    favBody.querySelectorAll('#tidal-fav-types .streaming-chip').find((tab) => tab.dataset.type === 'artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favRow = favRun.createdEls.filter((el) => el.className === 'streaming-result').at(-1);
    const favHeart = favRow.querySelectorAll('.streaming-fav, .track-fav')[0];
    assert.ok(favHeart, 'favorite artist rows must render a favorite heart');
    assert.equal(favHeart.dataset.favType, 'artists', 'the favorites artist heart must carry the artists type');
    assert.equal(favHeart.dataset.favId, 'a1', 'the favorites artist heart must carry the artist id');
    favHeart.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favFavCalls = favRun.fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1/favorite');
    assert.equal(favFavCalls.length, 1, 'the favorites artist heart must dispatch exactly one write-back request');
    assert.deepEqual(JSON.parse(favFavCalls[0].opts.body), { favorite: true },
        'the favorites artist heart must add through the same endpoint');
    assert.equal(favRun.fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1').length, 0,
        'the favorites artist heart click must not open the artist detail');
}

console.log('PASS  scripts/test_streaming_ui_actions.js');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
