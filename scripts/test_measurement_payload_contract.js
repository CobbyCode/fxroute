#!/usr/bin/env node
'use strict';

// Contract: measurement start payloads carry consistent channel fields.
//
// - normalizeMeasurementInputChannelSelections (app.js) clears the
//   reference channel when it equals the mic channel, so mic == reference
//   can never reach any start endpoint through the UI. All four start
//   paths (single, L/R repeat, hybrid, auto-sub) run it before reading
//   the channel selections.
// - buildHybridMeasurementForm (exported) emits a fixed field set;
//   the reference field follows the reference warning, calibration
//   follows the file-or-ref rule shared with the other paths.
//
// Both units are executed from their real sources (balanced-brace
// extraction for the app.js global, vm-loaded flows module).

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');

function extractGlobalFunction(source, name) {
    const marker = `\nfunction ${name}(`;
    const start = source.indexOf(marker);
    assert.notStrictEqual(start, -1, `${name} must exist in static/app.js`);
    let i = source.indexOf('(', start);
    let parens = 0;
    let quote = null;
    for (; i < source.length; i++) {
        const ch = source[i];
        if (quote) {
            if (ch === '\\') i++;
            else if (ch === quote) quote = null;
            continue;
        }
        if (ch === '"' || ch === "'" || ch === '`') quote = ch;
        else if (ch === '(') parens++;
        else if (ch === ')') {
            parens--;
            if (parens === 0) break;
        }
    }
    i = source.indexOf('{', i);
    let depth = 0;
    quote = null;
    let lineComment = false;
    let blockComment = false;
    for (; i < source.length; i++) {
        const ch = source[i];
        const next = source[i + 1];
        if (lineComment) {
            if (ch === '\n') lineComment = false;
            continue;
        }
        if (blockComment) {
            if (ch === '*' && next === '/') {
                blockComment = false;
                i++;
            }
            continue;
        }
        if (quote) {
            if (ch === '\\') i++;
            else if (ch === quote) quote = null;
            continue;
        }
        if (ch === '/' && next === '/') { lineComment = true; i++; continue; }
        if (ch === '/' && next === '*') { blockComment = true; i++; continue; }
        if (ch === '"' || ch === "'" || ch === '`') { quote = ch; continue; }
        if (ch === '{') depth++;
        if (ch === '}') {
            depth--;
            if (depth === 0) return source.slice(start + 1, i + 1);
        }
    }
    throw new Error(`unbalanced braces in ${name}`);
}

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
    const sandbox = {
        state: { measurement: state },
        getSelectedMeasurementInput: () => (channels == null ? null : { channels }),
    };
    vm.createContext(sandbox);
    vm.runInContext(
        extractGlobalFunction(appSource, 'normalizeMeasurementInputChannelSelections'),
        sandbox
    );
    vm.runInContext('normalizeMeasurementInputChannelSelections()', sandbox);
    return sandbox.state.measurement;
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
function makeFlowsContext(state, warning) {
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

// --- Part 3: per-side reference fields -----------------------------------
function runAppendReferenceFields(state, channelCount, warning) {
    const sandbox = {
        state: { measurement: state },
        FormData,
        normalizeMeasurementInputChannelSelections: () => {},
        getSelectedMeasurementInputChannelCount: () => channelCount,
        getMeasurementReferenceWarning: () => warning,
    };
    vm.createContext(sandbox);
    vm.runInContext(
        extractGlobalFunction(appSource, 'appendMeasurementReferenceFields'),
        sandbox
    );
    sandbox.fd = new FormData();
    vm.runInContext('appendMeasurementReferenceFields(fd)', sandbox);
    return formEntries(sandbox.fd);
}

let referenceEntries = runAppendReferenceFields(
    {
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '2',
        selectedReferenceInputChannelLeft: '2',
        selectedReferenceInputChannelRight: '3',
    },
    4,
    ''
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
    ''
);
assert.deepStrictEqual(referenceEntries, { reference_input_channel: '2' });
console.log('2-channel reference payload: ok');

// The warning is an injected dependency here; the split L/R fields must still
// go out unchanged while the shared field is gated. app.js only ever produces
// the shared-reference message (a split conflict is cleared beforehand).
referenceEntries = runAppendReferenceFields(
    {
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '2',
        selectedReferenceInputChannelLeft: '2',
        selectedReferenceInputChannelRight: '3',
    },
    4,
    'Electrical reference disabled'
);
assert.strictEqual(referenceEntries.reference_input_channel, '', 'warning gates the shared field');
assert.strictEqual(referenceEntries.reference_input_channel_left, '2');
assert.strictEqual(referenceEntries.reference_input_channel_right, '3');
console.log('split reference payload gated: ok');

console.log('measurement payload contracts: ok');
