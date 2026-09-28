#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Status reads must heal missed owner updates without rolling back newer ones.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

function playback(owner, seq = 1142) {
    const native = owner === 'tidal';
    return {
        _seq: seq, playback_owner: owner,
        current_track: native ? { id: '407368888', source: 'tidal', title: 'Saved My Life' } : null,
        current_file: native ? '/cache/tidal.mp4' : null,
        playing: native, paused: false, ended: false, position: 0, duration: native ? 416 : 0,
        queue: { active: false, count: 0, index: -1, tracks: [] },
        live_title: null, radio_metadata: null, stream_info: null, metadata: {},
        output_peak_warning: { available: true, vu_fresh: true, vu_db: -20 },
    };
}

function deferred() {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return { promise, resolve };
}

function harness(initial = playback('tidal', 1120)) {
    const intervals = new Map();
    const context = vm.createContext({
        console,
        window: { __footerSource: 'local', __visibleTab: 'radio' },
        document: { hidden: false, getElementById: () => null },
        setInterval(fn) { const id = intervals.size + 1; intervals.set(id, fn); return id; },
        clearInterval(id) { intervals.delete(id); },
        setTimeout, clearTimeout,
    });
    for (const file of ['playback_core.js', 'streaming_runtime.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../static', file), 'utf8'), context);
    }
    const core = context.FXRoutePlaybackCore;
    const streaming = context.FXRouteStreamingRuntime;
    const state = { playback: initial, library: {}, settings: {}, samplerate: {} };
    let response = initial;
    core.init({
        getState: () => state,
        fetchFn: async () => ({ ok: true, json: async () => response }),
        updatePlaybackUI: () => core.reconcileFooterSource(),
        claimWsSyncGeneration: () => 1,
        isWsSyncGenerationCurrent: () => true,
        handleIncomingSpotifyState: streaming.handleIncomingSpotifyState,
        handleIncomingQobuzState: streaming.handleIncomingQobuzState,
    });
    streaming.init({ reconcileFooterSource: core.reconcileFooterSource });
    core.reconcileFooterSource();
    return {
        core, state, context, streaming, intervals,
        respond(data) { response = data; },
        commit(data, options) { core.mergePlaybackState(data, options); core.syncFooterOwnershipFromPlayback(); },
        assertOwner(owner) {
            assert.equal(state.playback.playback_owner, owner, 'accepted playback owner');
            assert.equal(context.window.__footerSource, owner === 'tidal' || !owner ? 'local' : owner, 'footer owner');
            assert.equal(core.getEffectivePlaybackControlSource(), owner === 'tidal' || !owner ? 'local' : owner, 'transport owner');
        },
    };
}

for (const owner of ['spotify', 'qobuz']) {
    test(`fresh ${owner} status with no MPV track heals retained TIDAL`, async () => {
        const h = harness();
        h.respond(playback(owner));
        await h.core.fetchMetadata();
        h.assertOwner(owner);
        assert.equal(h.state.playback.current_track, null, 'stopped MPV metadata is cleared');
    });
}

test('owner polling recovers while the previous source is stopped', async () => {
    const h = harness(playback(null));
    h.respond(playback('qobuz'));
    await h.core.fetchMetadata();
    h.assertOwner('qobuz');
});

test('fresh stop clears native track and owner', async () => {
    const h = harness();
    h.respond(playback(null));
    await h.core.fetchMetadata();
    h.assertOwner(null);
    assert.equal(h.state.playback.current_track, null);
});

for (const entry of ['fetchMetadata', 'fetchPlaybackStatus', 'resyncPlaybackAfterReconnect']) {
    for (const [previous, next, seq] of [['tidal', 'spotify', 1142], ['qobuz', 'spotify', 1120], ['spotify', 'qobuz', 1120]]) {
        test(`${entry}: late ${previous} response cannot undo ${next} commit`, async () => {
            const h = harness(playback(previous, 1120));
            const read = deferred();
            h.respond(read.promise);
            const pending = h.core[entry]();
            h.commit(playback(next, seq));
            read.resolve(playback(previous, 1120));
            await pending;
            h.assertOwner(next);
        });
    }
}

test('metadata status retains sequence rejection even without an intervening commit', async () => {
    const h = harness(playback('spotify'));
    h.respond(playback('tidal', 1120));
    await h.core.fetchMetadata();
    h.assertOwner('spotify');
    assert.equal(h.state.playback._seq, 1142);
});

