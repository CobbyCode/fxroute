#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Reload recovery owns progress, cancellation and terminal result rendering.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const appSource = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
assert.match(appSource, /getCurrentAutoSubJob:\s*\(\) => fetch\('\/api\/measurements\/auto-sub-optimize\/current'/);
assert.match(appSource, /resetMeasurementTransientStatus\(\);\s*renderMeasurementPanel\(\);\s*void MeasurementFlows\.recoverAutoSubJob\(\);/);
assert.ok(/socket\.onopen = \(\) => \{[\s\S]*?if \(isMeasurementPanelOpen\(\)\) void MeasurementFlows\.recoverAutoSubJob\(\);/.test(appSource),
    'WebSocket reconnect checks an open measurement panel');

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

async function flush() {
    for (let i = 0; i < 8; i += 1) await new Promise((done) => setImmediate(done));
}

function harness(currentJob, { sleep = async () => {}, poll = async () => ({}) } = {}) {
    const state = { measurement: { jobGeneration: 1, autoSubJobId: '', autoSubInFlight: false,
        startInFlight: false, activeMeasurementKind: '', autoSubMeasurements: [], statusText: '' },
        outputSystem: { catalog: { active_mode: 'test', modes: { test: { topology: { sub_mode: 'mono' } } } } } };
    const button = { disabled: false, textContent: '' };
    const status = { textContent: 'Idle note', dataset: {} };
    const group = { classList: { add: () => {}, remove: () => {} } };
    const calls = [];
    const ctx = { console, Date, FormData, setTimeout, window: null,
        FXRouteMeasurementUI: {}, FXRouteHybridMeasurement: {} };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/measurement_flows.js'), 'utf8'), ctx);
    const flows = ctx.FXRouteMeasurementFlows;
    flows.init({
        getState: () => state,
        getElements: () => ({ measurementAutoSubStartBtn: button,
            measurementAutoSubGroup: group, measurementAutoSubStatus: status }),
        api: {
            getCurrentAutoSubJob: async () => ({ ok: true, json: async () => ({ job: currentJob }) }),
            pollAutoSubJob: async (id) => { calls.push(['poll', id]); return poll(); },
            cancelAutoSubJob: async (id) => { calls.push(['cancel', id]); return { ok: true, json: async () => ({}) }; },
        },
        sleep, showToast: (...args) => calls.push(['toast', ...args]),
        renderMeasurementPanel: () => flows.syncAutoSubButton(),
        getActiveMeasurementKind: () => state.measurement.autoSubInFlight ? 'auto_sub' : '',
        hasActiveMeasurementJob: () => !!state.measurement.autoSubInFlight,
        measurementModeReady: () => true,
        fetchAudioOutputOverview: async () => {},
        isSubwoofer22Mode: () => false,
    });
    return { flows, state: state.measurement, button, status, calls };
}

(async () => {
    const pause = deferred();
    const running = harness({ id: 'run-1', status: 'running', progress: { stage: 'coarse', current: 2, total: 7 },
        baseline_measurement: { id: 'baseline' } }, {
        sleep: () => pause.promise,
        poll: async () => ({ ok: true, json: async () => ({ job: { id: 'run-1', status: 'cancelled' } }) }),
    });
    const recovery = running.flows.recoverAutoSubJob();
    await flush();
    assert.equal(running.state.autoSubJobId, 'run-1');
    assert.equal(running.state.autoSubInFlight, true);
    assert.match(running.status.textContent, /Coarse scan: 2\/7 sweeps/);
    assert.equal(running.button.textContent, 'Cancel Auto Sub');
    assert.equal(running.button.disabled, false);
    await running.flows.cancelAutoSubOptimize();
    assert.deepEqual(running.calls.find(call => call[0] === 'cancel'), ['cancel', 'run-1']);
    pause.resolve();
    await recovery;
    assert.equal(running.state.statusText, 'Auto Sub cancelled.');
    assert.equal(running.state.autoSubInFlight, false);
    assert.equal(running.state.autoSubJobId, '');
    assert.equal(running.status.textContent, 'Idle note');

    const completed = harness({ id: 'run-2', status: 'completed', result: {
        mode: 'stereo', original_alignment_ms: 0, applied_alignment_ms: 1.5,
        winner: { score_pct: 80 }, baseline_measurement: { id: 'base', traces: [{ points: [] }] },
    } });
    await completed.flows.recoverAutoSubJob();
    assert.equal(completed.state.autoSubResult.applied_alignment_ms, 1.5);
    assert.match(completed.state.statusText, /Auto Sub .*1\.50/);
    assert.equal(completed.state.autoSubMeasurements[0].id, 'base');
    assert.equal(completed.state.autoSubInFlight, false);
    assert.equal(completed.calls.some(call => call[0] === 'poll'), false);
    const completedToasts = completed.calls.filter(call => call[0] === 'toast').length;
    await completed.flows.recoverAutoSubJob();
    assert.equal(completed.calls.filter(call => call[0] === 'toast').length, completedToasts,
        'reconnect must not show a finished run twice');

    const missing = harness(null);
    await missing.flows.recoverAutoSubJob();
    assert.equal(missing.state.autoSubInFlight, false);
    assert.equal(missing.state.statusText, '');

    const race = deferred();
    const stale = harness(null);
    stale.flows.init({ api: { getCurrentAutoSubJob: () => race.promise } });
    const lookup = stale.flows.recoverAutoSubJob();
    stale.state.jobGeneration += 1;
    stale.state.activeMeasurementKind = 'single';
    stale.state.activeJobId = 'other';
    race.resolve({ ok: true, json: async () => ({ job: { id: 'old', status: 'running' } }) });
    await lookup;
    assert.equal(stale.state.autoSubJobId, '');
    assert.equal(stale.state.activeMeasurementKind, 'single');

    const pending = deferred();
    const duplicate = harness(null);
    let requests = 0;
    duplicate.flows.init({ api: { getCurrentAutoSubJob: () => { requests += 1; return pending.promise; } } });
    const first = duplicate.flows.recoverAutoSubJob();
    const second = duplicate.flows.recoverAutoSubJob();
    pending.resolve({ ok: true, json: async () => ({ job: null }) });
    await Promise.all([first, second]);
    assert.equal(requests, 1, 'concurrent panel-open and WebSocket-open share one discovery');
    console.log('PASS test_autosub_reconnect.js');
})().catch((error) => { console.error(error); process.exitCode = 1; });
