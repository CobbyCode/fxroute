#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Reset discards unsaved captures without changing Saved Runs or the other assistant.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const MeasurementDsp = require('../static/measurement_dsp.js');
const MeasurementUI = require('../static/measurement_ui.js');
const convolverEditor = require('../static/measurement_convolver_editor.js');
const measurementJob = require('../static/measurement_job.js');

const appSource = fs.readFileSync(require.resolve('../static/app.js'), 'utf8');
const start = appSource.indexOf('function resetMeasurementGraph() {');
const end = appSource.indexOf('\n\nfunction waitForNextAnimationFrame()', start);
assert.ok(start >= 0 && end > start);
const resetSource = appSource.slice(start, end);
const readersSource = appSource.slice(appSource.indexOf('function getVisibleMeasurementEntries() {'),
    appSource.indexOf('function getAutoSubDisplayReferenceEntries() {'));
const graphSource = fs.readFileSync(require.resolve('../static/measurement_graph.js'), 'utf8');
const savedActionsSource = fs.readFileSync(require.resolve('../static/measurement_saved_actions.js'), 'utf8');

function capture(id) {
    return { id, name: id, traces: [{ kind: 'sweep-response', points: [[20, 0], [1000, 1]] }] };
}

function runReset(assistMode, { filters = [], targetCurve = 'neutral', convolver = {}, activeEditor = 'none', graphView = 'freq', measurement = {} } = {}) {
    const saved = [capture('saved')];
    const visibility = { saved: true };
    const reviewVisibility = { saved: true };
    const peq = { filters: filters.slice(), enabled: true, activeFilterId: filters[0]?.id || null, dragFilterId: 'drag', draft: { leftBands: [{ id: 'staged' }] } };
    const conv = { ...MeasurementUI.getDefaultMeasurementConvolverState(), targetCurve, ...convolver, draft: { left: { id: 'staged' }, right: null } };
    const state = { measurement: { assistMode, measurementView: graphView, currentMeasurement: null,
        pendingRepeatMeasurements: [], autoSubMeasurements: [], currentMeasurementSaved: false,
        currentMeasurementName: '', measurements: saved, visibilityById: visibility,
        reviewVisibilityById: reviewVisibility, savedGroupOpen: true,
        activeEditor, peqAssistant: peq, convolverAssistant: conv, ...measurement } };
    convolverEditor.init({ getState: () => state });
    measurementJob.init({ getState: () => state });
    const effects = [];
    const context = { state, MeasurementUI, window: {
        FXRouteMeasurementUI: MeasurementUI,
        FXRouteMeasurementDsp: MeasurementDsp,
        FXRouteMeasurementJob: measurementJob,
        FXRouteMeasurementPeqEditor: { ensureMeasurementPeqState: () => peq },
        FXRouteMeasurementConvolverEditor: convolverEditor,
        FXRouteMeasurementCalibration: { resetCustomHouseCurveDraft: () => effects.push('draft') },
    }, getMeasurementGraphView: () => graphView,
    getMeasurementActiveEditor: () => state.measurement.activeEditor,
    setMeasurementActiveEditor: (value) => { state.measurement.activeEditor = value; effects.push('editor'); },
    renderMeasurementPanel: () => effects.push('render'),
    MeasurementGraph: { scheduleMeasurementGraphRender: () => effects.push('graph') } };
    vm.createContext(context);
    vm.runInContext(readersSource + resetSource + graphSource + savedActionsSource, context);
    const graph = context.window.FXRouteMeasurementGraph;
    graph.init({
        getCurrentMeasurementEntries: () => context.getCurrentMeasurementEntries(),
        getVisibleMeasurementEntries: () => context.getVisibleMeasurementEntries(),
        getVisibleMeasurementColorById: () => ({ saved: '#60a5fa' }),
        getMeasurementDisplayTraces: (entry) => context.getMeasurementDisplayTraces(entry),
    });
    const graphBefore = graph.getGraphMeasurementEntries();
    context.resetMeasurementGraph();
    return { state, peq, conv, saved, visibility, reviewVisibility, effects, graphBefore,
        graphAfter: graph.getGraphMeasurementEntries(), reset: context.resetMeasurementGraph,
        savedActions: context.FXRouteMeasurementSavedActions };
}

function assertDiscarded(result) {
    assert.equal(result.state.measurement.currentMeasurement, null, 'current sweep is discarded');
    assert.equal(result.state.measurement.pendingRepeatMeasurements.length, 0, 'pending repeat captures are discarded');
    assert.equal(result.state.measurement.autoSubMeasurements.length, 0, 'pending AutoSub captures are discarded');
    assert.equal(result.state.measurement.currentMeasurementSaved, false);
    assert.equal(result.state.measurement.currentMeasurementName, '');
    assert.equal(result.graphAfter.some((entry) => entry.current), false, 'Current Measurement line disappears');
    assert.deepEqual(Array.from(result.graphAfter, (entry) => entry.id), ['saved'], 'saved trace remains visible');
    assert.equal(result.state.measurement.measurements, result.saved, 'Saved Runs are untouched');
    assert.equal(result.state.measurement.visibilityById, result.visibility);
    assert.deepEqual(result.visibility, { saved: true });
    assert.equal(result.state.measurement.reviewVisibilityById, result.reviewVisibility);
    assert.deepEqual(result.reviewVisibility, { saved: true });
    assert.equal(result.state.measurement.savedGroupOpen, true);
    assert.deepEqual(result.effects.slice(-2), ['render', 'graph']);
}

