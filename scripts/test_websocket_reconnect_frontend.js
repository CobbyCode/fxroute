#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Exercise the actual app connection lifecycle without network or wall-clock waits.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const config = source.slice(source.indexOf('const CONFIG ='), source.indexOf('const MeasurementDsp ='));
const globals = source.slice(source.indexOf('// WebSocket\n'), source.indexOf('let tidalFavoriteRequestInFlight'));
const lifecycle = source.slice(source.indexOf('function setupWebSocket()'), source.indexOf('// Volatile Try session hint'));

function harness({ failConnections = false, throwConnections = false, rejectRefresh = null, realLibrary = false } = {}) {
    let now = 0, nextTimer = 1;
    const timers = new Map(), sockets = [], attempts = [], calls = [], toasts = [], messages = [];
    const windowListeners = {}, documentListeners = {};
    const requests = [];
    const server = { scanning: false, albums: [{ id: 'initial', name: 'Initial album', artist: 'Artist' }] };
    const setTimeout = (callback, delay = 0) => {
        const id = nextTimer++;
        timers.set(id, { callback, at: now + delay });
        return id;
    };
    const refresh = name => async (...args) => {
        calls.push([name, ...args]);
        if (name === rejectRefresh) throw new Error('refresh unavailable');
    };
    class Transport {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSED = 3;
        constructor(url) {
            attempts.push(now);
            if (throwConnections) throw new Error('socket construction failed');
            this.url = url;
            this.readyState = Transport.CONNECTING;
            this.sent = [];
            sockets.push(this);
            if (failConnections) setTimeout(() => this.close(), 1);
        }
        open() {
            this.readyState = Transport.OPEN;
            this.onopen();
        }
        close() {
            this.readyState = Transport.CLOSED;
            this.onclose?.({ code: 1006, reason: 'test disconnect', wasClean: false });
        }
        send(text) {
            assert.equal(this.readyState, Transport.OPEN);
            this.sent.push(text);
        }
        message(data) { this.onmessage({ data: JSON.stringify(data) }); }
    }
    const hiddenClasses = new Set(['hidden']);
    const context = vm.createContext({
        WebSocket: Transport,
        setTimeout,
        clearTimeout: id => timers.delete(id),
        console: { log() {}, error() {}, debug() {}, warn() {} },
        window: {
            location: { protocol: 'https:', host: 'kiosk.test' },
            addEventListener: (name, callback) => { windowListeners[name] = callback; },
            FXRouteSrcSettings: { refresh: refresh('src') },
        },
        document: {
            hidden: false,
            addEventListener: (name, callback) => { documentListeners[name] = callback; },
        },
        state: { wsConnected: false },
        elements: { offlineIndicator: { classList: {
            add: name => hiddenClasses.add(name), remove: name => hiddenClasses.delete(name),
        } } },
        SettingsSystem: { updatePowerButtonConnectionState() {} },
        PlaybackCore: {
            stopMetadataPolling: () => calls.push(['metadata-stop']),
            startMetadataPolling: () => calls.push(['metadata-start']),
            startPeakStatusPolling: () => calls.push(['peaks-start']),
            stopPeakStatusPolling: () => calls.push(['peaks-stop']),
            resyncPlaybackAfterReconnect: refresh('playback'),
        },
        EffectsUI: { fetchEffects: refresh('effects') },
        LibraryUI: {
            fetchTracks: refresh('tracks'), fetchPlaylists: refresh('playlists'),
            fetchDownloadStatus: refresh('downloads'), fetchAlbums: refresh('albums'),
        },
        radioModule: { fetchStations: refresh('stations') },
        MeasurementFlows: { recoverAutoSubJob: refresh('measurement') },
        isMeasurementPanelOpen: () => true,
        fetchAudioSourceOverview: refresh('source'),
        fetchAudioOutputOverview: refresh('outputs'),
        fetchOutputSystemCatalog: refresh('catalog'),
        handleWebSocketMessage: msg => messages.push(msg),
        showToast: (...args) => toasts.push(args),
    });
    if (realLibrary) {
        context.state.library = {
            viewMode: 'tracks', tracks: [], albumsLoaded: true, selectedTrackIds: [],
            // An open provider detail suppresses background grid rendering, not data reads.
            tidalDetailOpen: true, scanning: false, scanStatus: null,
            albums: [{ id: 'cached', name: 'Cached album', artist: 'Artist' }],
        };
        context.fetch = async (url, options) => {
            requests.push({ url, method: options?.method || 'GET' });
            const payloads = {
                '/api/tracks': [], '/api/playlists': [], '/api/albums': server.albums,
                '/api/library/status': { scanning: server.scanning, files_seen: 10, tracks_found: 2 },
                '/api/download/status': { status: 'idle' },
            };
            assert.ok(url in payloads, `unexpected library request ${url}`);
            return { ok: true, json: async () => payloads[url] };
        };
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/library_ui.js'), 'utf8'), context);
        context.LibraryUI = context.FXRouteLibraryUI;
        context.LibraryUI.init({ getState: () => context.state, getElements: () => ({}) });
    }
    vm.runInContext(config + globals + lifecycle, context);
    const advance = ms => {
        const until = now + ms;
        let steps = 0;
        while (true) {
            const next = [...timers.entries()].filter(([, timer]) => timer.at <= until)
                .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
            if (!next) break;
            assert.ok(++steps < 10000, 'timers must not spin');
            now = next[1].at;
            timers.delete(next[0]);
            next[1].callback();
        }
        now = until;
    };
    return {
        context, sockets, attempts, calls, toasts, messages, hiddenClasses, advance, requests, server,
        run: code => vm.runInContext(code, context),
        online: () => windowListeners.online?.(),
        visible: () => documentListeners.visibilitychange?.(),
    };
}

