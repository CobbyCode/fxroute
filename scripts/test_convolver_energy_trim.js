#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
'use strict';

// Covers the convolver broadband energy trim: after the pointwise
// correction the 1/1-smoothed band energy must be re-centered on the
// target (median of smoothed residuals, AutoSub principle), without
// letting local valleys set the level.

const assert = require('assert');
const path = require('path');

const Dsp = require(path.join('..', 'static', 'measurement_dsp.js'));

function logRange(minHz, maxHz, count) {
    const points = [];
    for (let index = 0; index < count; index += 1) {
        const ratio = count === 1 ? 0 : index / (count - 1);
        points.push(minHz * ((maxHz / minHz) ** ratio));
    }
    return points;
}

function flatTrace(levelByBand) {
    // Full display trace: correction band 30-250 Hz plus reference 250-8000 Hz.
    return [
        ...logRange(30, 250, 25).map((frequency) => [frequency, levelByBand]),
        ...logRange(260, 8000, 15).map((frequency) => [frequency, 0]),
    ];
}

const NEUTRAL = [[20, 0], [20000, 0]];
const SETTINGS = {
    maxBoostDb: 6,
    maxCutDb: -9,
    dipGuard: 'off',
    safetyMarginDb: 1,
    autoGainEnabled: true,
};

// 1. Clamped dips leave a smoothed deficit -> trim must restore it.
{
    const full = flatTrace(-8);
    const band = full.filter(([frequency]) => frequency >= 30 && frequency <= 250);
    const analysis = Dsp.analyzeMeasurementConvolverCorrections(band, NEUTRAL, {
        ...SETTINGS,
        trimContextPoints: full,
    });
    assert.equal(typeof analysis.energyTrimDb, 'number', 'analysis must report energyTrimDb');
    assert.ok(
        Math.abs(analysis.energyTrimDb - 2) < 0.3,
        `flat -8 dB band (clamped to +6) must trim ~+2 dB, got ${analysis.energyTrimDb}`,
    );
    // Trim is uniform: relative shape untouched, headroom preserved via autoGain.
    const shifts = analysis.corrections.map((item) => item.correctionDb);
    const spread = Math.max(...shifts) - Math.min(...shifts);
    assert.ok(spread < 1e-9, `trim must be uniform across the band, spread ${spread}`);
    const peak = Math.max(...analysis.corrections.map((item) => item.correctionDb)) + analysis.autoGainDb;
    assert.ok(
        Math.abs(peak - -1) < 1e-9,
        `frequency peak must stay at -safetyMarginDb, got ${peak}`,
    );
}

// 2. A single deep valley must not set the level (median, not max).
{
    const full = flatTrace(-1);
    const spikeAt = full.findIndex(([frequency]) => frequency >= 30 && frequency <= 250 && frequency > 100);
    full[spikeAt][1] = -12;
    const band = full.filter(([frequency]) => frequency >= 30 && frequency <= 250);
    const analysis = Dsp.analyzeMeasurementConvolverCorrections(band, NEUTRAL, {
        ...SETTINGS,
        trimContextPoints: full,
    });
    assert.ok(
        Math.abs(analysis.energyTrimDb) < 0.5,
        `single valley must not move the trim, got ${analysis.energyTrimDb}`,
    );
}

// 3. Absurd deficits are bounded and flagged.
{
    const full = flatTrace(-20);
    const band = full.filter(([frequency]) => frequency >= 30 && frequency <= 250);
    const analysis = Dsp.analyzeMeasurementConvolverCorrections(band, NEUTRAL, {
        ...SETTINGS,
        trimContextPoints: full,
    });
    assert.equal(analysis.energyTrimDb, 3, `trim must clamp at +3 dB, got ${analysis.energyTrimDb}`);
    assert.equal(analysis.energyTrimClamped, true, 'clamped trim must be flagged');
}

// 4. Already-centered bands stay untouched.
{
    const full = flatTrace(-1.5);
    const band = full.filter(([frequency]) => frequency >= 30 && frequency <= 250);
    const analysis = Dsp.analyzeMeasurementConvolverCorrections(band, NEUTRAL, {
        ...SETTINGS,
        trimContextPoints: full,
    });
    assert.ok(
        Math.abs(analysis.energyTrimDb) < 0.3,
        `centered band must keep trim ~0, got ${analysis.energyTrimDb}`,
    );
    assert.equal(analysis.energyTrimClamped, false, 'unclamped trim must not be flagged');
}

// 5. Too few band points -> trim skipped, pointwise path unchanged.
{
    const analysis = Dsp.analyzeMeasurementConvolverCorrections(
        [[40, -10]], NEUTRAL,
        { maxBoostDb: 9, maxCutDb: -9, autoGainEnabled: false, correctionConfidence: [] },
    );
    assert.equal(analysis.energyTrimDb, 0, 'sparse bands must skip the trim');
    assert.equal(typeof analysis.energyTrimReason, 'string', 'skipped trim must carry a reason');
    assert.equal(analysis.corrections[0].correctionDb, 9, 'pointwise correction untouched');
}

console.log('convolver energy trim: ok');
