#!/usr/bin/env node
// Demo contracts for the two things a reload and a request must feel like:
//
// 1. Response pace — the simulated backend answers with a short, per-surface
//    delay so the frontend's own loading affordances appear. It must stay off
//    where a held response could never resolve (vm contexts without a browser
//    or without real timers) and when `?fast` is requested.
// 2. Session persistence — a reload reattaches to the running transport
//    (track, position, queue, volume) and keeps the DSP, output and
//    measurement state, while a fresh session still opens on the seeded stock.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');
const library2Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library2.js'), 'utf8');
const library3Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library3.js'), 'utf8');
const radioSource = fs.readFileSync(path.join(root, 'demo', 'data', 'radio.js'), 'utf8');
const measurementsSource = fs.readFileSync(path.join(root, 'demo', 'data', 'measurements.js'), 'utf8');
const alignmentSource = fs.readFileSync(path.join(root, 'demo', 'data', 'alignment.js'), 'utf8');
const stateSource = fs.readFileSync(path.join(root, 'demo', 'state.js'), 'utf8');
const routesSource = fs.readFileSync(path.join(root, 'demo', 'routes.js'), 'utf8');

// The demo parks its session in sessionStorage, like the Auto Sub job it
// already kept there; the stand-in survives a simulated reload.
function makeStorage(initial = {}) {
    const data = new Map(Object.entries(initial));
    return {
        getItem: key => (data.has(key) ? data.get(key) : null),
        setItem: (key, value) => { data.set(key, String(value)); },
        removeItem: key => { data.delete(key); },
    };
}

// `browser: true` gives the context what the paced demo checks for (a
// document, a location and real timers); a nested `search` string stands in
// for the page URL. Without it the context looks like the other vm tests.
function makeDemoContext({ storage = makeStorage(), browser = true, search = '' } = {}) {
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        sessionStorage: storage,
        Date,
        Math,
        console,
        URLSearchParams,
        FormData,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    if (browser) {
        ctx.setTimeout = (fn, ms) => setTimeout(fn, ms);
        ctx.clearTimeout = (handle) => clearTimeout(handle);
        ctx.document = {};
        ctx.location = { search, reload() {} };
    }
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const source of [librarySource, library2Source, library3Source, radioSource,
        measurementsSource, alignmentSource, stateSource, routesSource]) {
        vm.runInContext(source, ctx);
    }
    return ctx;
}

