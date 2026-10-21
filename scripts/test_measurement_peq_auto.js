#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// PEQ Auto: the summed filter curve is judged against the target on
// synthetic cases and on the real demo measurements, and the editor flow
// only ever loads an editable proposal into F1-F12.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const dsp = require('../static/measurement_dsp.js');
const ui = require('../static/measurement_ui.js');
const auto = require('../static/measurement_peq_auto.js');
const editor = require('../static/measurement_peq_editor.js');

const NEUTRAL = ui.measurementConvolverCurves.neutral.points;
const HARMAN = ui.measurementConvolverCurves.harman.points;
const BK = ui.measurementConvolverCurves.bk.points;
// Same grid as the analyzer's trusted trace: 192 log points, 20 Hz-20 kHz.
const FREQS = Array.from({ length: 192 }, (_, index) => 20 * (1000 ** (index / 191)));

function bell(freqHz, gainDb, q) {
    return { type: 'bell', freqHz, gainDb, q };
}

function synth({ target = NEUTRAL, levelDb = 0, features = [], ripple = 0.3 } = {}) {
    return FREQS.map((frequency) => {
        let level = dsp.getMeasurementConvolverCurveDbFromPoints(target, frequency) + levelDb;
        level += ripple * Math.sin(2 * Math.PI * Math.log2(frequency) * 2.7);
        features.forEach((feature) => { level += dsp.getMeasurementPeqFilterMagnitude(feature, frequency); });
        return [frequency, Math.round(level * 1000) / 1000];
    });
}

function peqSum(filters, frequency) {
    return filters.reduce((sum, filter) => sum + dsp.getMeasurementPeqFilterMagnitude(filter, frequency), 0);
}

function median(values) {
    const sorted = [...values].sort((left, right) => left - right);
    return sorted[Math.floor(sorted.length / 2)];
}

// Independent judge: measured + PEQ at 1/6 octave against the target over
// [lowHz, highHz], with the free level offset that best explains the curve.
function judge(points, filters, target, lowHz = 40, highHz = 12000) {
    const deviation = (curve) => dsp.smoothMeasurementTracePoints(curve, '1/6-oct')
        .filter(([frequency]) => frequency >= lowHz && frequency <= highHz)
        .map(([frequency, level]) => level - dsp.getMeasurementConvolverCurveDbFromPoints(target, frequency));
    const stats = (values) => {
        const level = median(values);
        const centered = values.map((value) => value - level);
        return {
            rmsDb: Math.sqrt(centered.reduce((sum, value) => sum + (value * value), 0) / centered.length),
            maxDb: Math.max(...centered.map(Math.abs)),
        };
    };
    const corrected = points.map(([frequency, level]) => [frequency, level + peqSum(filters, frequency)]);
    return { before: stats(deviation(points)), after: stats(deviation(corrected)) };
}

function suggest(points, target = NEUTRAL, options = {}) {
    const result = auto.suggestMeasurementPeqFilters({ sources: [{ points }], targetPoints: target, ...options });
    assertSafe(result);
    return result;
}

// Limits every suggestion keeps, whatever the input.
function assertSafe(result, label = '') {
    assert.ok(result.filters.length <= 12, `${label}: at most 12 filters`);
    result.filters.forEach((filter) => {
        assert.ok(['bell', 'low_shelf', 'high_shelf'].includes(filter.type), `${label}: type ${filter.type}`);
        assert.ok(Number.isInteger(filter.freqHz) && filter.freqHz >= 20 && filter.freqHz <= 20000, `${label}: frequency`);
        assert.ok(filter.gainDb >= -15 && filter.gainDb <= 6, `${label}: gain ${filter.gainDb}`);
        assert.ok(Math.abs(filter.gainDb) >= 0.5, `${label}: no negligible filter`);
        assert.ok(filter.q >= 0.3 && filter.q <= 10, `${label}: Q ${filter.q}`);
    });
    for (const frequency of FREQS) {
        const total = peqSum(result.filters, frequency);
        assert.ok(total <= 6.05, `${label}: total boost ${total.toFixed(2)} dB at ${frequency.toFixed(0)} Hz`);
        assert.ok(total >= -18.05, `${label}: total cut ${total.toFixed(2)} dB at ${frequency.toFixed(0)} Hz`);
    }
}

