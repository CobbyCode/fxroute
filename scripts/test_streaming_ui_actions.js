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
            // The + selection button is bound via li.querySelector in the
            // production code; surface the same object querySelectorAll builds
            // so the bound listener and the test click target match.
            if (sel === '.streaming-add[data-streaming-add]') {
                return el.querySelectorAll('.streaming-add[data-streaming-add]')[0];
            }
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
            if (sel === '.streaming-add[data-streaming-add]') {
                if (!el._addBtn) {
                    const m = el.innerHTML.match(/data-streaming-add="([^"]+)"/);
                    const btn = makeEl();
                    btn.className = 'streaming-add';
                    btn.dataset.streamingAdd = m ? m[1] : '';
                    el._addBtn = btn;
                }
                return [el._addBtn];
            }
            return [];
        },
        appendChild() {},
        prepend() {},
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
    const tabs = ['tracks', 'albums', 'artists', 'playlists'].map((name) => {
        const t = makeEl();
        t.dataset.browse = name;
        return t;
    });
    el.querySelector = (sel) => {
        if (!memo[sel]) {
            memo[sel] = makeEl();
            memo[sel].querySelectorAll = (inner) => {
                if (inner === '.view-tab' || inner === '.tidal-subbar .view-tab[data-browse]') return tabs;
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
    const tidalBrowseBody = makeEl();
    const tidalBrowseBodyEls = {};
    tidalBrowseBody.querySelectorAll = (sel) => {
        if (sel === '#tidal-search-result-types .view-tab') return Object.values(tidalSearchTypeButtons);
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
    const tidalFavoriteAlbums = options.tidalFavoriteAlbums || [];
    // Per-account cached snapshots (keyed by the ?user= query param);
    // ``tidalSnapshot`` serves one snapshot for any account.
    const tidalSnapshots = options.tidalSnapshots || (options.tidalSnapshot ? { '*': options.tidalSnapshot } : {});
    // Playlist writes: a created playlist is appended to the playlists list
    // (mirroring the backend cache invalidation + refetch); ``failPlaylistWrite``
    // makes the write endpoints fail so the error path keeps the selection.
    let createdPlaylist = null;

    const sandbox = {
        document,
        window: {
            __visibleTab: 'radio',
            setInterval: () => 0,
            clearInterval: () => {},
            setTimeout,
            clearTimeout,
        },
        setInterval: () => 0,
        clearInterval: () => {},
        setTimeout,
        clearTimeout,
        fetch: async (url, opts) => {
            fetchCalls.push({ url: String(url), opts: opts || {} });
            const u = String(url);
            let body = {};
            let failed = false;
            if (u.startsWith('/api/streaming/tidal/library/snapshot')) {
                const userMatch = u.match(/[?&]user=([^&]*)/);
                const userId = userMatch ? decodeURIComponent(userMatch[1]) : '';
                body = tidalSnapshots[userId] || tidalSnapshots['*'] || {};
            } else if (u === '/api/streaming/tidal/playlists/create' && (opts && opts.method) === 'POST') {
                if (options.failPlaylistWrite) {
                    failed = true;
                } else {
                    const req = JSON.parse((opts && opts.body) || '{}');
                    createdPlaylist = { id: 'pl-new', name: req.name || 'New Mix', art_url: '', track_count: (req.track_ids || []).length };
                    body = createdPlaylist;
                }
            } else if (u === '/api/streaming/tidal/playlists/pl-1/tracks' && (opts && opts.method) === 'POST') {
                if (options.failPlaylistWrite) {
                    failed = true;
                } else {
                    const req = JSON.parse((opts && opts.body) || '{}');
                    body = { playlist_id: 'pl-1', added_track_ids: req.track_ids || [] };
                }
            } else if (u === '/api/streaming/tidal/playlists') {
                body = [{ id: 'pl-1', name: 'Test Playlist', art_url: '', track_count: 2 }].concat(createdPlaylist ? [createdPlaylist] : []);
            } else if (u === '/api/streaming/tidal/playlists/pl-1/tracks') {
                body = options.playlistTracks || [
                    { id: 'p1', title: 'Playlist One', artist: 'Found Artist', duration: 10 },
                    { id: 'p2', title: 'Playlist Two', artist: 'Found Artist', duration: 12 },
                ];
            } else if (u === '/api/streaming/tidal/playlists/pl-1') {
                if (options.playlistDetail) {
                    body = options.playlistDetail;
                } else {
                    body = { id: 'pl-1', name: 'Test Playlist', description: '', art_url: '', track_count: 2 };
                }
            } else if (u.includes('/api/streaming/tidal/search?q=')) {
                if (u.includes('types=artists')) body = { artists: [{ id: 'a1', name: 'Found Artist', art_url: '' }] };
                else if (u.includes('types=albums')) body = { albums: [{ id: 'al1', title: 'Found Album', artist: 'Found Artist', art_url: '' }] };
                else if (u.includes('types=playlists')) body = { playlists: [{ id: 'pl-1', name: 'Found Playlist', art_url: '' }] };
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
                if (options.delayFavorites) {
                    await new Promise((resolve) => setTimeout(resolve, options.delayFavorites));
                }
                if (options.failFavorites) {
                    failed = true;
                } else if (u.includes('type=tracks')) {
                    body = tidalFavoriteTracks;
                } else if (u.includes('type=albums')) {
                    body = tidalFavoriteAlbums;
                } else if (u.includes('type=artists')) {
                    body = [{ id: 'a1', name: 'Found Artist', art_url: '' }];
                }
            } else if (u === '/api/streaming/tidal/status') {
                body = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
                if (options.tidalUser) body.user = { id: options.tidalUser };
            } else if (u.startsWith('/api/streaming/tidal/') && u.endsWith('/favorite')) {
                body = { favorite: !!JSON.parse((opts && opts.body) || '{}').favorite };
            }
            if (failed) return { ok: false, status: 502, json: async () => ({ detail: 'TIDAL unreachable' }) };
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
        decodeURIComponent,
        navigator: undefined,
    };
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);

    const api = {
        showToast() {},
        escapeHtml: (v) => String(v),
        formatTime: () => '0:00',
        formatRateKhz: (v) => String(v),
        trackRowHtml: ({ index, title, sub, favoriteButton, selectionButton, duration }) =>
            `<span class="track-index">${index}</span><button class="track-play">▶</button>` +
            `<div class="track-info"><div class="track-title">${title}</div>` +
            (sub ? `<div class="track-sub">${sub}</div>` : '') + `</div>` +
            (selectionButton || '') + favoriteButton + (duration ? `<span class="track-duration">${duration}</span>` : ''),
        factsHtml: (lines) => `<div class="album-detail-facts">${(lines || []).map((l) => `<div>${l}</div>`).join('')}</div>`,
        aboutHtml: (label, text) => `<details class="album-detail-about"><summary>${label}</summary><p>${text}</p></details>`,
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
    const statusLine = shells.tidal.querySelector('.streaming-status');
    const content = shells.tidal.querySelector('.streaming-content');
    assert.equal(statusLine.hidden, false, 'main browse surface must show the status pill');
    assert.equal(statusLine.textContent, 'Connected', 'status pill must show the shared Connected label');

    // Open the Playlists tab and click a playlist row to enter a detail view.
    const tabs = content.querySelectorAll('.view-tab');
    tabs.find((t) => t.dataset.browse === 'playlists').click();
    // renderTidalPlaylists fetches asynchronously; let the microtasks run.
    await new Promise((resolve) => setTimeout(resolve, 0));
    const playlistRow = createdEls.find((el) => el.className === 'album-card');
    assert.ok(playlistRow, 'a playlist tile must be created for the playlists tab');
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

// --- 5. TIDAL browse: Albums default + persistent search bar --------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');

    // First authenticated render lands on Albums, not an empty Search screen.
    assert.ok(content.innerHTML.includes('streaming-browse'), 'browse surface must render');
    assert.ok(content.innerHTML.includes('id="tidal-search-input"'), 'search bar must be part of the browse surface');
    assert.ok(!content.innerHTML.includes('tidal-search-btn'), 'direct search must not render a Search button');
    assert.ok(content.innerHTML.indexOf('tidal-search-input') > content.innerHTML.indexOf('tidal-subbar') &&
        content.innerHTML.indexOf('tidal-search-input') > content.innerHTML.indexOf('data-browse='),
        'search bar must share the second header row with the navigation');
    assert.ok(content.innerHTML.includes('id="tidal-refresh-btn"'), 'browse surface must carry the refresh button');
    assert.ok(!content.innerHTML.includes('data-browse="search"'), 'Search must not be a browse tab');
    assert.ok(content.innerHTML.includes('data-browse="tracks"') && content.innerHTML.includes('data-browse="albums"') &&
        content.innerHTML.includes('data-browse="artists"') && content.innerHTML.includes('data-browse="playlists"'),
        'browse navigation must be Tracks, Albums, Artists and Playlists in one row');
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-results'),
        'first authenticated render must show Albums content');

    // Keep the last browse category while switching into and out of search.
    content.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'albums').click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Typing waits for the debounce interval before executing a search.
    const input = content.querySelector('#tidal-search-input');
    input.value = 'daft punk';
    input.dispatch('input');
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(fetchCalls.filter((c) => c.url.startsWith('/api/streaming/tidal/search?')).length, 0,
        'typing must not trigger an immediate TIDAL search');

    // Once the pause expires, the search replaces the browse body with results.
    await new Promise((resolve) => setTimeout(resolve, 350));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/search?q=daft%20punk')),
        'debounced search must hit the TIDAL search endpoint');
    const resultCount = createdEls.filter((el) => el.className === 'streaming-result').length;
    assert.equal(resultCount, 2, 'search must render all track result rows');
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('Search results for'),
        'search must switch to the dedicated result state');
    assert.ok(!sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-results'),
        'browse categories must not be visible in search results');
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

    // The + button collects a persistent playlist selection without playing;
    // a later plain row click still starts playback with the visible queue.
    fetchCalls.length = 0;
    const firstRow = createdEls.filter((el) => el.className === 'streaming-result')[0];
    firstRow.querySelectorAll('.streaming-add[data-streaming-add]')[0].click();
    assert.equal(fetchCalls.filter((c) => c.url === '/api/play').length, 0,
        'the + button must never start playback');
    assert.ok(!sandbox.document.getElementById('tidal-playlist-save-row').classList.contains('hidden'),
        'the + button must reveal the playlist save row');
    firstRow.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const selectionPlayCalls = fetchCalls.filter((c) => c.url === '/api/play');
    assert.deepEqual(JSON.parse(selectionPlayCalls.at(-1).opts.body), {
        source: 'tidal', track_id: 's1', queue_track_ids: ['s1', 's2'],
    }, 'a plain row click must still play with the full visible queue');

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
    // Album search results render as album-card tiles.
    createdEls.filter((el) => el.className === 'album-card').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    content.querySelector('#tidal-detail-back').click();
    assert.ok(body.innerHTML.includes('Search results for'),
        'back from a search detail must restore the search result state');

    // A status refresh must not wipe the results (contentKey guard).
    const searchBodyBeforeRefresh = body.innerHTML;
    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    assert.equal(body.innerHTML, searchBodyBeforeRefresh,
        'status refresh must not re-render away the search results');

    // Clearing the field itself returns to the browse category without a search request.
    input.value = '';
    input.dispatch('input');
    await new Promise((resolve) => setTimeout(resolve, 0));
    const browseTabs = content.querySelectorAll('.view-tab');
    assert.ok(body.innerHTML.includes('tidal-fav-results') &&
        browseTabs.find((t) => t.dataset.browse === 'albums').classList.contains('is-active'),
        'clearing the search must return to the last browse category (Albums)');

    // Escape resets the search back to the browse section too.
    input.value = 'daft punk';
    input.dispatch('input');
    input.dispatch('keydown', { key: 'Escape', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-results'),
        'Escape must clear the search back to the browse section');

    // Browse category switching works across all four tabs.
    const tabs = content.querySelectorAll('.view-tab');
    for (const cat of ['playlists', 'albums', 'artists', 'tracks']) {
        tabs.find((t) => t.dataset.browse === cat).click();
        if (cat === 'playlists') {
            assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-playlists-results'),
                'Playlists tab must render the playlists section');
        } else {
            assert.ok(sandbox.document.getElementById('tidal-browse-body').innerHTML.includes('tidal-fav-results'),
                cat + ' tab must render the favorites section');
        }
    }
}

