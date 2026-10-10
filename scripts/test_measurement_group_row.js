#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// The sweep menu of the Measurement Assistant offers the Surround Global
// speaker choice (Front / Center / Side / Rear). It sets the same group as the
// Effects "Speaker" row, and the group reaches the area, the side chips and the
// start forms of Single Sweep, LR Repeat and Advanced.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const OutputState = require('../static/output_state.js');
const BankUI = require('../static/output_bank_ui.js');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');

// The bank UI reads the DOM only through optional lookups.
globalThis.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };

function classList() {
    const classes = new Set();
    return { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
        toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) };
}

// A row whose buttons are parsed from the markup it was given, so a rebuild
// (new button objects) differs from an in-place update.
function fakeRow() {
    const row = { classList: classList(), dataset: {}, listeners: {}, buttons: [], html: '',
        addEventListener(type, fn) { this.listeners[type] = fn; },
        querySelectorAll() { return this.buttons; } };
    Object.defineProperty(row, 'innerHTML', {
        get() { return this.html; },
        set(value) {
            this.html = value;
            this.buttons = [...value.matchAll(/<button ([^>]*)>([^<]*)<\/button>/g)].map(match => {
                const attrs = Object.fromEntries([...match[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
                const button = { attrs, textContent: match[2], classList: classList(), disabled: false,
                    getAttribute: name => attrs[name], setAttribute: (name, v) => { attrs[name] = String(v); } };
                if (/class="[^"]*is-active/.test(match[1])) button.classList.add('is-active');
                return button;
            });
        },
    });
    return row;
}

function catalogFor({ roles, selectedBank = 'global', mode = 'surround' }) {
    return { active_mode: mode, revision: 3, modes: { [mode]: {
        selected_bank: selectedBank,
        topology: { mode, roles, sub_roles: roles.filter(role => role.startsWith('sub')) },
        banks: { global: { label: 'Global', channel_mode: 'stereo' }, main: { label: 'Main L/R', channel_mode: 'stereo' },
            rear: { label: 'Rear L/R', channel_mode: 'stereo' } } } } };
}

const ALL_ROLES = ['main_l', 'main_r', 'center', 'rear_l', 'rear_r', 'sub1', 'sub2'];

function setup(catalog, measurement = {}) {
    const row = fakeRow();
    const state = { outputSystem: { catalog }, measurement: { globalGroup: '', allBanksGroup: '', ...measurement } };
    const elements = { measurementSweepGroupRow: row };
    BankUI.init({
        getState: () => state,
        getElements: () => elements,
        measurementArea: () => OutputState.measurementArea(state.outputSystem.catalog, undefined,
            state.outputSystem.catalog?.modes?.surround?.selected_bank === 'all'
                ? state.measurement.allBanksGroup : state.measurement.globalGroup),
        renderMeasurementArea: () => BankUI.renderSweepGroupRow(),
    });
    return { row, state, elements };
}

function chips(row) {
    return row.buttons.map(button => ({ id: button.attrs['data-sweep-group'],
        pressed: button.attrs['aria-pressed'] === 'true', active: button.classList.contains('is-active'),
        tooltip: button.attrs['data-tooltip'], label: button.attrs['aria-label'],
        disabled: button.disabled, text: button.textContent }));
}

// One chip per routed group, the effective one pressed; the tooltip carries the Effects wording.
{
    const { row } = setup(catalogFor({ roles: ALL_ROLES }));
    BankUI.renderSweepGroupRow();
    assert.equal(row.classList.contains('hidden'), false);
    assert.deepEqual(chips(row), [
        { id: 'front', pressed: true, active: true, tooltip: 'Front L/R + Sub', label: 'Front L/R + Sub', disabled: false, text: 'Front' },
        { id: 'center', pressed: false, active: false, tooltip: 'Center + Sub', label: 'Center + Sub', disabled: false, text: 'Center' },
        { id: 'rear', pressed: false, active: false, tooltip: 'Rear L/R + Sub', label: 'Rear L/R + Sub', disabled: false, text: 'Rear' },
    ]);
}

// Choosing updates the chips in place: a replaced button would read as an
// outside click and close the sweep menu (and drop keyboard focus).
{
    const { row, state } = setup(catalogFor({ roles: ALL_ROLES }));
    BankUI.renderSweepGroupRow();
    const before = [...row.buttons];
    state.measurement.globalGroup = 'rear';
    BankUI.renderSweepGroupRow();
    assert.equal(row.buttons.length, 3);
    row.buttons.forEach((button, index) => assert.equal(button, before[index], 'same button objects'));
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['rear']);
    assert.deepEqual(chips(row).filter(chip => chip.active).map(chip => chip.id), ['rear']);
    // A changed set of routed groups does rebuild the chips.
    state.outputSystem.catalog = catalogFor({ roles: ['main_l', 'main_r', 'center', 'sub1'] });
    BankUI.renderSweepGroupRow();
    assert.deepEqual(chips(row).map(chip => chip.id), ['front', 'center']);
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['front']);
}

