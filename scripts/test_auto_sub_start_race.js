#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}

const turn = () => new Promise((resolve) => setImmediate(resolve));
const response = (job, status = 200) => ({
    ok: status === 200, status, json: async () => ({ job, detail: 'Another measurement is running' }),
});

function harness(preflush = async () => {}) {
    const state = { measurement: {
        selectedInputId: 'capture-1', selectedInputKey: 'capture-key',
        selectedMicInputChannel: '1', autoSubInFlight: false, startInFlight: false,
        autoSubJobId: '', activeJobId: '', activeMeasurementKind: '',
        autoSubCancelRequested: false, autoSubResult: null, autoSubMeasurements: [],
        jobGeneration: 1,
    }, settings: {} };
    const sleep = deferred();
    const events = { starts: [], cancels: [], polls: [], toasts: [], errors: [], flushes: 0 };
    const context = { FormData, console: {
        error: (...args) => events.errors.push(args), warn: () => {},
    } };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/measurement_flows.js'), 'utf8'), context);
    const flows = context.FXRouteMeasurementFlows;
    flows.init({
        getState: () => state,
        measurementModeReady: () => true,
        flushSubwooferSettingsBeforeMeasurement: () => { events.flushes++; return preflush(); },
        sleep: () => sleep.promise,
        showToast: (...args) => events.toasts.push(args),
        formatTransitionErrorDetail: (detail) => String(detail),
        api: {
            startAutoSubOptimize: async (form) => {
                events.starts.push(Object.fromEntries(form.entries()));
                // Model the real backend guard: no second physical run, but
                // a second rejected request must not erase the first's owner.
                return events.starts.length === 1 ? response({ id: 'job-A', status: 'running' })
                    : response(null, 409);
            },
            pollAutoSubJob: async (id) => {
                events.polls.push(id);
                return response({ id, status: 'cancelled' });
            },
            cancelAutoSubJob: async (id) => { events.cancels.push(id); return response({ id }); },
        },
    });
    return { flows, state: state.measurement, events, sleep };
}

test('start claims ownership synchronously before a deferred preflush', async () => {
    const flush = deferred();
    const h = harness(() => flush.promise);
    const run = h.flows.startAutoSubOptimize();
    try {
        assert.equal(h.state.autoSubInFlight, true);
        assert.equal(h.state.startInFlight, true);
        assert.equal(h.state.activeMeasurementKind, 'auto_sub');
        assert.equal(h.events.starts.length, 0, 'must still wait for the settings commit');
    } finally {
        flush.resolve(); h.sleep.resolve(); await run;
    }
});

for (const immediate of [false, true]) {
    test(`parallel starts keep one job and its poll owner (${immediate ? 'fulfilled' : 'deferred'} preflush)`, async () => {
        const flush = deferred();
        const h = harness(() => immediate ? Promise.resolve() : flush.promise);
        const first = h.flows.startAutoSubOptimize();
        const second = h.flows.startAutoSubOptimize();
        flush.resolve();
        try {
            await turn();
            assert.equal(h.events.starts.length, 1, 'duplicate start must never reach the API');
            assert.equal(h.events.flushes, 1);
            assert.equal(h.events.starts[0].input_id, 'capture-1');
            assert.equal(h.state.autoSubJobId, 'job-A');
            assert.equal(h.state.autoSubInFlight, true);
            assert.equal(h.state.startInFlight, true);
            assert.equal(h.state.activeMeasurementKind, 'auto_sub');
            assert.deepEqual(h.events.toasts, [], 'rejected duplicate must not overwrite the first run');
            h.sleep.resolve();
            await Promise.all([first, second]);
            assert.deepEqual(h.events.polls, ['job-A'], 'the original job must remain polled');
            assert.equal(h.state.statusText, 'Auto Sub cancelled.');
        } finally {
            h.sleep.resolve(); await Promise.allSettled([first, second]);
        }
    });
}

test('preflush rejection is reported, releases ownership and allows a new start', async () => {
    let attempt = 0;
    const h = harness(async () => {
        if (++attempt === 1) throw new Error('settings commit failed');
    });
    await assert.doesNotReject(h.flows.startAutoSubOptimize());
    assert.equal(h.events.starts.length, 0);
    assert.match(h.state.statusText, /Auto Sub failed: settings commit failed/);
    assert.deepEqual(h.events.toasts, [['Auto Sub failed', 'error']]);
    assert.equal(h.state.autoSubInFlight, false);
    assert.equal(h.state.startInFlight, false);
    assert.equal(h.state.autoSubJobId, '');
    assert.equal(h.state.activeMeasurementKind, '');
    assert.equal(h.state.progressKind, '');
    h.sleep.resolve();
    await h.flows.startAutoSubOptimize();
    assert.equal(h.events.starts.length, 1, 'a failed settings commit must not permanently block retry');
    assert.deepEqual(h.events.polls, ['job-A']);
});

test('cancel during preflush is retained until the accepted job can be cancelled', async () => {
    const flush = deferred();
    const h = harness(() => flush.promise);
    const run = h.flows.startAutoSubOptimize();
    await h.flows.cancelAutoSubOptimize();
    flush.resolve(); h.sleep.resolve(); await run;
    assert.deepEqual(h.events.cancels, ['job-A']);
    assert.deepEqual(h.events.polls, ['job-A']);
    assert.equal(h.state.autoSubCancelRequested, false);
    assert.equal(h.state.autoSubInFlight, false);
});

test('already-owned measurement starts do not alter existing run state', async () => {
    for (const busy of [{ autoSubInFlight: true }, { startInFlight: true }, { activeJobId: 'sweep-B' }]) {
        const h = harness();
        Object.assign(h.state, busy, { statusText: 'existing result', autoSubResult: { winner: 42 } });
        const before = JSON.stringify(h.state);
        await h.flows.startAutoSubOptimize();
        assert.equal(JSON.stringify(h.state), before);
        assert.equal(h.events.flushes, 0);
        assert.equal(h.events.starts.length, 0);
    }
});

test('missing capture input still rejects without claiming a start', async () => {
    const h = harness();
    h.state.selectedInputId = '';
    await h.flows.startAutoSubOptimize();
    assert.equal(h.events.flushes, 0);
    assert.equal(h.events.starts.length, 0);
    assert.equal(h.state.autoSubInFlight, false);
    assert.equal(h.state.startInFlight, false);
    assert.deepEqual(h.events.toasts, [['No capture input', 'error']]);
});