// --- 6. TIDAL Tracks uses all visible tracks as a queue --------------------

{
    const favoriteTracks = [
        { id: 'f1', title: 'Favorite One', artist: 'Artist', duration: 10 },
        { id: 'f2', title: 'Favorite Two', artist: 'Artist', duration: 11 },
        { id: 'f3', title: 'Favorite Three', artist: 'Artist', duration: 12 },
        { id: 'f4', title: 'Favorite Four', artist: 'Artist', duration: 13 },
    ];
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({ tidalFavoriteTracks: favoriteTracks });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Albums is the default browse category; switch to Tracks to exercise the
    // favorite-track queue.
    shells.tidal.querySelector('.streaming-content').querySelectorAll('.view-tab')
        .find((tab) => tab.dataset.browse === 'tracks').click();
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
    // Artists render as album-card tiles (not streaming-result rows).
    createdEls.filter((el) => el.className === 'album-card').at(-1).click();
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

    // Track click from the artist detail: top tracks are streaming-result rows;
    // the single top track is the last such row.
    createdEls.filter((el) => el.className === 'streaming-result').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const artistTrackPlay = fetchCalls.filter((c) => c.url === '/api/play').at(-1);
    assert.deepEqual(JSON.parse(artistTrackPlay.opts.body), {
        source: 'tidal', track_id: 's1', queue_track_ids: ['s1'],
    }, 'artist top-track click must play through the native queue');

    // Album click from the artist detail opens the existing album detail.
    // Albums render as album-card tiles after the top-track rows.
    const albumRow = createdEls.filter((el) => el.className === 'album-card').at(-1);
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
    // Artist search results render as album-card tiles.
    createdEls.filter((el) => el.className === 'album-card').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));

    // Back from the artist returns exactly to the executed search results.
    content.querySelector('#tidal-detail-back').click();
    assert.ok(body.innerHTML.includes('Search results for'),
        'back from the artist must restore the previous artist search');
}

