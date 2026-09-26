#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Job lifecycle keeps generation guards, cancel-before-id and defensive render.
const assert = require('node:assert/strict');
require('../static/measurement_ui.js');
const job = require('../static/measurement_job.js');

function makeJobHarness(overrides = {}) {
    const state = {
        measurement: {
            jobGeneration: 7,
            activeJobId: '',
            activeMeasurementKind: '',
            startInFlight: false,
            cancelRequested: false,
            repeatJobActive: false,
            statusText: '',
            autoSubInFlight: false,
            speakerAlignInFlight: false,
            hybridWizard: {},
            measurements: [],
            visibilityById: {},
            reviewVisibilityById: {},
            calibrationUpdating: false,
            calibrationDeleting: false,
            inputsLoading: false,
        },
    };
    const elements = {
        measurementSweepToggleBtn: { disabled: false, textContent: '', setAttribute: () => {}, },
        measurementSweepMenu: { classList: { contains: () => true }, parentElement: { contains: () => true, insertBefore: () => {} } },
        measurementHybridHeaderActions: { contains: () => true },
        measurementRepeatStartBtn: { disabled: false, textContent: '' },
        measurementRepeatNote: { textContent: '' },
    };
    const calls = [];
    const harness = {
        state, elements, calls,
        fetchResponses: [],
        initExtra: {},
    };
    job.init({
        getState: () => state,
        getElements: () => elements,
        fetch: async (url) => {
            calls.push(['fetch', url]);
            const next = harness.fetchResponses.shift() || { ok: true, json: async () => ({ job: {} }) };
            return next;
        },
        showToast: (message, kind) => calls.push(['toast', message, kind]),
        renderMeasurementPanel: () => calls.push('render'),
        normalizeMeasurementKind: (kind) => kind,
        formatMeasurementInputLevelText: () => '',
        getMeasurementJobStatus: (j) => j.status || 'unknown',
        getMeasurementJobResultMeasurement: () => null,
        getMeasurementTimingInfo: () => ({}),
        normalizeMeasurementEntry: (entry) => entry,
        measurementModeReady: () => true,
        measurementRepeatBlockedReason: () => '',
        syncMeasurementRepeatNote: () => calls.push('repeat-note'),
        setMeasurementSweepMenuOpen: (open) => calls.push(['sweep-menu', open]),
        syncAutoSubButton: () => calls.push('autosub-btn'),
        syncSpeakerAlignButton: () => calls.push('align-btn'),
        cancelHybridWizardMeasurement: async () => calls.push('cancel-hybrid'),
        cancelAutoSubOptimize: async () => calls.push('cancel-autosub'),
        cancelSpeakerAlign: async () => calls.push('cancel-align'),
        postRuntimeDebugSnapshot: async () => calls.push('snapshot'),
        formatTransitionErrorDetail: (detail, fallback) => (typeof detail === 'string' ? detail : fallback),
        sleep: async () => {},
        ...overrides,
        ...(harness.initExtra),
    });
    return harness;
}

