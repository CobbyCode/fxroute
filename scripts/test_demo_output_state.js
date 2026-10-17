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
    assert.deepEqual(current.capabilities.modes, ['stereo', 'stereo-sub', 'surround']);
    // Capabilities follow each mode's own crossover switch.
    assert.ok(current.capabilities.roles['stereo-sub'].includes('left_high'));
    assert.ok(!current.capabilities.roles.stereo.includes('left_high'));
    // Surround adds the surrounds and the LFE pair; the stereo-sub front end
    // stays untouched.
    assert.ok(current.capabilities.roles.surround.includes('center'));
    assert.ok(current.capabilities.roles.surround.includes('sub1'));
    assert.ok(!current.capabilities.roles['stereo-sub'].includes('center'));
    for (const [mode, config] of Object.entries(current.modes)) {
        for (const [id, bank] of Object.entries(config.banks)) {
            if (id === 'global') continue;
            assert.equal([bank.preset, bank.preset_a, bank.preset_b].includes('Direct'), false, `${mode}/${id} has no Direct assignment`);
        }
    }

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

    for (const field of ['preset', 'preset_a', 'preset_b']) {
        await apply({ kind: 'set_bank_preset', bank_id: 'low', [field]: 'Direct' }, 400);
        assert.equal(current.revision, startRevision);
    }
    const beforeBuiltinCreate = JSON.stringify(await request('/api/dsp/presets'));
    for (const action of ['create-peq', 'create-with-ir', 'combine', 'import-json', 'import-bundle',
        'import-filter-dual', 'import-rew-peq']) {
        for (const name of ['Direct', 'Neutral', 'Direct.json']) {
            const response = await rawRequest(`/api/dsp/presets/${action}`, {
                presetName: name, preset_name: name, presetNames: ['Neutral'],
                bank_mode: 'stereo-sub', bank_id: 'low', expected_revision: revision,
            });
            assert.equal(response.status, 400, `${action} cannot create a built-in ${name}`);
        }
        const conflict = await rawRequest(`/api/dsp/presets/${action}`, {
            presetName: 'Custom', preset_name: 'Direct',
            bank_mode: 'stereo-sub', bank_id: 'low', expected_revision: revision,
        });
        assert.equal(conflict.status, 400, `${action} rejects a built-in through conflicting name fields`);
    }
    assert.equal((await request('/api/audio/output-state')).revision, revision);
    assert.equal(JSON.stringify(await request('/api/dsp/presets')), beforeBuiltinCreate);

    // A stale revision is refused and changes nothing.
    const beforeStale = JSON.stringify(current);
    assert.equal((await rawRequest('/api/audio/output-state/apply',
        { expected_revision: revision - 1, mutation: { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'low' } })).status, 409);
    assert.equal(JSON.stringify(await request('/api/audio/output-state')), beforeStale);

    // Unknown mode, unknown bank and an unknown kind are refused.
    for (const mutation of [{ kind: 'switch_mode', mode: 'atmos' },
        { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'no_such_bank' },
        { kind: 'teleport' }]) {
        const refused = await rawRequest('/api/audio/output-state/apply', { expected_revision: revision, mutation });
        assert.equal(refused.status, 400);
    }
    assert.equal((await request('/api/audio/output-state')).revision, revision);

    // A role id resolves to its owning bank, mirroring the backend's
    // resolve_bank; the catalog then reports the bank, not the role.
    await apply({ kind: 'select_bank', mode: 'stereo-sub', bank_id: 'left_low' });
    assert.equal(current.modes['stereo-sub'].selected_bank, 'low');
    await apply({ kind: 'select_bank', mode: 'stereo-sub', bank_id: 'global' });

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
    // A delete names the bank: Global has no preset of that name.
    assert.equal((await rawRequest('/api/dsp/presets/delete',
        { preset_name: 'Demo Bank EQ', bank_mode: 'stereo-sub', bank_id: 'global' })).status, 404);
    assert.equal((await rawRequest('/api/dsp/presets/delete',
        { preset_name: 'Demo Bank EQ', bank_mode: 'stereo-sub', bank_id: 'sub' })).status, 200);
    const afterDelete = await request('/api/audio/output-state');
    assert.equal(afterDelete.revision, revision + 1);
    revision = afterDelete.revision;
    const subBank = afterDelete.modes['stereo-sub'].banks.sub;
    assert.equal(subBank.preset, 'Neutral');
    assert.equal(subBank.preset_a, 'Neutral');
    assert.equal((await rawRequest('/api/dsp/presets/delete', { preset_name: 'Neutral' })).status, 400);

    // A preset name is unique within its bank only: Global and the Sub bank each keep their own.
    const owners = async (name) => (await request('/api/dsp/presets')).presets
        .filter(entry => entry.name === name).map(entry => `${entry.bank}/${entry.mode}`).sort();
    for (const bankId of ['sub', 'global']) {
        const shared = await request('/api/dsp/presets/create-peq',
            { presetName: 'Shared EQ', peq: { params: {} }, bank_mode: 'stereo-sub', bank_id: bankId, expected_revision: revision });
        assert.equal(shared.bank.assigned, true);
        revision = shared.bank.revision;
    }
    assert.deepEqual(await owners('Shared EQ'), ['global/stereo-sub', 'sub/null']);
    // Combine reads its sources from the bank it writes to.
    assert.equal((await rawRequest('/api/dsp/presets/combine', { presetName: 'Sub Combo',
        presetNames: ['Shared EQ', '+3'], bank_mode: 'stereo-sub', bank_id: 'sub' })).status, 404);
    assert.equal((await rawRequest('/api/dsp/presets/combine', { presetName: 'Global Combo',
        presetNames: ['Shared EQ', '+3'], bank_mode: 'stereo-sub', bank_id: 'global' })).status, 200);
    assert.deepEqual(await owners('Global Combo'), ['global/stereo-sub']);
    // Deleting the Sub bank's preset keeps Global's preset and Global's slot.
    assert.equal((await rawRequest('/api/dsp/presets/delete',
        { preset_name: 'Shared EQ', bank_mode: 'stereo-sub', bank_id: 'sub' })).status, 200);
    assert.deepEqual(await owners('Shared EQ'), ['global/stereo-sub']);
    const afterShared = await request('/api/audio/output-state');
    assert.equal(afterShared.modes['stereo-sub'].banks.sub.preset, 'Neutral');
    assert.equal(afterShared.modes['stereo-sub'].banks.global.preset, 'Shared EQ');
    assert.equal((await rawRequest('/api/dsp/presets/delete',
        { preset_name: 'Shared EQ', bank_mode: 'stereo-sub', bank_id: 'global' })).status, 200);
    assert.equal((await rawRequest('/api/dsp/presets/delete',
        { preset_name: 'Global Combo', bank_mode: 'stereo-sub', bank_id: 'global' })).status, 200);
    revision = (await request('/api/audio/output-state')).revision;

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
    // B starts Neutral, including banks whose A is also Neutral. An individual
    // switch is mixed until Ind. Banks switches every routed bank together.
    assert.equal(current.modes['stereo-sub'].banks.sub.preset_b, 'Neutral');
    assert.equal(current.modes['stereo-sub'].banks.low.preset_b, 'Neutral');
    await apply({ kind: 'set_bank_preset', bank_id: 'low', preset_b: 'Low B' });
    await apply({ kind: 'set_bank_preset', bank_id: 'low', active_side: 'B' });
    assert.equal(current.modes['stereo-sub'].all_banks.active_side, null);
    assert.equal(current.modes['stereo-sub'].banks.low.preset, 'Low B');
    assert.equal(current.modes['stereo-sub'].banks.sub.active_side, 'A');
    const globalBeforeSwitch = current.modes['stereo-sub'].banks.global;
    await apply({ kind: 'switch_all_banks', active_side: 'B' });
    const partial = current.modes['stereo-sub'];
    assert.equal(partial.banks.low.preset, 'Low B');
    for (const bankId of ['high', 'sub']) {
        assert.equal(partial.banks[bankId].preset, 'Neutral');
        assert.equal(partial.banks[bankId].active_side, 'B');
    }
    assert.deepEqual(partial.banks.global, globalBeforeSwitch);
    assert.equal(partial.all_banks.active_side, 'B');
    assert.equal(partial.all_banks.can_b, true);
    await apply({ kind: 'switch_all_banks', active_side: 'A' });
    assert.equal(current.modes['stereo-sub'].banks.low.preset, current.modes['stereo-sub'].banks.low.preset_a);
    for (const bankId of ['low', 'high', 'sub']) {
        assert.equal(current.modes['stereo-sub'].banks[bankId].active_side, 'A');
    }
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

    // Surround Global: a group filter fills its group of the listened slot,
    // an existing Neutral B stays independent, A/B switches the selection, and a delete
    // releases only the group holding the preset.
    const surroundApply = async (mutation) => {
        const state = await request('/api/audio/output-state');
        return request('/api/audio/output-state/apply',
            { expected_revision: state.revision, mutation: { mode: 'surround', ...mutation } });
    };
    await surroundApply({ kind: 'switch_mode' });
    await surroundApply({ kind: 'select_bank', bank_id: 'global' });
    const createGroup = async (presetName, bankGroup) => request('/api/dsp/presets/create-peq', {
        presetName, bank_mode: 'surround', bank_id: 'global', bank_group: bankGroup,
        expected_revision: (await request('/api/audio/output-state')).revision,
        peq: { params: { channelMode: 'stereo-linked', bands: [{ filterType: 'bell' }] } } });
    await createGroup('Front EQ', 'front');
    await createGroup('Rear EQ', 'rear');
    await createGroup('Rear EQ B', 'rear');
    const neutral = { front: 'Neutral', center: 'Neutral', side: 'Neutral', rear: 'Neutral' };
    const surroundGlobal = async () => (await request('/api/audio/output-state')).modes.surround.banks.global;
    assert.deepEqual((await surroundGlobal()).preset_a, { ...neutral, front: 'Front EQ', rear: 'Rear EQ B' });
    const groups = (await request('/api/dsp/presets')).presets.filter(entry => entry.mode === 'surround')
        .map(entry => [entry.name, entry.group]);
    assert.deepEqual(groups, [['Front EQ', 'front'], ['Rear EQ', 'rear'], ['Rear EQ B', 'rear']]);
    await surroundApply({ kind: 'set_bank_preset', bank_id: 'global', preset_b: 'Rear EQ', group: 'rear' });
    assert.deepEqual((await surroundGlobal()).preset_b, { ...neutral, rear: 'Rear EQ' });
    const same = await rawRequest('/api/audio/output-state/apply', { expected_revision: (await request('/api/audio/output-state')).revision,
        mutation: { kind: 'set_bank_preset', mode: 'surround', bank_id: 'global', preset_b: 'Rear EQ B', group: 'rear' } });
    assert.equal(same.status, 200, 'equal group assignments in A and B are allowed');
    assert.deepEqual((await surroundGlobal()).preset_b, { ...neutral, rear: 'Rear EQ B' });
    await surroundApply({ kind: 'set_bank_preset', bank_id: 'global', preset_b: 'Rear EQ', group: 'rear' });
    await surroundApply({ kind: 'set_bank_preset', bank_id: 'global', active_side: 'B' });
    assert.equal((await surroundGlobal()).active_side, 'B');
    assert.equal((await request('/api/dsp/presets')).active_preset, 'rear: Rear EQ');
    await request('/api/dsp/presets/delete', { preset_name: 'Front EQ', bank_mode: 'surround', bank_id: 'global' });
    const released = await surroundGlobal();
    assert.deepEqual([released.preset_a, released.preset_b, released.active_side],
        [{ ...neutral, rear: 'Rear EQ B' }, { ...neutral, rear: 'Rear EQ' }, 'B']);
    await surroundApply({ kind: 'set_bank_preset', bank_id: 'global', preset_b: 'Rear EQ B', group: 'rear' });
    const equalSlots = await surroundGlobal();
    assert.deepEqual(equalSlots.preset_a, equalSlots.preset_b);
    assert.equal(equalSlots.active_side, 'B', 'editing the listened B preserves B even when both slots match');
    // Combine stays within one speaker group.
    await createGroup('Front EQ 2', 'front');
    const mixed = await rawRequest('/api/dsp/presets/combine', { presetName: 'Both', presetNames: ['Rear EQ', 'Front EQ 2'],
        bank_mode: 'surround', bank_id: 'global' });
    assert.equal(mixed.status, 400);
    await request('/api/dsp/presets/combine', { presetName: 'Rears', presetNames: ['Rear EQ', 'Rear EQ B'],
        bank_mode: 'surround', bank_id: 'global' });
    assert.equal((await request('/api/dsp/presets')).presets.find(entry => entry.name === 'Rears').group, 'rear');

    console.log('Demo output-state flow passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