// --- 9b. TIDAL refresh invalidates caches and re-renders the active view ---

{
    const { sandbox, shells, fetchCalls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');
    assert.ok(body.innerHTML.includes('tidal-fav-results'), 'browse must start on Albums');

    fetchCalls.length = 0;
    content.querySelector('#tidal-refresh-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The refresh forces a fresh authoritative favorites/ids load, reloads the
    // provider status, and re-renders the active browse section.
    assert.ok(fetchCalls.filter((c) => c.url === '/api/streaming/tidal/favorites/ids').length >= 2,
        'refresh must force a fresh favorites/ids load (refresh + browse re-render)');
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/status'),
        'refresh must reload the provider status');
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/favorites?type=albums')),
        'refresh must re-render the active Albums category');
    // It must never log out, reset the session or touch playback.
    assert.ok(!fetchCalls.some((c) => c.url.includes('/auth/logout')), 'refresh must never log out');
    assert.ok(!fetchCalls.some((c) => c.url === '/api/play'), 'refresh must never touch playback');
    assert.ok(shells.tidal.querySelector('.streaming-status').hidden === false,
        'refresh must keep the Connected status visible');
}

// --- 9c. TIDAL refresh keeps an executed search view ------------------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    const content = shells.tidal.querySelector('.streaming-content');
    const body = sandbox.document.getElementById('tidal-browse-body');
    const input = content.querySelector('#tidal-search-input');

    input.value = 'daft punk';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(body.innerHTML.includes('Search results for'), 'search must be active before refresh');

    fetchCalls.length = 0;
    content.querySelector('#tidal-refresh-btn').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    // The active search view is re-executed with the stored query, never
    // dropped back to Favorites.
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/search?q=daft%20punk')),
        'refresh must re-run the active search with the stored query');
    assert.ok(body.innerHTML.includes('Search results for'),
        'refresh must keep the executed search view');
}

