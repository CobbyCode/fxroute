#!/usr/bin/env node
'use strict';

// The Advanced dialog chooses one routed group or all speakers by position. Runs
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

// `ungatedGroups`: groups whose direct response reports no gated lower limit.
function measurementFor(form, index, ungatedGroups = new Set()) {
    const role = form.get('measurement_role');
    const channel = form.get('channel');
    const group = form.get('measurement_group');
    const frequencies = [20, 40, 80, 160, 320, 640, 1280, 5000];
    const complex = (real) => ({ points: frequencies.map(frequency => [frequency, real, 0]) });
    const measurement = {
        id: `m-${index}`, channel, measurement_role: role,
        input_device: { label: 'Mic' }, input_channels: { mic: 1 }, calibration: {}, audio_output_context: {},
        traces: [{ kind: 'sweep', role: 'trusted', points: frequencies.map(frequency => [frequency, index]) }],
        analysis: {},
        measurement_target: { schema: 'fxroute.measurement-target', bank_id: 'global', global_group: group },
    };
    if (role === 'direct') {
        measurement.analysis.direct_response = { usable: true, points: frequencies.map(frequency => [frequency, 0]),
            gated_direct_lower_limit_hz: ungatedGroups.has(group) ? null : 300, direct_confidence: 1, gate_confidence: 1 };
    }
    if (role === 'mlp') measurement.analysis.complex_response = complex(1);
    // The sum of the two main-position captures.
    if (role === 'integration') measurement.analysis.complex_response = complex(2);
    return measurement;
}

