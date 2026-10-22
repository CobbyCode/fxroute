#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Functional checks for the shared streaming UI.
//
// Unlike test_streaming_ui_structure.js (string/static checks), this test
// actually executes static/streaming.js against a minimal DOM shim and
// asserts that:
//
//   1. each provider tab's transport buttons are bound to a real action
//      (Spotify -> app.js playerctl adapter, Qobuz -> /api/streaming/qobuz/...),
//   2. TIDAL has no tab, and everything the Library uses from it works through
//      the FXRouteStreaming.tidal adapter: detail views with their playback
//      queues, the favorites cache, the + selection and the playlist writes.

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
        focus() {},
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
    el.querySelector = (sel) => {
        if (!memo[sel]) {
            memo[sel] = makeEl();
            memo[sel].querySelectorAll = () => [];
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
    const providers = ['spotify', 'qobuz'];
    const shells = {};
    const tabButtons = {};
    const tabPanels = {};
    const byId = {};
    const createdEls = [];
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
    const { document, shells, tabButtons, tabPanels, createdEls } = buildDom();
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
            // Detail opens reset the window scroll; the shim has no viewport.
            scrollTo() {},
        },
        setInterval: () => 0,
        clearInterval: () => {},
        setTimeout,
        clearTimeout,
        fetch: async (url, opts) => {
            fetchCalls.push({ url: String(url), opts: opts || {} });
            const u = String(url);
            if (options.statusFetch && /^\/api\/streaming\/[^/]+\/status$/.test(u)) return options.statusFetch(u);
            let body = {};
            let failed = false;
            if (u === '/api/streaming/providers/discovery') {
                body = { providers: options.discoveryProviders || [{ id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: true, enabled: true }] };
            } else if (u.startsWith('/api/streaming/tidal/library/snapshot')) {
                const userMatch = u.match(/[?&]user=([^&]*)/);
                const userId = userMatch ? decodeURIComponent(userMatch[1]) : '';
                body = tidalSnapshots[userId] || tidalSnapshots['*'] || {};
            } else if (u === '/api/streaming/tidal/playlists/create' && (opts && opts.method) === 'POST') {
                if (options.failPlaylistWrite) {
                    failed = true;
                } else {
                    const req = JSON.parse((opts && opts.body) || '{}');
                    createdPlaylist = { id: 'pl-new', name: req.name || 'New Mix', art_url: '', track_count: (req.track_ids || []).length };
                    if (options.playlistSeedWarning) {
                        Object.assign(createdPlaylist, { warning: options.playlistSeedWarning, seed_status: 'unknown', added_track_ids: null });
                    }
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
                if (options.idsFetch) return options.idsFetch(u);
                body = { tracks: [], albums: [], artists: [], playlists: [] };
            } else if (u.startsWith('/api/streaming/tidal/favorites?type=')) {
                if (options.favoritesFetch) {
                    const held = options.favoritesFetch(u);
                    if (held) return held;
                }
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
                if (options.favoriteResponse) Object.assign(body, options.favoriteResponse(u, body));
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
    if (options.defaultEscape) delete api.escapeHtml;
    if (!options.deferInit) sandbox.window.FXRouteStreaming.init(api);
    return { sandbox, shells, tabButtons, tabPanels, fetchCalls, spotifyCommandCalls, spotifySeekCalls, createdEls, api };
}

// A Library host the adapter renders TIDAL details into (the production host
// is the Library tab; the shim container records what is written to it).
function attachHost(sandbox, extra) {
    const container = makeEl();
    const calls = { open: 0, close: 0, render: 0, search: [], localArtists: [] };
    sandbox.window.FXRouteStreaming.tidal.attachLibraryHost(Object.assign({
        container,
        onOpen() { calls.open += 1; },
        onClose() { calls.close += 1; },
        onRender() { calls.render += 1; },
        search(name, type) { calls.search.push([name, type]); },
        openLocalArtist(name) { calls.localArtists.push(name); return false; },
    }, extra || {}));
    return { container, calls };
}

const tick = (ms) => new Promise((resolve) => setTimeout(resolve, ms || 0));

const baseCaps = {
    transport: true, seek: true, shuffle: true, loop: true, progress: true, cover: true,
    audio_format: true, bit_depth: true, sample_rate: true,
};

async function main() {
// Overlapping provider requests must not roll back fresher status or external frames.
{
    const pending = [];
    const h = runStreaming({ statusFetch: (url) => new Promise((resolve) => pending.push({ url, resolve })) });
    await new Promise((resolve) => setTimeout(resolve, 0));
    // Startup status reads (every installed provider) stay pending; only later requests matter.
    pending.length = 0;
    const ui = h.sandbox.window.FXRouteStreaming;
    const data = (title) => ({ installed: true, available: true, authenticated: true, capabilities: baseCaps,
        status: 'Paused', title, artist: '', album: '', artUrl: '', position: 0, duration: 0 });
    const answer = async (request, title) => {
        request.resolve({ ok: true, json: async () => data(title) });
        await new Promise((resolve) => setTimeout(resolve, 0));
    };
    h.sandbox.window.__visibleTab = 'qobuz';
    ui.refreshActiveTab();
    const old = pending.at(-1);
    ui.refreshActiveTab();
    await answer(pending.at(-1), 'new');
    await answer(old, 'old');
    assert.equal(h.shells.qobuz.querySelector('.streaming-title').textContent, 'new');
    ui.refreshActiveTab();
    const beforeFrame = pending.at(-1);
    ui.renderProvider('qobuz', data('external'));
    await answer(beforeFrame, 'stale poll');
    assert.equal(h.shells.qobuz.querySelector('.streaming-title').textContent, 'external');
    ui.refreshActiveTab();
    const qobuz = pending.at(-1);
    // The Library refreshes TIDAL status independently of the Qobuz tab.
    h.sandbox.window.__visibleTab = 'library';
    ui.refreshActiveTab();
    const tidalRequest = pending.at(-1);
    assert.ok(tidalRequest.url.endsWith('/tidal/status'), 'the Library must refresh the TIDAL status');
    await answer(tidalRequest, 'tidal new');
    await answer(qobuz, 'qobuz independent');
    assert.equal(h.shells.qobuz.querySelector('.streaming-title').textContent, 'qobuz independent');
    h.sandbox.window.__visibleTab = 'qobuz';
    ui.refreshActiveTab();
    let previous = pending.at(-1);
    for (let index = 0; index < 4; index += 1) {
        ui.refreshActiveTab();
        await answer(previous, `slow-${index}`);
        assert.equal(h.shells.qobuz.querySelector('.streaming-title').textContent, `slow-${index}`);
        previous = pending.at(-1);
    }
    await answer(previous, 'slow-final');
}

// A failed DOM build leaves initialization retryable, without duplicate transport listeners.
{
    const h = runStreaming({ deferInit: true });
    const query = h.sandbox.document.querySelectorAll;
    h.sandbox.document.querySelectorAll = () => { throw new Error('DOM not ready'); };
    assert.throws(() => h.sandbox.window.FXRouteStreaming.init(h.api), /DOM not ready/);
    h.sandbox.document.querySelectorAll = query;
    h.sandbox.window.FXRouteStreaming.init(h.api);
    h.sandbox.window.FXRouteStreaming.init(h.api);
    h.shells.spotify.querySelector('[data-action="toggle"]').click();
    assert.deepEqual(h.spotifyCommandCalls, [['toggle']]);
}

// Standalone rendering must escape catalog names even without app.js injection.
{
    const h = runStreaming({ defaultEscape: true });
    await tick();
    const row = h.sandbox.window.FXRouteStreaming.tidal.buildTrackRow(
        { id: 'escape', title: '<img src=x onerror=alert(1)>', artist: 'A & B', album: '', art_url: '', duration: 1 },
        { tag: 'div', queueIds: ['escape'] },
    );
    assert.ok(!row.innerHTML.includes('<img src=x'), row.innerHTML);
    assert.ok(row.innerHTML.includes('&lt;img src=x'));
}

// --- 1. transport buttons are bound to real actions -------------------------

{
    const { sandbox, shells, fetchCalls, spotifyCommandCalls } = runStreaming();
    const spotifyData = { installed: true, available: true, authenticated: null, capabilities: baseCaps, status: 'Paused', title: 't', artist: 'a', album: 'b', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 100 };

    assert.equal(shells.tidal, undefined, 'TIDAL has no provider tab and therefore no shell');

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
}

// --- 2. TIDAL status feeds the Library without any tab ----------------------

{
    const pending = [];
    const h = runStreaming({ statusFetch: (url) => new Promise((resolve) => pending.push({ url, resolve })) });
    await tick();
    pending.length = 0;
    const ui = h.sandbox.window.FXRouteStreaming;
    // The former TIDAL tab id is no longer a polling surface.
    h.sandbox.window.__visibleTab = 'tidal';
    ui.refreshActiveTab();
    ui.onTabVisible('tidal');
    assert.equal(pending.length, 0, 'a tidal tab id must not trigger any status request');
    // Entering the Library reads the status once (no poll).
    ui.onTabVisible('library');
    assert.equal(pending.filter((request) => request.url.endsWith('/tidal/status')).length, 1,
        'entering the Library must read the TIDAL status once');
    const events = [];
    h.sandbox.window.dispatchEvent = (event) => events.push(event.type);
    h.sandbox.CustomEvent = function CustomEvent(type, init) { this.type = type; this.detail = init && init.detail; };
    pending[0].resolve({ ok: true, json: async () => ({ installed: true, available: true, authenticated: true, capabilities: baseCaps, user: { id: '7' } }) });
    await tick();
    assert.equal(ui.tidal.connection().authenticated, true, 'the adapter must report the connection from the status read');
    assert.equal(ui.tidal.connection().userId, '7');
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
}

// --- 4. TIDAL playlist detail in the Library keeps its complete queue -------

{
    const { sandbox, fetchCalls, createdEls } = runStreaming();
    await tick();
    const host = attachHost(sandbox);
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    adapter.openDetail('playlist', 'pl-1', 'Test Playlist', '');
    await tick();
    assert.equal(host.calls.open, 1, 'opening a detail must tell the Library host');
    assert.ok(host.calls.render >= 1, 'the Library host must be told about each render');
    assert.ok(host.container.innerHTML.includes('tidal-detail'), 'playlist detail view must render into the host container');
    assert.equal(adapter.isLibraryDetailOpen(), true);
    const playlistTrackRows = createdEls.filter((el) => el.className === 'streaming-result');
    playlistTrackRows.at(-1).querySelector('.track-play').click();
    await tick();
    const playlistPlayCalls = fetchCalls.filter((c) => c.url === '/api/play');
    assert.equal(playlistPlayCalls.length, 1, 'a detail play button must dispatch exactly one playback request');
    assert.deepEqual(JSON.parse(playlistPlayCalls[0].opts.body), {
        source: 'tidal', track_id: 'p2', queue_track_ids: ['p1', 'p2'], shuffle: false,
    }, 'playlist detail playback must keep its complete playlist queue');

    // Back leaves the detail and hands the Library its own view back.
    host.container.querySelector('#tidal-detail-back').click();
    assert.equal(host.calls.close, 1, 'back must tell the Library host the detail closed');
    assert.equal(adapter.isLibraryDetailOpen(), false);
}

// --- 4b. TIDAL plays carry the current shuffle intent like library plays ---

{
    const { sandbox, fetchCalls, createdEls } = runStreaming();
    await tick();
    attachHost(sandbox);
    sandbox.window.FXRouteStreaming.renderProvider('tidal', { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: true, loop: 'none', position: 0, duration: 0 });
    sandbox.window.FXRouteStreaming.tidal.openDetail('playlist', 'pl-1', 'Test Playlist', '');
    await tick();
    createdEls.filter((el) => el.className === 'streaming-result').at(-1).querySelector('.track-play').click();
    await tick();
    const playlistPlayCalls = fetchCalls.filter((c) => c.url === '/api/play');
    assert.equal(playlistPlayCalls.length, 1, 'a detail play button must dispatch exactly one playback request');
    assert.deepEqual(JSON.parse(playlistPlayCalls[0].opts.body), {
        source: 'tidal', track_id: 'p2', queue_track_ids: ['p1', 'p2'], shuffle: true,
    }, 'an enabled TIDAL shuffle must ride along on the play request like library plays');
}

// --- 4c. a host list can own playback of its TIDAL rows ----------------------

{
    const { sandbox, fetchCalls } = runStreaming();
    await tick();
    const played = [];
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    const row = adapter.buildTrackRow(
        { id: '77', title: 'Saved', artist: 'Band', album: 'LP', art_url: '', duration: 5 },
        { tag: 'div', queueIds: ['77'], onPlay: (id) => played.push(id) },
    );
    row.querySelector('.track-play').click();
    row.click();
    assert.deepEqual(played, ['77', '77'], 'both the play button and the row must call the host handler');
    assert.ok(!fetchCalls.some((c) => c.url === '/api/play'), 'the TIDAL-only queue must not start when the host owns playback');
    // The shown track is remembered for saved playlists.
    assert.deepEqual(JSON.parse(JSON.stringify(adapter.trackMeta(['77', 'unknown']))), {
        '77': { title: 'Saved', artist: 'Band', album: 'LP', duration: 5, art_url: '' },
    }, 'trackMeta must return the display data of tracks that were shown');
}

// --- 7. TIDAL artist detail: tracks, albums and the nested back chain -------

{
    const { sandbox, fetchCalls, createdEls } = runStreaming();
    await tick();
    const host = attachHost(sandbox);
    sandbox.window.FXRouteStreaming.tidal.openDetail('artist', 'a1', 'Found Artist', '');
    await tick();

    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/artists/a1'),
        'opening an artist must fetch the artist detail with the artist id');
    assert.ok(host.container.innerHTML.includes('tidal-detail') && host.container.innerHTML.includes('tidal-artist-facts'),
        'artist open must render the artist detail view');
    assert.ok(host.container.innerHTML.includes('Found Artist'), 'artist detail must show the artist name');
    const headings = createdEls.filter((el) => el.className === 'streaming-results-heading');
    assert.ok(headings.some((el) => el.textContent === 'Top Tracks'), 'artist detail must list top tracks');
    assert.ok(headings.some((el) => el.textContent === 'Albums'), 'artist detail must list albums');

    createdEls.filter((el) => el.className === 'streaming-result').at(-1).click();
    await tick();
    const artistTrackPlay = fetchCalls.filter((c) => c.url === '/api/play').at(-1);
    assert.deepEqual(JSON.parse(artistTrackPlay.opts.body), {
        source: 'tidal', track_id: 's1', queue_track_ids: ['s1'], shuffle: false,
    }, 'artist top-track click must play through the native queue');

    createdEls.filter((el) => el.className === 'album-card').at(-1).click();
    await tick();
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/albums/al1'),
        'album click from the artist detail must open the album detail');

    // Back from the album restores the artist detail with the artist's own id.
    host.container.querySelector('#tidal-detail-back').click();
    await tick();
    assert.equal(fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1').length, 2,
        'back from the album must restore the artist detail');
    assert.ok(!fetchCalls.some((c) => c.url === '/api/streaming/tidal/artists/al1'),
        'the restored artist detail must keep the artist id, not the album id');
}

// --- 8. artist search falls through to the Library search --------------------

{
    const { sandbox } = runStreaming();
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    adapter.searchArtist('Nobody');   // no host attached: nothing to search in, nothing breaks
    const host = attachHost(sandbox);
    adapter.searchArtist('Found Artist');
    assert.deepEqual(JSON.parse(JSON.stringify(host.calls.search)), [['Found Artist', 'artists']],
        'a name search from an artist page must run in the Library search');
}

// --- 10. artist heart toggles through the write-back endpoint ----------------

{
    const { sandbox, fetchCalls } = runStreaming();
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    await adapter.toggleFavorite('artists', 'a1');
    let favCalls = fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1/favorite');
    assert.equal(favCalls.length, 1, 'the artist heart must dispatch exactly one write-back request');
    assert.deepEqual(JSON.parse(favCalls[0].opts.body), { favorite: true }, 'the first click must add the artist');
    assert.equal(adapter.isFavorite('artists', 'a1'), true);
    assert.equal(fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1').length, 0,
        'the heart must not open the artist detail');
    await adapter.toggleFavorite('artists', 'a1');
    favCalls = fetchCalls.filter((c) => c.url === '/api/streaming/tidal/artists/a1/favorite');
    assert.equal(favCalls.length, 2, 'the second click must write back again');
    assert.deepEqual(JSON.parse(favCalls[1].opts.body), { favorite: false }, 'the second click must remove the artist');
    assert.equal(adapter.isFavorite('artists', 'a1'), false);
}

// --- 11. cached library is there first, refresh replaces it in background ---

{
    const snapshot = {
        user_id: '42',
        ids: { tracks: [], albums: ['c1'], artists: [], playlists: [] },
        tracks: [],
        albums: [{ id: 'c1', title: 'Cached Album', artist: 'Cached Artist', art_url: '' }],
        artists: [],
        playlists: [],
    };
    const { sandbox, fetchCalls } = runStreaming({
        tidalSnapshot: snapshot,
        tidalUser: '42',
        tidalFavoriteAlbums: [{ id: 'f1', title: 'Fresh Album', artist: 'Fresh Artist', art_url: '' }],
        delayFavorites: 40,
    });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    assert.equal(adapter.connection().authenticated, true);
    const loading = adapter.ensureLibrary();
    await tick(5);

    // The last-known library is there before the (delayed) live fetch returns.
    const titles = () => adapter.items('albums').map((item) => item.title);
    assert.deepEqual([...titles()], ['Cached Album'], 'the cached library must be available immediately');
    assert.ok(fetchCalls.some((c) => c.url.startsWith('/api/streaming/tidal/favorites?type=albums')),
        'the background refresh must still fetch fresh favorites');
    assert.equal(adapter.isFavorite('albums', 'c1'), true, 'cached favorite ids must drive the hearts immediately');

    // Once the live fetch resolves, fresh data replaces the cache in place.
    await loading;
    assert.deepEqual([...titles()], ['Fresh Album'], 'the stale cached payload must be replaced (not duplicated) by the fresh one');
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
    const { sandbox } = runStreaming({ tidalSnapshot: snapshot, tidalUser: '42', failFavorites: true });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    await adapter.ensureLibrary();
    assert.deepEqual([...adapter.items('albums').map((item) => item.title)], ['Cached Album'],
        'a failed refresh must keep the last-known library');
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
    const { sandbox } = runStreaming({ tidalSnapshots: snapshots, tidalUser: '99' });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    await adapter.ensureLibrary();
    assert.ok(!adapter.items('albums').some((item) => item.title === 'Old Account Album'),
        'account B must never see account A cached library');
    assert.deepEqual([...adapter.items('albums')], [], 'account B without a cache starts from its own live favorites');
}

// --- 13b. a favorites read never overwrites hearts toggled while it ran ----

{
    // Held responses: the test decides when (and in which order) they land.
    const heldReads = [];
    const hold = (body) => new Promise((resolve) => {
        heldReads.push({ release: () => resolve({ ok: true, json: async () => body }) });
    });
    const album = (id) => ({ id, title: 'Album ' + id, artist: 'Artist', art_url: '' });

    // An un-heart during the Library walk survives the older list.
    {
        const snapshot = { user_id: '42', ids: { tracks: [], albums: ['x1'], artists: [], playlists: [] }, tracks: [], albums: [album('x1')], artists: [], playlists: [] };
        const albumReads = [];
        const { sandbox } = runStreaming({
            tidalSnapshot: snapshot,
            tidalUser: '42',
            favoritesFetch: (u) => {
                if (!u.includes('type=albums')) return null;
                return hold([album('x1')]).then((r) => { albumReads.push(u); return r; });
            },
        });
        await tick();
        const adapter = sandbox.window.FXRouteStreaming.tidal;
        const loading = adapter.ensureLibrary();
        await tick(5);
        assert.equal(adapter.isFavorite('albums', 'x1'), true, 'the cached heart is there while the walk is out');
        await adapter.toggleFavorite('albums', 'x1');
        assert.equal(adapter.isFavorite('albums', 'x1'), false, 'the un-heart applies at once');
        heldReads.splice(0).forEach((read) => read.release());
        await loading;
        assert.equal(adapter.isFavorite('albums', 'x1'), false,
            'a list read before the un-heart must not bring the favorite back');
    }

    // A heart added during a walk stays, and an older walk cannot drop it from the list.
    {
        const snapshot = { user_id: '42', ids: { tracks: [], albums: [], artists: [], playlists: [] }, tracks: [], albums: [], artists: [], playlists: [] };
        const albumWalks = [];
        const { sandbox } = runStreaming({
            tidalSnapshot: snapshot,
            tidalUser: '42',
            favoritesFetch: (u) => {
                if (!u.includes('type=albums')) return null;
                return new Promise((resolve) => {
                    albumWalks.push((items) => resolve({ ok: true, json: async () => items }));
                });
            },
        });
        await tick();
        const adapter = sandbox.window.FXRouteStreaming.tidal;
        const loading = adapter.ensureLibrary();
        await tick(5);
        assert.equal(albumWalks.length, 1, 'the Library load starts one albums walk');
        await adapter.toggleFavorite('albums', 'y1');
        assert.equal(adapter.isFavorite('albums', 'y1'), true);
        await tick(5);
        assert.equal(albumWalks.length, 2, 'a heart added during the load starts a fresh albums walk');

        // The fresh walk (has y1) lands first, the older one (without y1) after.
        albumWalks[1]([album('y1')]);
        await tick(5);
        albumWalks[0]([]);
        await loading;
        await tick(5);
        assert.deepEqual([...adapter.items('albums').map((item) => item.id)], ['y1'],
            'an older walk must not replace the newer list');
        assert.equal(adapter.isFavorite('albums', 'y1'), true, 'the new heart survives the older walk');
    }

    // The favorite-ids read follows the same rule.
    {
        let release;
        const { sandbox } = runStreaming({
            tidalUser: '42',
            idsFetch: () => new Promise((resolve) => {
                release = () => resolve({ ok: true, json: async () => ({ tracks: [], albums: ['x1'], artists: [], playlists: [] }) });
            }),
        });
        await tick();
        const adapter = sandbox.window.FXRouteStreaming.tidal;
        const streaming = sandbox.window.FXRouteStreaming;
        await adapter.toggleFavorite('albums', 'x1');          // hearted before the read starts
        const reading = streaming.ensureTidalFavoritesLoaded(true);
        await tick(5);
        await adapter.toggleFavorite('albums', 'x1');          // un-hearted while it runs
        await adapter.toggleFavorite('albums', 'y1');          // hearted while it runs
        assert.equal(adapter.isFavorite('albums', 'x1'), false);
        release();
        assert.equal(await reading, true);
        assert.equal(adapter.isFavorite('albums', 'x1'), false, 'the ids read must not restore an un-hearted item');
        assert.equal(adapter.isFavorite('albums', 'y1'), true, 'the ids read must not drop a newly hearted item');
    }
}

// --- 13b2. quality variants of one album are a single favorite ---------------

{
    // The backend lists one entry per album and names the variants folded into it.
    const folded = { id: 'B', title: 'Album', artist: 'Artist', art_url: '', variant_ids: ['A', 'B'] };
    const other = { id: 'C', title: 'Other', artist: 'Artist', art_url: '' };
    const snapshot = { user_id: '42', ids: { tracks: [], albums: ['A', 'B', 'C'], artists: [], playlists: [] }, tracks: [], albums: [folded, other], artists: [], playlists: [] };

    // A list read keeps the heart of every folded variant.
    {
        const { sandbox } = runStreaming({
            tidalSnapshot: snapshot,
            tidalUser: '42',
            tidalFavoriteAlbums: [folded, other],
        });
        await tick();
        const adapter = sandbox.window.FXRouteStreaming.tidal;
        await adapter.ensureLibrary();
        await tick(5);
        assert.deepEqual([...adapter.items('albums').map((item) => item.id)], ['B', 'C'], 'the list shows one entry per album');
        ['A', 'B', 'C'].forEach((id) => assert.equal(adapter.isFavorite('albums', id), true,
            'a list read must keep the heart of folded variant ' + id));
    }

    // Removing the album removes the variants favorited with it, and a reload
    // that no longer lists any of them keeps them out.
    {
        let serverAlbums = [folded, other];
        const { sandbox, fetchCalls } = runStreaming({
            tidalSnapshot: snapshot,
            tidalUser: '42',
            favoritesFetch: (u) => (u.includes('type=albums')
                ? Promise.resolve({ ok: true, json: async () => serverAlbums }) : null),
            favoriteResponse: (u) => (u.includes('/albums/B/') ? { variant_ids: ['B', 'A'] } : {}),
        });
        await tick();
        const adapter = sandbox.window.FXRouteStreaming.tidal;
        await adapter.ensureLibrary();
        assert.equal(adapter.isFavorite('albums', 'A'), true);
        await adapter.toggleFavorite('albums', 'B');
        const write = fetchCalls.filter((c) => c.url.endsWith('/albums/B/favorite'));
        assert.equal(write.length, 1, 'one request removes the album');
        assert.deepEqual(JSON.parse(write[0].opts.body), { favorite: false });
        assert.equal(adapter.isFavorite('albums', 'B'), false, 'the removed variant is no favorite');
        assert.equal(adapter.isFavorite('albums', 'A'), false, 'its sibling goes with it');
        assert.equal(adapter.isFavorite('albums', 'C'), true, 'other albums are untouched');
        serverAlbums = [other];
        await adapter.ensureLibrary(true);
        await tick(5);
        assert.deepEqual(['A', 'B', 'C'].map((id) => adapter.isFavorite('albums', id)), [false, false, true],
            'a reload after the removal keeps the album out');
    }

    // Adding sets exactly the toggled id, and a plain removal stays single.
    {
        const { sandbox } = runStreaming({ tidalUser: '42' });
        await tick();
        const adapter = sandbox.window.FXRouteStreaming.tidal;
        await adapter.toggleFavorite('albums', 'B');
        assert.deepEqual(['A', 'B'].map((id) => adapter.isFavorite('albums', id)), [false, true]);
        await adapter.toggleFavorite('albums', 'B');
        assert.equal(adapter.isFavorite('albums', 'B'), false);
    }
}

// --- 13c. tracks in the + selection keep their display data -----------------

{
    const { sandbox } = runStreaming({ tidalUser: '42' });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    const song = (id) => ({ id, title: 'Song ' + id, artist: 'Artist', album: 'Album', art_url: '', duration: 10 });
    adapter.buildTrackRow(song('keep'), { tag: 'div', queueIds: ['keep'] });
    adapter.buildTrackRow(song('plain'), { tag: 'div', queueIds: ['plain'] });
    adapter.selectTracks(['keep']);
    // More distinct rows than the cache holds (a large favorites list).
    for (let i = 0; i < 2100; i += 1) adapter.buildTrackRow(song('fill' + i), { tag: 'div', queueIds: ['fill' + i] });
    const meta = JSON.parse(JSON.stringify(adapter.trackMeta(['keep', 'plain', 'fill0', 'fill2099'])));
    assert.equal(meta.keep && meta.keep.title, 'Song keep', 'a selected track keeps its display data');
    assert.equal(meta.plain, undefined, 'an unselected old track is evicted (the cache stays bounded)');
    assert.equal(meta.fill0, undefined, 'the oldest unselected rows are the ones that go');
    assert.equal(meta.fill2099 && meta.fill2099.title, 'Song fill2099', 'recent rows stay');

    // Once deselected, the track is an ordinary cache entry again.
    adapter.deselectTracks(['keep']);
    adapter.buildTrackRow(song('more'), { tag: 'div', queueIds: ['more'] });
    assert.equal(JSON.parse(JSON.stringify(adapter.trackMeta(['keep']))).keep, undefined,
        'a deselected track becomes evictable again');
}

// --- 14. + selection persists across surfaces; play/favorite never touch it -

{
    const { sandbox, fetchCalls } = runStreaming({ tidalUser: '42' });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    const song = (id) => ({ id, title: 'Song ' + id, artist: 'Artist', album: 'Album', art_url: '', duration: 10 });
    const buildRow = (id) => adapter.buildTrackRow(song(id), { tag: 'div', queueIds: ['s1', 's2'] });
    const add = (row) => row.querySelectorAll('.streaming-add[data-streaming-add]')[0].click();

    const favRow = buildRow('s2');
    assert.ok(favRow.innerHTML.includes('aria-pressed="false"'), 'unselected rows must show an inactive +');
    add(favRow);
    assert.deepEqual([...adapter.selectedIds()], ['s2'], 'a + click must add the track to the selection');

    // Playback does not touch the selection.
    fetchCalls.length = 0;
    favRow.querySelector('.track-play').click();
    await tick();
    assert.ok(fetchCalls.some((c) => c.url === '/api/play'), 'the row play button must dispatch playback');
    assert.deepEqual([...adapter.selectedIds()], ['s2'], 'playback must never clear the playlist selection');

    // Favoriting does not touch the selection.
    fetchCalls.length = 0;
    favRow.querySelectorAll('.streaming-fav, .track-fav')[0].click();
    await tick();
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/tracks/s2/favorite'),
        'the row heart must dispatch the favorite write-back');
    assert.deepEqual([...adapter.selectedIds()], ['s2'], 'favoriting must never clear the playlist selection');

    // Another surface shows the same selection state on a fresh row.
    assert.ok(buildRow('s2').innerHTML.includes('aria-pressed="true"'), 's2 must render selected on every surface');
    assert.ok(!buildRow('s1').innerHTML.includes('aria-pressed="true"'), 's1 must render unselected');

    // Select s1, then take it out again: exactly s2 stays.
    const s1Row = buildRow('s1');
    add(s1Row);
    assert.deepEqual([...adapter.selectedIds()].sort(), ['s1', 's2']);
    add(s1Row);
    assert.deepEqual([...adapter.selectedIds()], ['s2'], 'a second + click must take the track out again');

    // The Library selects and clears whole lists through the adapter.
    adapter.selectTracks(['a', 'b']);
    assert.deepEqual([...adapter.selectedIds()].sort(), ['a', 'b', 's2']);
    adapter.deselectTracks(['a']);
    assert.deepEqual([...adapter.selectedIds()].sort(), ['b', 's2']);
    adapter.clearSelection();
    assert.deepEqual([...adapter.selectedIds()], []);
}

// --- 15. save as a new TIDAL playlist: exact selection, then cleanup --------

{
    const { sandbox, fetchCalls } = runStreaming({ tidalUser: '42' });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    adapter.selectTracks(['s2']);

    fetchCalls.length = 0;
    const created = await adapter.createPlaylist('Test Mix', adapter.selectedIds());
    await tick();
    await tick();
    const createCall = fetchCalls.find((c) => c.url === '/api/streaming/tidal/playlists/create');
    assert.ok(createCall, 'save-as-new must POST to the create endpoint');
    assert.deepEqual(JSON.parse(createCall.opts.body), { name: 'Test Mix', track_ids: ['s2'] },
        'the created playlist must contain exactly the selected tracks');
    assert.equal(created.name, 'Test Mix');
    assert.deepEqual([...adapter.selectedIds()], [], 'a successful save must clear the selection');
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/playlists' && !c.opts.method),
        'a successful save must refetch the playlists list');
    assert.ok(adapter.items('playlists').some((item) => item.name === 'Test Mix'),
        'the freshly created playlist must be visible in the Library playlists');
}

