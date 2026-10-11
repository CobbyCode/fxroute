#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const OutputState = require('../static/output_state.js');
const repoRoot = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');

function catalog() {
    return {
        status: 'ok',
        revision: 4,
        active_mode: 'stereo-sub',
        device: { key: 'A', channels: 4, routing: { 'stereo-sub': ['main_l', 'main_r', 'sub1', 'sub1'], stereo: [] } },
        modes: {
            'stereo-sub': {
                crossover_enabled: false,
                selected_bank: 'sub1',
                banks: {
                    global: { preset: 'Neutral', preset_a: 'Neutral', preset_b: null, active_side: 'A' },
                    main: { label: 'Main L/R', roles: ['main_l', 'main_r'], channel_mode: 'stereo', preset: 'Neutral', preset_a: 'Neutral', preset_b: null, active_side: 'A' },
                    sub1: { label: 'Sub 1', roles: ['sub1'], channel_mode: 'mono', preset: 'Room', preset_a: 'Room', preset_b: 'Room IR', active_side: 'A' },
                },
                processing: {},
                bass_management: { frequency_hz: 80, main_highpass_enabled: true },
                extras: {},
                topology: { mode: 'stereo-sub', crossover_enabled: false, roles: ['main_l', 'main_r', 'sub1'], sub_roles: ['sub1'],
                    sub_mode: 'mono', left_ways: [], right_ways: [], way_count: null, issues: [] },
            },
            stereo: {
                crossover_enabled: true,
                selected_bank: 'global',
                banks: { global: { preset: 'Neutral', preset_a: 'Neutral', preset_b: null, active_side: 'A' } },
                processing: {},
                bass_management: { frequency_hz: 80, main_highpass_enabled: true },
                extras: {},
                topology: { mode: 'stereo', crossover_enabled: true, roles: [], sub_roles: [], sub_mode: 'none',
                    left_ways: [], right_ways: [], way_count: null, issues: ['Crossover requires complete Low/High, Low/Mid/High, or Low/Low-Mid/Mid/High ways'] },
            },
        },
        capabilities: {
            modes: ['stereo', 'stereo-sub'],
            roles: { 'stereo-sub': ['main_l', 'main_r', 'sub_l', 'sub_r', 'sub1', 'sub2'],
                     stereo: ['left_low', 'left_low_mid', 'left_mid', 'left_high', 'right_low', 'right_low_mid', 'right_mid', 'right_high'] },
            filter_families: { 'linkwitz-riley': [12, 24, 36, 48, 60, 72], butterworth: [6, 12], bessel: [6, 12] },
            max_slope_db_oct: 72,
            max_biquads_per_output: 32,
        },
    };
}

assert.equal(OutputState.roleLabel('main_l'), 'Main L');
assert.equal(OutputState.roleLabel('sub_r'), 'Sub R');
assert.equal(OutputState.roleLabel('sub1'), 'Sub 1');
assert.equal(OutputState.roleLabel('left_low_mid'), 'Low-Mid L');
assert.equal(OutputState.roleLabel('right_high'), 'High R');
assert.equal(OutputState.roleLabel('global'), 'Global');
assert.equal(OutputState.roleLabel('off'), 'Off');

assert.deepEqual(OutputState.rolesForMode('stereo-sub', catalog().capabilities),
    ['main_l', 'main_r', 'sub_l', 'sub_r', 'sub1', 'sub2']);
assert.deepEqual(OutputState.rolesForMode('surround', catalog().capabilities), []);

assert.equal(OutputState.modeLabel('stereo'), 'Stereo');
assert.equal(OutputState.modeLabel('stereo-sub'), 'Stereo + Sub');

// 2-channel devices hide the Mode selector (internally fixed Stereo);
// 3+ channels show Stereo and Stereo + Sub.
assert.equal(OutputState.modeSelectorVisible(0), false);
assert.equal(OutputState.modeSelectorVisible(1), false);
assert.equal(OutputState.modeSelectorVisible(2), false);
assert.equal(OutputState.modeSelectorVisible(3), true);
assert.equal(OutputState.modeSelectorVisible(18), true);

