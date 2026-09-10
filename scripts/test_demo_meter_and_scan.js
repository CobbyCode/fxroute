#!/usr/bin/env node
// Demo realism contracts:
// 1. The meter simulation follows a program envelope (correlated L/R,
//    smoothed attack/release, limiter clamp) instead of jumping to
//    uncorrelated random values every tick.
// 2. The library scan/share-discovery cycle: a boot-armed scan reports
//    `scanning: true` with ramping counts and then settles, and POST
//    /api/library/refresh arms the same cycle on demand. The share
//    discovery flag rides /api/music-libraries once after boot.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');
const library2Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library2.js'), 'utf8');
const radioSource = fs.readFileSync(path.join(root, 'demo', 'data', 'radio.js'), 'utf8');
const measurementsSource = fs.readFileSync(path.join(root, 'demo', 'data', 'measurements.js'), 'utf8');
const stateSource = fs.readFileSync(path.join(root, 'demo', 'state.js'), 'utf8');
const routesSource = fs.readFileSync(path.join(root, 'demo', 'routes.js'), 'utf8');

function makeDemoContext(bootArmed) {
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        Date,
        Math,
        console,
        URLSearchParams,
        FormData,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    if (bootArmed) ctx.window.__demoArmBootScan = true;
    vm.createContext(ctx);
    vm.runInContext(librarySource, ctx);
    vm.runInContext(library2Source, ctx);
    vm.runInContext(radioSource, ctx);
    vm.runInContext(measurementsSource, ctx);
    vm.runInContext(stateSource, ctx);
    vm.runInContext(routesSource, ctx);
    return ctx;
}

(async () => {
    // ── Meter envelope ──────────────────────────────────────────────────
    // The sim is browser-driven (setInterval); tests drive the exported
    // tick directly. Default DSP stock: limiter enabled at -1 dB.
    const ctx = makeDemoContext(false);
    const state = ctx.window.FXROUTE_DEMO_STATE;
    const meterTick = state.demoMeterTick;
    assert.equal(typeof meterTick, 'function', 'demoMeterTick must be exported for the interval and tests');

    state.playLocal(state.localTracks[0].id);
    assert.equal(state.getPlayback().playback_owner, 'local');
    let t = 0;
    const samples = [];
    for (let i = 0; i < 40; i += 1) {
        meterTick(t);
        t += 500;
        samples.push(state.getPeak());
    }
    for (const s of samples) {
        assert.ok(Number.isFinite(s.vu_db_l) && Number.isFinite(s.vu_db_r), 'meter levels must be finite');
        assert.ok(s.vu_db_l >= -60 && s.vu_db_r >= -60, 'meter levels must not go below the floor');
        assert.ok(s.vu_db_l <= 0.5 && s.vu_db_r <= 0.5,
            'levels must stay inside the limiter clamp (threshold -1 dB + headroom)');
        assert.ok(Math.abs(s.vu_db_l - s.vu_db_r) <= 4.5,
            'L/R must stay correlated (program spread + transient decorrelation)');
        assert.equal(s.vu_fresh, true, 'fresh flag must stay set while playing');
    }
    const lVals = samples.map((s) => s.vu_db_l);
    assert.ok(new Set(lVals).size > 5, 'meter must animate instead of sitting on one value');
    assert.ok(lVals.some((v) => v < -6), 'program level must breathe below the transient zone');
    // Playback always opens with a transient, so the meter reaches the upper
    // range within the first ticks (deterministic, not probabilistic).
    assert.ok(samples.slice(0, 5).some((s) => s.vu_db_l > -10),
        'start transient must push the level toward the top of the scale');

    // Paused playback resets the meter to the floor and drops freshness.
    state.togglePause();
    meterTick(t);
    const idle = state.getPeak();
    assert.equal(idle.vu_db_l, -60);
    assert.equal(idle.vu_db_r, -60);
    assert.equal(idle.vu_fresh, false);

    // ── Refresh cycle (POST /api/library/refresh) ───────────────────────
    // A manual refresh arms a short scan: the response reports scanning and
    // the status endpoint ramps tracks_found until the scan settles.
    const refresh = await (await ctx.fetch('/api/library/refresh', { method: 'POST' })).json();
    assert.equal(refresh.status, 'ok');
    assert.equal(refresh.scanning, true);
    assert.ok(state.demoScan && state.demoScan.active, 'refresh must arm the scan state');
    const activeTracks = state.activeLibraryTracks().length;
    const mid = await (await ctx.fetch('/api/library/status')).json();
    assert.equal(mid.scanning, true, 'status must report scanning while the scan runs');
    assert.ok(mid.tracks_found <= activeTracks, 'tracks_found must not exceed the target');
    // Fast-forward the scan (test hook: durationMs override) and verify it
    // settles back to the final counts.
    state.demoScan.durationMs = 0;
    const done = await (await ctx.fetch('/api/library/status')).json();
    assert.equal(done.scanning, false);
    assert.equal(done.tracks_found, activeTracks);
    assert.equal(state.demoScan, null, 'settled scan must be cleared');

    // Without the boot flag the status endpoint stays final on first call
    // (existing tests depend on this), and a second status read stays final.
    const idleStatus = await (await ctx.fetch('/api/library/status')).json();
    assert.equal(idleStatus.scanning, false);
    assert.equal(idleStatus.tracks_found, activeTracks);

    // ── Boot-armed scan + share discovery ───────────────────────────────
    // boot.js arms the cycle before the frontend's first poll; the first
    // library-status call reports scanning and the first music-libraries
    // call reports discovery_refreshing, then both settle.
    const bootCtx = makeDemoContext(true);
    const bootFetch = bootCtx.fetch;
    const bootState = bootCtx.window.FXROUTE_DEMO_STATE;
    const bootStatus = await (await bootFetch('/api/library/status')).json();
    assert.equal(bootStatus.scanning, true, 'boot must start with a short library scan');
    const bootActiveTracks = bootState.activeLibraryTracks().length;
    assert.ok(bootStatus.tracks_found >= 0 && bootStatus.tracks_found <= bootActiveTracks);
    const bootLibraries = await (await bootFetch('/api/music-libraries')).json();
    assert.equal(bootLibraries.discovery_refreshing, true,
        'boot share discovery must report discovery_refreshing on the first call');
    assert.equal(bootLibraries.active_id, 'demo-library-2');
    bootState.demoScan.durationMs = 0;
    const bootSettled = await (await bootFetch('/api/library/status')).json();
    assert.equal(bootSettled.scanning, false);
    assert.equal(bootSettled.tracks_found, bootActiveTracks);
    const bootLibraries2 = await (await bootFetch('/api/music-libraries')).json();
    assert.ok(!bootLibraries2.discovery_refreshing,
        'share discovery must settle on the follow-up call');

    console.log('ok demo meter + scan/discovery cycle');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});