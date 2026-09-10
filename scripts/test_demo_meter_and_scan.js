#!/usr/bin/env node
// Demo realism contracts:
// 1. The meter simulation follows a program envelope (correlated L/R,
//    smoothed attack/release) with a real-analog monitor tap: post-chain
//    behind the protection limiter (the live post-limiter tap), so an
//    engaged limiter caps the visible level while volume never moves the
//    meter, plus an independent fast peak path fed by the same tapped
//    signal (raw overs latch a hold, never derived from the smoothed VU).
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
        assert.ok(s.vu_db_l <= 3 && s.vu_db_r <= 3,
            'levels must stay inside the display bounds');
        assert.ok(Math.abs(s.vu_db_l - s.vu_db_r) <= 4.5,
            'L/R must stay correlated (program spread + transient decorrelation)');
        assert.equal(s.vu_fresh, true, 'fresh flag must stay set while playing');
    }
    const lVals = samples.map((s) => s.vu_db_l);
    assert.ok(new Set(lVals).size > 5, 'meter must animate instead of sitting on one value');
    assert.ok(lVals.some((v) => v < -6), 'program level must breathe below the transient zone');
    // Playback start shows the program level immediately: no forced
    // spike, no climb from the floor — signal is simply present.
    assert.ok(samples[0].vu_db_l > -26, 'meter shows program level on the first tick');
    assert.equal(samples[0].vu_db_l, samples[1].vu_db_l, 'no attack ramp on start: already on program');

    // Paused playback floors the VU at once, but freshness follows the live
    // monitor's no-data window: audio just stopped, so the meter stays fresh
    // until 3 s pass without samples, then goes stale.
    state.togglePause();
    meterTick(t); t += 500;
    const idle = state.getPeak();
    assert.equal(idle.vu_db_l, -60);
    assert.equal(idle.vu_db_r, -60);
    assert.equal(idle.vu_fresh, true, 'fresh within the no-data grace');
    assert.equal(idle.vu_age_ms, 500);
    for (let i = 0; i < 6; i += 1) { meterTick(t); t += 500; }
    const stale = state.getPeak();
    assert.equal(stale.vu_fresh, false, 'stale after the no-data timeout');
    assert.ok(stale.vu_age_ms > 3000, `sample age must exceed the timeout (${stale.vu_age_ms})`);

    // ── Settings response + independent peak path ─────────────────────
    // Seeded PRNG + fresh contexts make the program/transient schedule
    // identical across scenarios, so mean-level deltas isolate the DSP
    // setting under test.
    const realRandom = Math.random;
    function seededRandom(seed) {
        let a = seed >>> 0;
        return function () {
            a |= 0; a = (a + 0x6D2B79F5) | 0;
            let tt = Math.imul(a ^ (a >>> 15), 1 | a);
            tt = (tt + Math.imul(tt ^ (tt >>> 7), 61 | tt)) ^ tt;
            return ((tt ^ (tt >>> 14)) >>> 0) / 4294967296;
        };
    }
    async function meterMean(setup, ticks = 120) {
        Math.random = seededRandom(7);
        try {
            const c = makeDemoContext(false);
            const st = c.window.FXROUTE_DEMO_STATE;
            const post = (url, body) => c.fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
            await post('/api/play', { track_id: 'd2-midnight-relay_01' });
            if (setup) await setup(post);
            let sum = 0, n = 0, tt = 0;
            for (let i = 0; i < ticks + 20; i += 1) {
                st.demoMeterTick(tt); tt += 500;
                if (i >= 20) { sum += st.getMeter().vu_db_l; n += 1; }
            }
            return sum / n;
        } finally { Math.random = realRandom; }
    }
    const baseMean = await meterMean(null);
    const hrMean = await meterMean((post) => post('/api/dsp/extras', { headroom_enabled: true, headroom_gain_db: -6 }));
    assert.ok(hrMean - baseMean < -3, `headroom -6 dB must move the VU (delta ${hrMean - baseMean})`);
    // The default program never reaches the stock limiter threshold, so
    // toggling the limiter leaves the mean unchanged; the cap itself is
    // asserted on a hot master below.
    const limMean = await meterMean((post) => post('/api/dsp/extras', { limiter_enabled: false }));
    assert.ok(Math.abs(limMean - baseMean) < 2,
        `default program stays below the limiter threshold (delta ${limMean - baseMean})`);
    const plusMean = await meterMean((post) => post('/api/dsp/presets/load', { preset_name: '+6' }));
    assert.ok(plusMean - baseMean > 3, '+6 preset must lift the VU');

    // Hand-set VU levels alone must never trip the peak detector: holds
    // latch only from raw overs on the fast path.
    const peakCtx = makeDemoContext(false);
    const peakState = peakCtx.window.FXROUTE_DEMO_STATE;
    peakState.playLocal(peakState.localTracks[0].id);
    peakState.getMeter().vu_db_l = 0.5; peakState.getMeter().vu_db_r = 0.5; peakState.getMeter().vu_fresh = true;
    assert.equal(peakState.getPeak().detected, false, 'smoothed VU levels must not latch peaks');

    // A hot program latches peak holds only when unprotected: engaged
    // limiter caps raw peaks below 0 dBFS (reference: post-limiter tap).
    async function peakRun(setup, ticks = 200) {
        Math.random = seededRandom(11);
        try {
            const c = makeDemoContext(false);
            const st = c.window.FXROUTE_DEMO_STATE;
            const post = (url, body) => c.fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
            await post('/api/play', { track_id: 'd2-midnight-relay_01' });
            if (setup) await setup(post);
            let hits = 0, max = -60, tt = 0;
            for (let i = 0; i < ticks + 20; i += 1) {
                st.demoMeterTick(tt); tt += 500;
                if (i >= 20) {
                    if (st.getPeak().detected) hits += 1;
                    max = Math.max(max, st.getMeter().vu_db_l);
                }
            }
            return { hits, max };
        } finally { Math.random = realRandom; }
    }
    const hotOn = await peakRun((post) => post('/api/dsp/presets/load', { preset_name: '+6' }));
    assert.equal(hotOn.hits, 0, 'engaged limiter caps peaks below 0 dBFS even on a hot master');
    assert.ok(hotOn.max <= -1, `engaged limiter caps the visible level at its threshold (max ${hotOn.max})`);
    const hotOff = await peakRun(async (post) => {
        await post('/api/dsp/presets/load', { preset_name: '+6' });
        await post('/api/dsp/extras', { limiter_enabled: false });
    });
    assert.ok(hotOff.hits > 0, 'unprotected hot program overs');
    // A default-level program may flash red once in a while when
    // unprotected, but never with the limiter engaged: capped raw peaks
    // stay under 0 dBFS by construction.
    const calmOff = await peakRun((post) => post('/api/dsp/extras', { limiter_enabled: false }));
    assert.ok(calmOff.hits <= 15, `default program must stay out of the red except for rare flashes (got ${calmOff.hits})`);
    assert.ok(calmOff.hits > 0, 'a default program does flash once unprotected, unlike the stock-limiter calmOn run');
    // Gain staging gradient (limiter disengaged): default spars, +3 dB
    // clearly busier, +6 dB saturated.
    const midOff = await peakRun(async (post) => {
        await post('/api/dsp/presets/load', { preset_name: '+3' });
        await post('/api/dsp/extras', { limiter_enabled: false });
    });
    assert.ok(midOff.hits > calmOff.hits, `+3 dB must peak clearly more than default (${midOff.hits} vs ${calmOff.hits})`);
    assert.ok(midOff.hits < hotOff.hits, `+6 dB must peak clearly more than +3 dB (${hotOff.hits} vs ${midOff.hits})`);
    // Stock-limiter contract, deliberately here as documentation: with the
    // limiter engaged the cap sits below the detection threshold, so even a
    // default program cannot red. This is not a gain-staging assertion.
    const calmOn = await peakRun(null);
    assert.equal(calmOn.hits, 0, 'engaged limiter keeps a default program out of the red');
    for (const gain of [-3, -6]) {
        // Headroom must be judged with the limiter disengaged; with the
        // stock limiter engaged the cap alone would force hits to zero.
        const hrHits = await peakRun((post) => post('/api/dsp/extras', {
            headroom_enabled: true, headroom_gain_db: gain, limiter_enabled: false,
        }));
        assert.equal(hrHits.hits, 0, `headroom ${gain} dB must keep the meter out of the red`);
    }

    // Last-over stamps follow DSPPeakMonitor.snapshot(): the live monitor
    // never nulls them when the hold expires, so a consumer keeps the
    // "last clipped at …" time after the indicator clears.
    {
        Math.random = seededRandom(11);
        try {
            const c = makeDemoContext(false);
            const st = c.window.FXROUTE_DEMO_STATE;
            const post = (url, body) => c.fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
            await post('/api/play', { track_id: 'd2-midnight-relay_01' });
            await post('/api/dsp/presets/load', { preset_name: '+6' });
            await post('/api/dsp/extras', { limiter_enabled: false });
            let tt = 0, hit = null;
            for (let i = 0; i < 260 && !hit; i += 1) {
                st.demoMeterTick(tt); tt += 500;
                const snap = st.getPeak();
                if (snap.detected) hit = snap;
            }
            assert.ok(hit, 'a hot unprotected program must latch a peak hold');
            assert.ok(hit.last_over_at, 'a detected snapshot carries last_over_at');
            // Re-engage the limiter: capped peaks stay below 0 dBFS, so the
            // detector stops firing and the existing hold simply expires.
            await post('/api/dsp/extras', { limiter_enabled: true });
            st.demoMeterTick(tt); tt += 500;
            st.demoMeterTick(tt); tt += 500;
            const cleared = st.getPeak();
            assert.equal(cleared.detected, false, 'hold must expire once the peaks are capped');
            assert.equal(cleared.last_over_at, hit.last_over_at, 'last_over_at must survive the hold clearing');
            assert.equal(cleared.last_over_at_l, hit.last_over_at_l);
            assert.equal(cleared.last_over_at_r, hit.last_over_at_r);
        } finally { Math.random = realRandom; }
    }

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

    // ── Switch cycle (POST /api/music-libraries/select) ─────────────────
    // Switching to another share arms a short scan for the newly selected
    // catalog, so the UI shows "Scanning…" before the new tracks land.
    const switched = await (await ctx.fetch('/api/music-libraries/select', {
        method: 'POST',
        body: JSON.stringify({ id: 'local' }),
    })).json();
    assert.equal(switched.active_id, 'local');
    assert.ok(state.demoScan && state.demoScan.active,
        'library switch must arm a scan for the new catalog');
    assert.equal(state.demoScan.target, state.localTracks.length,
        'switch scan target must be the newly selected catalog');
    state.demoScan.durationMs = 0;
    const switchDone = await (await ctx.fetch('/api/library/status')).json();
    assert.equal(switchDone.scanning, false);
    assert.equal(switchDone.tracks_found, state.localTracks.length);
    // Selecting the already-active library is a no-op: no rescan.
    await ctx.fetch('/api/music-libraries/select', {
        method: 'POST',
        body: JSON.stringify({ id: 'local' }),
    });
    assert.equal(state.demoScan, null,
        'selecting the active library again must not re-arm a scan');

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