// --- 16. add the selection to an existing TIDAL playlist --------------------

{
    const warning = 'Playlist created (pl-new); track additions are unconfirmed. Check the existing playlist.';
    const { sandbox, fetchCalls } = runStreaming({ tidalUser: '42', playlistSeedWarning: warning });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    adapter.selectTracks(['s1']);
    fetchCalls.length = 0;
    const created = await adapter.createPlaylist('Partial Mix', adapter.selectedIds());
    await tick();
    await tick();
    assert.equal(created.id, 'pl-new');
    assert.equal(created.warning, warning, 'the caller must receive the partial-success warning');
    assert.equal(created.added_track_ids, null, 'unknown additions are not an empty confirmation');
    assert.deepEqual([...adapter.selectedIds()], [], 'confirmed creation consumes the selection to avoid a duplicate Save');
    assert.equal(fetchCalls.filter((c) => c.url === '/api/streaming/tidal/playlists/create').length, 1);
    assert.ok(adapter.items('playlists').some((item) => item.id === 'pl-new'), 'refresh exposes the existing playlist for recovery');
}

{
    const { sandbox, fetchCalls } = runStreaming({ tidalUser: '42' });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    adapter.selectTracks(['s1']);

    fetchCalls.length = 0;
    await adapter.addToPlaylist('pl-1', adapter.selectedIds());
    const addCall = fetchCalls.find((c) => c.url === '/api/streaming/tidal/playlists/pl-1/tracks' && c.opts.method === 'POST');
    assert.ok(addCall, 'add-to-playlist must POST to the playlist tracks endpoint');
    assert.deepEqual(JSON.parse(addCall.opts.body), { track_ids: ['s1'] },
        'add-to-playlist must send exactly the selected tracks');
    assert.deepEqual([...adapter.selectedIds()], [], 'a successful add must clear the selection');
}