const post = (ctx, url, body) => ctx.fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
const json = async (response) => (await response).json();
const flush = (ms) => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
    // ── Response pace ───────────────────────────────────────────────────
    // A control call answers in the short control window, the measurement
    // surface takes the longest — both far below a human-visible wait.
    const paced = makeDemoContext({ search: '' });
    const pace = paced.FXROUTE_DEMO_STATE.demoPace;
    assert.equal(pace.fast, false, 'a plain page load must run the paced demo');
    // Best of three: the first call in a fresh context pays for lazy
    // compilation and for serializing the fixture payloads, which is CPU
    // time, not the simulated network wait under test.
    async function timed(ctx, url, runs = 3) {
        let best = Infinity;
        for (let i = 0; i < runs; i += 1) {
            const start = Date.now();
            await ctx.fetch(url);
            best = Math.min(best, Date.now() - start);
        }
        return best;
    }
    const controlMs = await timed(paced, '/api/status');
    const heavyMs = await timed(paced, '/api/measurements');
    assert.ok(controlMs >= 15, `a control call must take the control window (got ${controlMs} ms)`);
    assert.ok(heavyMs >= 100, `a measurement read must take the heavy window (got ${heavyMs} ms)`);
    assert.ok(heavyMs <= 1500 && controlMs <= 500, 'the pace must stay a hint of work, not a wait');
    assert.ok(heavyMs > controlMs, 'heavier surfaces must take longer than controls');

    // `?fast` switches it off for browser checks; so does a context without a
    // browser (the vm tests), where a held response could never resolve.
    const fast = makeDemoContext({ search: '?fast' });
    assert.equal(fast.FXROUTE_DEMO_STATE.demoPace.fast, true, '?fast must switch the pace off');
    const fastMs = await timed(fast, '/api/status');
    assert.ok(fastMs < 15, `?fast must answer at once (got ${fastMs} ms)`);
    assert.ok(fastMs < controlMs, `?fast must drop the control wait (${fastMs} ms vs ${controlMs} ms)`);
    const vmOnly = makeDemoContext({ browser: false });
    assert.equal(vmOnly.FXROUTE_DEMO_STATE.demoPace.fast, true,
        'a context without a browser must not pace (delayed promises would never resolve)');
    assert.ok(await timed(vmOnly, '/api/status') < 15, 'a vm context must answer at once');

    // ── Session persistence ─────────────────────────────────────────────
    const storage = makeStorage();
    const trackId = 'demo_session_track';
    const first = makeDemoContext({ storage, search: '?fast' });
    // A playable id from the active demo catalog, whatever it holds.
    const playingId = first.FXROUTE_DEMO_STATE.localTracks
        ? (first.FXROUTE_DEMO_STATE.activeLibraryTracks()[0] || {}).id : null;
    assert.ok(playingId, 'the demo must ship a local track to play');
    await post(first, '/api/play', { track_id: playingId });
    assert.equal(first.FXROUTE_DEMO_STATE.getPlayback().playing, true);
    await post(first, '/api/volume', { volume: 71 });
    const { assignGlobalPreset } = require('./demo_preset_test_support.js');
    await assignGlobalPreset(first.fetch, '+6');
    await post(first, '/api/dsp/extras', { loudness_enabled: true, loudnessStrength: 5 });
    await json(await post(first, '/api/measurements/save', {
        measurement: { id: trackId, name: 'Session sweep', channel: 'left', traces: [] },
    }));
    const before = first.FXROUTE_DEMO_STATE.getPlayback();
    assert.equal(before.volume, 71);
    // The snapshot is throttled: give the trailing write its window, then
    // leave a full second of wall clock before the reload so the reattached
    // position has visibly advanced (that is the reattach contract).
    await flush(1200);
    await flush(1100);

    const second = makeDemoContext({ storage, search: '?fast' });
    const restored = second.FXROUTE_DEMO_STATE.getPlayback();
    assert.equal(restored.playing, true, 'a reload must reattach to the running transport');
    assert.equal(restored.paused, false);
    assert.equal(restored.playback_owner, 'local');
    assert.equal(restored.current_track.id, playingId, 'the same track must come back');
    // The reload wall-clock counts as playback time, exactly like reattaching
    // to a backend that never stopped.
    assert.ok(restored.position >= 1,
        `the reattached position must have advanced over the reload (got ${restored.position})`);
    assert.equal(second.FXROUTE_DEMO_STATE.getVolume(), 71, 'the volume must survive the reload');
    const dspPayload = await json(await second.fetch('/api/dsp/presets'));
    assert.equal(dspPayload.active_preset, '+6', 'the active preset must survive the reload');
    assert.equal(dspPayload.global_extras.loudness.enabled, true, 'the output extras must survive');
    const outputs = await json(await second.fetch('/api/audio/outputs'));
    assert.equal((await json(await second.fetch('/api/audio/output-state'))).active_mode,
        'stereo-sub', 'the output mode must survive');
    const settings = await json(await second.fetch('/api/measurements/settings'));
    assert.equal(settings.status, 'ok');
    const savedRuns = await json(await second.fetch('/api/measurements'));
    assert.ok(savedRuns.measurements.some(m => m.id === trackId),
        'a run created in the session must still be listed after the reload');

    // A stopped session comes back stopped, and a paused one comes back paused
    // — the backend has no track loaded either.
    const stoppedStorage = makeStorage();
    const stopped = makeDemoContext({ storage: stoppedStorage, search: '?fast' });
    await post(stopped, '/api/play', { track_id: playingId });
    await flush(1200);
    await json(await post(stopped, '/api/playback/stop', {}));
    await flush(1200);
    const stoppedReload = makeDemoContext({ storage: stoppedStorage, search: '?fast' });
    assert.equal(stoppedReload.FXROUTE_DEMO_STATE.getPlayback().playing, false,
        'a stopped session must not come back playing');

    // Dropping the stored session clears everything: the next load opens on
    // the stock.
    second.FXROUTE_DEMO_STATE.session.clear();
    const afterReset = makeDemoContext({ storage, search: '?fast' });
    assert.equal(afterReset.FXROUTE_DEMO_STATE.getPlayback().playing, false,
        'a dropped session must not reattach');
    assert.equal(afterReset.FXROUTE_DEMO_STATE.getPlayback().volume, 38, 'the stock volume must return');
    const stockDsp = await json(await afterReset.fetch('/api/dsp/presets'));
    assert.notEqual(stockDsp.active_preset, '+6', 'the stock preset must return after a reset');
    assert.equal((await json(await afterReset.fetch('/api/measurements'))).measurements
        .some(m => m.id === trackId), false, 'a dropped session must not keep its runs');

    // A fresh session (no stored snapshot at all) opens on the seeded stock
    // with the interface still selected.
    const fresh = makeDemoContext({ search: '?fast' });
    assert.equal(fresh.FXROUTE_DEMO_STATE.getPlayback().playing, false);
    assert.equal(fresh.FXROUTE_DEMO_STATE.getPlayback().volume, 38);
    const freshOutputs = await json(await fresh.fetch('/api/audio/outputs'));
    assert.match(freshOutputs.selected_output.key, /Scarlett_16i16/,
        'the seeded .104 system must be the default selection');

    console.log('ok demo session + pace contract');
})().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
