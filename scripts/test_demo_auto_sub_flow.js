#!/usr/bin/env node
// Runs the real Auto-Sub UI flow (static/measurement_flows.js) against the
// demo API with a controllable clock, and asserts the status lines rendered
// during the run and after completion never show placeholder '? ms' values.
// This guards the "Sub 1 ? ms / Sub 2 ? ms" regression at the UI level, not
// just the payload level: the same poll loop and result renderer the browser
// executes are exercised here, fast-forwarded through the timed stages.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

const DEMO_MODULES = [
    'demo/data/library.js',
    'demo/data/radio.js',
    'demo/data/measurements.js',
    'demo/state.js',
    'demo/routes.js',
];

function isSubwooferModeName(mode) {
    return ['subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
}
function isSubwoofer22Mode(mode) {
    return ['subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
}
function normalizeOutputModeName(mode) {
    return isSubwooferModeName(mode) ? mode : 'stereo';
}

// Fresh simulated page load: demo fixtures + API in one VM context, with a
// clock that only advances when the flow's sleep() runs.
function makeContext() {
    const clock = { now: Date.now() };
    const RealDate = Date;
    class FakeDate extends RealDate {
        static now() { return clock.now; }
    }
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        Date: FakeDate,
        Math,
        console,
        URLSearchParams,
        FormData,
        setTimeout() { return 0; },
        clearTimeout() {},
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of DEMO_MODULES) {
        vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), ctx);
    }
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    return { ctx, clock };
}

async function runAutoSubFlow(modeName) {
    const { ctx, clock } = makeContext();
    const MF = ctx.FXRouteMeasurementFlows;
    assert.ok(MF && typeof MF.startAutoSubOptimize === 'function', 'measurement_flows must expose the auto-sub flow');

    const state = {
        measurement: {
            statusText: '',
            autoSubInFlight: false,
            startInFlight: false,
            activeJobId: '',
            activeMeasurementKind: '',
            autoSubCancelRequested: false,
            autoSubJobId: '',
            autoSubResult: null,
            autoSubMeasurements: [],
            currentMeasurementSaved: false,
            currentMeasurementName: '',
            selectedInputId: 'demo_mic',
            selectedInputKey: 'demo_mic',
            selectedChannel: 'left',
            selectedMicInputChannel: '1',
            selectedCalibrationRef: '',
        },
        settings: { audioOutputs: { output_mode: null } },
    };

    const statusLines = [];
    const toasts = [];
    const statusEl = { _text: '' };
    Object.defineProperty(statusEl, 'textContent', {
        get() { return this._text; },
        set(value) { this._text = String(value); statusLines.push(String(value)); },
    });
    const subControl = () => ({ disabled: false });
    const elements = {
        measurementAutoSubStatus: statusEl,
        measurementCalibrationFile: undefined,
        effectsSubwooferDelay: subControl(),
        effectsSubwooferFrequencyNumber: subControl(),
        effectsSubwooferLevel: subControl(),
        effectsSubwooferPolarity: subControl(),
        effectsSubwooferSub2Level: subControl(),
        effectsSubwooferSub2Delay: subControl(),
        effectsSubwooferSub2Polarity: subControl(),
        effectsSubwooferMainHighpass: subControl(),
    };

    const refreshOutputMode = async () => {
        const out = await (await ctx.fetch('/api/audio/outputs')).json();
        state.settings.audioOutputs.output_mode = out.output_mode;
    };

    MF.init({
        getState: () => state,
        getElements: () => elements,
        api: {
            startAutoSubOptimize: (formData) => ctx.fetch('/api/measurements/auto-sub-optimize/start', { method: 'POST', body: formData }),
            pollAutoSubJob: (jobId) => ctx.fetch('/api/measurements/auto-sub-optimize/jobs/' + encodeURIComponent(jobId)),
            cancelAutoSubJob: (jobId) => ctx.fetch('/api/measurements/auto-sub-optimize/jobs/' + encodeURIComponent(jobId) + '/cancel', { method: 'POST' }),
        },
        showToast: (text, type) => toasts.push({ text, type }),
        renderMeasurementPanel: () => {},
        isSubwooferModeName,
        isSubwoofer22Mode,
        normalizeOutputModeName,
        getActiveMeasurementKind: () => state.measurement.activeMeasurementKind,
        hasActiveMeasurementJob: () => !!state.measurement.activeJobId,
        measurementModeReady: () => true,
        normalizeMeasurementInputChannelSelections: () => {},
        getAutoSubTargetCurveSnapshot: () => null,
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        postRuntimeDebugSnapshot: async () => {},
        formatTransitionErrorDetail: (detail, message) => message,
        fetchAudioOutputOverview: refreshOutputMode,
        sleep: async (ms) => { clock.now += ms; },
    });

    await (await ctx.fetch('/api/audio/output-mode', { method: 'POST', body: JSON.stringify({ mode: modeName }) })).json();
    await refreshOutputMode();

    await MF.startAutoSubOptimize();

    const result = state.measurement.autoSubResult;
    assert.ok(result, 'auto-sub flow must finish with a result');
    assert.equal(result.mode, modeName);
    assert.equal(result.applied, true);

    const rendered = [state.measurement.statusText, ...statusLines, ...toasts.map(t => t.text)];
    const placeholders = rendered.filter(line => /\?/.test(line));
    assert.deepEqual(placeholders, [], 'rendered auto-sub lines must never contain placeholders');

    return { state, statusLines, toasts, result };
}