// A remembered group is pressed; an unrouted one falls back to the first routed group.
{
    const { row, state } = setup(catalogFor({ roles: ALL_ROLES }), { globalGroup: 'rear' });
    BankUI.renderSweepGroupRow();
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['rear']);
    state.measurement.globalGroup = 'side';
    BankUI.renderSweepGroupRow();
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['front']);
}

// Nothing to choose, or a concrete bank: the row is hidden and empty.
for (const [label, catalog] of [
    ['one routed group', catalogFor({ roles: ['main_l', 'main_r', 'sub1'] })],
    ['one routed All Banks group', catalogFor({ roles: ['main_l', 'main_r', 'sub1'], selectedBank: 'all' })],
    ['area bank selected', catalogFor({ roles: ALL_ROLES, selectedBank: 'rear' })],
    ['other output mode', catalogFor({ roles: ['main_l', 'main_r', 'sub_l', 'sub_r'], mode: 'stereo-sub' })],
    ['no catalog yet', null],
]) {
    const { row } = setup(catalog);
    row.innerHTML = 'stale';
    BankUI.renderSweepGroupRow();
    assert.equal(row.classList.contains('hidden'), true, label);
    assert.equal(row.innerHTML, '', label);
}

// All Banks uses the same configured group chips and scope text as Global.
{
    const { row, state, elements } = setup(catalogFor({ roles: ALL_ROLES, selectedBank: 'all' }), { globalGroup: 'rear' });
    elements.effectsBankSelect = { innerHTML: '', value: '', closest: () => ({ classList: classList() }) };
    BankUI.renderSweepGroupRow();
    assert.equal(row.classList.contains('hidden'), false);
    assert.deepEqual(chips(row).map(chip => [chip.id, chip.text]),
        [['front', 'Front'], ['center', 'Center'], ['rear', 'Rear']]);
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['front']);
    const before = [...row.buttons];
    BankUI.selectEffectsGroup('front');
    assert.equal(state.measurement.allBanksGroup, 'front');
    assert.equal(state.measurement.globalGroup, 'rear', 'Global remembers its own group');
    row.buttons.forEach((button, index) => assert.equal(button, before[index]));
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['front']);
    state.outputSystem.catalog.modes.surround.all_banks = { active_side: 'B' };
    const area = OutputState.measurementArea(state.outputSystem.catalog, undefined, state.measurement.allBanksGroup);
    assert.deepEqual([area.bank_id, area.group, area.label, area.importable],
        ['all', 'front', 'All Banks · B · Front L/R + Sub', false]);
    assert.equal(area.polarity_supported, false);
    assert.equal(area.note, 'Only selected speakers and subs play.');
    BankUI.selectEffectsGroup('');
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['front']);
    assert.equal(state.measurement.globalGroup, 'rear');
    assert.equal(OutputState.measurementArea(state.outputSystem.catalog, undefined, '').group, 'front');
}

// Like Global, one group needs no row and vanished groups fall back to the first.
{
    const { row, state } = setup(catalogFor({ roles: ['main_l', 'main_r', 'sub1'], selectedBank: 'all' }),
        { allBanksGroup: 'rear' });
    BankUI.renderSweepGroupRow();
    assert.equal(row.classList.contains('hidden'), true);
    assert.deepEqual(chips(row), []);
    assert.equal(OutputState.measurementArea(state.outputSystem.catalog, undefined, 'rear').group, 'front');
    state.outputSystem.catalog = catalogFor({ roles: [], selectedBank: 'all' });
    BankUI.renderSweepGroupRow();
    assert.equal(row.classList.contains('hidden'), true);
    assert.equal(OutputState.measurementArea(state.outputSystem.catalog).available, false);
    state.outputSystem.catalog = catalogFor({ roles: ['sub1'], selectedBank: 'all' });
    assert.equal(OutputState.measurementArea(state.outputSystem.catalog).available, false, 'a group needs a speaker');
}

// Both banks expose the same group defaults, fallbacks, sides and descriptions.
for (const roles of [ALL_ROLES, ['center', 'sub1'], ['side_l', 'side_r', 'sub1'],
    ['rear_l', 'rear_r'], ['left_low', 'right_low', 'rear_l', 'rear_r', 'sub1']]) {
    for (const wanted of [undefined, '', 'front', 'center', 'side', 'rear', 'missing']) {
        const catalog = catalogFor({ roles, selectedBank: 'all' });
        const all = OutputState.measurementArea(catalog, undefined, wanted);
        const choice = OutputState.globalGroupChoice(catalog, wanted);
        catalog.modes.surround.selected_bank = 'global';
        const global = OutputState.measurementArea(catalog, undefined, wanted);
        assert.deepEqual(choice, OutputState.globalGroupChoice(catalog, wanted));
        for (const field of ['group', 'channel', 'channel_mode', 'available', 'sides', 'side_labels', 'note', 'repeat_supported']) {
            assert.deepEqual(all[field], global[field], field);
        }
    }
}

