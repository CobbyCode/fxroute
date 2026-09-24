#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Panel sections keep file selection, action locks and graph status in sync.

const assert = require('node:assert/strict');
const panel = require('../static/measurement_panel_ui.js');
const { escapeHtml } = require('../static/ui_helpers.js');

function element() {
    const classes = new Set();
    return {
        innerHTML: '', value: '', textContent: '', disabled: false, title: '',
        classList: {
            toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
            remove: (name) => classes.delete(name),
            contains: (name) => classes.has(name),
        },
    };
}

function selectElement() {
    const select = element();
    let html = '';
    Object.defineProperty(select, 'innerHTML', {
        get: () => html,
        set(value) {
            html = value;
            select.value = value.match(/<option value="([^"]*)" selected>/)?.[1]
                ?? value.match(/<option value="([^"]*)"/)?.[1] ?? '';
        },
    });
    return select;
}

const names = [
    'measurementCalibrationSelect', 'measurementCalibrationDeleteBtn', 'measurementCalibrationExportBtn',
    'measurementCalibrationUploadName', 'measurementCalibrationName', 'measurementHouseCurveSelect',
    'measurementHouseCurveDeleteBtn', 'measurementHouseCurveExportBtn', 'measurementHouseCurveUploadName',
    'measurementHouseCurveName', 'measurementNameInput', 'measurementRepeatStartBtn', 'measurementSaveBtn',
    'measurementAssistMode', 'measurementTargetCurve', 'measurementClearBtn', 'measurementSetupStatus',
    'measurementSummary', 'measurementGraphSubtitle', 'measurementEmpty', 'measurementGraphControls',
    'measurementGraph',
];
const elements = Object.fromEntries(names.map((name) => [name, element()]));
elements.measurementCalibrationSelect = selectElement();
elements.measurementHouseCurveSelect = selectElement();
const calls = [];
let activeKind = '';
let activeJob = false;
let repeatReason = '';
let editorTarget = 'actual';
panel.init({
    getElements: () => elements,
    escapeHtml,
    measurementAreaBadge: () => ({ title: 'Left mid · measured area' }),
    syncMeasurementSweepButton: () => calls.push('sweep'),
    getActiveMeasurementKind: () => activeKind,
    hasActiveMeasurementJob: () => activeJob,
    measurementRepeatBlockedReason: () => repeatReason,
    syncMeasurementRepeatNote: (running, blocked) => calls.push(['repeat', running, blocked]),
    measurementModeReady: () => true,
    ensureCustomHouseCurveState: () => ({ displayTarget: editorTarget }),
    getMeasurementConvolverCurveOptions: () => [
        { key: 'neutral', label: 'Neutral' }, { key: 'house:curve-1', label: 'Room & <curve>' },
    ],
    getDefaultMeasurementConvolverState: () => ({
        targetCurve: 'neutral', rangeStartHz: 20, rangeEndHz: 250,
        maxBoostDb: 6, maxCutDb: -9, dipGuard: 'off', quality: 'minimum_8192',
    }),
    measurementSetupStatusText: () => 'Host capture ready',
    buildMeasurementIrDiagnostics: (entries, frequencyView) => {
        calls.push(['diagnostics', entries.length, frequencyView]);
        return [{ id: 'ir' }];
    },
    buildMeasurementIrSummary: () => 'IR aligned',
    buildMeasurementIrDiagnosticsTooltip: () => 'IR timing detail',
    renderMeasurementIrDiagnostics: (entries, frequencyView) => calls.push(['ir-render', entries.length, frequencyView]),
});

const measurementState = {
    startInFlight: false, saveInFlight: false, inputsLoading: false,
    calibrationOptions: [{ id: 'cal-1', filename: 'Mic & <cal>.txt' }],
    selectedCalibrationRef: 'cal-1',
    houseCurveOptions: [{ id: 'curve-1', filename: 'Room & <curve>.txt' }],
    currentMeasurementName: 'Sweep 1',
};
const conv = {
    targetCurve: 'house:curve-1', rangeStartHz: 30, rangeEndHz: 500,
    maxBoostDb: 6, maxCutDb: -9, dipGuard: 'off', quality: 'minimum_8192',
};
const peq = { filters: [{ id: 'f1' }] };
const graphEntries = [{ id: 'measurement-1' }];
const context = { measurementState, conv, peq, graphEntries, current: { id: 'measurement-1' },
    assistMode: 'peq', activeEditor: 'peq', graphView: 'freq', frequencyView: true };

