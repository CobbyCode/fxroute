#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Shared footer ownership: the footer must follow the actually active
// playback owner, never a stale cached commit.
//
// Regression for the .104 observation: with qbzd Playing (Qobuz tile
// correct), the global footer showed a stale paused Spotify track. The
// ownership resolution had no live-qobuz branch at all, so a stale cached
// backend owner (or none) pinned the footer to Spotify while live qobuz
// playback could never reclaim it.
//
// This executes the VERBATIM decision functions extracted from
// static/app.js (no reimplementation) with live-captured .104 input shapes.

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

// --- structural guards ---------------------------------------------------------
// Live qobuz truth must participate in the shared ownership resolution ...
assert.ok(
    /function\s+qobuzPlayingOwnsFooter\s*\(/.test(src),
    'app.js must define qobuzPlayingOwnsFooter (live-qobuz ownership branch)',
);
const reconcileSrc = extractFunction(src, 'reconcileFooterSource');
assert.ok(
    reconcileSrc.includes('qobuzPlayingOwnsFooter'),
    'reconcileFooterSource must consult live qobuz state',
);
// ... without building a provider-specific parallel footer: the single
// streaming footer renderer stays provider-identity free. (Sliced by
// top-level function boundaries: the brace scanner cannot see regex
// literals inside the renderer body.)
const appLines = src.split('\n');
const rendererStart = appLines.findIndex((l) => l.startsWith('function updateFooterForStreamingOwner('));
assert.ok(rendererStart >= 0, 'missing updateFooterForStreamingOwner');
let rendererEnd = appLines.findIndex((l, i) => i > rendererStart && l.startsWith('function '));
assert.ok(rendererEnd > rendererStart, 'unterminated updateFooterForStreamingOwner');
const streamingFooterSrc = appLines.slice(rendererStart, rendererEnd).join('\n');
assert.ok(!/['"]qobuz['"]/.test(streamingFooterSrc), 'streaming footer renderer must not branch on qobuz');
assert.ok(!/['"]spotify['"]/.test(streamingFooterSrc), 'streaming footer renderer must not branch on spotify');

const FNAMES = [
    'getBackendFooterOwner',
    'getEffectivePlaybackControlSource',
    'setFooterSource',
    'spotifyPlayingOwnsFooter',
    'spotifyPausedHasFooterContext',
    'qobuzPlayingOwnsFooter',
    'localPlaybackHasFooterContext',
    'localEndedPlaybackHasFooterContext',
    'localFooterHoldHasContext',
    'activeLocalPlaybackBlocksSpotifyOwnership',
    'footerSingleTrackStartLockActive',
    'reconcileFooterSource',
    'syncFooterOwnershipFromPlayback',
];
const fns = FNAMES.map((n) => extractFunction(src, n)).join('\n');

function runCase({ ownerCache, spotify, qobuz, playback, entry }) {
    const sandbox = {
        window: {
            __footerSource: 'local',
            __spotifyLastData: spotify,
            __qobuzLastData: qobuz,
            __visibleTab: 'radio',
        },
        state: {
            playback: {
                playback_owner: ownerCache,
                current_track: null,
                playing: false,
                paused: false,
                ended: false,
                ...(playback || {}),
            },
        },
        _spotifyTakeoverUntil: 0,
        _localFooterHoldUntil: 0,
        pendingFooterSingleTrackStart: null,
        footerDebug: () => {},
        stopPlaybackPositionPoll: () => {},
        startSpotifyPoll: () => {},
        shouldPollSpotify: () => false,
        console,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        `${fns}\nthis.__run = () => {`
        + (entry === 'sync'
            ? `syncFooterOwnershipFromPlayback(state.playback); return window.__footerSource; };`
            : entry === 'control'
                ? `return getEffectivePlaybackControlSource(); };`
                : `reconcileFooterSource(); return window.__footerSource; };`),
        sandbox,
    );
    return vm.runInContext('__run()', sandbox);
}

// Live-captured .104 shapes (backend truth at capture: owner=qobuz).
const SPOTIFY_PAUSED_STALE = { available: true, status: 'Paused', title: 'Remmidemmi (Yippie Yippie Yeah)', artist: 'Deichkind' };
const SPOTIFY_PLAYING = { available: true, status: 'Playing', title: 'Remmidemmi (Yippie Yippie Yeah)', artist: 'Deichkind' };
const SPOTIFY_IDLE = { available: true, status: 'Stopped', title: '', artist: '' };
const QOBUZ_PLAYING = { available: true, status: 'Playing', title: 'Love Someone', artist: 'Dub FX' };
const QOBUZ_PAUSED = { available: true, status: 'Paused', title: 'Love Someone', artist: 'Dub FX' };
const QOBUZ_IDLE = { available: false, status: 'Stopped' };
const LOCAL_TRACK = { source: 'local', id: 'track-1', title: 'Local Song', url: '/music/a.flac' };

const cases = [
    // Reported bug shapes (via both entry points + transport routing).
    { name: 'reconcile: stale spotify cache + qobuz playing -> qobuz', entry: 'reconcile', ownerCache: 'spotify', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_PLAYING, want: 'qobuz' },
    { name: 'reconcile: null cache + qobuz playing -> qobuz', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_PLAYING, want: 'qobuz' },
    { name: 'sync: stale spotify cache + qobuz playing -> qobuz', entry: 'sync', ownerCache: 'spotify', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_PLAYING, want: 'qobuz' },
    { name: 'control: stale spotify cache + qobuz playing routes qobuz', entry: 'control', ownerCache: 'spotify', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_PLAYING, want: 'qobuz' },
    // Healthy states keep working (no behavior change).
    { name: 'reconcile: committed qobuz + qobuz playing -> qobuz', entry: 'reconcile', ownerCache: 'qobuz', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_PLAYING, want: 'qobuz' },
    { name: 'reconcile: committed spotify + spotify playing -> spotify', entry: 'reconcile', ownerCache: 'spotify', spotify: SPOTIFY_PLAYING, qobuz: QOBUZ_IDLE, want: 'spotify' },
    { name: 'reconcile: committed spotify paused, qobuz idle -> spotify', entry: 'reconcile', ownerCache: 'spotify', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, want: 'spotify' },
    { name: 'reconcile: null cache + spotify playing keeps priority', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PLAYING, qobuz: QOBUZ_PLAYING, want: 'spotify' },
    { name: 'reconcile: null cache + both live playing keeps commit priority', entry: 'reconcile', ownerCache: 'spotify', spotify: SPOTIFY_PLAYING, qobuz: QOBUZ_PLAYING, want: 'spotify' },
    // Committed local playback is never overridden by live external state.
    { name: 'reconcile: committed local playing + qobuz playing -> local', entry: 'reconcile', ownerCache: 'local', spotify: SPOTIFY_IDLE, qobuz: QOBUZ_PLAYING, playback: { current_track: LOCAL_TRACK, playing: true }, want: 'local' },
    { name: 'reconcile: null cache + local playing -> local', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_IDLE, qobuz: QOBUZ_IDLE, playback: { current_track: LOCAL_TRACK, playing: true }, want: 'local' },
    // Paused tie-break without a cache is unchanged (out of scope, documented).
    { name: 'reconcile: null cache + both paused stays spotify', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_PAUSED, want: 'spotify' },
];

let failures = 0;
for (const c of cases) {
    const got = runCase(c);
    const ok = got === c.want;
    if (!ok) failures += 1;
    console.log(`${ok ? 'ok' : 'FAIL'}  ${c.name}: footer=${got} (want ${c.want})`);
}
if (failures) {
    console.error(`${failures} case(s) failed`);
    process.exit(1);
}
console.log('footer owner resolution: ok');
