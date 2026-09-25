#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Speaker Align run lifecycle in the UI: a finished run hands Before/After to
// the normal measurement flow, no stale Verified table after a failed or
// cancelled run, and a backend cancel when polling gives up.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function element() {
    const classes = new Set();
    return { disabled: false, textContent: '', innerHTML: '', classList: {
        add: c => classes.add(c), remove: c => classes.delete(c),
        toggle: (c, on) => on ? classes.add(c) : classes.delete(c), contains: c => classes.has(c),
    } };
}

function fixture() {
    const roles = ['left_low', 'left_high', 'right_low', 'right_high'];
    const catalog = { active_mode: 'stereo', modes: { stereo: {
        crossover_enabled: true, selected_bank: 'global',
        topology: { way_count: 2, issues: [], roles, left_ways: roles.slice(0, 2), right_ways: roles.slice(2) },
        processing: Object.fromEntries(roles.map(role => [role, {
            highpass: role.endsWith('low') ? null : { frequency_hz: 2000 },
            lowpass: role.endsWith('high') ? null : { frequency_hz: 2000 },
        }])),
    } } };
    const state = { outputSystem: { catalog }, measurement: {
        selectedInputId: 'mic', selectedMicInputChannel: '1', selectedReferenceInputChannel: '2',
    } };
    const elements = Object.fromEntries(['LeftBtn', 'RightBtn', 'CancelBtn', 'Group', 'Status', 'Results']
        .map(key => [`measurementSpeakerAlign${key}`, element()]));
    const toasts = [];
    const context = { console: { ...console, warn() {}, error() {} }, window: {} };
    vm.createContext(context);
    const shell = fs.readFileSync(require.resolve('../static/index.html'), 'utf8');
    for (const match of shell.matchAll(/src="\/static\/(speaker_align|measurement_flows)\.js\?[^"]+"/g)) {
        vm.runInContext(fs.readFileSync(require.resolve(`../static/${match[1]}.js`), 'utf8'), context);
    }
    const flows = context.window.FXRouteMeasurementFlows;
    flows.init({ getState: () => state, getElements: () => elements,
        measurementModeReady: () => true,
        getActiveMeasurementKind: () => state.measurement.activeMeasurementKind,
        hasActiveMeasurementJob: () => !!state.measurement.speakerAlignInFlight,
        showToast: (message, kind) => toasts.push([message, kind]),
        renderMeasurementPanel: () => flows.syncSpeakerAlignButton(),
    });
    return { state, elements, flows, toasts };
}

const response = job => ({ ok: true, json: async () => ({ job }) });
const take = name => ({ id: `sweep-${name}`, name, channel: 'left', speaker_align_take: { side: 'left', take: name },
    traces: [{ kind: 'sweep-response', points: [[20, -3], [20000, -6]] }] });
const verified = { side: 'left', confirmed: true, committed_revision: 8, sample_rate_hz: 48000,
    measurements: { before: take('before'), after: take('after') },
    proposal: { start_revision: 7, processing_fingerprint: 'plan',
        arrival_ms: { left_low: 2, left_high: 5 }, added_delay_ms: { left_low: 3, left_high: 0 },
        added_gain_db: { left_low: 0, left_high: 0 }, reference_role: 'left_high' },
    check: { confirmed: true, reasons: [], warnings: [], before_spread_ms: 3, max_residual_ms: 0.021,
        tolerance_ms: 0.25, after_arrival_ms: { left_low: 5, left_high: 5.021 }, pairs: [] } };

function runApi(outcome) {
    return {
        startSpeakerAlign: async payload => response({ id: `${outcome.status}-run`, side: payload.side, status: 'queued' }),
        pollSpeakerAlignJob: async jobId => response({ id: jobId, side: 'left', ...outcome }),
    };
}

async function finishedRunJoinsTheNormalFlow() {
    const { state, elements, flows } = fixture();
    flows.init({ api: runApi({ status: 'committed', result: verified }) });
    await flows.startSpeakerAlign('left');
    assert.equal(state.measurement.speakerAlignInFlight, false);
    assert.equal(state.measurement.startInFlight, false, 'normal Save current is no longer blocked');
    assert.ok(state.measurement.speakerAlignResult?.proposal, 'result is kept');
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /Verified · committed rev 8/);
    assert.deepEqual(Array.from(state.measurement.pendingRepeatMeasurements, item => item.id),
        ['sweep-before', 'sweep-after'], 'Before and After are the pending pair');
    assert.equal(state.measurement.currentMeasurementSaved, false);
    assert.equal(state.measurement.measurementView, 'ir');
}

