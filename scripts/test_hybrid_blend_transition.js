#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const Hybrid = require('../static/hybrid_measurement.js');

// Reduced numerical reference from the saved Advanced pair created on 2026-09-05.
// Values are the aligned direct and listening models, not reconstructed raw IR.
const leftReference = {
    frequency: 717.801,
    limitHz: 712.9,
    shorterGateLimitHz: 720.0,
    roomDb: 2.9285,
    directDb: -0.6171,
    spatialSpreadDb: 1.882,
    legacyWeight: 0.6739411591336815,
};

function referenceBlend(limitHz) {
    const r = leftReference;
    const weight = Hybrid.getDirectModelWeight(
        r.frequency, limitHz, 1, r.roomDb - r.directDb, r.spatialSpreadDb,
    );
    return r.roomDb * (1 - weight) + r.directDb * weight;
}

test('one-sample shorter left gate no longer causes the 2.39 dB model jump', () => {
    const r = leftReference;
    const legacyJumpDb = (r.roomDb - r.directDb) * r.legacyWeight;
    assert(Math.abs(legacyJumpDb - 2.3895257738243805) < 1e-12);
    const jumpDb = Math.abs(referenceBlend(r.limitHz) - referenceBlend(r.shorterGateLimitHz));
    assert(jumpDb < 0.02, `One-sample gate change produced ${jumpDb} dB`);
});

test('direct weight starts at zero and rises smoothly on a logarithmic frequency axis', () => {
    // Quarter, half and three-quarter positions of a one-third-octave transition.
    for (const [octaves, expected] of [[0, 0], [1 / 12, 0.125], [1 / 6, 0.4], [1 / 4, 0.675], [1 / 3, 0.8]]) {
        const weight = Hybrid.getDirectModelWeight(300 * 2 ** octaves, 300, 0.8, 0, 0);
        assert(Math.abs(weight - expected) < 1e-12, `Unexpected weight at ${octaves} octaves`);
    }
    assert.equal(Hybrid.getDirectModelWeight(299.999, 300, 1, 0, 0), 0);
    assert(Hybrid.getDirectModelWeight(300 * (1 + 1e-6), 300, 1, 0, 0) < 1e-9);
    assert(Hybrid.getDirectModelWeight(300 * 2 ** (1 / 3) * (1 - 1e-6), 300, 1, 0, 0) > 1 - 1e-9);
});

test('weights above the transition retain confidence, agreement and spatial consistency', () => {
    const r = leftReference;
    for (const frequency of [910, 1000, 20000]) {
        assert.equal(Hybrid.getDirectModelWeight(frequency, r.limitHz, 1, r.roomDb - r.directDb, r.spatialSpreadDb), r.legacyWeight);
        assert.equal(Hybrid.getDirectModelWeight(frequency, r.limitHz, 0.8, 0, 6), 0.8);
        assert.equal(Hybrid.getDirectModelWeight(frequency, r.limitHz, 1, 6, 0), Math.exp(-1));
    }
    assert.equal(Hybrid.getDirectModelWeight(1000, r.limitHz, 0, 0, 0), 0);
});

test('right reference uses its own transition rather than the left frequency range', () => {
    const weight = Hybrid.getDirectModelWeight(242.549, 237.6, 1, 1.94525, 3.146);
    assert(weight > 0 && weight < 0.03);
    assert(Math.abs(Hybrid.getDirectModelWeight(301.329, 237.6, 1, 0.623, 4.642) - 0.9545) < 0.001);
});

test('invalid logarithmic gate limits do not contribute direct weight', () => {
    for (const limit of [0, -300, NaN, Infinity]) {
        assert.equal(Hybrid.getDirectModelWeight(1000, limit, 1, 0, 0), 0);
    }
});

test('curve and correction constraints use the same softened direct weight', () => {
    const middleHz = 300 * 2 ** (1 / 6);
    const roomPoints = [[250, 0], [300, 0], [middleHz, 0], [400, 0], [1000, 0]];
    const directResponse = {
        usable: true, gated_direct_lower_limit_hz: 300, direct_confidence: 1,
        points: roomPoints.map(([f]) => [f, f === middleHz ? 6 : 0]),
    };
    const captures = [
        { role: 'direct', position: 'direct-left', channel: 'left', measurement: { analysis: { direct_response: directResponse } } },
        ...[['mlp', 'mlp'], ['secondary', 'left'], ['secondary', 'right']].map(([role, position]) => ({
            role, position, channel: 'left', measurement: { traces: [{ points: roomPoints }] },
        })),
    ];
    const inputBefore = JSON.stringify(captures);
    const model = Hybrid.buildHybridSide(captures, 'left');
    assert.equal(model.modelBlend.gatedDirectLowerLimitHz, 300);
    assert.equal(model.modelBlend.directConfidence, 1);
    assert.equal(model.modelBlend.directLevelOffsetDb, 0);
    // Stable room and 6 dB disagreement give exp(-1), halved at the log midpoint.
    assert(Math.abs(model.points[2][1] - 1.103638323514327) < 1e-12);
    assert(Math.abs(model.constraints[2].spatialWeight - 0.8160602794142788) < 1e-12);
    assert.equal(model.constraints[0].spatialWeight, 1);
    assert.equal(model.constraints[3].spatialWeight, 0);
    assert.equal(JSON.stringify(captures), inputBefore, 'Blend must not mutate measurement data');
});