function makeSeries({ groups = ['front', 'center', 'side', 'rear'], selected = 'front', poorIntegrationOnce = [],
    ungated = [], cancelOnceAt = 0, failOnceAt = 0 } = {}) {
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
    const ungatedGroups = new Set(ungated);
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
        FXRouteUiHelpers: require('../static/ui_helpers.js'),
        FXRouteMeasurementUI: { MEASUREMENT_JOB_SUCCESS_STATES: new Set(['completed']),
            MEASUREMENT_JOB_FAILED_STATES: new Set(['failed']), MEASUREMENT_JOB_CANCELLED_STATES: new Set(['cancelled']),
            hybridSpeakerName: () => 'speaker',
            getTypedMeasurementName: require('../static/measurement_ui.js').getTypedMeasurementName },
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
                if (index === cancelOnceAt || index === failOnceAt) {
                    return { ok: true, status: 200, json: async () => ({ job: {
                        id, status: index === cancelOnceAt ? 'cancelled' : 'failed', message: 'Capture interrupted' } }) };
                }
                const measurement = measurementFor(form, index, ungatedGroups);
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
        assert.deepEqual(row.dataset.chips.split('|').map(chip => chip.split('=')[0]),
            ['front', 'center', 'side', 'rear', 'all']);
        const wizard = Flows.getHybridWizardState();
        assert.equal(wizard.scope.series === true, false, 'opening starts with the selected group, like before');
        assert.equal('series' in wizard, false, 'the scope alone says whether this is a series');
        assert.equal(wizard.sequence.length, 9);
        Flows.selectHybridWizardScope('rear');
        assert.deepEqual(calls.selected, ['rear']);
        assert.equal(choice.selected, 'rear');
        assert.equal(wizard.scope.series === true, false);
        assert.equal(wizard.scope.group, 'rear');
        assert.equal(wizard.sequence.length, 9);
        Flows.selectHybridWizardScope('center');
        assert.equal(wizard.sequence.length, 4, 'a single speaker is one channel with no integration step');
        Flows.selectHybridWizardScope('all');
        assert.equal(wizard.scope.series, true);
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
        assert.equal(Flows.getHybridWizardState().scope.series === true, false);
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
        assert.equal(elements.measurementHybridScopeRow.chips.all.classList.contains('is-active'), true,
            'a locked row still marks the selected speakers');
        assert.equal(elements.measurementHybridScopeRow.chips.rear.classList.contains('is-active'), false);
        Flows.selectHybridWizardScope('rear');
        assert.equal(wizard.scope.series, true, 'a locked choice is ignored');
        await runToEnd(Flows);
        assert.equal(forms.length, 31);
        assert.deepEqual(forms.slice(0, 7).map(form => [form.get('measurement_group'), form.get('channel'), form.get('measurement_role')]), [
            ['front', 'left', 'direct'], ['front', 'right', 'direct'], ['center', 'stereo', 'direct'],
            ['side', 'left', 'direct'], ['side', 'right', 'direct'], ['rear', 'left', 'direct'], ['rear', 'right', 'direct']]);
        assert.deepEqual(forms.slice(7, 17).map(form => [form.get('measurement_group'), form.get('channel'), form.get('measurement_role')]), [
            ['front', 'left', 'mlp'], ['front', 'right', 'mlp'], ['front', 'stereo', 'integration'], ['center', 'stereo', 'mlp'],
            ['side', 'left', 'mlp'], ['side', 'right', 'mlp'], ['side', 'stereo', 'integration'],
            ['rear', 'left', 'mlp'], ['rear', 'right', 'mlp'], ['rear', 'stereo', 'integration']]);
        assert.equal(choice.selected, 'front', 'the series never moves the shared selection');
        assert.equal(wizard.profile.series, true);
        assert.deepEqual(wizard.profile.parts.map(part => part.scope.group), ['front', 'center', 'side', 'rear']);
        assert.deepEqual(Object.keys(wizard.profile.parts[0].profile.models), ['left', 'right']);
        assert.deepEqual(Object.keys(wizard.profile.parts[1].profile.models), ['stereo']);
        assert.deepEqual(wizard.profile.parts.flatMap(part => Object.values(part.profile.models).map(model => model.timingMeasurement.id)),
            ['m-8', 'm-9', 'm-11', 'm-12', 'm-13', 'm-15', 'm-16'], 'timing stays tied to each speaker at Main');
        const bassLevels = wizard.profile.parts.flatMap(part => Object.values(part.profile.models).map(model => model.points[0][1]));
        [12.05, 13.05, 14.75, 15.75, 16.75, 18.45, 19.45].forEach((want, index) =>
            assert(Math.abs(bassLevels[index] - want) < 1e-9,
                `Model ${index} must combine only its own listening captures: got ${bassLevels[index]}, expected ${want}`));
        assert.equal(wizard.captures.length, 31);
        assert.equal(new Set(wizard.captures.map(capture => capture.stepId)).size, 31);
        assert.deepEqual(wizard.captures.map(capture => [capture.group, capture.position, capture.channel]),
            wizard.sequence.map(step => [step.group, step.position, step.channel]));
        const summary = elements.measurementHybridSummary.innerHTML;
        assert.deepEqual([...summary.matchAll(/<strong>([^<]+)<\/strong>/g)].map(match => match[1]),
            ['Front L/R + Sub', 'Center + Sub', 'Side L/R + Sub', 'Rear L/R + Sub']);
        assert.match(summary, /Gated direct lower limit: Left 300 Hz · Right 300 Hz/);
        assert.match(summary, /Gated direct lower limit: 300 Hz/);
        assert.match(summary, /subwoofer routing checked \(ok\)/);
        assert.equal(elements.measurementHybridPrimaryBtn.textContent, 'Show results');

        // An unsaved Auto Sub result would take the graph and "Save current".
        state.measurement.autoSubMeasurements = [{ id: 'auto-before' }, { id: 'auto-after' }];
        Flows.openHybridProfileInConvolver();
        assert.deepEqual(state.measurement.autoSubMeasurements, [],
            'the series models replace an unsaved Auto Sub result');
        const entries = state.measurement.pendingRepeatMeasurements;
        assert.equal(entries.length, 7, 'Front L/R, Center, Side L/R and Rear L/R');
        assert.equal(state.measurement.currentMeasurement, entries[0]);
        assert.equal(state.measurement.currentMeasurementSaved, false);
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

    // One start per listening position crosses group boundaries without moving the mic.
    {
        const { Flows, forms, elements } = makeSeries();
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        for (let index = 0; index < 7; index += 1) {
            await Flows.runHybridWizardSweep();
            assert.equal(forms.length, index + 1, 'nearfield measures only the current speaker');
            assert.equal(wizard.quality?.retry, false);
            assert.deepEqual(wizard.groupProfiles, {}, 'no profile is built before listening captures exist');
        }
        for (const [position, total] of [['mlp', 17], ['left', 24], ['right', 31]]) {
            assert.equal(wizard.sequence[wizard.stepIndex].position, position);
            assert.match(elements.measurementHybridInstruction.textContent, /All speakers will be measured automatically/);
            await Flows.runHybridWizardSweep();
            assert.equal(forms.length, total, `one start completes ${position} across all groups`);
            assert.equal(wizard.quality?.retry, false);
        }
        assert.ok(wizard.profile);
    }

    // A failed integration check is repeated while still at Main.
    for (const [group, requestCount] of [['front', 10], ['side', 14], ['rear', 17]]) {
        const { Flows, forms } = makeSeries({ poorIntegrationOnce: [group] });
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        await runToEnd(Flows, { stopOnError: true });
        assert.equal(wizard.quality?.level, 'error');
        assert.match(wizard.status, /integration check failed/i);
        const integrationStep = wizard.sequence[wizard.stepIndex];
        assert.deepEqual([integrationStep.role, integrationStep.group], ['integration', group]);
        assert.equal(forms.length, requestCount, 'integration is checked before moving away from Main');
        assert.equal(wizard.groupProfiles[group], undefined);
        await runToEnd(Flows);
        assert.equal(forms.length, 9 + 1 + 4 + 9 + 9, 'only the integration step was repeated');
        assert.deepEqual(wizard.profile.parts.map(part => part.scope.group), ['front', 'center', 'side', 'rear']);
        assert.equal(wizard.captures.length, 31, 'the repeated integration capture replaces its predecessor');
    }

    // Cancellation or failure mid-position resumes only the remaining speakers.
    for (const interrupt of [{ cancelOnceAt: 12 }, { failOnceAt: 12 }]) {
        const { Flows, forms } = makeSeries(interrupt);
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        for (let index = 0; index < 8; index += 1) await Flows.runHybridWizardSweep();
        assert.equal(wizard.stepIndex, 11, 'interrupted Side L stays selected at Main');
        assert.equal(wizard.captures.length, 11);
        await Flows.runHybridWizardSweep();
        assert.equal(wizard.stepIndex, 17, 'resume completes Main, without starting Left');
        assert.equal(forms.length, 18);
        await runToEnd(Flows);
        assert.ok(wizard.profile);
        assert.equal(forms.length, 32);
        assert.equal(wizard.captures.length, 31);
    }

    // Back to Main replaces that entire position and uses the latest timing data.
    {
        const { Flows, forms } = makeSeries();
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = Flows.getHybridWizardState();
        for (let index = 0; index < 9; index += 1) await Flows.runHybridWizardSweep();
        assert.equal(wizard.stepIndex, 24);
        wizard.stepIndex = Hybrid.getPreviousPositionIndex(wizard.sequence,
            Hybrid.getPreviousPositionIndex(wizard.sequence, wizard.stepIndex));
        assert.equal(wizard.stepIndex, 7);
        await runToEnd(Flows);
        assert.equal(forms.length, 48);
        assert.equal(wizard.captures.length, 31);
        assert.equal(wizard.profile.parts[0].profile.left.timingMeasurement.id, 'm-25');
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
        assert.deepEqual([wizard.scope.series === true, wizard.captures.length, wizard.sequence.length], [false, 0, 9]);
    }

    // A group without a gated lower limit says so instead of printing NaN, and
    // every pair group keeps its integration numbers one click away.
    {
        const { Flows, elements } = makeSeries({ ungated: ['front'] });
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        const wizard = await runToEnd(Flows);
        assert.ok(wizard.profile);
        const summary = elements.measurementHybridSummary.innerHTML;
        assert.doesNotMatch(summary, /NaN/);
        assert.match(summary, /Gated direct lower limit: Left unavailable · Right unavailable/);
        assert.match(summary, /Gated direct lower limit: 300 Hz/, 'gated groups keep their number');
        // Front, Side and Rear are pairs; Center is one speaker without an L/R check.
        assert.equal((summary.match(/<details class="hybrid-advanced-details">/g) || []).length, 3);
        assert.match(summary, /Consistency status: ok · band /);
    }

    // The single run prints the same text for an ungated response.
    {
        const { Flows, elements } = makeSeries({ ungated: ['front'] });
        Flows.openHybridMeasurementWizard();
        const wizard = await runToEnd(Flows);
        assert.ok(wizard.profile);
        assert.doesNotMatch(elements.measurementHybridSummary.innerHTML, /NaN/);
        assert.match(elements.measurementHybridSummary.innerHTML, /Left: unavailable<br>Right: unavailable/);
    }

    // A single group result also replaces an unsaved Auto Sub result.
    {
        const { Flows, state, calls } = makeSeries();
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('center');
        const wizard = await runToEnd(Flows);
        assert.ok(wizard.profile);
        state.measurement.autoSubMeasurements = [{ id: 'auto-before' }];
        Flows.openHybridProfileInConvolver();
        assert.deepEqual(state.measurement.autoSubMeasurements, []);
        assert.equal(state.measurement.pendingRepeatMeasurements.length, 1);
        assert.deepEqual(calls.assist, ['convolver']);
    }

    // A name typed into the field is kept by a single group result and by a
    // series; the series entries still carry their group in the saved name.
    {
        for (const scope of ['center', 'all']) {
            const { Flows, state } = makeSeries();
            Flows.openHybridMeasurementWizard();
            Flows.selectHybridWizardScope(scope);
            await runToEnd(Flows);
            state.measurement.currentMeasurementName = 'Seat A';
            state.measurement.typedMeasurementName = 'Seat A';
            Flows.openHybridProfileInConvolver();
            assert.equal(state.measurement.currentMeasurementName, 'Seat A', scope);
        }
        const { Flows, state } = makeSeries();
        Flows.openHybridMeasurementWizard();
        Flows.selectHybridWizardScope('all');
        await runToEnd(Flows);
        state.measurement.currentMeasurementName = 'Demo Measurement 4';
        state.measurement.typedMeasurementName = '';
        Flows.openHybridProfileInConvolver();
        assert.equal(state.measurement.currentMeasurementName, 'Advanced', 'a proposed name is replaced');
    }

    // The chips follow the group labels (routing changes them under the same
    // ids) and the row stays in place for a click.
    {
        const { Flows, elements } = makeSeries();
        Flows.openHybridMeasurementWizard();
        const row = elements.measurementHybridScopeRow;
        assert.match(row.innerHTML, /data-tooltip="Front L\/R \+ Sub"/);
        const original = GROUPS.front.label;
        GROUPS.front.label = 'Front L/R';
        try {
            Flows.renderHybridMeasurementWizard();
            assert.match(row.innerHTML, /data-tooltip="Front L\/R"/);
            assert.doesNotMatch(row.innerHTML, /Front L\/R \+ Sub/);
        } finally {
            GROUPS.front.label = original;
        }
    }

    // An Advanced series saves seven runs; each one needs its own colour next to the current green.
    {
        const UI = require('../static/measurement_ui.js');
        const palette = UI.measurementComparePalette;
        assert.ok(palette.length >= 7, 'seven saved series runs');
        assert.equal(new Set(palette).size, palette.length, 'no colour repeats inside the palette');
        assert.equal(palette.includes(UI.measurementCurrentColor), false, 'the current run keeps its own green');
    }

    // The app's initial wizard state has the keys the module's own default has.
    {
        const { Flows, state } = makeSeries();
        delete state.measurement.hybridWizard;
        const moduleKeys = Object.keys(Flows.getHybridWizardState()).sort();
        const appSource = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
        const literal = /\n        hybridWizard: \{\n([\s\S]*?)\n        \},/.exec(appSource);
        assert.ok(literal, 'app.js must declare the initial hybridWizard state');
        const appKeys = [...literal[1].matchAll(/^ {12}(\w+):/gm)].map(match => match[1]).sort();
        assert.deepEqual(appKeys, moduleKeys);
    }

    console.log('hybrid series flow: ok');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
