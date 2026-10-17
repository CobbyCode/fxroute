#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A/B card overview: the active preset of every bank in the active mode's
// chain, Global first, with long names shortened around their middle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ui = require('../static/output_state.js');
const BankUI = require('../static/output_bank_ui.js');
if (typeof globalThis.document === 'undefined') {
    globalThis.document = { querySelectorAll: () => [], getElementById: () => null };
}

const bank = (label, preset, side = 'A') => ({
    label, preset, preset_a: side === 'A' ? preset : 'Neutral', preset_b: side === 'B' ? preset : null,
    active_side: preset ? side : null, can_a: true, can_b: side === 'B',
});
const catalog = (mode, banks, selected = 'global') => ({
    revision: 4, active_mode: mode,
    modes: { [mode]: { selected_bank: selected, banks, topology: { roles: ['main_l', 'main_r', 'sub1', 'sub2'] } },
        other: { selected_bank: 'global', banks: { global: bank('Global', 'Elsewhere') } } },
});
const CONV = 'Conv LR HybAlign BK 20-3576Hz -4.5dB 233313';

// Short names stay; long ones keep the leading words and the last word.
assert.equal(ui.compactPresetName('Neutral'), 'Neutral');
assert.equal(ui.compactPresetName(CONV), 'Conv LR…233313');
assert.equal(ui.compactPresetName('PEQ LR Measurement 2f 233800'), 'PEQ LR…233800');
assert.equal(ui.compactPresetName('MyVeryLongPresetNameWithoutSpaces123'), 'MyVeryLongPresetNam…');
assert.equal(ui.compactPresetName('Room VeryLongTailWordHere12345'), 'Room VeryLongTailWo…');
assert.equal(ui.compactPresetName('  padded  '), 'padded');
assert.equal(ui.compactPresetName(null), '');
for (const name of [CONV, 'PEQ LR Measurement 2f 233800', 'MyVeryLongPresetNameWithoutSpaces123']) {
    assert.ok(ui.compactPresetName(name).length <= 20, `${name} must fit the budget`);
}

// Global shows only the active mode's area banks.
assert.deepEqual(ui.activePresetOverview(catalog('stereo-sub', {
    main: bank('Main L/R', 'Neutral'), global: bank('Global', CONV, 'B'),
    sub1: bank('Sub 1', 'PEQ', 'B'), sub2: bank('Sub 2', 'PEQ', 'B'),
})), [
    { ids: ['main'], label: 'Main L/R', preset: 'Neutral' },
    { ids: ['sub1', 'sub2'], label: 'Sub 1/2', preset: 'PEQ' },
]);

// Numbered banks merge only in a row, of one family, on the same preset.
assert.deepEqual(ui.activePresetOverview(catalog('stereo-sub', {
    global: bank('Global', 'Neutral'), sub1: bank('Sub 1', 'PEQ'), sub2: bank('Sub 2', 'Direct'),
})).map(entry => `${entry.label}=${entry.preset}`), ['Sub 1=PEQ', 'Sub 2=Direct']);
assert.deepEqual(ui.activePresetOverview(catalog('surround', {
    global: bank('Global', 'Neutral'), sub1: bank('Sub 1', 'Direct'), center: bank('Center', 'Direct'),
    sub2: bank('Sub 2', 'Direct'), sub3: bank('Sub 3', 'Direct'),
})).map(entry => entry.label), ['Sub 1', 'Center', 'Sub 2/3']);

// Channels on different presets have no single preset; they never merge.
assert.deepEqual(ui.activePresetOverview(catalog('stereo-sub', {
    global: bank('Global', 'Neutral'), sub1: bank('Sub 1', null), sub2: bank('Sub 2', null),
})).map(entry => [entry.label, entry.preset]), [['Sub 1', null], ['Sub 2', null]]);

// A bank without a label falls back to the role label of the mode.
assert.deepEqual(ui.activePresetOverview(catalog('surround', {
    global: { preset: 'Neutral' }, main: { preset: 'Direct' },
})).map(entry => entry.label), ['Front L/R']);
assert.deepEqual(ui.activePresetOverview(null), []);
assert.deepEqual(ui.activePresetOverview({ active_mode: 'stereo', modes: {} }), []);

