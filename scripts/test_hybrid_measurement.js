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
// Hybrid wizard flow bodies live in measurement_flows.js since the phase-4 split.
const flowsSource = fs.readFileSync(path.join(root, 'static', 'measurement_flows.js'), 'utf8');
assert(indexSource.includes('id="measurement-hybrid-panel"'));
assert(indexSource.indexOf('hybrid_measurement.js') < indexSource.indexOf('app.js?v='), 'hybrid model must load before app');
assert(flowsSource.includes("formData.append('measurement_role', step.role)"));
assert(flowsSource.includes("hybrid_constraints: model.constraints"));
assert(flowsSource.includes('HybridMeasurement.isUsableDirectMeasurement(measurement)'));
const jobSource = fs.readFileSync(path.join(root, 'static', 'measurement_job.js'), 'utf8');
assert(jobSource.includes("'Cancel measurement'"));
assert(appSource.includes('cancelHybridWizardMeasurement'));
assert(flowsSource.includes("processing ? `Processing ${step.channel"));
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
assert.equal(Hybrid.getDirectModelWeight(300, lower, 0.8, 0, 0), 0, 'direct model must fade in above its gate limit');
assert.equal(Hybrid.getDirectModelWeight(400, lower, 0.8, 0, 0), 0.8, 'direct confidence must cap its model weight');
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
assert(wrongRightPosition.reason.includes('L/R direct timing is inconsistent'));
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

// A narrow L/R cancellation must not turn small absolute capture noise into
// a system-wide integration failure through per-bin division by nearly zero.
const cancellationLeft = { points: [[40, 1, 0], [80, 1, 0], [160, 1, 0], [320, 1, 0]] };
const cancellationRight = { points: [[40, -0.999, 0], [80, 1, 0], [160, 1, 0], [320, 1, 0]] };
const noisyCancellation = Hybrid.validateComplexSum(
    measurement('left', 0, { complexResponse: cancellationLeft }),
    measurement('right', 0, { complexResponse: cancellationRight }),
    measurement('stereo', 0, { complexResponse: { points: [[40, 0.003, 0], [80, 2, 0], [160, 2, 0], [320, 2, 0]] } }),
);
assert.equal(noisyCancellation.status, 'warning', 'a narrow noisy null may warn but must not block the run');
assert(noisyCancellation.complexResidualRms < 0.001, 'complex residual must reflect the small error energy');

// Broadband routing faults must still block, including a missing output and
// a lost or inverted bass branch, even when the high band agrees exactly.
const balancedSide = { points: [[40, 1, 0], [80, 1, 0], [160, 1, 0], [320, 1, 0]] };
for (const [fault, actualPoints] of [
    ['missing right', [[40, 1, 0], [80, 1, 0], [160, 1, 0], [320, 1, 0]]],
    ['missing bass', [[40, 0, 0], [80, 0, 0], [160, 2, 0], [320, 2, 0]]],
    ['inverted bass', [[40, -2, 0], [80, -2, 0], [160, 2, 0], [320, 2, 0]]],
]) {
    const result = Hybrid.validateComplexSum(
        measurement('left', 0, { complexResponse: balancedSide }),
        measurement('right', 0, { complexResponse: balancedSide }),
        measurement('stereo', 0, { complexResponse: { points: actualPoints } }),
    );
    assert.equal(result.status, 'poor', `${fault} must still fail integration`);
}

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
assert(flowsSource.includes('complex residual'));
assert(flowsSource.includes('no separate integration sweep was performed'));
assert(flowsSource.includes('integration.limitation'));

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
assert(flowsSource.includes("error.retryRole === 'integration'"));

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
assert.deepEqual([stereoDiagram.layout, stereoDiagram.speakers], ['pair', { left: true, right: false, stereo: false }]);
assert.deepEqual(Hybrid.getDiagramState('stereo', 'stereo').speakers, { left: true, right: true, stereo: false });