panel.renderMeasurementPanelCalibrationSection(context);
assert.match(elements.measurementCalibrationSelect.innerHTML, /Mic &amp; &lt;cal&gt;\.txt/);
assert.equal(elements.measurementCalibrationSelect.value, 'cal-1');
assert.equal(elements.measurementCalibrationDeleteBtn.disabled, false);
assert.equal(elements.measurementCalibrationExportBtn.disabled, false);
assert.equal(elements.measurementCalibrationName.textContent, 'Mic & <cal>.txt');

panel.renderMeasurementPanelHouseCurveSection(context);
assert.match(elements.measurementHouseCurveSelect.innerHTML, /Room &amp; &lt;curve&gt;\.txt/);
assert.equal(elements.measurementHouseCurveSelect.value, 'curve-1');
assert.equal(elements.measurementHouseCurveDeleteBtn.disabled, false);
assert.equal(elements.measurementHouseCurveExportBtn.disabled, false);
assert.equal(elements.measurementHouseCurveName.textContent, 'Room & <curve>.txt');

panel.renderMeasurementPanelActionsSection(context);
assert.deepEqual(calls.slice(0, 2), ['sweep', ['repeat', false, '']]);
assert.equal(elements.measurementNameInput.value, 'Sweep 1');
assert.equal(elements.measurementNameInput.title, 'Measured area: Left mid · measured area');
assert.equal(elements.measurementRepeatStartBtn.textContent, 'Start LR Repeat');
assert.equal(elements.measurementSaveBtn.disabled, false);

panel.renderMeasurementPanelViewSection(context);
assert.equal(elements.measurementTargetCurve.value, 'house:curve-1');
assert.match(elements.measurementTargetCurve.innerHTML, /Room &amp; &lt;curve&gt;/);
assert.equal(elements.measurementClearBtn.disabled, false);
panel.renderMeasurementPanelStatusSection(context);
assert.equal(elements.measurementSetupStatus.textContent, 'Host capture ready');
assert.equal(elements.measurementSummary.textContent, '1/12 assistant filters');
assert.equal(elements.measurementGraphControls.textContent, 'Tap/click near 0 dB to add a filter, drag handles for freq/gain.');
assert.equal(elements.measurementGraph.title, '');
assert.deepEqual(calls.slice(-2), [['diagnostics', 1, true], ['ir-render', 1, true]]);

measurementState.startInFlight = true;
measurementState.activeJobId = 'job-1';
measurementState.calibrationDeleting = true;
measurementState.houseCurveExporting = true;
measurementState.calibrationFilename = 'Uploading cal.txt';
measurementState.houseCurveFilename = 'Uploading curve.txt';
activeKind = 'lr_repeat';
activeJob = true;
repeatReason = 'One-sided bank';
editorTarget = 'editing-custom-house-curve';
const irContext = { ...context, assistMode: 'convolver', activeEditor: 'houseCurve', graphView: 'ir', frequencyView: false };
panel.renderMeasurementPanelCalibrationSection(irContext);
panel.renderMeasurementPanelHouseCurveSection(irContext);
panel.renderMeasurementPanelActionsSection(irContext);
panel.renderMeasurementPanelViewSection(irContext);
panel.renderMeasurementPanelStatusSection(irContext);
assert.equal(elements.measurementCalibrationDeleteBtn.disabled, true);
assert.equal(elements.measurementCalibrationDeleteBtn.textContent, 'Deleting…');
assert.equal(elements.measurementCalibrationExportBtn.disabled, true);
assert.equal(elements.measurementCalibrationName.textContent, 'Uploading cal.txt');
assert.equal(elements.measurementHouseCurveDeleteBtn.disabled, true);
assert.equal(elements.measurementHouseCurveExportBtn.textContent, 'Exporting…');
assert.equal(elements.measurementHouseCurveName.textContent, 'Uploading curve.txt');
assert.equal(elements.measurementRepeatStartBtn.textContent, 'Cancel measurement');
assert.equal(elements.measurementRepeatStartBtn.disabled, true);
assert.deepEqual(calls.filter((item) => Array.isArray(item) && item[0] === 'repeat').at(-1), ['repeat', true, 'One-sided bank']);
assert.equal(elements.measurementSaveBtn.disabled, true);
assert.equal(elements.measurementTargetCurve.value, 'editing-custom-house-curve');
assert.equal(elements.measurementClearBtn.disabled, true);
assert.equal(elements.measurementSummary.textContent, 'IR -2–30 ms');
assert.equal(elements.measurementGraphControls.textContent, 'IR aligned');
assert.equal(elements.measurementGraphControls.title, 'IR timing detail');
assert.deepEqual(calls.slice(-2), [['diagnostics', 1, false], ['ir-render', 1, false]]);

console.log('measurement panel file/action/view/status sections: ok');