const noOp = runReset('peq');
assert.deepEqual(noOp.effects, [], 'neutral Reset does nothing');
const ir = runReset('peq', { filters: [{ id: 'f1' }], graphView: 'ir' });
assert.equal(ir.peq.filters.length, 1, 'IR never resets PEQ');
assert.deepEqual(ir.effects, []);

for (const assistMode of ['peq', 'convolver']) {
    for (const [label, measurement] of [
        ['single sweep', { currentMeasurement: capture('unsaved') }],
        ['LR Repeat', { currentMeasurement: capture('repeat-left'), pendingRepeatMeasurements: [capture('repeat-left'), capture('repeat-right')] }],
        ['AutoSub', { autoSubMeasurements: [capture('auto-sub')] }],
    ]) {
        const result = runReset(assistMode, { measurement: { currentMeasurementName: 'Unsaved', ...measurement } });
        assert.ok(result.graphBefore.some((entry) => entry.current), `${label} starts with a current trace`);
        assertDiscarded(result);
    }
}

for (const lock of [{ startInFlight: true }, { activeJobId: 'job-1' }, { saveInFlight: true },
    { autoSubInFlight: true, autoSubJobId: 'recovered-job' }, { speakerAlignInFlight: true }, { hybridWizard: { running: true } }]) {
    const current = capture('unsaved');
    const result = runReset('peq', { measurement: { currentMeasurement: current, ...lock } });
    assert.equal(result.state.measurement.currentMeasurement, current, 'active captures lock Reset');
    assert.deepEqual(result.effects, []);
}
const irCurrent = capture('unsaved');
const irCapture = runReset('peq', { graphView: 'ir', measurement: { currentMeasurement: irCurrent } });
assert.equal(irCapture.state.measurement.currentMeasurement, irCurrent, 'IR does not discard a capture');
assert.deepEqual(irCapture.effects, []);

const savedCurrent = capture('saved');
const savedOnly = runReset('peq', { measurement: { currentMeasurement: savedCurrent, currentMeasurementSaved: true } });
assert.equal(savedOnly.state.measurement.currentMeasurement, savedCurrent, 'a saved capture is not discarded');
assert.deepEqual(savedOnly.effects, []);

const peqReset = runReset('peq', { filters: [{ id: 'f1' }], targetCurve: 'harman', convolver: { maxCutDb: -12 },
    measurement: { currentMeasurement: capture('unsaved'), pendingRepeatMeasurements: [capture('repeat')],
        autoSubMeasurements: [capture('auto-sub')], currentMeasurementName: 'Unsaved' } });
assert.equal(peqReset.peq.filters.length, 0);
assert.equal(peqReset.peq.activeFilterId, null);
assert.equal(peqReset.conv.targetCurve, 'neutral');
assert.equal(peqReset.conv.maxCutDb, -12, 'PEQ reset preserves convolver settings');
assert.equal(peqReset.conv.draft.left, null, 'changing the shared target invalidates a staged FIR');
assert.equal(peqReset.peq.draft.leftBands.length, 1, 'staged PEQ draft is preserved');
assert.equal(peqReset.state.measurement.activeEditor, 'peq', 'PEQ assistant stays open');
assertDiscarded(peqReset);

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
    filters: [{ id: 'f1' }], measurement: { currentMeasurement: capture('unsaved'), currentMeasurementName: 'Unsaved' } });
assert.equal(convReset.conv.targetCurve, 'neutral');
assert.equal(convReset.conv.rangeStartHz, 20);
assert.equal(convReset.conv.maxCutDb, -9);
assert.equal(convReset.conv.safetyMarginDb, 1);
assert.equal(convReset.conv.autoGainEnabled, true);
assert.equal(convReset.conv.phaseMode, 'minimum');
assert.equal(convReset.conv.irLength, '8192');
assert.equal(convReset.peq.filters.length, 1, 'Convolver reset preserves PEQs');
assertDiscarded(convReset);

async function testResetDuringSave() {
    const current = capture('unsaved');
    const result = runReset('peq', { measurement: { currentMeasurement: current, currentMeasurementName: 'Unsaved', saveInFlight: true } });
    result.state.measurement.saveInFlight = false;
    let finishSave;
    const response = new Promise((resolve) => { finishSave = resolve; });
    result.savedActions.init({ getState: () => result.state, fetch: () => response });
    const save = result.savedActions.saveCurrentMeasurement();
    assert.equal(result.state.measurement.saveInFlight, true, 'save is pending');
    result.reset();
    finishSave({ ok: false, json: async () => ({ detail: 'Save failed' }) });
    await save;
    assert.equal(result.state.measurement.currentMeasurement, current, 'failed save retains the sweep for retry');
    assert.equal(result.state.measurement.currentMeasurementName, 'Unsaved');
    assert.equal(result.state.measurement.saveInFlight, false);
}

testResetDuringSave().then(() => console.log('measurement reset: ok')).catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