// A scope describes what one run measures: a speaker pair, one mono channel, or
// subwoofers without the 1 m direct response.
const subTopology = { sub_mode: 'dual-mono' };
const rearScope = Hybrid.buildScope({ bank_id: 'global', group: 'rear', label: 'Global · Rear L/R + Sub', channel_mode: 'stereo' }, subTopology);
const centerScope = Hybrid.buildScope({ bank_id: 'global', group: 'center', label: 'Global · Center + Sub', channel_mode: 'mono' }, subTopology);
const subScope = Hybrid.buildScope({ bank_id: 'sub1', label: 'Sub 1', channel_mode: 'mono' }, subTopology);
const subPairScope = Hybrid.buildScope({ bank_id: 'sub', label: 'Sub L/R', channel_mode: 'stereo' }, { sub_mode: 'stereo' });
const stereoScope = Hybrid.buildScope({ bank_id: 'global', label: 'Global', channel_mode: 'stereo' }, { sub_mode: 'none' });
const legacyIds = Hybrid.buildSequence('subwoofer-2.2').steps.map(item => item.id);
assert.deepEqual(Hybrid.buildSequence(rearScope).steps.map(item => item.id), legacyIds, 'a pair scope with subs is the legacy sequence');
assert.equal(Hybrid.buildSequence(rearScope).modeLabel, 'Rear L/R + Sub');
assert.equal(Hybrid.buildSequence(stereoScope).modeLabel, 'Stereo');
assert.deepEqual(Hybrid.buildSequence(stereoScope).steps.map(item => item.id), Hybrid.buildSequence('stereo').steps.map(item => item.id));
const centerSteps = Hybrid.buildSequence(centerScope).steps;
assert.deepEqual(centerSteps.map(item => [item.id, item.role, item.position, item.channel]),
    [['direct-stereo', 'direct', 'direct-stereo', 'stereo'], ['mlp', 'mlp', 'mlp', 'stereo'],
        ['area-left', 'secondary', 'left', 'stereo'], ['area-right', 'secondary', 'right', 'stereo']]);
assert(!centerScope.integration, 'one channel has no second summand for an integration check');
assert.match(centerSteps[0].instruction, /about 1 m from the center speaker/);
assert.doesNotMatch(centerSteps[1].instruction, /Left and right/);
assert.equal(centerSteps[1].active, 'Center speaker');
const subSteps = Hybrid.buildSequence(subScope).steps;
assert.deepEqual(subSteps.map(item => [item.id, item.role, item.channel]),
    [['mlp', 'mlp', 'stereo'], ['area-left', 'secondary', 'stereo'], ['area-right', 'secondary', 'stereo']]);
assert.deepEqual(Hybrid.buildSequence(subPairScope).steps.map(item => [item.role, item.position, item.channel].join('/')),
    ['mlp/mlp/left', 'mlp/mlp/right', 'secondary/left/left', 'secondary/left/right', 'secondary/right/left', 'secondary/right/right']);
assert(!subScope.direct && !subScope.integration && !subPairScope.integration);
assert.equal(Hybrid.getPositionSeriesEnd(Hybrid.buildSequence(subPairScope).steps, 0), 1, 'a sub pair measures L and R per position');
assert.equal(Hybrid.getPositionSeriesEnd(subSteps, 0), 0, 'a mono sub measures one sweep per position');
assert.notEqual(rearScope.id, centerScope.id);
assert.notEqual(Hybrid.buildScope({ bank_id: 'global', group: 'side', label: 'Global · Side L/R + Sub', channel_mode: 'stereo' }, subTopology).id, rearScope.id);
assert.deepEqual(Hybrid.getDiagramState(centerScope, 'stereo').speakers, { left: false, right: false, stereo: true });
assert.equal(Hybrid.getDiagramState(centerScope, 'stereo').layout, 'single');
assert.deepEqual(Hybrid.getDiagramState(subScope, 'stereo').speakers, { left: false, right: false, stereo: false });
assert.equal(Hybrid.getDiagramState(subScope, 'stereo').layout, 'sub');
assert(Hybrid.getDiagramState(subScope, 'stereo').subs.left.active, 'subs stay visible for a sub scope');
// A Surround group target covers every sub; an individual speaker bank mutes
// them, so its room view must not show subs that never play.
const centerBankScope = Hybrid.buildScope({ bank_id: 'center', label: 'Center', channel_mode: 'mono' }, subTopology);
const centerBankDiagram = Hybrid.getDiagramState(centerBankScope, 'stereo');
assert.equal(centerBankScope.includesSubs, false);
assert(!centerBankDiagram.subs.left.visible && !centerBankDiagram.subs.right.visible,
    'an individual Center bank does not measure the subs');
