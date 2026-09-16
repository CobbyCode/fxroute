#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const MeasurementUI = require('../static/measurement_ui.js');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');

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

function makeMeasurementContext({ pendingSave = null, fetchResponse = null } = {}) {
    const fetchCalls = [];
    const saveCalls = [];
    let releaseDebugSnapshot = null;
    let capturedStartForm = null;
    const state = {
        settings: {
            audioOutputs: {
                output_mode: { mode: 'subwoofer-2.2' },
            },
        },
        measurement: {
            hostCaptureAvailable: true,
            selectedInputId: 'pw-source-54',
            selectedMicInputChannel: '1',
            selectedReferenceInputChannel: '',
            selectedCalibrationRef: '',
        },
    };
    class TestFormData {
        constructor() {
            this.fields = [];
        }

        append(name, value) {
            this.fields.push([name, value]);
        }

        get(name) {
            const entry = this.fields.filter(([key]) => key === name).at(-1);
            return entry ? entry[1] : undefined;
        }
    }
    const context = {
        MeasurementUI,
        state,
        // Area contract: the sweep side and bank come from the A/B catalog.
        OutputState: require('../static/output_state.js'),
        outputCatalog: {
            active_mode: 'stereo',
            modes: { stereo: { selected_bank: 'main_l', banks: { global: {}, main_l: {} } } },
        },
        elements: { measurementCalibrationFile: null },
        FormData: class extends TestFormData {
            append(name, value) {
                super.append(name, value);
                if (name === 'channel' || name === 'measurement_bank') capturedStartForm = this;
            }
        },
        console,
        setTimeout,
        clearTimeout,
        fetch: async (url) => {
            fetchCalls.push(url);
            return fetchResponse || {
                ok: true,
                json: async () => ({ job: { id: 'measurement-1', job_kind: 'single', message: 'queued' } }),
            };
        },
    };
    vm.createContext(context);
    vm.runInContext(`
        let _subwooferPendingSave = null;
        let _subwooferSavePromise = null;
        function isSubwooferModeName(mode) {
            return ['subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
        }
        function setPendingSave(value) { _subwooferPendingSave = value; }
        function normalizeMeasurementInputChannelSelections() {}
        function getMeasurementReferenceWarning() { return false; }
        function appendMeasurementReferenceFields(formData) {
            formData.append('reference_input_channel', state.measurement.selectedReferenceInputChannel || '');
        }
        function formatMeasurementJobStatusText() { return 'Preparing sweep…'; }
        function normalizeMeasurementKind(value) { return value; }
        function postRuntimeDebugSnapshot() {
            return new Promise(resolve => { releaseDebugSnapshot = resolve; });
        }
        function releaseSnapshot() { if (releaseDebugSnapshot) releaseDebugSnapshot(); }
        function renderMeasurementPanel() {}
        function measurementAreaFromCatalog() {
            return OutputState.measurementArea(outputCatalog);
        }
        function outputSystemModule() {
            return OutputState;
        }
        async function pollMeasurementJob() {}
        ${extractFunction('formatTransitionErrorDetail')}
        ${extractFunction('flushSubwooferSettingsBeforeMeasurement')}
        ${extractFunction('startHostMeasurement')}
        ${extractFunction('startLrRepeatMeasurement')}
        ${extractFunction('measurementAreaBadge')}
    `, context);
    context.setPendingSave(pendingSave);
    return {
        context, fetchCalls, saveCalls, state,
        get startForm() { return capturedStartForm; },
    };
}

