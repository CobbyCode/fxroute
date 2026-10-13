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

// Only the active mode's banks, Global first even when the catalog lists it later.
assert.deepEqual(ui.activePresetOverview(catalog('stereo-sub', {
    main: bank('Main L/R', 'Neutral'), global: bank('Global', CONV, 'B'),
    sub1: bank('Sub 1', 'PEQ', 'B'), sub2: bank('Sub 2', 'PEQ', 'B'),
})), [
    { ids: ['global'], label: 'Global', preset: CONV },
    { ids: ['main'], label: 'Main L/R', preset: 'Neutral' },
    { ids: ['sub1', 'sub2'], label: 'Sub 1/2', preset: 'PEQ' },
]);

// Numbered banks merge only in a row, of one family, on the same preset.
assert.deepEqual(ui.activePresetOverview(catalog('stereo-sub', {
    global: bank('Global', 'Neutral'), sub1: bank('Sub 1', 'PEQ'), sub2: bank('Sub 2', 'Direct'),
})).map(entry => `${entry.label}=${entry.preset}`), ['Global=Neutral', 'Sub 1=PEQ', 'Sub 2=Direct']);
assert.deepEqual(ui.activePresetOverview(catalog('surround', {
    global: bank('Global', 'Neutral'), sub1: bank('Sub 1', 'Direct'), center: bank('Center', 'Direct'),
    sub2: bank('Sub 2', 'Direct'), sub3: bank('Sub 3', 'Direct'),
})).map(entry => entry.label), ['Global', 'Sub 1', 'Center', 'Sub 2/3']);

// Channels on different presets have no single preset; they never merge.
assert.deepEqual(ui.activePresetOverview(catalog('stereo-sub', {
    global: bank('Global', 'Neutral'), sub1: bank('Sub 1', null), sub2: bank('Sub 2', null),
})).map(entry => [entry.label, entry.preset]), [['Global', 'Neutral'], ['Sub 1', null], ['Sub 2', null]]);

// A bank without a label falls back to the role label of the mode.
assert.deepEqual(ui.activePresetOverview(catalog('surround', {
    global: { preset: 'Neutral' }, main: { preset: 'Direct' },
})).map(entry => entry.label), ['Global', 'Front L/R']);
assert.deepEqual(ui.activePresetOverview(null), []);
assert.deepEqual(ui.activePresetOverview({ active_mode: 'stereo', modes: {} }), []);

// Rendered into the compare badge: one chip per entry, the selected bank marked.
function render(state) {
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
    return elements.effectsCompareBanks.innerHTML;
}
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
        { selected: true, tooltip: 'Main L/R: A&amp;B &lt;x&gt;', text: 'Main L/R A&amp;B &lt;x&gt;' },
        { selected: false, tooltip: 'Sub 1/2: Direct', text: 'Sub 1/2 Direct' },
    ]);
    assert.ok(!html.includes('<x>'), 'preset names are escaped');
}
// A merged entry is marked when one of its banks is the selected one; All Banks marks none.
assert.deepEqual(chips(render({ outputSystem: { catalog: catalog('stereo-sub', banks, 'sub2') }, dsp: { presets } }))
    .map(chip => chip.selected), [false, false, true]);
assert.deepEqual(chips(render({ outputSystem: { catalog: {
    ...catalog('stereo-sub', banks, 'all'), modes: { 'stereo-sub': { selected_bank: 'all', banks,
        all_banks: { preset: null, preset_a: null, preset_b: null, active_side: null, can_a: true, can_b: true } } },
} }, dsp: { presets } })).map(chip => chip.selected), [false, false, false]);
// Mixed channel bindings read as per-channel; without a catalog the line is empty.
assert.match(render({ outputSystem: { catalog: catalog('stereo-sub', { global: bank('Global', 'Neutral'),
    main: bank('Main L/R', null) }) }, dsp: { presets } }), /Main L\/R<\/span> <span class="effects-compare-bank-preset">Per-channel</);
assert.equal(render({ outputSystem: { catalog: null }, dsp: { presets, compare: {}, active_preset: 'Neutral' } }), '');

// The page carries the container inside the Listening/Chain badge.
const page = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
assert.match(page, /id="effects-compare-active-badge"[\s\S]{0,400}id="effects-compare-chain"[\s\S]{0,200}id="effects-compare-banks"/);
assert.match(fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'),
    /effectsCompareBanks: document\.getElementById\('effects-compare-banks'\)/);

console.log('active preset overview tests passed');