test('a ten-minute outage keeps retrying after the tenth failed connection', () => {
    const h = harness({ failConnections: true });
    h.run('setupWebSocket()');
    h.advance(600000);
    assert.ok(h.attempts.length > 20, `only ${h.attempts.length} attempts`);
    assert.ok(h.attempts.at(-1) > 570000, 'still retrying near the end of the outage');
    assert.deepEqual(h.toasts, [], 'no permanent please-refresh failure');
    assert.equal(h.context.state.wsConnected, false);
    assert.ok(h.calls.some(([name]) => name === 'metadata-start'));
});

test('failed connections use increasing backoff capped at thirty seconds', () => {
    const h = harness({ failConnections: true });
    h.run('setupWebSocket()');
    h.advance(180000);
    const waits = h.attempts.slice(1).map((at, index) => at - h.attempts[index] - 1);
    assert.deepEqual(waits.slice(0, 6), [3000, 6000, 12000, 24000, 30000, 30000]);
    assert.ok(waits.every(delay => delay <= 30000));
});

test('successful recovery resets retry backoff and switches polling ownership', () => {
    const h = harness();
    h.run('setupWebSocket()');
    h.sockets[0].close(); h.advance(3000);
    h.sockets[1].close(); h.advance(6000);
    const recovered = h.sockets[2];
    recovered.open();
    assert.equal(h.context.state.wsConnected, true);
    assert.ok(h.hiddenClasses.has('hidden'));
    assert.ok(h.calls.some(([name]) => name === 'metadata-stop'));
    assert.ok(h.calls.some(([name]) => name === 'peaks-start'));
    recovered.close();
    const before = h.attempts.length;
    h.advance(2999);
    assert.equal(h.attempts.length, before);
    h.advance(1);
    assert.equal(h.attempts.length, before + 1);
});

test('duplicate reconnect requests never create concurrent connecting or open sockets', () => {
    const h = harness();
    h.run('setupWebSocket(); connectWebSocket(); scheduleReconnect(); scheduleReconnect()');
    h.advance(30000);
    // Even when the connection watchdog replaces a stuck connection, only its successor is live.
    assert.equal(h.sockets.filter(socket => socket.readyState === 0 || socket.readyState === 1).length, 1);
    h.sockets.at(-1).open();
    const before = h.attempts.length;
    h.run('connectWebSocket(); scheduleReconnect()');
    h.advance(3000);
    assert.equal(h.attempts.length, before);
});

test('late callbacks from replaced sockets cannot change current state or deliver pushes', () => {
    const h = harness();
    h.run('setupWebSocket()');
    const stale = h.sockets[0];
    stale.close(); h.advance(3000);
    const current = h.sockets[1];
    current.open();
    stale.onopen(); stale.message({ type: 'dsp', data: {} }); stale.close();
    assert.equal(h.context.state.wsConnected, true);
    assert.equal(h.messages.length, 0);
    current.message({ type: 'dsp', data: { current: true } });
    assert.equal(h.messages.length, 1);
});

test('online and visible-page events retry promptly without duplicate connections', () => {
    const h = harness();
    h.run('setupWebSocket()');
    h.sockets[0].close();
    h.context.document.hidden = true;
    h.visible();
    assert.equal(h.sockets.length, 1, 'hidden pages do not bypass backoff');
    h.online();
    assert.equal(h.sockets.length, 2);
    h.online();
    assert.equal(h.sockets.length, 2);
    h.sockets[1].close();
    h.context.document.hidden = false;
    h.visible();
    assert.equal(h.sockets.length, 3);
});

test('socket constructor failures are retried instead of stopping setup', () => {
    const h = harness({ throwConnections: true });
    assert.doesNotThrow(() => h.run('setupWebSocket()'));
    h.advance(180000);
    assert.ok(h.attempts.length > 6);
});

