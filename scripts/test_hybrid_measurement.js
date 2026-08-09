#!/usr/bin/env node
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const Hybrid = require('../static/hybrid_measurement.js');
const Dsp = require('../static/measurement_dsp.js');

const root = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8');
const appSource = fs.readFileSync(path.join(root, 'static/app.js'), 'utf8');
assert(indexSource.includes('id="measurement-hybrid-panel"'));
assert(indexSource.indexOf('hybrid_measurement.js') < indexSource.indexOf('app.js?v='), 'hybrid model must load before app');
assert(appSource.includes("formData.append('measurement_role', step.role)"));
assert(appSource.includes("hybrid_constraints: model.constraints"));

function points(level) {
    return [[40, level], [80, level], [160, level], [320, level], [1000, level], [5000, level], [12000, level]];
}

function measurement(channel, level, extras = {}) {
    return {
        channel,
        traces: [{ points: points(level) }],
        analysis: {
            impulse_response: { arrival_ms: extras.arrivalMs || 1, arrival_samples: 48, direct_arrival_index: 100, reference_peak_index: 52 },
            direct_response: extras.directResponse,
            complex_response: extras.complexResponse,
        },
    };
}

function capture(role, position, channel, value) {
    return { role, position, channel, measurement: value };
}

for (const mode of ['stereo', 'subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo']) {
    const sequence = Hybrid.buildSequence(mode);
    assert.equal(sequence.mode, mode);
    assert.equal(sequence.steps.filter(step => step.role === 'direct').length, 2);
    assert.equal(sequence.steps.filter(step => step.role === 'mlp').length, 2);
    assert.equal(sequence.steps.filter(step => step.role === 'secondary').length, 4);
    assert.equal(sequence.steps.filter(step => step.role === 'integration').length, mode === 'stereo' ? 0 : 1);
}

const localNullCaptures = [
    capture('mlp', 'mlp', 'left', measurement('left', -10)),
    capture('secondary', 'left', 'left', measurement('left', 0)),
    capture('secondary', 'right', 'left', measurement('left', 0)),
];
const nullModel = Hybrid.buildListeningModel(localNullCaptures, 'left');
assert(nullModel.constraints[0].boostConfidence <= 0.051, 'local bass null must veto boost');
assert.strictEqual(nullModel.timingMeasurement, localNullCaptures[0].measurement, 'MLP must be the timing source');

const stablePeakCaptures = [
    capture('mlp', 'mlp', 'left', measurement('left', 8)),
    capture('secondary', 'left', 'left', measurement('left', 7.5)),
    capture('secondary', 'right', 'left', measurement('left', 8.5)),
];
const peakModel = Hybrid.buildListeningModel(stablePeakCaptures, 'left');
assert(peakModel.constraints[0].cutConfidence > 0.9, 'spatially stable peak must retain cut confidence');

const nullAnalysis = Dsp.analyzeMeasurementConvolverCorrections(
    [[40, -10]], [[20, 0], [20000, 0]],
    { maxBoostDb: 9, maxCutDb: -9, autoGainEnabled: false, correctionConfidence: nullModel.constraints },
);
assert(nullAnalysis.corrections[0].correctionDb <= 0.51, 'vetoed null must not receive aggressive boost');
const peakAnalysis = Dsp.analyzeMeasurementConvolverCorrections(
    [[40, 8]], [[20, 0], [20000, 0]],
    { maxBoostDb: 9, maxCutDb: -9, autoGainEnabled: false, correctionConfidence: peakModel.constraints },
);
assert(peakAnalysis.corrections[0].correctionDb < -7, 'stable peak must remain cuttable');

const lower = 300;
assert.equal(Hybrid.getHybridDirectWeight(100, lower), 0);
const middleWeight = Hybrid.getHybridDirectWeight(400, lower);
assert(middleWeight > 0 && middleWeight < 0.85, 'transition must crossfade smoothly');
assert.equal(Hybrid.getHybridDirectWeight(1000, lower), 0.85);

const directResponse = { usable: true, lower_reliable_hz: 300, points: points(2) };
const profileCaptures = [];
for (const channel of ['left', 'right']) {
    profileCaptures.push(capture('direct', `direct-${channel}`, channel, measurement(channel, 0, { directResponse })));
    profileCaptures.push(capture('mlp', 'mlp', channel, measurement(channel, 0, { arrivalMs: channel === 'left' ? 1 : 2 })));
    profileCaptures.push(capture('secondary', 'left', channel, measurement(channel, 0)));
    profileCaptures.push(capture('secondary', 'right', channel, measurement(channel, 0)));
}
const profile = Hybrid.buildProfile(profileCaptures, 'stereo');
assert.strictEqual(profile.left.timingMeasurement, profileCaptures[1].measurement);
assert.strictEqual(profile.right.timingMeasurement, profileCaptures[5].measurement);
assert.equal(profile.left.transition.highFrequencyRoomWeight, 0.15);
assert(profile.left.transition.endHz > profile.left.transition.startHz);

const complexLeft = { schema: 'x', points: [[40, 1, 0], [80, 0, 1]] };
const complexRight = { schema: 'x', points: [[40, 0, 1], [80, 1, 0]] };
const complexActual = { schema: 'x', points: [[40, 1, 1], [80, 1, 1]] };
const validation = Hybrid.validateComplexSum(
    measurement('left', 0, { complexResponse: complexLeft }),
    measurement('right', 0, { complexResponse: complexRight }),
    measurement('stereo', 0, { complexResponse: complexActual }),
);
assert.equal(validation.status, 'ok');
assert(Math.abs(validation.rmsErrorDb) < 1e-9);
assert.deepEqual(validation.predicted, [[40, 1, 1], [80, 1, 1]]);

console.log('hybrid measurement tests: ok');
