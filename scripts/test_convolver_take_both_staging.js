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
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MeasurementUI = require('../static/measurement_ui.js');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    let parenDepth = 1;
    let brace = -1;
    for (let index = match.index + match[0].length; index < source.length; index += 1) {
        if (source[index] === '(') parenDepth += 1;
        if (source[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) { brace = source.indexOf('{', index); break; }
    }
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

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
    const context = {
        MeasurementUI,
        MeasurementDsp: require(path.join(__dirname, '..', 'static', 'measurement_dsp.js')),
        measurementConvolverCurves: MeasurementUI.measurementConvolverCurves,
        measurementConvolverPhaseModes: MeasurementUI.measurementConvolverPhaseModes,
        measurementConvolverTapOptions: MeasurementUI.measurementConvolverTapOptions,
        state,
        feedback,
        showMeasurementConvolverFeedback: (message) => { feedback.push(message); },
        renderMeasurementPanel: () => {},
        scheduleMeasurementGraphRender: () => {},
        saveMeasurementSetupSettings: () => {},
        clampMeasurementConvolverFrequency: MeasurementUI.clampMeasurementConvolverFrequency
            || ((value, fallback = 20) => {
                const numeric = Number(value);
                return Math.min(20000, Math.max(20, Number.isFinite(numeric) ? numeric : fallback));
            }),
        getMeasurementConvolverTypeKeys: MeasurementUI.getMeasurementConvolverTypeKeys,
        getMeasurementConvolverPhaseModeForType: MeasurementUI.getMeasurementConvolverPhaseModeForType,
        getMeasurementConvolverFirLengthForType: MeasurementUI.getMeasurementConvolverFirLengthForType,
    };
    vm.createContext(context);
    vm.runInContext([
        'clampMeasurementConvolverFrequency',
        'getDefaultMeasurementConvolverState',
        'getMeasurementConvolverCurveOptions',
        'getMeasurementConvolverCurve',
        'ensureMeasurementConvolverState',
        'clearMeasurementConvolverDraftForPhaseChange',
        'clearMeasurementConvolverDraftForSettingsChange',
        'updateMeasurementConvolverField',
    ].map(extractFunction).join('\n'), context);
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