// --- 9. same artist detail path from the Artists category -------------------

{
    const favRun = runStreaming();
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
    favRun.sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favBody = favRun.sandbox.document.getElementById('tidal-browse-body');
    const favContent = favRun.shells.tidal.querySelector('.streaming-content');
    favContent.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    // Artists favorites render as album-card tiles.
    favRun.createdEls.filter((el) => el.className === 'album-card').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(favRun.fetchCalls.some((c) => c.url === '/api/streaming/tidal/artists/a1'),
        'clicking a favorited artist must open the same artist detail');
    assert.ok(favContent.innerHTML.includes('tidal-artist-facts'),
        'favorited artist must open the same artist detail view');
    favContent.querySelector('#tidal-detail-back').click();
    assert.ok(favBody.innerHTML.includes('tidal-fav-results') &&
        favContent.querySelectorAll('.view-tab').find((t) => t.dataset.browse === 'artists').classList.contains('is-active'),
        'back from a favorited artist must restore the Artists category');
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

    // Artist search results render as album-card tiles.
    const artistRow = createdEls.filter((el) => el.className === 'album-card').at(-1);
    const heart = artistRow.querySelectorAll('.streaming-fav, .track-fav')[0];
    assert.ok(heart, 'artist tiles must render a favorite heart');
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

    // Artists category: same heart, same write-back endpoint.
    const favRun = runStreaming();
    favRun.sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favBody = favRun.sandbox.document.getElementById('tidal-browse-body');
    const favContent = favRun.shells.tidal.querySelector('.streaming-content');
    favContent.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'artists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const favRow = favRun.createdEls.filter((el) => el.className === 'album-card').at(-1);
    const favHeart = favRow.querySelectorAll('.streaming-fav, .track-fav')[0];
    assert.ok(favHeart, 'favorite artist tiles must render a favorite heart');
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

// --- 11. cached library renders first, refresh replaces it in background ---

{
    const snapshot = {
        user_id: '42',
        ids: { tracks: [], albums: ['c1'], artists: [], playlists: [] },
        tracks: [],
        albums: [{ id: 'c1', title: 'Cached Album', artist: 'Cached Artist', art_url: '' }],
        artists: [],
        playlists: [],
    };
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({
        tidalSnapshot: snapshot,
        tidalUser: '42',
        tidalFavoriteAlbums: [{ id: 'f1', title: 'Fresh Album', artist: 'Fresh Artist', art_url: '' }],
        delayFavorites: 40,
    });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const body = sandbox.document.getElementById('tidal-browse-body');
    const albumCards = () => createdEls.filter((el) => el.className === 'album-card');

    // The last-known library renders before the (delayed) live fetch returns.
    assert.ok(albumCards().some((el) => el.innerHTML.includes('Cached Album')),
        'the cached library must render immediately on open');
    assert.ok(!albumCards().some((el) => el.innerHTML.includes('Fresh Album')),
        'the fresh payload must not replace the cache before it arrives');
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/favorites?type=albums')),
        'the background refresh must still fetch fresh favorites');
    // Cached favorite ids drive the hearts immediately.
    const cachedCard = albumCards().find((el) => el.innerHTML.includes('Cached Album'));
    assert.ok(cachedCard, 'cached albums must render as tiles');
    const cachedHeart = cachedCard.querySelectorAll('.streaming-fav, .track-fav')[0];
    assert.equal(cachedHeart.dataset.favId, 'c1', 'cached album tile must carry the album id');
    assert.ok(cachedCard.innerHTML.includes('aria-pressed="true"') && cachedCard.innerHTML.includes('is-active'),
        'cached favorite ids must render the heart active');

    // Once the live fetch resolves, fresh data replaces the cache in place.
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.ok(albumCards().some((el) => el.innerHTML.includes('Fresh Album')),
        'the background refresh must update the visible library');
    assert.equal(albumCards().filter((el) => el.innerHTML.includes('Cached Album')).length, 1,
        'the stale cached payload must be replaced (not duplicated) by the fresh one');
}