const centerGroupDiagram = Hybrid.getDiagramState(centerScope, 'stereo');
assert(centerGroupDiagram.subs.left.visible && centerGroupDiagram.subs.left.active);
assert(centerGroupDiagram.subs.right.visible && centerGroupDiagram.subs.right.active);
const mainBankScope = Hybrid.buildScope({ bank_id: 'main', label: 'Main L/R', channel_mode: 'stereo' }, { sub_mode: 'stereo' });
assert(!Hybrid.getDiagramState(mainBankScope, 'left').subs.left.visible, 'an individual pair bank mutes the subs');
assert(Hybrid.getDiagramState('subwoofer-2.2', 'left').subs.left.visible, 'the whole-system scope keeps the subs');

// A series visits every speaker nearfield, then each listening position once.
// Routing and capture identities stay the same as the regular group sequences.
{
    const groupNames = { front: 'Front L/R', center: 'Center', side: 'Side L/R', rear: 'Rear L/R' };
    const seriesScopes = Object.keys(groupNames).map(group => Hybrid.buildScope(
        { bank_id: 'global', group, label: `Global · ${groupNames[group]} + Sub`, channel_mode: group === 'center' ? 'mono' : 'stereo' },
        subTopology));
    const series = Hybrid.buildSeriesSequence(seriesScopes);
    assert.equal(series.scope.series, true);
    assert.equal(series.steps.length, 9 + 4 + 9 + 9);
    assert.deepEqual(series.steps.slice(0, 7).map(item => item.id), [
        'front:direct-left', 'front:direct-right', 'center:direct-stereo',
        'side:direct-left', 'side:direct-right', 'rear:direct-left', 'rear:direct-right']);
    assert.deepEqual(series.steps.slice(7, 17).map(item => item.id), [
        'front:mlp-left', 'front:mlp-right', 'front:integration', 'center:mlp',
        'side:mlp-left', 'side:mlp-right', 'side:integration', 'rear:mlp-left', 'rear:mlp-right', 'rear:integration']);
    assert.deepEqual(series.steps.slice(17, 24).map(item => item.id), [
        'front:area-left-l', 'front:area-left-r', 'center:area-left',
        'side:area-left-l', 'side:area-left-r', 'rear:area-left-l', 'rear:area-left-r']);
    assert.deepEqual(series.steps.slice(24).map(item => item.id), [
        'front:area-right-l', 'front:area-right-r', 'center:area-right',
        'side:area-right-l', 'side:area-right-r', 'rear:area-right-l', 'rear:area-right-r']);
    const regularCaptures = seriesScopes.flatMap(scope => Hybrid.buildSequence(scope).steps.map(item =>
        [scope.group, item.id, item.role, item.position, item.channel].join('/'))).sort();
    assert.deepEqual(series.steps.map(item =>
        [item.group, item.id.split(':')[1], item.role, item.position, item.channel].join('/')).sort(), regularCaptures,
    'reordering must retain every existing capture and its identity');
    assert.deepEqual([...new Set(series.steps.map(item => item.group))], ['front', 'center', 'side', 'rear']);
    assert.equal(new Set(series.steps.map(item => item.id)).size, series.steps.length);
    assert.equal(series.steps[2].active, 'Center speaker', 'a single speaker already names itself');
    assert.equal(series.steps[0].active, 'Front L/R · Left speaker');
    assert.equal(Hybrid.scopeForStep(series.scope, series.steps[10]), seriesScopes[1]);
    assert.equal(Hybrid.scopeForStep(rearScope, series.steps[10]), rearScope, 'a plain scope is its own step scope');
    for (let index = 0; index < 7; index += 1) {
        assert.equal(Hybrid.getPositionSeriesEnd(series.steps, index), index, 'nearfield needs one start per speaker');
        assert.equal(series.steps[index].move, true);
    }
    for (const [start, end, previous] of [[7, 16, 6], [17, 23, 7], [24, 30, 17]]) {
        assert.equal(Hybrid.getPositionSeriesEnd(series.steps, start), end, 'one start measures every group at this position');
        assert.equal(Hybrid.getPositionSeriesEnd(series.steps, start + 2), end, 'a retry continues the remaining position batch');
        assert.equal(Hybrid.getPreviousPositionIndex(series.steps, start), previous);
        assert.equal(series.steps[start].move, true);
        assert.match(series.steps[start].instruction, /All speakers will be measured automatically/);
        assert(series.steps.slice(start + 1, end + 1).every(item => !item.move && item.instruction === 'Keep the microphone here.'));
    }
    const joinedDirect = [{ position: 'direct-stereo', group: 'a', move: true },
        { position: 'direct-stereo', group: 'b', move: true }];
    assert.equal(Hybrid.getPositionSeriesEnd(joinedDirect, 0), 0, 'equal nearfield labels are separate physical positions');
    assert.equal(Hybrid.getPreviousPositionIndex(joinedDirect, 2), 1);
    const partial = Hybrid.buildSeriesSequence([seriesScopes[0], seriesScopes[3]]).steps;
    assert.deepEqual(partial.filter(item => item.move).map(item => item.id), [
        'front:direct-left', 'front:direct-right', 'rear:direct-left', 'rear:direct-right',
        'front:mlp-left', 'front:area-left-l', 'front:area-right-l']);
    const noSubs = Hybrid.buildSeriesSequence(['front', 'center'].map(group => Hybrid.buildScope(
        { bank_id: 'global', group, label: groupNames[group], channel_mode: group === 'center' ? 'mono' : 'stereo' }, {}))).steps;
    assert.equal(noSubs.length, 12);
    assert.equal(noSubs.filter(item => item.role === 'integration').length, 0);
    assert.equal(Hybrid.getPositionSeriesEnd(noSubs, 3), 5, 'Main includes Center even without subs');
    assert.equal(rearScope.group, 'rear');
    assert.equal(Hybrid.getDiagramState(rearScope, 'left').group, 'rear');
    assert.equal(Hybrid.getDiagramState('stereo', 'left').group, '');
    // Captures of a group, and the finished series in series order.
    const tagged = [{ group: 'front', stepId: 'a' }, { group: 'rear', stepId: 'b' }, { stepId: 'c' }];
    assert.deepEqual(Hybrid.groupCaptures(tagged, 'rear').map(item => item.stepId), ['b']);
    assert.deepEqual(Hybrid.groupCaptures(tagged, '').map(item => item.stepId), ['c']);
    const profile = Hybrid.buildSeriesProfile(series.scope, { front: { id: 1 }, center: { id: 2 }, side: { id: 3 }, rear: { id: 4 } });
    assert.deepEqual(profile.parts.map(part => part.profile.id), [1, 2, 3, 4]);
    assert.throws(() => Hybrid.buildSeriesProfile(series.scope, { front: {} }), /Missing .* measurement profile/);
}

