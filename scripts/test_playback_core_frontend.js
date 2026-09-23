#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the playback core module: native transport,
// footer ownership/reconcile (single owner), volume, polling/metadata and
// queue commit paths live in static/playback_core.js. Rendering and gestures
// live in static/playback_ui.js; the provider runtime stays in app.js behind
// injected callbacks, as do the shared poll/takeover generations.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const Core = require('../static/playback_core.js');

const MOVED = [
    'isStreamingFooterSource', 'streamingFooterData', 'getBackendFooterOwner',
    'getEffectivePlaybackControlSource', 'globalTogglePlayback', 'globalPrevious', 'globalNext',
    'globalSeekChange', 'globalSeekEnd', 'stopPlayback', 'togglePlayback', 'clampVolumeValue',
    'sliderVolumeToActualVolume', 'actualVolumeToSliderValue', 'renderVolumeControlsFromActualVolume',
    'setLocalVolume', 'queueVolumeSend', 'mergePlaybackState', 'rememberLastRadioTrack', 'getLastRadioTrack',
    'buildOptimisticSingleTrackQueue', 'footerSingleTrackStartLockActive',
    'activeLocalPlaybackBlocksSpotifyOwnership', 'footerSingleTrackStartLockSatisfied',
    'clearPendingFooterSingleTrackStart', 'clearPendingOptimisticTrack', 'getLibraryPlaybackContext',
    'syncLibraryStateFromPlaybackContext', 'applyRemoteVolume', 'sendVolume', 'handleVolumeChange',
    'isPageHidden', 'startMetadataPolling', 'stopMetadataPolling', 'startPeakStatusPolling',
    'stopPeakStatusPolling', 'startSampleratePolling', 'stopSampleratePolling',
    'triggerSamplerateBurstPolling', 'fetchMetadata', 'fetchInitialData', 'fetchPlaybackStatus',
    'fetchSamplerateStatus', 'previousInQueue', 'nextInQueue', 'clearQueue', 'playRadio', 'playLocal',
    'applyNativePlayResponse', 'footerContentFreezeActive', 'armFooterContentFreeze', 'footerDebug',
    'setFooterSource', 'localPlaybackHasFooterContext', 'localEndedPlaybackHasFooterContext',
    'spotifyPlayingOwnsFooter', 'spotifyPausedHasFooterContext', 'qobuzPlayingOwnsFooter',
    'localFooterHoldHasContext', 'reconcileFooterSource', 'spotifyIsInstalled', 'shouldPollSpotify',
    'syncFooterOwnershipFromPlayback', 'startPlaybackPositionPoll', 'stopPlaybackPositionPoll',
    '_isSpotifyActive', 'playCoverQueueIndex', 'doSeek', 'resyncPlaybackAfterReconnect',
    'armSpotifyTakeover', 'armLocalFooterHold',
];

