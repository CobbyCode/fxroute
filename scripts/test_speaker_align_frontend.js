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
    } };
    const elements = Object.fromEntries(['LeftBtn', 'RightBtn', 'CancelBtn', 'Group', 'Status', 'Results', 'Sequence']
        .map(key => [`measurementSpeakerAlign${key}`, element()]));
    const calls = [];
    const result = { confirmed: true, committed_revision: 8,
        proposal: { arrival_ms: { right_low: 2, right_high: 5 }, added_delay_ms: { right_low: 3, right_high: 0 }, reference_role: 'right_high' },
        check: { before_spread_ms: 3, max_residual_ms: 0.021, tolerance_ms: 0.25,
            after_arrival_ms: { right_low: 5, right_high: 5.021 } } };
    const context = { console, window: {} };
    vm.createContext(context);
    const shell = fs.readFileSync(require.resolve('../static/index.html'), 'utf8');
    for (const match of shell.matchAll(/src="\/static\/(speaker_align|measurement_flows)\.js\?[^\"]+"/g)) {
        vm.runInContext(fs.readFileSync(require.resolve(`../static/${match[1]}.js`), 'utf8'), context);
    }
    const flows = context.window.FXRouteMeasurementFlows;
    const response = job => ({ ok: true, json: async () => ({ job }) });
    flows.init({ getState: () => state, getElements: () => elements, measurementModeReady: () => true,
        getActiveMeasurementKind: () => state.measurement.activeMeasurementKind,
        api: { startSpeakerAlign: async payload => {
            calls.push(payload);
            assert.equal(elements.measurementSpeakerAlignLeftBtn.disabled, true);
            assert.equal(elements.measurementSpeakerAlignRightBtn.disabled, true);
            return response({ id: 'alignment', side: payload.side, status: 'queued' });
        }, pollSpeakerAlignJob: async () => response({ id: 'alignment', side: 'right', status: 'committed', result }) },
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
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /speaker-align-table-wrap/);
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /speaker-align-table/);
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /5\.021/);
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /3\.000/);
    assert.match(measurementCss, /\.speaker-align-table-wrap\s*\{[^}]*max-width:\s*100%[^}]*overflow-x:\s*auto/);
    assert.match(measurementCss, /\.speaker-align-table\s*\{[^}]*min-width:\s*\d+px/);
    assert.match(elements.measurementSpeakerAlignStatus.textContent, /0\.021/);
    assert.match(elements.measurementSpeakerAlignStatus.textContent, /0\.250/);
    // Saveable time-domain run: Before from planning, After from
    // verification, no extra measurement; reopening renders the same view
    // plus alignment data. Guards the accidental helper removal.
    const runResult = { confirmed: true, committed_revision: 8, side: 'right', sample_rate_hz: 48000,
        proposal: { start_revision: 7, processing_fingerprint: 'test-fingerprint',
            arrival_ms: { right_low: 2, right_high: 5 }, added_delay_ms: { right_low: 3, right_high: 0 }, reference_role: 'right_high',
            way_levels_db: { right_low: -12, right_high: -8 }, added_gain_db: { right_low: 2, right_high: -2 },
            planning_isolation_db: { right_low: 18.5, right_high: 21.0 }, arrival_source: 'shared-planning-take' },
        check: { confirmed: true, reasons: [], warnings: [], before_spread_ms: 3, max_residual_ms: 0.021, tolerance_ms: 0.25,
            after_arrival_ms: { right_low: 5, right_high: 5.021 }, pairs: [],
            gain_spread_db: 0.2, gain_tolerance_db: 1.0, after_way_levels_db: { right_low: -10, right_high: -10.2 },
            way_isolation_db: { right_low: 19.0, right_high: 20.0 }, isolation_margin_db: 19.0 } };
    const run = Speaker.buildSpeakerAlignRun(runResult, {
        side: 'right', jobId: 'job-1', sampleRateHz: 48000, committedRevision: 8,
        params: { input_id: 'mic-1' },
        frequency: { right_low: { trusted_points: [[20, -12], [1000, -10]] } },
    });
    assert.equal(run.schema, 'speaker-align-run-v1');
    assert.equal(run.before.source, 'planning-take');
    assert.equal(run.after.source, 'verification-take');
    assert.deepEqual(run.ways, ['right_high', 'right_low']);
    assert.equal(run.corrections.added_delay_ms.right_low, 3);
    assert.equal(run.metadata.params.input_id, 'mic-1');
    assert.deepEqual(run.frequency.right_low.trusted_points, [[20, -12], [1000, -10]]);
    const view = Speaker.timeDomainView(run);
    assert.deepEqual(view.ways, ['right_high', 'right_low']);
    assert.equal(view.lanes.before.source, 'planning-take');
    assert.equal(view.lanes.after.source, 'verification-take');
    assert.ok(view.window_ms[0] <= 2 && view.window_ms[1] >= 5.021);
    const timeHtml = Speaker.renderSpeakerAlignTimeDomain(run, 'right');
    assert.match(timeHtml, /planning take/);
    assert.match(timeHtml, /verification take/);
    assert.match(timeHtml, /shared ms axis/);
    const measurement = Speaker.runToMeasurement(run, 'Right align');
    assert.equal(measurement.measurement_kind, 'speaker-align-run-v1');
    assert.ok(Array.isArray(measurement.traces) && measurement.traces.length === 1);
    const reopened = Speaker.measurementToRun(measurement);
    assert.deepEqual(reopened.ways, run.ways);
    // Finished results render the time-domain view next to the table.
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /speaker-align-time/);
    assert.match(elements.measurementSpeakerAlignResults.innerHTML, /shared ms axis/);
    state.outputSystem.catalog.modes.stereo.selected_bank = 'all';
    flows.syncSpeakerAlignButton();
    await flows.startSpeakerAlign('left');
    assert.equal(calls.length, 1, 'Hidden alignment must not be startable');
    assert.equal(elements.measurementSpeakerAlignGroup.classList.contains('hidden'), true);
    console.log('Speaker alignment UI flow: passed');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