function nearest(filters, frequency) {
    return [...filters].sort((left, right) => Math.abs(Math.log2(left.freqHz / frequency)) - Math.abs(Math.log2(right.freqHz / frequency)))[0];
}

// --- Synthetic cases -------------------------------------------------------

// Already matching: no filters, neutral or a sloped target at any level.
{
    const flat = suggest(synth());
    assert.equal(flat.reason, 'matching');
    assert.equal(flat.filters.length, 0);
    const harman = suggest(synth({ target: HARMAN, levelDb: 5 }), HARMAN);
    assert.equal(harman.reason, 'matching', 'a curve on its sloped target needs no filters');
}

// Strong narrow peak (room mode): one deep cut at the mode, nothing else.
const modePoints = synth({ features: [bell(63, 10, 6)] });
const mode = suggest(modePoints);
{
    assert.ok(mode.filters.length >= 1 && mode.filters.length <= 2, `mode: ${mode.filters.length} filters`);
    const cut = nearest(mode.filters, 63);
    assert.ok(Math.abs(Math.log2(cut.freqHz / 63)) < 1 / 6, `mode cut at ${cut.freqHz} Hz`);
    assert.ok(cut.gainDb <= -7, `mode cut depth ${cut.gainDb}`);
    const verdict = judge(modePoints, mode.filters, NEUTRAL);
    assert.ok(verdict.after.rmsDb <= 0.4 * verdict.before.rmsDb, `mode rms ${verdict.before.rmsDb} -> ${verdict.after.rmsDb}`);
    assert.ok(verdict.after.maxDb <= 2, `mode max ${verdict.after.maxDb}`);
}

// Broad deviation: one wide filter, not a row of narrow ones.
{
    const points = synth({ features: [bell(1000, 4, 0.6)] });
    const result = suggest(points);
    assert.ok(result.filters.length >= 1 && result.filters.length <= 2, `broad: ${result.filters.length} filters`);
    const main = nearest(result.filters, 1000);
    assert.ok(main.q <= 1.5 && main.gainDb <= -2.5, `broad filter ${JSON.stringify(main)}`);
    const verdict = judge(points, result.filters, NEUTRAL);
    assert.ok(verdict.after.rmsDb <= 0.4 * verdict.before.rmsDb, `broad rms ${verdict.before.rmsDb} -> ${verdict.after.rmsDb}`);
}

// Narrow deep dip: never filled.
{
    const result = suggest(synth({ features: [bell(150, -18, 8)] }));
    assert.ok(peqSum(result.filters, 150) <= 1, `dip boost ${peqSum(result.filters, 150)}`);
    assert.ok(!result.filters.some((filter) => filter.gainDb > 1 && Math.abs(Math.log2(filter.freqHz / 150)) < 0.5), 'no boost filter at the dip');
}

// Deep hole about an octave wide (cancellation): its deep centre is not filled.
{
    const result = suggest(synth({ features: [bell(120, -14, 1.4)] }));
    assert.ok(peqSum(result.filters, 120) <= 3, `hole boost ${peqSum(result.filters, 120)}`);
}

// Broad shallow deficit over several octaves: boosted.
{
    const result = suggest(synth({ features: [bell(2500, -4, 0.5)] }));
    const lift = peqSum(result.filters, 2500);
    assert.ok(lift >= 2 && lift <= 5, `broad deficit lift ${lift}`);
}

