#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Footer Qobuz resync: the shared footer must reach Qobuz through the same
// data paths Spotify has, not through additional heuristics.
//
// Regression for the .104 observation: with real external Spotify playback
// followed by an external switch to Qobuz, the backend committed
// playback_owner=qobuz and the Qobuz tile was correct, but the footer stayed
// on "Not Playing". Two load-bearing gaps combined:
//   1. WS init carried player + spotify but never qobuz, so a (re)connected
//      client started with __qobuzLastData=null.
//   2. app.js had no Qobuz HTTP fallback at all (no fetch, no poll), while
//      the Qobuz tab poll only feeds the tab, never the footer. A client
//      that missed the Qobuz WS broadcasts stayed stale permanently.
//
// This executes the VERBATIM functions extracted from static/app.js (no
// reimplementation) plus structural guards on the init/resync wiring.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const coreSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'playback_core.js'), 'utf8');
const runtimeSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming_runtime.js'), 'utf8');
const src = coreSource;

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const paren = source.indexOf('(', match.index);
    let pdepth = 0, quote = '', escaped = false, i = paren;
    for (; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '(') pdepth += 1;
        else if (c === ')' && --pdepth === 0) break;
    }
    const brace = source.indexOf('{', i);
    let depth = 0;
    quote = ''; escaped = false;
    for (let j = brace; j < source.length; j += 1) {
        const c = source[j];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, j + 1);
    }
    throw new Error(`unterminated ${name}`);
}

