#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the output bank UI module: bank selection,
// A/B compare binding and the bank-bound import/export UI keep their exact
// behavior; catalog/mutation control stays in output_system_controller.js
// and domain logic in output_state.js. app.js keeps thin wrappers.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
require('../static/output_state.js');
require('../static/api.js');
const BankUI = require('../static/output_bank_ui.js');

// Module boundary: canonical owner plus app.js delegation and script order.
for (const name of ['renderEffectsBankSelector', 'renderEffectsCompare', 'toggleComparePreset',
    'submitEffectsImport', 'createDualFilterPreset', 'deleteEffectsPreset',
    'outputSystemBankBinding', 'bankBindingJson', 'wireBankUi']) {
    assert.equal(typeof BankUI[name], 'function', `bank module must own ${name}`);
    assert.doesNotMatch(appSource, new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `shim dropped from app.js: ${name}`);
}
assert.match(appSource, /window\.FXRouteBankUI\.renderEffectsBankSelector\(/, 'bank calls point at the module directly');
assert.match(appSource, /window\.FXRouteBankUI\.renderEffectsCompare\(/, 'compare calls point at the module directly');
assert.match(appSource, /FXRouteBankUI/, 'app.js delegates bank UI to the module');
assert.match(indexSource, /output_bank_ui\.js\?v=\d+\.\d+\.\d+/);
assert.ok(indexSource.indexOf('output_bank_ui.js') < indexSource.indexOf('/static/app.js'),
    'bank module must load before app.js');
// Editors stay out of the bank module: the PEQ editor and the main effects
// fetch/render live in output_effects_ui.js now.
const effectsSource = fs.readFileSync(path.join(repoRoot, 'static', 'output_effects_ui.js'), 'utf8');
assert.match(effectsSource, /function createPeqPreset\(/, 'PEQ editor lives in the effects module');
assert.match(effectsSource, /function renderEffects\(/, 'main effects render lives in the effects module');
assert.match(effectsSource, /function fetchEffects\(/, 'effects fetch lives in the effects module');
assert.doesNotMatch(appSource, /\n(?:async function|function) fetchEffects\(/, 'effects fetch no longer in app.js');

function stubClassList() {
    const set = new Set();
    return { add: (c) => set.add(c), remove: (c) => set.delete(c),
        toggle: (c, force) => { if (force === undefined) { if (set.has(c)) set.delete(c); else set.add(c); } else if (force) set.add(c); else set.delete(c); },
        contains: (c) => set.has(c) };
}

function stubEl(overrides = {}) {
    return { value: '', textContent: '', innerHTML: '', disabled: false, checked: false, style: {},
        dataset: {}, files: [], accept: '', placeholder: '', classList: stubClassList(),
        setAttribute() {}, addEventListener() {}, focus() {}, select() {},
        closest: () => null, ...overrides };
}

const nodes = {};
const documentStub = {
    getElementById: (id) => nodes[id] || (nodes[id] = stubEl()),
    querySelector: (sel) => nodes[sel] || (nodes[sel] = stubEl()),
    querySelectorAll: () => [],
};
globalThis.document = documentStub;

function bankCatalog(selectedBank = 'main') {
    const bank = (id, label) => ({ id, label, preset: 'Neutral', preset_a: 'Neutral',
        preset_b: null, active_side: 'A', can_a: true, can_b: false });
    return { revision: 7, active_mode: 'stereo-sub',
        modes: { 'stereo-sub': { selected_bank: selectedBank,
            banks: { global: bank('global', 'Global'), main: bank('main', 'Main L/R') },
            topology: { roles: ['main_l', 'main_r'] } } } };
}

function bankElements() {
    return {
        effectsBankSelect: stubEl({ value: 'main' }),
        effectsCompareRow: stubEl(),
        effectsCompareA: stubEl(),
        effectsCompareB: stubEl(),
        effectsCompareActive: stubEl(),
        effectsCompareChain: stubEl(),
        effectsCompareToggle: stubEl(),
        effectsComparePresetName: stubEl(),
        effectsCombinePreset1: stubEl(),
        effectsCombinePreset2: stubEl(),
        effectsCombinePreset3: stubEl(),
        effectsCombinePresetName: stubEl(),
        effectsCombineSaveBtn: stubEl(),
        effectsDeleteBtn: stubEl(),
        effectsToggleImportBtn: stubEl(),
        effectsImportPanel: stubEl(),
        effectsImportFile: stubEl(),
        effectsImportFilename: stubEl(),
        effectsRewLeftText: stubEl(),
        effectsRewLeftFile: stubEl(),
        effectsRewRightText: stubEl(),
        effectsRewRightFile: stubEl(),
        effectsRewDualPresetName: stubEl(),
        effectsRewDualCreatePresetBtn: stubEl(),
        effectsStatus: stubEl(),
    };
}

function initBankUI(state, elements, extra = {}) {
    BankUI.init({ getState: () => state, getElements: () => elements,
        showToast: () => {}, escapeHtml: (value) => String(value),
        fetchEffects: async () => {}, refreshCatalog: async () => null,
        collectEffectsExtras: () => ({}),
        measurementBankSumsBothInputs: () => false,
        presetFileUrl: (name) => `/api/presets/file/${name}`,
        renderEffects: () => {}, renderMeasurementArea: () => {},
        measurementArea: () => null, applyMutation: async () => null,
        confirmDialog: () => true, ...extra });
}

const realFetch = globalThis.fetch;

(async () => {
    // Bank binding carries mode, bank and the committed revision.
    {
        const state = { outputSystem: { catalog: bankCatalog('main') }, dsp: {} };
        initBankUI(state, {});
        assert.deepEqual(BankUI.outputSystemBankBinding(),
            { bank_mode: 'stereo-sub', bank_id: 'main', expected_revision: 7 });
        assert.deepEqual(BankUI.bankBindingJson().bank_id, 'main');
    }

    // Bank selector follows the catalog and locks while busy.
    {
        const state = { outputSystem: { catalog: bankCatalog('main'), busy: true }, dsp: {} };
        const elements = bankElements();
        initBankUI(state, elements);
        BankUI.renderEffectsBankSelector();
        assert.equal(elements.effectsBankSelect.value, 'main');
        assert.equal(elements.effectsBankSelect.disabled, true);
    }

    // Compare toggle target walks A -> B -> A across the bank state.
    {
        const state = { outputSystem: { catalog: bankCatalog('main') },
            dsp: { presets: [{ name: 'Neutral' }, { name: 'Room' }], compare: {}, active_preset: '' } };
        initBankUI(state, bankElements());
        const idle = BankUI.getEffectsCompareToggleTarget(
            { effectiveActiveSide: null, activePreset: '', presetA: 'Neutral', presetB: '' });
        assert.deepEqual(idle, { target: 'Neutral', side: 'A' });
    }

    // Combine validation rejects aggregates, cross-bank picks and dupes.
    {
        const state = { outputSystem: { catalog: bankCatalog('all') }, dsp: {} };
        const elements = bankElements();
        initBankUI(state, elements);
        const checked = BankUI.getEffectsCombineValidationState();
        assert.equal(checked.aggregate, true);
        assert.equal(checked.isValid, false);
        await BankUI.createCombinedEffectsPreset();
        assert.match(elements.effectsStatus.innerHTML, /All Banks owns no presets/);
    }

    // Legacy compare load (no catalog) posts through the preset endpoint.
    {
        const posted = [];
        const toasts = [];
        const state = { outputSystem: { catalog: null },
            dsp: { presets: [], compare: {}, active_preset: '' } };
        const elements = bankElements();
        initBankUI(state, elements, { showToast: (message, type) => toasts.push([message, type]) });
        globalThis.fetch = async (url, options) => {
            posted.push([url, JSON.parse(options.body || '{}')]);
            return { ok: true, json: async () => ({}) };
        };
        await BankUI.loadEffectsComparePreset('Room', 'B', 'Neutral', 'Room');
        globalThis.fetch = realFetch;
        assert.ok(posted.some(([url, body]) => url === '/api/dsp/presets/load' && body.preset_name === 'Room'));
        assert.equal(state.dsp.active_preset, 'Room');
    }

    // Import routing: unknown suffixes stay out with a plain error.
    {
        const toasts = [];
        const state = { outputSystem: { catalog: bankCatalog('main') }, dsp: {} };
        const elements = bankElements();
        elements.effectsImportFile.files = [{ name: 'notes.txt' }];
        initBankUI(state, elements, { showToast: (message, type) => toasts.push([message, type]) });
        await BankUI.submitEffectsImport();
        assert.match(elements.effectsStatus.innerHTML, /Unsupported import file type/);
        assert.deepEqual(toasts.at(-1), ['Unsupported import file type', 'error']);
    }
    assert.equal(BankUI.detectEffectsImportType({ name: 'a.irs' }), 'convolver');
    assert.equal(BankUI.detectEffectsImportType({ name: 'a.WAV' }), 'convolver');
    assert.equal(BankUI.detectEffectsImportType({ name: 'a.json' }), 'preset-json');
    assert.equal(BankUI.detectEffectsImportType({ name: 'a.zip' }), 'preset-bundle');
    assert.equal(BankUI.detectEffectsImportType({ name: 'a.txt' }), null);
    assert.equal(BankUI.detectEffectsImportType(null), null);

    // Delete follows the selected bank's active preset exactly like Global,
    // not the legacy DSP active preset; All Banks has no Delete.
    {
        const bank = (id, preset, preset_a, preset_b) => ({ id, label: id, preset, preset_a, preset_b,
            active_side: preset === preset_a ? 'A' : 'B', can_a: true, can_b: !!preset_b });
        const catalog = { revision: 3, active_mode: 'stereo-sub',
            modes: { 'stereo-sub': { selected_bank: 'main',
                banks: { global: bank('global', 'Neutral', 'Neutral', 'Room'),
                    main: bank('main', '3', '2', '3'), sub1: bank('sub1', 'Direct', 'Neutral', 'Direct') },
                all_banks: { preset: null, preset_a: null, preset_b: null, active_side: 'A', can_a: true, can_b: true },
                topology: { roles: ['main_l', 'main_r', 'sub1'] } } } };
        const state = { outputSystem: { catalog }, dsp: { available: true, presets: [], compare: {}, active_preset: 'Neutral' } };
        const elements = bankElements();
        initBankUI(state, elements);
        const deleteButton = () => [elements.effectsDeleteBtn.disabled, elements.effectsDeleteBtn.classList.contains('hidden')];
        BankUI.syncEffectsDeleteButton();
        assert.deepEqual(deleteButton(), [false, false], 'Main listening to B=3 can delete 3');
        const deleted = [];
        globalThis.fetch = async (url, options) => {
            deleted.push(JSON.parse(options.body).preset_name);
            return { ok: true, json: async () => ({}) };
        };
        await BankUI.deleteEffectsPreset();
        globalThis.fetch = realFetch;
        assert.deepEqual(deleted, ['3'], 'Delete targets the bank\'s listened preset');
        for (const [selected, expected] of [['global', [true, false]], ['sub1', [true, false]], ['all', [true, true]]]) {
            catalog.modes['stereo-sub'].selected_bank = selected;
            BankUI.syncEffectsDeleteButton();
            assert.deepEqual(deleteButton(), expected, `Delete button in ${selected}`);
        }
    }

    // Picking B's preset for A works like Global's compare: A takes it and B
    // is cleared. B cannot take A's preset.
    {
        const bank = (id, preset, preset_a, preset_b) => ({ id, label: id, preset, preset_a, preset_b,
            active_side: preset === preset_a ? 'A' : 'B', can_a: true, can_b: !!preset_b });
        const catalog = { revision: 3, active_mode: 'stereo-sub',
            modes: { 'stereo-sub': { selected_bank: 'main',
                banks: { global: bank('global', 'Neutral', 'Neutral', null), main: bank('main', '3', '2', '3') },
                topology: { roles: ['main_l', 'main_r'] } } } };
        const mutations = [];
        const toasts = [];
        const state = { outputSystem: { catalog }, dsp: { presets: [], compare: {}, active_preset: '' } };
        const elements = bankElements();
        initBankUI(state, elements, { applyMutation: async (kind, fields) => { mutations.push([kind, fields]); },
            showToast: (message, type) => toasts.push([message, type]) });
        elements.effectsCompareA.value = '3';
        await BankUI.handleEffectsCompareSelectionChange('A');
        assert.deepEqual(mutations, [['set_bank_preset', { mode: 'stereo-sub', bank_id: 'main',
            preset_a: '3', preset_b: null, active_side: 'A' }]]);
        elements.effectsCompareB.value = '2';
        await BankUI.handleEffectsCompareSelectionChange('B');
        assert.equal(mutations.length, 1, 'B cannot take A\'s preset');
        assert.deepEqual(toasts.at(-1), ['A and B must use different presets', 'warning']);
    }

    // All Banks switches to B once one bank has a B; the others stay on A.
    {
        const mutations = [];
        const toasts = [];
        const catalog = bankCatalog('all');
        catalog.modes['stereo-sub'].all_banks = { preset: null, preset_a: null, preset_b: null,
            active_side: 'A', can_a: true, can_b: true };
        const state = { outputSystem: { catalog }, dsp: { presets: [], compare: {}, active_preset: '' } };
        const elements = bankElements();
        initBankUI(state, elements, { applyMutation: async (kind, fields) => { mutations.push([kind, fields]); },
            showToast: (message, type) => toasts.push([message, type]) });
        await BankUI.toggleComparePreset();
        assert.deepEqual(mutations, [['switch_all_banks', { mode: 'stereo-sub', active_side: 'B' }]]);
        catalog.modes['stereo-sub'].all_banks.can_b = false;
        await BankUI.toggleComparePreset();
        assert.equal(mutations.length, 1);
        assert.deepEqual(toasts.at(-1), ['Assign preset B in at least one bank first.', 'warning']);
    }

    // Preset delete guards the empty and built-in cases before confirming.
    {
        const toasts = [];
        const calls = [];
        const bare = (id, label) => ({ id, label });
        const emptyCatalog = { revision: 7, active_mode: 'stereo-sub',
            modes: { 'stereo-sub': { selected_bank: 'main',
                banks: { global: bare('global', 'Global'), main: bare('main', 'Main L/R') },
                topology: { roles: ['main_l', 'main_r'] } } } };
        const state = { outputSystem: { catalog: emptyCatalog },
            dsp: { presets: [], compare: {}, active_preset: '' } };
        initBankUI(state, bankElements(), { showToast: (message, type) => toasts.push([message, type]) });
        await BankUI.deleteEffectsPreset();
        assert.deepEqual(toasts.at(-1), ['No active preset to delete', 'warning']);
        // Built-in and delete paths resolve through the legacy state without
        // a catalog; the bank-backed state above only proved the empty case.
        state.outputSystem.catalog = null;
        state.dsp.active_preset = 'Direct';
        await BankUI.deleteEffectsPreset();
        assert.match(toasts.at(-1)[0], /built-in/);
        state.dsp.active_preset = 'Room';
        globalThis.fetch = async (url) => {
            calls.push(url);
            return { ok: true, json: async () => ({}) };
        };
        await BankUI.deleteEffectsPreset();
        globalThis.fetch = realFetch;
        assert.ok(calls.includes('/api/dsp/presets/delete'));
        assert.match(toasts.at(-1)[0], /Deleted preset: Room/);
    }

    console.log('output bank ui frontend tests: ok');
})().catch((error) => { globalThis.fetch = realFetch; console.error(error); process.exit(1); });
