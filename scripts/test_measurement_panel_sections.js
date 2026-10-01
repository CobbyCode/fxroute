#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Panel sections keep file selection, action locks and graph status in sync.

const assert = require('node:assert/strict');
const panel = require('../static/measurement_panel_ui.js');
const measurementUI = require('../static/measurement_ui.js');
const measurementJob = require('../static/measurement_job.js');
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
    hasActiveMeasurementJob: () => activeJob || measurementJob.hasActiveMeasurementJob(),
    measurementRepeatBlockedReason: () => repeatReason,
    syncMeasurementRepeatNote: (running, blocked) => calls.push(['repeat', running, blocked]),
    measurementModeReady: () => true,
    ensureCustomHouseCurveState: () => ({ displayTarget: editorTarget }),
    getMeasurementConvolverCurveOptions: () => [
        { key: 'neutral', label: 'Neutral' }, { key: 'house:curve-1', label: 'Room & <curve>' },
    ],
    getDefaultMeasurementConvolverState: measurementUI.getDefaultMeasurementConvolverState,
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
const conv = { ...measurementUI.getDefaultMeasurementConvolverState(), targetCurve: 'house:curve-1', rangeStartHz: 30, rangeEndHz: 500 };
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
// IR never offers Reset, even when the frequency editors have pending changes.
const irResetContext = { ...context, graphView: 'ir', frequencyView: false };
panel.renderMeasurementPanelViewSection(irResetContext);
assert.equal(elements.measurementClearBtn.disabled, true);
assert.equal(elements.measurementClearBtn.title, 'Only available in frequency view.');
const savedTake = { id: 'take-before', speaker_align_take: { side: 'right', take: 'before' } };
const savedOnlyContext = { ...context, current: null, peq: { filters: [] },
    conv: measurementUI.getDefaultMeasurementConvolverState(),
    measurements: [savedTake], measurementState: { ...measurementState, visibilityById: { 'take-before': true } } };
panel.renderMeasurementPanelViewSection(savedOnlyContext);
assert.equal(elements.measurementClearBtn.disabled, true);
for (const assistMode of ['peq', 'convolver']) {
    for (const [label, pending] of [
        ['single sweep', { currentMeasurement: { id: 'unsaved' } }],
        ['LR Repeat', { pendingRepeatMeasurements: [{ id: 'repeat-left' }, { id: 'repeat-right' }] }],
        ['AutoSub', { autoSubMeasurements: [{ id: 'auto-sub' }] }],
    ]) {
        const unsavedContext = { ...savedOnlyContext, assistMode,
            measurementState: { ...measurementState, currentMeasurementSaved: false, ...pending } };
        panel.renderMeasurementPanelViewSection(unsavedContext);
        assert.equal(elements.measurementClearBtn.disabled, false, `${label} enables ${assistMode} Reset at default settings`);
        for (const lock of [{ startInFlight: true }, { activeJobId: 'job-1' }, { saveInFlight: true },
            { autoSubInFlight: true, autoSubJobId: 'recovered-job' }, { speakerAlignInFlight: true }, { hybridWizard: { running: true } }]) {
            const lockedContext = { ...unsavedContext, measurementState: { ...unsavedContext.measurementState, ...lock } };
            measurementJob.init({ getState: () => ({ measurement: lockedContext.measurementState }) });
            panel.renderMeasurementPanelViewSection(lockedContext);
            assert.equal(elements.measurementClearBtn.disabled, true, 'active captures lock Reset');
        }
        measurementJob.init({ getState: () => ({ measurement: {} }) });
        panel.renderMeasurementPanelViewSection({ ...unsavedContext, graphView: 'ir', frequencyView: false });
        assert.equal(elements.measurementClearBtn.disabled, true, 'IR does not offer capture Reset');
    }
}
panel.renderMeasurementPanelViewSection({ ...savedOnlyContext,
    measurementState: { ...measurementState, currentMeasurement: savedTake, currentMeasurementSaved: true } });