// --- 12. failed background refresh keeps the visible library ----------------

{
    const snapshot = {
        user_id: '42',
        ids: { tracks: [], albums: ['c1'], artists: [], playlists: [] },
        tracks: [],
        albums: [{ id: 'c1', title: 'Cached Album', artist: 'Cached Artist', art_url: '' }],
        artists: [],
        playlists: [],
    };
    const { sandbox, createdEls } = runStreaming({ tidalSnapshot: snapshot, tidalUser: '42', failFavorites: true });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const body = sandbox.document.getElementById('tidal-browse-body');
    assert.ok(createdEls.some((el) => el.className === 'album-card' && el.innerHTML.includes('Cached Album')),
        'the cached library must render on open');

    // The background refresh fails: the visible library stays, nothing is wiped.
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(createdEls.some((el) => el.className === 'album-card' && el.innerHTML.includes('Cached Album')),
        'a failed refresh must keep the visible library');
    assert.ok(!sandbox.document.getElementById('tidal-fav-results').innerHTML.includes('content-state--error'),
        'a failed refresh must not replace the library with an error state');
}

// --- 13. account switch never shows another account's cached library -------

{
    const snapshots = {
        '42': {
            user_id: '42',
            ids: { tracks: [], albums: ['old'], artists: [], playlists: [] },
            tracks: [],
            albums: [{ id: 'old', title: 'Old Account Album', artist: 'Old Artist', art_url: '' }],
            artists: [],
            playlists: [],
        },
        '99': null,
    };
    const { sandbox, createdEls } = runStreaming({ tidalSnapshots: snapshots, tidalUser: '99' });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '99' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const body = sandbox.document.getElementById('tidal-browse-body');
    assert.ok(!createdEls.some((el) => el.className === 'album-card' && el.innerHTML.includes('Old Account Album')),
        'account B must never see account A cached library');
    // Account B has no cache yet: the live favorites path renders its state.
    assert.ok(sandbox.document.getElementById('tidal-fav-results').innerHTML.includes('No favorites yet.'),
        'account B with no cache must fall back to the live favorites state');
}

