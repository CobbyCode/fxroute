#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const dspSource = fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_dsp.js'), 'utf8');

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
        if (`'\"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const dsp = require(path.join(__dirname, '..', 'static', 'measurement_dsp.js'));
const state = {
    measurement: {
        activeEditor: 'none',
        houseCurveOptions: [],
        customHouseCurve: { open: false, displayTarget: 'actual', points: [], activePointId: null, dragPointId: null, name: '', nameTouched: false, saving: false },
        peqAssistant: { filters: [], enabled: false, activeFilterId: null, dragFilterId: null },
        convolverAssistant: { targetCurve: 'neutral', dragMode: null },
    },
};
const pointerState = { x: 0, y: 0, bounds: { left: 50, top: 20, width: 1000, height: 400 }, range: { minDb: -18, maxDb: 18 } };
const graph = {
    setPointerCapture: (pointerId) => { graph.captured = pointerId; },
    releasePointerCapture: (pointerId) => { graph.released = pointerId; },
    captured: null,
    released: null,
};
const context = {
    state,
    measurementPeqPalette: ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa'],
    measurementFrequencyToX: dsp.measurementFrequencyToX,
    measurementXToFrequency: dsp.measurementXToFrequency,
    measurementDbToY: dsp.measurementDbToY,
    measurementYToDb: dsp.measurementYToDb,
    renderMeasurementPanel: () => {},
    scheduleMeasurementGraphRender: () => {},
    ensureMeasurementPeqState: () => state.measurement.peqAssistant,
    ensureMeasurementConvolverState: () => state.measurement.convolverAssistant,
    getCustomHouseCurveNameSuggestion: () => 'Custom House Curve 1',
    getMeasurementGraphView: () => 'freq',
    getMeasurementFrequencyHoverTooltip: () => '',
    getMeasurementGraphPointerPosition: () => pointerState,
    elements: { measurementGraph: graph },
    MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX: 13,
    MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX: 24,
    updateMeasurementConvolverField: (field, value) => { if (field === 'targetCurve') state.measurement.convolverAssistant.targetCurve = value; },
};
vm.createContext(context);
vm.runInContext([
    'ensureCustomHouseCurveState', 'getMeasurementActiveEditor', 'setMeasurementActiveEditor',
    'openCustomHouseCurveEditor', 'handleMeasurementTargetCurveSelection',
    'addCustomHouseCurvePoint', 'addCustomHouseCurvePointAtPosition',
    'updateCustomHouseCurvePoint', 'deleteCustomHouseCurvePoint', 'resetCustomHouseCurveDraft', 'resetMeasurementGraph',
    'getCustomHouseCurvePointSlot', 'getCustomHouseCurvePointColor',
    'getMeasurementHouseCurvePreviewPoints', 'getMeasurementTargetCurvePreview',
    'getCustomHouseCurveHandlePosition', 'getCustomHouseCurveHandleHitRadius',
    'findCustomHouseCurveHandleAtPosition', 'handleMeasurementGraphPointerDown',
    'handleMeasurementGraphPointerMove', 'handleMeasurementGraphPointerUp',
].map(extractFunction).join('\n'), context);

const bounds = pointerState.bounds;
const range = pointerState.range;
context.measurementGraphPointerId = null;

// Target selection sequence: actual target -> editing sentinel -> actual target -> editing again.
context.handleMeasurementTargetCurveSelection('neutral');
assert.equal(state.measurement.convolverAssistant.targetCurve, 'neutral');
context.handleMeasurementTargetCurveSelection('create-custom-house-curve');
assert.equal(state.measurement.activeEditor, 'houseCurve');
assert.equal(state.measurement.customHouseCurve.displayTarget, 'editing-custom-house-curve');
assert.equal(context.getMeasurementTargetCurvePreview().shortLabel, 'Editing Custom House Curve…');
context.handleMeasurementTargetCurveSelection('neutral');
assert.equal(state.measurement.activeEditor, 'none');
assert.equal(state.measurement.customHouseCurve.displayTarget, 'actual');
context.handleMeasurementTargetCurveSelection('create-custom-house-curve');
assert.equal(state.measurement.activeEditor, 'houseCurve');

// A new editor starts with exactly the neutral P1 point and a flat 0 dB target.
assert.equal(state.measurement.customHouseCurve.points.length, 1);
const initialPoint = state.measurement.customHouseCurve.points[0];
assert.equal(initialPoint.slot, 0);
assert.equal(initialPoint.freqHz, 20);
assert.equal(initialPoint.gainDb, 0);
assert.deepEqual([20, 1000, 20000].map((frequency) => dsp.getMeasurementConvolverCurveDbFromPoints(context.getMeasurementHouseCurvePreviewPoints(), frequency)), [0, 0, 0]);

// Reset the draft before interaction testing; reset keeps the editor open.
context.resetCustomHouseCurveDraft();
assert.equal(state.measurement.activeEditor, 'houseCurve');
assert.equal(state.measurement.customHouseCurve.points.length, 1);
assert.equal(context.getCustomHouseCurvePointSlot(state.measurement.customHouseCurve.points[0].id), 0);
assert.equal(context.getCustomHouseCurvePointColor(state.measurement.customHouseCurve.points[0]), context.getCustomHouseCurvePointColor(state.measurement.customHouseCurve.points[0], 0));

// The visible Reset button uses the same path and keeps the editor open.
context.resetMeasurementGraph();
assert.equal(state.measurement.activeEditor, 'houseCurve');
assert.deepEqual(JSON.parse(JSON.stringify(state.measurement.customHouseCurve.points.map((point) => ({ slot: point.slot, freqHz: point.freqHz, gainDb: point.gainDb }))),), [{ slot: 0, freqHz: 20, gainDb: 0 }]);

// A free graph click uses the next free slot, maps log-X/Y, selects it, and is bounded.
const clickPoint = context.addCustomHouseCurvePointAtPosition({ x: bounds.left + bounds.width * 0.5, y: bounds.top + bounds.height * 0.25, bounds, range });
assert.ok(clickPoint);
assert.equal(clickPoint.slot, 1);
assert.equal(state.measurement.customHouseCurve.points.length, 2);
assert.equal(state.measurement.customHouseCurve.activePointId, clickPoint.id);
assert.ok(Math.abs(clickPoint.freqHz - 632) <= 1, `logarithmic frequency mapping: ${clickPoint.freqHz}`);
assert.equal(clickPoint.gainDb, 9);
const second = context.addCustomHouseCurvePointAtPosition({ x: bounds.left, y: bounds.top + bounds.height, bounds, range });
assert.equal(second.slot, 2);
assert.equal(second.freqHz, 20);
assert.equal(second.gainDb, -18);
assert.equal(context.getCustomHouseCurvePointSlot(clickPoint.id), 1);
assert.notEqual(context.getCustomHouseCurvePointColor(clickPoint, 1), context.getCustomHouseCurvePointColor(second, 2));

// Existing point updates both axes with the same PEQ-style frequency step and gain precision.
const oldFrequency = clickPoint.freqHz;
context.updateCustomHouseCurvePoint(clickPoint.id, { freqHz: oldFrequency + 12.7, gainDb: 4.36 });
assert.equal(clickPoint.freqHz, Math.round(oldFrequency + 12.7));
assert.equal(clickPoint.gainDb, 4.4);
assert.notEqual(clickPoint.freqHz, oldFrequency);

// Delete then refill the freed slot; eight points remain the hard limit.
for (let index = state.measurement.customHouseCurve.points.length; index < 8; index += 1) assert.ok(context.addCustomHouseCurvePoint({ freqHz: 100 + index, gainDb: 0 }));
assert.equal(context.addCustomHouseCurvePoint({ freqHz: 1000, gainDb: 0 }), null);
context.deleteCustomHouseCurvePoint(second.id);
assert.equal(state.measurement.customHouseCurve.points.length, 7);
assert.ok(context.addCustomHouseCurvePointAtPosition({ x: bounds.left + 10, y: bounds.top + 10, bounds, range }));
assert.equal(state.measurement.customHouseCurve.points.length, 8);

const sorted = JSON.parse(JSON.stringify(context.getMeasurementHouseCurvePreviewPoints()));
assert.deepEqual(sorted, sorted.slice().sort((a, b) => a[0] - b[0]));

// Exercise the actual pointer handlers: a mouse click creates exactly one point;
// dragging that existing handle changes X and Y without creating another point.
context.resetCustomHouseCurveDraft();
pointerState.x = bounds.left + bounds.width * 0.25;
pointerState.y = bounds.top + bounds.height * 0.75;
context.handleMeasurementGraphPointerDown({ pointerId: 11, pointerType: 'mouse', preventDefault() {} });
assert.equal(state.measurement.customHouseCurve.points.length, 2);
const pointerPoint = state.measurement.customHouseCurve.points.find((point) => point.slot === 1);
assert.ok(pointerPoint);
const beforeDrag = { freqHz: pointerPoint.freqHz, gainDb: pointerPoint.gainDb };
pointerState.x = bounds.left + bounds.width * 0.75;
pointerState.y = bounds.top + bounds.height * 0.25;
context.handleMeasurementGraphPointerMove({ pointerId: 11, pointerType: 'mouse', preventDefault() {} });
assert.notEqual(pointerPoint.freqHz, beforeDrag.freqHz);
assert.notEqual(pointerPoint.gainDb, beforeDrag.gainDb);
assert.equal(state.measurement.customHouseCurve.points.length, 2, 'dragging an existing handle must not create a third point');
context.handleMeasurementGraphPointerUp({ pointerId: 11, pointerType: 'mouse', preventDefault() {} });

// Touch uses the same handler path and touch-sized hit radius.
const touchHandle = context.getCustomHouseCurveHandlePosition(pointerPoint, bounds, range);
pointerState.x = touchHandle.x;
pointerState.y = touchHandle.y;
context.handleMeasurementGraphPointerDown({ pointerId: 12, pointerType: 'touch', preventDefault() {} });
assert.equal(state.measurement.customHouseCurve.points.length, 2, 'touch on an existing handle must not create a point');
pointerState.x = bounds.left + bounds.width * 0.1;
pointerState.y = bounds.top + bounds.height * 0.9;
context.handleMeasurementGraphPointerMove({ pointerId: 12, pointerType: 'touch', preventDefault() {} });
context.handleMeasurementGraphPointerUp({ pointerId: 12, pointerType: 'touch', preventDefault() {} });
assert.ok(pointerPoint.freqHz >= 20 && pointerPoint.freqHz <= 20000);
assert.ok(pointerPoint.gainDb >= range.minDb && pointerPoint.gainDb <= range.maxDb);

assert.match(source, /const point = hitPoint \|\| addCustomHouseCurvePointAtPosition/);
assert.match(source, /measurementXToFrequency\(pointer\.x, pointer\.bounds\)/);
assert.match(source, /Editing Custom House Curve…/);
assert.match(source, /if \(getMeasurementActiveEditor\(\) !== 'peq'\) return;/);
assert.match(dspSource, /const minLog = Math\.log10\(20\)/);
console.log('ok custom house curve interaction: mouse/touch-compatible free create, bounded log-X/Y drag state, sentinel target display, delete/refill and eight-slot guard');
