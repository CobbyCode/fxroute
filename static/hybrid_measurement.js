// SPDX-License-Identifier: AGPL-3.0-only
(function (root, factory) {
    const api = factory();
    root.FXRouteHybridMeasurement = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    const CONFIG = Object.freeze({
        listeningWeights: Object.freeze({ mlp: 0.70, left: 0.15, right: 0.15 }),
        roomResidualAtHighFrequency: 0.15,
        transitionStartFactor: 0.80,
        transitionEndFactor: 2.20,
        spatialConsistencyDb: 6.0,
        bassNullVetoHz: 300,
        bassNullVetoStartDb: 2.5,
        bassNullVetoFullDb: 7.0,
        minimumBoostConfidence: 0.05,
        minimumCutConfidence: 0.55,
        integrationMaxHz: 2000,
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
            step('mlp-left', 'mlp', 'mlp', 'left', 'Primary position · Left', 'Move the microphone to ear height at the primary listening position.', 'Left speaker', true),
            step('mlp-right', 'mlp', 'mlp', 'right', 'Primary position · Right', 'Keep the microphone here.', 'Right speaker', false),
            step('area-left-l', 'secondary', 'left', 'left', 'Listening area left · Left', 'Move the microphone 20–30 cm left of the primary position.', 'Left speaker', true),
            step('area-left-r', 'secondary', 'left', 'right', 'Listening area left · Right', 'Keep the microphone here.', 'Right speaker', false),
            step('area-right-l', 'secondary', 'right', 'left', 'Listening area right · Left', 'Move the microphone 20–30 cm right of the primary position.', 'Left speaker', true),
            step('area-right-r', 'secondary', 'right', 'right', 'Listening area right · Right', 'Keep the microphone here.', 'Right speaker', false),
        ];
        if (normalized !== 'stereo') {
            steps.push(step('integration', 'integration', 'mlp', 'stereo', 'System integration check', 'Move the microphone back to the primary listening position.', 'L + R with configured subwoofer routing', true));
        }
        return { mode: normalized, modeLabel: MODE_LABELS[normalized], steps };
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

    function getHybridDirectWeight(frequency, lowerReliableHz) {
        const start = Math.max(20, lowerReliableHz * CONFIG.transitionStartFactor);
        const end = Math.max(start * 1.15, lowerReliableHz * CONFIG.transitionEndFactor);
        if (frequency <= start) return 0;
        if (frequency >= end) return 1 - CONFIG.roomResidualAtHighFrequency;
        const ratio = (Math.log(frequency) - Math.log(start)) / (Math.log(end) - Math.log(start));
        return (1 - CONFIG.roomResidualAtHighFrequency) * (0.5 - (0.5 * Math.cos(Math.PI * ratio)));
    }

    function buildHybridSide(captures, channel) {
        const direct = findCapture(captures, 'direct', `direct-${channel}`, channel);
        if (!direct) throw new Error(`Missing ${channel} direct measurement`);
        const directResponse = direct.analysis?.direct_response || {};
        if (!directResponse.usable || !Array.isArray(directResponse.points) || !directResponse.points.length) {
            throw new Error(`${channel} direct response has no usable reflection-free window`);
        }
        const room = buildListeningModel(captures, channel);
        const lowerReliableHz = Number(directResponse.lower_reliable_hz) || 500;
        const transitionStartHz = Math.max(20, lowerReliableHz * CONFIG.transitionStartFactor);
        const transitionEndHz = Math.max(transitionStartHz * 1.15, lowerReliableHz * CONFIG.transitionEndFactor);
        const overlapOffsets = room.points
            .filter(([frequency]) => frequency >= transitionStartHz && frequency <= transitionEndHz)
            .map(([frequency, roomDb]) => roomDb - interpolate(directResponse.points, frequency))
            .sort((left, right) => left - right);
        const middle = Math.floor(overlapOffsets.length / 2);
        const directLevelOffsetDb = overlapOffsets.length
            ? (overlapOffsets.length % 2 ? overlapOffsets[middle] : (overlapOffsets[middle - 1] + overlapOffsets[middle]) / 2)
            : 0;
        const points = room.points.map(([frequency, roomDb]) => {
            const directWeight = getHybridDirectWeight(frequency, lowerReliableHz);
            const directDb = interpolate(directResponse.points, frequency) + directLevelOffsetDb;
            return [frequency, (roomDb * (1 - directWeight)) + (directDb * directWeight)];
        });
        return {
            channel,
            points,
            constraints: room.constraints,
            timingMeasurement: room.timingMeasurement,
            transition: {
                lowerReliableHz,
                startHz: transitionStartHz,
                endHz: transitionEndHz,
                directLevelOffsetDb,
                highFrequencyRoomWeight: CONFIG.roomResidualAtHighFrequency,
            },
        };
    }

    function complexAt(points, index) {
        return { real: Number(points[index]?.[1]) || 0, imag: Number(points[index]?.[2]) || 0 };
    }

    function validateComplexSum(leftMeasurement, rightMeasurement, actualMeasurement = null) {
        const left = leftMeasurement?.analysis?.complex_response?.points || [];
        const right = rightMeasurement?.analysis?.complex_response?.points || [];
        const actual = actualMeasurement?.analysis?.complex_response?.points || [];
        const count = Math.min(left.length, right.length, actual.length || Infinity);
        const predicted = [];
        let squaredError = 0;
        let compared = 0;
        for (let index = 0; index < count; index += 1) {
            const frequency = Number(left[index][0]);
            if (frequency > CONFIG.integrationMaxHz) break;
            const l = complexAt(left, index);
            const r = complexAt(right, index);
            const sum = { real: l.real + r.real, imag: l.imag + r.imag };
            const magnitude = Math.hypot(sum.real, sum.imag);
            predicted.push([frequency, sum.real, sum.imag]);
            if (actual.length) {
                const measured = complexAt(actual, index);
                const deltaDb = 20 * Math.log10(Math.max(1e-12, Math.hypot(measured.real, measured.imag)) / Math.max(1e-12, magnitude));
                squaredError += deltaDb * deltaDb;
                compared += 1;
            }
        }
        const rmsErrorDb = compared ? Math.sqrt(squaredError / compared) : null;
        return {
            predicted,
            comparedPoints: compared,
            rmsErrorDb,
            status: rmsErrorDb === null ? 'predicted' : (rmsErrorDb <= 3 ? 'ok' : (rmsErrorDb <= 6 ? 'warning' : 'poor')),
        };
    }

    function buildProfile(captures, mode = 'stereo') {
        const left = buildHybridSide(captures, 'left');
        const right = buildHybridSide(captures, 'right');
        const mlpLeft = findCapture(captures, 'mlp', 'mlp', 'left');
        const mlpRight = findCapture(captures, 'mlp', 'mlp', 'right');
        const integration = findCapture(captures, 'integration', 'mlp', 'stereo');
        return { mode, left, right, integration: validateComplexSum(mlpLeft, mlpRight, integration) };
    }

    return {
        CONFIG,
        MODE_LABELS,
        buildSequence,
        buildListeningModel,
        getHybridDirectWeight,
        buildHybridSide,
        validateComplexSum,
        buildProfile,
        interpolate,
    };
});
