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
for (const file of ['data/library.js', 'data/library2.js', 'data/library3.js', 'data/radio.js',
    'data/measurements.js', 'data/alignment.js', 'state.js', 'routes.js']) {
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
    const scarlett = initial.outputs.find(o => o.label === 'Focusrite Scarlett 16i16 4th Gen Pro');
    assert.ok(scarlett, 'the demo opens on the .104 interface');
    assert.equal(initial.selected_output.key, scarlett.key, 'the Scarlett is the default output');

    // ── The seeded .104 system ─────────────────────────────────────────
    let current = await request('/api/audio/output-state');
    const startRevision = current.revision;
    assert.ok(startRevision > 1, 'the seeded system carries a realistic revision');
    assert.equal(current.active_mode, 'stereo-sub');
    assert.equal(current.device.channels, 18);
    const seeded = current.modes['stereo-sub'];
    assert.equal(seeded.crossover_enabled, true);
    assert.equal(seeded.selected_bank, 'global');
    assert.equal(seeded.topology.way_count, 2);
    assert.equal(seeded.topology.sub_mode, 'dual-mono');
    assert.deepEqual(seeded.topology.issues, []);
    assert.deepEqual(Object.keys(seeded.banks), ['global', 'low', 'high', 'sub1', 'sub2']);
    assert.equal(seeded.bass_management.frequency_hz, 80);
    assert.equal(seeded.bass_management.main_highpass_enabled, true);
    assert.ok(Array.isArray(current.device.routing['stereo-sub']));
    assert.deepEqual(current.capabilities.modes, ['stereo', 'stereo-sub']);
    // Capabilities follow each mode's own crossover switch.
    assert.ok(current.capabilities.roles['stereo-sub'].includes('left_high'));
    assert.ok(!current.capabilities.roles.stereo.includes('left_high'));

    // Revisions are tracked relative to the seed, so the test does not hard
    // code the seeded number.
    let revision = current.revision;
    const apply = async (mutation, expect = 200) => {
        const response = await rawRequest('/api/audio/output-state/apply',
            { expected_revision: revision, mutation: { mode: 'stereo-sub', ...mutation } });
        assert.equal(response.status, expect, `${JSON.stringify(mutation)} -> ${response.status} ${JSON.stringify(response.data)}`);        if (expect === 200) revision = response.data.revision;
        current = await request('/api/audio/output-state');
        return response;
    };

    // A stale revision is refused and changes nothing.
    const beforeStale = JSON.stringify(current);
    assert.equal((await rawRequest('/api/audio/output-state/apply',
        { expected_revision: revision - 1, mutation: { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'low' } })).status, 409);
    assert.equal(JSON.stringify(await request('/api/audio/output-state')), beforeStale);

    // Unknown mode, unknown bank and an unknown kind are refused.
    for (const mutation of [{ kind: 'switch_mode', mode: 'surround' },
        { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'left_low' },
        { kind: 'teleport' }]) {
        const refused = await rawRequest('/api/audio/output-state/apply', { expected_revision: revision, mutation });
        assert.equal(refused.status, 400);
    }
    assert.equal((await request('/api/audio/output-state')).revision, revision);

    // Bank selection persists on the seeded system.
    const selected = await apply({ kind: 'select_bank', mode: 'stereo-sub', bank_id: 'sub1' });
    assert.equal(selected.data.revision, revision);
    assert.equal(current.modes['stereo-sub'].selected_bank, 'sub1');
    await apply({ kind: 'select_bank', mode: 'stereo-sub', bank_id: 'global' });

    // Crossover is an independent switch: turning it off brings Main back.
    await apply({ kind: 'set_crossover', mode: 'stereo-sub', enabled: false });
    await apply({ kind: 'set_routing', mode: 'stereo-sub',
        assignments: ['main_l', 'main_r', 'sub_l', 'sub_r'] });
    const flat = current.modes['stereo-sub'];
    assert.equal(flat.crossover_enabled, false);
    assert.equal(flat.topology.sub_mode, 'stereo');
    assert.deepEqual(flat.banks.sub.roles, ['sub_l', 'sub_r']);
    assert.equal(flat.topology.way_count, null);
    assert.equal(Object.keys(flat.banks).includes('low'), false, 'no way banks without the crossover');

    // Bank-bound preset creation assigns the target; conflicts keep the preset.
    const created = await request('/api/dsp/presets/create-peq',
        { presetName: 'Demo Bank EQ', peq: { params: {} }, bank_mode: 'stereo-sub', bank_id: 'sub', expected_revision: revision });
    assert.equal(created.bank.assigned, true);
    assert.equal(created.bank.revision, revision + 1);
    revision += 1;
    const conflicted = await rawRequest('/api/dsp/presets/create-peq',
        { presetName: 'Demo Bank EQ 2', peq: { params: {} }, bank_mode: 'stereo-sub', bank_id: 'sub', expected_revision: revision - 1 });
    assert.equal(conflicted.status, 409);
    assert.equal(conflicted.data.bank.assigned, false);

    // Deleting a bank preset releases its slots like Global's compare.
    await apply({ kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'sub', preset: 'Demo Bank EQ' });
    assert.equal((await rawRequest('/api/dsp/presets/delete', { preset_name: 'Demo Bank EQ' })).status, 200);
    const afterDelete = await request('/api/audio/output-state');
    assert.equal(afterDelete.revision, revision + 1);
    revision = afterDelete.revision;
    const subBank = afterDelete.modes['stereo-sub'].banks.sub;
    assert.equal(subBank.preset, 'Neutral');
    assert.equal(subBank.preset_a, 'Neutral');
    assert.equal((await rawRequest('/api/dsp/presets/delete', { preset_name: 'Neutral' })).status, 400);

    // Turn the crossover back on and route a complete stereo sub pair with
    // two ways per side; that is the shape the Crossover card graphs.
    await apply({ kind: 'set_crossover', mode: 'stereo-sub', enabled: true });
    await apply({ kind: 'set_routing', mode: 'stereo-sub',
        assignments: ['left_low', 'right_low', 'sub_l', 'sub_r', 'left_high', 'right_high'] });
    await apply({ kind: 'set_processing', mode: 'stereo-sub', role: 'left_low',
        lowpass: { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 300 } });
    const response = await request('/api/audio/output-state/crossover-response');
    assert.equal(response.mode, 'stereo-sub');
    assert.equal(response.crossover_enabled, true);
    assert.deepEqual(response.sub_roles, ['sub_l', 'sub_r']);
    assert.equal(response.bass_management.frequency_hz, 80);
    const low = response.ways.left_low;
    assert.equal(low.complete, true);
    assert.deepEqual(low.derived_highpass,
        { family: 'linkwitz-riley', slope_db_oct: 24, frequency_hz: 80 });
    assert.equal(low.filters.highpass, null);
    assert.ok(low.points.length > 100);
    const near = low.points.reduce((best, point) =>
        Math.abs(Math.log(point[0] / 300)) < Math.abs(Math.log(best[0] / 300)) ? point : best);
    assert.ok(Math.abs(near[1] + 6.02) < 0.3, `LR24 cutoff level: ${near[1]}`);
    // Routed subs with Main highpass on: the running Low curve carries the
    // sub-owned 80 Hz LR24 high-pass, so 20 Hz is deeply attenuated.
    assert.ok(low.points[0][1] < -20, `sub HPF shapes low bottom: ${low.points[0][1]}`);
    // Every routed way also runs the sub crossover, so the high way carries it too.
    assert.equal(response.ways.left_high.derived_highpass.frequency_hz, 80);

    // Compare slots per way bank, and the all-banks switch. Bank order
    // follows the routed roles, so the sub pair lands last.
    assert.deepEqual(Object.keys(current.modes['stereo-sub'].banks), ['global', 'low', 'high', 'sub']);
    // The routed sub pair carries no B slot yet: All Banks still switches the
    // ways that have one and keeps the sub pair on A.
    assert.equal(current.modes['stereo-sub'].banks.sub.preset_b, null);
    assert.ok(current.modes['stereo-sub'].banks.low.preset_b, 'the seeded ways carry a B slot');
    await apply({ kind: 'switch_all_banks', active_side: 'B' });
    const partial = current.modes['stereo-sub'];
    assert.equal(partial.banks.low.preset, partial.banks.low.preset_b);
    assert.equal(partial.banks.sub.preset, partial.banks.sub.preset_a);
    assert.equal(partial.all_banks.active_side, 'B');
    assert.equal(partial.all_banks.can_b, true);
    await apply({ kind: 'switch_all_banks', active_side: 'A' });
    assert.equal(current.modes['stereo-sub'].banks.low.preset, current.modes['stereo-sub'].banks.low.preset_a);
    await apply({ kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'low', preset_b: 'Low B' });
    await apply({ kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'high', preset_b: 'High B' });
    await apply({ kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'sub', preset_b: 'Sub B' });
    await apply({ kind: 'select_bank', mode: 'stereo-sub', bank_id: 'all' });
    assert.equal((await apply({ kind: 'switch_all_banks', active_side: 'B' })).status, 200);
    const mode = current.modes['stereo-sub'];
    assert.equal(mode.all_banks.active_side, 'B');
    assert.equal(mode.banks.global.preset, 'Neutral');
    assert.equal(mode.banks.low.preset, 'Low B');
    assert.equal(mode.banks.high.preset, 'High B');
    assert.equal(mode.banks.sub.preset, 'Sub B');
    assert.equal(mode.banks.all, undefined);

    // Stereo mode stays a plain main pair: crossover off, no way banks.
    const stereoMode = current.modes.stereo;
    assert.equal(stereoMode.crossover_enabled, false);
    assert.equal(stereoMode.selected_bank, 'global');
    assert.deepEqual(Object.keys(stereoMode.banks), ['global', 'main']);
    assert.equal(stereoMode.topology.sub_mode, 'none');
    assert.equal(stereoMode.topology.way_count, null);

    console.log('Demo output-state flow passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