// All Banks slots: every area bank with its A and B, Global left out.
const abBank = (label, presetA, presetB, side = 'A', extra = {}) => ({
    label, preset: side === 'B' ? presetB : presetA, preset_a: presetA, preset_b: presetB,
    active_side: side, can_a: true, can_b: !!presetB, ...extra,
});
assert.deepEqual(ui.allBanksCompareOverview(catalog('surround', {
    global: abBank('Global', 'Neutral', 'Direct'), main: abBank('Front L/R', 'Neutral', CONV),
    rear: abBank('Rear L/R', 'Neutral', null), sub1: abBank('Sub 1', 'Neutral', 'Direct'),
    sub2: abBank('Sub 2', 'Neutral', 'Direct'),
})), [
    { ids: ['main'], label: 'Front L/R', presetA: 'Neutral', presetB: CONV, canB: true, side: 'A' },
    { ids: ['rear'], label: 'Rear L/R', presetA: 'Neutral', presetB: null, canB: false, side: 'A' },
    { ids: ['sub1', 'sub2'], label: 'Sub 1/2', presetA: 'Neutral', presetB: 'Direct', canB: true, side: 'A' },
]);
// Numbered banks merge only on equal A, B and side; per-channel slots never merge.
assert.deepEqual(ui.allBanksCompareOverview(catalog('stereo-sub', {
    sub1: abBank('Sub 1', 'Neutral', 'Direct', 'B'), sub2: abBank('Sub 2', 'Neutral', 'Direct', 'A'),
    sub3: abBank('Sub 3', 'Neutral', 'PEQ'), sub4: abBank('Sub 4', 'Neutral', 'PEQ'),
})).map(row => row.label), ['Sub 1', 'Sub 2', 'Sub 3/4']);
assert.deepEqual(ui.allBanksCompareOverview(catalog('stereo-sub', {
    sub1: abBank('Sub 1', null, null, null, { can_b: true }), sub2: abBank('Sub 2', null, null, null, { can_b: true }),
    sub3: abBank('Sub 3', 'Neutral', null, 'A', { can_b: true }), sub4: abBank('Sub 4', 'Neutral', null, 'A', { can_b: true }),
})).map(row => [row.label, row.presetA, row.presetB, row.canB, row.side]), [
    ['Sub 1', null, null, true, null], ['Sub 2', null, null, true, null],
    ['Sub 3', 'Neutral', null, true, 'A'], ['Sub 4', 'Neutral', null, true, 'A'],
]);
// An unlabelled bank takes the role label of the mode.
assert.deepEqual(ui.allBanksCompareOverview(catalog('surround', { global: {}, main: { preset_a: 'Neutral' } }))
    .map(row => [row.label, row.canB]), [['Front L/R', false]]);
assert.deepEqual(ui.allBanksCompareOverview(null), []);

// Rendered into the compare badge: only banks outside the current selection.
function renderElements(state) {
    const elements = {
        effectsCompareRow: { style: {} },
        effectsCompareA: { innerHTML: '' },
        effectsCompareB: { innerHTML: '' },
        effectsCompareActive: { innerHTML: '', textContent: '' },
        effectsCompareChain: { textContent: '' },
        effectsCompareToggle: { textContent: '' },
        effectsCompareBanks: { innerHTML: 'stale' },
    };
    BankUI.init({
        getState: () => state,
        getElements: () => elements,
        escapeHtml: ui.esc,
        presetFileUrl: () => '',
        showToast: () => {},
    });
    BankUI.renderEffectsCompare();
    return elements;
}
const render = state => renderElements(state).effectsCompareBanks.innerHTML;
const chips = (html) => [...html.matchAll(/<span class="effects-compare-bank( is-selected)?" data-tooltip="([^"]*)">.*?<\/span><\/span>/g)]
    .map(match => ({ selected: !!match[1], tooltip: match[2],
        text: match[0].replace(/<[^>]+>/g, '') }));
const presets = [{ name: 'Neutral' }, { name: 'Direct' }, { name: CONV, bank: 'global' }, { name: 'A&B <x>', bank: 'main' }];
const banks = {
    global: bank('Global', CONV, 'B'), main: bank('Main L/R', 'A&B <x>'),
    sub1: bank('Sub 1', 'Direct'), sub2: bank('Sub 2', 'Direct'),
};
{
    const html = render({ outputSystem: { catalog: catalog('stereo-sub', banks, 'main') }, dsp: { presets } });
    assert.deepEqual(chips(html), [
        { selected: false, tooltip: `Global: ${CONV}`, text: 'Global Conv LR…233313' },
        { selected: false, tooltip: 'Sub 1/2: Direct', text: 'Sub 1/2 Direct' },
    ]);
    const escaped = render({ outputSystem: { catalog: catalog('stereo-sub', banks, 'global') }, dsp: { presets } });
    assert.ok(!escaped.includes('<x>'), 'preset names are escaped');
    assert.match(escaped, /A&amp;B &lt;x&gt;/);
}
// Filter before merging: selecting Sub 2 leaves Sub 1, not Sub 1/2.
assert.deepEqual(chips(render({ outputSystem: { catalog: catalog('stereo-sub', banks, 'sub2') }, dsp: { presets } }))
    .map(chip => [chip.selected, chip.text]), [[false, 'Global Conv LR…233313'],
        [false, 'Main L/R A&amp;B &lt;x&gt;'], [false, 'Sub 1 Direct']]);
const allCatalog = catalog('stereo-sub', banks, 'all');
allCatalog.modes['stereo-sub'].all_banks = { preset: null, preset_a: null, preset_b: null,
    active_side: null, can_a: true, can_b: true };
