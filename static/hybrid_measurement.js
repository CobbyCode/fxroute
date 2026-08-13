// SPDX-License-Identifier: AGPL-3.0-only
(function (root, factory) {
    const api = factory();
    root.FXRouteHybridMeasurement = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    const CONFIG = Object.freeze({
        listeningWeights: Object.freeze({ mlp: 0.70, left: 0.15, right: 0.15 }),
        spatialConsistencyDb: 6.0,
        bassNullVetoHz: 300,
        bassNullVetoStartDb: 2.5,
        bassNullVetoFullDb: 7.0,
        minimumBoostConfidence: 0.05,
        minimumCutConfidence: 0.55,
        integrationMinHz: 20,
        integrationMaxHz: 500,
        directTimingMaxDeltaMs: 2.5,
    });

    const MODE_LABELS = {
        stereo: 'Stereo',
        'subwoofer-2.1': '2.1',
        'subwoofer-2.2': '2.2 Mono',
        'subwoofer-2.2-stereo': '2.2 Stereo',
    };

    function step(id, role, position, channel, title, instruction, active, move) {
        return { id, role, position, channel, title, instruction, active, move };
    }

    function buildSequence(mode = 'stereo') {
        const normalized = MODE_LABELS[mode] ? mode : 'stereo';
        const steps = [
            step('direct-left', 'direct', 'direct-left', 'left', 'Direct response · Left', 'Place the microphone about 1 m from the left speaker on its listening axis.', 'Left speaker', true),
            step('direct-right', 'direct', 'direct-right', 'right', 'Direct response · Right', 'Place the microphone about 1 m from the right speaker on its listening axis.', 'Right speaker', true),
            step('mlp-left', 'mlp', 'mlp', 'left', 'Main listening position', 'Move the microphone to ear height at the main listening position. Left and right will be measured automatically.', 'Left speaker', true),
            step('mlp-right', 'mlp', 'mlp', 'right', 'Main listening position', 'Keep the microphone here.', 'Right speaker', false),
            step('area-left-l', 'secondary', 'left', 'left', 'Left listening position', 'Move the microphone 20–30 cm left of the main listening position. Left and right will be measured automatically.', 'Left speaker', true),
            step('area-left-r', 'secondary', 'left', 'right', 'Left listening position', 'Keep the microphone here.', 'Right speaker', false),
            step('area-right-l', 'secondary', 'right', 'left', 'Right listening position', 'Move the microphone 20–30 cm right of the main listening position. Left and right will be measured automatically.', 'Left speaker', true),
            step('area-right-r', 'secondary', 'right', 'right', 'Right listening position', 'Keep the microphone here.', 'Right speaker', false),
        ];
        if (normalized !== 'stereo') {
            steps.push(step('integration', 'integration', 'mlp', 'stereo', 'Subwoofer alignment check', 'Move the microphone back to the main listening position.', 'L + R with subwoofer routing', true));
        }
        return { mode: normalized, modeLabel: MODE_LABELS[normalized], steps };
    }

    function getPositionSeriesEnd(steps, startIndex) {
        const position = steps[startIndex]?.position;
        let endIndex = startIndex;
        while (endIndex + 1 < steps.length && steps[endIndex + 1].position === position) endIndex += 1;
        return endIndex;
    }

    function getPreviousPositionIndex(steps, currentIndex) {
        if (currentIndex <= 0) return 0;
        const previousPosition = steps[currentIndex - 1]?.position;
        let index = currentIndex - 1;
        while (index > 0 && steps[index - 1]?.position === previousPosition) index -= 1;
        return index;
    }

    function interpolate(points, frequency) {
        if (!Array.isArray(points) || !points.length) return 0;
        if (frequency <= points[0][0]) return Number(points[0][1]) || 0;
        for (let index = 1; index < points.length; index += 1) {
            if (frequency <= points[index][0]) {
                const left = points[index - 1];
                const right = points[index];
                const ratio = (Math.log(frequency) - Math.log(left[0])) / Math.max(1e-9, Math.log(right[0]) - Math.log(left[0]));
                return Number(left[1]) + ((Number(right[1]) - Number(left[1])) * ratio);
            }
        }
        return Number(points[points.length - 1][1]) || 0;
    }

    function tracePoints(measurement) {
        return measurement?.traces?.find(trace => Array.isArray(trace.points) && trace.points.length)?.points || [];
    }

    function findCapture(captures, role, position, channel) {
        return captures.find(item => item.role === role && item.position === position && item.channel === channel)?.measurement || null;
    }

    function buildListeningModel(captures, channel) {
        const mlp = findCapture(captures, 'mlp', 'mlp', channel);
        const left = findCapture(captures, 'secondary', 'left', channel);
        const right = findCapture(captures, 'secondary', 'right', channel);
        if (!mlp || !left || !right) throw new Error(`Missing ${channel} listening-area measurements`);
        const mlpPoints = tracePoints(mlp);
        const leftPoints = tracePoints(left);
        const rightPoints = tracePoints(right);
        const points = [];
        const constraints = [];
        mlpPoints.forEach(([frequency, mlpDb]) => {
            const leftDb = interpolate(leftPoints, frequency);
            const rightDb = interpolate(rightPoints, frequency);
            const roomDb = (mlpDb * CONFIG.listeningWeights.mlp)
                + (leftDb * CONFIG.listeningWeights.left)
                + (rightDb * CONFIG.listeningWeights.right);
            const spread = Math.max(mlpDb, leftDb, rightDb) - Math.min(mlpDb, leftDb, rightDb);
            const consistency = Math.exp(-spread / CONFIG.spatialConsistencyDb);
            const secondaryAboveMlp = ((leftDb + rightDb) / 2) - mlpDb;
            const vetoRatio = frequency <= CONFIG.bassNullVetoHz
                ? Math.min(1, Math.max(0, (secondaryAboveMlp - CONFIG.bassNullVetoStartDb)
                    / (CONFIG.bassNullVetoFullDb - CONFIG.bassNullVetoStartDb)))
                : 0;
            points.push([frequency, roomDb]);
            constraints.push({
                frequency,
                boostConfidence: Math.max(CONFIG.minimumBoostConfidence, consistency * (1 - vetoRatio)),
                cutConfidence: CONFIG.minimumCutConfidence + ((1 - CONFIG.minimumCutConfidence) * consistency),
                spatialSpreadDb: spread,
                bassNullVeto: vetoRatio,
            });
        });
        return { points, constraints, timingMeasurement: mlp };
    }

    function getDirectModelWeight(frequency, gatedDirectLowerLimitHz, directConfidence, disagreementDb, spatialSpreadDb) {
        if (frequency < gatedDirectLowerLimitHz) return 0;
        const confidence = Math.min(1, Math.max(0, Number(directConfidence) || 0));
        const agreement = Math.exp(-Math.abs(Number(disagreementDb) || 0) / CONFIG.spatialConsistencyDb);
        const spatialConsistency = Math.exp(-Math.max(0, Number(spatialSpreadDb) || 0) / CONFIG.spatialConsistencyDb);
        return confidence * (agreement + ((1 - agreement) * (1 - spatialConsistency)));
    }

    function buildHybridSide(captures, channel) {
        const direct = findCapture(captures, 'direct', `direct-${channel}`, channel);
        if (!direct) throw new Error(`Missing ${channel} direct measurement`);
        const directResponse = direct.analysis?.direct_response || {};
        if (!directResponse.usable || !Array.isArray(directResponse.points) || !directResponse.points.length) {
            throw new Error(`${channel} direct response has no usable reflection-free window`);
        }
        const room = buildListeningModel(captures, channel);
        const gatedDirectLowerLimitHz = Number(directResponse.gated_direct_lower_limit_hz);
        const directConfidence = Number(directResponse.direct_confidence) || 0;
        const overlapOffsets = room.points
            .filter(([frequency]) => frequency >= gatedDirectLowerLimitHz)
            .map(([frequency, roomDb]) => roomDb - interpolate(directResponse.points, frequency))
            .sort((left, right) => left - right);
        const middle = Math.floor(overlapOffsets.length / 2);
        const directLevelOffsetDb = overlapOffsets.length
            ? (overlapOffsets.length % 2 ? overlapOffsets[middle] : (overlapOffsets[middle - 1] + overlapOffsets[middle]) / 2)
            : 0;
        const points = room.points.map(([frequency, roomDb], index) => {
            const directDb = interpolate(directResponse.points, frequency) + directLevelOffsetDb;
            const directWeight = getDirectModelWeight(
                frequency,
                gatedDirectLowerLimitHz,
                directConfidence,
                roomDb - directDb,
                room.constraints[index].spatialSpreadDb,
            );
            return [frequency, (roomDb * (1 - directWeight)) + (directDb * directWeight)];
        });
        const constraints = room.constraints.map(constraint => {
            const roomDb = interpolate(room.points, constraint.frequency);
            const directDb = interpolate(directResponse.points, constraint.frequency) + directLevelOffsetDb;
            const directWeight = getDirectModelWeight(
                constraint.frequency,
                gatedDirectLowerLimitHz,
                directConfidence,
                roomDb - directDb,
                constraint.spatialSpreadDb,
            );
            const roomWeight = 1 - directWeight;
            return {
                ...constraint,
                boostConfidence: (constraint.boostConfidence * roomWeight) + directWeight,
                cutConfidence: (constraint.cutConfidence * roomWeight) + directWeight,
                bassNullVeto: constraint.bassNullVeto * roomWeight,
                spatialWeight: roomWeight,
            };
        });
        return {
            channel,
            points,
            constraints,
            timingMeasurement: room.timingMeasurement,
            modelBlend: {
                gatedDirectLowerLimitHz,
                directConfidence,
                directLevelOffsetDb,
                method: 'direct-confidence-agreement-spatial-consistency',
            },
        };
    }

    function complexAt(points, index) {
        return { real: Number(points[index]?.[1]) || 0, imag: Number(points[index]?.[2]) || 0 };
    }

    function isUsableDirectMeasurement(measurement) {
        const direct = measurement?.analysis?.direct_response;
        return !!direct?.usable && Array.isArray(direct.points) && direct.points.length > 0;
    }

    function getDirectTiming(measurement) {
        const analysis = measurement?.analysis || {};
        const reference = analysis.reference_path || {};
        const impulse = analysis.impulse_response || {};
        const timingMs = Number(reference.acoustic_arrival_corrected_ms ?? impulse.arrival_ms);
        const captureMode = String(reference.capture_mode || '');
        const timingStatus = String(reference.timing_status || '');
        if (!Number.isFinite(timingMs) || timingMs <= 0 || !captureMode) return null;
        if (timingStatus.includes('fallback') || timingStatus.includes('unstable')) return null;
        return { timingMs, captureMode, timingStatus };
    }

    function validateDirectMicrophonePosition(measurement, captures, channel) {
        const otherChannel = channel === 'left' ? 'right' : 'left';
        const other = findCapture(captures, 'direct', `direct-${otherChannel}`, otherChannel);
        const currentTiming = getDirectTiming(measurement);
        const otherTiming = getDirectTiming(other);
        if (!currentTiming || !otherTiming || currentTiming.captureMode !== otherTiming.captureMode) {
            return {
                available: false,
                plausible: true,
                method: 'paired-direct-relative-timing',
                reason: 'Comparable L/R timing is not available; no absolute distance is inferred.',
            };
        }
        const deltaMs = Math.abs(currentTiming.timingMs - otherTiming.timingMs);
        const plausible = deltaMs <= CONFIG.directTimingMaxDeltaMs;
        return {
            available: true,
            plausible,
            deltaMs,
            maxDeltaMs: CONFIG.directTimingMaxDeltaMs,
            equivalentPathDifferenceM: deltaMs * 0.343,
            method: 'paired-direct-relative-timing',
            reason: plausible
                ? 'L/R direct timing is consistent with comparable microphone distances.'
                : `The microphone appears to be too far from the ${channel} speaker. Move it approximately 1 m in front of that speaker and repeat the measurement.`,
        };
    }

    function validateComplexSum(leftMeasurement, rightMeasurement, actualMeasurement = null) {
        const left = leftMeasurement?.analysis?.complex_response?.points || [];
        const right = rightMeasurement?.analysis?.complex_response?.points || [];
        const actual = actualMeasurement?.analysis?.complex_response?.points || [];
        const count = Math.min(left.length, right.length, actual.length || Infinity);
        const predicted = [];
        let squaredError = 0;
        let squaredPhaseError = 0;
        let squaredComplexResidual = 0;
        let compared = 0;
        for (let index = 0; index < count; index += 1) {
            const frequency = Number(left[index][0]);
            if (frequency < CONFIG.integrationMinHz) continue;
            if (frequency > CONFIG.integrationMaxHz) break;
            if (Math.abs(frequency - Number(right[index]?.[0])) > Math.max(0.1, frequency * 0.001)) continue;
            const l = complexAt(left, index);
            const r = complexAt(right, index);
            const sum = { real: l.real + r.real, imag: l.imag + r.imag };
            const magnitude = Math.hypot(sum.real, sum.imag);
            predicted.push([frequency, sum.real, sum.imag]);
            if (actual.length) {
                const measured = complexAt(actual, index);
                if (Math.abs(frequency - Number(actual[index]?.[0])) > Math.max(0.1, frequency * 0.001)) continue;
                const deltaDb = 20 * Math.log10(Math.max(1e-12, Math.hypot(measured.real, measured.imag)) / Math.max(1e-12, magnitude));
                const predictedPhase = Math.atan2(sum.imag, sum.real);
                const measuredPhase = Math.atan2(measured.imag, measured.real);
                const phaseError = Math.abs(Math.atan2(Math.sin(measuredPhase - predictedPhase), Math.cos(measuredPhase - predictedPhase))) * 180 / Math.PI;
                const residual = Math.hypot(measured.real - sum.real, measured.imag - sum.imag) / Math.max(1e-12, magnitude);
                squaredError += deltaDb * deltaDb;
                squaredPhaseError += phaseError * phaseError;
                squaredComplexResidual += residual * residual;
                compared += 1;
            }
        }
        const rmsErrorDb = compared ? Math.sqrt(squaredError / compared) : null;
        const phaseRmsErrorDeg = compared ? Math.sqrt(squaredPhaseError / compared) : null;
        const complexResidualRms = compared ? Math.sqrt(squaredComplexResidual / compared) : null;
        const status = rmsErrorDb === null
            ? 'predicted'
            : (rmsErrorDb <= 3 && phaseRmsErrorDeg <= 30 && complexResidualRms <= 0.35
                ? 'ok'
                : (rmsErrorDb <= 6 && phaseRmsErrorDeg <= 60 && complexResidualRms <= 0.7 ? 'warning' : 'poor'));
        return {
            predicted,
            comparedPoints: compared,
            rmsErrorDb,
            magnitudeRmsErrorDb: rmsErrorDb,
            phaseRmsErrorDeg,
            complexResidualRms,
            validationBandHz: [CONFIG.integrationMinHz, CONFIG.integrationMaxHz],
            status,
            limitation: 'L+R validates response consistency; it cannot isolate poor Main/Sub summation already present inside an individual L or R capture.',
        };
    }

    function getDiagramState(mode, channel, complete = false) {
        const normalized = MODE_LABELS[mode] ? mode : 'stereo';
        const stereoStep = channel === 'stereo' || complete;
        const activeChannel = complete ? 'stereo' : channel;
        const subs = {
            left: { visible: false, active: false, label: 'SUB 1', single: false },
            right: { visible: false, active: false, label: 'SUB 2', single: false },
        };
        if (normalized === 'subwoofer-2.1') {
            subs.left = { visible: true, active: !!activeChannel, label: 'SUB', single: true };
        } else if (normalized === 'subwoofer-2.2') {
            subs.left.visible = subs.right.visible = true;
            subs.left.active = subs.right.active = !!activeChannel;
        } else if (normalized === 'subwoofer-2.2-stereo') {
            subs.left.visible = subs.right.visible = true;
            subs.left.active = stereoStep || activeChannel === 'left';
            subs.right.active = stereoStep || activeChannel === 'right';
        }
        return {
            speakers: {
                left: stereoStep || activeChannel === 'left',
                right: stereoStep || activeChannel === 'right',
            },
            subs,
        };
    }

    function buildProfile(captures, mode = 'stereo') {
        const mlpLeft = findCapture(captures, 'mlp', 'mlp', 'left');
        const mlpRight = findCapture(captures, 'mlp', 'mlp', 'right');
        const integrationMeasurement = findCapture(captures, 'integration', 'mlp', 'stereo');
        const integration = validateComplexSum(mlpLeft, mlpRight, integrationMeasurement);
        if (mode !== 'stereo' && integration.status === 'poor') {
            const error = new Error('System integration check failed. Verify routing and microphone position, then repeat the integration measurement.');
            error.retryRole = 'integration';
            throw error;
        }
        const left = buildHybridSide(captures, 'left');
        const right = buildHybridSide(captures, 'right');
        return { mode, left, right, integration };
    }

    return {
        CONFIG,
        MODE_LABELS,
        buildSequence,
        getPositionSeriesEnd,
        getPreviousPositionIndex,
        buildListeningModel,
        getDirectModelWeight,
        buildHybridSide,
        isUsableDirectMeasurement,
        validateDirectMicrophonePosition,
        validateComplexSum,
        buildProfile,
        getDiagramState,
        interpolate,
    };
});
