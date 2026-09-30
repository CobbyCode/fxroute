#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Exercise the real PEQ request builder and bank-local Combine rendering.
const assert = require('node:assert/strict');
const OutputState = require('../static/output_state.js');
const BankUI = require('../static/output_bank_ui.js');
const Effects = require('../static/output_effects_ui.js');

function element(overrides = {}) {
    return {
        value: '', innerHTML: '', textContent: '', disabled: false,
        classList: { toggle() {} }, querySelectorAll: () => [],
        addEventListener() {}, setAttribute() {}, focus() {}, ...overrides,
    };
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
    { name: 'Direct' }, { name: 'Neutral' }, { name: 'Legacy' },
    { name: 'Global PEQ', bank: 'global' }, { name: 'Main PEQ', bank: 'main' },
    { name: 'Sub 1 PEQ', bank: 'sub1' }, { name: 'Sub 2 PEQ', bank: 'sub2' },
];
const cases = [
    { bank: 'global', name: 'Global PEQ', revision: 7, visible: ['Direct', 'Neutral', 'Legacy', 'Global PEQ'] },
    { bank: 'main', name: 'Main PEQ', revision: 8, visible: ['Direct', 'Neutral', 'Main PEQ'] },
    { bank: 'sub1', name: 'Sub 1 PEQ', revision: 9, visible: ['Direct', 'Neutral', 'Sub 1 PEQ'] },
    { bank: 'sub2', name: 'Sub 2 PEQ', revision: 10, visible: ['Direct', 'Neutral', 'Sub 2 PEQ'] },
];
const state = {
    outputSystem: { catalog: { revision: 7, active_mode: 'stereo-sub', modes: {
        'stereo-sub': { selected_bank: 'global', banks, topology: { roles: ['main_l', 'main_r', 'sub1', 'sub2'] } },
    } } },
    dsp: { available: true, presets, active_preset: 'Neutral', peqDraft: Effects.getDefaultPeqDraft() },
};
const elements = {
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
globalThis.document = { activeElement: null, getElementById: () => null, querySelectorAll: () => [] };
BankUI.init({
    getState: () => state, getElements: () => elements,
    showToast: (message, type) => toasts.push({ message, type }),
    measurementArea: () => OutputState.measurementArea(state.outputSystem.catalog),
    fetchEffects: () => Effects.fetchEffects(),
});
Effects.init({
    getState: () => state, getElements: () => elements,
    showToast: (message, type) => toasts.push({ message, type }),
    measurementBankSumsBothInputs: () => OutputState.measurementArea(state.outputSystem.catalog).channel_mode === 'mono',
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
        assert.equal(body.loadAfterCreate, false);
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
    assert.match(elements.effectsStatus.innerHTML, /All Banks owns no presets/);
    assert.equal(requests.length, count, 'All Banks must not create or combine presets');
    assert.equal(inFlight, false);
    console.log('output PEQ bank creation and Combine: ok');
})().catch((error) => { console.error(error); process.exitCode = 1; }).finally(() => {
    globalThis.fetch = realFetch;
    globalThis.document = realDocument;
});
