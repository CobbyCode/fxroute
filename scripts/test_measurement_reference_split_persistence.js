#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Contract: the persisted measurement settings are authoritative, so the
// response order of /api/measurements (which carries measurement_settings) and
// /api/measurements/inputs (which carries the real channel count) must not
// change the resulting state.
//
// Regression: /api/measurements resolved first, so
// normalizeMeasurementInputChannelSelections() ran while the input topology was
// still unknown. It treated "unknown" like "2-channel" and collapsed the stored
// split reference onto the shared value, i.e. Ref L=3 / Ref R=4 became 3/3 and
// stayed that way for the rest of the session — a UI-started L/R repeat then
// sent reference_input_channel_right=3, so the right sweep used the left
// reference.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const repoRoot = path.join(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(appSource);
    assert.ok(match, `missing function ${name}`);
    let parenDepth = 1;
    let braceStart = -1;
    for (let index = match.index + match[0].length; index < appSource.length; index += 1) {
        if (appSource[index] === '(') parenDepth += 1;
        if (appSource[index] === ')') parenDepth -= 1;
        if (parenDepth === 0) {
            braceStart = appSource.indexOf('{', index);
            break;
        }
    }
    assert.notEqual(braceStart, -1, `missing function body ${name}`);
    let depth = 0;
    let quote = '';
    let escaped = false;
    for (let index = braceStart; index < appSource.length; index += 1) {
        const char = appSource[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (char === "'" || char === '"' || char === '`') quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return appSource.slice(match.index, index + 1);
    }
    throw new Error(`unterminated function ${name}`);
}

const EXTRACTED = [
    'getSelectedMeasurementInput',
    'getSelectedMeasurementInputChannelCount',
    'normalizeMeasurementInputChannelSelections',
    'getMeasurementReferenceWarning',
    'appendMeasurementReferenceFields',
    'applyMeasurementSetupSettings',
];

// Focusrite-like multichannel capture input: 18 channels, mic on 1, refs 3/4.
const MULTICHANNEL_INPUT = (id) => ({
    id,
    label: 'Scarlett 16i16',
    channels: 18,
    persistentId: 'device-serial:Focusrite_Scarlett_16i16',
});
const STEREO_INPUT = (id) => ({
    id,
    label: 'Stereo interface',
    channels: 2,
    persistentId: 'device-serial:Stereo-2ch',
});

function makeContext() {
    const state = {
        measurement: {
            inputs: [],
            selectedInputId: '',
            selectedMicInputChannel: '1',
            selectedReferenceInputChannel: '',
            selectedReferenceInputChannelLeft: '',
            selectedReferenceInputChannelRight: '',
        },
    };
    const context = { state, console, Math, Number, Object, String, Boolean, FormData };
    vm.createContext(context);
    vm.runInContext(EXTRACTED.map(extractFunction).join('\n'), context);
    return context;
}

// The persisted payload as the backend returns it for a wired 3/4 setup.
const splitSettings = () => ({
    selectedMicInputChannel: '1',
    selectedReferenceInputChannel: '3',
    selectedReferenceInputChannelLeft: '3',
    selectedReferenceInputChannelRight: '4',
    measurementSampleRate: 48000,
});

const referenceTriple = (context) => {
    const m = context.state.measurement;
    return [m.selectedReferenceInputChannel, m.selectedReferenceInputChannelLeft, m.selectedReferenceInputChannelRight];
};

function measurementReferencePayload(context) {
    const formData = new FormData();
    context.appendMeasurementReferenceFields(formData);
    return Object.fromEntries(formData.entries());
}

function assertSplitKept(context, label) {
    assert.deepEqual(
        referenceTriple(context),
        ['3', '3', '4'],
        `${label}: shared/left/right must stay 3/3/4`
    );
    const payload = measurementReferencePayload(context);
    assert.equal(payload.reference_input_channel_left, '3', `${label}: payload must carry Ref L=3`);
    assert.equal(
        payload.reference_input_channel_right,
        '4',
        `${label}: payload must carry Ref R=4 (right sweep must not reuse the left reference)`
    );
    assert.equal(context.getMeasurementReferenceWarning(), '', `${label}: no reference warning expected`);
}

// 1. The faulty order: settings first, input topology later. This is the
//    regression — before the fix this collapsed to 3/3.
{
    const context = makeContext();
    context.applyMeasurementSetupSettings(splitSettings());
    assert.deepEqual(
        referenceTriple(context),
        ['3', '3', '4'],
        'settings applied before inputs must keep the persisted split reference'
    );

    // /api/measurements/inputs resolves afterwards: normalize against the real
    // channel count now that the topology is known.
    context.state.measurement.inputs = [MULTICHANNEL_INPUT('pw-source-166')];
    context.state.measurement.selectedInputId = 'pw-source-166';
    context.normalizeMeasurementInputChannelSelections();
    assertSplitKept(context, 'settings-first order');
}

