#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const Speaker = require('../static/speaker_align.js');
const measurementCss = fs.readFileSync(require.resolve('../static/css/_measurement.css'), 'utf8');

function catalog(ways = ['low', 'high']) {
    const roles = ['left', 'right'].flatMap(side => ways.map(way => `${side}_${way}`));
    return { active_mode: 'stereo', modes: { stereo: {
        crossover_enabled: true, selected_bank: 'global',
        topology: { way_count: ways.length, issues: [], roles,
            left_ways: roles.filter(role => role.startsWith('left_')),
            right_ways: roles.filter(role => role.startsWith('right_')) },
        processing: Object.fromEntries(roles.map(role => [role, {
            highpass: role.endsWith('_low') ? null : { frequency_hz: 1000 },
            lowpass: role.endsWith('_high') ? null : { frequency_hz: 3000 },
        }])),
    } } };
}

function element() {
    const classes = new Set();
    return { disabled: false, textContent: '', innerHTML: '',
        classList: { add: c => classes.add(c), remove: c => classes.delete(c),
            toggle: (c, on) => on ? classes.add(c) : classes.delete(c), contains: c => classes.has(c) } };
}

async function main() {
    for (const ways of [['low', 'high'], ['low', 'mid', 'high'], ['low', 'low_mid', 'mid', 'high']]) {
        assert.equal(Speaker.speakerAlignVisible(catalog(ways)), true);
    }
    for (const selection of ['low', 'left_low', 'all']) {
        const value = catalog(); value.modes.stereo.selected_bank = selection;
        assert.equal(Speaker.speakerAlignVisible(value), false);
    }
    const invalid = catalog(); invalid.modes.stereo.topology.issues = ['Incomplete routing'];
    assert.equal(Speaker.speakerAlignVisible(invalid), false);
    invalid.modes.stereo.topology.issues = [];
    invalid.modes.stereo.processing.left_low.lowpass = null;
    assert.equal(Speaker.speakerAlignVisible(invalid), false);
    assert.equal(Speaker.speakerAlignVisible(null), false);

    const state = { outputSystem: { catalog: catalog() }, measurement: {
        selectedInputId: 'mic-1', selectedMicInputChannel: '1', selectedReferenceInputChannel: '',
        measurementView: 'freq',
    } };
    const elements = Object.fromEntries(['LeftBtn', 'RightBtn', 'CancelBtn', 'Group', 'Status', 'Results', 'Sequence']
        .map(key => [`measurementSpeakerAlign${key}`, element()]));
    const calls = [];
    const take = (name, takeName) => ({ id: `sweep-${name}`, name: `Speaker Align Right · ${name}`, channel: 'right',
        measurement_kind: 'sweep-response-v3', speaker_align_take: { side: 'right', take: takeName },
        traces: [{ kind: 'sweep-response', role: 'trusted', points: [[20, -3], [20000, -6]] }],
        review_traces: [{ kind: 'sweep-response-review', role: 'raw-review', points: [[20, -4], [20000, -7]] }],
        analysis: { impulse_response: { preview: { points: [[-2, 0], [0, 1], [30, 0]] } } } });
    const result = { confirmed: true, committed_revision: 8, side: 'right', dry_run: false,
        proposal: { arrival_ms: { right_low: 2, right_high: 5 }, added_delay_ms: { right_low: 3, right_high: 0 },
            added_gain_db: { right_low: 2, right_high: -2 }, reference_role: 'right_high',
            planning_isolation_db: { right_low: 18.5, right_high: null } },
        check: { confirmed: true, warnings: [], before_spread_ms: 3, max_residual_ms: 0.021, tolerance_ms: 0.25,
            after_arrival_ms: { right_low: 5, right_high: 5.021 },
            gain_spread_db: 0.2, before_gain_spread_db: 0.4, gain_tolerance_db: 1.0,
            way_isolation_db: { right_low: 19.0, right_high: null } },
        measurements: { before: take('Before (planning)', 'before'), after: take('After (verification)', 'after') } };
    const context = { console, window: {} };
    vm.createContext(context);
    const shell = fs.readFileSync(require.resolve('../static/index.html'), 'utf8');
    for (const match of shell.matchAll(/src="\/static\/(speaker_align|measurement_flows)\.js\?[^\"]+"/g)) {
        vm.runInContext(fs.readFileSync(require.resolve(`../static/${match[1]}.js`), 'utf8'), context);
    }
    const flows = context.window.FXRouteMeasurementFlows;
    for (const removed of ['saveSpeakerAlignRun', 'openSpeakerAlignRunById', 'renderSpeakerAlignResultActions',
        'listSpeakerAlignRuns', 'isSpeakerAlignRunEntry']) {
        assert.equal(flows[removed], undefined, `${removed} is gone with the separate run flow`);
    }
    for (const removed of ['timeDomainView', 'renderSpeakerAlignTimeDomain', 'buildSpeakerAlignRun',
        'runToMeasurement', 'measurementToRun']) {
        assert.equal(Speaker[removed], undefined, `${removed} is gone with the time-domain graph`);
    }
    const response = job => ({ ok: true, json: async () => ({ job }) });
    let pollJob = { id: 'alignment', side: 'right', status: 'committed', result };
    const speakerApi = { startSpeakerAlign: async payload => {
        calls.push(payload);
        assert.equal(elements.measurementSpeakerAlignLeftBtn.disabled, true);
        assert.equal(elements.measurementSpeakerAlignRightBtn.disabled, true);
        return response({ id: 'alignment', side: payload.side, status: 'queued' });
    }, pollSpeakerAlignJob: async () => response(pollJob) };
    const normalized = [];
    flows.init({ getState: () => state, getElements: () => elements, measurementModeReady: () => true,
        getActiveMeasurementKind: () => state.measurement.activeMeasurementKind,
        normalizeMeasurementEntry: (measurement, index) => { normalized.push(index); return { ...measurement, normalized: true }; },
        setMeasurementGraphView: view => { state.measurement.measurementView = view; },
        api: speakerApi,
    });
    flows.syncSpeakerAlignButton();
    assert.equal(elements.measurementSpeakerAlignGroup.classList.contains('hidden'), false);
    await flows.startSpeakerAlign('right');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].side, 'right');
    assert.equal(calls[0].mic_input_channel, '1');
    assert.equal(calls[0].reference_input_channel, '');
    assert.equal(calls[0].reference_id, 'fxroute_dsp_sink.monitor');
    assert.equal(calls[0].dry_run, false);

    // Compact numbers in the Speaker Align section: delay, gain, isolation,
    // verification and status; no time-domain graph there any more.
    const html = elements.measurementSpeakerAlignResults.innerHTML;
    assert.match(html, /speaker-align-table-wrap/);
    assert.match(html, /<th scope="col">Delay<\/th><th scope="col">Gain<\/th><th scope="col"[^>]*>Isolation<\/th>/);
    assert.match(html, /Right speaker · Verified · committed rev 8/);
    assert.match(html, /<th scope="row">Low<\/th><td>\+3\.000 ms<\/td><td>\+2\.00 dB<\/td><td>18\.5 → 19\.0 dB<\/td>/);
    assert.match(html, /<th scope="row">High · ref<\/th><td>\+0\.000 ms<\/td><td>-2\.00 dB<\/td><td>—<\/td>/);
    assert.match(html, /Verification: spread 3\.000 → 0\.021 ms \(limit 0\.250 ms\) · level spread 0\.40 → 0\.20 dB \(advisory 1\.00 dB\)/);
    assert.doesNotMatch(html, /speaker-align-time|<svg|Before level|data-speaker-align-save/);
    // The outcome goes to the panel status line once; the feature line next
    // to the buttons is back to its idle note (empty in this harness).
    assert.equal(state.measurement.statusText, 'Speaker Align Right verified and applied.');
    assert.equal(elements.measurementSpeakerAlignStatus.textContent, '');
    assert.match(measurementCss, /\.speaker-align-table-wrap\s*\{[^}]*max-width:\s*100%[^}]*overflow-x:\s*auto/);
    assert.match(measurementCss, /\.speaker-align-table\s*\{[^}]*min-width:\s*\d+px/);
    assert.match(measurementCss, /\.speaker-align-verification\s*\{/);
    assert.doesNotMatch(measurementCss, /\.speaker-align-time|\.speaker-align-result-actions/);
    assert.doesNotMatch(shell, /measurement-speaker-align-actions|measurement-speaker-align-save|speaker-align-run-row/,
        'no separate align-run controls');

    // Before (planning) and After (verification) are the pending pair of the
    // normal measurement flow, shown in the IR view and saved via Save current.
    // The pair is built inside the vm realm; compare as a plain array.
    assert.deepEqual(Array.from(state.measurement.pendingRepeatMeasurements, item => item.id),
        ['sweep-Before (planning)', 'sweep-After (verification)']);
    assert.ok(state.measurement.pendingRepeatMeasurements.every(item => item.normalized));
    assert.deepEqual(normalized, [0, 1]);
    assert.equal(state.measurement.currentMeasurement.id, 'sweep-Before (planning)');
    assert.equal(state.measurement.currentMeasurementName, 'Speaker Align Right');
    assert.equal(state.measurement.currentMeasurementSaved, false);
    assert.equal(state.measurement.measurementView, 'ir');
    assert.equal(state.measurement.reviewVisibilityById['sweep-After (verification)'], true);
    assert.deepEqual(Speaker.takeMeasurements({ measurements: { after: result.measurements.after,
        before: { id: 'empty', traces: [] } } }).map(item => item.id), ['sweep-After (verification)']);
    assert.deepEqual(Speaker.takeMeasurements({}), []);

    // Before/After is one current run like an L/R pair: the next run
    // replaces the unsaved pair. Saved runs keep their checkbox visibility;
    // a new run never changes it.
    const savedPair = Array.from(state.measurement.pendingRepeatMeasurements, item => ({ ...item, id: `saved-${item.id}` }));
    const roomSweep = { id: 'saved-room', name: 'Room sweep', traces: [] };
    state.measurement.measurements = [...savedPair, roomSweep];
    state.measurement.visibilityById = { 'saved-sweep-Before (planning)': true,
        'saved-sweep-After (verification)': true, 'saved-room': true };
    state.measurement.pendingRepeatMeasurements = [];
    state.measurement.currentMeasurement = null;
    await flows.startSpeakerAlign('right');
    assert.deepEqual({ ...state.measurement.visibilityById }, { 'saved-sweep-Before (planning)': true,
        'saved-sweep-After (verification)': true, 'saved-room': true });
    assert.deepEqual(Array.from(state.measurement.measurements, item => item.id),
        ['saved-sweep-Before (planning)', 'saved-sweep-After (verification)', 'saved-room']);
    assert.deepEqual(Array.from(state.measurement.pendingRepeatMeasurements, item => item.id),
        ['sweep-Before (planning)', 'sweep-After (verification)']);
    assert.equal(state.measurement.currentMeasurement.id, 'sweep-Before (planning)');
    assert.equal(state.measurement.currentMeasurementSaved, false);
    assert.equal(flows.hideSpeakerAlignTakes, undefined, 'no Speaker Align special case for saved runs');
    state.measurement.measurements = [];
    state.measurement.visibilityById = {};

    // Trial and unconfirmed outcomes read as such in the compact caption.
    assert.match(Speaker.renderSpeakerAlignResult({ ...result, committed_revision: null, dry_run: true }, 'right'),
        /Right speaker · Trial confirmed · not committed/);
    assert.match(Speaker.renderSpeakerAlignResult({ ...result, committed_revision: null, confirmed: false }, 'left'),
        /Left speaker · Not verified · previous delays kept/);
    assert.equal(Speaker.renderSpeakerAlignResult({ proposal: null }, 'left'), '');

    // A failed run clears the numbers but leaves the pending pair alone: it
    // is unsaved measurement data, not transient status.
    pollJob = { id: 'alignment', side: 'right', status: 'failed', error: 'mic unplugged' };
    state.measurement.speakerAlignInFlight = false;
    state.measurement.startInFlight = false;
    state.measurement.activeMeasurementKind = '';
    await flows.startSpeakerAlign('right');
    assert.equal(elements.measurementSpeakerAlignResults.innerHTML, '');
    assert.equal(state.measurement.statusText, 'Speaker Align Right failed: mic unplugged');
    assert.equal(state.measurement.pendingRepeatMeasurements.length, 2);
    pollJob = { id: 'alignment', side: 'right', status: 'committed', result };

    // Per-side loopback references: a right run carries the right loopback
    // in the start payload, never only the shared/left one.
    state.measurement.selectedReferenceInputChannel = '7';
    state.measurement.selectedReferenceInputChannelLeft = '7';
    state.measurement.selectedReferenceInputChannelRight = '8';
    state.measurement.speakerAlignInFlight = false;
    state.measurement.startInFlight = false;
    state.measurement.activeMeasurementKind = '';
    state.measurement.activeJobId = '';
    flows.init({ getSelectedMeasurementInputChannelCount: () => 18, api: speakerApi });
    await flows.startSpeakerAlign('right');
    const splitCall = calls.at(-1);
    assert.equal(splitCall.reference_input_channel, '7');
    assert.equal(splitCall.reference_input_channel_left, '7');
    assert.equal(splitCall.reference_input_channel_right, '8');
    assert.equal(splitCall.reference_id, 'mic-1:ch8:upstream');
    const startedRuns = calls.length;
    state.outputSystem.catalog.modes.stereo.selected_bank = 'all';
    flows.syncSpeakerAlignButton();
    await flows.startSpeakerAlign('left');
    assert.equal(calls.length, startedRuns, 'Hidden alignment must not be startable');
    assert.equal(elements.measurementSpeakerAlignGroup.classList.contains('hidden'), true);
    timelineViewChecks(take);
    console.log('Speaker alignment UI flow: passed');
}