// --- 17. failed write keeps the selection -----------------------------------

{
    const { sandbox, fetchCalls } = runStreaming({ tidalUser: '42', failPlaylistWrite: true });
    await tick();
    const adapter = sandbox.window.FXRouteStreaming.tidal;
    adapter.selectTracks(['s1']);

    fetchCalls.length = 0;
    await assert.rejects(adapter.createPlaylist('Keep Mix', adapter.selectedIds()), /TIDAL unreachable|Failed/);
    assert.ok(fetchCalls.some((c) => c.url === '/api/streaming/tidal/playlists/create'),
        'the failed save must still attempt the create write');
    assert.deepEqual([...adapter.selectedIds()], ['s1'], 'a failed save must keep the selection');
    await assert.rejects(adapter.addToPlaylist('pl-1', adapter.selectedIds()));
    assert.deepEqual([...adapter.selectedIds()], ['s1'], 'a failed add must keep the selection');
}

// --- 19. playlist detail: description, single-artist about, featuring line ---

{
    const run = (detail, extra) => runStreaming(Object.assign({ playlistDetail: detail }, extra || {}));
    const openPlaylist = async (detail, extra) => {
        const harness = run(detail, extra);
        await tick();
        const host = attachHost(harness.sandbox);
        harness.sandbox.window.FXRouteStreaming.tidal.openDetail('playlist', 'pl-1', 'Test Playlist', '');
        await tick();
        return { harness, host };
    };

    // Description wins over everything and the Play playlist button is gone.
    {
        const { harness, host } = await openPlaylist({
            id: 'pl-1', name: 'Test Playlist', description: 'A curated mix', art_url: '', track_count: 2,
            artists: [{ id: 'a1', name: 'Found Artist' }],
            enrichment: { available: true, artist: { about: 'A long artist bio' } },
        });
        assert.ok(harness.fetchCalls.some((c) => c.url === '/api/streaming/tidal/playlists/pl-1'),
            'opening a playlist must fetch the playlist detail');
        const info = host.container.querySelector('#tidal-playlist-info');
        assert.ok(info && info.innerHTML.includes('A curated mix'),
            'the playlist description must render as the header info');
        assert.ok(!host.container.innerHTML.includes('Play playlist'),
            'the playlist detail must not offer a Play playlist button');
        assert.ok(!host.container.innerHTML.includes('A long artist bio'),
            'the description must win over the single-artist about');
    }

    // No description, single artist: the MusicBrainz about text is used.
    {
        const { host } = await openPlaylist({
            id: 'pl-1', name: 'Test Playlist', description: '', art_url: '', track_count: 2,
            artists: [{ id: 'a1', name: 'Found Artist' }],
            enrichment: { available: true, artist: { about: 'A long artist bio' } },
        });
        const info = host.container.querySelector('#tidal-playlist-info');
        assert.ok(info && info.innerHTML.includes('A long artist bio'),
            'a single-artist playlist without description must use the enriched artist about');
    }

    // Multi-artist tracks without description/enrichment: a compact featuring
    // line built from the distinct track artists, never a fake biography.
    {
        const { host } = await openPlaylist({
            id: 'pl-1', name: 'Test Playlist', description: '', art_url: '', track_count: 3,
            artists: [{ id: 'a1', name: 'Artist A' }, { id: 'a2', name: 'Artist B' }],
        }, {
            playlistTracks: [
                { id: 'p1', title: 'One', artist: 'Artist A', duration: 10 },
                { id: 'p2', title: 'Two', artist: 'Artist A', duration: 12 },
                { id: 'p3', title: 'Three', artist: 'Artist B', duration: 13 },
            ],
        });
        const info = host.container.querySelector('#tidal-playlist-info');
        assert.ok(info && info.innerHTML.includes('Featuring'),
            'a multi-artist playlist without description must render a featuring line');
    }
}

