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
    const positions = ['direct-left', 'direct-right', 'direct-stereo', 'mlp', 'left', 'right'].map(position => {
        const node = element();
        node.getAttribute = () => position;
        return node;
    });
    const subs = ['left', 'right'].map(branch => {
        const node = element();
        node.getAttribute = () => branch;
        return node;
    });
    const layout = { attributes: {}, setAttribute(name, value) { this.attributes[name] = value; } };
    const panel = {
        classList: element().classList,
        querySelector: (selector) => (selector === '.hybrid-room-view' ? layout : null),
        querySelectorAll: (selector) => selector.includes('data-hybrid-speaker') ? speakers
            : selector.includes('data-hybrid-position') ? positions : selector.includes('data-hybrid-sub') ? subs : [],
    };
    const elements = { measurementHybridPanel: panel, measurementHybridSummary: element(), measurementHybridMode: element(),
        measurementHybridProgress: element(), measurementHybridTitle: element(), measurementHybridInstruction: element(),
        measurementHybridActive: element(), measurementHybridStatus: element(), measurementHybridPrimaryBtn: element(),
        measurementHybridBackBtn: element() };
    const ctx = { console, Math, JSON, Object, Array, Promise, String, Number, Boolean, Date, setTimeout, clearTimeout,
        FormData, window: {}, FXRouteHybridMeasurement: Hybrid, hybridSpeakerName: () => 'speaker',
        FXRouteMeasurementUI: { getTypedMeasurementName: require('../static/measurement_ui.js').getTypedMeasurementName } };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    const calls = { assist: [], panels: 0 };
    ctx.FXRouteMeasurementFlows.init({
        getState: () => state,
        getElements: () => elements,
        measurementArea: () => area,
        measurementModeReady: () => true,
        escapeHtml: require('../static/ui_helpers.js').escapeHtml,
        setMeasurementAssistMode: (mode) => calls.assist.push(mode),
        renderMeasurementPanel: () => { calls.panels += 1; },
        renderMeasurementPanelDefensively: () => {},
        showToast: () => {},
    });
    return { Flows: ctx.FXRouteMeasurementFlows, elements, layout, speakers, positions, subs, calls };
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

// A concrete bank must not fall back to the front-wall diagram or acquire a
// routing group. Only Global shows the subs; speaker/microphone states agree.
for (const [bankId, group, label, channelMode, labels] of [
    ['rear', 'rear', 'Rear L/R', 'stereo', ['RL', 'RR', 'C']],
    ['side', 'side', 'Side L/R', 'stereo', ['SL', 'SR', 'C']],
    ['center', 'center', 'Center', 'mono', ['L', 'R', 'C']],
    ['main', 'front', 'Front L/R', 'stereo', ['L', 'R', 'C']],
]) {
    const globalArea = { bank_id: 'global', group, label: `Global · ${label} + Sub`, channel_mode: channelMode };
    const bankArea = { bank_id: bankId, label, channel_mode: channelMode };
    const global = makeFlows({ area: globalArea, state: catalogState('dual-mono') });
    const bank = makeFlows({ area: bankArea, state: catalogState('dual-mono') });
    const globalScope = global.Flows.getCurrentHybridScope();
    const bankScope = bank.Flows.getCurrentHybridScope();
    assert.equal(bankScope.group, '', `${bankId}: display group must not become a routing group`);
    assert.equal(bankScope.id, `${bankId}||${channelMode === 'mono' ? 'stereo' : 'left,right'}|subwoofer-2.2`);
    const steps = Hybrid.buildSequence(bankScope).steps;
    assert.deepEqual(steps.map(({ id, role, position, channel }) => [id, role, position, channel]),
        Hybrid.buildSequence(globalScope).steps.map(({ id, role, position, channel }) => [id, role, position, channel]),
        'display metadata must not change the measurement sequence');
    for (const [step, complete] of [...steps.map(step => [step, false]), [{}, true]]) {
        global.Flows.renderHybridRoomDiagram(step, globalScope, complete);
        bank.Flows.renderHybridRoomDiagram(step, bankScope, complete);
        assert.equal(bank.layout.attributes['data-hybrid-group'], group, `${bankId}: bank speaker placement`);
        assert.deepEqual(bank.layout.attributes, global.layout.attributes, `${bankId}: same room geometry`);
        assert.deepEqual(bank.speakers.map(node => node.textContent), labels, `${bankId}: speaker labels`);
        assert.deepEqual(bank.speakers.map(node => node.classList.contains('is-active')),
            global.speakers.map(node => node.classList.contains('is-active')));
        assert.deepEqual(bank.positions.filter(node => node.classList.contains('is-target')).map(node => node.getAttribute()),
            complete ? [] : [step.position], `${bankId}: selected microphone`);
        assert(bank.subs.every(node => node.classList.contains('hidden')), `${bankId}: individual bank hides subs`);
        assert(global.subs.every(node => !node.classList.contains('hidden')), `${bankId}: Global keeps subs`);
    }
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
    const speaker = wizard.scope.speaker;
    wizard.scope.speaker = '<img src=x onerror=alert(1)>';
    Flows.renderHybridMeasurementWizard();
    assert.doesNotMatch(elements.measurementHybridSummary.innerHTML, /<img src=x/);
    assert.match(elements.measurementHybridSummary.innerHTML, /&lt;img src=x/);
    wizard.scope.speaker = speaker;
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

// A name typed into the field is kept by the model entries; a proposed one is
// not a typed name and is replaced.
{
    const area = { bank_id: 'global', group: 'rear', label: 'Global · Rear L/R + Sub', channel_mode: 'stereo' };
    for (const [shown, typed, expected] of [
        ['Seat A', 'Seat A', 'Seat A'],
        [' Seat A ', 'Seat A', 'Seat A'],
        ['Demo Measurement 4', '', 'Advanced Rear L/R + Sub'],
        ['Sweep 2', 'Seat A', 'Advanced Rear L/R + Sub'],
        ['', '', 'Advanced Rear L/R + Sub'],
    ]) {
        const state = catalogState('dual-mono');
        state.measurement.currentMeasurementName = shown;
        state.measurement.typedMeasurementName = typed;
        const { Flows } = makeFlows({ area, state });
        const sequence = Hybrid.buildSequence(Flows.getCurrentHybridScope());
        const integration = { status: 'ok', validationBandHz: [20, 500], magnitudeRmsErrorDb: 1, phaseRmsErrorDeg: 5,
            complexResidualRms: 0.1, comparedPoints: 10, limitation: 'x' };
        Object.assign(Flows.getHybridWizardState(), { open: true, mode: sequence.mode, scope: sequence.scope,
            sequence: sequence.steps, stepIndex: sequence.steps.length, profile: { mode: sequence.mode, scope: sequence.scope,
                integration, models: { left: model('left'), right: model('right') } } });
        Flows.openHybridProfileInConvolver();
        assert.equal(state.measurement.currentMeasurementName, expected, `shown "${shown}", typed "${typed}"`);
        // The entries keep their own names; only the field proposal differs.
        assert.deepEqual(state.measurement.pendingRepeatMeasurements.map(item => item.name),
            ['Advanced Rear L/R + Sub L', 'Advanced Rear L/R + Sub R']);
    }
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
