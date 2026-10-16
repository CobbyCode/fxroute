#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Surround Global A/B tile: each slot holds one preset picker per routed
// speaker group, listing that group's presets; a pick names its group, the
// slot pickers stay hidden, and Delete removes the listened preset of the
// group the measurement targets.
const assert = require('node:assert/strict');

const OutputState = require('../static/output_state.js');
const BankUI = require('../static/output_bank_ui.js');

const NEUTRAL = { front: 'Neutral', center: 'Neutral', side: 'Neutral', rear: 'Neutral' };
const selection = (groups) => ({ ...NEUTRAL, ...groups });

const classList = () => {
    const names = new Set(['hidden']);
    return { names, toggle: (name, on) => (on ? names.add(name) : names.delete(name)), contains: (name) => names.has(name) };
};
const slotEl = (slot) => ({ dataset: { compareSlot: slot }, classList: classList() });
const list = () => {
    const element = { innerHTML: '', classList: classList(), listeners: [] };
    element.addEventListener = (type, handler) => element.listeners.push([type, handler]);
    element.querySelectorAll = () => [];
    return element;
};

const PRESETS = [
    { name: 'Direct', bank: 'global', mode: null, group: null },
    { name: 'Neutral', bank: null, mode: null, group: null },
    { name: 'Front FIR', bank: 'global', mode: 'surround', group: 'front' },
    { name: 'Rear FIR', bank: 'global', mode: 'surround', group: 'rear' },
    { name: 'Rear FIR B', bank: 'global', mode: 'surround', group: 'rear' },
    { name: 'Conv LR HybAlign Neutral 20-2793Hz -3.5dB 233224', bank: 'global', mode: 'surround', group: 'side' },
    { name: 'Center PEQ', bank: 'global', mode: 'surround', group: 'center' },
    { name: 'Stereo Room', bank: 'global', mode: 'stereo', group: null },
];

function makeContext(global, { group = 'rear', roles = ['main_l', 'main_r', 'rear_l', 'rear_r', 'sub1'] } = {}) {
    const activeSide = JSON.stringify(global.preset) === JSON.stringify(global.preset_a) ? 'A'
        : JSON.stringify(global.preset) === JSON.stringify(global.preset_b) ? 'B' : null;
    const catalog = { revision: 7, active_mode: 'surround', modes: { surround: {
        selected_bank: 'global',
        banks: { global: { id: 'global', label: 'Global', ...global, active_side: activeSide,
            can_a: true, can_b: global.preset_b !== null } },
        topology: { mode: 'surround', roles },
        all_banks: { preset: null, preset_a: 'Neutral', preset_b: null, active_side: null, can_a: true, can_b: false },
    } } };
    const state = { outputSystem: { catalog, busy: false }, dsp: { presets: PRESETS, available: true }, measurement: {} };
    const elements = {
        effectsCompareRow: { style: {} },
        effectsCompareA: { innerHTML: '', closest: () => ({ classList: classList() }), addEventListener: () => {} },
        effectsCompareB: { innerHTML: '', closest: () => ({ classList: classList() }), addEventListener: () => {} },
        effectsCompareActive: { innerHTML: '', textContent: '' },
        effectsCompareChain: { textContent: 'Chain: —', classList: classList() },
        effectsCompareToggle: { textContent: '', classList: classList(), addEventListener: () => {} },
        effectsCompareABanks: list(),
        effectsCompareBBanks: list(),
        effectsDeleteBtn: { disabled: false, dataset: {}, classList: classList(),
            setAttribute(name, value) { this[name] = value; } },
    };
    const mutations = [];
    const confirmations = [];
    BankUI.init({
        getState: () => state,
        getElements: () => elements,
        showToast: () => {},
        escapeHtml: (text) => String(text ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
        measurementArea: () => ({ bank_id: 'global', group, available: true }),
        applyMutation: async (kind, fields) => { mutations.push([kind, fields]); return null; },
        confirmDialog: (message) => { confirmations.push(message); return false; },
    });
    const slots = [slotEl('A'), slotEl('B')];
    globalThis.document = { querySelectorAll: (selector) => (selector === '.effects-compare-slot' ? slots : []),
        getElementById: () => null };
    return { catalog, state, elements, mutations, confirmations, slots };
}

const rows = (html) => [...html.matchAll(/<li class="effects-compare-slot-bank effects-compare-slot-group" data-group="([^"]+)">.*?<select [^>]*data-slot="([AB])" data-group="[^"]+" data-tooltip="([^"]*)">(.*?)<\/select><\/li>/g)]
    .map(([, group, slot, tooltip, options]) => ({
        group, slot, tooltip,
        options: [...options.matchAll(/<option value="([^"]*)"( selected)?(?: title="[^"]*")?>([^<]*)<\/option>/g)].map(([, value]) => value),
        labels: [...options.matchAll(/<option value="[^"]*"(?: selected)?(?: title="[^"]*")?>([^<]*)<\/option>/g)].map(([, label]) => label),
        selected: (/<option value="([^"]*)" selected[ >]/.exec(options) || [])[1],
    }));

