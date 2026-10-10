#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// The name field: an Auto Sub result is not taken over as the base name of the
// next L/R repeat, a name the user typed is.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const ui = require('../static/measurement_ui.js');
const capture = require('../static/measurement_capture.js');

class FormStub {
    constructor() { this.fields = []; }
    append(key, value) { this.fields.push([key, value]); }
    get(key) { const hit = this.fields.filter(([k]) => k === key).at(-1); return hit ? hit[1] : undefined; }
}

function makeFlows() {
    const ctx = { console, setInterval() { return 0; }, clearInterval() {}, setTimeout() { return 0; }, clearTimeout() {} };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_ui.js'), 'utf8'), ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    return ctx.FXRouteMeasurementFlows;
}

function makeState(measurement = {}) {
    return { measurement: {
        jobGeneration: 0, hostCaptureAvailable: true, selectedInputId: 'in-1', selectedInputKey: '',
        sweepSide: 'stereo', selectedMicInputChannel: '1', selectedCalibrationRef: '', activeJobId: '',
        activeMeasurementKind: '', pendingRepeatMeasurements: [], autoSubMeasurements: [],
        currentMeasurementSaved: true, currentMeasurementName: '', typedMeasurementName: '',
        statusText: '', startInFlight: false, cancelRequested: false, repeatJobActive: false,
        autoSubResult: null, autoSubInFlight: false,
        ...measurement,
    } };
}

// The base name an L/R repeat start sends for this state.
async function lrRepeatBaseName(state) {
    const calls = [];
    capture.init({
        getState: () => state,
        getElements: () => ({}),
        getFormDataType: () => FormStub,
        fetch: async (url, options) => { calls.push(options.body); return { ok: true, json: async () => ({ job: { id: 'j', job_kind: 'lr_repeat' } }) }; },
        showToast: () => {},
        renderMeasurementPanel: () => {},
        requireMeasurableArea: () => true,
        measurementRepeatBlockedReason: () => '',
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        measurementAreaFromCatalog: () => ({ sides: ['stereo'], bank_id: 'global' }),
        appendMeasurementReferenceFields: () => {},
        postRuntimeDebugSnapshot: () => {},
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        normalizeMeasurementKind: (kind) => kind,
        formatMeasurementJobStatusText: () => '',
        pollMeasurementJob: async () => {},
        cancelMeasurement: async () => {},
    });
    await capture.startLrRepeatMeasurement(1);
    return calls[0].get('base_name');
}

// An Auto Sub result lands in the state, then an L/R repeat starts from it.
async function autoSubThenLrRepeat(measurement) {
    const state = makeState(measurement);
    const flows = makeFlows();
    flows.init({
        getState: () => state,
        getElements: () => ({}),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        isSubwoofer22Mode: () => false,
    });
    await flows.handleAutoSubResult({ status: 'completed', message: '', result: {
        mode: 'subwoofer-2.1', applied: true, applied_alignment_ms: 0.5, original_alignment_ms: 0,
        suggested_alignment_ms: 0.5, confidence: 'clear', apply_decision: 'applied_clear_confidence',
        winner: { delay_ms: 0.5, score_pct: 60 },
        baseline_measurement: { id: 'base', traces: [{ role: 'left', points: [[100, 0]] }] },
    } });
    return { afterAutoSub: state.measurement.currentMeasurementName, baseName: await lrRepeatBaseName(state) };
}

(async () => {
    // 1. Nothing typed: the L/R repeat after an Auto Sub result starts with a
    //    blank base name (the backend then names it "L/R Repeat <time>").
    {
        const { afterAutoSub, baseName } = await autoSubThenLrRepeat({});
        assert.equal(afterAutoSub, '');
        assert.equal(baseName, '');
    }

    // 2. A name typed before the Auto Sub run stays through it and is the
    //    base name of the repeat.
    {
        const { afterAutoSub, baseName } = await autoSubThenLrRepeat({
            currentMeasurementName: 'Seat A', typedMeasurementName: 'Seat A' });
        assert.equal(afterAutoSub, 'Seat A');
        assert.equal(baseName, 'Seat A');
    }

    // 3. A name proposed by an earlier result is not a typed one.
    {
        const { afterAutoSub, baseName } = await autoSubThenLrRepeat({
            currentMeasurementName: 'Demo Measurement 4', typedMeasurementName: '' });
        assert.equal(afterAutoSub, '');
        assert.equal(baseName, '');
    }

    // 4. A name typed while an Auto Sub result is pending is the base name of
    //    the repeat.
    {
        const state = makeState({ currentMeasurementName: 'Seat B', typedMeasurementName: 'Seat B',
            autoSubMeasurements: [{ id: 'autosub-1' }] });
        assert.equal(await lrRepeatBaseName(state), 'Seat B');
    }

    // 5. getTypedMeasurementName: the typed text counts only while the field
    //    still shows it (a proposal replaces it), compared without spaces.
    assert.equal(ui.getTypedMeasurementName({ typedMeasurementName: 'Seat A', currentMeasurementName: 'Seat A' }), 'Seat A');
    assert.equal(ui.getTypedMeasurementName({ typedMeasurementName: ' Seat A ', currentMeasurementName: 'Seat A' }), 'Seat A');
    assert.equal(ui.getTypedMeasurementName({ typedMeasurementName: 'Seat A', currentMeasurementName: 'Sweep 2' }), '');
    assert.equal(ui.getTypedMeasurementName({ typedMeasurementName: '', currentMeasurementName: '' }), '');
    assert.equal(ui.getTypedMeasurementName({}), '');

    // 6. The app keeps the typed text next to the shown name.
    {
        const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
        assert.match(app, /currentMeasurementName: '',\s*\n\s*typedMeasurementName: '',/);
        assert.match(app, /measurementNameInput\.addEventListener\('input', \(event\) => \{\s*\n\s*state\.measurement\.currentMeasurementName = event\.target\.value \|\| '';\s*\n\s*state\.measurement\.typedMeasurementName = state\.measurement\.currentMeasurementName;/);
    }

    console.log('measurement name field: ok');
})().catch((error) => { console.error(error); process.exitCode = 1; });