// Speaker roll-off at both ends: left alone, never boosted.
{
    const points = synth({ features: [
        { type: 'high_pass', freqHz: 45, q: 0.707 }, { type: 'high_pass', freqHz: 45, q: 0.707 },
        { type: 'low_pass', freqHz: 15000, q: 0.707 }, bell(80, 6, 5),
    ] });
    const result = suggest(points);
    assert.ok(result.bandHz[0] >= 35 && result.bandHz[1] <= 18000, `band ${result.bandHz}`);
    for (const frequency of [20, 25, 30, 35, 19000, 20000]) {
        assert.ok(peqSum(result.filters, frequency) <= 0.5, `no roll-off boost at ${frequency} Hz`);
    }
    assert.ok(nearest(result.filters, 80).gainDb <= -4, 'the mode inside the band is still cut');
}

// Mode cluster with a limited filter count: the biggest problems first,
// more filters only improve the fit.
const clusterPoints = synth({ features: [
    bell(40, 8, 8), bell(63, 6, 7), bell(90, 9, 9), bell(125, 5, 6), bell(180, 4, 5), bell(3000, 3, 0.8),
] });
{
    const three = suggest(clusterPoints, NEUTRAL, { maxFilters: 3 });
    assert.ok(three.filters.length <= 3);
    for (const frequency of [40, 90]) {
        assert.ok(peqSum(three.filters, frequency) <= -3, `limited fit still cuts the ${frequency} Hz mode`);
    }
    const full = suggest(clusterPoints);
    assert.ok(full.filters.length >= 5 && full.filters.length <= 9, `cluster: ${full.filters.length} filters`);
    const limited = judge(clusterPoints, three.filters, NEUTRAL);
    const complete = judge(clusterPoints, full.filters, NEUTRAL);
    assert.ok(limited.after.rmsDb < 0.75 * limited.before.rmsDb, `3 filters rms ${limited.before.rmsDb} -> ${limited.after.rmsDb}`);
    assert.ok(complete.after.rmsDb < limited.after.rmsDb, 'more filters fit better');
    assert.ok(complete.after.rmsDb <= 0.35 * complete.before.rmsDb, `cluster rms ${complete.before.rmsDb} -> ${complete.after.rmsDb}`);
    assert.deepEqual(suggest(clusterPoints), full, 'deterministic');
}

// The absolute level and the display smoothing never matter.
{
    const shifted = suggest(modePoints.map(([frequency, level]) => [frequency, level + 7]));
    assert.deepEqual(shifted.filters, mode.filters);
    assert.doesNotMatch(fs.readFileSync(path.join(__dirname, '..', 'static', 'measurement_peq_auto.js'), 'utf8'), /displaySmoothing|smoothMeasurementTracePoints/);
}

// An L/R pair is averaged into one proposal.
{
    const result = auto.suggestMeasurementPeqFilters({
        sources: [{ points: synth({ features: [bell(80, 8, 6)] }) }, { points: synth() }],
        targetPoints: NEUTRAL,
    });
    const cut = nearest(result.filters, 80);
    assert.ok(cut.gainDb <= -2.5 && cut.gainDb >= -6, `averaged cut ${cut.gainDb}`);
    assert.equal(result.sourceCount, 2);
}

// Unusable input: nothing, a too narrow range, a sparse curve, or an L/R
// pair with one sparse side (never silently fitted as one side).
assert.equal(auto.suggestMeasurementPeqFilters({ sources: [] }).reason, 'no-data');
assert.equal(suggest(synth().filter(([frequency]) => frequency >= 100 && frequency <= 150)).reason, 'no-data');
const sparse = [[20, 0], [200, 6], [2000, -3], [10000, 15]];
assert.equal(suggest(sparse).reason, 'no-data');
assert.equal(auto.suggestMeasurementPeqFilters({ sources: [{ points: modePoints }, { points: sparse }] }).reason, 'no-data');

// --- Real measurements -----------------------------------------------------