(async () => {
    // 2.1: single sub, coarse -> fine stages, single alignment result.
    // (Real .104 delays can be negative, so the status patterns allow a sign.)
    const r21 = await runAutoSubFlow('subwoofer-2.1');
    assert.ok(Number.isFinite(r21.result.applied_alignment_ms));
    assert.ok(Number.isFinite(r21.result.original_alignment_ms));
    assert.ok(r21.result.coarse_winner && Number.isFinite(r21.result.coarse_winner.delay_ms));
    assert.ok(r21.result.runner_up && Number.isFinite(r21.result.runner_up.delay_ms));
    assert.match(r21.state.measurement.statusText, /AutoSub applied: -?\d+\.\d+ ms \(was -?\d+\.\d+ ms\)/);
    // The inline status element must have shown live progress during the run.
    const sawProgress = r21.statusLines.some(line => /sweeps|candidates/.test(line));
    assert.ok(sawProgress, '2.1 run must show live sweep/candidate progress lines');
    const finalLine = r21.statusLines[r21.statusLines.length - 1];
    assert.match(finalLine, /Coarse: -?\d+\.\d+ ms \(\d+\.\d+ %\) · Fine checked: -?\d+\.\d+ ms \(\d+\.\d+ %\)/);

    // 2.2: per-sub coarse scans + combined matrix, both sub alignments set.
    const r22 = await runAutoSubFlow('subwoofer-2.2');
    assert.ok(Number.isFinite(r22.result.applied_sub1_alignment_ms));
    assert.ok(Number.isFinite(r22.result.applied_sub2_alignment_ms));
    assert.ok(Number.isFinite(r22.result.derived_main_delay_ms));
    assert.ok(Number.isFinite(r22.result.derived_sub1_delay_ms));
    assert.ok(Number.isFinite(r22.result.derived_sub2_delay_ms));
    assert.ok(r22.result.sub1_coarse_winner && Number.isFinite(r22.result.sub1_coarse_winner.delay_ms));
    assert.ok(r22.result.sub2_coarse_winner && Number.isFinite(r22.result.sub2_coarse_winner.delay_ms));
    assert.match(r22.state.measurement.statusText, /AutoSub 2\.2 applied: Sub 1 -?\d+\.\d+ ms \(was -?\d+\.\d+ ms\) · Sub 2 -?\d+\.\d+ ms \(was -?\d+\.\d+ ms\)/);
    const sawSubStages = r22.statusLines.some(line => /Optimizing Sub 1/.test(line))
        && r22.statusLines.some(line => /Optimizing Sub 2/.test(line));
    assert.ok(sawSubStages, '2.2 run must show per-sub stage progress lines');
    assert.match(r22.statusLines[r22.statusLines.length - 1], /Derived: Main -?\d+\.\d+ ms \/ Sub 1 -?\d+\.\d+ ms \/ Sub 2 -?\d+\.\d+ ms/);

    console.log('ok demo auto-sub UI flow (2.1 + 2.2, no placeholders)');
})();
