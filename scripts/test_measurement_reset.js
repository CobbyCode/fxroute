#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Reset only changes the active assistant's resettable settings and shared target.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
require('../static/measurement_dsp.js');
const MeasurementUI = require('../static/measurement_ui.js');
const convolverEditor = require('../static/measurement_convolver_editor.js');

const appSource = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
const start = appSource.indexOf('function resetMeasurementGraph() {');
const end = appSource.indexOf('\n\nfunction waitForNextAnimationFrame()', start);
assert.ok(start >= 0 && end > start);
const resetSource = appSource.slice(start, end);

function runReset(assistMode, { filters = [], targetCurve = 'neutral', convolver = {}, activeEditor = 'none', graphView = 'freq' } = {}) {
    const current = { id: 'unsaved' };
    const repeat = [{ id: 'repeat' }];
    const autoSub = [{ id: 'auto-sub' }];
    const peq = { filters: filters.slice(), enabled: true, activeFilterId: filters[0]?.id || null, dragFilterId: 'drag', draft: { leftBands: [{ id: 'staged' }] } };
    const conv = { ...MeasurementUI.getDefaultMeasurementConvolverState(), targetCurve, ...convolver, draft: { left: { id: 'staged' }, right: null } };
    const state = { measurement: { assistMode, measurementView: graphView, currentMeasurement: current,
        pendingRepeatMeasurements: repeat, autoSubMeasurements: autoSub, currentMeasurementSaved: false,
        currentMeasurementName: 'Unsaved', activeEditor, peqAssistant: peq, convolverAssistant: conv } };
    convolverEditor.init({ getState: () => state });
    const effects = [];
    const context = { state, MeasurementUI, window: {
        FXRouteMeasurementPeqEditor: { ensureMeasurementPeqState: () => peq },
        FXRouteMeasurementConvolverEditor: convolverEditor,
        FXRouteMeasurementCalibration: { resetCustomHouseCurveDraft: () => effects.push('draft') },
    }, getMeasurementGraphView: () => graphView,
    getMeasurementActiveEditor: () => state.measurement.activeEditor,
    setMeasurementActiveEditor: (value) => { state.measurement.activeEditor = value; effects.push('editor'); },
    renderMeasurementPanel: () => effects.push('render'),
    MeasurementGraph: { scheduleMeasurementGraphRender: () => effects.push('graph') } };
    vm.createContext(context);
    vm.runInContext(resetSource, context);
    context.resetMeasurementGraph();
    return { state, peq, conv, current, repeat, autoSub, effects };
}

const noOp = runReset('peq');
assert.deepEqual(noOp.effects, [], 'neutral Reset does nothing');
const ir = runReset('peq', { filters: [{ id: 'f1' }], graphView: 'ir' });
assert.equal(ir.peq.filters.length, 1, 'IR never resets PEQ');
assert.deepEqual(ir.effects, []);

const peqReset = runReset('peq', { filters: [{ id: 'f1' }], targetCurve: 'harman', convolver: { maxCutDb: -12 } });
assert.equal(peqReset.peq.filters.length, 0);
assert.equal(peqReset.peq.activeFilterId, null);
assert.equal(peqReset.conv.targetCurve, 'neutral');
assert.equal(peqReset.conv.maxCutDb, -12, 'PEQ reset preserves convolver settings');
assert.equal(peqReset.conv.draft.left, null, 'changing the shared target invalidates a staged FIR');
assert.equal(peqReset.peq.draft.leftBands.length, 1, 'staged PEQ draft is preserved');
assert.equal(peqReset.state.measurement.activeEditor, 'peq', 'PEQ assistant stays open');
assert.equal(peqReset.state.measurement.currentMeasurement, peqReset.current, 'measurement is preserved');
assert.equal(peqReset.state.measurement.pendingRepeatMeasurements, peqReset.repeat);
assert.equal(peqReset.state.measurement.autoSubMeasurements, peqReset.autoSub);
assert.equal(peqReset.state.measurement.currentMeasurementName, 'Unsaved');
assert.deepEqual(peqReset.effects.slice(-2), ['render', 'graph']);

const curveOnly = runReset('peq', { targetCurve: 'harman' });
assert.equal(curveOnly.conv.targetCurve, 'neutral', 'house curve alone is reset in PEQ');

const filtersOnly = runReset('peq', { filters: [{ id: 'f1' }] });
assert.ok(filtersOnly.conv.draft.left, 'unchanged convolver target leaves a staged FIR intact');

const unrelatedSettings = runReset('peq', { convolver: { maxCutDb: -12 } });
assert.deepEqual(unrelatedSettings.effects, [], 'PEQ Reset ignores convolver-only changes');
const unrelatedFilters = runReset('convolver', { filters: [{ id: 'f1' }] });
assert.deepEqual(unrelatedFilters.effects, [], 'Convolver Reset ignores PEQ-only changes');

const convReset = runReset('convolver', { targetCurve: 'harman', convolver: { rangeStartHz: 30, maxCutDb: -12,
    safetyMarginDb: 2, autoGainEnabled: false, phaseMode: 'linear', irLength: '4096', quality: 'linear_4096' },
    filters: [{ id: 'f1' }] });
assert.equal(convReset.conv.targetCurve, 'neutral');
assert.equal(convReset.conv.rangeStartHz, 20);
assert.equal(convReset.conv.maxCutDb, -9);
assert.equal(convReset.conv.safetyMarginDb, 1);
assert.equal(convReset.conv.autoGainEnabled, true);
assert.equal(convReset.conv.phaseMode, 'minimum');
assert.equal(convReset.conv.irLength, '8192');
assert.equal(convReset.peq.filters.length, 1, 'Convolver reset preserves PEQs');
assert.equal(convReset.state.measurement.currentMeasurement, convReset.current);
assert.deepEqual(convReset.effects.slice(-2), ['render', 'graph']);

console.log('measurement reset: ok');
