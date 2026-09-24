#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Convolver Take-Both staging regression: changing a correction-defining
// setting (target curve, range, boost/cut limits, dip guard) after staging
// a draft must invalidate that draft. Otherwise the stale staged analysis
// (old target) is silently saved under the new target's name and headroom,
// which is exactly the "second save after target switch fails to reflect
// the new calculation" defect: the visible numbers update, but Create
// reuses the old staged FIR.

const assert = require('assert/strict');

require('../static/measurement_dsp.js');
require('../static/measurement_ui.js');
const convolverEditor = require('../static/measurement_convolver_editor.js');

function stagedDraft(targetCurve = 'neutral') {
    return {
        left: { side: 'left', phaseMode: 'minimum', analysis: { autoGainDb: -4 }, metadata: { targetCurve } },
        right: { side: 'right', phaseMode: 'minimum', analysis: { autoGainDb: -4 }, metadata: { targetCurve } },
        presetName: 'staged',
        nameTouched: true,
        notice: '',
    };
}

function makeContext() {
    const state = {
        measurement: {
            activeEditor: 'none',
            houseCurveOptions: [],
            measurementSampleRate: '48000',
            convolverAssistant: {
                targetCurve: 'neutral',
                rangeStartHz: 20,
                rangeEndHz: 250,
                maxBoostDb: 6,
                maxCutDb: -9,
                dipGuard: 'off',
                phaseMode: 'minimum',
                irLength: '8192',
                quality: 'minimum_8192',
                draft: stagedDraft('neutral'),
            },
        },
    };
    const feedback = [];
    const feedbackElement = {
        _text: '',
        set textContent(value) { this._text = String(value); if (value) feedback.push(String(value)); },
        get textContent() { return this._text; },
        classList: { add: () => {}, remove: () => {} },
    };
    convolverEditor.init({
        getState: () => state,
        getElements: () => ({ measurementConvolverFeedback: feedbackElement }),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        scheduleMeasurementGraphRender: () => {},
        saveMeasurementSetupSettings: () => {},
    });
    const context = {
        state,
        updateMeasurementConvolverField: (...args) => convolverEditor.updateMeasurementConvolverField(...args),
    };
    return { context, state, feedback };
}

function convOf(context) {
    return context.state.measurement.convolverAssistant;
}

// 1. Target switch invalidates a staged draft.
{
    const { context, feedback } = makeContext();
    context.updateMeasurementConvolverField('targetCurve', 'harman');
    const conv = convOf(context);
    assert.equal(conv.targetCurve, 'harman');
    assert.equal(conv.draft.left, null, 'stale left draft must be cleared on target switch');
    assert.equal(conv.draft.right, null, 'stale right draft must be cleared on target switch');
    assert.equal(conv.draft.presetName, '', 'stale staged name must be cleared on target switch');
    assert.equal(conv.draft.nameTouched, false);
    assert.match(conv.draft.notice, /Take L\/R again/, 'retake hint must be shown');
    assert.ok(feedback.some((message) => /Take L\/R again/.test(message)), 'retake hint must be surfaced');
}

// 2. Same-value target selection keeps the draft (no-op, no spurious clear).
{
    const { context, feedback } = makeContext();
    context.updateMeasurementConvolverField('targetCurve', 'neutral');
    const conv = convOf(context);
    assert.ok(conv.draft.left && conv.draft.right, 'unchanged target must keep the staged draft');
    assert.equal(conv.draft.presetName, 'staged');
    assert.equal(feedback.length, 0, 'no-op target selection must not notify');
}

// 3. Invalid target value keeps both target and draft.
{
    const { context } = makeContext();
    context.updateMeasurementConvolverField('targetCurve', 'no-such-curve');
    const conv = convOf(context);
    assert.equal(conv.targetCurve, 'neutral');
    assert.ok(conv.draft.left && conv.draft.right, 'rejected target must keep the staged draft');
}

// 4. Range and limit changes invalidate the draft too.
for (const [field, value, hint] of [
    ['rangeStartHz', 30, /range/i],
    ['rangeEndHz', 3000, /range/i],
    ['maxBoostDb', 3, /limits/i],
    ['maxCutDb', -12, /limits/i],
    ['dipGuard', 'gentle', /Dip guard/i],
]) {
    const { context } = makeContext();
    context.updateMeasurementConvolverField(field, value);
    const conv = convOf(context);
    assert.equal(conv.draft.left, null, `${field} change must clear the staged draft`);
    assert.equal(conv.draft.right, null, `${field} change must clear the staged draft`);
    assert.match(conv.draft.notice, hint, `${field} change must hint at retake`);
}

// 5. Target switch with no staged draft stays quiet (no phantom notice).
{
    const { context, feedback } = makeContext();
    convOf(context).draft = { left: null, right: null, presetName: '', nameTouched: false, notice: '' };
    context.updateMeasurementConvolverField('targetCurve', 'harman');
    const conv = convOf(context);
    assert.equal(conv.targetCurve, 'harman');
    assert.equal(conv.draft.notice, '', 'empty draft must not gain a retake notice');
    assert.equal(feedback.length, 0, 'empty draft must not notify');
}

// 6. Phase changes keep their dedicated path and message.
{
    const { context } = makeContext();
    context.updateMeasurementConvolverField('phaseMode', 'linear');
    const conv = convOf(context);
    assert.equal(conv.phaseMode, 'linear');
    assert.equal(conv.draft.left, null);
    assert.match(conv.draft.notice, /Phase type changed/, 'phase change keeps its own notice');
}

console.log('convolver take-both staging tests: ok');
