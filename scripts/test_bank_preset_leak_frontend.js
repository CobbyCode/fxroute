#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Strict per-bank stock, wiring level: switching banks must never surface
// another bank's presets through visiblePresetEntriesForBank() — asserted
// against the real output_bank_ui.js picker wiring (catalog -> bank id ->
// presetsForBank) instead of the output_state.js helper in isolation.
// Untagged legacy reads as Global stock; All Banks lists none (joint A/B
// switching only). app.js keeps thin delegating wrappers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

require('../static/output_state.js');
const BankUI = require('../static/output_bank_ui.js');
const appSource = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
assert.equal(typeof BankUI.visiblePresetEntriesForBank, 'function', 'bank module owns the preset entry list');
assert.doesNotMatch(appSource, /\nfunction visiblePresetEntriesForBank\(/, 'shim dropped from app.js');
assert.match(appSource, /FXRouteBankUI/, 'app.js must delegate bank UI to the module');

function makeContext(presets, catalog) {
    const state = { dsp: { presets }, outputSystem: { catalog } };
    BankUI.init({ getState: () => state, getElements: () => ({}),
        showToast: () => {}, escapeHtml: (value) => String(value) });
    return state;
}

function namesFor(presets, catalog) {
    makeContext(presets, catalog);
    return BankUI.visiblePresetNamesForBank();
}

const presets = [
    { name: 'Direct' },
    { name: 'Neutral' },
    { name: 'LowCorr', bank: 'low' },
    { name: 'HighCorr', bank: 'high' },
    { name: 'MainCorr', bank: 'main' },
    { name: 'GlobCorr', bank: 'global' },
    { name: 'LegacyX' },
];

const catalog = (selectedBank) => ({
    revision: 7,
    active_mode: 'stereo-sub',
    modes: { 'stereo-sub': { selected_bank: selectedBank } },
});

// Every bank sees exactly its own stock plus built-ins, and never a foreign
// bank's presets — asserted after an explicit switch across all banks.
const expected = {
    global: ['Direct', 'Neutral', 'GlobCorr', 'LegacyX'],
    low: ['Direct', 'Neutral', 'LowCorr'],
    high: ['Direct', 'Neutral', 'HighCorr'],
    main: ['Direct', 'Neutral', 'MainCorr'],
};
const order = ['global', 'low', 'high', 'main'];
const seen = new Set();
for (let i = 0; i <= order.length; i += 1) {
    const from = order[i % order.length];
    const to = order[(i + 1) % order.length];
    const bankId = to;
    const names = namesFor(presets, catalog(bankId));
    assert.deepEqual(names, expected[bankId], `after switch ${from} -> ${to}`);
    seen.add(bankId);
    // Each foreign name is absent from every other bank's picker.
    for (const [owner, list] of Object.entries(expected)) {
        if (owner === bankId) continue;
        for (const name of list) {
            if (name === 'Direct' || name === 'Neutral') continue;
            assert.ok(!names.includes(name),
                `${bankId} must not list ${name} (owned by ${owner})`);
        }
    }
}
assert.equal(seen.size, 4, 'all four banks exercised');

// All Banks owns no presets: joint A/B switching only.
assert.deepEqual(namesFor(presets, catalog('all')), []);
// No catalog/module: unfiltered fallback.
assert.deepEqual((() => { makeContext(presets, null); return BankUI.visiblePresetNamesForBank(); })(),
    presets.map(p => p.name));
// Fallback when the module lacks presetsForBank.
{
    const outputState = require('../static/output_state.js');
    const real = outputState.presetsForBank;
    delete outputState.presetsForBank;
    try {
        makeContext(presets, catalog('low'));
        assert.deepEqual(BankUI.visiblePresetEntriesForBank(), presets);
    } finally {
        outputState.presetsForBank = real;
    }
}

console.log('bank preset leak frontend tests passed');
