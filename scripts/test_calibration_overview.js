#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const overview = require('../static/calibration_overview.js');

const processing = (level, global = 0, bank = 0) => ({ level_db: level, level_cal_db: { global, bank } });

function catalog(mode, roles, subRoles, entries) {
    return { active_mode: mode, modes: { [mode]: { topology: { roles, sub_roles: subRoles }, processing: entries } } };
}

const text = html => html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

async function main() {
    // Surround: every routed speaker in position order, both slots summed, subs apart.
    const surround = catalog('surround',
        ['main_l', 'main_r', 'center', 'side_l', 'side_r', 'rear_l', 'rear_r', 'sub1', 'sub2'], ['sub1', 'sub2'], {
            main_l: processing(0, -4.8), main_r: processing(0, -5), center: processing(0, 0, -2.3),
            side_l: processing(0), side_r: processing(0), rear_l: processing(0), rear_r: processing(0, -1, -0.7),
            // A dormant role keeps its stored trim but is not routed.
            left_low: processing(0, -9),
            sub1: processing(6.697), sub2: processing(-3),
        });
    let summary = overview.summarize(surround, null, null);
    assert.deepEqual(summary.levels.map(item => item.label), ['FL', 'FR', 'C', 'SL', 'SR', 'RL', 'RR']);
    assert.deepEqual(summary.levels.map(item => item.minDb), [-4.8, -5, -2.3, 0, 0, 0, -1.7]);
    assert.deepEqual(summary.subs, [{ label: 'S1', db: 6.697 }, { label: 'S2', db: -3 }],
        'sub gains are the output levels, not Level Matching trims');
    assert.equal(text(overview.overviewHtml(summary)),
        'Level FL −4.8 FR −5.0 C −2.3 SL 0.0 SR 0.0 RL 0.0 RR −1.7 dB Sub S1 +6.7 S2 −3.0 dB Delay — SPL 83.0 dB default');
    assert.equal((overview.overviewHtml(summary).match(/is-zero/g) || []).length, 3, 'untrimmed speakers are dimmed');
    assert.equal(overview.overviewHtml(summary).match(/effects-cal-row/g).length, 3, 'speakers, subs and system values are one row each');

    // Stereo names its pair L/R and shows no sub group; a single sub is SUB.
    summary = overview.summarize(catalog('stereo', ['main_l', 'main_r'], [],
        { main_l: processing(0, -1.2), main_r: processing(0) }), null, null);
    assert.deepEqual(summary.levels.map(item => item.label), ['L', 'R']);
    assert.doesNotMatch(overview.overviewHtml(summary), />Sub</);
    assert.equal(overview.overviewHtml(summary).match(/effects-cal-row/g).length, 2);
    summary = overview.summarize(catalog('stereo-sub', ['main_l', 'main_r', 'sub_l'], ['sub_l'],
        { main_l: processing(0), main_r: processing(0), sub_l: processing(3.4) }), null, null);
    assert.deepEqual(summary.subs, [{ label: 'SUB', db: 3.4 }]);

    // The ways of a crossover side are one speaker; unequal trims show their range.
    summary = overview.summarize(catalog('stereo', ['left_low', 'left_high', 'right_low', 'right_high'], [], {
        left_low: processing(-4.5, -2), left_high: processing(1, -2),
        right_low: processing(0, -3), right_high: processing(0, -1),
    }), null, null);
    assert.deepEqual(summary.levels, [{ label: 'L', minDb: -2, maxDb: -2 }, { label: 'R', minDb: -3, maxDb: -1 }]);
    assert.match(text(overview.overviewHtml(summary)), /L −2\.0 R −3\.0…−1\.0 dB/);

    summary = overview.summarize(catalog('surround', ['center_low', 'center_mid', 'center_high'], [], {
        center_low: processing(0, 0, -2), center_mid: processing(-1, 0, -2), center_high: processing(-3, 0, -2),
    }), null, null);
    assert.deepEqual(summary.levels, [{ label: 'C', minDb: -2, maxDb: -2 }]);

    // Effective delay: the engine readback, a dash while it is not ready.
    assert.equal(overview.summarize(null, null, { latency: { ready: true, actual_ms: 753.32 } }).delayMs, 753.32);
    assert.equal(overview.summarize(null, null, { latency: { ready: false, actual_ms: 12 } }).delayMs, null);
    assert.match(overview.overviewHtml(overview.summarize(null, null, { latency: { ready: true, actual_ms: 753.32 } })),
        /effects-cal-badge">753\.3 ms</);

    // SPL: the stored measurement with its Loudness offset; the default until calibrated.
    const extras = calibration => ({ loudness: { params: { calibration } } });
    assert.deepEqual(overview.summarize(null, extras({}), null).spl, { db: 83, isDefault: true, offsetDb: null });
    assert.deepEqual(overview.summarize(null, extras({ measuredSplDb: 74, requiredAdjustmentDb: 9, calibrated: false }), null).spl,
        { db: 74, isDefault: false, offsetDb: 9 }, 'a stored measurement outside 1 dB is still calibrated');
    assert.match(text(overview.overviewHtml(overview.summarize(null, extras({ measuredSplDb: 74, requiredAdjustmentDb: 9 }), null))),
        /SPL 74\.0 dB \+9\.0 dB$/);

    // Rendering and the delay poll.
    const host = { innerHTML: '', textContent: '' };
    let writes = 0;
    const tracked = { get innerHTML() { return host.innerHTML; }, set innerHTML(value) { writes += 1; host.innerHTML = value; } };
    const state = { dsp: { available: true, global_extras: extras({}) }, outputSystem: { catalog: surround } };
    const timers = [];
    let visible = true;
    let fetches = 0;
    let response = { ok: true, json: async () => ({ latency: { ready: true, actual_ms: 20 } }) };
    overview.init({ getElements: () => ({ effectsInfo: tracked }), getState: () => state,
        isVisible: () => visible, fetch: async () => { fetches += 1; return response; },
        setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; }, clearTimeout() {} });

    overview.refresh();
    assert.equal(writes, 1, 'a refresh renders the known values at once');
    assert.match(host.innerHTML, /effects-cal-pending/);
    assert.equal(timers.at(-1).delay, 200, 'the delay is re-read shortly after a change');
    await overview.poll();
    assert.match(host.innerHTML, /20\.0 ms/);
    assert.equal(timers.at(-1).delay, 3000);
    await overview.poll();
    assert.equal(writes, 2, 'an unchanged poll leaves the nodes alone');

    state.outputSystem.catalog.modes.surround.processing.sub1.level_db = 1;
    overview.refresh();
    assert.match(text(host.innerHTML), /S1 \+1\.0/, 'changed settings show without waiting for the poll');

    const ready = response;
    response = { ok: false, json: async () => ({ detail: 'unavailable' }) };
    await overview.poll();
    assert.match(host.innerHTML, /effects-cal-pending/, 'a failed readback never shows a stale delay');
    response = { ok: true, json: async () => ({ latency: { ready: false } }) };
    await overview.poll();
    assert.match(host.innerHTML, /effects-cal-pending/);
    assert.equal(timers.at(-1).delay, 800, 'an engine that is not ready is retried sooner');

    // A poll in flight must not overwrite the re-read a refresh scheduled.
    let release;
    response = { ok: true, json: () => new Promise((resolve) => { release = () => resolve({ latency: { ready: true, actual_ms: 99 } }); }) };
    const inFlight = overview.poll();
    await Promise.resolve();
    await Promise.resolve();
    overview.refresh();
    const scheduled = timers.length;
    release();
    await inFlight;
    assert.doesNotMatch(host.innerHTML, /99\.0 ms/);
    assert.equal(timers.length, scheduled, 'the dropped poll schedules nothing');

    visible = false;
    fetches = 0;
    response = ready;
    await overview.poll();
    assert.equal(fetches, 0, 'a hidden tile does not poll');

    state.dsp.available = false;
    host.innerHTML = 'DSP is not available';
    overview.render();
    assert.equal(host.innerHTML, 'DSP is not available');

    // Page wiring: the tile hosts the overview and the preset count is gone.
    const root = path.join(__dirname, '..', 'static');
    const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    assert.match(html, /<div id="effects-info" class="effects-info effects-cal"/);
    assert.match(html, /<script src="\/static\/calibration_overview\.js\?v=\d+\.\d+\.\d+"><\/script>[\s\S]*<script src="\/static\/app\.js\?v=\d+\.\d+\.\d+">/);
    assert.doesNotMatch(fs.readFileSync(path.join(root, 'output_effects_ui.js'), 'utf8'), /preset_count/);
    console.log('ok    calibration overview');
}

main().catch((error) => { console.error(error); process.exit(1); });