// Output Routing is only meaningful with more than two hardware outputs:
// two outputs are fixed to Out 1 Main L / Out 2 Main R.
assert.equal(OutputState.routingVisible(0), false);
assert.equal(OutputState.routingVisible(1), false);
assert.equal(OutputState.routingVisible(2), false);
assert.equal(OutputState.routingVisible(3), true);
assert.equal(OutputState.routingVisible(18), true);

// Mode status line: Stereo + Sub without a routed sub points to the routing;
// the hint disappears as soon as a sub role is routed.
const noSubHint = 'No subwoofer configured — assign a Sub output below.';
assert.equal(OutputState.modeHint('stereo-sub', { sub_roles: [], sub_mode: 'none' }, 4), noSubHint);
assert.equal(OutputState.modeHint('stereo-sub', catalog().modes['stereo-sub'].topology, 4),
    'Stereo + Sub · 4 hardware outputs');
assert.equal(OutputState.modeHint('stereo', { sub_roles: [], sub_mode: 'none' }, 4),
    'Stereo · 4 hardware outputs');

assert.equal(OutputState.topologySummary(catalog().modes['stereo-sub'].topology), 'Stereo · Mono sub');
assert.equal(
    OutputState.topologySummary({ sub_mode: 'stereo', way_count: null, issues: [] }),
    'Stereo · Stereo subs');
assert.equal(
    OutputState.topologySummary({ sub_mode: 'dual-mono', way_count: null, issues: [] }),
    'Stereo · Dual-mono subs');
assert.equal(
    OutputState.topologySummary({ sub_mode: 'mono', way_count: 3, issues: [] }),
    '3-Way · Mono sub');
assert.equal(
    OutputState.topologySummary({ sub_mode: 'none', way_count: 2, issues: [] }),
    '2-Way');
assert.match(
    OutputState.topologySummary(catalog().modes.stereo.topology),
    /incomplete|requires/i);

assert.deepEqual(
    OutputState.bankOptions(catalog().modes['stereo-sub'], catalog().capabilities).map(o => o.id),
    ['global', 'all', 'main', 'sub1']);
assert.equal(
    OutputState.bankOptions(catalog().modes['stereo-sub'], catalog().capabilities)[3].label, 'Sub 1');

assert.equal(OutputState.compareState(catalog()).activePreset, 'Room');
assert.equal(OutputState.compareState(catalog()).presetB, 'Room IR');

assert.deepEqual(
    OutputState.buildMutation('set_routing', { mode: 'stereo-sub', assignments: ['main_l', 'main_r'] }),
    { kind: 'set_routing', mode: 'stereo-sub', assignments: ['main_l', 'main_r'] });
assert.deepEqual(
    OutputState.buildMutation('set_crossover', { mode: 'stereo-sub', enabled: true }),
    { kind: 'set_crossover', mode: 'stereo-sub', enabled: true });
assert.throws(() => OutputState.buildMutation('teleport', {}), /Unknown mutation kind/);

assert.deepEqual(OutputState.subwooferView(catalog()).roles, ['sub1']);
assert.equal(OutputState.subwooferView(catalog()).sub_mode, 'mono');