test('pre-reconnect HTTP response cannot overwrite the new process snapshot', async () => {
    const h = harness();
    const read = deferred();
    h.respond(read.promise);
    const pending = h.core.fetchMetadata();
    h.commit(playback('qobuz', 3), { snapshot: true });
    read.resolve(playback('tidal', 50000));
    await pending;
    h.assertOwner('qobuz');
    assert.equal(h.state.playback._seq, 3);
});

test('position poll commits a discovered external owner to the shared state', async () => {
    const h = harness();
    h.respond(playback('spotify'));
    h.core.startPlaybackPositionPoll();
    await [...h.intervals.values()][0]();
    h.core.reconcileFooterSource();
    h.assertOwner('spotify');
    assert.equal(h.intervals.size, 0, 'native position polling stops for external playback');
});

test('late position poll cannot overwrite a newer native playback commit', async () => {
    const h = harness();
    const read = deferred();
    h.respond(read.promise);
    h.core.startPlaybackPositionPoll();
    const pending = [...h.intervals.values()][0]();
    h.commit(playback('tidal', 1150));
    read.resolve(playback('spotify'));
    await pending;
    h.assertOwner('tidal');
});

test('committed Spotify owner wins over stale Qobuz Playing telemetry', () => {
    const h = harness(playback('qobuz'));
    h.streaming.handleIncomingQobuzState({ available: true, installed: true, status: 'Playing', title: 'Evolution' });
    h.streaming.handleIncomingSpotifyState({ available: true, installed: true, status: 'Paused', title: 'Black Bird' });
    h.commit(playback('spotify'));
    h.assertOwner('spotify');
    h.streaming.handleIncomingQobuzState({ available: true, installed: true, status: 'Playing', title: 'Evolution' });
    h.assertOwner('spotify');
});

test('reconnect still seeds provider metadata when a concurrent status read wins', async () => {
    const h = harness(playback('qobuz'));
    const provider = deferred();
    let pollsStarted = 0;
    h.core.init({
        fetchQobuzStatus: () => provider.promise,
        shouldPollQobuz: h.streaming.shouldPollQobuz,
        startQobuzPoll: () => { pollsStarted += 1; },
    });
    const pending = h.core.resyncPlaybackAfterReconnect();
    await h.core.fetchPlaybackStatus();
    provider.resolve({ available: true, installed: true, status: 'Playing', title: 'Evolution' });
    await pending;
    h.assertOwner('qobuz');
    assert.equal(h.context.window.__qobuzLastData?.title, 'Evolution');
    assert.equal(pollsStarted, 1, 'footer provider polling is started after seeding');
});

for (const provider of ['spotify', 'qobuz']) {
    test(`reconnect cannot roll back newer ${provider} telemetry`, async () => {
        const h = harness(playback(provider));
        const read = deferred();
        const field = provider === 'spotify' ? '__spotifyLastData' : '__qobuzLastData';
        const handle = provider === 'spotify' ? h.streaming.handleIncomingSpotifyState : h.streaming.handleIncomingQobuzState;
        h.core.init(provider === 'spotify' ? { fetchSpotifyStatus: () => read.promise } : { fetchQobuzStatus: () => read.promise });
        const pending = h.core.resyncPlaybackAfterReconnect();
        handle({ available: true, installed: true, status: 'Playing', title: 'New track' });
        read.resolve({ available: true, installed: true, status: 'Paused', title: 'Old track' });
        await pending;
        assert.equal(h.context.window[field].title, 'New track');
        assert.equal(h.context.window[field].status, 'Playing');
        h.assertOwner(provider);
    });
}

for (const entry of ['fetchPlaybackStatus', 'resyncPlaybackAfterReconnect']) {
    test(`${entry}: live banner survives a concurrent WebSocket init`, async () => {
        const h = harness();
        const read = deferred();
        let bannerLive = false;
        h.respond(read.promise);
        h.core.init({ updateLiveBanner: data => { bannerLive = data.live; } });
        const pending = h.core[entry]();
        h.commit(playback('spotify'), { snapshot: true });
        read.resolve({ ...playback('tidal'), live: true });
        await pending;
        h.assertOwner('spotify');
        assert.equal(bannerLive, true);
    });
}
