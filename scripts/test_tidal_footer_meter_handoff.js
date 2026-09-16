#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// TIDAL footer/meter handoff: live TIDAL playback must own the shared footer
// (and with it the VU meter / peak detector gating), never a stale Spotify
// entry.
//
// Regression for the .104 observation: TIDAL playing while the footer still
// showed a paused Spotify track, so VU meter and peak detector stayed hidden
// even though /api/status already streamed fresh peak values (vu_fresh=true).
// Two gaps combined: playTidalTracks never committed the /api/play payload
// to the footer state (relying on the WS playback frame alone), and the
// shared footer-context predicates only knew local/radio, so TIDAL fell
// through to the stale Spotify paused context.
//
// This executes the VERBATIM decision functions extracted from
// static/app.js (no reimplementation) with live-captured .104 input shapes,
// plus structural guards that the TIDAL start rides the shared native commit
// path instead of a provider-specific footer.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const streamingJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');

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
// The TIDAL start must commit through the shared native response path ...
assert.ok(appJs.includes('function applyNativePlayResponse('), 'app.js must define applyNativePlayResponse');
const commitSrc = extractFunction(appJs, 'applyNativePlayResponse');
assert.ok(commitSrc.includes('mergePlaybackState('), 'shared commit must merge the authoritative payload');
assert.ok(commitSrc.includes('syncFooterOwnershipFromPlayback('), 'shared commit must re-resolve footer ownership');
assert.ok(!/tidal/i.test(commitSrc), 'shared commit must not branch on any provider identity');
assert.ok(streamingJs.includes('applyNativePlayResponse(data)'), 'playTidalTracks must commit through the shared native path');
// All three native starts commit through the same helper; no caller keeps an
// inline merge+UI block that could drift from the shared commit path.
const playRadioSrc = extractFunction(appJs, 'playRadio');
const playLocalSrc = extractFunction(appJs, 'playLocal');
assert.ok(playRadioSrc.includes('applyNativePlayResponse(data)'), 'playRadio must commit through the shared native path');
assert.ok(playLocalSrc.includes('applyNativePlayResponse(data)'), 'playLocal must commit through the shared native path');
assert.ok(!playRadioSrc.includes('mergePlaybackState('), 'playRadio must not keep an inline merge block');
assert.ok(!playLocalSrc.includes('mergePlaybackState('), 'playLocal must not keep an inline merge block');
// Library reaction and the track cue are provider-specific: they must stay in
// playLocal and run after the shared commit (not inside the helper).
assert.ok(
    playLocalSrc.indexOf('applyNativePlayResponse(data)') < playLocalSrc.indexOf('syncLibraryStateFromPlaybackContext(true)'),
    'playLocal must run the library sync after the shared commit',
);
assert.ok(playLocalSrc.includes('maybeShowNativeTrackCue('), 'playLocal must keep its queue-started cue');
// ... and the peak poll must heal a stale owner when a broadcast was missed.
assert.ok(
    /mergePlaybackState\(\{\s*current_track:\s*data\.current_track,[^}]*playback_owner:\s*data\.playback_owner/.test(appJs),
    'fetchMetadata must merge playback_owner so the peak poll heals a stale footer owner',
);

const FNAMES = [
    'isStreamingFooterSource',
    'getBackendFooterOwner',
    'getEffectivePlaybackControlSource',
    'setFooterSource',
    'nonAppSourceModeActive',
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
const fns = FNAMES.map((n) => extractFunction(appJs, n)).join('\n');

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
            // Source-mode ownership pin: app-playback here keeps every
            // existing owner case on its established path.
            settings: { sourceMode: { mode: 'app-playback', pending: false } },
        },
        _spotifyTakeoverUntil: 0,
        _spotifyPollGeneration: 0,
        _localFooterHoldUntil: 0,
        pendingFooterSingleTrackStart: null,
        footerDebug: () => {},
        stopPlaybackPositionPoll: () => {},
        startSpotifyPoll: () => {},
        stopSpotifyPoll: () => {},
        startQobuzPoll: () => {},
        stopQobuzPoll: () => {},
        shouldPollSpotify: () => false,
        shouldPollQobuz: () => false,
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

// Live-captured .104 shapes (backend truth at capture: owner=tidal, TIDAL
// Playing, Spotify/Qobuz Paused, peak vu_fresh=true).
const SPOTIFY_PAUSED_STALE = { available: true, status: 'Paused', title: 'My Game', artist: 'Deluxe' };
const SPOTIFY_PLAYING_STALE = { available: true, status: 'Playing', title: 'My Game', artist: 'Deluxe' };
const SPOTIFY_IDLE = { available: true, status: 'Stopped', title: '', artist: '' };
const QOBUZ_IDLE = { available: false, status: 'Stopped' };
const TIDAL_TRACK = { source: 'tidal', id: '175015094', title: 'Respect (2005 Remaster)', url: '/home/paul/.cache/fxroute/tidal/tidal-052c2f7bd01c.mp4' };
const LOCAL_TRACK = { source: 'local', id: 'track-1', title: 'Local Song', url: '/music/a.flac' };
const RADIO_TRACK = { id: 'radio_fip-hiphop', title: 'FIP Hip-Hop', artist: 'Radio', source: 'radio', url: 'https://icecast.radiofrance.fr/fiphiphop-midfi.mp3' };

const cases = [
    // Reported bug shapes: live TIDAL playback must own the footer, never a
    // stale Spotify entry (VU/peak gating derives from the footer owner).
    { name: 'reconcile: null cache + tidal playing + spotify paused -> local', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, playback: { current_track: TIDAL_TRACK, playing: true }, want: 'local' },
    { name: 'sync: committed tidal + tidal playing + spotify paused -> local', entry: 'sync', ownerCache: 'tidal', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, playback: { current_track: TIDAL_TRACK, playing: true }, want: 'local' },
    { name: 'control: committed tidal routes local transport', entry: 'control', ownerCache: 'tidal', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, playback: { current_track: TIDAL_TRACK, playing: true }, want: 'local' },
    // A stale Spotify Playing edge must not steal the footer from live TIDAL
    // once the backend commit is gone (missed broadcast, reboot mid-play).
    { name: 'reconcile: null cache + tidal playing + spotify playing -> local', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PLAYING_STALE, qobuz: QOBUZ_IDLE, playback: { current_track: TIDAL_TRACK, playing: true }, want: 'local' },
    // Ended TIDAL keeps its footer context like local instead of falling back
    // to a stale Spotify paused entry.
    { name: 'reconcile: null cache + tidal ended + spotify paused -> local', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, playback: { current_track: TIDAL_TRACK, ended: true }, want: 'local' },
    // Healthy states keep working (no behavior change).
    { name: 'reconcile: committed local playing + spotify paused -> local', entry: 'reconcile', ownerCache: 'local', spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, playback: { current_track: LOCAL_TRACK, playing: true }, want: 'local' },
    { name: 'reconcile: committed spotify + spotify playing -> spotify', entry: 'reconcile', ownerCache: 'spotify', spotify: SPOTIFY_PLAYING_STALE, qobuz: QOBUZ_IDLE, want: 'spotify' },
    { name: 'reconcile: null cache + idle natives + spotify paused stays spotify', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_PAUSED_STALE, qobuz: QOBUZ_IDLE, want: 'spotify' },
    { name: 'reconcile: null cache + all idle falls back local', entry: 'reconcile', ownerCache: null, spotify: SPOTIFY_IDLE, qobuz: QOBUZ_IDLE, want: 'local' },
];

let failures = 0;
for (const c of cases) {
    const got = runCase(c);
    const ok = got === c.want;
    if (!ok) failures += 1;
    console.log(`${ok ? 'ok' : 'FAIL'}  ${c.name}: footer=${got} (want ${c.want})`);
}

// --- radio /api/play response commit -----------------------------------------
// playRadio commits through applyNativePlayResponse. Run the VERBATIM helper
// and merge against a stale cached Spotify owner (the missed-broadcast class
// the shared path must heal) and assert the same postconditions playRadio's
// old inline block guaranteed: payload merged, Spotify Playing edge demoted,
// poll demoted when Spotify must not be polled, UI refreshed once.
const RADIO_PLAY_RESPONSE = {
    status: 'playing',
    playback: {
        _seq: 3701,
        playback_owner: 'radio',
        current_track: RADIO_TRACK,
        playing: true,
        paused: false,
        ended: false,
    },
};

function runRadioResponseCase({ spotify, ownerCache }) {
    const sandbox = {
        window: {
            __footerSource: 'spotify',
            __spotifyLastData: spotify,
            __qobuzLastData: QOBUZ_IDLE,
            __visibleTab: 'radio',
        },
        state: {
            playback: {
                playback_owner: ownerCache,
                current_track: null,
                playing: false,
                paused: false,
                ended: false,
            },
        },
        _spotifyTakeoverUntil: 12345, // stale takeover arm from the old owner
        _spotifyPollGeneration: 0,
        _localFooterHoldUntil: 0,
        pendingFooterSingleTrackStart: null,
        lastRadioTrack: null,
        footerDebug: () => {},
        stopPlaybackPositionPoll: () => {},
        startSpotifyPoll: () => {},
        stopSpotifyPoll: () => { sandbox.stopSpotifyPollCalls += 1; },
        stopSpotifyPollCalls: 0,
        startQobuzPoll: () => {},
        stopQobuzPoll: () => {},
        shouldPollSpotify: () => false,
        shouldPollQobuz: () => false,
        applyRemoteVolume: () => {},
        updatePlaybackUI: () => { sandbox.updatePlaybackUICalls += 1; },
        updatePlaybackUICalls: 0,
        console,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(
        `${fns}\n${extractFunction(appJs, 'mergePlaybackState')}\n`
        + `${extractFunction(appJs, 'rememberLastRadioTrack')}\n${extractFunction(appJs, 'applyNativePlayResponse')}\n`
        + 'this.__run = () => { applyNativePlayResponse(' + JSON.stringify(RADIO_PLAY_RESPONSE) + '); return state.playback; };',
        sandbox,
    );
    vm.runInContext('__run()', sandbox);
    return sandbox;
}

// Stale Spotify Playing owns the footer before the commit; the response owner
// is 'radio' (as /api/play answered on .104). The commit must heal the footer.
{
    const s = runRadioResponseCase({ spotify: SPOTIFY_PLAYING_STALE, ownerCache: 'spotify' });
    const pb = vm.runInContext('state.playback', s);
    const checks = [
        ['owner merged', pb.playback_owner === 'radio'],
        ['track merged', pb.current_track?.id === 'radio_fip-hiphop'],
        ['takeover reset', s._spotifyTakeoverUntil === 0],
        ['stale Playing edge demoted', s.window.__spotifyLastData.status === 'Paused'],
        // The sync's local-owner branch and the helper tail both demote the
        // poll (the production UI refresh would add more); the invariant is
        // that it IS demoted, not how many sites did it.
        ['spotify poll demoted', s.stopSpotifyPollCalls >= 1 && s._spotifyPollGeneration >= 1],
        ['ui refreshed once', s.updatePlaybackUICalls === 1],
        ['radio track remembered', s.lastRadioTrack?.id === 'radio_fip-hiphop'],
    ];
    const bad = checks.filter(([, ok]) => !ok);
    if (bad.length) failures += 1;
    console.log(`${bad.length ? 'FAIL' : 'ok'}  radio response commit heals stale spotify owner${bad.length ? ': ' + bad.map(([n]) => n).join(', ') : ''}`);
}
// Idle Spotify must not be rewritten by the commit (no fake Paused state).
{
    const s = runRadioResponseCase({ spotify: SPOTIFY_IDLE, ownerCache: null });
    const pb = vm.runInContext('state.playback', s);
    const checks = [
        ['owner merged', pb.playback_owner === 'radio'],
        ['idle spotify untouched', s.window.__spotifyLastData.status === 'Stopped'],
        ['ui refreshed once', s.updatePlaybackUICalls === 1],
    ];
    const bad = checks.filter(([, ok]) => !ok);
    if (bad.length) failures += 1;
    console.log(`${bad.length ? 'FAIL' : 'ok'}  radio response commit leaves idle spotify untouched${bad.length ? ': ' + bad.map(([n]) => n).join(', ') : ''}`);
}

if (failures) {
    console.error(`${failures} case(s) failed`);
    process.exit(1);
}
console.log('tidal footer/meter handoff: ok');
