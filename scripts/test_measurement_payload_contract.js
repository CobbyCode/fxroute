#!/usr/bin/env node
'use strict';

// Contract: measurement start payloads carry consistent channel fields.
//
// - normalizeMeasurementInputChannelSelections (measurement_setup.js) clears the
//   reference channel when it equals the mic channel, so mic == reference
//   can never reach any start endpoint through the UI. All four start
//   paths (single, L/R repeat, hybrid, auto-sub) run it before reading
//   the channel selections.
// - buildHybridMeasurementForm (exported) emits a fixed field set;
//   the reference field follows the reference warning, calibration
//   follows the file-or-ref rule shared with the other paths.
//
// Both units are executed from their real modules.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const measurementSetup = require('../static/measurement_setup.js');

function formEntries(formData) {
    const entries = {};
    for (const [key, value] of formData.entries()) {
        if (typeof value !== 'string' || !(value instanceof Blob)) entries[key] = value;
        else entries[key] = '[blob]';
    }
    return entries;
}

// --- Part 1: the shared channel-normalization choke point ---------------
function runNormalize(state, channels) {
    const measurement = { ...state, inputs: channels == null ? [] : [{ id: 'test-input', channels }],
        selectedInputId: channels == null ? '' : 'test-input' };
    measurementSetup.init({ getState: () => ({ measurement }) });
    measurementSetup.normalizeMeasurementInputChannelSelections();
    return measurement;
}

let normalized = runNormalize(
    { selectedMicInputChannel: '2', selectedReferenceInputChannel: '2' }, 4
);
assert.strictEqual(
    normalized.selectedReferenceInputChannel, '',
    'mic == reference must be cleared before any payload is built'
);
assert.strictEqual(normalized.selectedMicInputChannel, '2');

normalized = runNormalize(
    { selectedMicInputChannel: '9', selectedReferenceInputChannel: '7' }, 4
);
assert.strictEqual(normalized.selectedMicInputChannel, '4', 'mic clamps to channel count');
assert.strictEqual(normalized.selectedReferenceInputChannel, '', 'out-of-range ref clears');

normalized = runNormalize(
    { selectedMicInputChannel: '1', selectedReferenceInputChannel: '2' }, 4
);
assert.strictEqual(normalized.selectedMicInputChannel, '1');
assert.strictEqual(normalized.selectedReferenceInputChannel, '2', 'distinct valid pair survives');

// A previously shared reference seeds both split fields on a multi-channel
// interface; two distinct L/R values stay distinct and keep L as the summary.
normalized = runNormalize(
    { selectedMicInputChannel: '1', selectedReferenceInputChannel: '2' }, 4
);
assert.strictEqual(normalized.selectedReferenceInputChannelLeft, '2', 'shared ref seeds Ref L');
assert.strictEqual(normalized.selectedReferenceInputChannelRight, '2', 'shared ref seeds Ref R');
assert.strictEqual(normalized.selectedReferenceInputChannel, '2', 'identical L/R keeps the shared summary');

normalized = runNormalize(
    { selectedMicInputChannel: '1', selectedReferenceInputChannelLeft: '2', selectedReferenceInputChannelRight: '3' }, 4
);
assert.strictEqual(normalized.selectedReferenceInputChannelLeft, '2');
assert.strictEqual(normalized.selectedReferenceInputChannelRight, '3');
assert.strictEqual(normalized.selectedReferenceInputChannel, '2', 'split keeps the left reference as the shared summary');

// The 2-channel path still collapses to one shared reference.
normalized = runNormalize(
    { selectedMicInputChannel: '1', selectedReferenceInputChannelLeft: '2', selectedReferenceInputChannelRight: '3' }, 2
);
assert.strictEqual(normalized.selectedReferenceInputChannelLeft, '', 'out-of-range Ref L clears on 2-channel');
assert.strictEqual(normalized.selectedReferenceInputChannelRight, '', 'out-of-range Ref R clears on 2-channel');
console.log('channel normalization choke point: ok');

