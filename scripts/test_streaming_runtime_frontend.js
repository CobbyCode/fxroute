#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the streaming runtime module: Spotify/Qobuz
// status fetch, state merge, incoming-state processing, transport commands,
// polling with poll generations and the provider-side footer/ownership sync
// live in static/streaming_runtime.js. Footer ownership stays exclusively in
// static/playback_core.js (this module never assigns window.__footerSource),
// rendering stays in static/playback_ui.js, and the provider tabs/browse UI
// in static/streaming.js is a separate layer reached only via callbacks.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const Runtime = require('../static/streaming_runtime.js');

const MOVED = [
    'fetchSpotifyStatus', 'fetchQobuzStatus', 'spotifyTrackKey', 'mergeSpotifyState',
    'syncSpotifySourceOwnership', 'shouldAdoptSpotifyUpdate', 'syncSpotifyTabAvailability',
    'handleIncomingQobuzState', 'handleIncomingSpotifyState', 'renderSpotify',
    'updateGlobalControlsForSource', 'forceSpotifyRefreshBurst', 'qobuzCommand', 'qobuzSeek',
    'spotifyCommand', 'spotifySeek', 'qobuzIsInstalled', 'shouldPollQobuz', 'stopQobuzPoll',
    'startQobuzPoll', 'stopSpotifyPoll', 'startSpotifyPoll', 'renderSpotifyTab', 'initSpotify',
];

// Module loads before the app shell and owns every moved function.
assert.ok(
    indexSource.indexOf('streaming_runtime.js?v=') < indexSource.indexOf('app.js?v='),
    'streaming_runtime.js must load before app.js',
);
assert.match(indexSource, /streaming_runtime\.js\?v=\d+\.\d+\.\d+/);
for (const name of MOVED) {
    assert.equal(typeof Runtime[name], 'function', `module must export ${name}`);
    assert.doesNotMatch(
        appSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no app.js duplicate: ${name} lives in streaming_runtime.js`,
    );
    assert.doesNotMatch(
        appSource,
        new RegExp(`(?<![\\w.$'\`"])${name}\\s*\\(`),
        `no bare app.js call: ${name} goes through StreamingRuntime.`,
    );
}
// Ownership stays in the core: the runtime must never assign the footer
// source, and the ownership predicates must not move here.
const rtSource = fs.readFileSync(path.join(repoRoot, 'static', 'streaming_runtime.js'), 'utf8');
assert.doesNotMatch(rtSource, /window\.__footerSource\s*=(?![=])/, 'streaming_runtime.js must never assign the footer source');
for (const name of ['reconcileFooterSource', 'syncFooterOwnershipFromPlayback', 'getBackendFooterOwner',
    'setFooterSource', 'updatePlaybackUI', 'updateFooterForStreamingOwner']) {
    assert.doesNotMatch(
        rtSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no streaming_runtime.js duplicate: ${name} stays in its owner module`,
    );
}
// Module-only state: poll timers and generations left app.js.
for (const decl of [
    'let _spotifyInstalledKnown', 'let _spotifyPollTimer', 'let _spotifyCommandInFlight',
    'let _spotifySeekCommitTimer', 'let _spotifyLastRenderedTrackKey', 'let _spotifyLastPositionUpdateAt',
    'let _qobuzPollTimer', 'let _qobuzPollGeneration', 'let _qobuzPollTimerGeneration',
    'let _spotifyPollGeneration', 'let _spotifyPollTimerGeneration',
]) {
    assert.ok(
        new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm').test(rtSource),
        `runtime module must own: ${decl}`,
    );
    assert.doesNotMatch(appSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `app.js must not keep: ${decl}`);
}
// app.js wiring reaches the module through the StreamingRuntime alias.
for (const snippet of [
    'const StreamingRuntime = window.FXRouteStreamingRuntime || {};',
    'window.FXRouteStreamingRuntime?.init({',
    'StreamingRuntime.handleIncomingSpotifyState(',
    'StreamingRuntime.handleIncomingQobuzState(',
    'StreamingRuntime.startSpotifyPoll(',
    'StreamingRuntime.stopSpotifyPoll(',
    'StreamingRuntime.startQobuzPoll(',
    'StreamingRuntime.spotifyCommand(',
    'StreamingRuntime.qobuzCommand(',
    'StreamingRuntime.bumpSpotifyPollGeneration()',
    'StreamingRuntime.bumpQobuzPollGeneration()',
    'apiPostJson: (...args) => apiPostJson(...args),',
]) {
    assert.ok(appSource.includes(snippet), `app.js wiring must reference ${snippet}`);
}
assert.match(rtSource, /deps\.fetchFn\(/, 'fetch goes through injected getter');
assert.doesNotMatch(rtSource, /(?<![\w.$])state\./, 'no raw app state access');
assert.doesNotMatch(rtSource, /(?<![\w.$])elements\./, 'no raw app elements access');

// Pure helper contracts stay intact.
global.window = global.window || {};
assert.equal(Runtime.spotifyTrackKey({ trackId: 'a', title: 'T', duration: 1 }), 'a|T||||1000');
assert.equal(Runtime.qobuzIsInstalled({ installed: true }), true);
assert.equal(Runtime.qobuzIsInstalled({}), false);
assert.equal(Runtime.mergeSpotifyState({ title: 'T' }).title, 'T');

console.log('PASS  scripts/test_streaming_runtime_frontend.js (streaming runtime module owns provider fetch/commands/polling)');
