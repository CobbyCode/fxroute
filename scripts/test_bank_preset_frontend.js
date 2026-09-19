#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Strict per-bank preset picker: each bank lists exclusively its own
// presets (plus built-ins); untagged legacy reads as Global stock, where it
// historically lived. All Banks lists none (joint A/B switching only).
const assert = require('node:assert/strict');
const ui = require('../static/output_state.js');

const presets = [
    { name: 'Direct' },
    { name: 'Neutral' },
    { name: 'LowCorr', bank: 'low' },
    { name: 'HighCorr', bank: 'high' },
    { name: 'GlobCorr', bank: 'global' },
    { name: 'MainCorr', bank: 'main' },
    { name: 'LegacyX' },
    { name: 'LegacyNull', bank: null },
];

assert.deepEqual(ui.presetsForBank(presets, 'low').map(p => p.name),
    ['Direct', 'Neutral', 'LowCorr']);
assert.deepEqual(ui.presetsForBank(presets, 'high').map(p => p.name),
    ['Direct', 'Neutral', 'HighCorr']);
assert.deepEqual(ui.presetsForBank(presets, 'global').map(p => p.name),
    ['Direct', 'Neutral', 'GlobCorr', 'LegacyX', 'LegacyNull']);
assert.deepEqual(ui.presetsForBank(presets, 'main').map(p => p.name),
    ['Direct', 'Neutral', 'MainCorr']);
// No foreign presets leak into another bank's dropdown.
for (const bankId of ['low', 'high', 'main']) {
    const names = ui.presetsForBank(presets, bankId).map(p => p.name);
    assert.ok(!names.includes('GlobCorr'), `${bankId} must not list Global presets`);
    assert.ok(!names.includes('LegacyX'), `${bankId} must not list legacy stock`);
}
// All Banks owns no presets: joint A/B switching only.
assert.deepEqual(ui.presetsForBank(presets, 'all'), []);
assert.deepEqual(ui.presetsForBank(presets, ''), []);
assert.deepEqual(ui.presetsForBank('not-an-array', 'low'), []);

assert.equal(ui.isBuiltinBankPreset('Direct'), true);
assert.equal(ui.isBuiltinBankPreset('Neutral'), true);
assert.equal(ui.isBuiltinBankPreset('LowCorr'), false);

assert.equal(ui.presetBank({ name: 'x', bank: 'low' }), 'low');
assert.equal(ui.presetBank({ name: 'x', bank: '  ' }), null);
assert.equal(ui.presetBank({ name: 'x' }), null);
assert.equal(ui.presetBank({ metadata: { bank: 'high' } }), 'high');
assert.equal(ui.presetBank(null), null);

// Combine targets the concrete bank; All Banks is rejected.
const catalog = (selected_bank) => ({
    revision: 3, active_mode: 'stereo-sub',
    modes: { 'stereo-sub': { selected_bank } },
});
assert.deepEqual(ui.combineBank(catalog('main')), { bank_mode: 'stereo-sub', bank_id: 'main' });
assert.deepEqual(ui.combineBank(catalog('global')), { bank_mode: 'stereo-sub', bank_id: 'global' });
assert.throws(() => ui.combineBank(catalog('all')), /All Banks/);

console.log('bank preset frontend tests passed');
