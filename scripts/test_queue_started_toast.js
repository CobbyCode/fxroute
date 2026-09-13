#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Queue-started cue parity: Spotify, Qobuz and TIDAL must reuse the existing
// showNowPlayingCue path (same component, message, presentation, duration)
// with real provider metadata and the correct queue/track count.
// Local Library playLocal is the regression anchor.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const streamingJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`(async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let i = brace; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

// Real provider fixtures (shapes as served by .104).
const SPOTIFY_FIXTURE = {
    status: 'Playing', title: 'Too Easy', artist: 'Da Flyy Hooligan',
    album: "Today's Agenda", trackId: '/com/spotify/track/1n2kHqn6Yo0Vdum2gqfVs1',
    artUrl: 'https://i.scdn.co/image/ab67616d0000b273dd1a2887b598734def923c51',
};
const QOBUZ_FIXTURE = {
    status: 'Playing', title: 'Scrub Me Mama with a Boogie Beat', artist: 'Swing Republic',
    album: 'Electro Swing Republic', trackId: '46115439',
    artUrl: 'https://static.qobuz.com/images/covers/13/54/3614594365413_600.jpg',
    queue_len: 11, queue_index: 2,
    next_track: { id: '46115440', title: 'Any Old Thing' },
};
const TIDAL_TRACK = {
    id: '175015095', title: 'Warning (2005 Remaster)', artist: 'The Notorious B.I.G.',
    album: 'Music Inspired By Biggie: I Got A Story To Tell',
    art_url: 'https://resources.tidal.com/images/075cb816/006d/463b/8c40/104e2aef0eb2/640x640.jpg',
    source: 'tidal',
    artwork_available: true,
    artwork_url: 'https://resources.tidal.com/images/075cb816/006d/463b/8c40/104e2aef0eb2/640x640.jpg',
    artwork_source: 'tidal',
};

const cases = [];
async function run(label, fn) {
    try { await fn(); cases.push({ label, pass: true }); }
    catch (err) { cases.push({ label, pass: false, err }); }
}

// --- minimal DOM shim for streaming.js (subset of test_streaming_ui_actions.js) ---
function makeEl() {
    const listeners = {};
    const childEls = {};
    const el = {
        style: {}, dataset: {}, hidden: false, textContent: '', innerHTML: '',
        title: '', value: '', disabled: false, className: '', type: '',
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
        addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
        dispatch(type, ev) { (listeners[type] || []).forEach((fn) => fn(ev || {})); },
        click() { el.dispatch('click', { target: el, preventDefault() {}, stopPropagation() {} }); },
        focus() {},
        setAttribute(n, v) { el[n] = v; },
        getAttribute(n) { return el[n] != null ? String(el[n]) : null; },
        querySelector(sel) {
            if (sel === '.streaming-add[data-streaming-add]') return el.querySelectorAll(sel)[0];
            if (!childEls[sel]) childEls[sel] = makeEl();
            return childEls[sel];
        },
        querySelectorAll(sel) {
            if (sel === '.streaming-fav, .track-fav') {
                if (!el._favBtn) {
                    const m = el.innerHTML.match(/data-fav-type="([^"]+)" data-fav-id="([^"]+)"/);
                    const btn = makeEl(); btn.className = 'streaming-fav';
                    btn.dataset.favType = m ? m[1] : ''; btn.dataset.favId = m ? m[2] : '';
                    el._favBtn = btn;
                }
                return [el._favBtn];
            }
            if (sel === '.streaming-add[data-streaming-add]') {
                if (!el._addBtn) {
                    const m = el.innerHTML.match(/data-streaming-add="([^"]+)"/);
                    const btn = makeEl(); btn.className = 'streaming-add';
                    btn.dataset.streamingAdd = m ? m[1] : '';
                    el._addBtn = btn;
                }
                return [el._addBtn];
            }
            return [];
        },
        appendChild() {}, prepend() {}, closest() { return null; },
    };
    return el;
}
function shellEl(pid) {
    const el = makeEl(); el['data-provider'] = pid;
    const memo = {};
    const tabs = ['tracks', 'albums', 'artists', 'playlists'].map((n) => {
        const t = makeEl(); t.dataset.browse = n; return t;
    });
    el.querySelector = (sel) => {
        if (!memo[sel]) {
            memo[sel] = makeEl();
            memo[sel].querySelectorAll = (inner) => {
                if (inner === '.view-tab' || inner === '.tidal-subbar .view-tab[data-browse]') return tabs;
                return [];
            };
            memo[sel].querySelector = (inner) => {
                const k = sel + ' ' + inner;
                if (!memo[k]) memo[k] = makeEl();
                return memo[k];
            };
        }
        return memo[sel];
    };
    return el;
}