// Helpers: rows per routed group, the slot labels and the A/B compare state.
{
    const { catalog } = makeContext({ preset: selection({ front: 'Front FIR', rear: 'Rear FIR' }),
        preset_a: selection({ front: 'Front FIR', rear: 'Rear FIR' }), preset_b: null });
    assert.deepEqual(OutputState.globalGroupSlots(catalog, PRESETS), [
        { group: 'front', label: 'Front', presetA: 'Front FIR', presetB: 'Neutral', options: ['Front FIR'] },
        { group: 'rear', label: 'Rear', presetA: 'Rear FIR', presetB: 'Neutral', options: ['Rear FIR', 'Rear FIR B'] },
    ]);
    assert.equal(OutputState.globalSlotLabel(selection({ front: 'Front FIR', rear: 'Rear FIR' })), 'Front + Rear');
    assert.equal(OutputState.globalSlotLabel(NEUTRAL), 'Neutral');
    assert.equal(OutputState.globalSlotLabel('Direct'), 'Direct');
    const compare = OutputState.compareState(catalog);
    assert.equal(compare.groupSlots, true);
    assert.equal(compare.activePreset, 'Front + Rear');
    const stereo = { ...catalog, active_mode: 'stereo', modes: { stereo: { selected_bank: 'global',
        banks: { global: { preset: 'Room', preset_a: 'Room', preset_b: null, active_side: 'A' } } } } };
    assert.equal(OutputState.globalGroupSlots(stereo, PRESETS), null);
    assert.equal(OutputState.compareState(stereo).groupSlots, false);
    assert.deepEqual(OutputState.activePresetOverview(catalog), [], 'Global does not repeat its own status chip');
}

// A and B list one picker per routed group; the slot pickers hide.
{
    const slotA = selection({ front: 'Front FIR', rear: 'Rear FIR' });
    const slotB = selection({ front: 'Front FIR', rear: 'Rear FIR B' });
    const context = makeContext({ preset: slotB, preset_a: slotA, preset_b: slotB });
    BankUI.renderEffectsCompare();
    const { elements } = context;
    assert.equal(elements.effectsCompareActive.textContent, 'Listening: B');
    assert.equal(elements.effectsCompareChain.textContent, 'Filters: Front + Rear');
    assert.ok(!elements.effectsCompareABanks.classList.contains('hidden'));
    assert.ok(!elements.effectsCompareBBanks.classList.contains('hidden'));
    assert.deepEqual(rows(elements.effectsCompareABanks.innerHTML).map(row => [row.group, row.selected, row.options]), [
        ['front', 'Front FIR', ['Neutral', 'Front FIR', 'Direct']],
        ['rear', 'Rear FIR', ['Neutral', 'Rear FIR', 'Rear FIR B', 'Direct']],
    ]);
    const rowsB = rows(elements.effectsCompareBBanks.innerHTML);
    assert.deepEqual(rowsB.map(row => [row.group, row.selected]), [['front', 'Front FIR'], ['rear', 'Rear FIR B']]);
    assert.equal(rowsB[0].options[0], 'Neutral', 'B starts with Neutral, not an empty choice');
    assert.equal(rowsB[1].tooltip, 'Preset B · Rear: Rear FIR B');
    assert.ok(!elements.effectsCompareBBanks.innerHTML.includes('Center'), 'an unrouted group gets no picker');
    assert.deepEqual(rowsB[1].labels, ['Neutral', 'Rear FIR', 'Rear FIR B', 'Direct · all groups']);
    const [tileA, tileB] = context.slots;
    assert.ok(tileB.classList.contains('is-active') && tileA.classList.contains('is-armed'));
    // Delete removes the listened preset of the measured group.
    assert.equal(elements.effectsDeleteBtn.disabled, false);
    assert.equal(elements.effectsDeleteBtn.dataset.tooltip, 'Delete active Rear preset');
    BankUI.deleteEffectsPreset();
    assert.deepEqual(context.confirmations, ['Delete Rear preset "Rear FIR B"?']);
}