async function failedOrCancelledRunClearsTheResults() {
    for (const outcome of [
        { status: 'failed', error: 'Selected electrical reference was not captured',
            message: 'Speaker alignment failed: Selected electrical reference was not captured' },
        { status: 'cancelled', message: 'Speaker alignment cancelled.' },
    ]) {
        const { state, elements, flows } = fixture();
        flows.init({ api: runApi({ status: 'committed', result: verified }) });
        await flows.startSpeakerAlign('left');
        assert.match(elements.measurementSpeakerAlignResults.innerHTML, /Verified/);
        flows.init({ api: runApi(outcome) });
        await flows.startSpeakerAlign('left');
        assert.equal(elements.measurementSpeakerAlignResults.innerHTML, '', `${outcome.status} clears the table`);
        assert.equal(state.measurement.pendingRepeatMeasurements.length, 2,
            `${outcome.status} keeps the earlier unsaved Before/After`);
        assert.equal(state.measurement.speakerAlignResult, null);
        assert.equal(state.measurement.speakerAlignResults, null);
        assert.equal(elements.measurementSpeakerAlignLeftBtn.disabled, false);
        assert.equal(state.measurement.statusText, outcome.status === 'failed'
            ? 'Speaker Align Left failed: Selected electrical reference was not captured'
            : 'Speaker Align Left cancelled.');
    }
}

// Progress belongs to the feature line while the run is live; the panel
// status line stays empty until the outcome, and the two never carry the
// same text.
async function progressAndOutcomeStayApart() {
    const { state, elements, flows, toasts } = fixture();
    const seen = [];
    let polls = 0;
    flows.init({ api: {
        startSpeakerAlign: async payload => response({ id: 'run', side: payload.side, status: 'queued' }),
        pollSpeakerAlignJob: async jobId => {
            seen.push([elements.measurementSpeakerAlignStatus.textContent, state.measurement.statusText]);
            polls++;
            return polls === 1
                ? response({ id: jobId, side: 'left', status: 'acquiring', message: 'Measuring left low (1/2)…' })
                : response({ id: jobId, side: 'left', status: 'committed', result: verified });
        },
    } });
    await flows.startSpeakerAlign('left');
    assert.deepEqual(seen, [['Starting…', ''], ['Measuring left low (1/2)…', '']]);
    assert.equal(elements.measurementSpeakerAlignStatus.textContent, '', 'feature line is idle again');
    assert.equal(state.measurement.statusText, 'Speaker Align Left verified and applied.');
    assert.deepEqual(toasts.at(-1), ['Speaker Align Left applied', 'success']);
}

async function abandonedPollingCancelsTheBackendJob() {
    const { state, elements, flows, toasts } = fixture();
    let polls = 0;
    const cancelled = [];
    flows.init({ api: {
        startSpeakerAlign: async () => response({ id: 'still-running', side: 'left', status: 'queued' }),
        pollSpeakerAlignJob: async () => { polls++; throw new TypeError('Failed to fetch'); },
        cancelSpeakerAlignJob: async jobId => {
            cancelled.push(jobId);
            return response({ id: jobId, status: 'cancelling' });
        },
    } });
    await flows.startSpeakerAlign('left');
    assert.equal(polls, 40);
    assert.deepEqual(cancelled, ['still-running'], 'giving up polling cancels the backend job');
    assert.equal(state.measurement.statusText, 'Speaker Align Left interrupted: Failed to fetch. The run was cancelled.');
    assert.equal(toasts.at(-1)[1], 'error');
    assert.equal(state.measurement.speakerAlignJobId, '');
    assert.equal(elements.measurementSpeakerAlignLeftBtn.disabled, false);

    const second = fixture();
    second.flows.init({ api: {
        startSpeakerAlign: async () => response({ id: 'unreachable', side: 'left', status: 'queued' }),
        pollSpeakerAlignJob: async () => { throw new TypeError('Failed to fetch'); },
        cancelSpeakerAlignJob: async () => { throw new TypeError('Failed to fetch'); },
    } });
    await second.flows.startSpeakerAlign('left');
    assert.match(second.state.measurement.statusText, /Cancelling the run failed\./,
        'an unreachable backend is reported, not hidden');
}

async function main() {
    await finishedRunJoinsTheNormalFlow();
    await failedOrCancelledRunClearsTheResults();
    await progressAndOutcomeStayApart();
    await abandonedPollingCancelsTheBackendJob();
    console.log('Speaker Align run lifecycle UI: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