const baseCaps = { transport: true, seek: true, shuffle: true, loop: true, progress: true, cover: true, audio_format: true, bit_depth: true, sample_rate: true };

function runStreaming({ fetchImpl, cueCalls }) {
    const providers = ['spotify', 'qobuz', 'tidal'];
    const shells = {}; const tabButtons = {}; const tabPanels = {}; const byId = {}; const createdEls = [];
    for (const pid of providers) { shells[pid] = shellEl(pid); tabButtons[pid] = makeEl(); tabPanels[pid] = makeEl(); }
    const tidalBrowseBody = makeEl();
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
        createElement() { const el = makeEl(); createdEls.push(el); return el; },
    };
    const fetchCalls = [];
    const sandbox = {
        document,
        window: { __visibleTab: 'radio', setInterval: () => 0, clearInterval: () => {}, setTimeout, clearTimeout, scrollTo() {} },
        setInterval: () => 0, clearInterval: () => {}, setTimeout, clearTimeout,
        fetch: async (url, opts) => {
            fetchCalls.push({ url: String(url), opts: opts || {} });
            if (fetchImpl) return fetchImpl(String(url), opts || {});
            return { ok: true, json: async () => ({}) };
        },
        console, Date, String, Number, Object, JSON, RegExp, encodeURIComponent, decodeURIComponent, navigator: undefined,
    };
    vm.createContext(sandbox);
    vm.runInContext(streamingJs, sandbox);
    const api = {
        showToast() {}, showNowPlayingCue: (...a) => { cueCalls.push(a); },
        escapeHtml: (v) => String(v), formatTime: () => '0:00',
        trackRowHtml: ({ title, sub }) => `<button class="track-play">▶</button><div>${title}${sub || ''}</div>`,
        factsHtml: () => '', aboutHtml: () => '',
        spotifyCommand: async () => {}, spotifySeek: async () => {},
    };
    sandbox.window.FXRouteStreaming.init(api);
    return { sandbox, shells, fetchCalls, createdEls, document, tidalBrowseBody };
}

