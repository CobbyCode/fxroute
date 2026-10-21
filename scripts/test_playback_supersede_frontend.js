#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A play request that is overtaken by a newer one must not leave its
// optimistic markers behind: the superseded response returns early on its
// stale id, so the newer request has to drop them (playLocal/playRadio were
// covered by overwriting; playLibraryRefs never touched them).

const assert = require('node:assert/strict');

globalThis.window = globalThis;
globalThis.document = { hidden: true };
const Core = require('../static/playback_core.js');

const tracks = [
    { id: 'local_a.mp3', title: 'A', source: 'local' },
    { id: 'local_b.mp3', title: 'B', source: 'local' },
];
const state = {
    playback: { queue: {} },
    library: { tracks, shuffle: false, loop: false },
    stations: [{ id: 'groove', title: 'Groove', artist: 'Soma', stream_url: 'http://x/y' }],
    samplerate: {},
    settings: {},
};

// Every /api/play call waits until the test settles it.
let calls = [];
function deferredFetch(url, options) {
    return new Promise((resolve) => {
        calls.push({ url, body: JSON.parse(options.body), settle: (ok, payload) => resolve({
            ok,
            status: ok ? 200 : 500,
            json: async () => payload,
        }) });
    });
}

Core.init({ getState: () => state, fetchFn: deferredFetch });

const optimisticId = () => Core.getPendingOptimisticTrack()?.track?.id ?? null;
const nativeResponse = (track) => ({ playback: { current_track: track, playing: true, queue: {} } });

async function reset() {
    calls = [];
    Core.setPlaybackActionInFlight(false);
    Core.clearPendingOptimisticTrack();
    Core.clearPendingFooterSingleTrackStart();
}

(async () => {
    // A single local start keeps its optimistic track until the response lands.
    await reset();
    let pending = Core.playLocal('local_a.mp3');
    assert.equal(optimisticId(), 'local_a.mp3', 'playLocal marks its track optimistically');
    calls[0].settle(true, nativeResponse(tracks[0]));
    await pending;
    assert.equal(optimisticId(), null, 'a committed playLocal clears its marker');

    // playLibraryRefs overtakes an in-flight playLocal.
    await reset();
    const local = Core.playLocal('local_a.mp3');
    assert.equal(optimisticId(), 'local_a.mp3');
    assert.equal(Core.footerSingleTrackStartLockActive({ current_track: tracks[0] }), true, 'single-track lock armed');
    const refs = Core.playLibraryRefs('tidal:77', ['tidal:77', 'local_b.mp3']);
    assert.equal(optimisticId(), null, 'the overtaken request drops its optimistic track at once');
    assert.equal(Core.footerSingleTrackStartLockActive({ current_track: tracks[0] }), false, 'and its footer lock');
    assert.equal(calls.length, 2);
    // The newer response lands first, the stale one afterwards.
    calls[1].settle(true, nativeResponse({ id: '77', source: 'tidal', title: 'T' }));
    await refs;
    calls[0].settle(true, nativeResponse(tracks[0]));
    await local;
    assert.equal(optimisticId(), null, 'no stale marker after both responses');

    // Same when the overtaken request fails.
    await reset();
    const failing = Core.playLocal('local_b.mp3');
    const takeover = Core.playLibraryRefs('local_a.mp3', ['local_a.mp3', 'local_b.mp3']);
    calls[0].settle(false, { detail: 'no' });
    await failing;
    calls[1].settle(true, nativeResponse(tracks[0]));
    await takeover;
    assert.equal(optimisticId(), null, 'a failed overtaken request leaves no marker either');

    // Radio overtaken by a mixed queue.
    await reset();
    const radio = Core.playRadio('groove');
    assert.equal(optimisticId(), 'radio_groove');
    const mixed = Core.playLibraryRefs('local_a.mp3', ['local_a.mp3']);
    assert.equal(optimisticId(), null, 'a radio start overtaken by a queue drops its marker');
    calls[1].settle(true, nativeResponse(tracks[0]));
    calls[0].settle(true, nativeResponse({ id: 'radio_groove', source: 'radio' }));
    await Promise.all([radio, mixed]);
    assert.equal(optimisticId(), null);

    // A marker that outlived its request (action flag already reset by a
    // server push) goes as well when a queue starts.
    await reset();
    const orphan = Core.playLocal('local_a.mp3');
    Core.setPlaybackActionInFlight(false);
    assert.equal(optimisticId(), 'local_a.mp3');
    const afterPush = Core.playLibraryRefs('local_b.mp3', ['local_b.mp3']);
    assert.equal(optimisticId(), null, 'a queue start clears a marker whose flag was already reset');
    calls[1].settle(true, nativeResponse(tracks[1]));
    calls[0].settle(true, nativeResponse(tracks[0]));
    await Promise.all([orphan, afterPush]);
    assert.equal(optimisticId(), null);

    // A newer playLocal still owns the marker when it overtakes an older one.
    await reset();
    const first = Core.playLocal('local_a.mp3');
    const second = Core.playLocal('local_b.mp3');
    assert.equal(optimisticId(), 'local_b.mp3', 'the newest start keeps its optimistic track');
    calls[0].settle(true, nativeResponse(tracks[0]));
    await first;
    assert.equal(optimisticId(), 'local_b.mp3', 'the overtaken response must not clear the newer marker');
    calls[1].settle(true, nativeResponse(tracks[1]));
    await second;
    assert.equal(optimisticId(), null);

    console.log('playback supersede frontend: ok');
    process.exit(0);
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
