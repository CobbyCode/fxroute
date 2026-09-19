#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Strict per-bank stock, wiring level: switching banks must never surface
// another bank's presets through visiblePresetEntriesForBank() — the same
// guard as test_bank_preset_frontend.js, but asserted against the actual
// app.js picker wiring (catalog -> bank id -> presetsForBank) instead of the
// output_state.js helper in isolation. Untagged legacy reads as Global stock;
// All Banks lists none (joint A/B switching only).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');

// Pull a function body out of app.js by name (brace counting).
function extractFunction(name) {
    const start = source.indexOf(`function ${name}(`);
    assert.ok(start !== -1, `function ${name} found in app.js`);
    let depth = 0;
    let end = -1;
    for (let i = source.indexOf('{', start); i < source.length; i += 1) {
        if (source[i] === '{') depth += 1;
        if (source[i] === '}') {
            depth -= 1;
            if (depth === 0) { end = i + 1; break; }
        }
    }
    assert.ok(end !== -1, `function ${name} body complete`);
    return source.slice(start, end);
}

function makeContext(presets, catalog) {
    const context = {
        state: { dsp: { presets }, outputSystem: { catalog } },
        outputSystemModule: () => require('../static/output_state.js'),
        window: {},
        console,
    };
    vm.createContext(context);
    vm.runInContext(extractFunction('visiblePresetEntriesForBank'), context);
    vm.runInContext(extractFunction('visiblePresetNamesForBank'), context);
    return context;
}

function namesFor(presets, catalog) {
    return vm.runInContext('visiblePresetNamesForBank()', makeContext(presets, catalog));
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
assert.deepEqual(
    vm.runInContext('visiblePresetNamesForBank()',
        makeContext(presets, null)),
    presets.map(p => p.name));
// Fallback when the module lacks presetsForBank (defensive parity with app.js).
{
    const context = {
        state: { dsp: { presets }, outputSystem: { catalog: catalog('low') } },
        outputSystemModule: () => ({}),
        window: {},
        console,
    };
    vm.createContext(context);
    vm.runInContext(extractFunction('visiblePresetEntriesForBank'), context);
    assert.deepEqual(vm.runInContext('visiblePresetEntriesForBank()', context),
        presets);
}

console.log('bank preset leak frontend tests passed');