(async () => {
    // 1. Local regression anchor: playLocal still uses the canonical cue.
    await run('local playLocal keeps the canonical queue-started cue', async () => {
        assert.ok(appJs.includes('async function playLocal('), 'missing playLocal');
        assert.ok(appJs.includes("showNowPlayingCue(playedTrack, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing')"),
            'playLocal must keep the Queue started / Now playing cue');
    });

    // 2. Architecture: no provider-specific toast, same injected path.
    await run('streaming reuses the injected showNowPlayingCue (no provider toast)', async () => {
        assert.ok(streamingJs.includes('let showNowPlayingCue = function () {};'), 'streaming must declare the injected cue');
        assert.ok(streamingJs.includes("if (typeof api.showNowPlayingCue === 'function') showNowPlayingCue = api.showNowPlayingCue;"),
            'streaming must accept the injected cue');
        assert.ok(appJs.includes('showNowPlayingCue,'), 'app.js must inject showNowPlayingCue into streaming');
        assert.ok(!/function\s+showSpotify.*Toast|function\s+showQobuz.*Toast|function\s+showTidal.*Toast/.test(streamingJs),
            'streaming must not build a provider-specific toast');
        assert.ok(!/function\s+showSpotify.*Toast|function\s+showQobuz.*Toast|function\s+showTidal.*Toast/.test(appJs),
            'app.js must not build a provider-specific toast');
    });

    // 3. TIDAL wiring: playTidalTracks uses the shared cue with playback metadata.
    await run('tidal playTidalTracks calls the shared cue', async () => {
        assert.ok(streamingJs.includes('async function playTidalTracks('), 'missing playTidalTracks');
        assert.ok(streamingJs.includes('showNowPlayingCue(playedTrack, queueCount > 1 ? `Queue started'),
            'playTidalTracks must call the shared cue with the queue count');
        assert.ok(streamingJs.includes('data?.playback?.current_track'), 'tidal cue must use playback.current_track');
        assert.ok(streamingJs.includes('data?.playback?.queue?.count'), 'tidal cue must use playback.queue.count');
    });

    // 4. Spotify wiring in app.js: the incoming-state path cues (covers the
    // FXRoute transport response, poll refreshes and out-of-band starts).
    await run('spotify incoming state reuses the shared cue on queue start', async () => {
        assert.ok(appJs.includes('function handleIncomingSpotifyState('), 'missing handleIncomingSpotifyState');
        assert.ok(appJs.includes("maybeShowStreamingQueueCue('spotify'"), 'spotify incoming state must call the shared streaming cue');
        assert.ok(appJs.includes('async function spotifyCommand('), 'missing spotifyCommand');
        assert.ok(appJs.includes('handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true })'),
            'spotifyCommand must route responses through the incoming-state path');
        assert.ok(appJs.includes('function showStreamingQueueStarted('), 'missing showStreamingQueueStarted helper');
        assert.ok(appJs.includes('function maybeShowStreamingQueueCue('), 'missing maybeShowStreamingQueueCue decision');
        assert.ok(appJs.includes('__lastPlayingQueueKey'), 'resume check must use the last playing key');
    });

    // 5. Qobuz wiring: footer command, tab transport and incoming state reuse
    // the shared cue (immediate command feedback + poll/out-of-band starts).
    await run('qobuz command, tab transport and incoming state reuse the shared cue', async () => {
        assert.ok(appJs.includes('async function qobuzCommand('), 'missing qobuzCommand');
        assert.ok(appJs.includes("maybeShowStreamingQueueCue('qobuz'"), 'qobuz paths must call the shared cue decision');
        assert.ok(appJs.includes('function handleIncomingQobuzState('), 'missing handleIncomingQobuzState');
        assert.ok(streamingJs.includes('maybeShowProviderQueueStarted('), 'qobuz tab transport must call the shared cue decision');
        assert.ok(streamingJs.includes('Number(data.queue_len || 0)'), 'qobuz cue must use the real queue_len');
    });

    // 6. Decision semantics: the maybe-helper behaves like playLocal, and a
    // Next-from-paused split (new id while Paused, then the Playing edge)
    // still cues instead of misreading the start as a resume.
    await run('streaming decision: queue message matches local library', async () => {
        const sandbox = { window: {}, showStreamingQueueStartedCalls: [] };
        vm.createContext(sandbox);
        sandbox.showStreamingQueueStarted = (...a) => { sandbox.showStreamingQueueStartedCalls.push(a); };
        for (const name of ['streamingCueTrackId', 'streamingCueKey', 'lastPlayingQueueKey', 'recordPlayingQueueKey', 'maybeShowStreamingQueueCue']) {
            vm.runInContext(extractFunction(appJs, name), sandbox);
        }
        const playing = (over = {}) => ({ ...SPOTIFY_FIXTURE, ...over });
        const cues = () => sandbox.showStreamingQueueStartedCalls.length;
        // First snapshot after page load anchors the key and stays silent.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', {}, playing()), false);
        assert.equal(cues(), 0);
        // Poll refresh of the same playing track: silent.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing(), playing()), false);
        assert.equal(cues(), 0);
        // Paused resume of the identical track is transport-only (like local toggle).
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ status: 'Paused' }), playing()), false);
        assert.equal(cues(), 0);
        // Playing -> Playing with a different track is a queue start.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing(), playing({ trackId: 'other' })), true);
        assert.equal(cues(), 1);
        // Split transition: new id lands while Paused (silent), then the
        // Playing edge with that id must cue (regression: same-id resume check).
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ trackId: 'other' }), playing({ trackId: 'split', status: 'Paused' })), false);
        assert.equal(cues(), 1);
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ trackId: 'split', status: 'Paused' }), playing({ trackId: 'split' })), true);
        assert.equal(cues(), 2);
        // Restart after Stopped cues even for the known track.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ trackId: 'split', status: 'Stopped' }), playing({ trackId: 'split' })), true);
        assert.equal(cues(), 3);
        // Qobuz queue_len branching mirrors playLocal's queueCount > 1.
        const qCount = Number(QOBUZ_FIXTURE.queue_len || 0);
        assert.ok(qCount > 1, 'qobuz fixture must carry a multi-track queue');
        assert.equal(`Queue started · ${qCount} tracks`, 'Queue started · 11 tracks');
    });

    // 7. Functional: TIDAL multi-track click shows Queue started with real metadata.
    await run('tidal track click shows Queue started with real metadata', async () => {
        const cueCalls = [];
        const favTracks = [
            { id: '175015090', title: 'Big Poppa (2005 Remaster)', artist: 'The Notorious B.I.G.', album: 'Biggie', art_url: TIDAL_TRACK.art_url, duration: 253 },
            { id: TIDAL_TRACK.id, title: TIDAL_TRACK.title, artist: TIDAL_TRACK.artist, album: TIDAL_TRACK.album, art_url: TIDAL_TRACK.art_url, duration: 220 },
        ];
        const { sandbox, shells, createdEls } = runStreaming({
            cueCalls,
            fetchImpl: async (u, opts) => {
                if (u === '/api/play') {
                    return { ok: true, json: async () => ({ playback: { current_track: { ...TIDAL_TRACK }, queue: { count: 14 } } }) };
                }
                if (u === '/api/streaming/tidal/status') {
                    return { ok: true, json: async () => ({ installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', shuffle: false }) };
                }
                if (u.startsWith('/api/streaming/tidal/favorites?type=tracks')) return { ok: true, json: async () => favTracks };
                if (u === '/api/streaming/tidal/favorites/ids') return { ok: true, json: async () => ({ tracks: [], albums: [], artists: [], playlists: [] }) };
                if (u.startsWith('/api/streaming/tidal/favorites')) return { ok: true, json: async () => [] };
                return { ok: true, json: async () => ({}) };
            },
        });
        const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
        sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
        await new Promise((r) => setTimeout(r, 0));
        shells.tidal.querySelector('.streaming-content').querySelectorAll('.view-tab').find((t) => t.dataset.browse === 'tracks').click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        const rows = createdEls.filter((el) => el.className === 'streaming-result');
        assert.ok(rows.length >= 2, 'tidal tracks must render');
        rows[1].click();
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(cueCalls.length, 1, 'tidal track click must show exactly one cue');
        const [track, message] = cueCalls[0];
        assert.equal(track.title, TIDAL_TRACK.title);
        assert.equal(track.artist, TIDAL_TRACK.artist);
        assert.equal(track.album, TIDAL_TRACK.album);
        assert.equal(track.artwork_url, TIDAL_TRACK.artwork_url);
        assert.equal(message, 'Queue started · 14 tracks');
    });

    // 8. Functional: TIDAL single-track shows Now playing (same as local single).
    await run('tidal single track shows Now playing', async () => {
        const cueCalls = [];
        const { sandbox, shells, createdEls } = runStreaming({
            cueCalls,
            fetchImpl: async (u) => {
                if (u === '/api/play') {
                    return { ok: true, json: async () => ({ playback: { current_track: { ...TIDAL_TRACK }, queue: { count: 1 } } }) };
                }
                if (u === '/api/streaming/tidal/status') {
                    return { ok: true, json: async () => ({ installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', shuffle: false }) };
                }
                if (u === '/api/streaming/tidal/favorites/ids') return { ok: true, json: async () => ({ tracks: [], albums: [], artists: [], playlists: [] }) };
                if (u.startsWith('/api/streaming/tidal/favorites')) return { ok: true, json: async () => [{ id: TIDAL_TRACK.id, title: TIDAL_TRACK.title, artist: TIDAL_TRACK.artist, album: TIDAL_TRACK.album, art_url: TIDAL_TRACK.art_url, duration: 220 }] };
                return { ok: true, json: async () => ({}) };
            },
        });
        const tidalData = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
        sandbox.window.FXRouteStreaming.renderProvider('tidal', tidalData);
        await new Promise((r) => setTimeout(r, 0));
        shells.tidal.querySelector('.streaming-content').querySelectorAll('.view-tab').find((t) => t.dataset.browse === 'tracks').click();
        await new Promise((r) => setTimeout(r, 0));
        await new Promise((r) => setTimeout(r, 0));
        createdEls.filter((el) => el.className === 'streaming-result')[0].click();
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(cueCalls.length, 1);
        assert.equal(cueCalls[0][1], 'Now playing');
        assert.equal(cueCalls[0][0].title, TIDAL_TRACK.title);
    });

    // 9. Functional: Qobuz tab toggle from Stopped shows Queue started with queue_len.
    await run('qobuz tab toggle shows Queue started with real queue_len', async () => {
        const cueCalls = [];
        const { sandbox, shells } = runStreaming({
            cueCalls,
            fetchImpl: async (u) => {
                if (u === '/api/streaming/qobuz/toggle') return { ok: true, json: async () => ({ ...QOBUZ_FIXTURE }) };
                if (u === '/api/streaming/qobuz/status') return { ok: true, json: async () => ({ ...QOBUZ_FIXTURE }) };
                return { ok: true, json: async () => ({}) };
            },
        });
        const stopped = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Stopped', title: '', artist: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
        sandbox.window.FXRouteStreaming.renderProvider('qobuz', stopped);
        shells.qobuz.querySelector('[data-action="toggle"]').click();
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(cueCalls.length, 1, 'qobuz queue start must cue once');
        const [track, message] = cueCalls[0];
        assert.equal(track.title, QOBUZ_FIXTURE.title);
        assert.equal(track.artist, QOBUZ_FIXTURE.artist);
        assert.equal(track.album, QOBUZ_FIXTURE.album);
        assert.equal(track.artwork_url, QOBUZ_FIXTURE.artUrl);
        assert.equal(message, 'Queue started · 11 tracks');
    });

    // 10. Functional: Qobuz resume of the same paused track shows no cue.
    await run('qobuz resume shows no cue (like local toggle)', async () => {
        const cueCalls = [];
        const pausedSame = { installed: true, available: true, authenticated: true, capabilities: baseCaps, status: 'Paused', title: QOBUZ_FIXTURE.title, artist: QOBUZ_FIXTURE.artist, album: QOBUZ_FIXTURE.album, trackId: QOBUZ_FIXTURE.trackId, artUrl: QOBUZ_FIXTURE.artUrl, queue_len: 11, shuffle: false, loop: 'none', position: 10, duration: 200 };
        const { sandbox, shells } = runStreaming({
            cueCalls,
            fetchImpl: async (u) => {
                if (u === '/api/streaming/qobuz/toggle') return { ok: true, json: async () => ({ ...QOBUZ_FIXTURE }) };
                if (u === '/api/streaming/qobuz/status') return { ok: true, json: async () => ({ ...QOBUZ_FIXTURE }) };
                return { ok: true, json: async () => ({}) };
            },
        });
        sandbox.window.FXRouteStreaming.renderProvider('qobuz', pausedSame);
        shells.qobuz.querySelector('[data-action="toggle"]').click();
        await new Promise((r) => setTimeout(r, 0));
        assert.equal(cueCalls.length, 0, 'resume must not cue');
    });

    // 11. Functional: Spotify helper shows Now playing with real cover (no queue count).
    await run('spotify queue start shows Now playing with real metadata', async () => {
        const sandbox = { showNowPlayingCueCalls: [] };
        vm.createContext(sandbox);
        sandbox.showNowPlayingCue = (...a) => { sandbox.showNowPlayingCueCalls.push(a); };
        vm.runInContext(extractFunction(appJs, 'streamingCueTrack'), sandbox);
        vm.runInContext(extractFunction(appJs, 'showStreamingQueueStarted'), sandbox);
        sandbox.showStreamingQueueStarted('spotify', SPOTIFY_FIXTURE);
        assert.equal(sandbox.showNowPlayingCueCalls.length, 1);
        const [track, message] = sandbox.showNowPlayingCueCalls[0];
        assert.equal(track.title, 'Too Easy');
        assert.equal(track.artist, 'Da Flyy Hooligan');
        assert.equal(message, 'Now playing');
        assert.equal(track.artwork_url, SPOTIFY_FIXTURE.artUrl);
    });

    // 12. Incoming-path decision: out-of-band track starts cue exactly once,
    // resumes and first snapshots stay silent, repeats are deduped.
    await run('incoming queue-start decision cues new tracks, dedupes repeats', async () => {
        const sandbox = { window: {}, showStreamingQueueStartedCalls: [] };
        vm.createContext(sandbox);
        sandbox.showStreamingQueueStarted = (...a) => { sandbox.showStreamingQueueStartedCalls.push(a); };
        for (const name of ['streamingCueTrackId', 'streamingCueKey', 'lastPlayingQueueKey', 'recordPlayingQueueKey', 'maybeShowStreamingQueueCue']) {
            vm.runInContext(extractFunction(appJs, name), sandbox);
        }
        const playing = (over = {}) => ({ ...SPOTIFY_FIXTURE, ...over });
        // First snapshot after page load anchors the key and stays silent.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', {}, playing()), false);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 0);
        // Resume of the last-playing paused track: no cue.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ status: 'Paused' }), playing()), false);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 0);
        // Playing -> Playing with a new track (external next): cue once.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing(), playing({ trackId: 'other' })), true);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 1);
        assert.equal(sandbox.showStreamingQueueStartedCalls[0][0], 'spotify');
        // Same poll result again (command echo / refresh): deduped, no second cue.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing(), playing({ trackId: 'other' })), false);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 1);
        // Paused -> Playing with a different track: cue.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ status: 'Paused' }), playing({ trackId: 'next' })), true);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 2);
        // Split transition (Next-from-paused): new id while Paused is silent,
        // the Playing edge with that id cues instead of reading as resume.
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ trackId: 'next' }), playing({ trackId: 'split', status: 'Paused' })), false);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 2);
        assert.equal(sandbox.maybeShowStreamingQueueCue('spotify', playing({ trackId: 'split', status: 'Paused' }), playing({ trackId: 'split' })), true);
        assert.equal(sandbox.showStreamingQueueStartedCalls.length, 3);
    });

    const failed = cases.filter((c) => !c.pass);
    for (const c of failed) console.error(`FAIL ${c.label}:`, c.err);
    console.log(`ok — ${cases.length - failed.length}/${cases.length} queue-started cue cases`);
    if (failed.length) process.exit(1);
})();

