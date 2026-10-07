#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const index = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const outputState = fs.readFileSync(path.join(root, 'static', 'output_state.js'), 'utf8');
const levelMatch = require('../static/measurement_level_match.js');

// Markup: one Calibration popover holds both calibrations, in the Start Sweep menu pattern.
assert.match(index, /id="measurement-calibration-toggle"[^>]*aria-haspopup="true"[^>]*aria-controls="measurement-calibration-menu">Calibration<\/button>/);
assert.match(index, /id="measurement-calibration-menu"[^>]*class="measurement-workflow-menu-panel hidden"|class="measurement-workflow-menu-panel hidden"[^>]*id="measurement-calibration-menu"/);
assert.match(index, /<div id="measurement-calibration-menu"[\s\S]*id="measurement-spl-calibration-open"[^>]*>SPL Calibration<\/button>[\s\S]*id="measurement-level-match-open"[^>]*>Level Matching<\/button>/);
assert.ok(index.indexOf('measurement_spl_calibration.js?v=') < index.indexOf('measurement_level_match.js?v='));
assert.ok(index.indexOf('measurement_level_match.js?v=') < index.indexOf('app.js?v='));
assert.match(outputState, /'set_level_calibration'/);
assert.match(app, /FXRouteMeasurementLevelMatch\?\.init\(/);
assert.match(app, /FXRouteMeasurementLevelMatch\?\.bindEvents\(\)/);
assert.match(index, /<button id="level-match-reset"[^>]*>Reset to 0 dB<\/button>/);
assert.match(app, /levelMatchReset: document\.getElementById\('level-match-reset'\)/);

// A sub bank keeps only SPL Calibration: Level Matching and Polarity Check are hidden there.
assert.match(app, /\n\s+syncMeasurementCalibrationMenu\(area\);\n\}/, 'renderMeasurementArea syncs the calibration menu');
const calibrationMenuSync = app.match(/function syncMeasurementCalibrationMenu\(area\) \{[\s\S]*?\n\}\n/)?.[0] || '';
assert.match(calibrationMenuSync, /isSubwooferBank\?\.\(area\.bank_id\)/);
assert.match(calibrationMenuSync, /\[elements\.levelMatchOpen, elements\.polarityCheckOpen\]/);
assert.match(calibrationMenuSync, /closest\('\.measurement-workflow-menu-choice'\)\?\.classList\.toggle\('hidden', subwooferArea\)/);
assert.doesNotMatch(calibrationMenuSync, /spl-?[cC]alibration/i, 'SPL Calibration is never hidden');

function element() {
    const listeners = {};
    return {
        textContent: '', innerHTML: '', disabled: false, listeners,
        classList: { remove() {}, add() {} },
        addEventListener(type, handler) { listeners[type] = handler; },
        querySelector: () => null,
    };
}

function harness({ area, status, jobs, applied = { ok: true }, reset = { ok: true, body: { mode: 'stereo', values: { main_l: { bank: 0 }, main_r: { bank: 0 } } } }, blocked = false }) {
    const elements = {
        levelMatchOpen: element(), levelMatchPanel: element(), levelMatchClose: element(),
        levelMatchArea: element(), levelMatchStart: element(), levelMatchReset: element(), levelMatchActionStatus: element(),
        levelMatchResults: element(), levelMatchStatus: element(),
    };
    const calls = [];
    const mutations = [];
    const jobQueue = [...jobs];
    levelMatch.init({
        getElements: () => elements,
        getState: () => ({ measurement: { selectedInputId: 'mic-1', selectedInputKey: 'key', selectedMicInputChannel: '2', selectedCalibrationRef: 'cal-9' } }),
        fetch: async (url, options = {}) => {
            calls.push({ url, options });
            if (url.startsWith('/api/measurements/level-match?')) return { ok: true, json: async () => status };
            if (url === '/api/measurements/level-match/start') return { ok: true, json: async () => ({ job: { id: 'job-1', status: 'running', progress: { step: 1, total: 2, label: 'left' } } }) };
            if (url === '/api/measurements/level-match/reset') return { ok: reset.ok, json: async () => reset.body };
            if (url.includes('/jobs/job-1/cancel')) return { ok: true, json: async () => ({}) };
            if (url.includes('/jobs/job-1')) return { ok: true, json: async () => ({ job: jobQueue.shift() }) };
            throw new Error(`unexpected ${url}`);
        },
        openModal: () => {},
        closeModal: () => {},
        measurementArea: () => area,
        ensureAreaCatalog: async () => {},
        normalizeMeasurementInputChannelSelections: () => {},
        applyMutation: async (kind, fields) => { mutations.push({ kind, fields }); return applied.ok ? {} : null; },
        hasActiveMeasurementJob: () => blocked,
        escapeHtml: (value) => String(value),
        roleLabel: (role) => `label:${role}`,
        sleep: async () => {},
    });
    return { elements, calls, mutations };
}