// --- 14. + selection persists across surfaces; play/favorite never touch it -

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({
        tidalUser: '42',
        tidalFavoriteTracks: [{ id: 's2', title: 'Fav Song', artist: 'Fav Artist', album: 'Fav Album', art_url: '', duration: 10 }],
    });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const content = shells.tidal.querySelector('.streaming-content');
    const saveRow = () => sandbox.document.getElementById('tidal-playlist-save-row');
    const trackRows = () => createdEls.filter((el) => el.className === 'streaming-result' || el.className.startsWith('streaming-result '));

    // Favorites -> Tracks: select s2 with the row +.
    content.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'tracks').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.equal(trackRows().length, 1, 'favorites tracks must render one row');
    const favRow = trackRows().at(-1);
    assert.ok(favRow.innerHTML.includes('aria-pressed="false"'), 'unselected rows must show an inactive +');
    favRow.querySelectorAll('.streaming-add[data-streaming-add]')[0].click();
    assert.ok(!saveRow().classList.contains('hidden'), 'a + click must reveal the playlist save row');

    // Playback does not touch the selection.
    fetchCalls.length = 0;
    favRow.querySelector('.streaming-result-play').click();
    assert.ok(fetchCalls.some((c) => c.url === '/api/play'), 'the row play button must dispatch playback');
    assert.ok(!saveRow().classList.contains('hidden'), 'playback must never clear the playlist selection');

    // Favoriting does not touch the selection.
    fetchCalls.length = 0;
    favRow.querySelectorAll('.streaming-fav, .track-fav')[0].click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/tracks/s2/favorite'),
        'the row heart must dispatch the favorite write-back');
    assert.ok(!saveRow().classList.contains('hidden'), 'favoriting must never clear the playlist selection');

    // Search results: the same + language, selection carried over (s2 active).
    const rowBase = trackRows().length;
    const input = content.querySelector('#tidal-search-input');
    input.value = 'found';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const searchRows = trackRows().slice(rowBase);
    assert.equal(searchRows.length, 2, 'search must render two track rows');
    assert.ok(searchRows[0].innerHTML.includes('data-streaming-add="s1"') && !searchRows[0].innerHTML.includes('aria-pressed="true"'),
        'the newly-searched s1 row must render unselected');
    assert.ok(searchRows[1].innerHTML.includes('data-streaming-add="s2"') && searchRows[1].innerHTML.includes('aria-pressed="true"'),
        'the s2 selection must survive the view switch and render active');
    searchRows[0].querySelectorAll('.streaming-add[data-streaming-add]')[0].click();
    assert.ok(!saveRow().classList.contains('hidden'), 'adding s1 must keep the save row visible');

    // Browse-tab navigation (back to favorites) keeps the selection too.
    const navBase = trackRows().length;
    content.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'tracks').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(trackRows().slice(navBase).at(-1).innerHTML.includes('aria-pressed="true"'),
        'the selection must survive browse-tab navigation');
}

// --- 15. save as a new TIDAL playlist: exact selection, then cleanup --------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({
        tidalUser: '42',
        tidalFavoriteTracks: [{ id: 's2', title: 'Fav Song', artist: 'Fav Artist', album: 'Fav Album', art_url: '', duration: 10 }],
    });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const content = shells.tidal.querySelector('.streaming-content');
    const saveRow = () => sandbox.document.getElementById('tidal-playlist-save-row');
    const trackRows = () => createdEls.filter((el) => el.className === 'streaming-result' || el.className.startsWith('streaming-result '));

    // Select s2 in favorites, then s1 in search, then remove s1 again in the
    // album detail (toggle-off) — the save must contain exactly s2.
    content.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'tracks').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    trackRows()[0].querySelectorAll('.streaming-add[data-streaming-add]')[0].click();
    const input = content.querySelector('#tidal-search-input');
    input.value = 'found';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    trackRows().find((row) => row.innerHTML.includes('data-streaming-add="s1"')).querySelectorAll('.streaming-add[data-streaming-add]')[0].click();
    // Album detail: the same + renders inside the shared track row.
    sandbox.document.getElementById('tidal-browse-body').querySelector('#tidal-search-type-albums').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    createdEls.filter((el) => el.className === 'album-card').at(-1).click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const detailRow = createdEls
        .filter((el) => (el.className === 'streaming-result' || el.className.startsWith('streaming-result ')) && el.innerHTML.includes('data-streaming-add="s1"'))
        .at(-1);
    assert.ok(detailRow && detailRow.innerHTML.includes('aria-pressed="true"'),
        'the album detail row must render the existing s1 selection active');
    detailRow.querySelectorAll('.streaming-add[data-streaming-add]')[0].click();

    // Save as a new playlist: exactly the remaining selection is sent.
    fetchCalls.length = 0;
    const saveControls = content.querySelector('#tidal-playlist-save-row');
    const nameInput = saveControls.querySelector('#tidal-playlist-name');
    nameInput.value = 'Test Mix';
    saveControls.querySelector('#tidal-save-playlist').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const createCall = fetchCalls.find((c) => c.url === '/api/streaming/tidal/playlists/create');
    assert.ok(createCall, 'save-as-new must POST to the create endpoint');
    assert.deepEqual(JSON.parse(createCall.opts.body),
        { name: 'Test Mix', track_ids: ['s2'] },
        'the created playlist must contain exactly the selected tracks');
    assert.ok(saveRow().classList.contains('hidden'), 'a successful save must clear the selection and hide the save row');
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/playlists' && !c.opts.method),
        'a successful save must refetch the playlists list');

    // The new playlist appears in the Playlists section.
    content.querySelectorAll('.view-tab').find((tab) => tab.dataset.browse === 'playlists').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(createdEls.some((el) => el.className === 'album-card' && el.innerHTML.includes('Test Mix')),
        'the freshly created playlist must be visible in the Playlists section');
}