async function applyFlow() {
    const seen = [];
    const fetchImpl = async (url, options) => {
        seen.push(JSON.parse(options.body));
        if (seen.length === 1) {
            return { ok: false, status: 409,
                     json: async () => ({ detail: { code: 'revision-conflict', revision: 5 } }) };
        }
        return { ok: true, status: 200,
                 json: async () => ({ status: 'ok', revision: 6, live_applied: true }) };
    };
    let current = catalog();
    const fresh = { ...catalog(), revision: 5 };
    const getCatalog = async () => fresh;
    const result = await OutputState.applyMutation(fetchImpl, current, getCatalog,
        { kind: 'select_bank', mode: 'stereo-sub', bank_id: 'main_l' });
    assert.equal(seen.length, 2);
    assert.equal(seen[0].expected_revision, 4);
    assert.equal(seen[1].expected_revision, 5);
    assert.equal(result.data.revision, 6);

    const failing = async () => ({ ok: false, status: 409, json: async () => ({}) });
    await assert.rejects(
        OutputState.applyMutation(failing, current, getCatalog, { kind: 'set_crossover', mode: 'stereo-sub', enabled: true }),
        (error) => {
            assert.equal(error.status, 409);
            assert.equal(error.catalog.revision, 5);
            return true;
        });

    const calibrationPosts = [];
    const calibrationFetch = async (url, options) => {
        calibrationPosts.push(JSON.parse(options.body));
        return calibrationPosts.length === 1
            ? { ok: false, status: 409, json: async () => ({ detail: { message: 'Measure again', revision: 11 } }) }
            : { ok: true, status: 200, json: async () => ({ revision: 12 }) };
    };
    await assert.rejects(OutputState.applyMutation(calibrationFetch, { ...catalog(), revision: 11 },
        async () => ({ ...catalog(), revision: 11 }), { kind: 'set_level_calibration', mode: 'stereo',
            state_revision: 10, measurement_job_id: 'job-1', values: { main_l: { global: -4 } } }),
        error => error.status === 409);
    assert.equal(calibrationPosts.length, 1, 'Measurement results must never rebase');
    assert.equal(calibrationPosts[0].expected_revision, 10, 'Apply uses the measurement revision');

    const locked = async () => ({ ok: false, status: 423, json: async () => ({}) });
    await assert.rejects(OutputState.applyMutation(locked, current, getCatalog, { kind: 'switch_mode', mode: 'stereo-sub' }),
        /423/);
}

function measurementAreaTests() {
    const selected = catalog();
    const area = OutputState.measurementArea(selected);
    assert.deepEqual(area, {
        bank_id: 'sub1',
        label: 'Sub 1',
        channel: 'stereo',
        channel_mode: 'mono',
        available: true,
        sides: [],
        note: 'Only Sub 1 stays audible; every other output is muted for this sweep.',
        repeat_supported: false,
        repeat_note: 'Sub 1 is a mono target. Use a single sweep.',
        polarity_supported: false,
    });
    assert.equal(OutputState.measurementArea(selected, 'global').repeat_supported, true);
    assert.equal(OutputState.measurementArea(selected, 'global').channel, 'stereo');
    assert.deepEqual(OutputState.measurementArea(selected, 'global').sides, ['left', 'stereo', 'right']);
    assert.equal(OutputState.measurementArea(selected, 'main').channel, 'stereo');
    assert.equal(OutputState.measurementArea(selected, 'main').repeat_supported, true);
    assert.deepEqual(OutputState.measurementArea(selected, 'main').sides, ['left', 'stereo', 'right']);
    assert.deepEqual(OutputState.summedRoleIds({ sub_roles: ['sub_l'], sub_mode: 'mono' }), ['sub_l']);
    assert.deepEqual(OutputState.summedRoleIds({ sub_roles: ['sub_l', 'sub_r'], sub_mode: 'stereo' }), []);
    assert.deepEqual(OutputState.summedRoleIds({ sub_roles: [], sub_mode: 'none' }), []);
    assert.deepEqual(OutputState.summedRoleIds({}), []);
    for (const bankId of ['sub', 'sub_l', 'sub_r', 'sub1', 'sub2']) {
        assert.equal(OutputState.isSubwooferBank(bankId), true, bankId);
    }
    for (const bankId of ['global', 'all', 'main', 'low_mid', 'side', 'rear', 'front', 'center', '', null, undefined]) {
        assert.equal(OutputState.isSubwooferBank(bankId), false, String(bankId));
    }

    const crossover = { ...selected, active_mode: 'stereo' };
    crossover.modes.stereo.banks.low_mid = { label: 'Low-Mid L/R', roles: ['left_low_mid', 'right_low_mid'], channel_mode: 'stereo' };
    assert.equal(OutputState.measurementArea(crossover, 'low_mid').label, 'Low-Mid L/R');
    assert.equal(OutputState.measurementArea(crossover, 'low_mid').repeat_supported, true);
    assert.deepEqual(OutputState.measurementArea(crossover, 'low_mid').sides, ['left', 'stereo', 'right']);
    // Read-only: no bank id falls back to the whole system without inventing one.
    const empty = OutputState.measurementArea(null);
    assert.equal(empty.bank_id, 'global');
    assert.equal(empty.label, 'Global');
}