const demoSource = fs.readFileSync(path.join(__dirname, '..', 'demo', 'data', 'measurements.js'), 'utf8');
const demoWindow = {};
new Function('window', demoSource)(demoWindow);
const demoMeasurements = demoWindow.FXROUTE_DEMO_MEASUREMENTS.savedMeasurements;
assert.ok(demoMeasurements.length >= 20);
let demoFilters = 0;
for (const target of [NEUTRAL, BK, HARMAN]) {
    for (const measurement of demoMeasurements) {
        const points = measurement.traces[0].points;
        const label = `${measurement.name} / ${target === NEUTRAL ? 'neutral' : (target === BK ? 'bk' : 'harman')}`;
        const result = auto.suggestMeasurementPeqFilters({
            sources: [{ points, confidence: measurement.analysis?.hybrid_constraints || [] }],
            targetPoints: target,
        });
        assertSafe(result, label);
        assert.ok(['', 'matching'].includes(result.reason), `${label}: ${result.reason}`);
        demoFilters += result.filters.length;
        const [lowHz, highHz] = result.bandHz;
        const verdict = judge(points, result.filters, target, lowHz, highHz);
        if (result.filters.length) {
            assert.ok(verdict.after.rmsDb < verdict.before.rmsDb, `${label}: rms ${verdict.before.rmsDb} -> ${verdict.after.rmsDb}`);
        }
        // Deep narrow notches (8 dB under their +-1/2 octave median) stay unfilled.
        const smoothed = dsp.smoothMeasurementTracePoints(points, '1/6-oct');
        smoothed.forEach(([frequency, level]) => {
            if (frequency < lowHz || frequency > highHz) return;
            const around = smoothed.filter(([other]) => Math.abs(Math.log2(other / frequency)) <= 0.5).map(([, value]) => value);
            if (median(around) - level < 8) return;
            const lift = peqSum(result.filters, frequency);
            assert.ok(lift <= 1.5, `${label}: ${lift.toFixed(2)} dB into the notch at ${frequency.toFixed(0)} Hz`);
        });
    }
}
assert.ok(demoFilters / (3 * demoMeasurements.length) <= 8, 'real curves get a moderate number of filters');

// --- Editor flow -------------------------------------------------------------

const state = { measurement: { activeEditor: 'peq', displaySmoothing: '1/1-oct' }, dsp: {} };
let sources = [];
let confirmAnswer = true;
const calls = [];
const timers = [];
editor.init({
    getState: () => state,
    getElements: () => ({}),
    getMeasurementActiveEditor: () => state.measurement.activeEditor,
    setMeasurementActiveEditor: (mode) => { state.measurement.activeEditor = mode; },
    renderMeasurementPanel: () => calls.push('render'),
    scheduleMeasurementGraphRender: () => calls.push('graph'),
    showToast: (message, kind) => calls.push(['toast', kind, message]),
    getMeasurementSourceEntries: () => sources,
    // A smoothed display trace must not be what Auto fits.
    getMeasurementDisplayTraces: () => [{ points: [[20, 0], [20000, 0]] }],
    getMeasurementTargetCurve: () => ({ label: 'Neutral', shortLabel: 'Neutral', points: NEUTRAL }),
    confirm: (message) => { calls.push(['confirm', message]); return confirmAnswer; },
    fetch: () => { throw new Error('Auto must not save anything'); },
    setTimeout: (callback) => { timers.push(callback); return timers.length; },
    clearTimeout: () => {},
});
const runPending = () => { while (timers.length) timers.shift()(); };
const confirms = () => calls.filter((call) => call[0] === 'confirm');
const peq = editor.ensureMeasurementPeqState();

// No measurement: blocked, explained, nothing changes.
assert.equal(editor.getMeasurementPeqAutoAvailability().available, false);
assert.equal(editor.runMeasurementPeqAuto(), false);
assert.equal(peq.filters.length, 0);