async function main() {
    // 1. Kind dispatch stays exact across auto_sub / speaker / hybrid / repeat / single.
    {
        const { state } = makeJobHarness();
        assert.equal(job.getActiveMeasurementKind(), '');
        assert.equal(job.hasActiveMeasurementJob(), false);
        state.measurement.autoSubInFlight = true;
        assert.equal(job.getActiveMeasurementKind(), 'auto_sub');
        state.measurement.autoSubInFlight = false;
        state.measurement.speakerAlignInFlight = true;
        assert.equal(job.getActiveMeasurementKind(), 'speaker_align');
        state.measurement.speakerAlignInFlight = false;
        state.measurement.hybridWizard = { running: true };
        assert.equal(job.getActiveMeasurementKind(), 'hybrid');
        state.measurement.hybridWizard = {};
        state.measurement.activeJobId = 'j1';
        state.measurement.activeMeasurementKind = 'single';
        assert.equal(job.getActiveMeasurementKind(), 'single');
        state.measurement.repeatJobActive = true;
        assert.equal(job.getActiveMeasurementKind(), 'single', 'explicit kind wins while set');
        state.measurement.activeMeasurementKind = '';
        assert.equal(job.getActiveMeasurementKind(), 'lr_repeat');
        assert.equal(job.hasActiveMeasurementJob(), true);
    }

    // 2. Cancel without a job id is a silent no-op; cancelRequested path stays.
    {
        const { state, calls } = makeJobHarness();
        await job.cancelMeasurement();
        assert.ok(!calls.some((c) => Array.isArray(c) && c[0] === 'fetch'), 'no cancel POST without a job id');
        await job.requestMeasurementCancellation();
        assert.deepEqual(calls, [], 'idle cancel stays silent');
        state.measurement.startInFlight = true;
        await job.requestMeasurementCancellation();
        assert.equal(state.measurement.cancelRequested, true);
        // Cancelling is progress: sweep feature line, not the outcome line.
        assert.equal(state.measurement.progressKind, 'sweep');
        assert.equal(state.measurement.progressText, 'Cancelling…');
        assert.equal(state.measurement.statusText, '');
    }

    // 3. Dispatch prefers hybrid / auto_sub / speaker_align over the sweep cancel.
    {
        for (const [kind, marker] of [['hybrid', 'cancel-hybrid'], ['auto_sub', 'cancel-autosub'], ['speaker_align', 'cancel-align']]) {
            const { state, calls } = makeJobHarness();
            state.measurement.activeJobId = 'j1';
            state.measurement.activeMeasurementKind = kind === 'hybrid' ? '' : kind;
            if (kind === 'hybrid') state.measurement.hybridWizard = { running: true };
            if (kind === 'auto_sub') state.measurement.autoSubInFlight = true;
            if (kind === 'speaker_align') state.measurement.speakerAlignInFlight = true;
            await job.requestMeasurementCancellation();
            assert.ok(calls.includes(marker), `${kind} dispatches to its owner`);
        }
    }

    // 4. Stale poll responses never clobber the newer job.
    {
        const h = makeJobHarness();
        h.state.measurement.activeJobId = 'job-A';
        h.state.measurement.jobGeneration = 7;
        let releaseFetch;
        const gate = new Promise((resolve) => { releaseFetch = resolve; });
        job.init({
            getState: () => h.state,
            getElements: () => h.elements,
            fetch: async () => { await gate; return { ok: true, json: async () => ({ job: { id: 'job-A', status: 'completed', message: 'A done' } }) }; },
            showToast: () => {},
            renderMeasurementPanel: () => {},
            normalizeMeasurementKind: (k) => k,
            formatMeasurementInputLevelText: () => '',
            getMeasurementJobStatus: (j) => j.status,
            getMeasurementJobResultMeasurement: () => null,
            getMeasurementTimingInfo: () => ({}),
            normalizeMeasurementEntry: (e) => e,
            measurementModeReady: () => true,
            measurementRepeatBlockedReason: () => '',
            syncMeasurementRepeatNote: () => {},
            setMeasurementSweepMenuOpen: () => {},
            syncAutoSubButton: () => {},
            syncSpeakerAlignButton: () => {},
            cancelHybridWizardMeasurement: async () => {},
            cancelAutoSubOptimize: async () => {},
            cancelSpeakerAlign: async () => {},
            postRuntimeDebugSnapshot: async () => {},
            formatTransitionErrorDetail: (d, f) => f,
            sleep: async () => {},
        });
        const polling = job.pollMeasurementJob('job-A', 7);
        await new Promise((r) => setImmediate(r));
        h.state.measurement.activeJobId = 'job-B';
        h.state.measurement.jobGeneration = 8;
        h.state.measurement.statusText = 'B running';
        releaseFetch();
        await polling;
        assert.equal(h.state.measurement.activeJobId, 'job-B', 'late A response keeps B active');
        assert.equal(h.state.measurement.statusText, 'B running');
    }

    // 5. Defensive render clears the job and reports once.
    {
        const { state, calls } = makeJobHarness({
            renderMeasurementPanel: () => { throw new Error('render boom'); },
        });
        state.measurement.activeJobId = 'j1';
        state.measurement.startInFlight = true;
        assert.equal(job.renderMeasurementPanelDefensively('test render'), false);
        assert.equal(state.measurement.activeJobId, '');
        assert.equal(state.measurement.startInFlight, false);
        assert.match(state.measurement.statusText, /could not be rendered/);
        assert.ok(calls.some((c) => Array.isArray(c) && c[0] === 'toast'));
    }

    // 6. Sweep fallback disables repeat while blocked and labels LR cancel.
    {
        const { state, elements } = makeJobHarness({ measurementRepeatBlockedReason: () => 'One-sided area' });
        state.measurement.activeMeasurementKind = 'lr_repeat';
        state.measurement.activeJobId = 'j1';
        job.syncMeasurementStartButtonFallback();
        assert.equal(elements.measurementRepeatStartBtn.textContent, 'Cancel measurement');
        assert.equal(elements.measurementRepeatStartBtn.disabled, false, 'running repeat stays cancellable');
    }

    // 7. Progress stays on the sweep feature line while the job runs; the
    //    panel status line only gets the labelled outcome. The idle note of
    //    the page shell comes back once the job ends.
    {
        const seen = [];
        const h = makeJobHarness({
            getMeasurementTimingInfo: () => ({ line: 'Acoustic-only timing · delay 3.21 ms · timing stable' }),
            getMeasurementJobResultMeasurement: (j) => j.result?.measurement || null,
            setMeasurementGraphView: (view) => { h.state.measurement.measurementView = view; },
        });
        h.state.measurement.measurementView = 'ir';
        const sweepStatus = { textContent: 'Measures frequency and impulse response.', dataset: {} };
        h.elements.measurementSweepStatus = sweepStatus;
        h.state.measurement.activeJobId = 'j1';
        h.state.measurement.activeMeasurementKind = 'single';
        h.state.measurement.startInFlight = true;
        h.fetchResponses.push(
            { ok: true, json: async () => ({ job: { id: 'j1', status: 'running', message: 'Running sweep…' } }) },
            { ok: true, json: async () => ({ job: { id: 'j1', status: 'completed', message: 'Measurement finished.',
                result: { measurement: { id: 'm1', review_traces: [] } } } }) },
        );
        job.init({ sleep: async () => { seen.push([sweepStatus.textContent, h.state.measurement.statusText]); } });
        await job.pollMeasurementJob('j1', 7);
        assert.deepEqual(seen, [['Running sweep…', '']]);
        assert.equal(h.state.measurement.statusText, 'Sweep finished · Acoustic-only timing · delay 3.21 ms · timing stable');
        assert.equal(sweepStatus.textContent, 'Measures frequency and impulse response.');
        assert.ok(h.calls.some((c) => Array.isArray(c) && c[0] === 'toast' && c[1] === 'Sweep finished'));
        // A sweep result opens in the frequency view, even after an IR one.
        assert.equal(h.state.measurement.measurementView, 'freq');
    }

    // 7b. An L/R repeat result opens in the frequency view too.
    {
        const h = makeJobHarness({
            setMeasurementGraphView: (view) => { h.state.measurement.measurementView = view; },
        });
        h.state.measurement.measurementView = 'ir';
        h.state.measurement.activeJobId = 'j3';
        h.state.measurement.activeMeasurementKind = 'lr_repeat';
        h.state.measurement.repeatJobActive = true;
        h.fetchResponses.push({ ok: true, json: async () => ({ job: { id: 'j3', status: 'completed',
            message: 'Measurement finished.', result: { base_name: 'Seat',
                measurements: [{ id: 'l', review_traces: [] }, { id: 'r', review_traces: [] }] } } }) });
        await job.pollMeasurementJob('j3', 7);
        assert.deepEqual(h.state.measurement.pendingRepeatMeasurements.map((item) => item.id), ['l', 'r']);
        assert.equal(h.state.measurement.measurementView, 'freq');
    }

    // 8. An L/R repeat cancel is labelled as such.
    {
        const h = makeJobHarness();
        h.state.measurement.activeJobId = 'j2';
        h.state.measurement.activeMeasurementKind = 'lr_repeat';
        h.state.measurement.repeatJobActive = true;
        h.fetchResponses.push({ ok: true, json: async () => ({ job: { id: 'j2', status: 'cancelled', message: 'Measurement cancelled.' } }) });
        await job.pollMeasurementJob('j2', 7);
        assert.equal(h.state.measurement.statusText, 'L/R repeat cancelled.');
        assert.equal(h.state.measurement.progressText, '');
    }

    console.log('measurement job lifecycle: ok');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
