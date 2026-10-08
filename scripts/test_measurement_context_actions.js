#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Context actions must disappear, while matching stays available across banks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const OutputState = require('../static/output_state.js');
const app = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
function element() {
    const hidden = new Set();
    const row = { classList: { toggle(name, value) { value ? hidden.add(name) : hidden.delete(name); }, contains: name => hidden.has(name) } };
    return { disabled: false, title: '', closest: () => row, row };
}
const elements = Object.fromEntries(['measurementHybridOpenBtn', 'measurementSweepStartBtn', 'levelMatchOpen', 'polarityCheckOpen', 'measurementRepeatStartBtn'].map(name => [name, element()]));
const catalog = { active_mode: 'stereo-sub', modes: { 'stereo-sub': { topology: { mode: 'stereo-sub', roles: ['main_l', 'main_r', 'sub_l', 'sub_r'], sub_mode: 'stereo' }, banks: {
    global: { channel_mode: 'stereo' }, main: { channel_mode: 'stereo' }, sub: { channel_mode: 'stereo' },
} } } };
const context = { elements, state: { measurement: {} }, outputSystemModule: () => OutputState,
    ensureOutputSystemBoxes() {}, window: { FXRouteBankUI: { renderSweepGroupRow() {} } },
    syncSweepSideRow() {}, MeasurementJob: { getActiveMeasurementKind: () => '' },
};
vm.createContext(context);
for (const name of ['renderMeasurementArea', 'syncMeasurementCalibrationMenu', 'syncMeasurementRepeatNote']) {
    const source = app.match(new RegExp(`function ${name}\\([^]*?\\n\\}\\n`))?.[0];
    assert.ok(source, name);
    vm.runInContext(source, context);
}
for (const [bank, repeat, advanced, polarity] of [
    ['global', true, true, false], ['all', true, false, false], ['main', true, true, true], ['sub', false, false, false],
]) {
    catalog.modes['stereo-sub'].selected_bank = bank;
    const area = OutputState.measurementArea(catalog);
    context.measurementAreaFromCatalog = () => area;
    context.renderMeasurementArea();
    context.syncMeasurementRepeatNote(false, area.repeat_supported ? '' : 'No LR repeat');
    assert.equal(elements.levelMatchOpen.row.classList.contains('hidden'), false, `${bank}: mode matching`);
    assert.equal(elements.polarityCheckOpen.row.classList.contains('hidden'), !polarity, `${bank}: polarity`);
    assert.equal(elements.measurementHybridOpenBtn.row.classList.contains('hidden'), !advanced, `${bank}: advanced`);
    assert.equal(elements.measurementRepeatStartBtn.row.classList.contains('hidden'), !repeat, `${bank}: repeat`);
}
context.syncMeasurementRepeatNote(true, 'No LR repeat');
assert.equal(elements.measurementRepeatStartBtn.row.classList.contains('hidden'), false, 'running repeat remains cancellable');
catalog.active_mode = 'stereo';
catalog.modes.stereo = { selected_bank: 'global', topology: { mode: 'stereo', roles: ['main_l', 'main_r'] }, banks: { global: {} } };
context.measurementAreaFromCatalog = () => OutputState.measurementArea(catalog);
context.renderMeasurementArea();
assert.equal(elements.polarityCheckOpen.row.classList.contains('hidden'), false, 'implicit Stereo speaker pair');
console.log('measurement context actions ok');
