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
    el.querySelector = (sel) => {
        if (!memo[sel]) memo[sel] = makeEl();
        return memo[sel];
    };
    return el;
}

function buildDom() {
    const providers = ['spotify', 'qobuz', 'tidal'];
    const shells = {};
    const tabButtons = {};
    const tabPanels = {};
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
            return m ? tabPanels[m[1]] : null;
        },
        createElement() { return makeEl(); },
    };
    return { document, shells, tabButtons, tabPanels };
}

function runStreaming() {
    const { document, shells } = buildDom();
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
            return { ok: true, json: async () => ({}) };
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
        spotifyCommand: (...a) => { spotifyCommandCalls.push(a); return Promise.resolve(); },
        spotifySeek: (...a) => { spotifySeekCalls.push(a); return Promise.resolve(); },
    };
    sandbox.window.FXRouteStreaming.init(api);
    return { sandbox, shells, fetchCalls, spotifyCommandCalls, spotifySeekCalls };
}

const baseCaps = {
    transport: true, seek: true, shuffle: true, loop: true, progress: true, cover: true,
    audio_format: true, bit_depth: true, sample_rate: true,
};

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
    assert.equal(shells.tidal.querySelector('.streaming-empty').hidden, false,
        'stopped render must show the empty state');
    assert.equal(shells.tidal.querySelector('.streaming-now-playing').hidden, true,
        'stopped render must hide the now-playing card (no ghost transport)');
}

console.log('PASS  scripts/test_streaming_ui_actions.js');
