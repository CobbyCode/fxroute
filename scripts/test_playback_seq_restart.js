#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Regression: a browser tab kept open across an FXRoute restart.
//
// The player sequence (_seq) restarts at 0 with every FXRoute process, while
// an open tab keeps the highest _seq of the previous process. Without a
// baseline reset every later playback update looked stale and was dropped
// (frozen position, mixed footer state) until a hard reload. The WebSocket
// init snapshot of a (re)connected socket resets the ordering baseline;
// ordinary updates keep the stale-order guard.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

globalThis.window = globalThis.window || {};
const PlaybackCore = require('../static/playback_core.js');

const state = { playback: {}, library: {}, samplerate: {}, settings: {} };
PlaybackCore.init({ getState: () => state, getElements: () => ({}) });

// Previous FXRoute process: the tab saw a high sequence.
PlaybackCore.mergePlaybackState({ _seq: 50000, position: 10, playing: true });
assert.equal(state.playback.position, 10);

// New process (restarted counter): ordinary updates are judged stale.
PlaybackCore.mergePlaybackState({ _seq: 7, position: 1 });
assert.equal(state.playback.position, 10, 'guard still drops lower sequences');

// The WebSocket init snapshot of the reconnected socket resets the baseline.
PlaybackCore.mergePlaybackState({ _seq: 5, position: 2, playing: true }, { snapshot: true });
assert.equal(state.playback.position, 2);
assert.equal(state.playback._seq, 5);

// Later updates of the new process flow again; stale ones stay dropped.
PlaybackCore.mergePlaybackState({ _seq: 6, position: 3 });
assert.equal(state.playback.position, 3);
PlaybackCore.mergePlaybackState({ _seq: 4, position: 99 });
assert.equal(state.playback.position, 3);

// A snapshot without a sequence clears the baseline instead of keeping the old one.
PlaybackCore.mergePlaybackState({ _seq: 50000 });
PlaybackCore.mergePlaybackState({ position: 4 }, { snapshot: true });
assert.equal(state.playback._seq, null);
PlaybackCore.mergePlaybackState({ _seq: 1, position: 5 });
assert.equal(state.playback.position, 5);

// Both reconnect snapshots bypass the old baseline: the WebSocket init and
// the /api/status resync after the socket reopened.
const appSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
assert.ok(
    appSource.includes('PlaybackCore.mergePlaybackState(data.player.state, { snapshot: true });'),
    'WebSocket init must merge the player state as a snapshot',
);

(async () => {
    const reconnectState = { playback: { _seq: 50000, position: 10 }, library: {}, samplerate: {}, settings: {} };
    PlaybackCore.init({
        getState: () => reconnectState,
        getElements: () => ({}),
        fetchFn: async (url) => ({
            ok: true,
            json: async () => (url === '/api/status' ? { _seq: 3, position: 1, playing: true } : null),
        }),
        claimWsSyncGeneration: () => 1,
        isWsSyncGenerationCurrent: () => true,
        fetchSpotifyStatus: async () => null,
        fetchQobuzStatus: async () => null,
        seedNativeTrackCueKey: () => {},
        updateLiveBanner: () => {},
        updatePlaybackUI: () => {},
        stopSpotifyPoll: () => {},
        bumpSpotifyPollGeneration: () => {},
    });
    await PlaybackCore.resyncPlaybackAfterReconnect();
    assert.equal(reconnectState.playback._seq, 3, 'reconnect resync resets the baseline');
    assert.equal(reconnectState.playback.position, 1);
    console.log('PASS test_playback_seq_restart.js');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
