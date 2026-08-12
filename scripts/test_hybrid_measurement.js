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
assert(appSource.includes('HybridMeasurement.isUsableDirectMeasurement(measurement)'));
assert(appSource.includes("'Cancel measurement'"));
assert(appSource.includes('cancelHybridWizardMeasurement'));
assert(appSource.includes("processing ? `Processing ${step.channel"));
assert(indexSource.includes('hybrid-seat-cushion-left'));

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

function directTimingMeasurement(channel, arrivalMs) {
    const value = measurement(channel, 0, { directResponse });
    value.analysis.reference_path = {
        capture_mode: 'dual-channel',
        timing_status: 'acoustic-only',
        acoustic_arrival_corrected_ms: arrivalMs,
    };
    return value;
}

for (const mode of ['stereo', 'subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo']) {
    const sequence = Hybrid.buildSequence(mode);
    assert.equal(sequence.mode, mode);
    assert.equal(sequence.steps.filter(step => step.role === 'direct').length, 2);
    assert.equal(sequence.steps.filter(step => step.role === 'mlp').length, 2);
    assert.equal(sequence.steps.filter(step => step.role === 'secondary').length, 4);
    assert.equal(sequence.steps.filter(step => step.role === 'integration').length, mode === 'stereo' ? 0 : 1);
}

const positionSequence = Hybrid.buildSequence('stereo').steps;
assert.equal(Hybrid.getPositionSeriesEnd(positionSequence, 0), 0, 'direct left requires its own start');
assert.equal(Hybrid.getPositionSeriesEnd(positionSequence, 1), 1, 'direct right requires its own start');
assert.equal(Hybrid.getPositionSeriesEnd(positionSequence, 2), 3, 'main position runs left and right automatically');
assert.equal(Hybrid.getPositionSeriesEnd(positionSequence, 4), 5, 'left secondary position runs left and right automatically');
assert.equal(Hybrid.getPositionSeriesEnd(positionSequence, 6), 7, 'right secondary position runs left and right automatically');
assert.equal(Hybrid.getPreviousPositionIndex(positionSequence, 6), 4, 'Back returns to the start of the previous position');

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
assert.equal(Hybrid.getDirectModelWeight(299, lower, 1, 0, 0), 0, 'direct model must stop below its gate limit');
assert.equal(Hybrid.getDirectModelWeight(300, lower, 0.8, 0, 0), 0.8, 'direct confidence must cap its model weight');
const stableDisagreementWeight = Hybrid.getDirectModelWeight(400, lower, 1, 6, 0);
const variableDisagreementWeight = Hybrid.getDirectModelWeight(400, lower, 1, 6, 6);
assert(variableDisagreementWeight > stableDisagreementWeight, 'spatially unstable area data must favor the direct model');
assert.equal(Hybrid.getDirectModelWeight(400, lower, 1, 0, 6), 1, 'agreement must make the model choice neutral');

const directResponse = { usable: true, gated_direct_lower_limit_hz: 300, direct_confidence: 1, points: points(2) };
assert(Hybrid.isUsableDirectMeasurement(measurement('left', 0, { directResponse })), 'good direct measurement must be accepted');
const leftDirectCapture = capture('direct', 'direct-left', 'left', directTimingMeasurement('left', 84.0));
const wrongRightPosition = Hybrid.validateDirectMicrophonePosition(
    directTimingMeasurement('right', 89.5),
    [leftDirectCapture],
    'right',
);
assert(wrongRightPosition.available);
assert(!wrongRightPosition.plausible, 'large paired timing difference must reject the wrong direct microphone position');
assert(wrongRightPosition.reason.includes('too far from the right speaker'));
const correctRightPosition = Hybrid.validateDirectMicrophonePosition(
    directTimingMeasurement('right', 84.8),
    [leftDirectCapture],
    'right',
);
assert(correctRightPosition.plausible, 'normal L/R direct timing difference must remain accepted');
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
assert.equal(profile.left.modelBlend.gatedDirectLowerLimitHz, 300);
assert(!('startHz' in profile.left.modelBlend));
assert(!('endHz' in profile.left.modelBlend));

