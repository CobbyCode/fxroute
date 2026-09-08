#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
'use strict';

// Convolver auto-headroom is energy-based: the headroom must track the
// log-weighted (|H|^2, equal per octave) energy gain of the unattenuated
// FIR correction, not the peak boost. Peak logic gave a single -7 dB for
// every target; energy logic must differentiate narrow vs. broad boosts
// while keeping the shared conservative stereo headroom on a 0.5 dB grid.

const assert = require('assert');
const path = require('path');

const root = path.resolve(__dirname, '..');
const Dsp = require(path.join(root, 'static', 'measurement_dsp.js'));
const UI = require(path.join(root, 'static', 'measurement_ui.js'));

function analyze(curveKey, corrections, rangeStartHz, rangeEndHz, extra = {}) {
    return Dsp.analyzeMeasurementConvolverCorrections(
        corrections,
        UI.measurementConvolverCurves[curveKey].points,
        {
            maxBoostDb: 6, maxCutDb: -9, dipGuard: 'off',
            rangeStartHz, rangeEndHz, autoGainEnabled: true,
            correctionConfidence: [],
            ...extra,
        },
    );
}

function logGridEnergyDb(corrections, rangeStartHz, rangeEndHz) {
    return Dsp.getMeasurementConvolverEnergyGainDb({ corrections }, rangeStartHz, rangeEndHz);
}

const narrow = [
    [30, 0], [60, 0], [100, 0], [140, -2], [146, -8], [152, -2],
    [200, 0], [500, 0], [1000, 0], [2000, 0], [3000, 0],
];
const broad = [
    [30, -4], [60, -4], [100, -4], [140, -4], [200, -4], [500, -4],
    [1000, 0], [2000, 0], [3000, 0],
];

const narrowAnalysis = analyze('neutral', narrow.map(([f, m]) => [f, m]), 30, 3000);
const broadAnalysis = analyze('neutral', broad.map(([f, m]) => [f, m]), 30, 3000);
assert.equal(narrowAnalysis.maxPositive, 6, 'narrow fixture must hit the boost clamp');
assert.ok(broadAnalysis.maxPositive < 6, 'broad fixture must stay below the boost clamp');
assert.ok(
    narrowAnalysis.energyGainDb < broadAnalysis.energyGainDb,
    `narrow peak (${narrowAnalysis.energyGainDb} dB) must weigh less than broad lift (${broadAnalysis.energyGainDb} dB)`,
);
assert.ok(
    narrowAnalysis.autoGainDb > broadAnalysis.autoGainDb,
    `narrow headroom (${narrowAnalysis.autoGainDb} dB) must be milder than broad headroom (${broadAnalysis.autoGainDb} dB)`,
);

const flat = analyze('neutral', [[30, 0], [100, 0], [3000, 0]], 30, 3000);
assert.equal(flat.energyGainDb, 0, 'flat correction carries no energy gain');
assert.equal(flat.autoGainDb, -1, 'flat correction keeps only the fixed safety reserve');

const disabled = analyze('neutral', narrow.map(([f, m]) => [f, m]), 30, 3000, { autoGainEnabled: false });
assert.equal(disabled.autoGainDb, 0, 'disabled auto gain must stay 0');

const empty = Dsp.analyzeMeasurementConvolverCorrections([], [[20, 0], [20000, 0]], { autoGainEnabled: true });
assert.equal(empty.energyGainDb, 0, 'empty correction carries no energy gain');
assert.equal(empty.autoGainDb, -1, 'empty correction keeps only the fixed safety reserve');

for (const analysis of [narrowAnalysis, broadAnalysis]) {
    assert.ok(
        analysis.autoGainDb <= -(analysis.energyGainDb + 0.75),
        `headroom ${analysis.autoGainDb} dB must cover energy ${analysis.energyGainDb} dB + safety on the 0.5 dB grid`,
    );
    assert.equal(analysis.autoGainDb * 2, Math.round(analysis.autoGainDb * 2), 'headroom must stay on the 0.5 dB grid');
    assert.ok(
        Math.abs(logGridEnergyDb(analysis.corrections, 30, 3000) - analysis.energyGainDb) < 0.005,
        'exported energy helper must reproduce the analysis value',
    );
}

const left = analyze('neutral', narrow.map(([f, m]) => [f, m]), 30, 3000);
const right = analyze('neutral', broad.map(([f, m]) => [f, m]), 30, 3000);
assert.equal(
    Math.min(left.autoGainDb, right.autoGainDb),
    broadAnalysis.autoGainDb,
    'shared stereo headroom stays the conservative (lower) side',
);

console.log('convolver energy headroom tests: ok');
