#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// All Banks compare tile: the status names the listened side only, each
// slot lists the bank presets of its side in place of the hidden picker, and
// the toggle reads "Compare A/B" as in a single bank; non-aggregate mode keeps
// the real preset pickers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

require('../static/output_state.js');
const BankUI = require('../static/output_bank_ui.js');
if (typeof globalThis.document === 'undefined') {
    globalThis.document = { querySelectorAll: () => [], getElementById: () => null };
}

const appSource = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
assert.match(appSource, /window\.FXRouteBankUI\.renderEffectsCompare\(/, 'app.js delegates compare rendering to the bank module');
assert.match(appSource, /FXRouteBankUI/, 'app.js must delegate bank UI to the module');

const bank = (id, label, presetB = null, side = 'A') => ({
    id, ...(label ? { label } : {}), preset: side === 'B' ? presetB : 'Neutral', preset_a: 'Neutral', preset_b: presetB,
    active_side: side, can_a: true, can_b: !!presetB,
});

const classList = () => {
    const names = new Set(['hidden']);
    return { names, toggle: (name, on) => (on ? names.add(name) : names.delete(name)), contains: (name) => names.has(name) };
};
const slotEl = (slot) => ({ dataset: { compareSlot: slot }, classList: classList() });

function makeContext(selectedBank, banks, allBanks = { preset: null, preset_a: 'Neutral', preset_b: null,
    active_side: null, can_a: true, can_b: false }, { mode = 'stereo-sub', roles = ['main_l', 'main_r', 'sub1'],
    presets = [{ name: 'Neutral' }] } = {}) {
    const config = {
        selected_bank: selectedBank,
        banks,
        topology: { mode, roles },
        all_banks: allBanks,
    };
    const catalog = { revision: 7, active_mode: mode,
        modes: { [mode]: config } };
    const escapeHtmlStub = (text) => String(text ?? '').replace(/&/g, '&amp;')
        .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    const sharedState = {
        outputSystem: { catalog, busy: false },
        dsp: { presets, compare: {}, active_preset: '' },
    };
    const sharedElements = {
        effectsCompareRow: { style: {} },
        effectsCompareA: { innerHTML: '' },
        effectsCompareB: { innerHTML: '' },
        effectsCompareActive: { innerHTML: '', textContent: '' },
        effectsCompareChain: { textContent: 'Chain: —', classList: classList() },
        effectsCompareToggle: { textContent: '', classList: classList() },
        effectsCompareABanks: { innerHTML: '', classList: classList() },
        effectsCompareBBanks: { innerHTML: '', classList: classList() },
    };
    const slots = [slotEl('A'), slotEl('B')];
    sharedElements.effectsCompareChain.classList.toggle('hidden', false);
    BankUI.init({
        getState: () => sharedState,
        getElements: () => sharedElements,
        showToast: () => {},
        escapeHtml: escapeHtmlStub,
        fetchEffects: async () => {},
        refreshCatalog: async () => null,
        collectEffectsExtras: () => ({}),
        measurementBankSumsBothInputs: () => false,
        presetFileUrl: () => '',
        renderEffects: () => {},
        renderMeasurementArea: () => {},
        measurementArea: () => null,
        applyMutation: async () => null,
        confirmDialog: () => true,
    });
    globalThis.document = { querySelectorAll: (selector) => (selector === '.effects-compare-slot' ? slots : []),
        getElementById: () => null };
    const context = {
        state: sharedState,
        elements: sharedElements,
        slots,
        window: { FXRouteBankUI: BankUI },
        document: globalThis.document,
        console,
    };
    vm.createContext(context);
    vm.runInContext(
        'function renderEffectsCompare() { return window.FXRouteBankUI.renderEffectsCompare(...arguments); }',
        context);
    return context;
}

const banks = {
    global: bank('global', 'Global', 'Global B', 'B'),
    main: bank('main', 'Main L/R', 'Conv LR <B>'),
    sub1: bank('sub1'),
};
const rows = (html) => [...html.matchAll(/<li class="([^"]*)" data-tooltip="([^"]*)"><span class="effects-compare-slot-bank-key">([^<]*)<\/span><span class="effects-compare-slot-bank-preset">([^<]*)<\/span><\/li>/g)]
    .map(([, classes, tooltip, key, preset]) => ({ classes, tooltip, key, preset }));

// Aggregate on A: the status names the side only, the slots list every
// switched area bank (Global stays out), the toggle reads like a single bank.
{
    const context = makeContext('all', banks, { preset: 'Neutral', preset_a: 'Neutral', preset_b: null,
        active_side: 'A', can_a: true, can_b: true });
    vm.runInContext('renderEffectsCompare()', context);
    const { elements } = context;
    assert.equal(elements.effectsCompareActive.textContent, 'Listening: A · All Banks');
    assert.ok(elements.effectsCompareChain.classList.contains('hidden'), 'the chain line leaves the status');
    assert.equal(elements.effectsCompareToggle.textContent, 'Compare A/B');
    assert.ok(!elements.effectsCompareABanks.classList.contains('hidden'));
    assert.ok(!elements.effectsCompareBBanks.classList.contains('hidden'));
    assert.deepEqual(rows(elements.effectsCompareABanks.innerHTML), [
        { classes: 'effects-compare-slot-bank', tooltip: 'Main L/R: Neutral', key: 'Main L/R', preset: 'Neutral' },
        { classes: 'effects-compare-slot-bank', tooltip: 'Sub 1: Neutral', key: 'Sub 1', preset: 'Neutral' },
    ]);
    // B: the assigned preset (escaped), or "stays on A" for a bank without a B.
    assert.deepEqual(rows(elements.effectsCompareBBanks.innerHTML), [
        { classes: 'effects-compare-slot-bank', tooltip: 'Main L/R: Conv LR &lt;B&gt;', key: 'Main L/R', preset: 'Conv LR &lt;B&gt;' },
        { classes: 'effects-compare-slot-bank is-fixed', tooltip: 'Sub 1: no preset B, stays on A', key: 'Sub 1', preset: 'stays on A' },
    ]);
    assert.ok(!elements.effectsCompareBBanks.innerHTML.includes('Global'), 'Global is not switched by All Banks');
    const [slotA, slotB] = context.slots;
    assert.ok(slotA.classList.contains('is-active') && !slotA.classList.contains('is-armed'));
    assert.ok(!slotB.classList.contains('is-active') && slotB.classList.contains('is-armed'));
}

// Aggregate on B marks the B slot; the rows do not repeat the slot marking.
{
    const onB = { ...banks, main: bank('main', 'Main L/R', 'Conv LR <B>', 'B') };
    const context = makeContext('all', onB, { preset: null, preset_a: 'Neutral', preset_b: null,
        active_side: 'B', can_a: true, can_b: true });
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(context.elements.effectsCompareActive.textContent, 'Listening: B · All Banks');
    const [slotA, slotB] = context.slots;
    assert.ok(slotB.classList.contains('is-active') && !slotA.classList.contains('is-active'));
    assert.ok(!context.elements.effectsCompareABanks.innerHTML.includes('is-heard'));
    assert.ok(!context.elements.effectsCompareBBanks.innerHTML.includes('is-heard'));
}

// Mixed A/B: no slot is active, each bank marks the side it listens to.
{
    const mixed = { global: bank('global', 'Global'), main: bank('main', 'Main L/R', 'PEQ', 'B'),
        sub1: bank('sub1', null, 'Sub PEQ', 'A') };
    const context = makeContext('all', mixed);
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(context.elements.effectsCompareActive.textContent, 'Listening: Mixed A/B');
    assert.ok(context.slots.every(slot => !slot.classList.contains('is-active')));
    assert.deepEqual(rows(context.elements.effectsCompareABanks.innerHTML).map(row => [row.key, row.classes]),
        [['Main L/R', 'effects-compare-slot-bank'], ['Sub 1', 'effects-compare-slot-bank is-heard']]);
    assert.deepEqual(rows(context.elements.effectsCompareBBanks.innerHTML).map(row => [row.key, row.classes]),
        [['Main L/R', 'effects-compare-slot-bank is-heard'], ['Sub 1', 'effects-compare-slot-bank']]);
}

// No area banks (Global only): the slots say so.
{
    const context = makeContext('all', { global: bank('global', 'Global') });
    vm.runInContext('renderEffectsCompare()', context);
    assert.match(context.elements.effectsCompareABanks.innerHTML, />No area banks</);
    assert.match(context.elements.effectsCompareBBanks.innerHTML, />No area banks</);
}

// Non-aggregate: real preset pickers stay in place.
{
    const context = makeContext('main', banks);
    // Coming from All Banks: lists shown, chain line hidden.
    context.elements.effectsCompareABanks.classList.toggle('hidden', false);
    context.elements.effectsCompareBBanks.classList.toggle('hidden', false);
    context.elements.effectsCompareChain.classList.toggle('hidden', true);
    vm.runInContext('renderEffectsCompare()', context);
    assert.match(context.elements.effectsCompareA.innerHTML, /<option value="Neutral" selected>Neutral<\/option>/);
    assert.match(context.elements.effectsCompareB.innerHTML, /Select preset…/);
    assert.notEqual(context.elements.effectsCompareA.innerHTML, '<option>All Banks · A</option>');
    // The single-bank view keeps its chain line and toggle label, the lists stay hidden.
    assert.ok(!context.elements.effectsCompareChain.classList.contains('hidden'));
    assert.equal(context.elements.effectsCompareChain.textContent, 'Chain: Single preset');
    assert.equal(context.elements.effectsCompareToggle.textContent, 'Compare A/B');
    assert.ok(context.elements.effectsCompareABanks.classList.contains('hidden'));
    assert.ok(context.elements.effectsCompareBBanks.classList.contains('hidden'));
}

// Surround Global: each slot lists its set's filters per speaker group, the
// listened set's groups name their filters in the status, and a group without
// a routed speaker is marked as such.
{
    const surround = (name, filters) => ({ name, bank: 'global', mode: 'surround', layout: 'surround', group_filters: filters });
    const presets = [{ name: 'Direct', bank: 'global', mode: null }, { name: 'Neutral', bank: null, mode: null },
        surround('Set <B>', [{ group: 'front', type: 'convolver', enabled: true, kernel: 'Conv LR HybAlign Neutral 20-2793Hz -3.5dB 232159' },
            { group: 'rear', type: 'equalizer', enabled: true, bands: 7 }, { group: 'side', type: 'equalizer', enabled: false, bands: 1 }]),
        surround('Front only', [{ group: 'front', type: 'convolver', enabled: true, kernel: 'Front IR' }])];
    const global = { id: 'global', label: 'Global', preset: 'Set <B>', preset_a: 'Front only', preset_b: 'Set <B>',
        active_side: 'B', can_a: true, can_b: true };
    const context = makeContext('global', { global }, undefined, { mode: 'surround', presets,
        roles: ['main_l', 'main_r', 'rear_l', 'rear_r', 'sub1'] });
    vm.runInContext('renderEffectsCompare()', context);
    const { elements } = context;
    const slotRows = (html) => [...html.matchAll(/<li class="([^"]*)" data-group="(\w+)" data-tooltip="([^"]*)"><span class="effects-compare-slot-bank-key">([^<]*)<\/span><span class="effects-compare-slot-bank-preset">([^<]*)<\/span><\/li>/g)]
        .map(([, classes, group, tooltip, key, value]) => ({ classes, group, tooltip, key, value }));
    assert.equal(elements.effectsCompareChain.textContent,
        'Filters: Front L/R Convolver · Side L/R PEQ (not routed) · Rear L/R PEQ');
    assert.deepEqual(slotRows(elements.effectsCompareBBanks.innerHTML), [
        { classes: 'effects-compare-slot-bank', group: 'front', key: 'Front L/R',
            tooltip: 'Front L/R: Convolver · Conv LR HybAlign Neutral 20-2793Hz -3.5dB 232159',
            value: 'Convolver · Conv LR HybAlign…232159' },
        { classes: 'effects-compare-slot-bank is-fixed', group: 'side', key: 'Side L/R',
            tooltip: 'Side L/R: PEQ · 1 band (off) · not routed, stays inactive', value: 'PEQ · 1 band (off)' },
        { classes: 'effects-compare-slot-bank', group: 'rear', key: 'Rear L/R', tooltip: 'Rear L/R: PEQ · 7 bands',
            value: 'PEQ · 7 bands' },
    ]);
    assert.deepEqual(slotRows(elements.effectsCompareABanks.innerHTML).map(row => [row.key, row.value]),
        [['Front L/R', 'Convolver · Front IR'], ['Rear L/R', 'No filter']]);
    assert.ok(!elements.effectsCompareABanks.classList.contains('hidden'));
    assert.ok(!elements.effectsCompareBBanks.classList.contains('hidden'));
    assert.ok(!elements.effectsCompareChain.classList.contains('hidden'));
    assert.match(elements.effectsCompareA.innerHTML, /<option value="Front only" selected>/);

    // Built-ins: Neutral holds no filter, Direct bypasses every group; an empty B lists nothing.
    context.state.outputSystem.catalog.modes.surround.banks.global = { ...global, preset: 'Direct', preset_a: 'Direct',
        preset_b: null, active_side: 'A', can_b: false };
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(elements.effectsCompareChain.textContent, 'Filters: bypassed (Direct)');
    assert.deepEqual(slotRows(elements.effectsCompareABanks.innerHTML).map(row => [row.key, row.value, row.classes]),
        [['Front L/R', 'Bypass', 'effects-compare-slot-bank is-fixed'], ['Rear L/R', 'Bypass', 'effects-compare-slot-bank is-fixed']]);
    assert.equal(elements.effectsCompareBBanks.innerHTML, '');
    assert.ok(elements.effectsCompareBBanks.classList.contains('hidden'));
    context.state.outputSystem.catalog.modes.surround.banks.global.preset = 'Neutral';
    context.state.outputSystem.catalog.modes.surround.banks.global.preset_a = 'Neutral';
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(elements.effectsCompareChain.textContent, 'Filters: none');

    // A concrete Surround bank keeps the plain chain line and no slot lists.
    context.state.outputSystem.catalog.modes.surround.selected_bank = 'rear';
    context.state.outputSystem.catalog.modes.surround.banks.rear = { id: 'rear', label: 'Rear L/R', preset: 'Neutral',
        preset_a: 'Neutral', preset_b: null, active_side: 'A', can_a: true, can_b: false };
    vm.runInContext('renderEffectsCompare()', context);
    assert.equal(elements.effectsCompareChain.textContent, 'Chain: Single preset');
    assert.equal(elements.effectsCompareABanks.innerHTML, '');
    assert.ok(elements.effectsCompareABanks.classList.contains('hidden'));
}

// The page carries one bank list per slot, next to the hidden picker.
{
    const page = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
    for (const slot of ['a', 'b']) {
        assert.match(page, new RegExp(`data-compare-slot="${slot.toUpperCase()}"[\\s\\S]{0,400}id="effects-compare-${slot}"[\\s\\S]{0,200}<ul id="effects-compare-${slot}-banks" class="effects-compare-slot-banks hidden"`));
        assert.match(appSource, new RegExp(`effectsCompare${slot.toUpperCase()}Banks: document\\.getElementById\\('effects-compare-${slot}-banks'\\)`));
    }
    assert.ok(!/Switch all to/.test(fs.readFileSync(path.join(__dirname, '../static/output_bank_ui.js'), 'utf8')));
}

console.log('effects compare labels tests passed');
