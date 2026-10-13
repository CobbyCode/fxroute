#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Convolver draft metadata and invalidation stay exact across target, range,
// phase and tap changes; preset creation shares the in-flight gate and writes
// the IR at the rate of the measurement the draft was taken from.
const assert = require('node:assert/strict');
require('../static/measurement_dsp.js');
require('../static/measurement_ui.js');
const editor = require('../static/measurement_convolver_editor.js');

function stagedDraft(overrides = {}) {
    const base = {
        side: 'left',
        phaseMode: 'minimum',
        createdAt: '2026-01-01T00:00:00.000Z',
        analysis: { side: 'left', points: 10, autoGainDb: -4 },
        timing: { available: true },
        metadata: {
            targetCurve: 'neutral',
            rangeStartHz: 20,
            rangeEndHz: 250,
            maxBoostDb: 6,
            maxCutDb: -9,
            dipGuard: 'off',
            safetyMarginDb: 1,
            autoGainDb: -4,
            sampleRate: 48000,
            quality: 'minimum_8192',
            phaseMode: 'minimum',
            irLength: '8192',
            sourceMeasurementId: 'm-left',
            sourceMeasurementName: 'Left',
            sourceMeasurementCreatedAt: '',
            sourceChannel: 'left',
        },
    };
    return { ...base, ...overrides, metadata: { ...base.metadata, ...(overrides.metadata || {}) } };
}

function makeState() {
    return {
        measurement: {
            houseCurveOptions: [],
            convolverAssistant: {
                targetCurve: 'neutral',
                rangeStartHz: 20,
                rangeEndHz: 250,
                maxBoostDb: 6,
                maxCutDb: -9,
                dipGuard: 'off',
                safetyMarginDb: 1,
                phaseMode: 'minimum',
                irLength: '8192',
                quality: 'minimum_8192',
                draft: {
                    left: stagedDraft(),
                    right: { ...stagedDraft(), side: 'right', metadata: { ...stagedDraft().metadata, sourceMeasurementId: 'm-right', sourceChannel: 'right' } },
                    presetName: 'staged',
                    nameTouched: true,
                    notice: '',
                },
            },
        },
        dsp: {},
    };
}

class FormStub {
    constructor() { this.fields = new Map(); }
    append(key, value, filename) { this.fields.set(key, { value, filename }); }
}

