#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const ui = require('../static/output_state.js');

const bank = (id, label, roles, channel_mode = 'stereo') => ({
    id, label, roles, channel_mode, preset: 'Neutral', preset_a: 'Neutral',
    preset_b: 'Room', active_side: 'A', can_a: true, can_b: true,
});
const config = {
    selected_bank: 'main',
    banks: {
        global: bank('global', 'Global', ['global']),
        main: bank('main', 'Main L/R', ['main_l', 'main_r']),
        sub1: bank('sub1', 'Sub 1', ['sub1'], 'mono'),
    },
    topology: { roles: ['main_l', 'main_r', 'sub1'] },
    all_banks: { preset: null, preset_a: 'Neutral', preset_b: null, active_side: null, can_a: true, can_b: false },
};
const catalog = { revision: 7, active_mode: 'stereo-sub', modes: { 'stereo-sub': config } };
assert.deepEqual(ui.bankOptions(config).map(b => b.id), ['global', 'all', 'main', 'sub1']);
assert.equal(ui.bankSelectorVisible(config), true);
assert.equal(ui.bankSelectorVisible({ ...config, topology: { roles: ['main_l', 'main_r'] } }), false);
assert.equal(ui.measurementArea(catalog).channel, 'stereo');
assert.equal(ui.measurementArea(catalog).channel_mode, 'stereo');
assert.equal(ui.measurementArea(catalog).repeat_supported, true);
assert.equal(ui.measurementArea(catalog, 'sub1').channel_mode, 'mono');
assert.equal(ui.measurementArea(catalog, 'sub1').repeat_supported, false);
assert.deepEqual(ui.peqParams('mono', [{ frequencyHz: 100 }], [{ frequencyHz: 200 }], 'IIR'),
    { channelMode: 'stereo-linked', eqMode: 'IIR', bands: [{ frequencyHz: 100 }] });
assert.deepEqual(ui.peqParams('stereo', [{ frequencyHz: 100 }], [{ frequencyHz: 200 }], 'IIR'),
    { channelMode: 'dual', eqMode: 'IIR', leftBands: [{ frequencyHz: 100 }], rightBands: [{ frequencyHz: 200 }] });
assert.deepEqual(ui.bankBinding(catalog), { bank_mode: 'stereo-sub', bank_id: 'main', expected_revision: 7 });
assert.equal(ui.compareState(catalog).presetB, 'Room');
config.selected_bank = 'all';
assert.equal(ui.compareState(catalog).aggregate, true);
assert.equal(ui.compareState(catalog).effectiveActiveSide, null);
assert.equal(ui.compareState(catalog).canB, false);
assert.equal(ui.measurementArea(catalog).available, false);
assert.throws(() => ui.bankBinding(catalog), /Select a filter bank/);
assert.deepEqual(ui.buildMutation('switch_all_banks', { mode: 'stereo-sub', active_side: 'A' }),
    { kind: 'switch_all_banks', mode: 'stereo-sub', active_side: 'A' });
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const calls = [];
const context = {
    state: { outputSystem: { catalog }, dsp: { compare: {}, active_preset: 'Wrong legacy preset' } },
    elements: { effectsCompareA: { value: 'Neutral' }, effectsCompareB: { value: 'Room' },
        effectsMeasureOpenBtn: { disabled: false, title: '' },
        effectsToggleImportBtn: { disabled: false, title: '' } },
    effectsCompareLoadInFlight: false,
    outputSystemModule: () => ui,
    measurementAreaFromCatalog: () => ui.measurementArea(context.state.outputSystem.catalog),
    applyOutputSystemMutation: async (kind, fields) => { calls.push({ kind, ...fields }); return {}; },
    renderEffectsCompare() {}, renderEffects() {}, setEffectsCompareLoadBusy() {},
    showToast(message) { throw new Error(message); }, console,
    fetch() { throw new Error('Bank compare must not call the legacy global preset endpoint'); },
};
vm.createContext(context);
for (const name of ['getEmptyEffectsCompareState', 'normalizeEffectsCompareSelection',
    'getEffectiveEffectsCompareSide', 'getEffectsCompareState', 'getEffectsCompareToggleTarget',
    'handleEffectsCompareSelectionChange', 'loadEffectsComparePreset', 'toggleComparePreset',
    'syncBankActionButtons']) {
    const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`));
    assert.ok(match, name);
    vm.runInContext(match[0], context);
}
(async () => {
    config.selected_bank = 'main';
    assert.equal(context.getEffectsCompareState().activePreset, 'Neutral');
    await context.handleEffectsCompareSelectionChange('B');
    assert.deepEqual(JSON.parse(JSON.stringify(calls.pop())), {
        kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'main', preset_b: 'Room',
    });
    await context.toggleComparePreset();
    assert.deepEqual(JSON.parse(JSON.stringify(calls.pop())), {
        kind: 'set_bank_preset', mode: 'stereo-sub', bank_id: 'main', active_side: 'B',
    });
    config.selected_bank = 'all';
    config.all_banks.can_b = true;
    await context.toggleComparePreset();
    assert.deepEqual(JSON.parse(JSON.stringify(calls.pop())), {
        kind: 'switch_all_banks', mode: 'stereo-sub', active_side: 'A',
    });
    // All Banks only switches A/B jointly: Measure and Import stay disabled.
    // Global and concrete banks keep both actions enabled.
    const buttons = [context.elements.effectsMeasureOpenBtn, context.elements.effectsToggleImportBtn];
    for (const selected of ['main', 'global']) {
        config.selected_bank = selected;
        context.syncBankActionButtons();
        assert.deepEqual(buttons.map(button => button.disabled), [false, false]);
        assert.deepEqual(buttons.map(button => button.title), ['', '']);
    }
    config.selected_bank = 'all';
    context.syncBankActionButtons();
    assert.deepEqual(buttons.map(button => button.disabled), [true, true]);
    assert.match(buttons[0].title, /All Banks/);
    assert.match(buttons[1].title, /All Banks/);
    console.log('Paired bank frontend tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