function allBanksAreaTests() {
    const withAll = (extra = {}, roles = ['main_l', 'main_r', 'sub1']) => ({ active_mode: 'stereo-sub', modes: { 'stereo-sub': {
        selected_bank: 'all', topology: { mode: 'stereo-sub', roles }, banks: {}, ...extra } } });
    const mixed = OutputState.measurementArea(withAll());
    assert.deepEqual(mixed, {
        bank_id: 'all', label: 'All Banks', channel: 'stereo', channel_mode: 'stereo', available: true,
        sides: ['left', 'stereo', 'right'],
        note: 'Whole system as listened (every bank on its A/B side); not importable.',
        repeat_supported: true, repeat_note: '', polarity_supported: false, importable: false,
    });
    // The joint A/B side the sweep will be heard on labels the area.
    assert.equal(OutputState.measurementArea(withAll({ all_banks: { active_side: 'B' } })).label, 'All Banks · B');
    assert.equal(OutputState.measurementArea(withAll({ all_banks: { active_side: null } })).label, 'All Banks');
    // Nothing routed: nothing to measure.
    const none = OutputState.measurementArea(withAll({}, []));
    assert.deepEqual([none.available, none.sides, none.note], [false, [], 'Select a filter bank for measurement.']);
    // Concrete areas never report importable.
    assert.equal('importable' in OutputState.measurementArea(catalog(), 'global'), false);
}

function onFrontBank(surround) {
    return { ...surround, modes: { surround: { ...surround.modes.surround, selected_bank: 'main' } } };
}

function surroundGlobalAreaTests() {
    const roles = ['main_l', 'main_r', 'center', 'rear_l', 'rear_r', 'sub1', 'sub2'];
    const surround = { active_mode: 'surround', revision: 4, modes: { surround: {
        selected_bank: 'global',
        topology: { mode: 'surround', roles, sub_roles: ['sub1', 'sub2'] },
        banks: { global: { label: 'Global', channel_mode: 'stereo' }, main: { label: 'Front L/R', channel_mode: 'stereo' },
            center: { label: 'Center', channel_mode: 'mono' }, rear: { label: 'Rear L/R', channel_mode: 'stereo' } },
    } } };
    assert.deepEqual(OutputState.globalGroupOptions(surround.modes.surround).map(option => [option.id, option.label, option.channel_mode]),
        [['front', 'Front L/R + Sub', 'stereo'], ['center', 'Center + Sub', 'mono'], ['rear', 'Rear L/R + Sub', 'stereo']]);
    assert.deepEqual(OutputState.globalGroupOptions({ topology: { mode: 'stereo-sub', roles: ['main_l', 'main_r'] } }), []);
    // No group yet, or one without a routed speaker, resolves to the first routed group.
    for (const wanted of [undefined, '', 'side', 'lfe']) {
        assert.equal(OutputState.resolveGlobalGroup(surround.modes.surround, wanted).id, 'front');
    }
    const front = OutputState.measurementArea(surround, undefined, 'front');
    assert.deepEqual(front, {
        bank_id: 'global', label: 'Global · Front L/R + Sub', channel: 'stereo', channel_mode: 'stereo',
        available: true, sides: ['left', 'stereo', 'right'],
        note: 'Only selected speakers and subs play.',
        repeat_supported: true, repeat_note: '', polarity_supported: false, group: 'front',
        side_labels: { left: 'Front L', stereo: 'Front L+R', right: 'Front R' },
    });
    const center = OutputState.measurementArea(surround, 'global', 'center');
    assert.deepEqual([center.group, center.channel_mode, center.sides, center.repeat_supported],
        ['center', 'mono', [], false]);
    assert.equal(center.repeat_note, 'Global · Center + Sub is a mono target. Use a single sweep.');
    const rear = OutputState.measurementArea(surround, 'global', 'rear');
    assert.equal(rear.label, 'Global · Rear L/R + Sub');
    // The side chips name the group's speaker; Center has no sides to name.
    assert.deepEqual(rear.side_labels, { left: 'Rear L', stereo: 'Rear L+R', right: 'Rear R' });
    assert.equal('side_labels' in center, false);
    assert.equal('side_labels' in OutputState.measurementArea(surround, 'main'), false);
    // The speaker choice exists only for Surround Global: every routed group, one in effect.
    const choice = OutputState.globalGroupChoice(surround, 'rear');
    assert.deepEqual(choice.options.map(option => [option.id, option.name, option.short]),
        [['front', 'Front L/R', 'Front'], ['center', 'Center', 'Center'], ['rear', 'Rear L/R', 'Rear']]);
    assert.equal(choice.selected, 'rear');
    assert.equal(OutputState.globalGroupChoice(surround, 'side').selected, 'front');
    assert.equal(OutputState.globalGroupChoice(onFrontBank(surround), 'rear'), null);
    assert.equal(OutputState.globalGroupChoice(catalog(), 'rear'), null);
    assert.equal(OutputState.globalGroupChoice(null, 'rear'), null);
    // Area banks and the other modes keep their exact shape (no group fields).
    assert.equal('group' in OutputState.measurementArea(surround, 'main', 'rear'), false);
    assert.equal('group' in OutputState.measurementArea(catalog(), 'global', 'rear'), false);
    // Without any routed speaker the Surround Global area is not measurable.
    const none = { ...surround, modes: { surround: { ...surround.modes.surround,
        topology: { mode: 'surround', roles: [], sub_roles: [] } } } };
    assert.equal(OutputState.measurementArea(none).available, false);
    // The create/import binding names the group only for Surround Global.
    assert.deepEqual(OutputState.bankBinding(surround, 'rear'),
        { bank_mode: 'surround', bank_id: 'global', expected_revision: 4, bank_group: 'rear' });
    assert.deepEqual(OutputState.bankBinding(surround, 'bogus'),
        { bank_mode: 'surround', bank_id: 'global', expected_revision: 4, bank_group: 'front' });
    const onFront = { ...surround, modes: { surround: { ...surround.modes.surround, selected_bank: 'main' } } };
    assert.deepEqual(OutputState.bankBinding(onFront, 'rear'),
        { bank_mode: 'surround', bank_id: 'main', expected_revision: 4 });
    assert.equal('bank_group' in OutputState.bankBinding(catalog(), 'rear'), false);
}