// Module loads before the app shell and owns every moved function.
assert.ok(
    indexSource.indexOf('playback_core.js?v=') < indexSource.indexOf('app.js?v='),
    'playback_core.js must load before app.js',
);
assert.match(indexSource, /playback_core\.js\?v=\d+\.\d+\.\d+/);
for (const name of MOVED) {
    assert.equal(typeof Core[name], 'function', `module must export ${name}`);
    assert.doesNotMatch(
        appSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no app.js duplicate: ${name} lives in playback_core.js`,
    );
    assert.doesNotMatch(
        appSource,
        new RegExp(`(?<![\\w.$'\`"])${name}\\s*\\(`),
        `no bare app.js call: ${name} goes through PlaybackCore.`,
    );
}
// Ownership decisions live in exactly one module: the UI module must not
// define them, and app.js must route provider/WS paths through the core.
const uiSource = fs.readFileSync(path.join(repoRoot, 'static', 'playback_ui.js'), 'utf8');
for (const name of ['reconcileFooterSource', 'syncFooterOwnershipFromPlayback', 'getBackendFooterOwner',
    'spotifyPlayingOwnsFooter', 'qobuzPlayingOwnsFooter', 'setFooterSource']) {
    assert.doesNotMatch(
        uiSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no playback_ui.js duplicate: ${name} is core-owned`,
    );
}
assert.doesNotMatch(uiSource, /window\.__footerSource\s*=(?![=])/, 'playback_ui.js must never assign the footer source');
// Module-only state: moved declarations left app.js, shared flags stayed.
for (const decl of [
    'let playbackActionInFlight', 'let pendingPlaybackRequestId',
    'let pendingFooterSingleTrackStart', 'let pendingOptimisticTrack',
    'let pauseActionRequestId', 'const FOOTER_SINGLE_TRACK_START_LOCK_MS',
    'let volumeTimer', 'let volumeRequestInFlight', 'let pendingVolume',
    'let volumeGestureActive', 'let optimisticVolume', 'let lastConfirmedVolume',
    'let volumeSyncGraceUntil', 'const VOLUME_SEND_DEBOUNCE_MS', 'const VOLUME_SYNC_GRACE_MS',
    'const VOLUME_CURVE_GAMMA', 'let libraryModeSyncArmed',
    'let lastLibraryPlaybackContextSignature', 'let metadataPollTimer',
    'let sampleratePollTimer', 'let samplerateBurstPollTimers', 'let peakStatusPollTimer',
    'const SAMPLERATE_POLL_INTERVAL_MS', 'const SAMPLERATE_BURST_POLL_DELAYS_MS',
    'const PEAK_STATUS_POLL_INTERVAL_MS', 'let playbackPositionPollTimer',
    'let _footerContentFreezeUntil', 'let _footerContentFreezeTimer',
    'let _spotifyTakeoverUntil', 'let _localFooterHoldUntil',
]) {
    assert.ok(
        new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm').test(
            fs.readFileSync(path.join(repoRoot, 'static', 'playback_core.js'), 'utf8'),
        ),
        `core module must own: ${decl}`,
    );
    assert.doesNotMatch(appSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `app.js must not keep: ${decl}`);
}
for (const decl of [
    'let tidalFavoriteRequestInFlight = false;',
    'let libraryModeRequestInFlight = false;',
    'let lastSampleratePlaybackSignature = null;',
]) {
    assert.match(appSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `shared flag stays in app.js: ${decl}`);
}
// Poll generations moved with the streaming runtime (single polling owner).
const rtSource = fs.readFileSync(path.join(repoRoot, 'static', 'streaming_runtime.js'), 'utf8');
for (const decl of [
    'let _spotifyPollGeneration = 0;',
    'let _qobuzPollGeneration = 0;',
]) {
    assert.match(rtSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `poll generation lives in streaming_runtime.js: ${decl}`);
    assert.doesNotMatch(appSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `app.js must not keep: ${decl}`);
}
// app.js wiring reaches the module through the PlaybackCore alias.
for (const snippet of [
    'const PlaybackCore = window.FXRoutePlaybackCore || {};',
    'window.FXRoutePlaybackCore?.init({',
    'PlaybackCore.mergePlaybackState(',
    'PlaybackCore.playRadio(',
    'PlaybackCore.reconcileFooterSource(',
    'PlaybackCore.syncFooterOwnershipFromPlayback(',
    'PlaybackCore.setPlaybackActionInFlight(false)',
    'getState: () => state,',
    'shouldPollQobuz: (...args) => StreamingRuntime.shouldPollQobuz(...args),',
]) {
    assert.ok(appSource.includes(snippet), `app.js wiring must reference ${snippet}`);
}
const coreSource = fs.readFileSync(path.join(repoRoot, 'static', 'playback_core.js'), 'utf8');
assert.match(coreSource, /deps\.getState\(\)/, 'state goes through injected getter');
assert.match(coreSource, /deps\.getElements\(\)/, 'elements go through injected getter');
assert.doesNotMatch(coreSource, /(?<![\w.$])state\./, 'no raw app state access');
assert.doesNotMatch(coreSource, /(?<![\w.$])elements\./, 'no raw app elements access');

// Pure helper contracts stay intact.
assert.equal(Core.clampVolumeValue(150), 100);
assert.equal(Core.clampVolumeValue(-5), 0);
assert.equal(Core.sliderVolumeToActualVolume(50), 50);
assert.equal(Core.actualVolumeToSliderValue(50), 50);
assert.equal(Core.isStreamingFooterSource('spotify'), true);
assert.equal(Core.isStreamingFooterSource('qobuz'), true);
assert.equal(Core.isStreamingFooterSource('local'), false);
assert.equal(Core.spotifyPausedHasFooterContext({ available: true, status: 'Paused' }), true);
assert.equal(Core.spotifyPausedHasFooterContext({ available: true, status: 'Playing' }), false);

console.log('PASS  scripts/test_playback_core_frontend.js (playback core module owns transport/ownership/volume)');
