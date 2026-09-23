#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Graph edits and preset creation share the Effects PEQ in-flight gate.
const assert = require('node:assert/strict');
const dsp = require('../static/measurement_dsp.js');
require('../static/measurement_ui.js');
const editor = require('../static/measurement_peq_editor.js');

const state = { measurement: { activeEditor: 'peq' }, dsp: {} };
const bounds = { left: 40, top: 10, width: 200, height: 100 };
const range = { minDb: -12, maxDb: 12 };
const pointer = { x: 140, y: dsp.measurementDbToY(0, bounds, range), bounds, range };
let graphPointerId = null;
let sharedBusy = false;
let resolvePost;
const calls = [];
const elements = {
    measurementGraph: { setPointerCapture: (id) => calls.push(['capture', id]) },
    measurementPeqPanel: { classList: { contains: () => false }, focus: () => calls.push('focus') },
    measurementPeqCreateBtn: { disabled: false },
    measurementPeqPresetName: { value: 'Room PEQ' },
};
editor.init({
    getState: () => state, getElements: () => elements,
    getMeasurementActiveEditor: () => state.measurement.activeEditor,
    setMeasurementActiveEditor: (mode) => { state.measurement.activeEditor = mode; },
    renderMeasurementPanel: () => calls.push('render'),
    scheduleMeasurementGraphRender: () => calls.push('graph'),
    getGraphPointerId: () => graphPointerId,
    setGraphPointerId: (id) => { graphPointerId = id; },
    getMeasurementGraphPointerPosition: () => pointer,
    showToast: (message, kind) => calls.push(['toast', message, kind]),
    requireConcreteFilterBank: () => true,
    bankBindingJson: () => ({ bank_id: 'main' }),
    measurementCommitSourceId: () => 'measurement-1',
    measurementPeqParams: (leftBands, rightBands, mode) => ({ leftBands, rightBands, mode }),
    fetch: async (url, options) => {
        calls.push(['post', url, JSON.parse(options.body)]);
        return new Promise((resolve) => { resolvePost = resolve; });
    },
    fetchEffects: async () => { calls.push('effects'); },
    fetchOutputSystemCatalog: () => { calls.push('catalog'); },
    isPeqCreateInFlight: () => sharedBusy,
    setPeqCreateInFlight: (active) => { sharedBusy = active; calls.push(['busy', active]); },
});

async function main() {
    const down = { pointerId: 7, pointerType: 'touch', preventDefault: () => calls.push('prevent') };
    editor.handleMeasurementPeqPointerDown(down, pointer, 'touch');
    const peq = editor.ensureMeasurementPeqState();
    assert.equal(peq.filters.length, 1);
    assert.equal(peq.dragFilterId, peq.filters[0].id);
    assert.equal(graphPointerId, 7);
    assert.deepEqual(calls.slice(0, 2), ['prevent', ['capture', 7]]);
    pointer.x = 175;
    editor.handleMeasurementPeqPointerMove({ pointerId: 7, pointerType: 'mouse' });
    assert.equal(peq.filters[0].freqHz, Math.round(dsp.measurementXToFrequency(175, bounds)));
    editor.clearMeasurementPeqPointerDrag();
    assert.equal(peq.dragFilterId, null);
    const currentCount = peq.filters.length;
    pointer.x = 100;
    editor.handleMeasurementPeqPointerDown(down, pointer, 'touch');
    assert.equal(peq.filters.length, currentCount, 'touch cooldown prevents duplicate creation');
    assert.equal(editor.stepMeasurementPeqGain(peq.filters[0].id, 1, 0.1), 0.1);

    editor.takeMeasurementPeqToPreset('both');
    assert.equal(peq.draft.leftBands.length, 1);
    const creating = editor.createMeasurementPeqPresetFromDraft();
    assert.equal(sharedBusy, true);
    await editor.createMeasurementPeqPresetFromDraft();
    assert.equal(calls.filter((call) => call[0] === 'post').length, 1);
    const posted = calls.find((call) => call[0] === 'post')[2];
    assert.equal(posted.source_measurement_id, 'measurement-1');
    assert.equal(posted.bank_id, 'main');
    resolvePost({ ok: true, json: async () => ({ preset: { name: 'Room PEQ' } }) });
    await creating;
    assert.equal(sharedBusy, false);
    assert.deepEqual(calls.slice(-4).map((call) => Array.isArray(call) ? call[0] : call),
        ['catalog', 'toast', 'busy', 'render']);
    assert.equal(peq.draft.leftBands.length, 0);
    console.log('measurement PEQ graph and preset creation: ok');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