// One channel: the profile carries one model and no integration; a sub scope
// uses the listening-position model alone (no direct blend).
const monoCaptures = [capture('direct', 'direct-stereo', 'stereo', measurement('stereo', 0, { directResponse })),
    capture('mlp', 'mlp', 'stereo', measurement('stereo', 0)),
    capture('secondary', 'left', 'stereo', measurement('stereo', 0)),
    capture('secondary', 'right', 'stereo', measurement('stereo', 0))];
const monoProfile = Hybrid.buildProfile(monoCaptures, centerScope);
assert.deepEqual(Object.keys(monoProfile.models), ['stereo']);
assert.equal(monoProfile.integration, null);
assert.equal(monoProfile.left, null);
assert.equal(monoProfile.models.stereo.modelBlend.gatedDirectLowerLimitHz, 300);
const subProfile = Hybrid.buildProfile(monoCaptures.slice(1), subScope);
assert.equal(subProfile.models.stereo.modelBlend, null);
assert.equal(subProfile.models.stereo.points.length, 7);
assert.equal(subProfile.models.stereo.constraints.length, 7);
assert.strictEqual(subProfile.models.stereo.timingMeasurement, monoCaptures[1].measurement);
assert.throws(() => Hybrid.buildProfile(monoCaptures.slice(2), subScope), /Missing stereo listening-area measurements/);
const pairProfile = Hybrid.buildProfile(profileCaptures, rearScope);
assert.deepEqual(Object.keys(pairProfile.models), ['left', 'right']);
assert.strictEqual(pairProfile.left, pairProfile.models.left);
assert.equal(pairProfile.mode, 'subwoofer-2.2');

console.log('hybrid measurement tests: ok');
