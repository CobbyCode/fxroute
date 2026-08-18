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
        dispatch(type) { (listeners[type] || []).forEach((fn) => fn()); },
        click() { el.dispatch('click'); },
        setAttribute(name, value) { el[name] = value; },
        getAttribute(name) { return el[name] != null ? String(el[name]) : null; },
        querySelector() { return makeEl(); },
        querySelectorAll() { return []; },
        appendChild() {},
        closest() { return null; },
    };
    return el;
}

function shellEl(providerId) {
    const el = makeEl();
    el['data-provider'] = providerId;
    const memo = {};
    // Browse tabs are real interactive elements in the shim so detail views
    // can be entered by clicking the Playlists tab.
    const tabs = ['search', 'favorites', 'playlists'].map((name) => {
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

function runStreaming() {
    const { document, shells, createdEls } = buildDom();
    const fetchCalls = [];
    const spotifyCommandCalls = [];
    const spotifySeekCalls = [];

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
    const { sandbox, shells, createdEls } = runStreaming();
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
    assert.ok(content.innerHTML.includes('tidal-detail'), 'playlist detail view must render');
    assert.equal(statusLine.hidden, true, 'detail view must hide the standalone status line');

    // Leaving the detail view restores the status line immediately.
    content.querySelector('#tidal-detail-back').click();
    assert.equal(statusLine.hidden, false, 'back navigation must restore the status line');
}

console.log('PASS  scripts/test_streaming_ui_actions.js');
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
