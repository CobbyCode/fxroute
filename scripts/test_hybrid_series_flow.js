#!/usr/bin/env node
'use strict';

// The Advanced dialog chooses the speakers (one routed group, or all of them
// in turn) and a series measures every group with its regular sequence.  Runs
// the real flows module with only the DOM and the measurement API simulated.

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.resolve(__dirname, '..');
const Hybrid = require('../static/hybrid_measurement.js');

const GROUPS = {
    front: { id: 'front', short: 'Front', label: 'Front L/R + Sub', area: { label: 'Global · Front L/R + Sub', channel_mode: 'stereo' } },
    center: { id: 'center', short: 'Center', label: 'Center + Sub', area: { label: 'Global · Center + Sub', channel_mode: 'mono' } },
    side: { id: 'side', short: 'Side', label: 'Side L/R + Sub', area: { label: 'Global · Side L/R + Sub', channel_mode: 'stereo' } },
    rear: { id: 'rear', short: 'Rear', label: 'Rear L/R + Sub', area: { label: 'Global · Rear L/R + Sub', channel_mode: 'stereo' } },
};

function areaFor(group) {
    return { bank_id: 'global', group, ...GROUPS[group].area };
}

function classList() {
    const classes = new Set();
    return { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
        toggle(c, on) { if (on) classes.add(c); else classes.delete(c); } };
}

function element() {
    return { textContent: '', innerHTML: '', dataset: {}, disabled: false, classList: classList() };
}

// A row whose chips are parsed from the markup the module writes.
function chipRow() {
    const row = element();
    row.querySelectorAll = () => [...row.innerHTML.matchAll(/data-hybrid-scope="([a-z]+)"/g)].map((match) => {
        row.chips = row.chips || {};
        if (!row.chips[match[1]]) {
            const attributes = { 'data-hybrid-scope': match[1] };
            row.chips[match[1]] = { disabled: false, classList: classList(),
                getAttribute: name => attributes[name], setAttribute: (name, value) => { attributes[name] = value; } };
        }
        return row.chips[match[1]];
    });
    return row;
}

function measurementFor(form, index) {
    const role = form.get('measurement_role');
    const channel = form.get('channel');
    const group = form.get('measurement_group');
    const frequencies = [20, 40, 80, 160, 320, 640, 1280, 5000];
    const complex = (real) => ({ points: frequencies.map(frequency => [frequency, real, 0]) });
    const measurement = {
        id: `m-${index}`, channel, measurement_role: role,
        input_device: { label: 'Mic' }, input_channels: { mic: 1 }, calibration: {}, audio_output_context: {},
        traces: [{ kind: 'sweep', role: 'trusted', points: frequencies.map(frequency => [frequency, 0]) }],
        analysis: {},
        measurement_target: { schema: 'fxroute.measurement-target', bank_id: 'global', global_group: group },
    };
    if (role === 'direct') {
        measurement.analysis.direct_response = { usable: true, points: frequencies.map(frequency => [frequency, 0]),
            gated_direct_lower_limit_hz: 300, direct_confidence: 1, gate_confidence: 1 };
    }
    if (role === 'mlp') measurement.analysis.complex_response = complex(1);
    // The sum of the two main-position captures.
    if (role === 'integration') measurement.analysis.complex_response = complex(2);
    return measurement;
}

