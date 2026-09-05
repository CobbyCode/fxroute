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

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

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
assert.ok(/function\s+fetchQobuzStatus\s*\(/.test(src), 'app.js must define fetchQobuzStatus');
assert.ok(/function\s+handleIncomingQobuzState\s*\(/.test(src), 'app.js must define handleIncomingQobuzState');
assert.ok(/function\s+shouldPollQobuz\s*\(/.test(src), 'app.js must define shouldPollQobuz');
assert.ok(/function\s+startQobuzPoll\s*\(/.test(src), 'app.js must define startQobuzPoll');
assert.ok(/if\s*\(data\.qobuz\)/.test(src), 'WS init must handle data.qobuz');
assert.ok(/fetchQobuzStatus\(\)/.test(src), 'resync/tab paths must fetch Qobuz status');

const FNAMES = [
    'getBackendFooterOwner',
    'setFooterSource',
    'spotifyPlayingOwnsFooter',
    'spotifyPausedHasFooterContext',
    'qobuzPlayingOwnsFooter',
    'qobuzIsInstalled',
    'shouldPollQobuz',
    'localPlaybackHasFooterContext',
    'localEndedPlaybackHasFooterContext',
    'localFooterHoldHasContext',
    'activeLocalPlaybackBlocksSpotifyOwnership',
    'footerSingleTrackStartLockActive',
    'footerContentFreezeActive',
    'reconcileFooterSource',
    'syncFooterOwnershipFromPlayback',
    'handleIncomingQobuzState',
];
const fns = FNAMES.map((n) => extractFunction(src, n)).join('\n');

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
        },
        document: { hidden: false },
        _spotifyTakeoverUntil: 0,
        _localFooterHoldUntil: 0,
        _footerContentFreezeUntil: 0,
        pendingFooterSingleTrackStart: null,
        footerDebug: () => {},
        stopPlaybackPositionPoll: () => {},
        startSpotifyPoll: () => {},
        shouldPollSpotify: () => false,
        updateFooterForStreamingOwner: (data) => { rendered.push(data); },
        console,
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
