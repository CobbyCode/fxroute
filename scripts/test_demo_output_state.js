#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ctx = { console, Date, Math, URLSearchParams, FormData, setInterval() {}, clearInterval() {},
    fetch() { throw new Error('Unexpected real fetch'); } };
ctx.window = ctx;
vm.createContext(ctx);
for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js', 'data/measurements.js', 'state.js', 'routes.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'demo', file), 'utf8'), ctx);
}
async function rawRequest(url, body) {
    const response = await ctx.fetch(url, body ? { method: 'POST', body: JSON.stringify(body) } : {});
    return { status: response.status, data: JSON.parse(JSON.stringify(await response.json())) };
}
async function request(url, body) {
    const { status, data } = await rawRequest(url, body);
    assert.equal(status, 200);
    return data;
}
(async () => {
    const initial = await request('/api/audio/outputs');
    const scarlett = initial.outputs.find(o => o.label === 'Focusrite Scarlett 16i16');
    await request('/api/audio/outputs', { key: scarlett.key });
    const catalog = await request('/api/audio/output-state');
    assert.equal(catalog.revision, 1);
    assert.equal(catalog.active_mode, 'stereo');
    assert.deepEqual(Object.keys(catalog.modes.stereo.banks), ['global', 'main_l', 'main_r']);
    assert.equal(catalog.modes.stereo.topology.sub_mode, 'none');
    assert.equal(catalog.modes['stereo-sub'].topology.sub_mode, 'mono');
    assert.equal(catalog.modes['stereo-sub'].crossover_enabled, false);
    assert.ok(catalog.device.channels > 0);
    assert.ok(Array.isArray(catalog.device.routing['stereo-sub']));
    assert.deepEqual(catalog.capabilities.modes, ['stereo', 'stereo-sub']);

    // Unknown mode and unknown bank are refused without touching the revision.
    for (const mutation of [{ kind: 'switch_mode', mode: 'surround' },
        { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'left_low' },
        { kind: 'teleport' }]) {
        const refused = await rawRequest('/api/audio/output-state/apply', { expected_revision: 1, mutation });
        assert.equal(refused.status, 400);
    }
    assert.equal((await request('/api/audio/output-state')).revision, 1);

    // Mode switch + bank selection persist with revision guards.
    let result = await request('/api/audio/output-state/apply',
        { expected_revision: 1, mutation: { kind: 'switch_mode', mode: 'stereo-sub' } });
    assert.equal(result.revision, 2);
    assert.equal(result.active_mode, 'stereo-sub');
    result = await request('/api/audio/output-state/apply',
        { expected_revision: 2, mutation: { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'sub1' } });
    assert.equal(result.revision, 3);
    const stale = await rawRequest('/api/audio/output-state/apply',
        { expected_revision: 2, mutation: { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'main_l' } });
    assert.equal(stale.status, 409);

    // Routing edits create banks on demand and report derived topology.
    result = await request('/api/audio/output-state/apply',
        { expected_revision: 3,
          mutation: { kind: 'set_routing', mode: 'stereo-sub',
                      assignments: ['main_l', 'main_r', 'sub_l', 'sub_r'] } });
    assert.equal(result.revision, 4);
    const stereoAfter = (await request('/api/audio/output-state')).modes['stereo-sub'];
    assert.equal(stereoAfter.topology.sub_mode, 'stereo');
    assert.ok(stereoAfter.banks.sub_l);

    // Bank-bound preset creation assigns the target; conflicts keep the preset.
    const created = await request('/api/dsp/presets/create-peq',
        { presetName: 'Demo Bank EQ', peq: { params: {} }, bank_mode: 'stereo-sub', bank_id: 'sub1', expected_revision: 4 });
    assert.equal(created.bank.assigned, true);
    assert.equal(created.bank.revision, 5);
    const conflicted = await rawRequest('/api/dsp/presets/create-peq',
        { presetName: 'Demo Bank EQ 2', peq: { params: {} }, bank_mode: 'stereo-sub', bank_id: 'sub1', expected_revision: 4 });
    assert.equal(conflicted.status, 409);
    assert.equal(conflicted.data.bank.assigned, false);

    // Bank-pinned presets cannot be deleted.
    await request('/api/audio/output-state/apply',
        { expected_revision: 5, mutation: { kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'sub1', preset: 'Demo Bank EQ' } });
    const pinned = await rawRequest('/api/dsp/presets/delete', { preset_name: 'Demo Bank EQ' });
    assert.equal(pinned.status, 400);

    // Crossover is an independent switch: enabling it replaces Main with ways.
    await request('/api/audio/output-state/apply', { expected_revision: 6,
        mutation: { kind: 'set_crossover', mode: 'stereo-sub', enabled: true } });
    await request('/api/audio/output-state/apply', { expected_revision: 7,
        mutation: { kind: 'set_routing', mode: 'stereo-sub',
            assignments: ['left_low', 'right_low', 'sub_l', 'sub_r', 'left_high', 'right_high'] } });
    await request('/api/audio/output-state/apply', { expected_revision: 8,
        mutation: { kind: 'set_processing', mode: 'stereo-sub', role: 'left_low',
            lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 300 } } });
    const response = await request('/api/audio/output-state/crossover-response');
    assert.equal(response.mode, 'stereo-sub');
    assert.equal(response.crossover_enabled, true);
    const low = response.ways.left_low;
    assert.equal(low.complete, true);
    assert.ok(low.points.length > 100);
    const near = low.points.reduce((best, point) =>
        Math.abs(Math.log(point[0] / 300)) < Math.abs(Math.log(best[0] / 300)) ? point : best);
    assert.ok(Math.abs(near[1] + 6.02) < 0.3, `LR24 cutoff level: ${near[1]}`);
    assert.ok(low.points[0][1] > -0.5);
    console.log('Demo output-state flow passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