function makeSeries({ groups = ['front', 'center', 'side', 'rear'], selected = 'front', poorIntegrationOnce = [] } = {}) {
    const state = {
        measurement: { selectedInputId: 'in1', selectedInputKey: 'k', selectedMicInputChannel: '1',
            selectedReferenceInputChannel: '', pendingRepeatMeasurements: [] },
        outputSystem: { catalog: { active_mode: 'surround', modes: { surround: { topology: { mode: 'surround',
            roles: ['main_l', 'main_r', 'center', 'side_l', 'side_r', 'rear_l', 'rear_r', 'sub1', 'sub2'],
            sub_roles: ['sub1', 'sub2'], sub_mode: 'dual-mono' } } } } },
    };
    const choice = { selected };
    const forms = [];
    const poorLeft = new Set(poorIntegrationOnce);
    const calls = { assist: [], views: [], selected: [], panels: 0 };
    const elements = {
        measurementHybridPanel: Object.assign(element(), {
            querySelector: () => ({ setAttribute: () => {} }), querySelectorAll: () => [] }),
        measurementHybridSummary: element(), measurementHybridMode: element(), measurementHybridProgress: element(),
        measurementHybridTitle: element(), measurementHybridInstruction: element(), measurementHybridActive: element(),
        measurementHybridStatus: element(), measurementHybridPrimaryBtn: element(), measurementHybridBackBtn: element(),
        measurementHybridScopeRow: chipRow(),
    };
    const ctx = { console, Math, JSON, Object, Array, Promise, String, Number, Boolean, Date, Set, setTimeout, clearTimeout,
        FormData, window: {},
        FXRouteMeasurementUI: { MEASUREMENT_JOB_SUCCESS_STATES: new Set(['completed']),
            MEASUREMENT_JOB_FAILED_STATES: new Set(['failed']), MEASUREMENT_JOB_CANCELLED_STATES: new Set(['cancelled']),
            hybridSpeakerName: () => 'speaker' },
        FXRouteHybridMeasurement: Hybrid };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8'), ctx);
    const Flows = ctx.FXRouteMeasurementFlows;
    Flows.init({
        getState: () => state,
        getElements: () => elements,
        api: {
            startMeasurement: async (form) => {
                forms.push(form);
                return { ok: true, json: async () => ({ job: { id: `job-${forms.length}` } }) };
            },
            pollMeasurementJob: async (id) => {
                const index = Number(id.slice(4));
                const form = forms[index - 1];
                const measurement = measurementFor(form, index);
                // The first integration check of a listed group fails once.
                if (form.get('measurement_role') === 'integration' && poorLeft.has(form.get('measurement_group'))) {
                    poorLeft.delete(form.get('measurement_group'));
                    measurement.analysis.complex_response = { points: [20, 40, 80, 160, 320].map(f => [f, 0.05, 0]) };
                }
                return { ok: true, status: 200, json: async () => ({ job: { id, status: 'completed', measurement } }) };
            },
            cancelMeasurementJob: async () => ({ ok: true, json: async () => ({}) }),
        },
        sleep: async () => {},
        showToast: () => {},
        renderMeasurementPanel: () => { calls.panels += 1; },
        renderMeasurementPanelDefensively: () => {},
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        normalizeMeasurementInputChannelSelections: () => {},
        measurementModeReady: () => true,
        escapeHtml: require('../static/ui_helpers.js').escapeHtml,
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        getMeasurementJobStatus: job => job.status,
        getMeasurementJobResultMeasurement: job => job.measurement,
        normalizeMeasurementEntry: entry => entry,
        setMeasurementAssistMode: mode => calls.assist.push(mode),
        setMeasurementGraphView: view => calls.views.push(view),
        measurementAreaBank: () => 'global',
        measurementAreaGroup: () => choice.selected,
        measurementArea: () => areaFor(choice.selected),
        measurementAreaForGroup: group => areaFor(group),
        measurementGroupChoice: () => ({ selected: choice.selected, options: groups.map(id => GROUPS[id]) }),
        selectMeasurementGroup: (group) => { choice.selected = group; calls.selected.push(group); },
    });
    return { Flows, state, elements, forms, calls, choice };
}

// Runs "Start measurement" until the wizard is complete or a run reports an error.
async function runToEnd(Flows, { stopOnError = false } = {}) {
    const wizard = Flows.getHybridWizardState();
    for (let guard = 0; guard < 60 && !wizard.profile; guard += 1) {
        await Flows.runHybridWizardSweep();
        if (stopOnError && wizard.quality?.level === 'error') break;
    }
    return wizard;
}

