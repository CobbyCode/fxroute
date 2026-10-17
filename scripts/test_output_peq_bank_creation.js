#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Exercise the real PEQ request builder and bank-local Combine rendering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const OutputState = require('../static/output_state.js');
require('../static/ui_helpers.js');
const BankUI = require('../static/output_bank_ui.js');
const Effects = require('../static/output_effects_ui.js');

function element(overrides = {}) {
    const classes = new Set();
    return {
        value: '', innerHTML: '', textContent: '', disabled: false,
        classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
            contains: (name) => classes.has(name) }, querySelectorAll: () => [],
        closest: () => null,
        addEventListener() {}, setAttribute() {}, focus() {}, ...overrides,
    };
}

function chipRow() {
    const row = element({ dataset: {}, listeners: {}, buttons: [],
        addEventListener(type, listener) { this.listeners[type] = listener; },
        querySelectorAll() { return this.buttons; } });
    let html = '';
    Object.defineProperty(row, 'innerHTML', {
        get: () => html,
        set(value) {
            html = value;
            row.buttons = [...value.matchAll(/<button ([^>]*)>([^<]*)<\/button>/g)].map(([, attrs, text]) => {
                const attributes = Object.fromEntries([...attrs.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
                const button = element({ textContent: text,
                    getAttribute: (name) => attributes[name],
                    setAttribute: (name, value) => { attributes[name] = value; } });
                return button;
            });
        },
    });
    return row;
}

function bandColumn(frequency) {
    const fields = { filterType: 'bell', frequencyHz: frequency, gainDb: 0, q: 1, delayMs: 0 };
    const band = { querySelector: (selector) => {
        const field = selector.match(/data-peq-field="([^"]+)"/)[1];
        return { value: String(fields[field]) };
    } };
    return element({ querySelectorAll: (selector) => selector === '[data-peq-band]' ? [band] : [] });
}

const banks = {
    global: { id: 'global', roles: ['global'], channel_mode: 'stereo' },
    main: { id: 'main', roles: ['main_l', 'main_r'], channel_mode: 'stereo' },
    sub1: { id: 'sub1', roles: ['sub1'], channel_mode: 'mono' },
    sub2: { id: 'sub2', roles: ['sub2'], channel_mode: 'mono' },
};
const presets = [
    { name: 'Direct' }, { name: 'Neutral' },
    { name: 'Global PEQ', bank: 'global', mode: 'stereo-sub' }, { name: 'Main PEQ', bank: 'main' },
    { name: 'Stereo PEQ', bank: 'global', mode: 'stereo' },
    { name: 'Sub 1 PEQ', bank: 'sub1' }, { name: 'Sub 2 PEQ', bank: 'sub2' },
];
const cases = [
    { bank: 'global', name: 'Global PEQ', revision: 7, visible: ['Direct', 'Neutral', 'Global PEQ'] },
    { bank: 'main', name: 'Main PEQ', revision: 8, visible: ['Neutral', 'Main PEQ'] },
    { bank: 'sub1', name: 'Sub 1 PEQ', revision: 9, visible: ['Neutral', 'Sub 1 PEQ'] },
    { bank: 'sub2', name: 'Sub 2 PEQ', revision: 10, visible: ['Neutral', 'Sub 2 PEQ'] },
];
const state = {
    measurement: { globalGroup: '' },
    outputSystem: { catalog: { revision: 7, active_mode: 'stereo-sub', modes: {
        'stereo-sub': { selected_bank: 'global', banks, topology: { roles: ['main_l', 'main_r', 'sub1', 'sub2'] } },
    } } },
    dsp: { available: true, presets, active_preset: 'Neutral', peqDraft: Effects.getDefaultPeqDraft() },
};
const elements = {
    effectsBankSelect: element({ closest: () => element() }),
    effectsImportGroupRow: chipRow(), effectsPeqGroupRow: chipRow(), effectsCombineGroupRow: chipRow(),
    effectsInfo: element(), effectsDeleteBtn: element(), effectsStatus: element(),
    effectsPeqPresetName: element(), effectsPeqModeSelect: element({ value: 'IIR' }),
    effectsPeqCreatePresetBtn: element(), effectsPeqDisclosure: element({ open: true }),
    effectsPeqLeftBands: bandColumn(100), effectsPeqRightBands: bandColumn(200),
    effectsCombinePreset1: element(), effectsCombinePreset2: element(), effectsCombinePreset3: element(),
    effectsCombinePresetName: element(), effectsCombineSaveBtn: element(),
};
const toasts = [];
const requests = [];
let inFlight = false;
const realFetch = globalThis.fetch;
const realDocument = globalThis.document;
const rightPane = element();
const leftLabel = element();
globalThis.document = { activeElement: null, querySelector: () => null, querySelectorAll: () => [],
    getElementById: (id) => id === 'effects-peq-left-bands-label' ? leftLabel
        : id === 'effects-peq-right-bands' ? { closest: () => rightPane } : null };
const area = () => OutputState.measurementArea(state.outputSystem.catalog, undefined, state.measurement.globalGroup);
BankUI.init({
    getState: () => state, getElements: () => elements,
    showToast: (message, type) => toasts.push({ message, type }),
    measurementArea: area,
    fetchEffects: () => Effects.fetchEffects(),
});
Effects.init({
    getState: () => state, getElements: () => elements,
    showToast: (message, type) => toasts.push({ message, type }),
    measurementBankSumsBothInputs: () => area().channel_mode === 'mono',
    isPeqCreateInFlight: () => inFlight, setPeqCreateInFlight: (value) => { inFlight = value; },
});
globalThis.fetch = async (url, options) => {
    if (url === '/api/dsp/presets') {
        return { ok: true, json: async () => ({ available: true, presets, preset_count: presets.length, active_preset: 'Neutral' }) };
    }
    assert.ok(['/api/dsp/presets/create-peq', '/api/dsp/presets/combine'].includes(url), `unexpected request: ${url}`);
    assert.equal(options.method, 'POST');
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    return { ok: true, json: async () => ({ preset: { name: body.presetName } }) };
};

function combineOptions() {
    return [elements.effectsCombinePreset1, elements.effectsCombinePreset2, elements.effectsCombinePreset3]
        .map((select) => [...select.innerHTML.matchAll(/<option value="([^"]+)"/g)].map((match) => match[1]));
}

(async () => {
    for (const entry of cases) {
        state.outputSystem.catalog.modes['stereo-sub'].selected_bank = entry.bank;
        state.outputSystem.catalog.revision = entry.revision;
        elements.effectsPeqPresetName.value = entry.name;
        elements.effectsPeqDisclosure.open = true;
        const before = requests.length;
        await Effects.createPeqPreset();
        assert.equal(requests.length, before + 1,
            `${entry.bank}: PEQ must reach the API without a JavaScript error; ${elements.effectsStatus.innerHTML}`);
        const { url, body } = requests.at(-1);
        assert.equal(url, '/api/dsp/presets/create-peq');
        assert.deepEqual({ bank_mode: body.bank_mode, bank_id: body.bank_id, expected_revision: body.expected_revision },
            { bank_mode: 'stereo-sub', bank_id: entry.bank, expected_revision: entry.revision });
        assert.equal(body.presetName, entry.name);
        assert.equal(body.peq.enabled, true);
        assert.equal(body.peq.params.eqMode, 'IIR');
        if (entry.bank === 'sub1' || entry.bank === 'sub2') {
            assert.equal(body.peq.params.channelMode, 'stereo-linked');
            assert.equal(body.peq.params.bands[0].frequencyHz, 100);
            assert.equal('rightBands' in body.peq.params, false);
        } else {
            assert.equal(body.peq.params.channelMode, 'dual');
            assert.equal(body.peq.params.leftBands[0].frequencyHz, 100);
            assert.equal(body.peq.params.rightBands[0].frequencyHz, 200);
        }
        assert.equal(inFlight, false);
        assert.equal(elements.effectsPeqCreatePresetBtn.disabled, false);
        assert.equal(elements.effectsPeqDisclosure.open, false);
        assert.equal(elements.effectsPeqPresetName.value, '');
        assert.equal(elements.effectsStatus.innerHTML, '');
        assert.equal(toasts.at(-1).type, 'success');
        assert.deepEqual(combineOptions(), [entry.visible, entry.visible, entry.visible], `${entry.bank}: all Combine slots stay bank-local`);

        elements.effectsCombinePreset1.value = entry.name;
        elements.effectsCombinePreset2.value = 'Neutral';
        elements.effectsCombinePresetName.value = `Combined ${entry.bank}`;
        await BankUI.createCombinedEffectsPreset();
        assert.deepEqual(requests.at(-1), { url: '/api/dsp/presets/combine', body: {
            presetName: `Combined ${entry.bank}`, presetNames: [entry.name, 'Neutral'],
            bank_mode: 'stereo-sub', bank_id: entry.bank,
        } });

        elements.effectsCombinePreset2.value = entry.bank === 'main' ? 'Global PEQ' : 'Main PEQ';
        const count = requests.length;
        assert.equal(BankUI.getEffectsCombineValidationState().crossBank, true);
        await BankUI.createCombinedEffectsPreset();
        assert.equal(requests.length, count, 'foreign presets must not be combined');
        elements.effectsCombinePreset1.value = '';
        elements.effectsCombinePreset2.value = '';
    }
    state.outputSystem.catalog.modes['stereo-sub'].selected_bank = 'all';
    BankUI.renderEffectsCombine();
    assert.deepEqual(combineOptions(), [[], [], []]);
    elements.effectsPeqPresetName.value = 'Aggregate PEQ';
    elements.effectsCombinePreset1.value = 'Direct';
    elements.effectsCombinePreset2.value = 'Neutral';
    elements.effectsCombinePresetName.value = 'Aggregate combined';
    const count = requests.length;
    await Effects.createPeqPreset();
    assert.deepEqual(toasts.at(-1), { message: 'Select a filter bank for import or measurement.', type: 'warning' });
    await BankUI.createCombinedEffectsPreset();
    assert.match(elements.effectsStatus.innerHTML, /Ind. Banks owns no presets/);
    assert.equal(requests.length, count, 'Ind. Banks must not create or combine presets');
    assert.equal(inFlight, false);

    // The workshop chips must choose the real request target, not a separate draft-only group.
    const neutral = { front: 'Neutral', center: 'Neutral', side: 'Neutral', rear: 'Neutral' };
    state.outputSystem.catalog = { revision: 20, active_mode: 'surround', modes: { surround: {
        selected_bank: 'global', topology: { mode: 'surround',
            roles: ['main_l', 'main_r', 'center', 'side_l', 'side_r', 'rear_l', 'rear_r', 'sub1'] },
        banks: { global: { ...banks.global, preset: neutral, preset_a: neutral, preset_b: null } },
    } } };
    BankUI.renderEffectsBankSelector();
    const row = elements.effectsPeqGroupRow;
    const chipIds = () => row.buttons.map(button => button.getAttribute('data-peq-group'));
    assert.deepEqual(chipIds(), ['front', 'center', 'side', 'rear'], 'PEQ offers every routed Global group');
    assert.equal(row.classList.contains('hidden'), false);
    const combineRow = elements.effectsCombineGroupRow;
    assert.deepEqual(combineRow.buttons.map(button => button.getAttribute('data-combine-group')),
        ['front', 'center', 'side', 'rear'], 'Combine offers every routed Global group');
    assert.deepEqual(combineRow.buttons.map(button => button.textContent), ['Front', 'Center', 'Side', 'Rear']);
    assert.equal(combineRow.classList.contains('hidden'), false);

    const appSource = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
    const clickBinding = /elements\.effectsPeqGroupRow\?\.addEventListener\('click', \(event\) => \{[\s\S]*?\n    \}\);/.exec(appSource);
    assert.ok(clickBinding, 'the PEQ group chips have a click binding');
    let measurementActive = false;
    new Function('elements', 'state', 'window', 'selectMeasurementSpeakerGroup', clickBinding[0])(
        elements, state, { FXRouteMeasurementJob: { hasActiveMeasurementJob: () => measurementActive } },
        (group) => BankUI.selectEffectsGroup(group));
    const combineBinding = /elements\.effectsCombineGroupRow\?\.addEventListener\('click', \(event\) => \{[\s\S]*?\n    \}\);/.exec(appSource);
    assert.ok(combineBinding, 'the Combine group chips have a click binding');
    new Function('elements', 'state', 'window', 'selectMeasurementSpeakerGroup', combineBinding[0])(
        elements, state, { FXRouteMeasurementJob: { hasActiveMeasurementJob: () => measurementActive } },
        (group) => BankUI.selectEffectsGroup(group));
    const combineClick = (group) => combineRow.listeners.click({ target: { closest: () => combineRow.buttons
        .find(button => button.getAttribute('data-combine-group') === group) } });
    const click = (group) => row.listeners.click({ target: { closest: () => row.buttons.find(button => button.getAttribute('data-peq-group') === group) } });
    for (const group of ['rear', 'center', 'side', 'front']) {
        click(group);
        assert.equal(state.measurement.globalGroup, group);
        assert.deepEqual(row.buttons.filter(button => button.getAttribute('aria-pressed') === 'true')
            .map(button => button.getAttribute('data-peq-group')), [group]);
        assert.deepEqual(elements.effectsImportGroupRow.buttons.filter(button => button.getAttribute('aria-pressed') === 'true')
            .map(button => button.getAttribute('data-import-group')), [group], 'import and PEQ share the choice');
        assert.equal(rightPane.classList.contains('hidden'), group === 'center');
        assert.equal(leftLabel.textContent, group === 'center' ? 'Mono bands' : 'Left bands');
        elements.effectsPeqPresetName.value = `${group} workshop`;
        await Effects.createPeqPreset();
        const body = requests.at(-1).body;
        assert.deepEqual([body.bank_mode, body.bank_id, body.bank_group, body.expected_revision],
            ['surround', 'global', group, 20]);
        assert.deepEqual(body.peq.params, group === 'center'
            ? { channelMode: 'stereo-linked', eqMode: 'IIR', bands: [{ filterType: 'bell', frequencyHz: 100, gainDb: 0, q: 1, delayMs: 0 }] }
            : { channelMode: 'dual', eqMode: 'IIR',
                leftBands: [{ filterType: 'bell', frequencyHz: 100, gainDb: 0, q: 1, delayMs: 0 }],
                rightBands: [{ filterType: 'bell', frequencyHz: 200, gainDb: 0, q: 1, delayMs: 0 }] });
    }
    presets.push({ name: 'Surround PEQ', bank: 'global', mode: 'surround', group: 'front' });
    for (const group of ['rear', 'center', 'side', 'front']) {
        combineClick(group);
        assert.equal(state.measurement.globalGroup, group);
        assert.deepEqual(combineRow.buttons.filter(button => button.getAttribute('aria-pressed') === 'true')
            .map(button => button.getAttribute('data-combine-group')), [group]);
        assert.deepEqual(row.buttons.filter(button => button.getAttribute('aria-pressed') === 'true')
            .map(button => button.getAttribute('data-peq-group')), [group], 'PEQ and Combine share the choice');
        elements.effectsCombinePreset1.value = 'Surround PEQ';
        elements.effectsCombinePreset2.value = 'Neutral';
        elements.effectsCombinePresetName.value = `${group} combined`;
        await BankUI.createCombinedEffectsPreset();
        assert.deepEqual(requests.at(-1), { url: '/api/dsp/presets/combine', body: {
            presetName: `${group} combined`, presetNames: ['Surround PEQ', 'Neutral'],
            bank_mode: 'surround', bank_id: 'global', bank_group: group,
        } });
    }
    state.measurement.startInFlight = true;
    click('rear');
    combineClick('rear');
    assert.equal(state.measurement.globalGroup, 'front', 'do not change group while a measurement starts');
    state.measurement.startInFlight = false;
    measurementActive = true;
    click('rear');
    combineClick('rear');
    assert.equal(state.measurement.globalGroup, 'front', 'do not change group during a measurement');
    measurementActive = false;
    state.outputSystem.catalog.modes.surround.topology.roles = ['center'];
    BankUI.renderEffectsBankSelector();
    assert.equal(row.classList.contains('hidden'), true, 'one routed group needs no choice');
    assert.equal(combineRow.classList.contains('hidden'), true);
    assert.deepEqual(BankUI.bankBindingJson().bank_group, 'center', 'a vanished selection falls back to the available group');
    assert.equal(rightPane.classList.contains('hidden'), true);
    state.outputSystem.catalog.modes.surround.topology.roles = ['main_l', 'main_r', 'rear_l', 'rear_r'];
    BankUI.renderEffectsBankSelector();
    assert.deepEqual(combineRow.buttons.map(button => button.getAttribute('data-combine-group')), ['front', 'rear'],
        'Combine excludes unrouted Center and Side');
    for (const selected of ['all', 'main']) {
        state.outputSystem.catalog.modes.surround.selected_bank = selected;
        BankUI.renderEffectsBankSelector();
        assert.equal(row.innerHTML, '', `${selected}: no Surround Global chips`);
        assert.equal(row.classList.contains('hidden'), true);
        assert.equal(combineRow.innerHTML, '', `${selected}: no Combine group chips`);
        assert.equal(combineRow.classList.contains('hidden'), true);
    }
    state.outputSystem.catalog = { revision: 21, active_mode: 'stereo-sub', modes: { 'stereo-sub': {
        selected_bank: 'global', banks, topology: { roles: ['main_l', 'main_r', 'sub1', 'sub2'] },
    } } };
    BankUI.renderEffectsBankSelector();
    assert.equal(row.innerHTML, '');
    assert.equal(row.classList.contains('hidden'), true);
    assert.equal(combineRow.innerHTML, '');
    assert.equal(combineRow.classList.contains('hidden'), true);
    assert.equal(rightPane.classList.contains('hidden'), false);
    assert.equal('bank_group' in BankUI.bankBindingJson(), false, 'the remembered group does not leak into other modes');
    console.log('output PEQ bank creation and Combine: ok');
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => {
    globalThis.fetch = realFetch;
    globalThis.document = realDocument;
});
