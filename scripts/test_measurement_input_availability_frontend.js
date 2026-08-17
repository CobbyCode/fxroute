#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
//
// Regression tests for the measurement capture-input availability warning:
//
// * persisted input missing        -> warning shown
// * present input deliberately     -> warning gone, no latched missing-state
//   selected from the dropdown        survives (incl. stale in-flight scans)
// * switching between two present  -> no warning, newer selection wins over
//   devices                           stale fetch snapshots
// * previously missing device      -> state reconciled when re-discovered
// * close/open of the setup        -> status line derives from current truth

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');

const WARNING_TEXT = 'The selected measurement microphone is currently unavailable. Reconnect it or deliberately select another input.';

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

const UMIK = (id) => ({
    id,
    label: 'UMIK-1',
    channels: 1,
    supportedRates: [48000],
    measurementSampleRate: 48000,
    persistentId: 'device-serial:miniDSP-UMIK-123',
});
const OTHER = (id) => ({
    id,
    label: 'Other mic',
    channels: 2,
    supportedRates: [48000],
    measurementSampleRate: 48000,
    persistentId: 'device-serial:OTHER-456',
});

function makeContext() {
    const fetchCalls = [];
    const inputsResolvers = [];
    const state = {
        measurement: {
            inputsLoading: false,
            startInFlight: false,
            saveInFlight: false,
            activeJobId: '',
            inputs: [],
            selectedInputId: '',
            selectedInputLegacyId: '',
            selectedInputKey: '',
            selectedInputConfigured: false,
            selectedInputUnavailable: false,
            selectedMicInputChannel: '1',
            selectedReferenceInputChannel: '',
            measurementSampleRate: '48000',
            hostCaptureAvailable: false,
            captureAvailable: false,
            statusText: 'Sweep ready. Calibration file is optional.',
        },
    };
    const context = {
        console,
        state,
        setImmediate,
        renderMeasurementPanel: () => {},
        fetch: (url, options = {}) => {
            fetchCalls.push(url);
            if (url === '/api/measurements/settings') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'ok',
                        measurement_settings: {
                            selectedInputId: state.measurement.selectedInputId,
                            selectedInputKey: state.measurement.selectedInputKey,
                            selectedInputConfigured: !!state.measurement.selectedInputKey,
                            selectedMicInputChannel: state.measurement.selectedMicInputChannel || '1',
                            selectedReferenceInputChannel: state.measurement.selectedReferenceInputChannel || '',
                            measurementSampleRate: 48000,
                        },
                    }),
                });
            }
            return new Promise((resolve) => inputsResolvers.push({ url, resolve }));
        },
    };
    vm.createContext(context);
    vm.runInContext(`
        let measurementSettingsRevision = 0;
        function getSelectedMeasurementInput() {
            const measurementState = state.measurement || {};
            return (measurementState.inputs || []).find(input => input.id === measurementState.selectedInputId) || null;
        }
        function normalizeMeasurementInputChannelSelections() {}
        ${extractFunction('describeMeasurementScope')}
        ${extractFunction('measurementModeNoteText')}
        ${extractFunction('measurementInputAvailabilityMessage')}
        ${extractFunction('measurementSetupStatusText')}
        ${extractFunction('applyMeasurementSetupSettings')}
        ${extractFunction('saveMeasurementSetupSettings')}
        ${extractFunction('applyMeasurementInputSelection')}
        ${extractFunction('fetchMeasurementInputs')}
        this.getRevision = () => measurementSettingsRevision;
    `, context);
    return { context, fetchCalls, inputsResolvers, state };
}

function resolveInputsFetch(inputsResolvers, payload) {
    assert.equal(inputsResolvers.length, 1, 'expected exactly one pending inputs fetch');
    inputsResolvers.shift().resolve({
        ok: true,
        json: async () => payload,
    });
}

const inputsPayload = (inputs, selection, captureAvailable = true) => ({
    status: 'ok',
    scope_note: 'host-local',
    inputs,
    selection,
    capture_available: captureAvailable,
});