// --- Part 2: hybrid builder field snapshot -------------------------------
function makeFlowsContext(state, warning, areaBank = '') {
    const ctx = {
        console, Math, JSON, Object, Array, Promise, String, Number, Boolean,
        setTimeout, clearTimeout, FormData,
        window: {},
        FXRouteMeasurementUI: {},
        FXRouteHybridMeasurement: {},
        hybridSpeakerName: () => 'speaker',
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(
        fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx
    );
    const MF = ctx.FXRouteMeasurementFlows;
    MF.init({
        getState: () => state,
        getElements: () => ({}),
        normalizeMeasurementInputChannelSelections: () => {},
        getMeasurementReferenceWarning: () => warning,
        measurementAreaBank: () => areaBank,
        api: {},
        sleep: async () => {},
        showToast: () => {},
        renderMeasurementPanel: () => {},
    });
    return MF;
}

const hybridState = {
    measurement: {
        selectedInputId: 'in1',
        selectedInputKey: 'k',
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '2',
    },
    settings: {},
};
const step = { channel: 'left', role: 'secondary' };

let MF = makeFlowsContext(hybridState, '');
let entries = formEntries(MF.buildHybridMeasurementForm(step));
assert.deepStrictEqual(entries, {
    input_id: 'in1',
    input_key: 'k',
    channel: 'left',
    measurement_role: 'secondary',
    mic_input_channel: '1',
    reference_input_channel: '2',
});
console.log('hybrid payload with reference: ok');

MF = makeFlowsContext(hybridState, 'Electrical reference disabled');
entries = formEntries(MF.buildHybridMeasurementForm(step));
assert.strictEqual(entries.reference_input_channel, '', 'warning gates the reference field');
assert.strictEqual(entries.mic_input_channel, '1');
assert.strictEqual(entries.measurement_role, 'secondary');
console.log('hybrid payload gated reference: ok');

// A selected area turns every advanced step into an internal way sweep of the
// same frozen area; without one the payload keeps its legacy field set.
MF = makeFlowsContext(hybridState, '', 'left_low');
entries = formEntries(MF.buildHybridMeasurementForm(step));
assert.strictEqual(entries.measurement_bank, 'left_low');
assert.strictEqual(entries.measurement_role, 'secondary');
assert.strictEqual(entries.channel, 'left');
console.log('hybrid payload selected area: ok');

// --- Part 3: per-side reference fields -----------------------------------
function runAppendReferenceFields(state, channelCount) {
    const measurement = { ...state, inputs: channelCount == null ? [] : [{ id: 'test-input', channels: channelCount }],
        selectedInputId: channelCount == null ? '' : 'test-input' };
    measurementSetup.init({ getState: () => ({ measurement }) });
    const formData = new FormData();
    measurementSetup.appendMeasurementReferenceFields(formData);
    return formEntries(formData);
}

let referenceEntries = runAppendReferenceFields(
    {
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '2',
        selectedReferenceInputChannelLeft: '2',
        selectedReferenceInputChannelRight: '3',
    },
    4,
);
assert.deepStrictEqual(referenceEntries, {
    reference_input_channel_left: '2',
    reference_input_channel_right: '3',
    reference_input_channel: '2',
});
console.log('split reference payload: ok');

referenceEntries = runAppendReferenceFields(
    {
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '2',
        selectedReferenceInputChannelLeft: '2',
        selectedReferenceInputChannelRight: '2',
    },
    2,
);
assert.deepStrictEqual(referenceEntries, { reference_input_channel: '2' });
console.log('2-channel reference payload: ok');

// Before the input topology arrives, a conflicting shared reference is gated
// without changing stored split settings or inventing split payload fields.
referenceEntries = runAppendReferenceFields(
    {
        selectedMicInputChannel: '2',
        selectedReferenceInputChannel: '2',
        selectedReferenceInputChannelLeft: '3',
        selectedReferenceInputChannelRight: '3',
    },
    null,
);
assert.strictEqual(referenceEntries.reference_input_channel, '', 'warning gates the shared field');
assert.strictEqual(referenceEntries.reference_input_channel_left, undefined);
assert.strictEqual(referenceEntries.reference_input_channel_right, undefined);
console.log('unknown-topology reference payload gated: ok');

// Exercise the app's actual wiring: isolated builder tests miss a dropped
// dependency and silently fall back to the shared reference for both sides.
{
    const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
    const wiring = appSource.match(/window\.FXRouteMeasurementFlows\?\.init\(\{[\s\S]*?\n\}\);/)[0];
    const state = { measurement: {
        inputs: [{ id: 'interface', channels: 18 }], selectedInputId: 'interface',
        selectedMicInputChannel: '1', selectedReferenceInputChannel: '7',
        selectedReferenceInputChannelLeft: '7', selectedReferenceInputChannelRight: '8',
    } };
    const ctx = { state, elements: {}, FormData, console,
        measurementAreaFromCatalog: () => ({ bank_id: 'global' }),
    };
    // Unrelated app callbacks are lazy; the real setup and flow modules own
    // all channel normalization and request serialization in this check.
    for (const match of wiring.matchAll(/^    (\w+),$/gm)) ctx[match[1]] = () => {};
    ctx.window = ctx;
    vm.createContext(ctx);
    for (const file of ['measurement_setup.js', 'hybrid_measurement.js', 'measurement_flows.js']) {
        vm.runInContext(fs.readFileSync(path.join(root, 'static', file), 'utf8'), ctx);
    }
    ctx.FXRouteMeasurementSetup.init({ getState: () => state });
    vm.runInContext(wiring, ctx);
    for (const mode of ['subwoofer-2.2', 'subwoofer-2.2-stereo']) {
        for (const step of ctx.FXRouteHybridMeasurement.buildSequence(mode).steps) {
            const form = ctx.FXRouteMeasurementFlows.buildHybridMeasurementForm(step);
            assert.equal(form.get('reference_input_channel_left'), '7', `${mode}/${step.id}: retain Ref L`);
            assert.equal(form.get('reference_input_channel_right'), '8', `${mode}/${step.id}: retain Ref R`);
            assert.equal(form.get('channel'), step.channel);
            assert.equal(form.get('measurement_bank'), 'global');
        }
    }
    state.measurement.inputs[0].channels = 2;
    state.measurement.selectedReferenceInputChannel = '2';
    const sharedForm = ctx.FXRouteMeasurementFlows.buildHybridMeasurementForm({ channel: 'right', role: 'mlp' });
    assert.equal(sharedForm.get('reference_input_channel'), '2', 'two-channel interfaces retain their shared reference');
    assert.equal(sharedForm.has('reference_input_channel_right'), false);
    console.log('app-wired Advanced reference payloads: ok');
}

console.log('measurement payload contracts: ok');
