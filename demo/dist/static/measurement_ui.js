// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute measurement UI leaf helpers.
 * Pure normalization/formatting/geometry/state-default helpers extracted
 * from static/app.js (phase 1 of the measurement block split).
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

const MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS = 6.0;
const MEASUREMENT_JOB_CANCELLED_STATES = new Set(['cancelled', 'canceled']);
const MEASUREMENT_JOB_FAILED_STATES = new Set(['failed', 'failure', 'error']);
const MEASUREMENT_JOB_SUCCESS_STATES = new Set(['completed', 'complete', 'finished', 'success', 'ready', 'done', 'ok']);
const MEASUREMENT_LR_REPEAT_DELTA_TOOLTIP = 'L/R delta is calculated as Right - Left. Positive means the right channel arrives later.';
const measurementComparePalette = ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa', '#f87171', '#facc15'];
const measurementCurrentColor = '#22c55e';
const measurementPeqPalette = ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa'];
const measurementPeqTypes = ['bell', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass', 'notch', 'gain'];
const measurementPeqTypeLabels = {
    bell: 'Bell',
    low_shelf: 'Low shelf',
    high_shelf: 'High shelf',
    low_pass: 'Low pass',
    high_pass: 'High pass',
    notch: 'Notch',
    gain: 'Gain',
};
const measurementConvolverPhaseModes = ['linear', 'minimum', 'minimum_aligned', 'hybrid_aligned'];
const measurementConvolverAlignedPhaseModes = ['minimum_aligned', 'hybrid_aligned'];
const measurementConvolverCurves = {
    neutral: { label: 'Neutral', shortLabel: 'Neutral', points: [[20, 0], [20000, 0]] },
    bass_shelf: { label: 'Bass Shelf', shortLabel: 'Bass', points: [[20, 4], [30, 4], [50, 3], [80, 2], [120, 1], [200, 0], [1000, 0], [20000, 0]] },
    harman: { label: 'Harman-style', shortLabel: 'Harman', points: [[20, 5], [30, 4.5], [50, 4], [80, 3], [120, 2], [200, 1], [500, 0.5], [1000, 0], [2000, -1], [5000, -2.5], [10000, -4], [20000, -5]] },
    bk: { label: 'Bruel & Kjaer-style', shortLabel: 'BK', points: [[20, 2], [50, 2], [100, 1.5], [200, 1], [500, 0.5], [1000, 0], [2000, -0.5], [5000, -1.5], [10000, -2.5], [20000, -3.5]] },
};
const measurementConvolverTapOptions = [2048, 4096, 8192, 16384, 32768];
const measurementConvolverTypeOptions = measurementConvolverPhaseModes.flatMap((phaseMode) => measurementConvolverTapOptions.map((taps) => ({
    key: `${phaseMode}_${taps}`,
    phaseMode,
    taps,
    label: `${phaseMode === 'minimum' ? 'Min.' : phaseMode === 'minimum_aligned' ? 'Min.align' : phaseMode === 'hybrid_aligned' ? 'Hybrid aligned' : 'Linear'} phase ${taps}`,
})));

function normalizeMeasurementTrace(trace = {}, index = 0) {
    const points = Array.isArray(trace.points)
        ? trace.points.filter(point => Array.isArray(point) && point.length === 2 && Number.isFinite(Number(point[0])) && Number.isFinite(Number(point[1]))).map(point => [Number(point[0]), Number(point[1])])
        : [];
    return {
        kind: String(trace.kind || 'measured'),
        label: String(trace.label || `Trace ${index + 1}`),
        color: String(trace.color || ['#6ee7b7', '#a78bfa', '#f59e0b', '#60a5fa'][index % 4]),
        role: String(trace.role || ''),
        points,
        // AutoSub calibrated-to-display correction. The graph uses it with
        // saved Main references to move AutoSub traces into its normal axis.
        ...(Number.isFinite(Number(trace.display_offset_db))
            ? { display_offset_db: Number(trace.display_offset_db) }
            : {}),
    };
}

function normalizeMeasurementEntry(measurement = {}, index = 0) {
    const traces = Array.isArray(measurement.traces) ? measurement.traces.map((trace, traceIndex) => normalizeMeasurementTrace(trace, traceIndex)).filter(trace => trace.points.length) : [];
    const reviewTraces = Array.isArray(measurement.review_traces) ? measurement.review_traces.map((trace, traceIndex) => normalizeMeasurementTrace(trace, traceIndex)).filter(trace => trace.points.length) : [];
    return {
        id: String(measurement.id || `measurement-${index + 1}`),
        name: String(measurement.name || `Measurement ${index + 1}`),
        created_at: String(measurement.created_at || ''),
        channel: String(measurement.channel || 'left'),
        measurement_kind: String(measurement.measurement_kind || ''),
        measurement_role: String(measurement.measurement_role || ''),
        input_device: measurement.input_device || {},
        input_channels: measurement.input_channels || {},
        calibration: measurement.calibration || {},
        autosub_meta: measurement.autosub_meta || null,
        summary: measurement.summary || {},
        review_summary: measurement.review_summary || {},
        analysis: measurement.analysis || {},
        audio_output_context: measurement.audio_output_context || {},
        storage_path: measurement.storage_path || '',
        traces,
        review_traces: reviewTraces,
    };
}

function normalizeMeasurementVisibility(measurements = [], previous = {}) {
    const next = {};
    measurements.forEach((measurement) => {
        if (typeof previous?.[measurement.id] === 'boolean') {
            next[measurement.id] = previous[measurement.id];
        } else {
            next[measurement.id] = false;
        }
    });
    return next;
}

function formatMeasurementDate(value) {
    if (!value) return 'Unknown date';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function normalizeMeasurementReviewVisibility(measurements = [], previous = {}) {
    const next = {};
    measurements.forEach((measurement) => {
        next[measurement.id] = typeof previous?.[measurement.id] === 'boolean' ? previous[measurement.id] : false;
    });
    return next;
}

function trackFileUrl(trackId = '') {
    return `/api/tracks/file/${encodeURIComponent(String(trackId || ''))}`;
}

function measurementFileUrl(measurementId = '') {
    return `/api/measurements/${encodeURIComponent(String(measurementId || ''))}/file`;
}

function presetFileUrl(presetName = '') {
    return `/api/dsp/presets/${encodeURIComponent(String(presetName || ''))}/file`;
}

function getDefaultMeasurementPeqFilter(index = 0) {
    return {
        id: `measurement-peq-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        type: 'bell',
        freqHz: 1000,
        gainDb: 0,
        q: 1,
        color: measurementPeqPalette[index % measurementPeqPalette.length] || '#60a5fa',
    };
}

function getDefaultMeasurementPeqState() {
    return {
        enabled: false,
        filters: [],
        activeFilterId: null,
        dragFilterId: null,
        draft: { leftBands: [], rightBands: [], presetName: '', nameTouched: false },
    };
}

function getDefaultMeasurementConvolverState() {
    return {
        targetCurve: 'neutral',
        rangeStartHz: 20,
        rangeEndHz: 250,
        maxBoostDb: 6,
        maxCutDb: -9,
        dipGuard: 'off',
        safetyMarginDb: 1,
        autoGainEnabled: true,
        quality: 'linear_8192',
        phaseMode: 'minimum',
        irLength: '8192',
        dragMode: null,
        creatingPreset: false,
        draft: { left: null, right: null, presetName: '', nameTouched: false, notice: '' },
    };
}

function getMeasurementConvolverDraftPhaseMode(draft = null) {
    const phaseMode = String(draft?.phaseMode || draft?.metadata?.phaseMode || '');
    return measurementConvolverPhaseModes.includes(phaseMode) ? phaseMode : '';
}

function getMeasurementPeqNameSuffix(date = new Date()) {
    const pad = (value) => String(value).padStart(2, '0');
    return `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function getMeasurementConvolverMultiSourceWarning() {
    return 'Select one measurement or one L/R selection.';
}

function formatMeasurementConvolverGain(value) {
    const numeric = Number(value) || 0;
    return `${numeric > 0 ? '+' : ''}${Number.isInteger(numeric) ? numeric.toFixed(0) : numeric.toFixed(1)}dB`;
}

function getMeasurementConvolverNameSuffix(date = new Date()) {
    const pad = (value) => String(value).padStart(2, '0');
    return `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function getMeasurementConvolverPreviewMode(leftAnalysis, rightAnalysis, leftDraft = null, rightDraft = null) {
    if (leftDraft && rightDraft) return 'both';
    if (rightDraft) return 'right';
    if (leftDraft) return 'left';
    if (leftAnalysis && rightAnalysis) return 'both';
    if (rightAnalysis) return 'right';
    if (leftAnalysis) return 'left';
    return 'both';
}

function getMeasurementConvolverPreviewGain(mode = 'both', leftAnalysis = null, rightAnalysis = null, leftDraft = null, rightDraft = null) {
    const left = leftDraft?.analysis || leftAnalysis || null;
    const right = rightDraft?.analysis || rightAnalysis || null;
    const gains = mode === 'both'
        ? [left?.autoGainDb, right?.autoGainDb]
        : [mode === 'right' ? right?.autoGainDb : left?.autoGainDb];
    const numericGains = gains.map(Number).filter(Number.isFinite);
    return numericGains.length ? Math.min(...numericGains) : 0;
}

function getMeasurementConvolverTypeOption(type = 'linear_4096') {
    return measurementConvolverTypeOptions.find((option) => option.key === type) || measurementConvolverTypeOptions[0];
}

function getMeasurementConvolverTypeKeys() {
    return measurementConvolverTypeOptions.map((option) => option.key);
}

function getMeasurementConvolverPhaseModeForType(type = 'linear_4096') {
    return getMeasurementConvolverTypeOption(type).phaseMode;
}

function getMeasurementConvolverPhaseLabel(phaseMode = 'linear') {
    if (phaseMode === 'minimum') return 'Minimum phase FIR';
    if (phaseMode === 'minimum_aligned') return 'Minimum phase aligned FIR';
    if (phaseMode === 'hybrid_aligned') return 'Hybrid aligned FIR';
    return 'Linear FIR';
}

function getMeasurementConvolverPhaseTag(phaseMode = 'linear') {
    if (phaseMode === 'minimum') return 'Min';
    if (phaseMode === 'minimum_aligned') return 'MinAlign';
    if (phaseMode === 'hybrid_aligned') return 'HybAlign';
    return 'Lin';
}

function getMeasurementConvolverFirLengthForType(type = 'linear_4096') {
    return getMeasurementConvolverTypeOption(type).taps;
}

function getMeasurementConvolverTypeLabel(type = 'linear_4096') {
    return getMeasurementConvolverTypeOption(type).label;
}

function getMeasurementConvolverTimingMs(timing = {}) {
    const arrivalMs = Number(timing?.arrivalMs);
    return Number.isFinite(arrivalMs) ? arrivalMs : null;
}

function getMeasurementConvolverTimingDelta(leftTiming, rightTiming) {
    if (!leftTiming?.available || !rightTiming?.available) return null;
    const leftMs = getMeasurementConvolverTimingMs(leftTiming);
    const rightMs = getMeasurementConvolverTimingMs(rightTiming);
    if (leftMs === null || rightMs === null) return null;
    const deltaMs = rightMs - leftMs;
    if (!Number.isFinite(deltaMs)) return null;
    const result = {
        deltaMs,
        absMs: Math.abs(deltaMs),
        laterSide: deltaMs >= 0 ? 'R' : 'L',
        correctionSide: deltaMs >= 0 ? 'L' : 'R',
        leftTiming,
        rightTiming,
    };
    console.debug('[measurement-convolver-timing]', {
        left: {
            channel: leftTiming.channel || 'left',
            source: leftTiming.source || '',
            measurementId: leftTiming.measurementId || '',
            measurementName: leftTiming.measurementName || '',
            correctedArrivalMs: leftMs,
            peakSample: leftTiming.peakSample ?? null,
            directSample: leftTiming.directSample ?? null,
            referencePeakSample: leftTiming.referencePeakSample ?? null,
            referenceAnchorSample: leftTiming.referenceAnchorSample ?? null,
            arrivalSamples: leftTiming.arrivalSamples ?? null,
            selectedScore: leftTiming.selectedScore ?? null,
            selectedSupportScore: leftTiming.selectedSupportScore ?? null,
            confidence: leftTiming.confidence ?? null,
            selectionRule: leftTiming.selectionRule || '',
            firstThresholdSample: leftTiming.firstThresholdSample ?? null,
            firstThresholdOffsetFromPeakSamples: leftTiming.firstThresholdOffsetFromPeakSamples ?? null,
            candidateCount: leftTiming.candidateCount ?? null,
            topCandidates: leftTiming.topCandidates || [],
            chronologicalCandidates: leftTiming.chronologicalCandidates || [],
            sampleRate: leftTiming.sampleRate ?? null,
        },
        right: {
            channel: rightTiming.channel || 'right',
            source: rightTiming.source || '',
            measurementId: rightTiming.measurementId || '',
            measurementName: rightTiming.measurementName || '',
            correctedArrivalMs: rightMs,
            peakSample: rightTiming.peakSample ?? null,
            directSample: rightTiming.directSample ?? null,
            referencePeakSample: rightTiming.referencePeakSample ?? null,
            referenceAnchorSample: rightTiming.referenceAnchorSample ?? null,
            arrivalSamples: rightTiming.arrivalSamples ?? null,
            selectedScore: rightTiming.selectedScore ?? null,
            selectedSupportScore: rightTiming.selectedSupportScore ?? null,
            confidence: rightTiming.confidence ?? null,
            selectionRule: rightTiming.selectionRule || '',
            firstThresholdSample: rightTiming.firstThresholdSample ?? null,
            firstThresholdOffsetFromPeakSamples: rightTiming.firstThresholdOffsetFromPeakSamples ?? null,
            candidateCount: rightTiming.candidateCount ?? null,
            topCandidates: rightTiming.topCandidates || [],
            chronologicalCandidates: rightTiming.chronologicalCandidates || [],
            sampleRate: rightTiming.sampleRate ?? null,
        },
        deltaMs,
        deltaSamples: Number.isFinite(Number(leftTiming.sampleRate)) ? Math.round(deltaMs / 1000 * Number(leftTiming.sampleRate)) : null,
        sampleRate: leftTiming.sampleRate || rightTiming.sampleRate || null,
    });
    return result;
}

function getMeasurementConvolverTimingSafetyMessage(timingDelta, limitMs = MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
    if (!timingDelta || !(timingDelta.absMs > limitMs)) return '';
    return `Timing offset too large: ${timingDelta.absMs.toFixed(2)} ms. Filter was not created. Move the microphone closer to the center position or raise the safety limit for test measurements.`;
}

function alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, leftTiming, rightTiming, maxAlignMs = MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
    const timingDelta = getMeasurementConvolverTimingDelta(leftTiming, rightTiming);
    if (!timingDelta) return [leftImpulse, rightImpulse];
    if (timingDelta.absMs > maxAlignMs) {
        throw new Error(getMeasurementConvolverTimingSafetyMessage(timingDelta, maxAlignMs) || 'Timing offset exceeds safety limit.');
    }
    const deltaMs = timingDelta.deltaMs;
    if (Math.abs(deltaMs) < 0.005) return [leftImpulse, rightImpulse];
    const cappedMs = Math.max(-maxAlignMs, Math.min(maxAlignMs, deltaMs));
    const alignedSamples = Math.round(Math.abs(cappedMs) / 1000 * sampleRate);
    if (alignedSamples < 1) return [leftImpulse, rightImpulse];
    const silence = new Float64Array(alignedSamples);
    if (cappedMs > 0) {
        const newLeft = new Float64Array(alignedSamples + leftImpulse.length);
        newLeft.set(silence, 0);
        newLeft.set(leftImpulse, alignedSamples);
        return [newLeft, rightImpulse];
    } else {
        const newRight = new Float64Array(alignedSamples + rightImpulse.length);
        newRight.set(silence, 0);
        newRight.set(rightImpulse, alignedSamples);
        return [leftImpulse, newRight];
    }
}

function getMeasurementAnalysisSampleRate(measurement = {}) {
    const candidates = [
        measurement?.analysis?.sample_rate,
        measurement?.analysis?.sampleRate,
        measurement?.summary?.sample_rate,
        measurement?.summary?.sampleRate,
        measurement?.review_summary?.sample_rate,
        measurement?.review_summary?.sampleRate,
        measurement?.sample_rate,
        measurement?.sampleRate,
    ];
    const sampleRate = candidates.map(Number).find((value) => Number.isFinite(value) && value > 0);
    return sampleRate || null;
}

function getMeasurementDirectArrivalTiming(measurement = {}) {
    const impulse = measurement?.analysis?.impulse_response || null;
    const timingInfo = getMeasurementTimingInfo(measurement);
    const channel = String(measurement?.channel || '').toLowerCase();
    if (channel === 'stereo') {
        return { available: false, reason: 'merged-measurement' };
    }
    const sampleRate = getMeasurementAnalysisSampleRate(measurement);
    if (!impulse || !sampleRate) {
        return { available: false, reason: 'missing-direct-arrival-timing' };
    }
    const arrivalMs = Number.isFinite(Number(timingInfo.delayMs)) ? Number(timingInfo.delayMs) : Number(impulse?.arrival_ms);
    const arrivalSamples = Number.isFinite(Number(timingInfo.arrivalSamples)) ? Number(timingInfo.arrivalSamples) : Number(impulse?.arrival_samples);
    const directSample = Number(impulse?.direct_arrival_index);
    const referencePeakSample = Number(impulse?.reference_peak_index);
    const referenceAnchorSample = Number(measurement?.analysis?.alignment_samples);
    if (
        !Number.isFinite(arrivalMs)
        || !Number.isFinite(arrivalSamples)
        || !Number.isFinite(directSample)
        || !Number.isFinite(referencePeakSample)
    ) {
        return { available: false, reason: 'missing-direct-arrival-timing' };
    }
    const peakSample = Number(impulse?.peak_index);
    const mapDirectCandidate = (candidate) => ({
            sample: Number.isFinite(Number(candidate?.sample)) ? Number(candidate.sample) : null,
            offsetFromPeakSamples: Number.isFinite(Number(candidate?.offset_from_peak_samples)) ? Number(candidate.offset_from_peak_samples) : null,
            offsetFromPeakMs: Number.isFinite(Number(candidate?.offset_from_peak_ms)) ? Number(candidate.offset_from_peak_ms) : null,
            score: Number.isFinite(Number(candidate?.score)) ? Number(candidate.score) : null,
            peakScore: Number.isFinite(Number(candidate?.peak_score)) ? Number(candidate.peak_score) : null,
            relativeDb: Number.isFinite(Number(candidate?.relative_db)) ? Number(candidate.relative_db) : null,
            localEnergyRelative: Number.isFinite(Number(candidate?.local_energy_relative)) ? Number(candidate.local_energy_relative) : null,
            prominenceRelative: Number.isFinite(Number(candidate?.prominence_relative)) ? Number(candidate.prominence_relative) : null,
            prominenceRatio: Number.isFinite(Number(candidate?.prominence_ratio)) ? Number(candidate.prominence_ratio) : null,
            supportScore: Number.isFinite(Number(candidate?.support_score)) ? Number(candidate.support_score) : null,
            distanceFromFirstThresholdSamples: Number.isFinite(Number(candidate?.distance_from_first_threshold_samples)) ? Number(candidate.distance_from_first_threshold_samples) : null,
            weakThresholdEdge: !!candidate?.weak_threshold_edge,
            strongerImpulseRegion: !!candidate?.stronger_impulse_region,
        });
    const scoreSortedSource = Array.isArray(impulse?.direct_candidates_by_score)
        ? impulse.direct_candidates_by_score
        : impulse?.direct_candidates;
    const topCandidates = Array.isArray(scoreSortedSource)
        ? scoreSortedSource.slice(0, 8).map(mapDirectCandidate)
        : [];
    const chronologicalCandidates = Array.isArray(impulse?.direct_candidates_chronological)
        ? impulse.direct_candidates_chronological.slice(0, 12).map(mapDirectCandidate)
        : [];
    return {
        available: true,
        arrivalMs,
        arrivalSamples,
        peakSample: Number.isFinite(peakSample) ? peakSample : null,
        directSample,
        referencePeakSample,
        referenceAnchorSample: Number.isFinite(referenceAnchorSample) ? referenceAnchorSample : null,
        selectedScore: Number.isFinite(Number(impulse?.direct_selected_score)) ? Number(impulse.direct_selected_score) : null,
        selectedSupportScore: Number.isFinite(Number(impulse?.direct_selected_support_score)) ? Number(impulse.direct_selected_support_score) : null,
        confidence: Number.isFinite(Number(impulse?.direct_confidence)) ? Number(impulse.direct_confidence) : null,
        selectionRule: impulse?.direct_selection_rule || '',
        firstThresholdSample: Number.isFinite(Number(impulse?.direct_first_threshold_index)) ? Number(impulse.direct_first_threshold_index) : null,
        firstThresholdOffsetFromPeakSamples: Number.isFinite(Number(impulse?.direct_first_threshold_offset_from_peak_samples)) ? Number(impulse.direct_first_threshold_offset_from_peak_samples) : null,
        candidateCount: Number.isFinite(Number(impulse?.direct_candidate_count)) ? Number(impulse.direct_candidate_count) : null,
        topCandidates,
        chronologicalCandidates,
        sampleRate,
        channel,
        measurementId: measurement?.id || '',
        measurementName: measurement?.name || '',
        measurementKind: measurement?.measurement_kind || '',
        repeatPairKey: measurement?.measurement_kind === 'lr-repeat-summary'
            ? `${String(measurement?.name || '').replace(/\s*·\s*[LR]\s*$/i, '')}|${String(measurement?.created_at || '')}`
            : '',
        source: timingInfo.source || impulse?.timing_source || 'direct_arrival_minus_reference_peak',
        timingStatus: timingInfo.status || '',
        timingLabel: timingInfo.label || '',
    };
}

function getMeasurementGraphBounds(displayWidth, displayHeight) {
    return {
        left: 62,
        top: 22,
        width: Math.max(120, displayWidth - 84),
        height: Math.max(120, displayHeight - 58),
    };
}

function getMeasurementGraphDisplaySize(canvas) {
    if (!canvas) return { width: 0, height: 0 };
    const rect = canvas.getBoundingClientRect();
    return {
        width: Math.max(1, Math.round(rect.width || canvas.clientWidth || 0)),
        height: Math.max(1, Math.round(rect.height || canvas.clientHeight || 0)),
    };
}

function getMeasurementIrPreviewPoints(measurement = {}) {
    const preview = measurement?.analysis?.impulse_response?.preview || {};
    if (Array.isArray(preview.points)) {
        return preview.points
            .filter(point => Array.isArray(point) && point.length === 2 && Number.isFinite(Number(point[0])) && Number.isFinite(Number(point[1])))
            .map(point => [Number(point[0]), Number(point[1])]);
    }
    const times = Array.isArray(preview.times_ms) ? preview.times_ms : [];
    const amplitudes = Array.isArray(preview.amplitudes) ? preview.amplitudes : [];
    return times.map((time, index) => [Number(time), Number(amplitudes[index])])
        .filter(([time, amplitude]) => Number.isFinite(time) && Number.isFinite(amplitude));
}

function getMeasurementIrPeakAbs(points = [], minMs = -0.5, maxMs = 0.5) {
    return points.reduce((peak, [timeMs, amplitude]) => {
        if (timeMs < minMs || timeMs > maxMs) return peak;
        return Math.max(peak, Math.abs(Number(amplitude) || 0));
    }, 0);
}

function getMeasurementIrWindowRms(points = [], minMs = 5, maxMs = 30) {
    const values = points
        .filter(([timeMs]) => timeMs >= minMs && timeMs <= maxMs)
        .map(([, amplitude]) => Number(amplitude) || 0);
    if (!values.length) return null;
    const meanSquare = values.reduce((sum, value) => sum + (value * value), 0) / values.length;
    return Math.sqrt(meanSquare);
}

function getMeasurementIrStrongestAbs(points = [], minMs = 0.5, maxMs = 10) {
    return points.reduce((strongest, [timeMs, amplitude]) => {
        if (timeMs < minMs || timeMs > maxMs) return strongest;
        const absAmplitude = Math.abs(Number(amplitude) || 0);
        if (!strongest || absAmplitude > strongest.absAmplitude) {
            return { timeMs, absAmplitude };
        }
        return strongest;
    }, null);
}

function formatMeasurementIrDb(valueDb) {
    if (!Number.isFinite(valueDb)) return 'n/a';
    const rounded = Math.round(valueDb);
    return `${rounded > 0 ? '+' : ''}${rounded} dB`;
}

function formatMeasurementIrMs(valueMs) {
    if (!Number.isFinite(valueMs)) return 'n/a';
    return `${Number(valueMs).toFixed(1)} ms`;
}

function formatMeasurementIrAmplitude(value) {
    if (!Number.isFinite(value)) return 'n/a';
    const rounded = Number(value).toFixed(2);
    return `${value > 0 ? '+' : ''}${rounded}`;
}

function getMeasurementIrDiagnostics(entry = {}) {
    const trace = (entry.traces || [])[0] || {};
    const points = Array.isArray(trace.points) ? trace.points : [];
    if (!points.length) return null;

    const directPeakAbs = getMeasurementIrPeakAbs(points, -0.5, 0.5)
        || getMeasurementIrPeakAbs(points, -2, 2)
        || points.reduce((peak, [, amplitude]) => Math.max(peak, Math.abs(Number(amplitude) || 0)), 0);
    if (!(directPeakAbs > 0)) return null;

    const tailRms = getMeasurementIrWindowRms(points, 5, 30);
    const peakToTailDb = tailRms && tailRms > 0 ? 20 * Math.log10(directPeakAbs / tailRms) : null;
    const early = getMeasurementIrStrongestAbs(points, 0.5, 10);
    const earlyDb = early && early.absAmplitude > 0 ? 20 * Math.log10(early.absAmplitude / directPeakAbs) : null;
    const label = trace.label || entry.name || 'IR';
    return {
        label,
        color: entry.graphColor || trace.color || '',
        peakToTailDb,
        earlyDb,
        earlyTimeMs: early ? early.timeMs : null,
    };
}

function buildMeasurementIrDiagnostics(graphEntries = [], frequencyView = true) {
    return frequencyView
        ? []
        : graphEntries.map(getMeasurementIrDiagnostics).filter(Boolean);
}

function buildMeasurementIrSummary(diagnostics = []) {
    if (!diagnostics.length) return '';
    const traceText = `${diagnostics.length} ${diagnostics.length === 1 ? 'trace' : 'traces'}`;
    const peakToTailText = formatMeasurementIrCompactRange(diagnostics.map(item => item.peakToTailDb), { digits: 0, suffix: ' dB' });
    const earlyTimeText = formatMeasurementIrCompactRange(diagnostics.map(item => item.earlyTimeMs), { digits: 1, suffix: ' ms' });
    return `IR: ${traceText} · P/T ${peakToTailText} · early refl. ${earlyTimeText} · aligned to 0 ms`;
}

function buildMeasurementIrDiagnosticsTooltip(diagnostics = []) {
    if (!diagnostics.length) return '';
    return diagnostics.map((item) => {
        const peakToTailText = item.peakToTailDb === null ? 'P/T n/a' : `P/T ${formatMeasurementIrDb(item.peakToTailDb)}`;
        const earlyText = item.earlyDb === null || item.earlyTimeMs === null
            ? 'refl. n/a'
            : `refl. ${formatMeasurementIrDb(item.earlyDb)} at ${formatMeasurementIrMs(item.earlyTimeMs)}`;
        return `${item.label}: ${peakToTailText} · ${earlyText}`;
    }).join('\n');
}

function formatMeasurementHoverFrequency(frequencyHz) {
    const frequency = Number(frequencyHz);
    if (!Number.isFinite(frequency)) return '';
    if (frequency >= 1000) {
        const valueKhz = frequency / 1000;
        const digits = valueKhz >= 10 ? 1 : 1;
        return `${valueKhz.toFixed(digits).replace(/\.0$/, '')} kHz`;
    }
    return `${Math.round(frequency)} Hz`;
}

function formatMeasurementHoverDb(valueDb) {
    const value = Number(valueDb);
    if (!Number.isFinite(value)) return '';
    return `${value >= 0 ? '+' : ''}${value.toFixed(1)} dB`;
}

function getMeasurementTraceDisplayedDbAtFrequency(points = [], frequencyHz = 1000) {
    const frequency = Number(frequencyHz);
    if (!Number.isFinite(frequency) || frequency <= 0 || !Array.isArray(points) || !points.length) return null;
    const normalizedPoints = points
        .map(point => [Number(point?.[0]), Number(point?.[1])])
        .filter(([pointFrequency, level]) => Number.isFinite(pointFrequency) && pointFrequency > 0 && Number.isFinite(level))
        .sort((a, b) => a[0] - b[0]);
    if (!normalizedPoints.length) return null;
    if (frequency < normalizedPoints[0][0] || frequency > normalizedPoints[normalizedPoints.length - 1][0]) return null;
    for (let index = 1; index < normalizedPoints.length; index += 1) {
        const [prevFrequency, prevLevel] = normalizedPoints[index - 1];
        const [nextFrequency, nextLevel] = normalizedPoints[index];
        if (frequency > nextFrequency) continue;
        if (frequency <= prevFrequency || Math.abs(nextFrequency - prevFrequency) < 1e-9) return prevLevel;
        const prevLog = Math.log10(prevFrequency);
        const nextLog = Math.log10(nextFrequency);
        const ratio = (Math.log10(frequency) - prevLog) / Math.max(1e-9, nextLog - prevLog);
        return prevLevel + ((nextLevel - prevLevel) * Math.max(0, Math.min(1, ratio)));
    }
    return normalizedPoints[normalizedPoints.length - 1][1];
}

function measurementIrTimeToX(timeMs, bounds) {
    const minMs = -2;
    const maxMs = 30;
    const ratio = (Math.max(minMs, Math.min(maxMs, Number(timeMs))) - minMs) / (maxMs - minMs);
    return bounds.left + (ratio * bounds.width);
}

function measurementXToIrTime(x, bounds) {
    const minMs = -2;
    const maxMs = 30;
    const ratio = (Number(x) - bounds.left) / Math.max(1, bounds.width);
    return minMs + (Math.max(0, Math.min(1, ratio)) * (maxMs - minMs));
}

function measurementIrAmplitudeToY(amplitude, bounds) {
    const ratio = (Math.max(-1, Math.min(1, Number(amplitude))) + 1) / 2;
    return bounds.top + ((1 - ratio) * bounds.height);
}

function getNearestMeasurementIrPoint(points = [], targetTimeMs = 0) {
    return points.reduce((nearest, point) => {
        const [timeMs, amplitude] = point;
        const distance = Math.abs(Number(timeMs) - targetTimeMs);
        if (!nearest || distance < nearest.distance) {
            return { timeMs: Number(timeMs), amplitude: Number(amplitude), distance };
        }
        return nearest;
    }, null);
}

function summarizeMeasurementBand(summary = {}, fallbackLabel = 'No points') {
    return summary.point_count ? `${summary.point_count} pts · ${summary.min_hz || 20}–${summary.max_hz || 20000} Hz` : fallbackLabel;
}

function formatMeasurementUpperLimit(maxHz) {
    const numericMaxHz = Number(maxHz);
    if (!Number.isFinite(numericMaxHz) || numericMaxHz <= 0) return '';
    if (numericMaxHz >= 1000) return `${(numericMaxHz / 1000).toFixed(numericMaxHz >= 10000 ? 1 : 2).replace(/\.0$/, '')}k`;
    return `${Math.round(numericMaxHz)} Hz`;
}

function summarizeMeasurementEntry(measurement = {}) {
    const displaySummary = (measurement.review_summary || {}).point_count ? (measurement.review_summary || {}) : (measurement.summary || {});
    return summarizeMeasurementBand(displaySummary, 'No graph data');
}

function formatMeasurementQualityReason(item = {}) {
    const code = String(item?.code || '').trim();
    const message = String(item?.message || '').trim();
    const lookup = {
        'soft-start-alignment': 'soft start',
        'soft-end-alignment': 'soft end',
        'clock-drift-high': 'clock drift',
        'capture-level-low': 'level low',
        'capture-level-high': 'level high',
        'volume-low': 'volume low',
        'volume-high': 'volume high',
    };
    if (lookup[code]) return lookup[code];
    if (/volume\s+low/i.test(message)) return 'volume low';
    if (/volume\s+high/i.test(message)) return 'volume high';
    if (/level\s+low/i.test(message)) return 'level low';
    if (/level\s+high/i.test(message)) return 'level high';
    if (/clock|drift/i.test(message)) return 'clock drift';
    if (/start/i.test(message)) return 'soft start';
    if (/end/i.test(message)) return 'soft end';
    return 'qc warn';
}

function getMeasurementQualitySummary(measurement = {}) {
    const items = Array.isArray(measurement.analysis?.quality_checks?.items) ? measurement.analysis.quality_checks.items : [];
    const warnings = items.filter(item => item?.level === 'warning');
    if (!warnings.length) return 'QC pass';
    return formatMeasurementQualityReason(warnings[0]);
}

function getMeasurementQualityTitle(measurement = {}) {
    const items = Array.isArray(measurement.analysis?.quality_checks?.items) ? measurement.analysis.quality_checks.items : [];
    return items.map(item => item?.message).filter(Boolean).join(' · ');
}

function formatSignedMeasurementMs(valueMs, digits = 2) {
    const numeric = Number(valueMs);
    if (!Number.isFinite(numeric)) return '';
    return `${numeric >= 0 ? '+' : ''}${numeric.toFixed(digits)} ms`;
}

function getMeasurementLrRepeatGlobalDeltaMs(measurement = {}, repeat = {}) {
    const pairedDelta = Number(repeat?.delta_center_ms);
    if (Number.isFinite(pairedDelta)) return pairedDelta;

    const perSideDelta = Number(repeat?.delta_ms);
    if (!Number.isFinite(perSideDelta)) return null;
    if (repeat?.pre_averaged) {
        const channel = String(measurement?.channel || '').toLowerCase();
        return channel === 'right' ? -perSideDelta : perSideDelta;
    }
    return perSideDelta;
}

function formatMeasurementLrRepeatDelta(measurement = {}, repeat = {}) {
    const globalDeltaMs = getMeasurementLrRepeatGlobalDeltaMs(measurement, repeat);
    if (!Number.isFinite(globalDeltaMs)) return '';
    const laterText = Math.abs(globalDeltaMs) < 0.005
        ? 'aligned'
        : (globalDeltaMs > 0 ? 'R later' : 'L later');
    return `L/R delta ${formatSignedMeasurementMs(globalDeltaMs)} · ${laterText}`;
}

function getMeasurementAutoSubSummary(measurement = {}) {
    const meta = measurement?.autosub_meta || null;
    if (!meta || typeof meta !== 'object') return null;
    const parts = [];
    const targetLabel = String(meta?.target?.label || '').trim();
    if (targetLabel) parts.push(`Target: ${targetLabel}`);
    const gains = meta?.final_gains_db || null;
    if (gains && typeof gains === 'object') {
        const gainText = (value, decimals = 1) => {
            const numeric = Number(value);
            if (!Number.isFinite(numeric)) return '';
            const rounded = Math.round(numeric * 10) / 10;
            const sign = rounded > 0 ? '+' : (rounded < 0 ? '\u2212' : '');
            return `${sign}${Math.abs(rounded).toFixed(decimals)} dB`;
        };
        const sub1 = gainText(gains.sub1);
        if (sub1) parts.push(`Sub 1 ${sub1}`);
        const sub2 = gainText(gains.sub2);
        if (sub2) parts.push(`Sub 2 ${sub2}`);
    }
    if (!parts.length) return null;
    const line = parts.join(' · ');
    return {
        status: 'autosub',
        label: 'AutoSub',
        line,
        detail: `${line} · AutoSub result metadata`,
        delayMs: null,
        arrivalSamples: null,
        source: 'autosub_meta',
    };
}

function getMeasurementTimingInfo(measurement = {}) {
    if (String(measurement?.measurement_kind || '').trim() === 'auto_sub') {
        const autoSubSummary = getMeasurementAutoSubSummary(measurement);
        if (autoSubSummary) return autoSubSummary;
    }
    const referencePath = measurement?.analysis?.reference_path || {};
    const impulse = measurement?.analysis?.impulse_response || {};
    const repeat = measurement?.analysis?.lr_repeat || null;
    const rawStatus = String(referencePath?.timing_status || '').trim();
    const hasElectricalReference = !!measurement?.input_channels?.electrical_reference || referencePath?.capture_mode === 'electrical-input';
    let status = rawStatus || (hasElectricalReference ? 'electrical-reference-candidate' : 'acoustic-only');
    if (referencePath?.electrical_reference_fallback) status = 'electrical-reference-fallback';
    if (referencePath?.electrical_reference_used || referencePath?.usable === true && referencePath?.capture_mode === 'electrical-input') status = 'electrical-reference';

    const correctedMs = Number(referencePath?.acoustic_arrival_corrected_ms);
    const impulseArrivalMs = Number(impulse?.arrival_ms);
    const delayMs = Number.isFinite(correctedMs) ? correctedMs : (Number.isFinite(impulseArrivalMs) ? impulseArrivalMs : null);
    const correctedSamples = Number(referencePath?.acoustic_arrival_corrected_samples);
    const impulseArrivalSamples = Number(impulse?.arrival_samples);
    const arrivalSamples = Number.isFinite(correctedSamples) ? correctedSamples : (Number.isFinite(impulseArrivalSamples) ? impulseArrivalSamples : null);
    const referenceDelayMs = Number(referencePath?.electrical_reference_delay_ms);
    const acousticDelayMs = Number(referencePath?.acoustic_arrival_delay_ms);
    const confidence = Number(referencePath?.confidence ?? impulse?.direct_confidence);
    const stable = status === 'electrical-reference'
        ? String(referencePath?.stability || '').toLowerCase() === 'stable' || confidence >= 0.75
        : confidence >= 0.75;
    const delayText = delayMs === null ? 'delay unavailable' : `delay ${delayMs.toFixed(2)} ms`;
    const promotionApplied = !!impulse?.promotion_applied;

    if (repeat) {
        const preAveraged = !!repeat.pre_averaged;
        const acceptedRuns = preAveraged ? Number(repeat.repeat_count) || 0 : Number(repeat.accepted_runs) || 0;
        const repeatCount = Number(repeat.repeat_count) || 0;
        const spreadMs = Number(repeat.delta_spread_ms ?? repeat.timing_spread_ms);
        const pairedMethod = repeat.timing_method === 'paired-delta-cluster';
        const timingStable = repeat.timing_stable ?? repeat.paired_timing_stable ?? String(referencePath?.stability || '').toLowerCase() === 'stable';
        const spreadText = Number.isFinite(spreadMs) ? ` · spread ${spreadMs.toFixed(2)} ms` : '';
        const deltaText = (pairedMethod || preAveraged) ? formatMeasurementLrRepeatDelta(measurement, repeat) : '';
        const deltaLineText = deltaText ? ` · ${deltaText}` : '';
        const deltaDetailText = deltaText ? ` · ${MEASUREMENT_LR_REPEAT_DELTA_TOOLTIP}` : '';
        if (!timingStable) {
            return {
                status: 'lr-repeat-unstable',
                label: 'L/R repeat timing unstable',
                line: `L/R repeat timing unstable · accepted ${acceptedRuns}/${repeatCount}`,
                detail: `L/R repeat timing unstable · accepted ${acceptedRuns}/${repeatCount}${spreadText}${deltaLineText}${deltaDetailText}`,
                delayMs: null,
                arrivalSamples: null,
                source: 'lr_repeat_unstable',
            };
        }
        const methodLabel = preAveraged ? 'ER pre-averaged' : (pairedMethod ? 'paired-delta' : (repeat.electrical_reference_used ? 'electrical reference' : 'acoustic-only'));
        const label = preAveraged ? 'L/R repeat timing (ER pre-averaged)' : (pairedMethod ? 'L/R repeat timing (paired)' : 'L/R repeat timing');
        return {
            status: 'lr-repeat',
            label,
            line: `L/R repeat timing · ${delayText} · accepted ${acceptedRuns}/${repeatCount}${spreadText}${deltaLineText}`,
            detail: `L/R repeat timing · ${delayText} · accepted ${acceptedRuns}/${repeatCount}${spreadText}${deltaLineText}${deltaDetailText} · ${methodLabel}`,
            delayMs,
            arrivalSamples,
            source: preAveraged ? 'lr_repeat_er_pre_averaged' : (pairedMethod ? 'lr_repeat_paired_delta' : (repeat.electrical_reference_used ? 'lr_repeat_electrical_reference' : 'lr_repeat_acoustic')),
        };
    }

    if (status === 'electrical-reference') {
        const promoText = promotionApplied ? ' · peak promoted' : '';
        return {
            status,
            label: 'Electrical reference active',
            line: `Electrical reference active · ${delayText} · ${stable ? 'timing stable' : 'timing active'}${promoText}`,
            detail: `Electrical reference active · corrected ${delayText}${promoText}${Number.isFinite(referenceDelayMs) ? ` · reference ${referenceDelayMs.toFixed(2)} ms` : ''}${Number.isFinite(acousticDelayMs) ? ` · acoustic ${acousticDelayMs.toFixed(2)} ms` : ''}${!stable && confidence != null ? ` · confidence ${confidence.toFixed(2)}` : ''}`,
            delayMs,
            arrivalSamples,
            source: 'electrical_reference_corrected',
        };
    }
    if (status === 'electrical-reference-fallback') {
        return {
            status,
            label: 'Reference fallback',
            line: `Electrical reference fallback · ${delayText} · acoustic-only timing`,
            detail: `Electrical reference fallback · ${delayText}${referencePath?.warning ? ` · ${referencePath.warning}` : ''}`,
            delayMs,
            arrivalSamples,
            source: 'acoustic_fallback',
        };
    }
    return {
        status: 'acoustic-only',
        label: 'Acoustic-only timing',
        line: `Acoustic-only timing · ${delayText} · ${stable ? 'timing stable' : 'lower confidence'}`,
        detail: `Acoustic-only timing · ${delayText}${Number.isFinite(confidence) ? ` · confidence ${confidence.toFixed(2)}` : ''}`,
        delayMs,
        arrivalSamples,
        source: 'acoustic_only',
    };
}

function getMeasurementJobStatus(job = {}) {
    return String(job?.status || '').trim().toLowerCase();
}

function normalizeMeasurementKind(kind) {
    if (kind === 'lr-repeat' || kind === 'lr_repeat') return 'lr_repeat';
    if (kind === 'auto_sub' || kind === 'auto-sub' || kind === 'autosub') return 'auto_sub';
    if (kind === 'single' || kind === 'hybrid') return kind;
    return '';
}

function getMeasurementJobResultMeasurement(job = {}) {
    if (job?.result?.measurement && typeof job.result.measurement === 'object') return job.result.measurement;
    if (job?.measurement && typeof job.measurement === 'object') return job.measurement;
    if (job?.result && typeof job.result === 'object' && (Array.isArray(job.result.traces) || job.result.summary || job.result.analysis)) return job.result;
    return null;
}

function formatMeasurementInputLevelText(inputLevel = {}) {
    if (!inputLevel || typeof inputLevel !== 'object') return '';
    if (inputLevel.clipped) return 'CLIP';
    const peakDbfs = Number(inputLevel.peak_dbfs);
    if (!Number.isFinite(peakDbfs)) return '';
    if (peakDbfs <= -90) return 'Peak < -90 dBFS';
    return `Peak ${Math.round(peakDbfs)} dBFS`;
}

function hybridSpeakerName(channel) {
    if (channel === 'left') return 'left speaker';
    if (channel === 'right') return 'right speaker';
    return 'left and right speakers';
}

    return {
        MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS,
        MEASUREMENT_JOB_CANCELLED_STATES,
        MEASUREMENT_JOB_FAILED_STATES,
        MEASUREMENT_JOB_SUCCESS_STATES,
        MEASUREMENT_LR_REPEAT_DELTA_TOOLTIP,
        measurementComparePalette,
        measurementCurrentColor,
        measurementPeqPalette,
        measurementPeqTypes,
        measurementPeqTypeLabels,
        measurementConvolverPhaseModes,
        measurementConvolverAlignedPhaseModes,
        measurementConvolverCurves,
        measurementConvolverTapOptions,
        measurementConvolverTypeOptions,
        normalizeMeasurementTrace,
        normalizeMeasurementEntry,
        normalizeMeasurementVisibility,
        formatMeasurementDate,
        normalizeMeasurementReviewVisibility,
        trackFileUrl,
        measurementFileUrl,
        presetFileUrl,
        getDefaultMeasurementPeqFilter,
        getDefaultMeasurementPeqState,
        getDefaultMeasurementConvolverState,
        getMeasurementConvolverDraftPhaseMode,
        getMeasurementPeqNameSuffix,
        getMeasurementConvolverMultiSourceWarning,
        formatMeasurementConvolverGain,
        getMeasurementConvolverNameSuffix,
        getMeasurementConvolverPreviewMode,
        getMeasurementConvolverPreviewGain,
        getMeasurementConvolverTypeOption,
        getMeasurementConvolverTypeKeys,
        getMeasurementConvolverPhaseModeForType,
        getMeasurementConvolverPhaseLabel,
        getMeasurementConvolverPhaseTag,
        getMeasurementConvolverFirLengthForType,
        getMeasurementConvolverTypeLabel,
        getMeasurementConvolverTimingMs,
        getMeasurementConvolverTimingDelta,
        getMeasurementConvolverTimingSafetyMessage,
        alignStereoImpulsesForMinimumAligned,
        getMeasurementAnalysisSampleRate,
        getMeasurementDirectArrivalTiming,
        getMeasurementGraphBounds,
        getMeasurementGraphDisplaySize,
        getMeasurementIrPreviewPoints,
        getMeasurementIrPeakAbs,
        getMeasurementIrWindowRms,
        getMeasurementIrStrongestAbs,
        formatMeasurementIrDb,
        formatMeasurementIrMs,
        formatMeasurementIrAmplitude,
        getMeasurementIrDiagnostics,
        buildMeasurementIrDiagnostics,
        buildMeasurementIrSummary,
        buildMeasurementIrDiagnosticsTooltip,
        formatMeasurementHoverFrequency,
        formatMeasurementHoverDb,
        getMeasurementTraceDisplayedDbAtFrequency,
        measurementIrTimeToX,
        measurementXToIrTime,
        measurementIrAmplitudeToY,
        getNearestMeasurementIrPoint,
        summarizeMeasurementBand,
        formatMeasurementUpperLimit,
        summarizeMeasurementEntry,
        formatMeasurementQualityReason,
        getMeasurementQualitySummary,
        getMeasurementQualityTitle,
        formatSignedMeasurementMs,
        getMeasurementLrRepeatGlobalDeltaMs,
        formatMeasurementLrRepeatDelta,
        getMeasurementTimingInfo,
        getMeasurementAutoSubSummary,
        getMeasurementJobStatus,
        normalizeMeasurementKind,
        getMeasurementJobResultMeasurement,
        formatMeasurementInputLevelText,
        hybridSpeakerName,
    };
});