// While a sweep starts the chips are disabled, like the side chips.
{
    const { row } = setup(catalogFor({ roles: ALL_ROLES }), { startInFlight: true });
    BankUI.renderSweepGroupRow();
    assert.ok(chips(row).every(chip => chip.disabled));
}

// The click handler in app.js sets the shared group and refreshes the panel.
function clickHandler(overrides) {
    const match = /elements\.measurementSweepGroupRow\?\.addEventListener\('click', \(event\) => \{[\s\S]*?\n    \}\);\n/.exec(appSource);
    assert.ok(match, 'app.js must bind clicks on the sweep group row');
    const row = fakeRow();
    const calls = [];
    const env = {
        elements: { measurementSweepGroupRow: row },
        state: { measurement: { startInFlight: false } },
        window: {
            FXRouteBankUI: { selectEffectsGroup: group => calls.push(['group', group]) },
            FXRouteMeasurementJob: { hasActiveMeasurementJob: () => false },
        },
        renderMeasurementPanel: () => calls.push(['panel']),
        ...overrides,
    };
    new Function('elements', 'state', 'window', 'renderMeasurementPanel', match[0])(
        env.elements, env.state, env.window, env.renderMeasurementPanel);
    const chip = group => ({ target: { closest: () => ({ getAttribute: () => group }) } });
    return { fire: event => row.listeners.click(event), chip, calls, env };
}

{
    const { fire, chip, calls } = clickHandler();
    fire(chip('rear'));
    assert.deepEqual(calls, [['group', 'rear'], ['panel']]);
    calls.length = 0;
    fire({ target: { closest: () => null } });
    assert.deepEqual(calls, [], 'a click beside the chips does nothing');
}
{
    const { fire, chip, calls } = clickHandler({ state: { measurement: { startInFlight: true } } });
    fire(chip('rear'));
    assert.deepEqual(calls, [], 'no change while a sweep starts');
}
{
    const { fire, chip, calls } = clickHandler({ window: {
        FXRouteBankUI: { selectEffectsGroup: () => calls.push('group') },
        FXRouteMeasurementJob: { hasActiveMeasurementJob: () => true } } });
    fire(chip('rear'));
    assert.deepEqual(calls, [], 'no change while a measurement runs');
}

// The side chips name the group's speaker and fall back to the plain wording.
function syncSideRow(area, sweepSide = 'stereo') {
    const match = /function syncSweepSideRow\(area\) \{[\s\S]*?\n\}\n/.exec(appSource);
    assert.ok(match, 'app.js must define syncSweepSideRow');
    const buttons = ['left', 'stereo', 'right'].map(side => ({ side, textContent: side, classList: classList(),
        attrs: {}, disabled: false, getAttribute(name) { return name === 'data-sweep-side' ? side : this.attrs[name]; },
        setAttribute(name, value) { this.attrs[name] = value; } }));
    const row = { classList: classList(), querySelectorAll: () => buttons };
    const state = { measurement: { sweepSide, startInFlight: false } };
    new Function('elements', 'state', 'measurementAreaFromCatalog', 'MeasurementFlows', `${match[0]}; syncSweepSideRow(arguments[4]);`)(
        { measurementSweepSideRow: row }, state, () => area, { syncSpeakerAlignButton: () => {} }, area);
    return { buttons, row, state };
}

{
    const catalog = catalogFor({ roles: ALL_ROLES });
    const rear = syncSideRow(OutputState.measurementArea(catalog, undefined, 'rear'));
    assert.deepEqual(rear.buttons.map(button => button.textContent), ['Rear L', 'Rear L+R', 'Rear R']);
    assert.deepEqual(rear.buttons.map(button => button.attrs['aria-pressed']), ['false', 'true', 'false']);
    const front = syncSideRow(OutputState.measurementArea(catalog, undefined, 'front'), 'right');
    assert.deepEqual(front.buttons.map(button => button.textContent), ['Front L', 'Front L+R', 'Front R']);
    assert.equal(front.state.measurement.sweepSide, 'right');
    // Center is mono: no side chips, and a remembered side is reset.
    const center = syncSideRow(OutputState.measurementArea(catalog, undefined, 'center'), 'left');
    assert.equal(center.row.classList.contains('hidden'), true);
    assert.equal(center.state.measurement.sweepSide, 'stereo');
    // A bank area and Global in other modes keep the plain wording.
    const plain = syncSideRow(OutputState.measurementArea(catalog, 'rear'));
    assert.deepEqual(plain.buttons.map(button => button.textContent), ['Left', 'Stereo', 'Right']);
    const restored = syncSideRow(OutputState.measurementArea(catalogFor({ roles: ['main_l', 'main_r', 'sub_l', 'sub_r'], mode: 'stereo-sub' })));
    assert.deepEqual(restored.buttons.map(button => button.textContent), ['Left', 'Stereo', 'Right']);
}