// --- 16. add the selection to an existing TIDAL playlist --------------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({ tidalUser: '42' });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const content = shells.tidal.querySelector('.streaming-content');
    const saveRow = () => sandbox.document.getElementById('tidal-playlist-save-row');
    const trackRows = () => createdEls.filter((el) => el.className === 'streaming-result' || el.className.startsWith('streaming-result '));

    const input = content.querySelector('#tidal-search-input');
    input.value = 'found';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    trackRows().find((row) => row.innerHTML.includes('data-streaming-add="s1"')).querySelectorAll('.streaming-add[data-streaming-add]')[0].click();

    fetchCalls.length = 0;
    const saveControls = content.querySelector('#tidal-playlist-save-row');
    const target = saveControls.querySelector('#tidal-playlist-target');
    target.value = 'pl-1';
    target.options = [{ textContent: 'Test Playlist' }];
    target.selectedIndex = 0;
    saveControls.querySelector('#tidal-add-to-playlist').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));
    const addCall = fetchCalls.find((c) => c.url === '/api/streaming/tidal/playlists/pl-1/tracks' && c.opts.method === 'POST');
    assert.ok(addCall, 'add-to-playlist must POST to the playlist tracks endpoint');
    assert.deepEqual(JSON.parse(addCall.opts.body), { track_ids: ['s1'] },
        'add-to-playlist must send exactly the selected tracks');
    assert.ok(saveRow().classList.contains('hidden'), 'a successful add must clear the selection and hide the save row');
}

// --- 17. failed write keeps the selection and the entered name -------------

{
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({ tidalUser: '42', failPlaylistWrite: true });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const content = shells.tidal.querySelector('.streaming-content');
    const saveRow = () => sandbox.document.getElementById('tidal-playlist-save-row');
    const trackRows = () => createdEls.filter((el) => el.className === 'streaming-result' || el.className.startsWith('streaming-result '));

    const input = content.querySelector('#tidal-search-input');
    input.value = 'found';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    trackRows()[0].querySelectorAll('.streaming-add[data-streaming-add]')[0].click();

    fetchCalls.length = 0;
    const saveControls = content.querySelector('#tidal-playlist-save-row');
    const nameInput = saveControls.querySelector('#tidal-playlist-name');
    nameInput.value = 'Keep Mix';
    saveControls.querySelector('#tidal-save-playlist').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/playlists/create'),
        'the failed save must still attempt the create write');
    assert.ok(!saveRow().classList.contains('hidden'), 'a failed save must keep the selection and the save row');
    assert.equal(nameInput.value, 'Keep Mix', 'a failed save must keep the entered playlist name');
    const errorEl = sandbox.document.getElementById('tidal-playlist-save-error');
    assert.ok(errorEl && errorEl.hidden === false && errorEl.textContent,
        'a failed save must surface an understandable error state');
}

// --- 18. re-opening the TIDAL tab re-syncs the active browse section -------
// The last-known library stays visible while a fresh TIDAL fetch replaces it
// in the background; searches and detail views are left untouched.

{
    const snapshot = {
        user_id: '42',
        ids: { tracks: [], albums: ['c1'], artists: [], playlists: [] },
        tracks: [],
        albums: [{ id: 'c1', title: 'Cached Album', artist: 'Cached Artist', art_url: '' }],
        artists: [],
        playlists: [],
    };
    const { sandbox, shells, fetchCalls, createdEls } = runStreaming({
        tidalSnapshot: snapshot,
        tidalUser: '42',
        tidalFavoriteAlbums: [{ id: 'f1', title: 'Fresh Album', artist: 'Fresh Artist', art_url: '' }],
        delayFavorites: 40,
    });
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0, user: { id: '42' } };

    sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const albumCards = () => createdEls.filter((el) => el.className === 'album-card');
    assert.ok(albumCards().some((el) => el.innerHTML.includes('Cached Album')),
        'the cached library must render on first open');
    // Let the initial background refresh finish and settle on fresh data.
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.ok(albumCards().some((el) => el.innerHTML.includes('Fresh Album')),
        'the first background refresh must adopt fresh data');

    // Re-entering the tab (as if switching away and back) re-syncs the active
    // Albums section: the visible library stays, a fresh fetch is triggered.
    fetchCalls.length = 0;
    sandbox.window.FXRouteStreaming.onTabVisible('tidal');
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(albumCards().some((el) => el.innerHTML.includes('Fresh Album')),
        're-entering the tab must keep the visible library (no blanking)');
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/favorites?type=albums')),
        're-entering the tab must re-sync the active browse section in the background');
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/status'),
        're-entering the tab must also restart the status poll');

    // A search is live and must not be clobbered by the re-sync.
    const { sandbox: s2, shells: shells2, fetchCalls: fc2, createdEls: ce2 } = runStreaming({
        tidalSnapshot: snapshot,
        tidalUser: '42',
        delayFavorites: 40,
    });
    s2.window.FXRouteStreaming.renderProvider('tidal', tidalData);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const input = shells2.tidal.querySelector('.streaming-content').querySelector('#tidal-search-input');
    input.value = 'found';
    input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(s2.document.getElementById('tidal-browse-body').innerHTML.includes('Search results for'),
        'search must be active before re-entering the tab');
    fc2.length = 0;
    s2.window.FXRouteStreaming.onTabVisible('tidal');
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.ok(s2.document.getElementById('tidal-browse-body').innerHTML.includes('Search results for'),
        're-entering the tab must never clobber live search results');
    assert.ok(!fc2.some((c) => c.url.startsWith('/api/streaming/tidal/favorites?type=')),
        're-entering the tab must skip the section re-sync while a search is showing');
}