const spatialTransitionCaptures = [];
for (const channel of ['left', 'right']) {
    spatialTransitionCaptures.push(capture('direct', `direct-${channel}`, channel, measurement(channel, 0, { directResponse })));
    spatialTransitionCaptures.push(capture('mlp', 'mlp', channel, measurement(channel, -10)));
    spatialTransitionCaptures.push(capture('secondary', 'left', channel, measurement(channel, 0)));
    spatialTransitionCaptures.push(capture('secondary', 'right', channel, measurement(channel, 0)));
}
const spatialProfile = Hybrid.buildProfile(spatialTransitionCaptures, 'stereo');
const lowConstraint = spatialProfile.left.constraints.find(item => item.frequency === 40);
const highConstraint = spatialProfile.left.constraints.find(item => item.frequency === 5000);
assert(lowConstraint.boostConfidence <= 0.051, 'secondary veto must remain effective in the room-dominant bass range');
assert(highConstraint.boostConfidence > 0.8, 'secondary veto must fade in the direct-dominant range');
assert(highConstraint.spatialWeight < lowConstraint.spatialWeight, 'spatial influence must crossfade smoothly with frequency');

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

const phaseInverted = Hybrid.validateComplexSum(
    measurement('left', 0, { complexResponse: { points: [[40, 1, 0]] } }),
    measurement('right', 0, { complexResponse: { points: [[40, 0, 0]] } }),
    measurement('stereo', 0, { complexResponse: { points: [[40, -1, 0]] } }),
);
assert(Math.abs(phaseInverted.magnitudeRmsErrorDb) < 1e-9, 'phase inversion has the same magnitude');
assert.equal(phaseInverted.phaseRmsErrorDeg, 180);
assert.equal(phaseInverted.complexResidualRms, 2);
assert.equal(phaseInverted.status, 'poor', '180 degree complex error must never validate as OK');
assert.deepEqual(phaseInverted.validationBandHz, [20, 500]);
assert(appSource.includes('complex residual'));
assert(appSource.includes('no separate integration sweep was performed'));
assert(appSource.includes('integration.limitation'));

const poorIntegrationCaptures = profileCaptures.map(item => {
    if (item.role !== 'mlp') return item;
    return capture(item.role, item.position, item.channel, measurement(item.channel, 0, {
        complexResponse: { points: [[40, item.channel === 'left' ? 1 : 0, 0]] },
    }));
});
poorIntegrationCaptures.push(capture(
    'integration',
    'mlp',
    'stereo',
    measurement('stereo', 0, { complexResponse: { points: [[40, -1, 0]] } }),
));
let integrationError = null;
try {
    Hybrid.buildProfile(poorIntegrationCaptures, 'subwoofer-2.1');
} catch (error) {
    integrationError = error;
}
assert.match(integrationError?.message || '', /integration check failed/i, 'poor integration must prevent profile generation');
assert.equal(integrationError?.retryRole, 'integration', 'quality gate must identify the measurement to repeat');
assert(appSource.includes("error.retryRole === 'integration'"));

const stereoDiagram = Hybrid.getDiagramState('stereo', 'left');
assert(!stereoDiagram.subs.left.visible && !stereoDiagram.subs.right.visible);
const oneSubDiagram = Hybrid.getDiagramState('subwoofer-2.1', 'left');
assert(oneSubDiagram.subs.left.visible && oneSubDiagram.subs.left.single && !oneSubDiagram.subs.right.visible);
const monoSubsDiagram = Hybrid.getDiagramState('subwoofer-2.2', 'left');
assert(monoSubsDiagram.subs.left.active && monoSubsDiagram.subs.right.active);
const stereoSubsLeft = Hybrid.getDiagramState('subwoofer-2.2-stereo', 'left');
assert(stereoSubsLeft.subs.left.active && !stereoSubsLeft.subs.right.active);
const stereoSubsRight = Hybrid.getDiagramState('subwoofer-2.2-stereo', 'right');
assert(!stereoSubsRight.subs.left.active && stereoSubsRight.subs.right.active);

console.log('hybrid measurement tests: ok');