async function main() {
    // 1. Invalidation keeps exact notices for target, range, phase, taps.
    const cases = [
        ['targetCurve', 'harman', /Target curve changed/],
        ['rangeStartHz', 30, /Correction range changed/],
        ['rangeEndHz', 3000, /Correction range changed/],
        ['maxBoostDb', 3, /Correction limits changed/],
        ['maxCutDb', -12, /Correction limits changed/],
        ['dipGuard', 'gentle', /Dip guard changed/],
        ['phaseMode', 'linear', /Phase type changed/],
        ['irLength', '4096', /Convolver taps changed/],
        ['quality', 'minimum_4096', /Convolver taps changed/],
    ];
    for (const [field, value, hint] of cases) {
        const state = makeState();
        // Feedback element mock pushes via textContent setter.
        const messages = [];
        editor.init({
            getState: () => state,
            getElements: () => ({
                measurementConvolverFeedback: {
                    set textContent(v) { if (v) messages.push(String(v)); },
                    get textContent() { return ''; },
                    classList: { add: () => {}, remove: () => {} },
                },
            }),
            showToast: () => {},
            renderMeasurementPanel: () => {},
            scheduleMeasurementGraphRender: () => {},
        });
        editor.updateMeasurementConvolverField(field, value);
        const conv = state.measurement.convolverAssistant;
        assert.equal(conv.draft.left, null, `${field} must clear the left draft`);
        assert.equal(conv.draft.right, null, `${field} must clear the right draft`);
        assert.match(conv.draft.notice, hint, `${field} keeps its retake hint`);
        assert.ok(messages.some((m) => hint.test(m)), `${field} surfaces its hint`);
    }

    // 2. Preset creation uses staged metadata, shares the gate, clears draft.
    // The drafts come from 96 kHz measurements: the IRs are written at 96 kHz
    // with the staged tap count.
    const state = makeState();
    for (const draft of [state.measurement.convolverAssistant.draft.left, state.measurement.convolverAssistant.draft.right]) {
        draft.metadata.sampleRate = 96000;
    }
    const calls = [];
    let sharedBusy = false;
    const elements = {
        measurementConvolverFeedback: { textContent: '', classList: { add: () => {}, remove: () => {} } },
        measurementConvolverPresetName: { value: 'Room Conv' },
    };
    let resolvePost;
    let visibleEntries = [];
    editor.init({
        getState: () => state,
        getElements: () => elements,
        getFormDataType: () => FormStub,
        fetch: async (url, options) => {
            calls.push(['fetch', url, options.body.fields]);
            return new Promise((resolve) => { resolvePost = resolve; });
        },
        showToast: (message, kind) => calls.push(['toast', message, kind]),
        renderMeasurementPanel: () => calls.push('render'),
        scheduleMeasurementGraphRender: () => {},
        collectEffectsExtras: () => ({
            limiterEnabled: false, headroomEnabled: true, headroomGainDb: -3,
            autogainEnabled: false, autogainTargetDb: -12, bassEnabled: false, bassAmount: 0,
            toneEffectEnabled: false, toneEffectMode: 'crystalizer',
        }),
        appendBankBindingFields: (form) => form.append('bank_id', 'main'),
        measurementCommitSourceId: () => 'measurement-1',
        requireConcreteFilterBank: () => true,
        measurementBankSumsBothInputs: () => false,
        fetchEffects: async () => { calls.push('effects'); },
        fetchOutputSystemCatalog: async () => { calls.push('catalog'); },
        formatTransitionErrorDetail: (detail, fallback) => detail?.message || fallback,
        getVisibleMeasurementEntries: () => visibleEntries,
        getCurrentMeasurementEntries: () => [],
        getMeasurementDisplayTraces: (measurement) => measurement.traces || [],
        smoothMeasurementTracePoints: (points) => points,
        waitForNextAnimationFrame: async () => {},
        isConvolverCreateInFlight: () => sharedBusy,
        setConvolverCreateInFlight: (active) => { sharedBusy = active; calls.push(['busy', active]); },
        getTimingSafetyLimitMs: () => 6,
    });
    const creating = editor.createMeasurementConvolverPresetFromDraft();
    assert.equal(sharedBusy, true, 'creation holds the shared gate');
    await editor.createMeasurementConvolverPresetFromDraft();
    assert.equal(calls.filter((c) => c[0] === 'fetch').length, 1, 'concurrent create is gated');
    const fields = calls.find((c) => c[0] === 'fetch')[2];
    assert.equal(fields.get('preset_name').value, 'Room Conv');
    assert.equal(fields.get('source_measurement_id').value, 'measurement-1');
    assert.equal(fields.get('bank_id').value, 'main');
    assert.ok(fields.get('left_file'), 'both-mode create sends L/R IR files');
    assert.ok(fields.get('right_file'), 'both-mode create sends L/R IR files');
    for (const key of ['left_file', 'right_file']) {
        const wav = Buffer.from(await fields.get(key).value.arrayBuffer());
        assert.equal(wav.readUInt32LE(24), 96000, `${key} is written at the measurement rate`);
        assert.equal(wav.readUInt32LE(40) / 4, 8192, `${key} keeps the staged tap count`);
    }
    resolvePost({ ok: true, json: async () => ({ preset: { name: 'Room Conv' }, ir: {} }) });
    await creating;
    assert.equal(sharedBusy, false);
    assert.equal(state.measurement.convolverAssistant.draft.left, null);
    assert.equal(state.measurement.convolverAssistant.draft.right, null);
    assert.equal((state.dsp.assistStack || []).length, 1);
    assert.equal(state.dsp.assistStack[0].metadata.sampleRate, 96000, 'generation keeps staged metadata');
    assert.equal(state.dsp.assistStack[0].metadata.irLength, '8192');

    // 3. The FIR rate is the rate of the source measurement; entries without
    // an analysis rate fall back to their frozen target, their output context
    // and finally 48 kHz.
    const rateOf = editor.getMeasurementConvolverSampleRate;
    assert.equal(rateOf({ analysis: { sample_rate: 96000 }, measurement_target: { sample_rate_hz: 48000 }, audio_output_context: { sample_rate_hz: 44100 } }), 96000);
    assert.equal(rateOf({ analysis: {}, measurement_target: { sample_rate_hz: 88200 }, audio_output_context: { sample_rate_hz: 44100 } }), 88200);
    assert.equal(rateOf({ audio_output_context: { sample_rate_hz: 44100 } }), 44100);
    assert.equal(rateOf({ analysis: { sample_rate: 0 }, measurement_target: { sample_rate_hz: null } }), 48000);
    assert.equal(rateOf({}), 48000);
    assert.equal(rateOf(null), 48000);

    // 4. Take stages the rate of each source measurement.
    const sourceMeasurement = (channel, sampleRate) => ({
        id: `m-${channel}`, name: channel, channel,
        analysis: { sample_rate: sampleRate },
        traces: [{ points: [[30, -2], [60, 1], [120, -1], [200, 0]] }],
    });
    const drafts = state.measurement.convolverAssistant.draft;
    visibleEntries = [sourceMeasurement('left', 96000), sourceMeasurement('right', 96000)];
    editor.takeMeasurementConvolverToDraft('both');
    assert.equal(drafts.left.metadata.sampleRate, 96000);
    assert.equal(drafts.right.metadata.sampleRate, 96000);
    assert.equal(drafts.left.metadata.irLength, 8192, 'the taps stay as selected');

    // 5. Left and Right from measurements at different rates are not combined.
    visibleEntries = [sourceMeasurement('right', 48000)];
    editor.takeMeasurementConvolverToDraft('right');
    assert.equal(drafts.left.metadata.sampleRate, 96000);
    assert.equal(drafts.right.metadata.sampleRate, 48000);
    calls.length = 0;
    await editor.createMeasurementConvolverPresetFromDraft();
    assert.equal(calls.filter((c) => c[0] === 'fetch').length, 0, 'mismatched L/R rates create nothing');
    assert.ok(calls.some((c) => c[0] === 'toast' && /different sample rates/.test(c[1]) && c[2] === 'warning'));
    assert.match(elements.measurementConvolverFeedback.textContent, /different sample rates/);
    assert.ok(drafts.left && drafts.right, 'the drafts stay staged');
    assert.equal(sharedBusy, false);

    // 6. A single side from a 44.1 kHz measurement is written at 44.1 kHz.
    drafts.left = null;
    drafts.right.metadata.sampleRate = 44100;
    const single = editor.createMeasurementConvolverPresetFromDraft();
    await Promise.resolve();
    await Promise.resolve();
    const singleCall = calls.find((c) => c[0] === 'fetch');
    assert.equal(singleCall[1], '/api/dsp/presets/create-with-ir');
    const singleWav = Buffer.from(await singleCall[2].get('file').value.arrayBuffer());
    assert.equal(singleWav.readUInt32LE(24), 44100);
    assert.equal(singleWav.readUInt32LE(40) / 4, 8192);
    resolvePost({ ok: true, json: async () => ({ preset: { name: 'Room Conv' }, ir: {} }) });
    await single;
    console.log('measurement convolver editor: ok');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