measurementAreaTests();
allBanksAreaTests();
surroundGlobalAreaTests();

applyFlow().then(() => {
    assert.match(indexSource, /output_state\.js\?v=\d+\.\d+\.\d+/);
    assert.match(indexSource, /id="settings-crossover-select"/);
    assert.match(indexSource, /id="settings-crossover-group"/);
    assert.match(indexSource, /id="settings-mode-group"/);
    assert.match(indexSource, /id="settings-routing-grid"/);
    assert.match(indexSource, /id="effects-bank-select"/);
    assert.doesNotMatch(indexSource, /id="os-mode-select"/);
    assert.doesNotMatch(indexSource, /id="os-routing-grid"/);
    // Routing UI shows only the topology status line: no dormant-role
    // note, no revision counter.
    assert.doesNotMatch(indexSource, /id="os-revision"/);
    assert.doesNotMatch(indexSource, /Roles not on any output/);
    // Settings order: Source before Music Library, Device Name directly
    // above Maintenance (Amplifier hidden), Maintenance last.
    const sectionOrder = ['<h3>Audio Output</h3>', '<h3>Source</h3>', '<h3>Music Library</h3>',
        '<h3>Device Name</h3>', '<h3>Maintenance</h3>'].map((h) => indexSource.indexOf(h));
    assert.ok(sectionOrder.every((pos) => pos >= 0), 'all settings sections present');
    assert.deepEqual([...sectionOrder].sort((a, b) => a - b), sectionOrder);
    assert.match(indexSource, /<section class="radio-manage-section settings-section hidden">\s*<div class="radio-manage-section-header">\s*<h3>Amplifier Controller<\/h3>/);
    // Audio Output field order: Device, Sample Rate, Mode, Crossover, Routing.
    const fieldOrder = ['for="settings-output-select"', 'for="settings-samplerate-select"',
        'for="settings-output-mode-select"', 'for="settings-crossover-select"',
        'id="settings-routing-label"'].map((m) => indexSource.indexOf(m));
    assert.ok(fieldOrder.every((pos) => pos >= 0), 'all audio output fields present');
    assert.deepEqual([...fieldOrder].sort((a, b) => a - b), fieldOrder);
    console.log('output-state frontend tests: ok');
}).catch((error) => { console.error(error); process.exitCode = 1; });
