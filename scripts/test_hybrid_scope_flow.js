#!/usr/bin/env node
'use strict';

// The Advanced wizard follows the selected measuring area: a speaker pair, one
// mono channel (Center), or subwoofers. Runs the real flows module with only
// the DOM and network boundaries simulated.

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const Hybrid = require('../static/hybrid_measurement.js');

function element() {
    const classes = new Set();
    return { textContent: '', innerHTML: '', dataset: {}, disabled: false,
        classList: { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
            toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)) } };
}

function makeFlows({ area, topology, state }) {
    const speakers = [{ speaker: 'left' }, { speaker: 'right' }, { speaker: 'stereo' }].map(item => {
        const node = element();
        node.getAttribute = () => item.speaker;
        return node;
    });
    const layout = { attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
    const panel = {
        classList: element().classList,
        querySelector: (selector) => (selector === '.hybrid-room-view' ? layout : null),
        querySelectorAll: (selector) => (selector.includes('data-hybrid-speaker') ? speakers : []),
    };
    const elements = { measurementHybridPanel: panel, measurementHybridSummary: element(), measurementHybridMode: element(),
        measurementHybridProgress: element(), measurementHybridTitle: element(), measurementHybridInstruction: element(),
        measurementHybridActive: element(), measurementHybridStatus: element(), measurementHybridPrimaryBtn: element(),
        measurementHybridBackBtn: element() };
    const ctx = { console, Math, JSON, Object, Array, Promise, String, Number, Boolean, Date, setTimeout, clearTimeout,
        FormData, window: {}, FXRouteMeasurementUI: {}, FXRouteHybridMeasurement: Hybrid, hybridSpeakerName: () => 'speaker' };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    const calls = { assist: [], panels: 0 };
    ctx.FXRouteMeasurementFlows.init({
        getState: () => state,
        getElements: () => elements,
        measurementArea: () => area,
        measurementModeReady: () => true,
        escapeHtml: String,
        setMeasurementAssistMode: (mode) => calls.assist.push(mode),
        renderMeasurementPanel: () => { calls.panels += 1; },
        renderMeasurementPanelDefensively: () => {},
        showToast: () => {},
    });
    return { Flows: ctx.FXRouteMeasurementFlows, elements, layout, calls };
}

function catalogState(subMode) {
    return { measurement: {}, outputSystem: { catalog: { active_mode: 'surround', modes: { surround: {
        topology: { mode: 'surround', roles: ['main_l', 'main_r', 'center', 'rear_l', 'rear_r', 'sub1', 'sub2'],
            sub_roles: ['sub1', 'sub2'], sub_mode: subMode } } } } } };
}

const target = { schema: 'fxroute.measurement-target', bank_id: 'global', global_group: 'center' };

function points() {
    return [[40, 0, 0], [80, 0, 0]];
}

function model(channel) {
    return { channel, points: points(), constraints: [], modelBlend: { gatedDirectLowerLimitHz: 310 },
        timingMeasurement: { input_device: { label: 'Mic' }, input_channels: { mic: 1 }, calibration: {},
            audio_output_context: {}, analysis: { reference_path: {} }, measurement_target: target } };
}

// Scope: derived from the area and the active topology, so it follows the selection.
{
    const area = { bank_id: 'global', group: 'center', label: 'Global · Center + Sub', channel_mode: 'mono' };
    const { Flows } = makeFlows({ area, state: catalogState('dual-mono') });
    const scope = Flows.getCurrentHybridScope();
    assert.deepEqual([scope.channels, scope.direct, scope.integration, scope.label],
        [['stereo'], true, false, 'Center + Sub']);
    const noArea = makeFlows({ area: null, state: catalogState('dual-mono') }).Flows.getCurrentHybridScope();
    assert.equal(noArea.id, 'subwoofer-2.2', 'without an area the legacy whole-system scope applies');
}

// Center + Sub: one model, handed over like the left side of a mono area, with its target.
{
    const area = { bank_id: 'global', group: 'center', label: 'Global · Center + Sub', channel_mode: 'mono' };
    const state = catalogState('dual-mono');
    const { Flows, elements, layout, calls } = makeFlows({ area, state });
    const sequence = Hybrid.buildSequence(Flows.getCurrentHybridScope());
    const wizard = Flows.getHybridWizardState();
    Object.assign(wizard, { open: true, mode: sequence.mode, scope: sequence.scope, sequence: sequence.steps,
        stepIndex: sequence.steps.length, profile: { mode: sequence.mode, scope: sequence.scope, integration: null,
            left: null, right: null, models: { stereo: model('stereo') } } });
    Flows.renderHybridMeasurementWizard();
    assert.equal(elements.measurementHybridMode.textContent, 'Center + Sub · measuring area');
    assert.equal(layout.attributes['data-hybrid-layout'], 'single');
    const summary = elements.measurementHybridSummary.innerHTML;
    assert.match(summary, /Center speaker response captured/);
    assert.match(summary, /Gated direct lower limit:<\/strong><br>310 Hz/);
    assert.doesNotMatch(summary, /Left and right|Advanced details|NaN/);
    Flows.openHybridProfileInConvolver();
    assert.equal(state.measurement.pendingRepeatMeasurements.length, 1);
    const entry = state.measurement.pendingRepeatMeasurements[0];
    assert.equal(entry.name, 'Advanced Center + Sub');
    assert.equal(entry.channel, 'left');
    assert.deepEqual(entry.measurement_target, target);
    assert.deepEqual(entry.analysis.hybrid_source_roles, ['direct', 'mlp', 'secondary']);
    assert.equal(state.measurement.currentMeasurementName, 'Advanced Center + Sub');
    assert.deepEqual(calls.assist, ['convolver']);
}

// Sub bank: listening-only model, no direct limit in the summary.
{
    const area = { bank_id: 'sub1', label: 'Sub 1', channel_mode: 'mono' };
    const state = catalogState('dual-mono');
    const { Flows, elements, layout } = makeFlows({ area, state });
    const sequence = Hybrid.buildSequence(Flows.getCurrentHybridScope());
    const subModel = { ...model('stereo'), modelBlend: null };
    Object.assign(Flows.getHybridWizardState(), { open: true, mode: sequence.mode, scope: sequence.scope,
        sequence: sequence.steps, stepIndex: sequence.steps.length, profile: { mode: sequence.mode, scope: sequence.scope,
            integration: null, left: null, right: null, models: { stereo: subModel } } });
    Flows.renderHybridMeasurementWizard();
    assert.equal(layout.attributes['data-hybrid-layout'], 'sub');
    assert.doesNotMatch(elements.measurementHybridSummary.innerHTML, /Gated direct|NaN|speaker response captured/);
    assert.match(elements.measurementHybridSummary.innerHTML, /Listening-position measurements combined/);
    Flows.openHybridProfileInConvolver();
    assert.deepEqual(state.measurement.pendingRepeatMeasurements[0].analysis.hybrid_source_roles, ['mlp', 'secondary']);
}

// Pair: L and R models keep the legacy names, and each carries the target.
{
    const area = { bank_id: 'global', group: 'rear', label: 'Global · Rear L/R + Sub', channel_mode: 'stereo' };
    const state = catalogState('dual-mono');
    const { Flows } = makeFlows({ area, state });
    const sequence = Hybrid.buildSequence(Flows.getCurrentHybridScope());
    const integration = { status: 'ok', validationBandHz: [20, 500], magnitudeRmsErrorDb: 1, phaseRmsErrorDeg: 5,
        complexResidualRms: 0.1, comparedPoints: 10, limitation: 'x' };
    Object.assign(Flows.getHybridWizardState(), { open: true, mode: sequence.mode, scope: sequence.scope,
        sequence: sequence.steps, stepIndex: sequence.steps.length, profile: { mode: sequence.mode, scope: sequence.scope,
            integration, models: { left: model('left'), right: model('right') } } });
    Flows.openHybridProfileInConvolver();
    assert.deepEqual(state.measurement.pendingRepeatMeasurements.map(item => [item.name, item.channel]),
        [['Advanced Rear L/R + Sub L', 'left'], ['Advanced Rear L/R + Sub R', 'right']]);
    assert.equal(state.measurement.currentMeasurementName, 'Advanced Rear L/R + Sub');
}

// The wizard refuses to continue when the area changed underneath it.
{
    const area = { bank_id: 'global', group: 'rear', label: 'Global · Rear L/R + Sub', channel_mode: 'stereo' };
    const state = catalogState('dual-mono');
    const { Flows } = makeFlows({ area, state });
    const sequence = Hybrid.buildSequence(Flows.getCurrentHybridScope());
    const wizard = Object.assign(Flows.getHybridWizardState(), { open: true, mode: sequence.mode,
        scope: { ...sequence.scope, id: 'global|front|left,right|subwoofer-2.2' }, sequence: sequence.steps, stepIndex: 0 });
    Flows.runHybridWizardSweep().then(() => {
        assert.match(wizard.status, /area or output mode changed/);
        assert.equal(wizard.running, false);
        console.log('hybrid scope flow: ok');
    }).catch(error => { console.error(error); process.exit(1); });
}