// One measurement, empty draft: filled without asking, ordinary filters.
sources = [{ name: 'Sweep L', channel: 'left', traces: [{ points: modePoints }] }];
assert.deepEqual(editor.getMeasurementPeqAutoAvailability(), { available: true, busy: false, reason: '' });
assert.equal(editor.runMeasurementPeqAuto(), true);
assert.equal(editor.getMeasurementPeqAutoAvailability().busy, true, 'busy until the deferred fit ran');
assert.equal(editor.runMeasurementPeqAuto(), false, 'a second click while fitting is ignored');
runPending();
assert.equal(confirms().length, 0);
assert.deepEqual(peq.filters.map(({ type, freqHz, gainDb, q }) => ({ type, freqHz, gainDb, q })), mode.filters);
peq.filters.forEach((filter, index) => {
    assert.match(filter.id, /^measurement-peq-/);
    assert.equal(filter.color, ui.getDefaultMeasurementPeqFilter(index).color);
});
assert.equal(peq.activeFilterId, peq.filters[0].id);
assert.deepEqual(peq.draft.leftBands, [], 'Auto never stages');
assert.ok(calls.some((call) => call[0] === 'toast' && call[1] === 'success' && /^Auto: \d+ filters? for Sweep L vs Neutral/.test(call[2])));

// Auto filters are edited and deleted like manual ones.
const firstId = peq.filters[0].id;
editor.updateMeasurementPeqFilter(firstId, { gainDb: -3, q: 2 });
assert.equal(peq.filters[0].gainDb, -3);
editor.deleteMeasurementPeqFilter(firstId);
assert.equal(peq.filters.some((filter) => filter.id === firstId), false);
editor.addMeasurementPeqFilter({ freqHz: 500, gainDb: 2 });
editor.addMeasurementPeqFilter({ freqHz: 2000, gainDb: -1 });
const draftBefore = peq.filters.map((filter) => ({ ...filter }));
assert.ok(draftBefore.length >= 2);

// A second Auto asks before replacing; cancel keeps the draft untouched.
sources = [{ name: 'Sweep L', channel: 'left', traces: [{ points: clusterPoints }] }];
confirmAnswer = false;
editor.runMeasurementPeqAuto();
runPending();
assert.equal(confirms().length, 1);
assert.match(confirms()[0][1], new RegExp(`^Replace the current ${draftBefore.length} filters with the Auto suggestion \\(\\d+ filters\\)\\?$`));
assert.deepEqual(peq.filters, draftBefore);

// Confirm replaces it.
confirmAnswer = true;
editor.runMeasurementPeqAuto();
runPending();
assert.equal(confirms().length, 2);
assert.deepEqual(peq.filters.map(({ type, freqHz, gainDb, q }) => ({ type, freqHz, gainDb, q })),
    suggest(clusterPoints).filters);

// An L/R pair is averaged; an ambiguous selection is refused.
sources = [
    { name: 'L', channel: 'left', traces: [{ points: clusterPoints }] },
    { name: 'R', channel: 'right', traces: [{ points: modePoints }] },
];
assert.equal(editor.getMeasurementPeqAutoSource().label, 'L+R average');
assert.equal(editor.getMeasurementPeqAutoSource().sources[0].points, clusterPoints, 'trusted trace, not the display trace');
sources = [...sources, { name: 'Other', channel: 'left', traces: [{ points: modePoints }] }];
assert.match(editor.getMeasurementPeqAutoAvailability().reason, /one measurement or one L\/R pair/);

// A matching curve leaves existing filters alone and asks nothing.
sources = [{ name: 'Flat', channel: 'left', traces: [{ points: synth() }] }];
const kept = peq.filters.map((filter) => ({ ...filter }));
editor.runMeasurementPeqAuto();
runPending();
assert.equal(confirms().length, 2);
assert.deepEqual(peq.filters, kept);
assert.ok(calls.some((call) => call[0] === 'toast' && call[1] === 'info' && /already follows Neutral/.test(call[2])));

// Take keeps working on Auto filters exactly as on manual ones.
editor.takeMeasurementPeqToPreset('both');
assert.equal(peq.draft.leftBands.length, kept.length);
assert.deepEqual(peq.draft.rightBands, kept.map((filter) => editor.measurementPeqFilterToBand(filter)));

console.log('measurement PEQ auto: ok');