assert.equal(elements.measurementClearBtn.disabled, true, 'a saved capture alone does not enable Reset');
for (const [label, overrides, enabled] of [
    ['neutral PEQ', {}, false],
    ['PEQ filters', { peq: { filters: [{ id: 'f1' }] } }, true],
    ['PEQ house curve', { conv: { ...savedOnlyContext.conv, targetCurve: 'harman' } }, true],
    ['PEQ ignores convolver settings', { conv: { ...savedOnlyContext.conv, maxCutDb: -12 } }, false],
    ['neutral convolver', { assistMode: 'convolver' }, false],
    ['convolver ignores PEQ filters', { assistMode: 'convolver', peq: { filters: [{ id: 'f1' }] } }, false],
    ['convolver range', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, rangeStartHz: 35 } }, true],
    ['convolver phase', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, phaseMode: 'linear', quality: 'linear_8192' } }, true],
    ['convolver taps', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, irLength: '4096', quality: 'minimum_4096' } }, true],
    ['convolver quality', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, quality: 'linear_8192' } }, true],
    ['convolver house curve', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, targetCurve: 'harman' } }, true],
    ['neutral custom editor', { assistMode: 'convolver', activeEditor: 'houseCurve' }, false],
    ['convolver safety margin', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, safetyMarginDb: 2 } }, true],
    ['convolver auto gain', { assistMode: 'convolver', conv: { ...savedOnlyContext.conv, autoGainEnabled: false } }, true],
]) {
    panel.renderMeasurementPanelViewSection({ ...savedOnlyContext, ...overrides });
    assert.equal(elements.measurementClearBtn.disabled, !enabled, label);
}
const activeJobContext = { ...savedOnlyContext, conv: { ...savedOnlyContext.conv, targetCurve: 'harman' }, measurementState: { ...measurementState, activeJobId: 'job-1' } };
measurementJob.init({ getState: () => ({ measurement: activeJobContext.measurementState }) });
panel.renderMeasurementPanelViewSection(activeJobContext);
assert.equal(elements.measurementClearBtn.disabled, true, 'active jobs lock Reset');
measurementJob.init({ getState: () => ({ measurement: measurementState }) });
panel.renderMeasurementPanelViewSection(context);
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

// IR texts follow the two IR parts: Align takes on their timing lanes and
// the normal IR overlay; each part only reports what it draws.
const timeline = { minMs: -1.5, maxMs: 2, lanes: [{}] };
const alignEntry = { id: 'align-before' };
const plainEntry = { id: 'room-sweep' };
panel.init({
    getMeasurementIrParts: (entries) => ({ timeline: entries.includes(alignEntry) ? timeline : null,
        plainEntries: entries.filter((entry) => entry !== alignEntry) }),
    speakerAlignTimelineSummary: () => 'Timing: lanes',
});
const irTextContext = { ...irContext, activeEditor: 'none' };
panel.renderMeasurementPanelStatusSection({ ...irTextContext, graphEntries: [alignEntry] });
assert.equal(elements.measurementSummary.textContent, 'Timing -1.5–2.0 ms');
assert.equal(elements.measurementGraphControls.textContent, 'Timing: lanes');
assert.equal(elements.measurementGraphSubtitle.textContent,
    'Impulse response view: Speaker Align takes on their shared time base.');
panel.renderMeasurementPanelStatusSection({ ...irTextContext, graphEntries: [plainEntry, alignEntry] });
assert.equal(elements.measurementSummary.textContent, 'IR -2–30 ms · Timing -1.5–2.0 ms');
assert.equal(elements.measurementGraphControls.textContent, 'Timing: lanes | IR aligned');
assert.equal(elements.measurementGraphSubtitle.textContent,
    'Impulse response view: Speaker Align takes on their shared time base; other measurements -2 ms to +30 ms.');
panel.renderMeasurementPanelStatusSection({ ...irTextContext, graphEntries: [plainEntry] });
assert.equal(elements.measurementSummary.textContent, 'IR -2–30 ms');
assert.equal(elements.measurementGraphControls.textContent, 'IR aligned');
assert.equal(elements.measurementGraphSubtitle.textContent, 'Impulse response view: -2 ms to +30 ms.');

console.log('measurement panel file/action/view/status sections: ok');