(async () => {
    // The dialog offers each routed group plus All, and a group choice is the shared selection.
    {
        const { Flows, elements, calls, choice } = makeSeries();
        Flows.openHybridMeasurementWizard();
        const row = elements.measurementHybridScopeRow;
        assert.equal(row.classList.contains('hidden'), false);
        assert.equal(row.dataset.groups, 'front,center,side,rear,all');
        const wizard = Flows.getHybridWizardState();
        assert.equal(wizard.series, false, 'opening starts with the selected group, like before');
        assert.equal(wizard.sequence.length, 9);
        Flows.selectHybridWizardScope('rear');
        assert.deepEqual(calls.selected, ['rear']);
        assert.equal(choice.selected, 'rear');
        assert.equal(wizard.series, false);
        assert.equal(wizard.scope.group, 'rear');
        assert.equal(wizard.sequence.length, 9);
        Flows.selectHybridWizardScope('center');
        assert.equal(wizard.sequence.length, 4, 'a single speaker is one channel with no integration step');
        Flows.selectHybridWizardScope('all');
        assert.equal(wizard.series, true);
        assert.equal(choice.selected, 'center', 'All is the wizard only; the speaker selection stays');
        assert.deepEqual([...new Set(wizard.sequence.map(step => step.group))], ['front', 'center', 'side', 'rear']);
        assert.equal(wizard.sequence.length, 9 + 4 + 9 + 9);
        assert.equal(new Set(wizard.sequence.map(step => step.id)).size, wizard.sequence.length, 'step ids stay unique');
        assert.match(elements.measurementHybridMode.textContent, /^Front L\/R \+ Sub · measuring area · 1 \/ 4$/);
        assert.equal(elements.measurementHybridScopeRow.chips.all.classList.contains('is-active'), true);
    }

    // Without a choice (one routed group) the row stays hidden and no series exists.
    {
        const { Flows, elements } = makeSeries({ groups: ['front'] });
        Flows.openHybridMeasurementWizard();
        assert.equal(elements.measurementHybridScopeRow.classList.contains('hidden'), true);
        Flows.selectHybridWizardScope('all');
        assert.equal(Flows.getHybridWizardState().series, false);
    }

    // A series measures each group with its own group in the request and ends with every model.
    {
        const { Flows, state, elements, forms, calls, choice } = makeSeries({ selected: 'front' });
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        await Flows.runHybridWizardSweep();
        assert.equal(wizard.stepIndex, 1, 'each direct measurement is its own position series');
        assert.equal(elements.measurementHybridScopeRow.chips.all.disabled, true, 'the choice is locked once measuring began');
        Flows.selectHybridWizardScope('rear');
        assert.equal(wizard.series, true, 'a locked choice is ignored');
        await runToEnd(Flows);
        assert.equal(forms.length, 31);
        const groupsOfRequests = [];
        forms.forEach((form) => {
            const group = form.get('measurement_group');
            if (groupsOfRequests[groupsOfRequests.length - 1] !== group) groupsOfRequests.push(group);
        });
        assert.deepEqual(groupsOfRequests, ['front', 'center', 'side', 'rear'], 'each step names its own group');
        assert.equal(choice.selected, 'front', 'the series never moves the shared selection');
        assert.equal(wizard.profile.series, true);
        assert.deepEqual(wizard.profile.parts.map(part => part.scope.group), ['front', 'center', 'side', 'rear']);
        assert.deepEqual(Object.keys(wizard.profile.parts[0].profile.models), ['left', 'right']);
        assert.deepEqual(Object.keys(wizard.profile.parts[1].profile.models), ['stereo']);
        const summary = elements.measurementHybridSummary.innerHTML;
        assert.deepEqual([...summary.matchAll(/<strong>([^<]+)<\/strong>/g)].map(match => match[1]),
            ['Front L/R + Sub', 'Center + Sub', 'Side L/R + Sub', 'Rear L/R + Sub']);
        assert.match(summary, /Gated direct lower limit: Left 300 Hz · Right 300 Hz/);
        assert.match(summary, /Gated direct lower limit: 300 Hz/);
        assert.match(summary, /subwoofer routing checked \(ok\)/);
        assert.equal(elements.measurementHybridPrimaryBtn.textContent, 'Show results');

        Flows.openHybridProfileInConvolver();
        const entries = state.measurement.pendingRepeatMeasurements;
        assert.equal(entries.length, 7, 'Front L/R, Center, Side L/R and Rear L/R');
        assert.equal(new Set(entries.map(entry => entry.id)).size, 7, 'one save rejects duplicate ids');
        assert.deepEqual(entries.map(entry => entry.save_suffix), [
            'Front L/R + Sub · L', 'Front L/R + Sub · R', 'Center + Sub',
            'Side L/R + Sub · L', 'Side L/R + Sub · R', 'Rear L/R + Sub · L', 'Rear L/R + Sub · R']);
        assert.deepEqual(entries.map(entry => entry.measurement_target.global_group),
            ['front', 'front', 'center', 'side', 'side', 'rear', 'rear']);
        assert.deepEqual(entries.map(entry => entry.channel), ['left', 'right', 'left', 'left', 'right', 'left', 'right']);
        assert.equal(state.measurement.currentMeasurementName, 'Advanced');
        assert.deepEqual(calls.assist, [], 'the convolver takes one L/R pair, not a series');
        assert.deepEqual(calls.views, ['freq']);
        assert.match(state.measurement.statusText, /series finished/i);
        assert.equal(wizard.open, false);
    }

    // A failed integration check is repeated for that group before the next group starts.
    {
        const { Flows, forms } = makeSeries({ poorIntegrationOnce: ['front'] });
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        await runToEnd(Flows, { stopOnError: true });
        assert.equal(wizard.quality?.level, 'error');
        assert.match(wizard.status, /integration check failed/i);
        const integrationStep = wizard.sequence[wizard.stepIndex];
        assert.deepEqual([integrationStep.role, integrationStep.group], ['integration', 'front']);
        assert.equal(forms.length, 9, 'no other group was measured yet');
        assert.equal(wizard.groupProfiles.front, undefined);
        await runToEnd(Flows);
        assert.equal(forms.length, 9 + 1 + 4 + 9 + 9, 'only the integration step was repeated');
        assert.deepEqual(wizard.profile.parts.map(part => part.scope.group), ['front', 'center', 'side', 'rear']);
    }

    // The series compares the routed groups: a changed group set stops it.
    {
        const { Flows } = makeSeries();
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        wizard.scope = { ...wizard.scope, id: 'series+stale' };
        await Flows.runHybridWizardSweep();
        assert.match(wizard.status, /area or output mode changed/);
        assert.equal(wizard.running, false);
    }

    // Opening the wizard again starts a fresh single-group run.
    {
        const { Flows } = makeSeries();
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        Flows.getHybridWizardState().open = false;
        Flows.openHybridMeasurementWizard();
        const wizard = Flows.getHybridWizardState();
        assert.deepEqual([wizard.series, wizard.captures.length, wizard.sequence.length], [false, 0, 9]);
    }

    console.log('hybrid series flow: ok');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
