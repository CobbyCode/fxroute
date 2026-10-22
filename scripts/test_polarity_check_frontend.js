#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..');
const index = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const polarityCheck = require('../static/measurement_polarity_check.js');

// Markup: the Calibration popover holds the three calibrations in menu order.
assert.match(index, /<div id="measurement-calibration-menu"[\s\S]*id="measurement-spl-calibration-open"[\s\S]*id="measurement-level-match-open"[\s\S]*id="measurement-polarity-check-open"[^>]*>Polarity Check<\/button>/);
assert.match(index, /id="polarity-check-panel"[^>]*class="manage-overlay measurement-overlay hidden"/);
for (const id of ['polarity-check-close', 'polarity-check-area', 'polarity-check-start', 'polarity-check-action-status', 'polarity-check-results', 'polarity-check-status']) {
    assert.ok(index.includes(`id="${id}"`), id);
}
assert.ok(index.indexOf('measurement_level_match.js?v=') < index.indexOf('measurement_polarity_check.js?v='));
assert.ok(index.indexOf('measurement_polarity_check.js?v=') < index.indexOf('app.js?v='));
assert.match(app, /FXRouteMeasurementPolarityCheck\?\.init\(/);
assert.match(app, /FXRouteMeasurementPolarityCheck\?\.bindEvents\(\)/);
assert.match(app, /#measurement-polarity-check-open'\)\) setMeasurementCalibrationMenuOpen\(false\)/);

function element() {
    const listeners = {};
    return {
        textContent: '', innerHTML: '', disabled: false, listeners,
        classList: { remove() {}, add() {} },
        addEventListener(type, handler) { listeners[type] = handler; },
        querySelector: () => null,
    };
}

function harness({ area, status, jobs }) {
    const elements = {
        polarityCheckOpen: element(), polarityCheckPanel: element(), polarityCheckClose: element(),
        polarityCheckArea: element(), polarityCheckStart: element(), polarityCheckActionStatus: element(),
        polarityCheckResults: element(), polarityCheckStatus: element(),
    };
    const calls = [];
    const jobQueue = [...jobs];
    polarityCheck.init({
        getElements: () => elements,
        getState: () => ({ measurement: { selectedInputId: 'mic-1', selectedInputKey: 'key', selectedMicInputChannel: '2', selectedCalibrationRef: 'cal-9' } }),
        fetch: async (url, options = {}) => {
            calls.push({ url, options });
            if (url.startsWith('/api/measurements/polarity-check?')) return { ok: true, json: async () => status };
            if (url === '/api/measurements/polarity-check/start') return { ok: true, json: async () => ({ job: { id: 'job-1', status: 'running', progress: { step: 1, total: 2, label: 'left' } } }) };
            if (url.includes('/jobs/job-1/cancel')) return { ok: true, json: async () => ({}) };
            if (url.includes('/jobs/job-1')) return { ok: true, json: async () => ({ job: jobQueue.shift() }) };
            throw new Error(`unexpected ${url}`);
        },
        openModal: () => {},
        closeModal: () => {},
        measurementArea: () => area,
        ensureAreaCatalog: async () => {},
        normalizeMeasurementInputChannelSelections: () => {},
        hasActiveMeasurementJob: () => false,
        escapeHtml: (value) => String(value),
        roleLabel: (role) => `label:${role}`,
        sleep: async () => {},
    });
    return { elements, calls };
}

const MAIN = { bank_id: 'main', label: 'Main L/R', available: true };
const STATUS = { mode: 'stereo', units: ['left', 'right'], job: null };

function completed(rows, extra = {}) {
    return { id: 'job-1', status: 'completed', bank_id: 'main', result: { mode: 'stereo', rows, all_reversed: false, ...extra } };
}

function row(roles, verdict, extra = {}) {
    return { label: 'left', roles, verdict, reason: '', inverted: false, ...extra };
}

(async () => {
    assert.equal(polarityCheck.verdictText({ verdict: 'correct' }), 'Correct');
    assert.equal(polarityCheck.verdictText({ verdict: 'reversed' }), 'Reversed');
    assert.equal(polarityCheck.verdictText({ verdict: 'inconclusive', reason: 'weak_signal' }), 'Too quiet');
    assert.equal(polarityCheck.verdictText({ verdict: 'inconclusive', reason: 'no_arrival' }), 'No sweep heard');
    assert.equal(polarityCheck.verdictText({ verdict: 'inconclusive', reason: 'dsp_filter_in_band' }), 'Filter in band');
    assert.equal(polarityCheck.verdictText({ verdict: 'inconclusive', reason: 'dsp_unknown' }), 'DSP not checkable');

    // A run shows one verdict per speaker and sends nothing but the measurement requests.
    {
        const rows = [row(['main_l'], 'correct'), row(['main_r'], 'reversed', { label: 'right' })];
        const { elements, calls } = harness({ area: MAIN, status: STATUS, jobs: [completed(rows)] });
        await polarityCheck.openPolarityCheck();
        assert.equal(elements.polarityCheckArea.textContent, 'Main L/R');
        assert.equal(elements.polarityCheckStart.disabled, false);
        assert.match(elements.polarityCheckActionStatus.textContent, /left and right one after the other/);
        await polarityCheck.startPolarityCheck();
        const start = calls.find((call) => call.url === '/api/measurements/polarity-check/start');
        assert.equal(start.options.method, 'POST');
        assert.equal(start.options.body.get('bank_id'), 'main');
        assert.equal(start.options.body.get('input_id'), 'mic-1');
        assert.equal(start.options.body.get('mic_input_channel'), '2');
        assert.equal(start.options.body.get('calibration_ref'), 'cal-9');
        assert.ok(calls.every((call) => call.url.startsWith('/api/measurements/polarity-check')), 'diagnostic only: no output change');
        assert.match(elements.polarityCheckResults.innerHTML, /label:main_l[\s\S]*polarity-check-verdict-correct">Correct/);
        assert.match(elements.polarityCheckResults.innerHTML, /label:main_r[\s\S]*polarity-check-verdict-reversed">Reversed/);
        assert.match(elements.polarityCheckStatus.textContent, /reversed speaker plays its first pulse inverted/i);
        assert.equal(elements.polarityCheckStart.textContent, 'Check polarity');
    }

    // Every speaker reversed does not prove a wiring fault: an inverted microphone reads the same.
    {
        const rows = [row(['main_l'], 'reversed'), row(['main_r'], 'reversed', { label: 'right' })];
        const { elements } = harness({ area: MAIN, status: STATUS, jobs: [completed(rows, { all_reversed: true })] });
        await polarityCheck.openPolarityCheck();
        await polarityCheck.startPolarityCheck();
        assert.match(elements.polarityCheckStatus.textContent, /inverted microphone or input does that too/);
        assert.doesNotMatch(elements.polarityCheckStatus.textContent, /Swap its/);
    }

    // A speaker the sweep never reached gets no verdict and a hint, not a guess.
    {
        const rows = [row(['main_l'], 'inconclusive', { reason: 'no_arrival' }), row(['main_r'], 'correct', { label: 'right' })];
        const { elements } = harness({ area: MAIN, status: STATUS, jobs: [completed(rows)] });
        await polarityCheck.openPolarityCheck();
        await polarityCheck.startPolarityCheck();
        assert.match(elements.polarityCheckResults.innerHTML, /polarity-check-verdict-inconclusive">No sweep heard/);
        assert.match(elements.polarityCheckStatus.textContent, /did not reach the microphone/);
    }

    // A DSP stage that rotates the measured range, or cannot be judged, withholds the verdict and names the stage.
    {
        const rows = [
            row(['left_high'], 'inconclusive', { reason: 'dsp_filter_in_band', guard: { state: 'rotates', dsp_cos: -0.5, stage: 'left_high crossover' } }),
            row(['main_r'], 'inconclusive', { label: 'right', reason: 'dsp_unknown', guard: { state: 'unknown', dsp_cos: null, stage: 'global loudness' } }),
        ];
        const { elements } = harness({ area: MAIN, status: STATUS, jobs: [completed(rows)] });
        await polarityCheck.openPolarityCheck();
        await polarityCheck.startPolarityCheck();
        assert.match(elements.polarityCheckResults.innerHTML, /polarity-check-verdict-inconclusive">Filter in band/);
        assert.match(elements.polarityCheckResults.innerHTML, /polarity-check-verdict-inconclusive">DSP not checkable/);
        const status = elements.polarityCheckStatus.textContent;
        assert.match(status, /The left_high crossover rotates the phase of the measured range/);
        assert.match(status, /The global loudness may rotate the phase of the measured range/);
        assert.equal((status.match(/select Direct for Global/g) || []).length, 2);
        assert.doesNotMatch(status, /All checked speakers have correct polarity|Swap its/);
    }

    // Without a named stage the hints stay general; a changed DSP state asks for a repeat.
    {
        const hint = (reason, stage) => polarityCheck.summarize({ rows: [row(['main_l'], 'inconclusive', { reason, guard: { state: 'unknown', stage } })] });
        assert.match(hint('dsp_filter_in_band', ''), /^A filter rotates the phase/);
        assert.match(hint('dsp_unknown', 'DSP path'), /could not be checked for phase effects\. Select Direct for Global and repeat/);
        assert.equal(hint('dsp_unknown', 'DSP path changed'), 'The DSP settings changed during the check, so the polarity cannot be judged. Repeat the check.');
        assert.match(polarityCheck.summarize({ rows: [row(['main_l'], 'inconclusive', { reason: 'dsp_unknown' })] }), /could not be checked for phase effects/);
    }

    // Wiring polarity, an inverted output and a multi-way side are labelled.
    {
        const rows = [row(['left_low', 'left_high'], 'correct', { inverted: true }), row(['right_low', 'right_high'], 'correct', { label: 'right' })];
        const { elements } = harness({ area: MAIN, status: STATUS, jobs: [completed(rows)] });
        await polarityCheck.openPolarityCheck();
        await polarityCheck.startPolarityCheck();
        assert.match(elements.polarityCheckResults.innerHTML, /Left \(all ways\)[\s\S]*output inverted/);
        assert.match(elements.polarityCheckResults.innerHTML, /Right \(all ways\)/);
        assert.equal(elements.polarityCheckStatus.textContent, 'All checked speakers have correct polarity.');
    }

    // The newest finished job of this area shows again when the dialog reopens; another area's does not.
    {
        const job = completed([row(['main_l'], 'correct')]);
        const { elements } = harness({ area: MAIN, status: { ...STATUS, job }, jobs: [] });
        await polarityCheck.openPolarityCheck();
        assert.match(elements.polarityCheckResults.innerHTML, /label:main_l/);
    }
    {
        const job = { ...completed([row(['rear_l'], 'correct')]), bank_id: 'rear' };
        const { elements } = harness({ area: MAIN, status: { ...STATUS, job }, jobs: [] });
        await polarityCheck.openPolarityCheck();
        assert.equal(elements.polarityCheckResults.innerHTML, '');
    }

    // "All Banks" has no measurement area.
    {
        const { elements, calls } = harness({ area: { bank_id: 'all', label: 'All Banks', available: false }, status: {}, jobs: [] });
        await polarityCheck.openPolarityCheck();
        assert.equal(elements.polarityCheckStart.disabled, true);
        assert.match(elements.polarityCheckActionStatus.textContent, /Select a speaker bank/);
        assert.equal(calls.length, 0);
    }

    // Aggregate contexts and subs cannot start a wiring check.
    for (const area of [
        { bank_id: 'global', label: 'Global', available: true },
        { bank_id: 'all', label: 'All Banks', available: true, importable: false },
        { bank_id: 'sub', label: 'Sub L/R', available: true },
        { bank_id: 'global', label: 'Global · Front L/R + Sub', available: true, group: 'front' },
    ]) {
        const { elements, calls } = harness({
            area,
            status: {}, jobs: [] });
        await polarityCheck.openPolarityCheck();
        assert.equal(elements.polarityCheckStart.disabled, true);
        assert.match(elements.polarityCheckActionStatus.textContent, /Select a speaker bank/);
        assert.equal(calls.length, 0);
    }

    // A failed measurement shows the reason and no result.
    {
        const failed = { id: 'job-1', status: 'failed', error: 'The measurement carried no polarity reading' };
        const { elements } = harness({ area: MAIN, status: STATUS, jobs: [failed] });
        await polarityCheck.openPolarityCheck();
        await polarityCheck.startPolarityCheck();
        assert.match(elements.polarityCheckStatus.textContent, /no polarity reading/);
        assert.equal(elements.polarityCheckResults.innerHTML, '');
    }

    // A cancelled job reports it.
    {
        const { elements } = harness({ area: MAIN, status: STATUS, jobs: [{ id: 'job-1', status: 'cancelled' }] });
        await polarityCheck.openPolarityCheck();
        await polarityCheck.startPolarityCheck();
        assert.equal(elements.polarityCheckStatus.textContent, 'Polarity Check cancelled.');
        assert.equal(elements.polarityCheckStart.textContent, 'Check polarity');
    }

    console.log('polarity check frontend ok');
})().catch((error) => { console.error(error); process.exit(1); });