test('a socket stuck in CONNECTING is closed and replaced', () => {
    const h = harness();
    h.run('setupWebSocket()');
    h.advance(60000);
    assert.equal(h.sockets[0].readyState, 3);
    assert.ok(h.sockets.length >= 2);
});

test('an open but unresponsive socket is replaced even without a browser close event', () => {
    const h = harness();
    h.run('setupWebSocket()');
    h.sockets[0].open();
    h.advance(60000);
    assert.ok(h.sockets[0].sent.length > 0, 'heartbeat sent');
    assert.equal(h.sockets[0].readyState, 3);
    assert.ok(h.sockets.length >= 2);
});

test('server pongs keep a healthy socket open and are not dispatched as app state', () => {
    const h = harness();
    h.run('setupWebSocket()');
    const socket = h.sockets[0];
    socket.open();
    for (let index = 0; index < 5; index++) {
        h.advance(25000);
        assert.equal(socket.sent.length, index + 1);
        socket.message({ type: 'pong' });
    }
    assert.equal(h.sockets.length, 1);
    assert.equal(h.context.state.wsConnected, true);
    assert.equal(h.messages.length, 0);
});

test('a reconnect refreshes all state omitted from the server init frame', async () => {
    const h = harness();
    h.run('setupWebSocket()');
    h.sockets[0].open();
    await new Promise(resolve => setImmediate(resolve));
    h.calls.length = 0;
    h.sockets[0].close(); h.advance(3000); h.sockets[1].open();
    await new Promise(resolve => setImmediate(resolve));
    for (const name of ['playback', 'source', 'effects', 'tracks', 'albums', 'playlists', 'downloads',
        'stations', 'outputs', 'catalog', 'src', 'measurement']) {
        assert.ok(h.calls.some(([called]) => called === name), `missing ${name} resync`);
    }
    assert.ok(h.calls.some(([name, force]) => name === 'catalog' && force === true), 'bypass stale catalog cache');
});

test('one rejected state refresh does not prevent the other reconnect refreshes', async () => {
    const h = harness({ rejectRefresh: 'effects' });
    h.run('setupWebSocket()');
    h.sockets[0].open();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(h.calls.some(([name]) => name === 'effects'));
    assert.ok(h.calls.some(([name]) => name === 'downloads'));
    assert.ok(h.calls.some(([name]) => name === 'catalog'));
    assert.equal(h.context.state.wsConnected, true);
});

test('recovery replaces populated album caches through read-only requests in the real library module', async () => {
    const h = harness({ realLibrary: true });
    h.run('setupWebSocket()');
    h.sockets[0].open();
    await new Promise(resolve => setImmediate(resolve));
    const readsBeforeCachedFetch = h.requests.length;
    await h.context.LibraryUI.fetchAlbums();
    assert.equal(h.requests.length, readsBeforeCachedFetch, 'ordinary navigation still uses the cache');
    h.sockets[0].close();
    h.server.albums = [{ id: 'after-outage', name: 'Added during outage', artist: 'Artist' }];
    h.advance(3000); h.sockets[1].open();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.context.state.library.albums[0].id, 'after-outage');
    assert.ok(h.requests.some(request => request.url === '/api/albums'));
    assert.ok(h.requests.every(request => request.method === 'GET'), 'recovery must not start a scan');
});

test('recovery queues a fresh album read when an older request is still in flight', async () => {
    const h = harness({ realLibrary: true });
    const transport = h.context.fetch;
    let releaseOldRequest;
    h.context.fetch = (url, options) => {
        if (url === '/api/albums' && !releaseOldRequest) {
            return new Promise(resolve => { releaseOldRequest = resolve; });
        }
        return transport(url, options);
    };
    const oldRead = h.context.LibraryUI.fetchAlbums(true);
    h.run('setupWebSocket()');
    h.sockets[0].open();
    await new Promise(resolve => setImmediate(resolve));
    releaseOldRequest({ ok: true, json: async () => [{ id: 'before-outage', name: 'Old snapshot', artist: 'Artist' }] });
    await oldRead;
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.context.state.library.albums[0].id, 'initial', 'an older in-flight read cannot swallow recovery');
});

test('repeated real-library resyncs keep one scan-status polling chain and stop it after completion', async () => {
    const h = harness({ realLibrary: true });
    h.server.scanning = true;
    h.run('setupWebSocket()');
    h.sockets[0].open();
    await new Promise(resolve => setImmediate(resolve));
    for (let index = 0; index < 3; index++) {
        h.visible();
        await new Promise(resolve => setImmediate(resolve));
    }
    const statusReads = () => h.requests.filter(request => request.url === '/api/library/status').length;
    const before = statusReads();
    h.advance(1200);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(statusReads() - before, 1, 'recovery must not multiply recurring scan polls');
    h.server.scanning = false;
    h.advance(1200);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.context.state.library.scanning, false);
    const settled = statusReads();
    h.advance(3600);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(statusReads(), settled, 'completed scans have no remaining polling timer');
});
