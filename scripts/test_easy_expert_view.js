#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Easy/Expert view of the DSP tab and the Surround Global import group.
// Easy (the default) works on Global without a bank selector; Expert shows
// the stored bank selection. The switch is a per-browser view choice and
// never writes the output state. A Surround Global import takes its speaker
// group from chips in the import panel (configured groups only).

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const OutputState = require('../static/output_state.js');
require('../static/ui_helpers.js');
require('../static/api.js');
const Controller = require('../static/output_system_controller.js');
const BankUI = require('../static/output_bank_ui.js');

function stubClassList() {
    const set = new Set();
    return { add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
        toggle: (c, force) => { if (force ?? !set.has(c)) set.add(c); else set.delete(c); } };
}

function stubEl(overrides = {}) {
    const el = { value: '', textContent: '', innerHTML: '', disabled: false, checked: false, style: {},
        dataset: {}, files: [], accept: '', placeholder: '', classList: stubClassList(),
        setAttribute() {}, addEventListener() {}, focus() {}, querySelectorAll: () => [],
        closest: () => null, ...overrides };
    return el;
}

// An element inside a wrapper that carries the visibility class.
function wrapped(className) {
    const wrapper = stubEl();
    const el = stubEl({ closest: (selector) => (selector === `.${className}` ? wrapper : null) });
    return { el, wrapper };
}

const nodes = {};
globalThis.document = {
    getElementById: (id) => nodes[id] || (nodes[id] = stubEl()),
    querySelector: (sel) => nodes[sel] || (nodes[sel] = stubEl()),
    querySelectorAll: () => [],
};

const bank = (id, label, roles, channel_mode = 'stereo') => ({ id, label, roles, channel_mode,
    preset: 'Neutral', preset_a: 'Neutral', preset_b: null, active_side: 'A', can_a: true, can_b: false });

function surroundCatalog(selectedBank) {
    return { revision: 11, active_mode: 'surround', modes: {
        stereo: { selected_bank: 'global', banks: { global: bank('global', 'Global', ['global']) },
            topology: { mode: 'stereo', roles: ['main_l', 'main_r'] }, all_banks: {} },
        surround: { selected_bank: selectedBank, all_banks: { active_side: 'A' },
            banks: { global: bank('global', 'Global', ['global']), main: bank('main', 'Front L/R', ['main_l', 'main_r']),
                rear: bank('rear', 'Rear L/R', ['rear_l', 'rear_r']), sub1: bank('sub1', 'Sub 1', ['sub1'], 'mono') },
            topology: { mode: 'surround', roles: ['main_l', 'main_r', 'rear_l', 'rear_r', 'sub1'], sub_roles: ['sub1'] } },
    } };
}

function pureStereoCatalog() {
    return { revision: 3, active_mode: 'stereo', modes: {
        stereo: { selected_bank: 'global', banks: { global: bank('global', 'Global', ['global']) },
            topology: { mode: 'stereo', roles: ['main_l', 'main_r'] }, all_banks: {} } } };
}

// --- The view: Easy works on Global, Expert shows the stored selection. ---
{
    const stored = surroundCatalog('rear');
    const easy = OutputState.viewCatalog(stored, false);
    assert.deepEqual(Object.values(easy.modes).map(config => config.selected_bank), ['global', 'global']);
    assert.equal(stored.modes.surround.selected_bank, 'rear', 'the stored catalog is not changed');
    assert.equal(easy.revision, stored.revision, 'the view keeps the revision mutations are checked against');
    assert.equal(OutputState.viewCatalog(stored, true), stored, 'Expert shows the catalog as it is');
    assert.equal(OutputState.viewCatalog(null, false), null);
    // Easy measures and imports into Global; the Surround group follows the selection.
    assert.equal(OutputState.measurementArea(easy, undefined, 'rear').bank_id, 'global');
    assert.equal(OutputState.bankBinding(easy, 'rear').bank_group, 'rear');
    assert.deepEqual(OutputState.bankOptions(stored.modes.surround).map(option => option.label),
        ['Global', 'Ind. Banks', 'Front L/R', 'Rear L/R', 'Sub 1']);
    assert.equal(OutputState.roleLabel('all'), 'Ind. Banks');
}

// --- The bank selector row belongs to Expert only. ---
{
    for (const [catalog, expert, hidden] of [[surroundCatalog('rear'), false, true],
        [surroundCatalog('rear'), true, false], [pureStereoCatalog(), true, true]]) {
        const { el, wrapper } = wrapped('effects-bank-row');
        OutputState.renderBankSelector(el, catalog, catalog.active_mode, expert);
        assert.equal(wrapper.classList.contains('hidden'), hidden, `bank row hidden=${hidden} (expert=${expert})`);
    }
}