// All Banks uses the identical per-group sides, including mono Center.
{
    const catalog = catalogFor({ roles: [...ALL_ROLES, 'side_l', 'side_r'], selectedBank: 'all' });
    for (const [group, labels] of [
        ['front', ['Front L', 'Front L+R', 'Front R']],
        ['side', ['Side L', 'Side L+R', 'Side R']],
        ['rear', ['Rear L', 'Rear L+R', 'Rear R']],
    ]) {
        const area = OutputState.measurementArea(catalog, undefined, group);
        assert.equal(area.group, group);
        assert.equal(area.repeat_supported, true);
        assert.deepEqual(syncSideRow(area).buttons.map(button => button.textContent), labels);
    }
    const area = OutputState.measurementArea(catalog, undefined, 'center');
    assert.equal(area.channel_mode, 'mono');
    assert.equal(area.repeat_supported, false);
    const center = syncSideRow(area, 'right');
    assert.equal(center.row.classList.contains('hidden'), true);
    assert.equal(center.state.measurement.sweepSide, 'stereo');
}

// The app reads the separate memories instead of leaking Global's group into All Banks.
{
    const match = /function measurementAreaFromCatalog\(\) \{[\s\S]*?\n\}\n/.exec(appSource);
    const catalog = catalogFor({ roles: ALL_ROLES, selectedBank: 'all' });
    const state = { outputSystem: { catalog }, measurement: { globalGroup: 'rear', allBanksGroup: '' } };
    const area = () => new Function('state', 'outputSystemModule', `${match[0]}; return measurementAreaFromCatalog();`)(
        state, () => OutputState);
    assert.equal(area().group, 'front');
    state.measurement.allBanksGroup = 'center';
    assert.equal(area().group, 'center');
    catalog.modes.surround.selected_bank = 'global';
    assert.equal(area().group, 'rear');
}

// One choice end to end: the shared setter re-renders the sweep chips, and the
// area and create/import binding follow it; A/B has no group selection.
{
    const { row, state, elements } = setup(catalogFor({ roles: ALL_ROLES }));
    const select = () => ({ innerHTML: '', value: '', disabled: false, closest: () => ({ classList: classList() }) });
    elements.effectsBankSelect = select();
    BankUI.renderEffectsBankSelector();
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['front']);
    BankUI.selectEffectsGroup('rear');
    assert.equal(state.measurement.globalGroup, 'rear');
    assert.deepEqual(chips(row).filter(chip => chip.pressed).map(chip => chip.id), ['rear']);
    const area = OutputState.measurementArea(state.outputSystem.catalog, undefined, state.measurement.globalGroup);
    assert.deepEqual([area.group, area.label, area.bank_id], ['rear', 'Global \u00b7 Rear L/R + Sub', 'global']);
    assert.equal(OutputState.bankBinding(state.outputSystem.catalog, state.measurement.globalGroup).bank_group, 'rear');
}

// The start forms carry the group of the area (Single Sweep, LR Repeat, Advanced).
for (const [file, pattern] of [
    ['measurement_capture.js', /if \(area\.group\) formData\.append\('measurement_group', area\.group\)/],
    ['measurement_capture.js', /if \(area\?\.group\) formData\.append\('measurement_group', area\.group\)/],
    ['measurement_flows.js', /if \(areaGroup\) formData\.append\('measurement_group', areaGroup\)/],
]) {
    assert.match(fs.readFileSync(path.join(root, 'static', file), 'utf8'), pattern, file);
}

// Shell: the group row sits in the sweep menu between the area line and the side chips.
assert.match(indexSource,
    /id="measurement-area-row"[\s\S]*?id="measurement-sweep-group-row"[^>]*role="group" aria-label="Speaker group"[\s\S]*?id="measurement-sweep-side-row"/);
assert.match(indexSource, /id="measurement-sweep-group-row"[^>]*class="[^"]*hidden|class="[^"]*hidden[^"]*" id="measurement-sweep-group-row"/);
assert.match(appSource, /measurementSweepGroupRow: document\.getElementById\('measurement-sweep-group-row'\)/);
assert.match(appSource, /window\.FXRouteBankUI\.renderSweepGroupRow\(\);\s*syncSweepSideRow\(area\);/,
    'the area render refreshes the group row before the side chips');

console.log('measurement group row: ok');