async function main() {
    // 1. Persisted device missing -> warning shown (derived, not latched).
    {
        const { context, inputsResolvers, state } = makeContext();
        const pending = context.fetchMeasurementInputs();
        resolveInputsFetch(inputsResolvers, inputsPayload(
            [],
            { configured: true, unavailable: true, input_id: '', persistent_id: 'device-serial:miniDSP-UMIK-123' },
            false,
        ));
        await pending;
        assert.equal(state.measurement.selectedInputUnavailable, true);
        assert.equal(context.measurementInputAvailabilityMessage(), WARNING_TEXT);
        assert.equal(context.measurementSetupStatusText(), WARNING_TEXT);
        assert.doesNotMatch(state.measurement.statusText, /currently unavailable/,
            'warning must be derived at display time, not latched into statusText');
    }

    // 2. Deliberately selecting a present input clears the warning at once.
    {
        const { context, inputsResolvers, state } = makeContext();
        const pending = context.fetchMeasurementInputs();
        resolveInputsFetch(inputsResolvers, inputsPayload(
            [],
            { configured: true, unavailable: true, input_id: '', persistent_id: 'device-serial:miniDSP-UMIK-123' },
            false,
        ));
        await pending;
        assert.equal(state.measurement.selectedInputUnavailable, true);

        state.measurement.inputs = [UMIK('pw-source-65')];
        state.measurement.hostCaptureAvailable = true;
        context.applyMeasurementInputSelection('pw-source-65');
        assert.equal(state.measurement.selectedInputId, 'pw-source-65');
        assert.equal(state.measurement.selectedInputKey, 'device-serial:miniDSP-UMIK-123');
        assert.equal(state.measurement.selectedInputUnavailable, false);
        assert.equal(context.measurementInputAvailabilityMessage(), '');
        assert.doesNotMatch(context.measurementSetupStatusText(), /currently unavailable/);
    }

    // 3. Missing -> present switch: a stale in-flight scan (snapshotted before
    //    the save) must not resurrect the missing-state.
    {
        const { context, inputsResolvers, state } = makeContext();
        state.measurement.selectedInputConfigured = true;
        state.measurement.inputs = [UMIK('pw-source-65')];
        const pending = context.fetchMeasurementInputs();
        context.applyMeasurementInputSelection('pw-source-65');
        resolveInputsFetch(inputsResolvers, inputsPayload(
            [UMIK('pw-source-65')],
            // Backend snapshot from BEFORE the save: old device still missing.
            { configured: true, unavailable: true, input_id: '', persistent_id: 'device-serial:miniDSP-UMIK-123' },
        ));
        await pending;
        assert.equal(state.measurement.selectedInputId, 'pw-source-65',
            'stale fetch must not wipe the deliberate re-selection');
        assert.equal(state.measurement.selectedInputKey, 'device-serial:miniDSP-UMIK-123');
        assert.equal(state.measurement.selectedInputUnavailable, false);
        assert.equal(context.measurementInputAvailabilityMessage(), '');
        assert.equal(context.measurementSetupStatusText(), state.measurement.statusText);
        assert.doesNotMatch(context.measurementSetupStatusText(), /currently unavailable/);
    }

    // 4. Switch between two present devices: newer selection wins over a stale
    //    snapshot that still resolved the previous device.
    {
        const { context, inputsResolvers, state } = makeContext();
        state.measurement.selectedInputConfigured = true;
        state.measurement.inputs = [UMIK('pw-source-54'), OTHER('pw-source-70')];
        const pending = context.fetchMeasurementInputs();
        context.applyMeasurementInputSelection('pw-source-70');
        resolveInputsFetch(inputsResolvers, inputsPayload(
            [UMIK('pw-source-54'), OTHER('pw-source-70')],
            { configured: true, unavailable: false, input_id: 'pw-source-54', persistent_id: 'device-serial:miniDSP-UMIK-123' },
        ));
        await pending;
        assert.equal(state.measurement.selectedInputId, 'pw-source-70');
        assert.equal(state.measurement.selectedInputUnavailable, false);
        assert.equal(context.measurementInputAvailabilityMessage(), '');
    }

    // 5. Previously missing device is re-discovered -> state reconciled by a
    //    fresh fetch with the persisted selection.
    {
        const { context, inputsResolvers, state } = makeContext();
        state.measurement.selectedInputConfigured = true;
        state.measurement.selectedInputUnavailable = true;
        const pending = context.fetchMeasurementInputs();
        resolveInputsFetch(inputsResolvers, inputsPayload(
            [UMIK('pw-source-65')],
            { configured: true, unavailable: false, input_id: 'pw-source-65', persistent_id: 'device-serial:miniDSP-UMIK-123' },
        ));
        await pending;
        assert.equal(state.measurement.selectedInputId, 'pw-source-65');
        assert.equal(state.measurement.selectedInputUnavailable, false);
        assert.equal(context.measurementInputAvailabilityMessage(), '');
        assert.match(context.measurementSetupStatusText(), /sweep ready/i);
        assert.equal(context.getRevision(), 1, 'reconciliation save must persist the resolved device');
    }

    // 6. Close/open does not conserve a stale warning: the status line is
    //    derived from the current truth on every render.
    {
        const { context, state } = makeContext();
        state.measurement.inputs = [UMIK('pw-source-65')];
        state.measurement.selectedInputId = 'pw-source-65';
        state.measurement.selectedInputKey = 'device-serial:miniDSP-UMIK-123';
        state.measurement.selectedInputConfigured = true;
        state.measurement.selectedInputUnavailable = false;
        state.measurement.hostCaptureAvailable = true;
        // Close + reopen re-renders the status line from current state.
        const before = context.measurementSetupStatusText();
        assert.doesNotMatch(before, /currently unavailable/);
        assert.equal(before, state.measurement.statusText);

        state.measurement.selectedInputUnavailable = true;
        const after = context.measurementSetupStatusText();
        assert.equal(after, WARNING_TEXT, 'reopen must reflect the actual state');
    }

    console.log('measurement input availability frontend tests: ok');
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