// --- structural guards: qobuz must travel the same init/resync paths ------
// Provider runtime lives in streaming_runtime.js; ownership lives in
// playback_core.js.
assert.ok(/function\s+fetchQobuzStatus\s*\(/.test(runtimeSource), 'streaming_runtime.js must define fetchQobuzStatus');
assert.ok(/function\s+handleIncomingQobuzState\s*\(/.test(runtimeSource), 'streaming_runtime.js must define handleIncomingQobuzState');
assert.ok(/function\s+shouldPollQobuz\s*\(/.test(runtimeSource), 'streaming_runtime.js must define shouldPollQobuz');
assert.ok(/function\s+startQobuzPoll\s*\(/.test(runtimeSource), 'streaming_runtime.js must define startQobuzPoll');
assert.ok(/if\s*\(data\.qobuz\)/.test(appSource), 'WS init must handle data.qobuz');
assert.ok(/fetchQobuzStatus\(\)/.test(appSource + coreSource + runtimeSource), 'resync/tab paths must fetch Qobuz status');

const CORE_FNAMES = [
    'getBackendFooterOwner',
    'setFooterSource',
    'spotifyPlayingOwnsFooter',
    'spotifyPausedHasFooterContext',
    'qobuzPlayingOwnsFooter',
    'localPlaybackHasFooterContext',
    'localEndedPlaybackHasFooterContext',
    'localFooterHoldHasContext',
    'activeLocalPlaybackBlocksSpotifyOwnership',
    'footerSingleTrackStartLockActive',
    'footerContentFreezeActive',
    'reconcileFooterSource',
    'syncFooterOwnershipFromPlayback',
];
// handleIncomingQobuzState + shouldPollQobuz + qobuzIsInstalled live in the
// streaming runtime module; they reach ownership through the PlaybackCore
// namespace.
const APP_FNAMES = [];
const RT_FNAMES = ['handleIncomingQobuzState', 'shouldPollQobuz', 'qobuzIsInstalled'];
const fns = CORE_FNAMES.map((n) => extractFunction(coreSource, n)).join('\n')
    + '\n' + RT_FNAMES.map((n) => extractFunction(runtimeSource, n)).join('\n');

function makeSandbox({ footerSource = 'local', visibleTab = 'radio', owner = null, qobuz = null, spotify = null } = {}) {
    const rendered = [];
    const sandbox = {
        window: {
            __footerSource: footerSource,
            __spotifyLastData: spotify,
            __qobuzLastData: qobuz,
            __visibleTab: visibleTab,
            __streamingSeeking: false,
        },
        state: {
            playback: {
                playback_owner: owner,
                current_track: null,
                playing: false,
                paused: false,
                ended: false,
            },
            // Source-mode ownership pin: app-playback here keeps every
            // existing owner case on its established path.
            settings: { sourceMode: { mode: 'app-playback', pending: false } },
        },
        document: { hidden: false },
        _spotifyTakeoverUntil: 0,
        _localFooterHoldUntil: 0,
        _footerContentFreezeUntil: 0,
        pendingFooterSingleTrackStart: null,
        footerDebug: () => {},
        // handleIncomingQobuzState forwards every snapshot to the shared cue
        // decision; the footer-resync contract under test is independent of it.
        maybeShowStreamingQueueCue: () => false,
        stopPlaybackPositionPoll: () => {},
        startSpotifyPoll: () => {},
        shouldPollSpotify: () => false,
        updateFooterForStreamingOwner: (data) => { rendered.push(data); },
        // Core-owned state reads through deps; the staying provider runtime
        // reaches ownership through these namespaces (mirrors app.js wiring).
        deps: {
            getState: () => sandbox.state,
            nonAppSourceModeActive: () => false,
            stopSpotifyPoll: () => {},
            bumpSpotifyPollGeneration: () => {},
            reconcileFooterSource: (...args) => sandbox.PlaybackCore.reconcileFooterSource(...args),
            getBackendFooterOwner: (...args) => sandbox.PlaybackCore.getBackendFooterOwner(...args),
            updateFooterForStreamingOwner: (data) => { rendered.push(data); },
            maybeShowStreamingQueueCue: () => false,
        },
        console,
    };
    // Namespace shims for the staying provider runtime under test: ownership
    // forwards to the extracted core functions, rendering to the test spy.
    sandbox.PlaybackCore = {
        reconcileFooterSource: (...args) => sandbox.reconcileFooterSource(...args),
        getBackendFooterOwner: (...args) => sandbox.getBackendFooterOwner(...args),
    };
    sandbox.PlaybackUI = {
        updateFooterForStreamingOwner: (data) => { rendered.push(data); },
        maybeShowStreamingQueueCue: () => false,
    };
    sandbox.rendered = rendered;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(fns, sandbox);
    return { sandbox, rendered: () => rendered };
}

let failures = 0;
function check(name, fn) {
    try {
        fn();
        console.log(`ok  ${name}`);
    } catch (e) {
        failures += 1;
        console.log(`FAIL  ${name}: ${e.message}`);
    }
}

// 1. Incoming Qobuz Playing claims an empty footer (the init gap shape).
check('qobuz playing into null cache reaches qobuz footer', () => {
    const { sandbox, rendered } = makeSandbox({ owner: 'qobuz' });
    vm.runInContext(
        `handleIncomingQobuzState({ available: true, installed: true, status: 'Playing', title: 'Made', artist: 'Dub FX' }, { renderFooter: true });`,
        sandbox,
    );
    assert.equal(sandbox.window.__footerSource, 'qobuz');
    assert.equal(sandbox.window.__qobuzLastData.title, 'Made');
    assert.equal(rendered().length, 1);
});

// 2. Stale paused Qobuz data is replaced by the fresh snapshot, not merged away.
check('fresh qobuz snapshot replaces stale paused cache', () => {
    const { sandbox } = makeSandbox({
        owner: 'qobuz',
        footerSource: 'qobuz',
        qobuz: { available: true, installed: true, status: 'Paused', title: 'Old', artist: 'Old' },
    });
    vm.runInContext(
        `handleIncomingQobuzState({ available: true, installed: true, status: 'Playing', title: 'Made', artist: 'Dub FX' }, { renderFooter: true });`,
        sandbox,
    );
    assert.equal(sandbox.window.__qobuzLastData.status, 'Playing');
    assert.equal(sandbox.window.__qobuzLastData.title, 'Made');
});

// 4. Volume-domain guard: raw provider snapshots must not slam the master.
check('raw qobuz snapshot without source_volume keeps master volume', () => {
    const { sandbox, rendered } = makeSandbox({ owner: 'qobuz', footerSource: 'qobuz' });
    vm.runInContext(
        `handleIncomingQobuzState({ available: true, installed: true, status: 'Playing', title: 'Made', artist: 'Dub FX', volume: 100 }, { renderFooter: true });`,
        sandbox,
    );
    // Display state is adopted (Not-Playing repair still works) ...
    assert.equal(sandbox.window.__qobuzLastData.title, 'Made');
    assert.equal(sandbox.window.__footerSource, 'qobuz');
    // ... but the raw engine volume never reaches the footer renderer.
    assert.equal(rendered().length, 1);
    assert.ok(!('volume' in rendered()[0]), 'raw volume must be stripped before render');
    assert.ok(!('volume' in sandbox.window.__qobuzLastData), 'raw volume must not be cached');
});
check('normalized qobuz snapshot keeps master volume', () => {
    const { sandbox, rendered } = makeSandbox({ owner: 'qobuz', footerSource: 'qobuz' });
    vm.runInContext(
        `handleIncomingQobuzState({ available: true, installed: true, status: 'Playing', title: 'Made', artist: 'Dub FX', volume: 25, source_volume: 100 }, { renderFooter: true });`,
        sandbox,
    );
    assert.equal(rendered().length, 1);
    assert.equal(rendered()[0].volume, 25);
});

// 3. Poll gates: footer resync runs exactly when the footer can need Qobuz.
check('shouldPollQobuz gates', () => {
    const poll = (opts) => {
        const { sandbox } = makeSandbox(opts);
        return vm.runInContext('shouldPollQobuz()', sandbox);
    };
    const installed = { available: true, installed: true, status: 'Paused', title: 'Made' };
    assert.equal(poll({ qobuz: installed, visibleTab: 'qobuz' }), true);
    assert.equal(poll({ qobuz: installed, footerSource: 'qobuz' }), true);
    // Repair path: backend commits qobuz while the footer still points local.
    assert.equal(poll({ qobuz: installed, owner: 'qobuz' }), true);
    assert.equal(poll({ qobuz: null }), false);
    assert.equal(poll({ qobuz: { available: true, installed: false } }), false);
    assert.equal(poll({ qobuz: installed }), false);
});

if (failures) {
    console.error(`${failures} case(s) failed`);
    process.exit(1);
}
console.log('footer qobuz resync: ok');