// --- 19. playlist detail: description, single-artist about, featuring line ---

{
    const run = (detail, extra) => runStreaming(Object.assign({ playlistDetail: detail }, extra || {}));
    const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };

    // Description wins over everything and the Play playlist button is gone.
    {
        const { sandbox, shells, fetchCalls, createdEls } = run({
            id: 'pl-1', name: 'Test Playlist', description: 'A curated mix', art_url: '', track_count: 2,
            artists: [{ id: 'a1', name: 'Found Artist' }],
            enrichment: { available: true, artist: { about: 'A long artist bio' } },
        });
        sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
        const content = shells.tidal.querySelector('.streaming-content');
        const body = sandbox.document.getElementById('tidal-browse-body');
        const input = content.querySelector('#tidal-search-input');
        input.value = 'mix';
        input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        body.querySelector('#tidal-search-type-playlists').click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        createdEls.filter((el) => el.className === 'album-card').at(-1).click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/playlists/pl-1'),
            'opening a playlist must fetch the playlist detail');
        const info = content.querySelector('#tidal-playlist-info');
        assert.ok(info && info.innerHTML.includes('A curated mix'),
            'the playlist description must render as the header info');
        assert.ok(!content.innerHTML.includes('Play playlist'),
            'the playlist detail must not offer a Play playlist button');
        assert.ok(!content.innerHTML.includes('A long artist bio'),
            'the description must win over the single-artist about');
    }

    // No description, single artist: the MusicBrainz about text is used.
    {
        const { sandbox, shells, createdEls } = run({
            id: 'pl-1', name: 'Test Playlist', description: '', art_url: '', track_count: 2,
            artists: [{ id: 'a1', name: 'Found Artist' }],
            enrichment: { available: true, artist: { about: 'A long artist bio' } },
        });
        sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
        const content = shells.tidal.querySelector('.streaming-content');
        const body = sandbox.document.getElementById('tidal-browse-body');
        const input = content.querySelector('#tidal-search-input');
        input.value = 'mix';
        input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        body.querySelector('#tidal-search-type-playlists').click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        createdEls.filter((el) => el.className === 'album-card').at(-1).click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        const info = content.querySelector('#tidal-playlist-info');
        assert.ok(info && info.innerHTML.includes('A long artist bio'),
            'a single-artist playlist without description must use the enriched artist about');
    }

    // Multi-artist tracks without description/enrichment: a compact featuring
    // line built from the distinct track artists, never a fake biography.
    {
        const { sandbox, shells, createdEls } = run({
            id: 'pl-1', name: 'Test Playlist', description: '', art_url: '', track_count: 3,
            artists: [{ id: 'a1', name: 'Artist A' }, { id: 'a2', name: 'Artist B' }],
        }, {
            playlistTracks: [
                { id: 'p1', title: 'One', artist: 'Artist A', duration: 10 },
                { id: 'p2', title: 'Two', artist: 'Artist A', duration: 12 },
                { id: 'p3', title: 'Three', artist: 'Artist B', duration: 13 },
            ],
        });
        sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
        const content = shells.tidal.querySelector('.streaming-content');
        const body = sandbox.document.getElementById('tidal-browse-body');
        const input = content.querySelector('#tidal-search-input');
        input.value = 'mix';
        input.dispatch('keydown', { key: 'Enter', preventDefault() {} });
        await new Promise((resolve) => setTimeout(resolve, 0));
        body.querySelector('#tidal-search-type-playlists').click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        createdEls.filter((el) => el.className === 'album-card').at(-1).click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        const info = content.querySelector('#tidal-playlist-info');
        assert.ok(info && info.innerHTML.includes('Featuring'),
            'a multi-artist playlist without description must render a featuring line');
    }
}

console.log('PASS  scripts/test_streaming_ui_actions.js');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