// 2. The reverse order must produce the identical state (order independence).
{
    const context = makeContext();
    context.state.measurement.inputs = [MULTICHANNEL_INPUT('pw-source-166')];
    context.state.measurement.selectedInputId = 'pw-source-166';
    context.normalizeMeasurementInputChannelSelections();

    context.applyMeasurementSetupSettings(splitSettings());
    assertSplitKept(context, 'inputs-first order');
}

// 3. A partial save response (only the changed field) must not reshape the pair.
{
    const context = makeContext();
    context.state.measurement.inputs = [MULTICHANNEL_INPUT('pw-source-166')];
    context.state.measurement.selectedInputId = 'pw-source-166';
    context.applyMeasurementSetupSettings(splitSettings());
    context.applyMeasurementSetupSettings({ selectedReferenceInputChannelRight: '5' }, new Set(['selectedReferenceInputChannelRight']));
    assert.deepEqual(referenceTriple(context), ['3', '3', '5'], 'single-field response must keep Ref L=3 and apply Ref R=5');
}

// 4. Unknown topology must not clamp the mic channel either (same defect class):
//    a persisted mic on channel 3 survives until the count is known.
{
    const context = makeContext();
    context.applyMeasurementSetupSettings({ selectedMicInputChannel: '3' });
    assert.equal(context.state.measurement.selectedMicInputChannel, '3', 'mic must not be clamped while the topology is unknown');
    context.state.measurement.inputs = [MULTICHANNEL_INPUT('pw-source-166')];
    context.state.measurement.selectedInputId = 'pw-source-166';
    context.normalizeMeasurementInputChannelSelections();
    assert.equal(context.state.measurement.selectedMicInputChannel, '3', 'mic stays valid on an 18-channel input');
}

// 5. Unknown topology still validates values, and out-of-range values are
//    dropped as soon as the real channel count is known.
{
    const context = makeContext();
    context.applyMeasurementSetupSettings({
        selectedReferenceInputChannelLeft: 'abc',
        selectedReferenceInputChannelRight: '99',
        selectedReferenceInputChannel: '3',
    });
    assert.deepEqual(
        referenceTriple(context),
        ['3', '', '99'],
        'unknown topology drops non-numeric values but keeps in-range ones unclamped'
    );
    context.state.measurement.inputs = [MULTICHANNEL_INPUT('pw-source-166')];
    context.state.measurement.selectedInputId = 'pw-source-166';
    context.normalizeMeasurementInputChannelSelections();
    assert.deepEqual(
        referenceTriple(context),
        ['3', '3', '3'],
        'the out-of-range 99 clears; with no split value left the shared reference seeds both sides'
    );
}

// 6. Interfaces without split references keep their behavior: a 2-channel
//    interface still uses one shared reference, seeded from either shape.
{
    const context = makeContext();
    context.state.measurement.inputs = [STEREO_INPUT('pw-source-65')];
    context.state.measurement.selectedInputId = 'pw-source-65';
    context.applyMeasurementSetupSettings({ selectedMicInputChannel: '1', selectedReferenceInputChannel: '2' });
    assert.deepEqual(referenceTriple(context), ['2', '2', '2'], '2-channel interface keeps one shared reference');

    context.applyMeasurementSetupSettings({
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '',
        selectedReferenceInputChannelLeft: '',
        selectedReferenceInputChannelRight: '',
    });
    assert.deepEqual(referenceTriple(context), ['', '', ''], '2-channel interface keeps a cleared reference cleared');
    const payload = measurementReferencePayload(context);
    assert.equal(
        payload.reference_input_channel_right,
        undefined,
        'a 2-channel interface must not send split reference fields'
    );
}

// 7. Mic == reference is still cleared per side on a split interface, and the
//    surviving side keeps its own channel.
{
    const context = makeContext();
    context.state.measurement.inputs = [MULTICHANNEL_INPUT('pw-source-166')];
    context.state.measurement.selectedInputId = 'pw-source-166';
    context.applyMeasurementSetupSettings({
        selectedMicInputChannel: '1',
        selectedReferenceInputChannelLeft: '1',
        selectedReferenceInputChannelRight: '4',
    });
    assert.deepEqual(referenceTriple(context), ['4', '', '4'], 'Ref L equals the mic channel and must clear');
    assert.equal(measurementReferencePayload(context).reference_input_channel_left, '', 'cleared side is sent empty');
    assert.equal(measurementReferencePayload(context).reference_input_channel_right, '4', 'other side is unaffected');
}

console.log('measurement reference split persistence: ok');
