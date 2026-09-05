#!/usr/bin/env node
'use strict';

// Gate-confidence regression: multiplicative factor on direct confidence.
// Left (decisive) stays unchanged, fragile right is damped toward the room model.

const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const Hybrid = require('../static/hybrid_measurement.js');

const pairInputs = JSON.parse(fs.readFileSync(
    path.join(__dirname, 'fixtures', 'hybrid-pair-blend-inputs.json'), 'utf8'));

test('gate confidence defaults to full weight for old saves', () => {
    assert.equal(Hybrid.getDirectModelWeight(1000, 300, 0.8, 0, 0), 0.8);
    assert.equal(Hybrid.getDirectModelWeight(1000, 300, 0.8, 0, 0, undefined), 0.8);
    assert.equal(Hybrid.getDirectModelWeight(1000, 300, 0.8, 0, 0, NaN), 0);
});

test('gate confidence scales the direct weight linearly', () => {
    const full = Hybrid.getDirectModelWeight(1000, 300, 0.8, 1, 2);
    assert.equal(Hybrid.getDirectModelWeight(1000, 300, 0.8, 1, 2, 0.25), full * 0.25);
    assert.equal(Hybrid.getDirectModelWeight(1000, 300, 0.8, 1, 2, 0), 0);
    assert.equal(Hybrid.getDirectModelWeight(1000, 300, 0.8, 1, 2, 2), full);
    assert.equal(Hybrid.getDirectModelWeight(200, 300, 0.8, 1, 2, 0.25), 0);
});

function capturesFromTriplets(side, gateConfidence) {
    const input = pairInputs[side];
    const directPts = input.rows.map(([f, , direct]) => [f, direct]);
    const trace = (delta) => input.rows.map(([f, room, , spread]) => [f, room + delta * spread / 2]);
    const direct = { usable: true, gated_direct_lower_limit_hz: input.limitHz, direct_confidence: 1, points: directPts };
    if (gateConfidence !== undefined) direct.gate_confidence = gateConfidence;
    return [
        { role: 'direct', position: `direct-${side}`, channel: side, measurement: { analysis: { direct_response: direct } } },
        { role: 'mlp', position: 'mlp', channel: side, measurement: { traces: [{ points: trace(0) }] } },
        { role: 'secondary', position: 'left', channel: side, measurement: { traces: [{ points: trace(1) }] } },
        { role: 'secondary', position: 'right', channel: side, measurement: { traces: [{ points: trace(-1) }] } },
    ];
}

function roomDb(side, f) {
    const rows = pairInputs[side].rows;
    return rows.reduce((a, b) => Math.abs(Math.log(b[0] / f)) < Math.abs(Math.log(a[0] / f)) ? b : a)[1];
}

for (const side of ['left', 'right']) {
    test(`${side}: explicit full gate confidence matches missing gate confidence exactly`, () => {
        const explicit = Hybrid.buildHybridSide(capturesFromTriplets(side, 1), side);
        const missing = Hybrid.buildHybridSide(capturesFromTriplets(side, undefined), side);
        assert.deepEqual(explicit.points, missing.points);
        assert.deepEqual(explicit.constraints, missing.constraints);
        assert.equal(explicit.modelBlend.gateConfidence, 1);
    });

    test(`${side}: harness reproduces room model and stored level offset`, () => {
        const model = Hybrid.buildHybridSide(capturesFromTriplets(side, 0), side);
        for (const [f] of model.points) {
            assert(Math.abs(Hybrid.interpolate(model.points, f) - roomDb(side, f)) < 1e-9);
        }
        const full = Hybrid.buildHybridSide(capturesFromTriplets(side, 1), side);
        assert(Math.abs(full.modelBlend.directLevelOffsetDb - pairInputs[side].offsetDb) < 0.005);
    });

    test(`${side}: full gate confidence lets the direct model contribute`, () => {
        const model = Hybrid.buildHybridSide(capturesFromTriplets(side, 1), side);
        const contrib = Math.max(...model.points.map(([f, hybridDb]) => Math.abs(hybridDb - roomDb(side, f))));
        assert(contrib > 0.5, `direct model contributes only ${contrib} dB`);
    });

    test(`${side}: reduced gate confidence damps monotonically toward the room model`, () => {
        const full = Hybrid.buildHybridSide(capturesFromTriplets(side, 1), side);
        const quarter = Hybrid.buildHybridSide(capturesFromTriplets(side, 0.25), side);
        for (const [f, fullDb] of full.points) {
            const room = roomDb(side, f);
            const part = Hybrid.interpolate(quarter.points, f);
            assert(Math.abs(part - room) <= Math.abs(fullDb - room) + 1e-9, `not damped at ${f} Hz`);
        }
    });
}
