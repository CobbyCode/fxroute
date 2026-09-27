#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// The measurement PEQ assistant draws each band with the response the DSP
// engine renders for the stored parameters. The reference was captured from
// the real engine (fxroute-dsp-offline, area-bank biquads) on the .104 test
// machine; test_native_dsp_bank_chain.py keeps the engine (area and Global
// bank) on the same reference, so the two sides cannot drift apart. Shelves
// store Q like every other type, not the RBJ shelf slope S.

const assert = require('node:assert/strict');
const dsp = require('../static/measurement_dsp.js');
const reference = require('./fixtures/peq_engine_reference.json');

// float32 engine coefficients leave up to ~0.06 dB at 20 Hz; reading the
// shelf value as slope S instead of Q cost 0.16-1.13 dB on these cases.
const TOLERANCE_DB = 0.08;

const shelves = reference.cases.filter((band) => band.filterType.endsWith('_shelf'));
assert.ok(shelves.length >= 6, 'reference must cover several low and high shelves');

for (const band of reference.cases) {
    reference.frequencies_hz.forEach((frequency, index) => {
        const assistantDb = dsp.getMeasurementPeqFilterMagnitude(
            { type: band.filterType, freqHz: band.frequencyHz, gainDb: band.gainDb, q: band.q },
            frequency, reference.sample_rate_hz);
        const engineDb = band.engine_db[index];
        assert.ok(Math.abs(assistantDb - engineDb) <= TOLERANCE_DB,
            `${band.filterType} ${band.frequencyHz} Hz ${band.gainDb} dB Q ${band.q} @ ${frequency} Hz: `
            + `assistant ${assistantDb.toFixed(3)} dB != engine ${engineDb} dB`);
    });
}

console.log('measurement PEQ assistant/engine parity: ok');
