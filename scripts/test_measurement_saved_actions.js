#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Saved actions keep payloads, guards and confirm/prompt flows.
const assert = require('node:assert/strict');
const saved = require('../static/measurement_saved_actions.js');

function makeSavedHarness(overrides = {}) {
    const state = {
        measurement: {
            currentMeasurement: { id: 'cur', name: 'Sweep', channel: 'left' },
            currentMeasurementName: 'Kept name',
            currentMeasurementSaved: false,
            autoSubMeasurements: [],
            pendingRepeatMeasurements: [],
            saveInFlight: false,
            startInFlight: false,
            statusText: '',
            visibilityById: {},
            reviewVisibilityById: {},
            measurements: [],
        },
    };
    const calls = [];
    const harness = { state, calls, fetchHandler: null, confirmResult: true, promptResult: 'Merged name' };
    saved.init({
        getState: () => state,
        fetch: async (url, options) => {
            calls.push(['fetch', url, options]);
            if (harness.fetchHandler) return harness.fetchHandler(url, options);
            return { ok: true, json: async () => ({}), text: async () => '' };
        },
        showToast: (message, kind) => calls.push(['toast', message, kind]),
        renderMeasurementPanel: () => calls.push('render'),
        fetchMeasurements: async () => calls.push('reload'),
        formatTransitionErrorDetail: (detail, fallback) => (typeof detail === 'string' ? detail : fallback),
        normalizeMeasurementEntry: (entry) => entry,
        getVisibleMeasurementEntries: () => (overrides.visible || []).map((m) => m),
        confirm: () => { calls.push('confirm'); return harness.confirmResult; },
        prompt: () => { calls.push('prompt'); return harness.promptResult; },
    });
    return harness;
}

async function main() {
    // 1. Save guards concurrent saves and clears the current entry.
    {
        const h = makeSavedHarness();
        h.state.measurement.saveInFlight = true;
        await saved.saveCurrentMeasurement();
        assert.ok(!h.calls.some((c) => c[0] === 'fetch'), 'concurrent save is gated');
        h.state.measurement.saveInFlight = false;
        h.fetchHandler = async () => ({ ok: true, json: async () => ({ measurement: { id: 'saved-1', name: 'Kept name' } }) });
        await saved.saveCurrentMeasurement();
        const post = h.calls.find((c) => c[0] === 'fetch');
        assert.equal(post[1], '/api/measurements/save');
        assert.equal(JSON.parse(post[2].body).name, 'Kept name');
        assert.equal(h.state.measurement.currentMeasurement, null);
        assert.equal(h.state.measurement.saveInFlight, false);
        assert.ok(h.calls.includes('reload'));
    }

    // 2. Repeat save renames L/R sides and drops the pending list.
    {
        const h = makeSavedHarness();
        h.state.measurement.pendingRepeatMeasurements = [
            { id: 'l', channel: 'left' }, { id: 'r', channel: 'right' },
        ];
        h.state.measurement.currentMeasurementName = 'Evening';
        h.fetchHandler = async (url, options) => {
            h.calls.push(['save-body', JSON.parse(options.body)]);
            return { ok: true, json: async () => ({ measurements: [{ id: 's1' }] }) };
        };
        await saved.saveCurrentMeasurement();
        const body = h.calls.find((c) => c[0] === 'save-body')[1];
        assert.deepEqual(body.measurements.map((m) => m.name), ['Evening · L', 'Evening · R']);
        assert.deepEqual(h.state.measurement.pendingRepeatMeasurements, []);
    }

    // 3. Single delete confirms, clears visibility and reloads.
    {
        const h = makeSavedHarness();
        h.state.measurement.visibilityById = { m1: true };
        h.state.measurement.reviewVisibilityById = { m1: true };
        await saved.deleteMeasurement('m1', 'Take');
        assert.ok(h.calls.includes('confirm'));
        assert.equal(h.calls.find((c) => c[0] === 'fetch')[1], '/api/measurements/m1');
        assert.equal(h.state.measurement.visibilityById.m1, undefined);
        assert.equal(h.state.measurement.saveInFlight, false);
        h.confirmResult = false;
        h.calls.length = 0;
        await saved.deleteMeasurement('m2', 'Take');
        assert.ok(!h.calls.some((c) => c[0] === 'fetch'), 'dismissed confirm never POSTs');
    }

    // 4. Merge needs two selections, prompts once and toasts the result.
    {
        const h = makeSavedHarness({ visible: [{ id: 'a', name: 'A' }] });
        await saved.mergeSelectedMeasurements();
        assert.ok(h.calls.some((c) => c[0] === 'toast' && /at least two/.test(c[1])), 'single selection explains the merge gate');
        const h2 = makeSavedHarness({ visible: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }] });
        h2.fetchHandler = async () => ({ ok: true, text: async () => JSON.stringify({ measurement: { id: 'm', name: 'Merged name' } }) });
        await saved.mergeSelectedMeasurements();
        const post = h2.calls.find((c) => c[0] === 'fetch');
        assert.equal(post[1], '/api/measurements/merge');
        assert.deepEqual(JSON.parse(post[2].body).measurementIds, ['a', 'b']);
        assert.ok(h2.calls.some((c) => c[0] === 'toast' && /Merged name/.test(c[1])));
        h2.promptResult = null;
        h2.calls.length = 0;
        await saved.mergeSelectedMeasurements();
        assert.ok(!h2.calls.some((c) => c[0] === 'fetch'), 'dismissed prompt never POSTs');
    }

    console.log('measurement saved actions: ok');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
