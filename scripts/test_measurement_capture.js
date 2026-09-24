#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Capture start keeps payloads, state transitions and flush-before-sweep order.
const assert = require('node:assert/strict');
const capture = require('../static/measurement_capture.js');

class FormStub {
    constructor() { this.fields = []; }
    append(key, value) { this.fields.push([key, value]); }
    get(key) { const hit = this.fields.filter(([k]) => k === key).at(-1); return hit ? hit[1] : undefined; }
}

function makeHarness({ area = { sides: ['left', 'right', 'stereo'], bank_id: 'main' }, calibrationFile = null } = {}) {
    const state = {
        measurement: {
            jobGeneration: 0,
            hostCaptureAvailable: true,
            selectedInputId: 'pw-source-1',
            selectedInputKey: 'key-1',
            sweepSide: 'stereo',
            selectedMicInputChannel: '1',
            selectedCalibrationRef: 'cal-1',
            currentMeasurementName: 'Take',
            activeJobId: '',
            activeMeasurementKind: '',
            pendingRepeatMeasurements: [],
            currentMeasurementSaved: true,
            statusText: '',
            startInFlight: false,
            cancelRequested: false,
            repeatJobActive: false,
        },
    };
    const elements = { measurementCalibrationFile: calibrationFile ? { files: [calibrationFile] } : null };
    const calls = [];
    let jobResponse = { job: { id: 'job-1', job_kind: 'single' } };
    capture.init({
        getState: () => state,
        getElements: () => elements,
        getFormDataType: () => FormStub,
        fetch: async (url, options) => {
            calls.push(['fetch', url, options.body]);
            return { ok: true, json: async () => jobResponse };
        },
        showToast: (message, kind) => calls.push(['toast', message, kind]),
        renderMeasurementPanel: () => calls.push('render'),
        requireConcreteFilterBank: () => true,
        measurementModeReady: () => !!(state.measurement.hostCaptureAvailable && state.measurement.selectedInputId),
        measurementRepeatBlockedReason: () => '',
        flushSubwooferSettingsBeforeMeasurement: async () => { calls.push('flush'); },
        measurementAreaFromCatalog: () => area,
        appendMeasurementReferenceFields: (form) => form.append('reference_input_channel', 'ref'),
        postRuntimeDebugSnapshot: () => calls.push('snapshot'),
        formatTransitionErrorDetail: (detail, fallback) => (typeof detail === 'string' ? detail : fallback),
        normalizeMeasurementKind: (kind) => kind,
        formatMeasurementJobStatusText: () => 'Preparing…',
        pollMeasurementJob: async (jobId) => calls.push(['poll', jobId]),
        cancelMeasurement: async () => calls.push('cancel'),
    });
    return { state, calls, setJobResponse: (r) => { jobResponse = r; } };
}

async function main() {
    // 1. Single sweep flushes subwoofer saves before the POST and sends area/bank/ref/calibration.
    {
        const { state, calls } = makeHarness();
        await capture.startHostMeasurement(1);
        const fetchCall = calls.find((c) => Array.isArray(c) && c[0] === 'fetch');
        assert.ok(calls.includes('flush'), 'subwoofer commit lands before the sweep POST');
        assert.ok(calls.indexOf('flush') < calls.indexOf(fetchCall), 'flush runs before fetch');
        assert.equal(fetchCall[1], '/api/measurements/start');
        const form = fetchCall[2];
        assert.equal(form.get('input_id'), 'pw-source-1');
        assert.equal(form.get('channel'), 'stereo');
        assert.equal(form.get('measurement_bank'), 'main');
        assert.equal(form.get('mic_input_channel'), '1');
        assert.equal(form.get('reference_input_channel'), 'ref');
        assert.equal(form.get('calibration_ref'), 'cal-1');
        assert.equal(state.measurement.activeJobId, 'job-1');
        assert.equal(state.measurement.activeMeasurementKind, 'single');
        assert.deepEqual(calls.at(-1), ['poll', 'job-1']);
    }

    // 2. A picked calibration file wins over the stored reference.
    {
        const { calls } = makeHarness({ calibrationFile: { name: 'mic.txt' } });
        await capture.startHostMeasurement(1);
        const form = calls.find((c) => c[0] === 'fetch')[2];
        assert.equal(form.get('calibration_file').name, 'mic.txt');
        assert.equal(form.get('calibration_ref'), undefined);
    }

    // 3. L/R repeat freezes the bank, carries the base name and no channel side.
    {
        const { state, calls, setJobResponse } = makeHarness();
        setJobResponse({ job: { id: 'job-1', job_kind: 'lr_repeat' } });
        await capture.startLrRepeatMeasurement(2);
        const fetchCall = calls.find((c) => c[0] === 'fetch');
        assert.equal(fetchCall[1], '/api/measurements/lr-repeat/start');
        assert.equal(fetchCall[2].get('measurement_bank'), 'main');
        assert.equal(fetchCall[2].get('base_name'), 'Take');
        assert.equal(fetchCall[2].get('channel'), undefined);
        assert.equal(state.measurement.repeatJobActive, true);
        assert.equal(state.measurement.activeMeasurementKind, 'lr_repeat');
    }

    // 4. startMeasurement guards concurrent starts and clears the flight flag.
    {
        const { state, calls } = makeHarness();
        state.measurement.startInFlight = true;
        state.measurement.activeJobId = 'busy';
        await capture.startMeasurement();
        assert.ok(!calls.some((c) => c[0] === 'fetch'), 'a running job blocks a second start');
        state.measurement.startInFlight = false;
        state.measurement.activeJobId = '';
        await capture.startMeasurement();
        assert.equal(state.measurement.jobGeneration, 1);
        assert.equal(state.measurement.startInFlight, false);
        assert.equal(state.measurement.activeJobId, 'job-1');
    }

    // 5. startMeasurement without a usable input reports and never POSTs.
    {
        const { state, calls } = makeHarness();
        state.measurement.selectedInputId = '';
        await capture.startMeasurement();
        assert.ok(!calls.some((c) => c[0] === 'fetch'));
        assert.match(state.measurement.statusText, /No usable host capture source/);
    }

    // 6. startLrRepeat respects the area block reason.
    {
        const { state, calls } = makeHarness();
        capture.init({
            getState: () => state,
            getElements: () => ({}),
            getFormDataType: () => FormStub,
            fetch: async () => { calls.push('fetch'); return { ok: true, json: async () => ({}) }; },
            showToast: (message, kind) => calls.push(['toast', message, kind]),
            renderMeasurementPanel: () => calls.push('render'),
            requireConcreteFilterBank: () => true,
            measurementModeReady: () => true,
            measurementRepeatBlockedReason: () => 'One-sided area',
            flushSubwooferSettingsBeforeMeasurement: async () => {},
            measurementAreaFromCatalog: () => null,
            appendMeasurementReferenceFields: () => {},
            postRuntimeDebugSnapshot: () => {},
            formatTransitionErrorDetail: (d, f) => f,
            normalizeMeasurementKind: (k) => k,
            formatMeasurementJobStatusText: () => '',
            pollMeasurementJob: async () => {},
            cancelMeasurement: async () => {},
        });
        await capture.startLrRepeat();
        assert.equal(state.measurement.statusText, 'One-sided area');
        assert.ok(!calls.some((c) => c === 'fetch' || (Array.isArray(c) && c[0] === 'fetch')));
    }

    console.log('measurement capture start: ok');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
