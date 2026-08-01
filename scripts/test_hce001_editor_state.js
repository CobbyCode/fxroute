#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

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
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'\"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const state = {
    measurement: {
        activeEditor: 'none',
        houseCurveOptions: [],
        customHouseCurve: { open: false, points: [], activePointId: null, name: '', nameTouched: false, saving: false },
        peqAssistant: { filters: [], enabled: false, activeFilterId: null, dragFilterId: null, draft: { leftBands: [], rightBands: [] } },
        convolverAssistant: { targetCurve: 'neutral', dragMode: null },
    },
};
const context = {
    state,
    renderMeasurementPanel: () => {},
    scheduleMeasurementGraphRender: () => {},
    ensureMeasurementPeqState: () => state.measurement.peqAssistant,
    ensureMeasurementConvolverState: () => state.measurement.convolverAssistant,
    getCustomHouseCurveNameSuggestion: () => 'Custom House Curve 1',
    updateMeasurementConvolverField: (field, value) => { if (field === 'targetCurve') state.measurement.convolverAssistant.targetCurve = value; },
};
vm.createContext(context);
vm.runInContext([
    'ensureCustomHouseCurveState', 'getMeasurementActiveEditor', 'setMeasurementActiveEditor',
    'openCustomHouseCurveEditor', 'addCustomHouseCurvePoint', 'handleMeasurementTargetCurveSelection',
    'getMeasurementHouseCurvePreviewPoints',
].map(extractFunction).join('\n'), context);

// 1. Custom opens deterministically and PEQ is closed.
state.measurement.peqAssistant.filters = [{ id: 'f1' }];
context.setMeasurementActiveEditor('peq');
context.openCustomHouseCurveEditor();
assert.equal(context.getMeasurementActiveEditor(), 'houseCurve');
assert.equal(state.measurement.customHouseCurve.open, true);
assert.equal(state.measurement.customHouseCurve.points.length, 1);
const preservedInitialPointId = state.measurement.customHouseCurve.points[0].id;

// 2. PEQ activation closes House Curve.
context.setMeasurementActiveEditor('peq');
assert.equal(context.getMeasurementActiveEditor(), 'peq');
assert.equal(state.measurement.customHouseCurve.open, false);

// 3. A normal target closes House Curve.
context.setMeasurementActiveEditor('houseCurve');
context.handleMeasurementTargetCurveSelection('harman');
assert.equal(context.getMeasurementActiveEditor(), 'none');
assert.equal(state.measurement.customHouseCurve.open, false);
assert.equal(state.measurement.convolverAssistant.targetCurve, 'harman');

// 4. The sentinel is an action: handling it reopens the editor, while the
// actual target remains selected so the action can be chosen again.
context.handleMeasurementTargetCurveSelection('create-custom-house-curve');
assert.equal(context.getMeasurementActiveEditor(), 'houseCurve');
assert.equal(state.measurement.convolverAssistant.targetCurve, 'harman');
assert.equal(state.measurement.customHouseCurve.points[0].id, preservedInitialPointId, 'mode switch preserves the existing draft');
context.handleMeasurementTargetCurveSelection('create-custom-house-curve');
assert.equal(context.getMeasurementActiveEditor(), 'houseCurve');

// 5. P values are the only House-Curve preview data and remain log-sortable.
state.measurement.customHouseCurve.points = [
    { id: 'p1', freqHz: 1000, gainDb: 4 },
    { id: 'p2', freqHz: 100, gainDb: -2 },
];
assert.deepEqual(JSON.parse(JSON.stringify(context.getMeasurementHouseCurvePreviewPoints())), [[100, -2], [1000, 4]]);
assert.equal(state.measurement.peqAssistant.filters.length, 1, 'PEQ data remains intact');

// Source-level guards for rendering and independent correction methods.
assert.match(source, /activeEditor: 'none'/);
assert.match(source, /if \(getMeasurementActiveEditor\(\) !== 'peq'\) return;/, 'PEQ overlay is isolated');
assert.match(source, /if \(getMeasurementActiveEditor\(\) === 'houseCurve'\) return;/, 'House Curve blocks PEQ graph interaction');
assert.match(source, /getMeasurementTargetCurvePreview/);
assert.match(source, /getMeasurementConvolverCurveDbFromPoints\(points, frequency\)/, 'logarithmic target interpolation remains in use');
console.log('ok HCE-001 state sequence: none/peq/houseCurve, sentinel action, isolated live target preview');
