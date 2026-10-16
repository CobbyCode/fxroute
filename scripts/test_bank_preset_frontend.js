#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Strict per-bank preset picker: each bank lists exclusively its own
// presets (plus built-ins), Global one set per output mode. A preset name is
// unique within its bank only. All Banks lists none (joint A/B switching only).
const assert = require('node:assert/strict');
const ui = require('../static/output_state.js');

const builtins = [{ name: 'Direct', bank: null, mode: null }, { name: 'Neutral', bank: null, mode: null }];
const presets = [
    ...builtins,
    { name: 'LowCorr', bank: 'low', mode: null },
    { name: 'HighCorr', bank: 'high', mode: null },
    { name: 'MainCorr', bank: 'main', mode: null },
    { name: 'StereoRoom', bank: 'global', mode: 'stereo' },
    { name: 'SubRoom', bank: 'global', mode: 'stereo-sub' },
    { name: 'SurroundRoom', bank: 'global', mode: 'surround', layout: 'surround' },
    // One name, a separate preset in every bank that has it.
    { name: 'Room', bank: 'global', mode: 'stereo', source_presets: ['a', 'b'] },
    { name: 'Room', bank: 'global', mode: 'stereo-sub' },
    { name: 'Room', bank: 'main', mode: null },
];
const names = (bankId, mode) => ui.presetsForBank(presets, bankId, mode).map(p => p.name);

assert.deepEqual(names('low', 'stereo-sub'), ['Neutral', 'LowCorr']);
assert.deepEqual(names('high', 'stereo-sub'), ['Neutral', 'HighCorr']);
// An area bank is the same library in every output mode.
for (const mode of ['stereo', 'stereo-sub', 'surround']) {
    assert.deepEqual(names('main', mode), ['Neutral', 'MainCorr', 'Room']);
    for (const bank of ui.rolesForMode(mode).concat(['low', 'low_mid', 'mid', 'high', 'main', 'side', 'rear', 'sub'])) {
        assert.equal(names(bank, mode).includes('Direct'), false, `${mode}/${bank} excludes Direct`);
        assert.equal(names(bank, mode).includes('Neutral'), true, `${mode}/${bank} offers Neutral`);
    }
}
// Every output mode lists its own Global presets and no other mode's.
assert.deepEqual(names('global', 'stereo'), ['Direct', 'Neutral', 'StereoRoom', 'Room']);
assert.deepEqual(names('global', 'stereo-sub'), ['Direct', 'Neutral', 'SubRoom', 'Room']);
assert.deepEqual(names('global', 'surround'), ['Direct', 'Neutral', 'SurroundRoom']);
// The entry a bank lists under a shared name is its own.
assert.deepEqual(ui.presetsForBank(presets, 'global', 'stereo').find(p => p.name === 'Room').source_presets, ['a', 'b']);
assert.equal(ui.presetsForBank(presets, 'main', 'stereo').find(p => p.name === 'Room').bank, 'main');
// A Global preset without its output mode belongs to no Global bank.
assert.deepEqual(ui.presetsForBank([...builtins, { name: 'Orphan', bank: 'global' }], 'global', 'stereo').map(p => p.name),
    ['Direct', 'Neutral']);
// All Banks owns no presets: joint A/B switching only.
assert.deepEqual(ui.presetsForBank(presets, 'all', 'stereo-sub'), []);
assert.deepEqual(ui.presetsForBank(presets, '', 'stereo-sub'), []);
assert.deepEqual(ui.presetsForBank('not-an-array', 'low', 'stereo-sub'), []);

assert.equal(ui.isBuiltinBankPreset('Direct'), true);
assert.equal(ui.isBuiltinBankPreset('Neutral'), true);
assert.equal(ui.isBuiltinBankPreset('LowCorr'), false);

// Combine, delete and download address the concrete bank; All Banks is rejected.
const catalog = (selected_bank) => ({
    revision: 3, active_mode: 'stereo-sub',
    modes: { 'stereo-sub': { selected_bank } },
});
assert.deepEqual(ui.bankOwner(catalog('main')), { bank_mode: 'stereo-sub', bank_id: 'main' });
assert.deepEqual(ui.bankOwner(catalog('global')), { bank_mode: 'stereo-sub', bank_id: 'global' });
assert.throws(() => ui.bankOwner(catalog('all')), /All Banks/);
assert.equal(ui.bankOwner(null), null);

console.log('bank preset frontend tests passed');