async function main() {
    const formatterContext = {};
    vm.createContext(formatterContext);
    vm.runInContext(extractFunction('formatTransitionErrorDetail'), formatterContext);
    assert.equal(
        formatterContext.formatTransitionErrorDetail('plain failure', 'fallback'),
        'plain failure',
    );
    const structured = formatterContext.formatTransitionErrorDetail(
        { message: 'Playback transition failed at effects-helper-links: graph incomplete', stage: 'effects-helper-links' },
        'fallback',
    );
    assert.match(structured, /effects-helper-links/);
    assert.doesNotMatch(structured, /\[object Object\]/);
    assert.equal(
        formatterContext.formatTransitionErrorDetail(
            { message: 'graph incomplete', stage: 'effects-helper-links' },
            'fallback',
        ),
        'graph incomplete (stage: effects-helper-links)',
    );
    assert.equal(formatterContext.formatTransitionErrorDetail({}, 'fallback'), 'fallback');

    // A committed 2.2 mode must go straight to measurement start; preflush is
    // a no-op and must not issue an identical output-mode POST.
    const committed = makeMeasurementContext();
    const committedStart = committed.context.startHostMeasurement();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(committed.fetchCalls, ['/api/measurements/start']);
    committed.context.releaseSnapshot();
    await committedStart;
    // The selected area decides the sweep side and the frozen bank target.
    const committedStartForm = committed.startForm;
    assert.equal(committedStartForm.get('channel'), 'left');
    assert.equal(committedStartForm.get('measurement_bank'), 'main_l');

    const repeat = makeMeasurementContext();
    const repeatStart = repeat.context.startLrRepeatMeasurement();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(repeat.fetchCalls, ['/api/measurements/lr-repeat/start']);
    repeat.context.releaseSnapshot();
    await repeatStart;
    // The repeat freezes the same selected area for its internal way sweeps.
    assert.equal(repeat.startForm.get('measurement_bank'), 'main_l');
    assert.equal(repeat.startForm.get('channel'), undefined);

    // Saved results carry the frozen area they were captured in; legacy
    // results without a target stay unlabelled.
    const badgeTarget = {
        schema: 'fxroute.measurement-target', version: 1, mode: 'crossover',
        bank_id: 'left_low', legacy: false,
    };
    const badge = committed.context.measurementAreaBadge({ measurement_target: badgeTarget });
    assert.deepEqual({ ...badge }, {
        label: 'Left Low', mode: 'crossover', stale: false,
        title: 'Left Low · Crossover · measured area only',
    });
    assert.equal(
        committed.context.measurementAreaBadge({ measurement_target: { ...badgeTarget, bank_id: 'global' } }).title,
        'Global · Crossover · measured whole system',
    );
    assert.deepEqual(
        committed.context.measurementAreaBadge({ measurement_target: { legacy: true } }), null,
        'legacy results are not silently labelled Global',
    );
    assert.equal(committed.context.measurementAreaBadge({}), null);
    assert.equal(committed.context.measurementAreaBadge(undefined), null);
    assert.equal(
        committed.context.measurementAreaBadge({
            measurement_target: { ...badgeTarget, bank_id: 'left_mid' },
            measurement_target_stale: true,
        }).stale, true,
        'a result whose processing moved on is marked stale',
    );

    // A still-debounced subwoofer edit is started exactly once and awaited
    // before the measurement endpoint is reached.
    let pendingStarts = 0;
    const pending = {
        start: () => {
            pendingStarts += 1;
            return Promise.resolve({ saved: true });
        },
    };
    const pendingContext = makeMeasurementContext({ pendingSave: pending });
    await pendingContext.context.startHostMeasurement();
    assert.equal(pendingStarts, 1);
    assert.deepEqual(pendingContext.fetchCalls, ['/api/measurements/start']);

    // The actual measurement-start path must expose a structured transition
    // error as readable text rather than JavaScript's object stringification.
    const failure = makeMeasurementContext({
        fetchResponse: {
            ok: false,
            json: async () => ({
                detail: {
                    message: 'Playback transition failed at effects-helper-links: graph incomplete',
                    stage: 'effects-helper-links',
                    transition_id: 'tr-frontend-test',
                },
            }),
        },
    });
    await assert.rejects(
        failure.context.startHostMeasurement(),
        (error) => {
            assert.equal(error.message, 'Playback transition failed at effects-helper-links: graph incomplete');
            assert.doesNotMatch(error.message, /\[object Object\]/);
            return true;
        },
    );

    assert.match(indexSource, /app\.js\?v=\d+\.\d+\.\d+/);
    console.log('measurement output-mode frontend tests: ok');
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