// Before and After share one time base: zero is the reference way, so the
// delayed way moves from minus its delay onto it.
function timelineViewChecks(take) {
    const pulse = arrival => [[arrival - 0.1, 0], [arrival, 1], [arrival + 0.1, 0]];
    const withTimeline = (measurement, arrivals, current = true) => ({
        ...measurement, current, graphColor: current ? '#22c55e' : '#60a5fa',
        analysis: { ...measurement.analysis, speaker_align_timeline: {
            schema: 'fxroute.speaker-align-timeline.v1', time_origin: 'reference-way-arrival',
            reference_role: 'right_high', sample_rate: 48000, arrival_ms: arrivals,
            full_band: pulse(0),
            ways: Object.fromEntries(Object.entries(arrivals).map(([role, arrival]) => [role, pulse(arrival)])),
        } } });
    const before = withTimeline(take('Before (planning)', 'before'), { right_low: -0.375, right_high: 0 });
    const after = withTimeline(take('After (verification)', 'after'), { right_low: 0.020833, right_high: 0 });

    const timeline = Speaker.takeTimeline(before);
    assert.equal(timeline.referenceRole, 'right_high');
    assert.deepEqual(timeline.ways.map(way => [way.label, way.arrivalMs]), [['Low', -0.375], ['High', 0]]);
    assert.equal(Speaker.takeTimeline(take('Before (planning)', 'before')), null);
    const unanchored = withTimeline(take('Before', 'before'), { right_low: -0.375, right_high: 0 });
    unanchored.analysis.speaker_align_timeline.reference_role = 'right_mid';
    assert.equal(Speaker.takeTimeline(unanchored), null);

    const view = Speaker.timelineView([before, after], { widthPx: 700 });
    assert.deepEqual(view.lanes.map(lane => lane.label), ['Right · Before', 'Right · After']);
    assert.ok(view.minMs < -0.375 && view.maxMs > 0.020833, 'both takes fit one axis');
    assert.ok(view.ticks.includes(0));
    assert.equal(Speaker.timelineSpreadText(view.lanes[0]), 'spread 0.375 ms');
    assert.equal(Speaker.timelineSpreadText(view.lanes[1]), 'residual 0.021 ms');
    assert.equal(Speaker.timelineSummary(view),
        'Timing: 0 ms = High arrival (not delayed) · Right · Before spread 0.375 ms · Right · After residual 0.021 ms');
    assert.equal(Speaker.timelineHoverText(view, 0, -0.4), 'Right · Before · −0.400 ms · Low arrival −0.375 ms');
    assert.equal(Speaker.timelineHoverText(view, 1, 0.018), 'Right · After · +0.018 ms · Low arrival +0.021 ms');

    // Each entry decides its own part of the IR graph: an Align take with a
    // timeline is always a timing lane, everything else stays in the normal
    // IR overlay, whatever the order or the other visible entries.
    const plain = { ...take('Plain', 'before'), speaker_align_take: undefined, current: false };
    const savedBefore = withTimeline(before, { right_low: -0.375, right_high: 0 }, false);
    const lanes = parts => parts.timeline.lanes.map(lane => [lane.label, lane.ways.map(way => way.arrivalMs)]);
    const alone = Speaker.irParts([before, after]);
    for (const order of [[plain, before, after], [before, plain, after], [before, after, plain]]) {
        const parts = Speaker.irParts(order);
        assert.deepEqual(lanes(parts), lanes(alone));
        assert.deepEqual(parts.plainEntries.map(entry => entry.name), [plain.name]);
    }
    assert.deepEqual(Speaker.irParts([plain]), { timeline: null, plainEntries: [plain] });
    // Lanes keep the graph's entry order (current run, then saved runs in
    // saved-list order); only within one pair Before goes above After.
    const withSaved = Speaker.irParts([before, after, plain, savedBefore]);
    assert.deepEqual(withSaved.timeline.lanes.map(lane => lane.label),
        ['Right · Before', 'Right · After', 'Right · Before (saved)']);
    const savedAfter = withTimeline(after, { right_low: 0.020833, right_high: 0 }, false);
    const leftAfter = { ...savedAfter, speaker_align_take: { side: 'left', take: 'after' } };
    const labels = entries => Speaker.irParts(entries).timeline.lanes.map(lane => lane.label);
    // The saved list is newest first, so a saved pair arrives After, Before.
    assert.deepEqual(labels([before, after, savedAfter, savedBefore, savedAfter, savedBefore]),
        ['Right · Before', 'Right · After', 'Right · Before (saved)', 'Right · After (saved)',
            'Right · Before (saved)', 'Right · After (saved)']);
    // No reordering beyond that: other sides, a current After next to a
    // saved Before, and lone takes stay where they are.
    assert.deepEqual(labels([leftAfter, savedBefore]), ['Left · After (saved)', 'Right · Before (saved)']);
    assert.deepEqual(labels([before, after, savedBefore]), ['Right · Before', 'Right · After', 'Right · Before (saved)']);
    assert.deepEqual(labels([after, savedBefore]), ['Right · After', 'Right · Before (saved)']);
    assert.doesNotMatch(Speaker.timelineSummary(withSaved.timeline), /hidden/);
    // A take without timeline data (saved before timelines existed) stays in
    // the normal IR overlay: there is no shared time base to draw it on.
    assert.deepEqual(Speaker.irParts([take('Old', 'before')]).plainEntries.length, 1);
    // Narrow graphs get fewer ticks.
    assert.ok(Speaker.timelineView([before, after], { widthPx: 260 }).ticks.length < view.ticks.length);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
