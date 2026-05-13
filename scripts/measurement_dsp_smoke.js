// SPDX-License-Identifier: AGPL-3.0-only
const assert = require('node:assert/strict');
const dsp = require('../static/measurement_dsp.js');

const smoothInput = [[20, 0], [40, 2], [80, 4], [160, 2], [320, 0]];
const smoothed = dsp.smoothMeasurementTracePoints(smoothInput, '1/3-oct');
assert.equal(smoothed.length, smoothInput.length);
assert.notEqual(smoothed, smoothInput);

const curveDb = dsp.getMeasurementConvolverCurveDbFromPoints([[20, 3], [200, 0], [20000, -3]], 200);
assert.equal(curveDb, 0);

const analysis = dsp.analyzeMeasurementConvolverCorrections(
    [[40, -4], [80, -8], [160, -4], [320, -2], [640, -1]],
    [[20, 0], [20000, 0]],
    { maxBoostDb: 6, maxCutDb: -9, dipGuard: 'gentle', safetyMarginDb: 1, autoGainEnabled: true },
);
assert.equal(analysis.corrections.length, 5);
assert.ok(analysis.maxPositive <= 6);
assert.ok(analysis.autoGainDb <= -1);

const impulseLinear = dsp.buildMeasurementConvolverImpulse({ corrections: analysis.corrections }, 48000, 64, analysis.autoGainDb, 'linear');
const impulseMinimum = dsp.buildMeasurementConvolverImpulse({ corrections: analysis.corrections }, 48000, 64, analysis.autoGainDb, 'minimum');
assert.equal(impulseLinear.length, 64);
assert.equal(impulseMinimum.length, 64);
assert.ok(Array.from(impulseLinear).every(Number.isFinite));
assert.ok(Array.from(impulseMinimum).every(Number.isFinite));

const peqMag = dsp.getMeasurementPeqFilterMagnitude({ type: 'bell', freqHz: 1000, gainDb: 3, q: 1 }, 1000);
assert.ok(peqMag > 2.9 && peqMag < 3.1);

const range = dsp.getMeasurementGraphRange([{ traces: [{ points: [[20, -6], [1000, 3], [20000, 0]] }] }]);
assert.deepEqual(range, { minDb: -12, maxDb: 12 });

const wav = dsp.writeMeasurementConvolverWav([impulseLinear], 48000);
assert.equal(wav.type, 'audio/wav');
assert.equal(wav.size, 44 + (64 * 2));

console.log('measurement_dsp smoke ok');
