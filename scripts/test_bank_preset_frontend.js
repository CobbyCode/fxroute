#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Strict per-bank preset picker: each bank lists exclusively its own
// presets (plus built-ins); untagged legacy reads as Global stock, where it
// historically lived. Every output mode owns its Global presets. All Banks
// lists none (joint A/B switching only).
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

// Every output mode lists its own Global presets and no other mode's.
const layered = [
    { name: 'Direct' },
    { name: 'Neutral' },
    { name: 'StereoRoom', bank: 'global', mode: 'stereo' },
    { name: 'SubRoom', bank: 'global', mode: 'stereo-sub' },
    { name: 'OldRoom', bank: 'global' },
    { name: 'LegacyRoom' },
    { name: 'SurroundRoom', bank: 'global', layout: 'surround', mode: 'surround' },
    { name: 'OldSurround', bank: 'global', layout: 'surround' },
    { name: 'FrontOnly', bank: 'main' },
];
const globalNames = (mode) => ui.presetsForBank(layered, 'global', mode).map(p => p.name);
assert.deepEqual(globalNames('surround'), ['Direct', 'Neutral', 'SurroundRoom', 'OldSurround']);
// A Global preset from before the split belongs to Stereo; a file without
// any bank tag has no owner until the backend migrates it.
assert.deepEqual(globalNames('stereo'), ['Direct', 'Neutral', 'StereoRoom', 'OldRoom', 'LegacyRoom']);
assert.deepEqual(globalNames(undefined), globalNames('stereo'));
assert.deepEqual(globalNames('stereo-sub'), ['Direct', 'Neutral', 'SubRoom', 'LegacyRoom']);
for (const [owner, name] of [['stereo', 'StereoRoom'], ['stereo-sub', 'SubRoom'], ['surround', 'SurroundRoom']]) {
    for (const mode of ['stereo', 'stereo-sub', 'surround']) {
        assert.equal(globalNames(mode).includes(name), mode === owner, `${name} in ${mode} Global`);
    }
}
assert.deepEqual(ui.presetsForBank(layered, 'main', 'surround').map(p => p.name),
    ['Direct', 'Neutral', 'FrontOnly']);
assert.equal(ui.presetMode({ name: 'x', bank: 'global', mode: 'stereo-sub' }), 'stereo-sub');
assert.equal(ui.presetMode({ name: 'x', bank: 'global' }), 'stereo');
assert.equal(ui.presetMode({ name: 'x', bank: 'global', layout: 'surround' }), 'surround');

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