(async () => {
    assert.equal(levelMatch.formatTrim(-2.04), '-2.0 dB');
    assert.equal(levelMatch.formatTrim(0), '0.0 dB');
    assert.equal(levelMatch.formatTrim(undefined), '—');

    // Measure, then commit the proposed slots through the output-state mutation.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'],
            entries: [{ role: 'main_l', slot: 'bank', valid: true, trim_db: -2.5 }, { role: 'main_r', slot: 'bank', valid: false, trim_db: null }] };
        const done = { id: 'job-1', status: 'completed', result: { mode: 'stereo', values: { main_l: { bank: -2.5 }, main_r: { bank: 0 } } } };
        const { elements, calls, mutations } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [done] });
        await levelMatch.openLevelMatch();
        assert.equal(elements.levelMatchArea.textContent, 'Main L/R');
        assert.equal(elements.levelMatchStart.disabled, false);
        assert.match(elements.levelMatchActionStatus.textContent, /left and right one after the other/);
        assert.match(elements.levelMatchResults.innerHTML, /label:main_l[\s\S]*-2\.5 dB/);
        assert.match(elements.levelMatchResults.innerHTML, /label:main_r[\s\S]*outdated/);
        await levelMatch.startLevelMatch();
        const start = calls.find((call) => call.url === '/api/measurements/level-match/start');
        assert.equal(start.options.method, 'POST');
        assert.equal(start.options.body.get('bank_id'), 'main');
        assert.equal(start.options.body.get('input_id'), 'mic-1');
        assert.equal(start.options.body.get('mic_input_channel'), '2');
        assert.equal(start.options.body.get('calibration_ref'), 'cal-9');
        assert.deepEqual(mutations, [{ kind: 'set_level_calibration', fields: { mode: 'stereo', values: { main_l: { bank: -2.5 }, main_r: { bank: 0 } } } }]);
        assert.match(elements.levelMatchStatus.textContent, /Level Calibration applied/);
        assert.equal(elements.levelMatchStart.textContent, 'Match levels');
    }

    // Surround speakers wait for the Front L/R reference.
    {
        const status = { mode: 'surround', requires_reference: true, reference_ready: false, units: ['center'], entries: [] };
        const { elements } = harness({ area: { bank_id: 'center', label: 'Center', available: true }, status, jobs: [] });
        await levelMatch.openLevelMatch();
        assert.equal(elements.levelMatchStart.disabled, true);
        assert.match(elements.levelMatchActionStatus.textContent, /Measure Front L\/R first/);
    }

    // "All Banks" has no measurement area.
    {
        const { elements, calls } = harness({ area: { bank_id: 'all', label: 'All Banks', available: false }, status: {}, jobs: [] });
        await levelMatch.openLevelMatch();
        assert.equal(elements.levelMatchStart.disabled, true);
        assert.match(elements.levelMatchActionStatus.textContent, /Select a speaker bank/);
        assert.equal(calls.length, 0);
    }

    // A Surround Global group is one speaker plus the subs, not a speaker bank.
    {
        const { elements, calls } = harness({
            area: { bank_id: 'global', label: 'Global · Front L/R + Sub', available: true, group: 'front' },
            status: {}, jobs: [] });
        await levelMatch.openLevelMatch();
        assert.equal(elements.levelMatchStart.disabled, true);
        assert.match(elements.levelMatchActionStatus.textContent, /Select a speaker bank/);
        assert.equal(calls.length, 0);
    }

    // A failed measurement commits nothing and shows the reason.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'], entries: [] };
        const failed = { id: 'job-1', status: 'failed', error: 'Level difference of 75.0 dB at main_l is implausible' };
        const { elements, mutations } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [failed] });
        await levelMatch.openLevelMatch();
        await levelMatch.startLevelMatch();
        assert.deepEqual(mutations, []);
        assert.match(elements.levelMatchStatus.textContent, /implausible/);
    }

    // A rejected commit is reported, not claimed as applied.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'], entries: [] };
        const done = { id: 'job-1', status: 'completed', result: { mode: 'stereo', values: { main_l: { bank: -1 } } } };
        const { elements } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [done], applied: { ok: false } });
        await levelMatch.openLevelMatch();
        await levelMatch.startLevelMatch();
        assert.match(elements.levelMatchStatus.textContent, /could not be applied/);
    }

    // A cancelled job changes nothing.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'], entries: [] };
        const cancelled = { id: 'job-1', status: 'cancelled' };
        const { elements, mutations } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [cancelled] });
        await levelMatch.openLevelMatch();
        await levelMatch.startLevelMatch();
        assert.deepEqual(mutations, []);
        assert.match(elements.levelMatchStatus.textContent, /cancelled\. Nothing was changed/);
    }

    // Reset clears the area on the server, then commits the returned 0 dB slots like a measurement does.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'],
            entries: [{ role: 'main_l', slot: 'bank', valid: true, trim_db: -2.5 }] };
        const { elements, calls, mutations } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [] });
        await levelMatch.openLevelMatch();
        assert.equal(elements.levelMatchReset.disabled, false);
        await levelMatch.resetLevelMatch();
        const reset = calls.find((call) => call.url === '/api/measurements/level-match/reset');
        assert.equal(reset.options.method, 'POST');
        assert.equal(reset.options.body.get('bank_id'), 'main');
        assert.deepEqual(mutations, [{ kind: 'set_level_calibration', fields: { mode: 'stereo', values: { main_l: { bank: 0 }, main_r: { bank: 0 } } } }]);
        assert.match(elements.levelMatchStatus.textContent, /reset to 0 dB/);
        assert.equal(elements.levelMatchReset.disabled, false);
        assert.equal(elements.levelMatchStart.disabled, false);
    }

    // A refused reset commits nothing and shows the reason.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'], entries: [] };
        const { elements, mutations } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [],
            reset: { ok: false, body: { detail: 'Auto Sub Optimize is in progress' } } });
        await levelMatch.openLevelMatch();
        await levelMatch.resetLevelMatch();
        assert.deepEqual(mutations, []);
        assert.match(elements.levelMatchStatus.textContent, /Auto Sub Optimize is in progress/);
    }

    // A rejected commit is reported, not claimed as reset.
    {
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'], entries: [] };
        const { elements } = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [], applied: { ok: false } });
        await levelMatch.openLevelMatch();
        await levelMatch.resetLevelMatch();
        assert.match(elements.levelMatchStatus.textContent, /could not be applied/);
    }

    // No area or another running measurement: Reset is unavailable and does nothing.
    {
        const { elements, calls } = harness({ area: { bank_id: 'all', label: 'All Banks', available: false }, status: {}, jobs: [] });
        await levelMatch.openLevelMatch();
        assert.equal(elements.levelMatchReset.disabled, true);
        await levelMatch.resetLevelMatch();
        assert.equal(calls.length, 0);
        const status = { mode: 'stereo', requires_reference: false, reference_ready: true, units: ['left', 'right'], entries: [] };
        const other = harness({ area: { bank_id: 'main', label: 'Main L/R', available: true }, status, jobs: [], blocked: true });
        await levelMatch.openLevelMatch();
        assert.equal(other.elements.levelMatchReset.disabled, true);
    }

    console.log('level match frontend ok');
})().catch((error) => { console.error(error); process.exit(1); });
