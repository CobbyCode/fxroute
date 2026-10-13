#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// A result measured through All Banks can be saved and inspected, but it never
// becomes a filter: the app reports why, the PEQ editor refuses to take or
// create while one is shown, and the convolver editor refuses to take it.  A
// convolver Create follows its draft: a result shown after the take is no
// source, and the backend rejects a taken All Banks source.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
require('../static/measurement_dsp.js');
require('../static/measurement_ui.js');
const peqEditor = require('../static/measurement_peq_editor.js');
const convolverEditor = require('../static/measurement_convolver_editor.js');

const appSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const match = /function measurementImportBlockedReason\(\) \{[\s\S]*?\n\}\n/.exec(appSource);
assert.ok(match, 'app.js must define measurementImportBlockedReason');

function reasonFor({ current = [], saved = [], visible = {} }) {
    const state = { measurement: { measurements: saved, visibilityById: visible } };
    return new Function('state', 'getCurrentMeasurementEntries', `${match[0]}; return measurementImportBlockedReason();`)(
        state, () => current);
}

const throughAll = (id) => ({ id, measurement_target: { bank_id: 'all', mode: 'surround' } });
const throughBank = (id) => ({ id, measurement_target: { bank_id: 'rear', mode: 'surround' } });

assert.equal(reasonFor({}), '');
assert.equal(reasonFor({ current: [throughBank('a'), { id: 'legacy' }] }), '');
assert.match(reasonFor({ current: [throughAll('a')] }), /measured through All Banks/);
assert.match(reasonFor({ current: [{ id: 'grouped', measurement_target: {
    bank_id: 'all', mode: 'surround', global_group: 'rear', active_side: 'B' } }] }), /measured through All Banks/);
// A visible saved All Banks result blocks too; a hidden one does not.
assert.match(reasonFor({ saved: [throughBank('b'), throughAll('c')], visible: { b: true, c: true } }), /All Banks/);
assert.equal(reasonFor({ saved: [throughAll('c')], visible: { c: false } }), '');

const toasts = [];
const feedback = [];
const feedbackElement = {
    set textContent(value) { if (value) feedback.push(String(value)); },
    get textContent() { return ''; },
    classList: { add: () => {}, remove: () => {} },
};
let blocked = 'blocked reason';
const creates = [];
function init() {
    const state = { measurement: {} };
    const shared = {
        getState: () => state,
        showToast: (message, kind) => toasts.push([message, kind]),
        getElements: () => ({ measurementConvolverFeedback: feedbackElement, measurementPeqTakeFeedback: feedbackElement }),
        setTimeout: () => 1,
        renderMeasurementPanel: () => {},
        requireConcreteFilterBank: () => true,
        measurementImportBlockedReason: () => blocked,
        fetch: async (...args) => { creates.push(args); return { ok: true, json: async () => ({}) }; },
    };
    peqEditor.init(shared);
    convolverEditor.init(shared);
    return state;
}

(async () => {
    init();
    const peq = peqEditor.ensureMeasurementPeqState();
    peq.filters = [{ id: 'f0', freqHz: 1000, gainDb: -1, q: 1 }];
    peqEditor.takeMeasurementPeqToPreset('both');
    assert.deepEqual(toasts.pop(), ['blocked reason', 'warning']);
    assert.equal(peq.draft.leftBands.length, 0, 'a blocked take stages nothing');
    await peqEditor.createMeasurementPeqPresetFromDraft();
    assert.deepEqual(toasts.pop(), ['blocked reason', 'warning']);
    convolverEditor.takeMeasurementConvolverToDraft('both');
    assert.deepEqual(toasts.pop(), ['blocked reason', 'warning']);
    assert.ok(feedback.includes('blocked reason'));
    assert.equal(convolverEditor.ensureMeasurementConvolverState().draft.left, null, 'a blocked take stages nothing');
    await convolverEditor.createMeasurementConvolverPresetFromDraft();
    assert.deepEqual(toasts.pop(), ['Take L, R or Both into the convolver draft first', 'warning'],
        'the convolver Create looks at its draft, not at the results shown');
    assert.deepEqual(creates, [], 'a blocked create must not reach the backend');

    // Nothing blocks an ordinary result: the take stages its bands.
    blocked = '';
    peqEditor.takeMeasurementPeqToPreset('both');
    assert.equal(peq.draft.leftBands.length, 1);
    assert.deepEqual(toasts.pop(), ['Measurement PEQ staged Left and Right bands', 'success']);
    console.log('all banks import block: ok');
})().catch(error => { console.error(error); process.exitCode = 1; });