// A long generated name keeps its start and its distinguishing last word.
{
    const slotA = selection({ side: 'Conv LR HybAlign Neutral 20-2793Hz -3.5dB 233224' });
    const context = makeContext({ preset: slotA, preset_a: slotA, preset_b: null },
        { roles: ['main_l', 'main_r', 'side_l', 'side_r', 'sub1'], group: 'side' });
    BankUI.renderEffectsCompare();
    const side = rows(context.elements.effectsCompareABanks.innerHTML).find(row => row.group === 'side');
    assert.equal(side.selected, 'Conv LR HybAlign Neutral 20-2793Hz -3.5dB 233224');
    assert.equal(side.labels[1], 'Conv LR HybAlign…233224');
    assert.equal(side.tooltip, 'Preset A · Side: Conv LR HybAlign Neutral 20-2793Hz -3.5dB 233224');
}

// Global A/B uses short group names only; bank and measurement labels keep L/R.
{
    const { catalog, elements } = makeContext({ preset: NEUTRAL, preset_a: NEUTRAL, preset_b: NEUTRAL },
        { roles: ['main_l', 'main_r', 'rear_l', 'rear_r', 'side_l', 'side_r', 'center', 'sub1', 'sub2'] });
    BankUI.renderEffectsCompare();
    assert.deepEqual(OutputState.globalGroupSlots(catalog, PRESETS).map(row => row.label),
        ['Front', 'Center', 'Side', 'Rear']);
    for (const target of [elements.effectsCompareABanks, elements.effectsCompareBBanks]) {
        assert.doesNotMatch(target.innerHTML, /L\/R|Sub|No preset|Select preset/);
        assert.ok(rows(target.innerHTML).every(row => row.selected === 'Neutral'));
    }
    assert.equal(elements.effectsCompareToggle.disabled, false);
    assert.equal(OutputState.SURROUND_GROUP_LABELS.front, 'Front L/R');
}

// A legacy empty B displays Neutral; a Neutral group disables Delete.
{
    const slotA = selection({ front: 'Front FIR' });
    const context = makeContext({ preset: slotA, preset_a: slotA, preset_b: null });
    BankUI.renderEffectsCompare();
    assert.deepEqual(rows(context.elements.effectsCompareBBanks.innerHTML).map(row => row.selected), ['Neutral', 'Neutral']);
    assert.equal(context.elements.effectsDeleteBtn.disabled, true);
    assert.equal(context.elements.effectsCompareToggle.disabled, true);
}

// Direct shows in every group; Neutral reads as no filters.
{
    const context = makeContext({ preset: 'Direct', preset_a: 'Direct', preset_b: NEUTRAL });
    BankUI.renderEffectsCompare();
    assert.equal(context.elements.effectsCompareChain.textContent, 'Filters: bypassed (Direct)');
    assert.deepEqual(rows(context.elements.effectsCompareABanks.innerHTML).map(row => row.selected), ['Direct', 'Direct']);
    assert.deepEqual(rows(context.elements.effectsCompareBBanks.innerHTML).map(row => row.selected), ['Neutral', 'Neutral']);
}

// A pick sends its slot and group, including Neutral in B.
(async () => {
    const slotA = selection({ front: 'Front FIR', rear: 'Rear FIR' });
    const slotB = selection({ front: 'Front FIR', rear: 'Rear FIR B' });
    const context = makeContext({ preset: slotB, preset_a: slotA, preset_b: slotB });
    BankUI.setupEffectsCompareActions();
    BankUI.renderEffectsCompare();
    const change = (list, dataset, value) => list.listeners.find(([type]) => type === 'change')[1]({
        target: { dataset, value, classList: { contains: (name) => name === 'compare-group-select' } } });
    await change(context.elements.effectsCompareABanks, { slot: 'A', group: 'rear' }, 'Rear FIR B');
    await change(context.elements.effectsCompareBBanks, { slot: 'B', group: 'front' }, 'Neutral');
    assert.deepEqual(context.mutations, [
        ['set_bank_preset', { mode: 'surround', bank_id: 'global', preset_a: 'Rear FIR B', group: 'rear' }],
        ['set_bank_preset', { mode: 'surround', bank_id: 'global', preset_b: 'Neutral', group: 'front' }],
    ]);
    console.log('surround global slot tests passed');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