assert.deepEqual(chips(render({ outputSystem: { catalog: allCatalog }, dsp: { presets } })), [
    { selected: false, tooltip: `Global: ${CONV}`, text: 'Global Conv LR…233313' },
]);
// Mixed channel bindings read as per-channel; without a catalog the line is empty.
assert.match(render({ outputSystem: { catalog: catalog('stereo-sub', { global: bank('Global', 'Neutral'),
    main: bank('Main L/R', null) }) }, dsp: { presets } }), /Main L\/R<\/span> <span class="effects-compare-bank-preset">Per-channel</);
assert.equal(render({ outputSystem: { catalog: null }, dsp: { presets, compare: {}, active_preset: 'Neutral' } }), '');

// Easy hides only Neutral area chips, using the listened preset, not an inactive B.
const mixedCatalog = catalog('stereo-sub', {
    global: bank('Global', CONV, 'B'), main: abBank('Main L/R', 'Neutral', CONV),
    sub1: bank('Sub 1', 'PEQ', 'B'), sub2: bank('Sub 2', 'PEQ', 'B'),
});
for (const expert of [undefined, false, true]) {
    const before = JSON.stringify(mixedCatalog);
    const html = render({ outputSystem: { catalog: mixedCatalog, expert }, dsp: { presets } });
    assert.deepEqual(chips(html).map(chip => chip.text), expert
        ? ['Main L/R Neutral', 'Sub 1/2 PEQ'] : ['Sub 1/2 PEQ'],
    `expert=${expert}: only Expert includes Neutral area chips`);
    assert.equal(JSON.stringify(mixedCatalog), before, 'chip rendering never changes the catalog');
}

// All-Neutral Easy has no area chips, including merged Sub 1/2; Expert keeps them.
const neutralCatalog = catalog('surround', {
    global: bank('Global', 'Neutral'), main: bank('Front L/R', 'Neutral'),
    sub1: bank('Sub 1', 'Neutral'), sub2: bank('Sub 2', 'Neutral'),
});
assert.equal(render({ outputSystem: { catalog: neutralCatalog, expert: false }, dsp: { presets } }), '');
assert.deepEqual(chips(render({ outputSystem: { catalog: neutralCatalog, expert: true }, dsp: { presets } }))
    .map(chip => chip.text), ['Front L/R Neutral', 'Sub 1/2 Neutral']);

// Global status is preserved in both views, including Neutral and Direct.
for (const preset of ['Neutral', 'Direct', CONV]) {
    for (const expert of [false, true]) {
        const globalCatalog = catalog('stereo-sub', {
            global: bank('Global', preset), main: bank('Main L/R', 'Neutral'),
        }, 'main');
        assert.deepEqual(chips(render({ outputSystem: { catalog: globalCatalog, expert }, dsp: { presets } }))
            .map(chip => chip.tooltip), [`Global: ${preset}`]);
    }
}

// A view switch updates existing chips only; all other A/B fields stay unchanged.
{
    const state = { outputSystem: { catalog: mixedCatalog, expert: false }, dsp: { presets } };
    const elements = renderElements(state);
    const otherCompareFields = () => JSON.stringify(Object.fromEntries(Object.entries(elements)
        .filter(([key]) => key !== 'effectsCompareBanks')));
    const before = otherCompareFields();
    state.outputSystem.expert = true;
    BankUI.renderEffectsCompare();
    assert.deepEqual(chips(elements.effectsCompareBanks.innerHTML).map(chip => chip.text),
        ['Main L/R Neutral', 'Sub 1/2 PEQ']);
    assert.equal(otherCompareFields(), before, 'Expert changes no other A/B output');
    state.outputSystem.expert = false;
    BankUI.renderEffectsCompare();
    assert.deepEqual(chips(elements.effectsCompareBanks.innerHTML).map(chip => chip.text), ['Sub 1/2 PEQ']);
    assert.equal(otherCompareFields(), before, 'returning to Easy changes no other A/B output');

    const main = mixedCatalog.modes['stereo-sub'].banks.main;
    main.preset = CONV;
    main.active_side = 'B';
    BankUI.renderEffectsCompare();
    assert.deepEqual(chips(elements.effectsCompareBanks.innerHTML).map(chip => chip.text),
        ['Main L/R Conv LR…233313', 'Sub 1/2 PEQ'], 'a newly active area filter appears in Easy');
    main.preset = 'Neutral';
    main.active_side = 'A';
    BankUI.renderEffectsCompare();
    assert.deepEqual(chips(elements.effectsCompareBanks.innerHTML).map(chip => chip.text),
        ['Sub 1/2 PEQ'], 'returning an area bank to Neutral removes its chip');
}

// The page carries the container inside the Listening/Chain badge.
const page = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
assert.match(page, /id="effects-compare-active-badge"[\s\S]{0,400}id="effects-compare-chain"[\s\S]{0,200}id="effects-compare-banks"/);
assert.match(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'),
    /effectsCompareBanks: document\.getElementById\('effects-compare-banks'\)/);

console.log('active preset overview tests passed');