// --- 20. provider tab visibility: a disabled provider stays hidden -----------
//
// Regression guard: a Spotify status payload carries `installed` only, and it
// arrives far more often than discovery. The shared provider module is the one
// owner of tab visibility, so no incoming state - playing, paused, stopped or
// a discovery poll - may re-show the tab of a provider the user disabled.
{
    const discovery = (spotifyEnabled) => ([
        { id: 'spotify', name: 'Spotify', implemented: true, installed: true, available: true, authenticated: null, enabled: spotifyEnabled },
        { id: 'qobuz', name: 'Qobuz', implemented: true, installed: true, available: true, authenticated: true, enabled: true },
        { id: 'tidal', name: 'Tidal', implemented: true, installed: true, available: true, authenticated: true, enabled: true },
    ]);
    const spotifyStatus = (status) => ({
        installed: true, available: true, authenticated: null, capabilities: baseCaps,
        status, title: 't', artist: 'a', album: 'b', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 100,
    });

    const providers = discovery(false);
    const { sandbox, tabButtons, tabPanels } = runStreaming({ discoveryProviders: providers });
    const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
    const spotifyHidden = () => tabButtons.spotify.style.display === 'none'
        && tabButtons.spotify.hidden === true
        && tabButtons.spotify.classList.contains('hidden');

    await tick();
    await sandbox.window.FXRouteStreaming.refreshEnabledFlags();
    assert.ok(spotifyHidden(), 'a disabled provider tab must be hidden');
    assert.equal(tabPanels.spotify.hidden, true, 'a disabled provider panel must be hidden too');

    for (const status of ['Paused', 'Playing', 'Playing', 'Stopped']) {
        sandbox.window.FXRouteStreaming.renderProvider('spotify', spotifyStatus(status));
        assert.ok(spotifyHidden(), `a disabled provider tab must stay hidden on an incoming ${status} state`);
    }
    await tick();
    await sandbox.window.FXRouteStreaming.refreshEnabledFlags();
    assert.ok(spotifyHidden(), 'a disabled provider tab must stay hidden across discovery polls');

    // Re-enabling the provider brings the tab back.
    providers[0].enabled = true;
    await sandbox.window.FXRouteStreaming.refreshEnabledFlags();
    sandbox.window.FXRouteStreaming.renderProvider('spotify', spotifyStatus('Playing'));
    assert.ok(!spotifyHidden() && tabButtons.spotify.hidden === false,
        're-enabling the provider must restore its tab');

    // Hiding the tab the user is looking at hands the view back to radio.
    const switched = [];
    sandbox.switchTab = (id) => { switched.push(id); };
    sandbox.window.__visibleTab = 'spotify';
    providers[0].enabled = false;
    await sandbox.window.FXRouteStreaming.refreshEnabledFlags();
    assert.ok(spotifyHidden(), 'disabling the provider must hide its tab again');
    assert.deepEqual(switched, ['radio'],
        'hiding the active provider tab must switch back to radio');
}

console.log('PASS  scripts/test_streaming_ui_actions.js');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
