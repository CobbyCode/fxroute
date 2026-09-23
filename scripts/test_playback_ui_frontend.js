#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the playback UI module: footer layout/render,
// meter, queue UI, seek UI, cover detail, artwork, track cues, footer
// favorites and the streaming-owner footer renderer live in
// static/playback_ui.js. Ownership and transport resolve through
// static/playback_core.js behind explicit callbacks; the module never
// assigns the footer source itself.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const UI = require('../static/playback_ui.js');

const MOVED = [
    'syncPlaybackFooterSpace', 'schedulePlaybackFooterSpaceSync', 'initPlaybackFooterLayout',
    'setFooterProgressState', 'showVolumeDisplayTemporarily', 'setupPlaybackControls',
    'renderFooterModeButtons', 'toggleFooterShuffle', 'toggleFooterLoop', 'updatePlaybackUI',
    'updatePlayPauseButton', 'highlightActiveTrack', 'renderQueueUI', 'meterLitCount',
    'responsiveMeterSegmentCount', 'renderMeterChannel', 'isFiniteVuDb', 'rememberValidVu', 'heldVuSnapshot',
    'renderStereoMeter', 'formatOutputLevelBadgeDb', 'renderPeakWarningBadge', 'coverDetailSections',
    'isCoverDetailOpen', 'coverDetailMeta', 'coverDetailStreamingMeta', 'setCoverDetailText', 'coverDetailExtra',
    'coverQueuePlayTarget', 'renderCoverDetailCard', 'openCoverDetailCard', 'closeCoverDetailCard',
    'toggleCoverDetailCard', 'trackCoverUrl', 'trackCoverInfoUrl', 'trackCoverKnownAvailable',
    'playbackArtworkUrl', 'playbackArtworkKnownAvailable', 'streamingArtworkItem', 'updatePlaybackCover',
    'scheduleNowPlayingCueRemoval', 'revealNowPlayingCoverWhenReady', 'showNowPlayingCue',
    'streamingCueTrack', 'streamingCueTrackId', 'streamingCueKey', 'showStreamingQueueStarted',
    'lastPlayingQueueKey', 'recordPlayingQueueKey', 'maybeShowStreamingQueueCue', 'nativeTrackCueKey',
    'maybeShowNativeTrackCue', 'seedNativeTrackCueKey', 'maybeCueNativePlaybackTrack', 'initSeek', 'seekStart',
    'seekEnd', 'seekChange', 'updateSeekUI', 'renderSamplerateUI', 'formatRadioStreamLine',
    'formatStreamingMetaLine', 'updateFooterForStreamingOwner', 'renderFooterFavoriteFromTidalChange',
    'toggleTidalFooterFavorite', 'toggleCurrentTrackFavorite',
];

// Module loads before the app shell and owns every moved function.
assert.ok(
    indexSource.indexOf('playback_ui.js?v=') < indexSource.indexOf('app.js?v='),
    'playback_ui.js must load before app.js',
);
assert.match(indexSource, /playback_ui\.js\?v=\d+\.\d+\.\d+/);
for (const name of MOVED) {
    assert.equal(typeof UI[name], 'function', `module must export ${name}`);
    assert.doesNotMatch(
        appSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no app.js duplicate: ${name} lives in playback_ui.js`,
    );
    assert.doesNotMatch(
        appSource,
        new RegExp(`(?<![\\w.$'\`"])${name}\\s*\\(`),
        `no bare app.js call: ${name} goes through PlaybackUI.`,
    );
}
// The UI module renders through the shared streaming renderer without
// branching on provider identity (same rule as the owner-resolution test).
const uiSource = fs.readFileSync(path.join(repoRoot, 'static', 'playback_ui.js'), 'utf8');
const appLines = uiSource.split('\n');
const rendererStart = appLines.findIndex((l) => l.startsWith('function updateFooterForStreamingOwner('));
assert.ok(rendererStart >= 0, 'missing updateFooterForStreamingOwner');
const rendererEnd = appLines.findIndex((l, i) => i > rendererStart && l.startsWith('function '));
assert.ok(rendererEnd > rendererStart, 'unterminated updateFooterForStreamingOwner');
const streamingFooterSrc = appLines.slice(rendererStart, rendererEnd).join('\n');
assert.ok(!/['"]qobuz['"]/.test(streamingFooterSrc), 'streaming footer renderer must not branch on qobuz');
assert.ok(!/['"]spotify['"]/.test(streamingFooterSrc), 'streaming footer renderer must not branch on spotify');
// Module-only state: moved declarations left app.js.
for (const decl of [
    'let volumeDisplayTimer', 'let nowPlayingCueTimer', 'let nowPlayingCueCoverAbort',
    'let playbackFooterResizeObserver', 'let playbackFooterSpaceFrame',
    'let seekDragging', 'let seekPendingPos',
    'let lastValidVuSnapshot', 'const VU_HOLDOVER_MS', 'let coverDetailQueueSignature',
]) {
    assert.ok(
        new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm').test(uiSource),
        `UI module must own: ${decl}`,
    );
    assert.doesNotMatch(appSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `app.js must not keep: ${decl}`);
}
// app.js wiring reaches the module through the PlaybackUI alias.
for (const snippet of [
    'const PlaybackUI = window.FXRoutePlaybackUI || {};',
    'window.FXRoutePlaybackUI?.init({',
    'PlaybackUI.updatePlaybackUI(',
    'PlaybackUI.renderFooterModeButtons(',
    'PlaybackUI.setupPlaybackControls()',
    'PlaybackUI.initSeek()',
    'maybeShowNativeTrackCue: (...args) => PlaybackUI.maybeShowNativeTrackCue',
    'renderSamplerateUI: (...args) => PlaybackUI.renderSamplerateUI',
    'isStreamingFooterSource: (...args) => PlaybackCore.isStreamingFooterSource',
]) {
    assert.ok(appSource.includes(snippet), `app.js wiring must reference ${snippet}`);
}
assert.match(uiSource, /deps\.getState\(\)/, 'state goes through injected getter');
assert.match(uiSource, /deps\.getElements\(\)/, 'elements go through injected getter');
assert.doesNotMatch(uiSource, /(?<![\w.$])state\./, 'no raw app state access');
assert.doesNotMatch(uiSource, /(?<![\w.$])elements\./, 'no raw app elements access');

// Pure helper contracts stay intact.
assert.equal(UI.meterLitCount(null, 6), 0);
assert.equal(UI.meterLitCount(-60, 6), 0);
assert.ok(UI.meterLitCount(-6, 6) > 0);
assert.equal(UI.streamingCueKey({ trackId: 'a', status: 'Playing' }), UI.streamingCueKey({ trackId: 'a', status: 'Playing' }));
assert.equal(UI.nativeTrackCueKey({ id: 'x', source: 'local' }), UI.nativeTrackCueKey({ id: 'x', source: 'local' }));

console.log('PASS  scripts/test_playback_ui_frontend.js (playback UI module owns footer/meter/queue/seek/cover)');
