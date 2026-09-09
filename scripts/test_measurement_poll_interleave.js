#!/usr/bin/env node
'use strict';

// Contract: a stale measurement poll (job A) must not overwrite UI state
// after job B became active. Mirrors the jobGeneration/activeJobId guard of
// the single-measurement path (app.js pollMeasurementJob), which the
// auto-sub and hybrid poll loops lacked: a late response from A wrote
// statusText/measurements/captures unconditionally, clobbering B.
//
// Both tests drive the real poll loops from static/measurement_flows.js in
// a vm sandbox with a deferred first fetch: while A's response is held
// back, B becomes active (new activeJobId/kind/generation); A's late
// completion must then leave B's state untouched.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function deferred() {
    let resolve;
    const promise = new Promise((res) => { resolve = res; });
    return { promise, resolve };
}

function flush(times = 10) {
    let chain = Promise.resolve();
    for (let i = 0; i < times; i++) chain = chain.then(() => new Promise((r) => setImmediate(r)));
    return chain;
}

function makeFlowsContext({ state, api, toasts }) {
    const ctx = {
        console,
        Math,
        JSON,
        Object,
        Array,
        Promise,
        String,
        Number,
        Boolean,
        setTimeout,
        clearTimeout,
        FormData,
        window: {},
        // measurement_flows.js captures these from window.* at load time.
        FXRouteMeasurementUI: {
            MEASUREMENT_JOB_SUCCESS_STATES: new Set(['completed']),
            MEASUREMENT_JOB_FAILED_STATES: new Set(['failed']),
            MEASUREMENT_JOB_CANCELLED_STATES: new Set(['cancelled']),
        },
        FXRouteHybridMeasurement: { getDiagramState: () => ({ speakers: {} }) },
        hybridSpeakerName: () => 'speaker',
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    const MF = ctx.FXRouteMeasurementFlows;
    assert.ok(MF && typeof MF.pollAutoSubJob === 'function');
    assert.ok(typeof MF.runHybridWizardStep === 'function');
    MF.init({
        getState: () => state,
        getElements: () => ({}),
        api,
        sleep: async () => {},
        showToast: (message) => { toasts.push(String(message)); },
        renderMeasurementPanel: () => {},
        renderMeasurementPanelDefensively: () => {},
        isSubwooferModeName: () => false,
        isSubwoofer22Mode: () => false,
        getActiveMeasurementKind: () => state.measurement.activeMeasurementKind || '',
        hasActiveMeasurementJob: () => false,
        measurementModeReady: () => true,
        normalizeMeasurementInputChannelSelections: () => {},
        getAutoSubTargetCurveSnapshot: () => null,
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        postRuntimeDebugSnapshot: async () => {},
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        fetchAudioOutputOverview: async () => ({}),
        getMeasurementReferenceWarning: () => '',
        normalizeOutputModeName: (mode) => mode || 'stereo',
        getMeasurementJobStatus: (job) => job.status || 'unknown',
        normalizeMeasurementEntry: (entry) => entry,
        getMeasurementJobResultMeasurement: () => null,
        setMeasurementAssistMode: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
    });
    return MF;
}

function completedAutoSubResponse() {
    return {
        ok: true,
        status: 200,
        json: async () => ({
            job: {
                id: 'job-A',
                status: 'completed',
                message: 'Auto Sub A done',
                baseline_measurement: { id: 'base-A' },
                result: { mode: 'stereo' },
            },
        }),
    };
}

async function testStaleAutoSubPollDoesNotClobberNewJob() {
    const toasts = [];
    const state = {
        measurement: {
            statusText: '',
            autoSubInFlight: true,
            startInFlight: false,
            activeJobId: '',
            activeMeasurementKind: 'auto_sub',
            autoSubCancelRequested: false,
            autoSubJobId: 'job-A',
            autoSubResult: null,
            autoSubMeasurements: [],
            currentMeasurementSaved: false,
            jobGeneration: 5,
        },
        settings: {},
    };
    const gate = deferred();
    const MF = makeFlowsContext({
        state,
        toasts,
        api: { pollAutoSubJob: () => gate.promise },
    });

    const poll = MF.pollAutoSubJob('job-A');
    await flush();
    // Job B (single sweep) becomes active while A's response is in flight.
    state.measurement.activeJobId = 'job-B';
    state.measurement.activeMeasurementKind = 'single';
    state.measurement.jobGeneration = 6;
    state.measurement.statusText = 'Single: running';
    gate.resolve(completedAutoSubResponse());
    await poll;

    assert.strictEqual(
        state.measurement.statusText,
        'Single: running',
        'stale auto-sub completion must not overwrite the new job status'
    );
    assert.strictEqual(state.measurement.autoSubResult, null);
    assert.strictEqual(state.measurement.autoSubMeasurements.length, 0);
    assert.ok(
        !toasts.some((message) => message.includes('Auto Sub A done')),
        'stale auto-sub completion must not toast into the new job'
    );
    console.log('stale auto-sub poll does not clobber new job: ok');
}

function completedHybridResponse() {
    return {
        ok: true,
        status: 200,
        json: async () => ({
            job: { id: 'job-H', status: 'completed', message: 'Hybrid H done' },
        }),
    };
}

async function testStaleHybridPollDoesNotClobberNewJob() {
    const toasts = [];
    const state = {
        measurement: {
            statusText: '',
            activeJobId: '',
            activeMeasurementKind: '',
            jobGeneration: 5,
            selectedInputId: 'in1',
            selectedInputKey: 'k',
            selectedChannel: 'left',
            selectedMicInputChannel: '1',
            selectedReferenceInputChannel: '',
        },
        settings: {},
    };
    const gate = deferred();
    const MF = makeFlowsContext({
        state,
        toasts,
        api: {
            startMeasurement: async () => ({
                ok: true,
                json: async () => ({ job: { id: 'job-H' } }),
            }),
            pollMeasurementJob: () => gate.promise,
            cancelMeasurementJob: async () => ({ ok: true, json: async () => ({}) }),
        },
    });

    const step = { id: 's1', role: 'secondary', position: 'left', channel: 'left' };
    const run = MF.runHybridWizardStep(step);
    await flush(20);
    assert.strictEqual(state.measurement.activeJobId, 'job-H', 'hybrid step must own the active job first');
    // Job B (single sweep) becomes active while H's response is in flight.
    state.measurement.activeJobId = 'job-B';
    state.measurement.activeMeasurementKind = 'single';
    state.measurement.jobGeneration = 6;
    gate.resolve(completedHybridResponse());
    await run;

    const wizard = MF.getHybridWizardState();
    assert.strictEqual(
        JSON.stringify(wizard.captures),
        '[]',
        'stale hybrid step must not capture'
    );
    assert.strictEqual(wizard.stepIndex, 0, 'stale hybrid step must not advance');
    console.log('stale hybrid poll does not clobber new job: ok');
}

(async () => {
    await testStaleAutoSubPollDoesNotClobberNewJob();
    await testStaleHybridPollDoesNotClobberNewJob();
    console.log('measurement poll interleave contracts: ok');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