// --- The controller stores the view of every fetched catalog. ---
(async () => {
    const realFetch = globalThis.fetch;
    for (const [expert, expected] of [[false, 'global'], [true, 'all']]) {
        const state = { outputSystem: { catalog: null, busy: false, expert } };
        Controller.init({ getState: () => state, outputSystemModule: () => OutputState });
        globalThis.fetch = async () => ({ ok: true, json: async () => surroundCatalog('all') });
        await Controller.fetchOutputSystemCatalog(true);
        assert.equal(state.outputSystem.catalog.modes.surround.selected_bank, expected,
            `expert=${expert} stores selection ${expected}`);
    }
    globalThis.fetch = realFetch;

    // --- The stored choice: Easy without one, Expert after switching. ---
    const storage = {};
    globalThis.localStorage = { getItem: key => storage[key] ?? null, setItem: (key, value) => { storage[key] = String(value); } };
    assert.equal(BankUI.readStoredExpertView(), false, 'Easy is the default');
    {
        const state = { outputSystem: { catalog: surroundCatalog('all'), expert: false }, dsp: {}, measurement: {} };
        const refreshes = [];
        const mutations = [];
        BankUI.init({ getState: () => state, getElements: () => ({}),
            refreshCatalog: async (force) => refreshes.push(force),
            applyMutation: async (...args) => mutations.push(args) });
        await BankUI.setExpertView(true);
        assert.equal(state.outputSystem.expert, true);
        assert.equal(storage['fx-dsp-expert'], '1');
        assert.equal(BankUI.readStoredExpertView(), true);
        assert.deepEqual(refreshes, [true], 'the catalog is read again through the other view');
        await BankUI.setExpertView(true);
        assert.deepEqual(refreshes, [true], 'an unchanged view is not read again');
        await BankUI.setExpertView(false);
        assert.equal(storage['fx-dsp-expert'], '0');
        assert.deepEqual(mutations, [], 'switching the view never writes the output state');
    }
    globalThis.localStorage = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
    assert.equal(BankUI.readStoredExpertView(), false, 'unavailable storage keeps Easy');
    delete globalThis.localStorage;

    // --- Expert switch and import chips on the rendered tile. ---
    function render(catalog, { expert = false, group } = {}) {
        const state = { outputSystem: { catalog: OutputState.viewCatalog(catalog, expert), expert, busy: false },
            dsp: {}, measurement: { globalGroup: group } };
        const toggle = wrapped('effects-expert-toggle');
        const select = wrapped('effects-bank-row');
        const elements = { effectsBankSelect: select.el, effectsExpertToggle: toggle.el,
            effectsImportGroupRow: stubEl() };
        BankUI.init({ getState: () => state, getElements: () => elements,
            renderMeasurementArea: () => {},
            measurementArea: () => OutputState.measurementArea(state.outputSystem.catalog, undefined,
                state.measurement.globalGroup) });
        BankUI.renderEffectsBankSelector();
        const form = [];
        BankUI.appendBankBindingFields({ append: (key, value) => form.push([key, value]) });
        return { toggle, select, row: elements.effectsImportGroupRow, form: Object.fromEntries(form) };
    }

    {
        const easy = render(surroundCatalog('all'), { group: 'rear' });
        assert.equal(easy.toggle.wrapper.classList.contains('hidden'), false, 'Surround offers the Expert switch');
        assert.equal(easy.toggle.el.checked, false);
        assert.equal(easy.select.wrapper.classList.contains('hidden'), true, 'Easy hides the bank selector');
        assert.equal(nodes['effects-import-bank-target'].textContent, 'Target: Global · Rear L/R + Sub · Stereo L/R');
        // Chips for the configured groups only; the chosen one binds the import.
        assert.equal(easy.row.classList.contains('hidden'), false, 'Surround Global shows the group chips');
        assert.match(easy.row.innerHTML, /data-import-group="front"/);
        assert.match(easy.row.innerHTML, /data-import-group="rear"/);
        assert.doesNotMatch(easy.row.innerHTML, /data-import-group="(center|side)"/, 'unconfigured groups offer no chip');
        assert.deepEqual(easy.form, { bank_mode: 'surround', bank_id: 'global', bank_group: 'rear', expected_revision: '11' });
        assert.equal(render(surroundCatalog('all'), { group: 'front' }).form.bank_group, 'front');

        const expert = render(surroundCatalog('all'), { expert: true, group: 'rear' });
        assert.equal(expert.toggle.el.checked, true);
        assert.equal(expert.select.wrapper.classList.contains('hidden'), false, 'Expert shows the bank selector');
        assert.equal(expert.row.classList.contains('hidden'), true, 'Ind. Banks has no import chips');
        // A single bank imports bank-bound, without a group.
        const single = render(surroundCatalog('rear'), { expert: true, group: 'front' });
        assert.equal(single.row.classList.contains('hidden'), true, 'a single bank shows no group chips');
        assert.deepEqual(single.form, { bank_mode: 'surround', bank_id: 'rear', expected_revision: '11' });
        assert.equal(render(surroundCatalog('global'), { expert: true, group: 'rear' }).form.bank_group, 'rear',
            'Global chosen in Expert imports per group too');

        const stereo = render(pureStereoCatalog());
        assert.equal(stereo.toggle.wrapper.classList.contains('hidden'), true, 'pure Stereo has no bank selection to show');
        assert.equal(stereo.row.classList.contains('hidden'), true);
    }

    // --- Markup and wiring. ---
    const header = indexSource.slice(indexSource.indexOf('effects-compare-header'), indexSource.indexOf('id="effects-compare-row"'));
    assert.ok(header.indexOf('id="effects-bank-select"') < header.indexOf('id="effects-expert-toggle"'),
        'the Expert switch sits at the right end of the A/B header');
    const importHeader = indexSource.slice(indexSource.indexOf('id="effects-import-panel"'), indexSource.indexOf('id="effects-import-file-head"'));
    assert.match(importHeader, /id="effects-import-group-row"/, 'the group chips sit in the import panel header');
    assert.match(appSource, /expert: window\.FXRouteBankUI \? window\.FXRouteBankUI\.readStoredExpertView\(\) : false/);
    // The chips set the shared speaker group under the same guard as the sweep menu chips.
    assert.match(appSource, /closest\?\.\('\[data-import-group\]'\);\n\s+if \(!button \|\| state\.measurement\.startInFlight \|\| window\.FXRouteMeasurementJob\.hasActiveMeasurementJob\(\)\) return;\n\s+selectMeasurementSpeakerGroup\(/);

    console.log('easy/expert view tests passed');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
