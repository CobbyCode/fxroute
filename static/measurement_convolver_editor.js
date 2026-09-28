// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement convolver state, analysis and preset staging. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementConvolverEditor = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const ui = root.FXRouteMeasurementUI || {};
    const dsp = root.FXRouteMeasurementDsp || {};
    let deps = {
        getState: () => ({ measurement: {}, dsp: {} }),
        getElements: () => ({}),
        fetch: (...args) => root.fetch(...args),
        getFormDataType: () => root.FormData,
        showToast: () => {},
        renderMeasurementPanel: () => {},
        scheduleMeasurementGraphRender: () => {},
        saveMeasurementSetupSettings: () => {},
        collectEffectsExtras: () => ({}),
        appendBankBindingFields: () => {},
        measurementCommitSourceId: () => '',
        requireConcreteFilterBank: () => false,
        measurementBankSumsBothInputs: () => false,
        fetchEffects: async () => {},
        fetchOutputSystemCatalog: async () => {},
        formatTransitionErrorDetail: (_detail, fallback) => fallback,
        getVisibleMeasurementEntries: () => [],
        getCurrentMeasurementEntries: () => [],
        getMeasurementDisplayTraces: () => [],
        smoothMeasurementTracePoints: (points) => points,
        waitForNextAnimationFrame: () => Promise.resolve(),
        isConvolverCreateInFlight: () => false,
        setConvolverCreateInFlight: () => {},
        getTimingSafetyLimitMs: () => ui.MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS || 8,
        setTimeout: (...args) => root.setTimeout(...args),
        clearTimeout: (...args) => root.clearTimeout(...args),
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function clampMeasurementConvolverFrequency(value, fallback = 20) {
        return dsp.clampMeasurementConvolverFrequency(value, fallback);
    }

    function getDefaultMeasurementConvolverState() {
        return ui.getDefaultMeasurementConvolverState();
    }

    function ensureMeasurementConvolverState() {
        if (!deps.getState().measurement) deps.getState().measurement = {};
        const defaults = getDefaultMeasurementConvolverState();
        if (!deps.getState().measurement.convolverAssistant || typeof deps.getState().measurement.convolverAssistant !== 'object') {
            deps.getState().measurement.convolverAssistant = { ...defaults };
        }
        const conv = deps.getState().measurement.convolverAssistant;
        Object.entries(defaults).forEach(([key, value]) => {
            if (conv[key] === undefined || conv[key] === null || conv[key] === '') conv[key] = value;
        });
        conv.targetCurve = getMeasurementConvolverCurveOptions().some((curve) => curve.key === conv.targetCurve) ? conv.targetCurve : defaults.targetCurve;
        conv.rangeStartHz = Math.round(clampMeasurementConvolverFrequency(conv.rangeStartHz, defaults.rangeStartHz));
        conv.rangeEndHz = Math.round(clampMeasurementConvolverFrequency(conv.rangeEndHz, defaults.rangeEndHz));
        if (conv.rangeEndHz <= conv.rangeStartHz) conv.rangeEndHz = Math.min(20000, conv.rangeStartHz + 1);
        conv.maxBoostDb = [0, 3, 6, 9].includes(Number(conv.maxBoostDb)) ? Number(conv.maxBoostDb) : defaults.maxBoostDb;
        conv.maxCutDb = [-3, -6, -9, -12, -18, -24].includes(Number(conv.maxCutDb)) ? Number(conv.maxCutDb) : defaults.maxCutDb;
        conv.dipGuard = ['off', 'gentle', 'adaptive'].includes(String(conv.dipGuard)) ? String(conv.dipGuard) : defaults.dipGuard;
        conv.safetyMarginDb = Math.max(0, Number(conv.safetyMarginDb) || defaults.safetyMarginDb);
        const qualityAliases = { auto: 'linear_4096', normal: 'linear_4096', high: 'linear_8192' };
        const incomingQuality = qualityAliases[String(conv.quality)] || String(conv.quality || defaults.quality);
        const hasValidPhaseMode = ui.measurementConvolverPhaseModes.includes(String(conv.phaseMode));
        const hasValidIrLength = ui.measurementConvolverTapOptions.includes(Number(conv.irLength));
        if ((!hasValidPhaseMode || !hasValidIrLength) && getMeasurementConvolverTypeKeys().includes(incomingQuality)) {
            conv.phaseMode = getMeasurementConvolverPhaseModeForType(incomingQuality);
            conv.irLength = String(getMeasurementConvolverFirLengthForType(incomingQuality));
        }
        conv.phaseMode = ui.measurementConvolverPhaseModes.includes(String(conv.phaseMode)) ? String(conv.phaseMode) : defaults.phaseMode;
        conv.irLength = ui.measurementConvolverTapOptions.includes(Number(conv.irLength)) ? String(conv.irLength) : defaults.irLength;
        conv.quality = `${conv.phaseMode}_${conv.irLength}`;
        conv.creatingPreset = !!conv.creatingPreset;
        if (!conv.draft || typeof conv.draft !== 'object') conv.draft = { left: null, right: null, presetName: '', nameTouched: false, notice: '' };
        if (!conv.draft.left || typeof conv.draft.left !== 'object') conv.draft.left = null;
        if (!conv.draft.right || typeof conv.draft.right !== 'object') conv.draft.right = null;
        if (typeof conv.draft.presetName !== 'string') conv.draft.presetName = '';
        conv.draft.nameTouched = !!conv.draft.nameTouched;
        if (typeof conv.draft.notice !== 'string') conv.draft.notice = '';
        return conv;
    }

    function getMeasurementConvolverCurveOptions() {
        const customCurves = (deps.getState().measurement?.houseCurveOptions || [])
            .filter((curve) => Array.isArray(curve.points) && curve.points.length >= 2)
            .map((curve) => ({ key: `house:${curve.id}`, label: curve.filename || 'House curve', shortLabel: curve.filename || 'House', points: curve.points }));
        return [
            ...Object.entries(ui.measurementConvolverCurves).map(([key, curve]) => ({ key, ...curve })),
            ...customCurves,
        ];
    }

    function getMeasurementConvolverCurve(curveKey) {
        return getMeasurementConvolverCurveOptions().find((curve) => curve.key === curveKey) || ui.measurementConvolverCurves.neutral;
    }

    function getMeasurementConvolverCurveDb(curveKey, frequencyHz) {
        const curve = getMeasurementConvolverCurve(curveKey);
        const points = curve.points || ui.measurementConvolverCurves.neutral.points;
        return dsp.getMeasurementConvolverCurveDbFromPoints(points, frequencyHz);
    }

    function getMeasurementConvolverDraftPhaseMode(draft = null) {
        return ui.getMeasurementConvolverDraftPhaseMode(draft);
    }

    function getMeasurementConvolverDrafts(conv = ensureMeasurementConvolverState()) {
        return [conv.draft?.left || null, conv.draft?.right || null].filter(Boolean);
    }

    function getMeasurementConvolverDraftPhaseMismatch(conv = ensureMeasurementConvolverState()) {
        const currentPhaseMode = String(conv.phaseMode || '');
        const mismatched = getMeasurementConvolverDrafts(conv).find((draft) => {
            const draftPhaseMode = getMeasurementConvolverDraftPhaseMode(draft);
            return draftPhaseMode && draftPhaseMode !== currentPhaseMode;
        });
        return mismatched ? getMeasurementConvolverDraftPhaseMode(mismatched) : '';
    }

    function clearMeasurementConvolverDraftForPhaseChange(previousPhaseMode = '') {
        const conv = ensureMeasurementConvolverState();
        if (previousPhaseMode === conv.phaseMode) return false;
        if (!conv.draft?.left && !conv.draft?.right) {
            conv.draft.notice = '';
            return false;
        }
        conv.draft.left = null;
        conv.draft.right = null;
        conv.draft.presetName = '';
        conv.draft.nameTouched = false;
        conv.draft.notice = 'Phase type changed. Take L/R again.';
        showMeasurementConvolverFeedback(conv.draft.notice);
        return true;
    }

    function clearMeasurementConvolverDraftForSettingsChange(notice = '') {
        const conv = ensureMeasurementConvolverState();
        if (!conv.draft?.left && !conv.draft?.right) {
            if (notice) conv.draft.notice = '';
            return false;
        }
        conv.draft.left = null;
        conv.draft.right = null;
        conv.draft.presetName = '';
        conv.draft.nameTouched = false;
        conv.draft.notice = notice;
        if (notice) showMeasurementConvolverFeedback(notice);
        return true;
    }

    function updateMeasurementConvolverField(field, value) {
        const conv = ensureMeasurementConvolverState();
        const previousPhaseMode = conv.phaseMode;
        const previousIrLength = String(conv.irLength ?? '');
        const previousSampleRate = String(deps.getState().measurement?.measurementSampleRate ?? '');
        if (field === 'targetCurve') {
            const nextTarget = getMeasurementConvolverCurveOptions().some((curve) => curve.key === value) ? value : conv.targetCurve;
            if (nextTarget !== conv.targetCurve) {
                conv.targetCurve = nextTarget;
                clearMeasurementConvolverDraftForSettingsChange('Target curve changed. Take L/R again.');
            }
        }
        if (field === 'rangeStartHz') {
            const nextStart = Math.min(Math.round(clampMeasurementConvolverFrequency(value, conv.rangeStartHz)), conv.rangeEndHz - 1);
            if (nextStart !== conv.rangeStartHz) {
                conv.rangeStartHz = nextStart;
                clearMeasurementConvolverDraftForSettingsChange('Correction range changed. Take L/R again.');
            }
        }
        if (field === 'rangeEndHz') {
            const nextEnd = Math.max(Math.round(clampMeasurementConvolverFrequency(value, conv.rangeEndHz)), conv.rangeStartHz + 1);
            if (nextEnd !== conv.rangeEndHz) {
                conv.rangeEndHz = nextEnd;
                clearMeasurementConvolverDraftForSettingsChange('Correction range changed. Take L/R again.');
            }
        }
        if (field === 'maxBoostDb') {
            const nextBoost = [0, 3, 6, 9].includes(Number(value)) ? Number(value) : conv.maxBoostDb;
            if (nextBoost !== conv.maxBoostDb) {
                conv.maxBoostDb = nextBoost;
                clearMeasurementConvolverDraftForSettingsChange('Correction limits changed. Take L/R again.');
            }
        }
        if (field === 'maxCutDb') {
            const nextCut = [-3, -6, -9, -12, -18, -24].includes(Number(value)) ? Number(value) : conv.maxCutDb;
            if (nextCut !== conv.maxCutDb) {
                conv.maxCutDb = nextCut;
                clearMeasurementConvolverDraftForSettingsChange('Correction limits changed. Take L/R again.');
            }
        }
        if (field === 'dipGuard') {
            const nextDipGuard = ['off', 'gentle', 'adaptive'].includes(String(value)) ? String(value) : conv.dipGuard;
            if (nextDipGuard !== conv.dipGuard) {
                conv.dipGuard = nextDipGuard;
                clearMeasurementConvolverDraftForSettingsChange('Dip guard changed. Take L/R again.');
            }
        }
        if (field === 'sampleRate') {
            deps.getState().measurement.measurementSampleRate = String(value || '48000');
            if (String(deps.getState().measurement.measurementSampleRate) !== previousSampleRate) {
                clearMeasurementConvolverDraftForSettingsChange('Sample rate changed. Take L/R again.');
            }
            void deps.saveMeasurementSetupSettings({ measurementSampleRate: Number(deps.getState().measurement.measurementSampleRate) });
        }
        if (field === 'phaseMode') conv.phaseMode = ui.measurementConvolverPhaseModes.includes(String(value)) ? String(value) : conv.phaseMode;
        if (field === 'irLength') conv.irLength = ui.measurementConvolverTapOptions.includes(Number(value)) ? String(value) : conv.irLength;
        if (field === 'quality') {
            const quality = String(value || 'linear_4096');
            if (getMeasurementConvolverTypeKeys().includes(quality)) {
                conv.phaseMode = getMeasurementConvolverPhaseModeForType(quality);
                conv.irLength = String(getMeasurementConvolverFirLengthForType(quality));
            }
        }
        ensureMeasurementConvolverState();
        // IR length is a generation input like the limits above: creating from a
        // stale draft would silently ignore the newly selected taps. This also
        // covers the legacy 'quality' path, which remaps taps without passing
        // through the 'irLength' branch.
        if (String(conv.irLength ?? '') !== previousIrLength) {
            clearMeasurementConvolverDraftForSettingsChange('Convolver taps changed. Take L/R again.');
        }
        if (field === 'phaseMode' || field === 'quality') {
            clearMeasurementConvolverDraftForPhaseChange(previousPhaseMode);
        }
        deps.renderMeasurementPanel();
        deps.scheduleMeasurementGraphRender();
    }

    function getMeasurementConvolverSelectedSourceEntries() {
        const visibleSavedEntries = deps.getVisibleMeasurementEntries().filter(Boolean);
        return visibleSavedEntries.length ? visibleSavedEntries : deps.getCurrentMeasurementEntries().filter(Boolean);
    }

    function getMeasurementConvolverSourceEntries() {
        return getMeasurementConvolverSelectedSourceEntries();
    }

    function getMeasurementConvolverSourceSelectionState() {
        const entries = getMeasurementConvolverSourceEntries()
            .filter((measurement) => deps.getMeasurementDisplayTraces(measurement || {}).length > 0);
        const leftEntries = [];
        const rightEntries = [];
        const otherEntries = [];
        entries.forEach((measurement) => {
            const channel = String(measurement?.channel || 'left').toLowerCase();
            if (channel === 'left') {
                leftEntries.push(measurement);
            } else if (channel === 'right') {
                rightEntries.push(measurement);
            } else {
                otherEntries.push(measurement);
            }
        });
        const take = { left: false, right: false, both: false };
        let mode = '';
        if (entries.length === 1 && leftEntries.length === 1) {
            take.left = true;
            mode = 'left';
        } else if (entries.length === 1 && rightEntries.length === 1) {
            take.right = true;
            mode = 'right';
        } else if (entries.length === 2 && leftEntries.length === 1 && rightEntries.length === 1 && otherEntries.length === 0) {
            take.both = true;
            mode = 'both';
        }
        const warning = entries.length > 1 && !mode
            ? 'Select one measurement or one L/R selection.'
            : '';
        return {
            entries,
            count: entries.length,
            mode,
            take,
            warning,
            left: leftEntries[0] || null,
            right: rightEntries[0] || null,
        };
    }

    function getMeasurementConvolverMeasurementForSide(side = 'left') {
        const desired = side === 'right' ? 'right' : 'left';
        const selection = getMeasurementConvolverSourceSelectionState();
        if (selection.mode === 'both') return desired === 'right' ? selection.right : selection.left;
        if (selection.mode === desired) return desired === 'right' ? selection.right : selection.left;
        return null;
    }

    function getMeasurementConvolverTracePoints(side = 'left') {
        const measurement = getMeasurementConvolverMeasurementForSide(side);
        const trace = deps.getMeasurementDisplayTraces(measurement || {}).find((item) => (item.points || []).length) || null;
        return trace ? deps.smoothMeasurementTracePoints(trace.points || [], '1/6-oct') : [];
    }

    function getMeasurementConvolverAdaptiveDipGuardStrength(frequencyHz) {
        return dsp.getMeasurementConvolverAdaptiveDipGuardStrength(frequencyHz);
    }

    function applyMeasurementConvolverDipGuard(requestedCorrections, index, mode = 'off') {
        return dsp.applyMeasurementConvolverDipGuard(requestedCorrections, index, mode);
    }

    function analyzeMeasurementConvolverSide(side = 'left') {
        const conv = ensureMeasurementConvolverState();
        const points = getMeasurementConvolverTracePoints(side).filter(([frequency]) => frequency >= conv.rangeStartHz && frequency <= conv.rangeEndHz);
        if (!points.length) return null;
        const curve = getMeasurementConvolverCurve(conv.targetCurve);
        const measurement = getMeasurementConvolverMeasurementForSide(side);
        const analysisSettings = {
            ...conv,
            correctionConfidence: measurement?.analysis?.hybrid_constraints || [],
        };
        return {
            side,
            points: points.length,
            ...dsp.analyzeMeasurementConvolverCorrections(points, curve.points || ui.measurementConvolverCurves.neutral.points, analysisSettings),
        };
    }

    function getMeasurementConvolverSelectedSourceCount() {
        return getMeasurementConvolverSourceSelectionState().count;
    }

    function getMeasurementConvolverMultiSourceWarning() {
        return ui.getMeasurementConvolverMultiSourceWarning();
    }

    function buildMeasurementConvolverWarnings(analyses = []) {
        const conv = ensureMeasurementConvolverState();
        const warnings = [];
        const selectionWarning = getMeasurementConvolverSourceSelectionState().warning;
        if (selectionWarning) warnings.push(selectionWarning);
        if (analyses.some((analysis) => analysis && analysis.lowBassBoost)) warnings.push('Deep bass boost can demand much more amplifier power and speaker excursion.');
        if (ui.measurementConvolverAlignedPhaseModes.includes(conv.phaseMode)) {
            const leftTiming = conv.draft?.left?.timing || getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('left'));
            const rightTiming = conv.draft?.right?.timing || getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('right'));
            const safetyMessage = getMeasurementConvolverTimingSafetyMessage(getMeasurementConvolverTimingDelta(leftTiming, rightTiming));
            if (safetyMessage) warnings.push('Filter not created because timing offset exceeds safety limit.');
        }
        return warnings;
    }

    function formatMeasurementConvolverGain(value) {
        return ui.formatMeasurementConvolverGain(value);
    }

    function getMeasurementConvolverNameSuffix(date = new Date()) {
        return ui.getMeasurementConvolverNameSuffix(date);
    }

    function getMeasurementConvolverItemName(mode = 'both', autoGainDb = 0, options = {}) {
        const conv = ensureMeasurementConvolverState();
        const curve = getMeasurementConvolverCurve(conv.targetCurve);
        const prefix = mode === 'both' ? 'Conv LR' : (mode === 'right' ? 'Conv R' : 'Conv L');
        const phaseMode = ui.measurementConvolverPhaseModes.includes(String(options.phaseMode)) ? String(options.phaseMode) : conv.phaseMode;
        const phaseTag = getMeasurementConvolverPhaseTag(phaseMode);
        const base = `${prefix} ${phaseTag} ${curve.shortLabel || curve.label} ${Math.round(conv.rangeStartHz)}-${Math.round(conv.rangeEndHz)}Hz ${formatMeasurementConvolverGain(autoGainDb)}`;
        return options.unique ? `${base} ${getMeasurementConvolverNameSuffix()}` : base;
    }

    function getMeasurementConvolverPreviewMode(leftAnalysis, rightAnalysis, leftDraft = null, rightDraft = null) {
        return ui.getMeasurementConvolverPreviewMode(leftAnalysis, rightAnalysis, leftDraft, rightDraft);
    }

    function getMeasurementConvolverPreviewGain(mode = 'both', leftAnalysis = null, rightAnalysis = null, leftDraft = null, rightDraft = null) {
        return ui.getMeasurementConvolverPreviewGain(mode, leftAnalysis, rightAnalysis, leftDraft, rightDraft);
    }

    function showMeasurementConvolverFeedback(message) {
        if (!deps.getElements().measurementConvolverFeedback) return;
        deps.getElements().measurementConvolverFeedback.textContent = message;
        deps.getElements().measurementConvolverFeedback.classList.add('is-visible');
        deps.setTimeout(() => deps.getElements().measurementConvolverFeedback?.classList.remove('is-visible'), 2600);
    }

    function getMeasurementConvolverSampleRate() {
        const selected = Number(deps.getState().measurement?.measurementSampleRate);
        return Number.isFinite(selected) && selected > 0 ? selected : 48000;
    }

    function getMeasurementConvolverTypeOption(type = 'linear_4096') {
        return ui.getMeasurementConvolverTypeOption(type);
    }

    function getMeasurementConvolverTypeKeys() {
        return ui.getMeasurementConvolverTypeKeys();
    }

    function getMeasurementConvolverPhaseModeForType(type = 'linear_4096') {
        return ui.getMeasurementConvolverPhaseModeForType(type);
    }

    function getMeasurementConvolverPhaseLabel(phaseMode = 'linear') {
        return ui.getMeasurementConvolverPhaseLabel(phaseMode);
    }

    function getMeasurementConvolverPhaseTag(phaseMode = 'linear') {
        return ui.getMeasurementConvolverPhaseTag(phaseMode);
    }

    function getMeasurementConvolverFirLengthForType(type = 'linear_4096') {
        return ui.getMeasurementConvolverFirLengthForType(type);
    }

    function getMeasurementConvolverFirLength() {
        const conv = ensureMeasurementConvolverState();
        return getMeasurementConvolverFirLengthForType(conv.quality);
    }

    function getMeasurementConvolverTypeLabel(type = 'linear_4096') {
        return ui.getMeasurementConvolverTypeLabel(type);
    }

    function interpolateMeasurementConvolverCorrection(analysis, frequencyHz, autoGainDb) {
        return dsp.interpolateMeasurementConvolverCorrection(analysis, frequencyHz, autoGainDb);
    }

    function buildMeasurementConvolverMagnitudeBins(analysis, sampleRate, length, autoGainDb) {
        return dsp.buildMeasurementConvolverMagnitudeBins(analysis, sampleRate, length, autoGainDb);
    }

    function buildMeasurementConvolverLinearImpulseFromMagnitudes(magnitudes, length) {
        return dsp.buildMeasurementConvolverLinearImpulseFromMagnitudes(magnitudes, length);
    }

    function fftMeasurementConvolverComplex(real, imag, inverse = false) {
        return dsp.fftMeasurementConvolverComplex(real, imag, inverse);
    }

    function buildMeasurementConvolverMinimumSpectrum(magnitudes, length) {
        return dsp.buildMeasurementConvolverMinimumSpectrum(magnitudes, length);
    }

    function buildMeasurementConvolverImpulseFromSpectrum(real, imag) {
        return dsp.buildMeasurementConvolverImpulseFromSpectrum(real, imag);
    }

    function buildMeasurementConvolverImpulse(analysis, sampleRate, length, autoGainDb, phaseMode = 'linear') {
        return dsp.buildMeasurementConvolverImpulse(analysis, sampleRate, length, autoGainDb, phaseMode);
    }

    function getMeasurementConvolverTimingMs(timing = {}) {
        return ui.getMeasurementConvolverTimingMs(timing);
    }

    function getMeasurementConvolverTimingDelta(leftTiming, rightTiming) {
        return ui.getMeasurementConvolverTimingDelta(leftTiming, rightTiming);
    }

    function getMeasurementConvolverTimingPairDebug(timingDelta) {
        const leftTiming = timingDelta?.leftTiming || {};
        const rightTiming = timingDelta?.rightTiming || {};
        const pendingIds = new Set((deps.getState().measurement.pendingRepeatMeasurements || []).map((measurement) => String(measurement?.id || '')));
        const samePendingRepeatResult = !!leftTiming.measurementId
            && !!rightTiming.measurementId
            && pendingIds.has(String(leftTiming.measurementId))
            && pendingIds.has(String(rightTiming.measurementId));
        const leftRepeatSummary = leftTiming.measurementKind === 'lr-repeat-summary';
        const rightRepeatSummary = rightTiming.measurementKind === 'lr-repeat-summary';
        const inferredSameSavedRepeatResult = leftRepeatSummary
            && rightRepeatSummary
            && !!leftTiming.repeatPairKey
            && leftTiming.repeatPairKey === rightTiming.repeatPairKey;
        const sameLrRepeatResult = samePendingRepeatResult || inferredSameSavedRepeatResult;
        return {
            left: {
                measurementId: leftTiming.measurementId || '',
                measurementName: leftTiming.measurementName || '',
                channel: leftTiming.channel || 'left',
                correctedArrivalMs: getMeasurementConvolverTimingMs(leftTiming),
            },
            right: {
                measurementId: rightTiming.measurementId || '',
                measurementName: rightTiming.measurementName || '',
                channel: rightTiming.channel || 'right',
                correctedArrivalMs: getMeasurementConvolverTimingMs(rightTiming),
            },
            calculatedDeltaMs: timingDelta?.deltaMs ?? null,
            displayedAbsDeltaMs: timingDelta?.absMs ?? null,
            sameIntendedLrPair: sameLrRepeatResult ? true : 'unknown',
            sameLrRepeatResult,
            pairEvidence: samePendingRepeatResult
                ? 'current-pending-lr-repeat-result'
                : (inferredSameSavedRepeatResult ? 'saved-lr-repeat-summary-name-and-timestamp' : 'no-explicit-pair-metadata'),
        };
    }

    function formatMeasurementConvolverTimingRelation(timingDelta) {
        if (!timingDelta) return 'Timing unavailable';
        const earlierSide = timingDelta.laterSide === 'R' ? 'L' : 'R';
        const line = `${timingDelta.laterSide} arrives ${timingDelta.absMs.toFixed(2)} ms later than ${earlierSide}`;
        console.info('[measurement-convolver-visible-lr-delta]', {
            line,
            ...getMeasurementConvolverTimingPairDebug(timingDelta),
        });
        return line;
    }

    function getMeasurementConvolverTimingSafetyMessage(timingDelta, limitMs = deps.getTimingSafetyLimitMs()) {
        return ui.getMeasurementConvolverTimingSafetyMessage(timingDelta, limitMs);
    }

    function alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, leftTiming, rightTiming, maxAlignMs = deps.getTimingSafetyLimitMs()) {
        return ui.alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, leftTiming, rightTiming, maxAlignMs);
    }

    function getMeasurementAnalysisSampleRate(measurement = {}) {
        return ui.getMeasurementAnalysisSampleRate(measurement);
    }

    function getMeasurementDirectArrivalTiming(measurement = {}) {
        return ui.getMeasurementDirectArrivalTiming(measurement);
    }

    function writeMeasurementConvolverWav(channels, sampleRate) {
        return dsp.writeMeasurementConvolverWav(channels, sampleRate);
    }

    function appendMeasurementConvolverExtras(formData) {
        const extras = deps.collectEffectsExtras();
        formData.append('load_after_create', 'false');
        formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
        formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
        formData.append('headroom_gain_db', String(extras.headroomGainDb));
        formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
        formData.append('autogain_target_db', String(extras.autogainTargetDb));
        formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
        formData.append('bass_amount', String(extras.bassAmount));
        formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
        formData.append('tone_effect_mode', extras.toneEffectMode);
    }

    async function createMeasurementConvolverPreset(mode, analyses, sharedAutoGainDb, itemName, options = {}) {
        if (!deps.requireConcreteFilterBank()) throw new Error('Select a filter bank for import or measurement.');
        const sampleRate = Number(options.sampleRate) || getMeasurementConvolverSampleRate();
        const length = Number(options.irLength) || getMeasurementConvolverFirLength();
        const phaseMode = ui.measurementConvolverPhaseModes.includes(options.phaseMode) ? options.phaseMode : ensureMeasurementConvolverState().phaseMode;
        const filenameBase = itemName.replace(/[^a-z0-9._-]+/gi, '-').replace(/^-+|-+$/g, '') || 'measurement-convolver';
        const bySide = Object.fromEntries(analyses.map((analysis) => [analysis.side, analysis]));
        const hybridTransition = typeof dsp.getMeasurementConvolverHybridTransition === 'function'
            ? dsp.getMeasurementConvolverHybridTransition()
            : { hybridMinHz: null, hybridLinearHz: null };
        console.debug('[measurement-convolver-fir-generation]', {
            phaseMode,
            hybridMinHz: hybridTransition.hybridMinHz,
            hybridLinearHz: hybridTransition.hybridLinearHz,
            taps: length,
            rangeStart: Number.isFinite(Number(options.rangeStartHz)) ? Number(options.rangeStartHz) : null,
            rangeEnd: Number.isFinite(Number(options.rangeEndHz)) ? Number(options.rangeEndHz) : null,
        });
        if (mode === 'both') {
            const applyPhaseMode = phaseMode === 'minimum_aligned' ? 'minimum' : phaseMode;
            const leftImpulse = buildMeasurementConvolverImpulse(bySide.left, sampleRate, length, sharedAutoGainDb, applyPhaseMode);
            const rightImpulse = buildMeasurementConvolverImpulse(bySide.right, sampleRate, length, sharedAutoGainDb, applyPhaseMode);
            const [finalLeft, finalRight] = ui.measurementConvolverAlignedPhaseModes.includes(phaseMode)
                ? alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, options.leftTiming || null, options.rightTiming || null, Number(options.timingSafetyLimitMs) || deps.getTimingSafetyLimitMs())
                : [leftImpulse, rightImpulse];
            const leftBlob = writeMeasurementConvolverWav([finalLeft], sampleRate);
            const rightBlob = writeMeasurementConvolverWav([finalRight], sampleRate);
            const formData = new (deps.getFormDataType())();
            formData.append('preset_name', itemName);
            appendMeasurementConvolverExtras(formData);
            formData.append('left_file', leftBlob, `${filenameBase}-L.wav`);
            formData.append('right_file', rightBlob, `${filenameBase}-R.wav`);
            deps.appendBankBindingFields(formData);
            formData.append('source_measurement_id', deps.measurementCommitSourceId());
            const resp = await deps.fetch('/api/dsp/presets/import-filter-dual', { method: 'POST', body: formData });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Convolver preset creation failed'));
            void deps.fetchOutputSystemCatalog(true);
            return data;
        }
        const side = mode === 'right' ? 'right' : 'left';
        const applyPhaseMode = phaseMode === 'minimum_aligned' ? 'minimum' : phaseMode;
        const impulse = buildMeasurementConvolverImpulse(bySide[side], sampleRate, length, sharedAutoGainDb, applyPhaseMode);
        const blob = writeMeasurementConvolverWav([impulse], sampleRate);
        const formData = new (deps.getFormDataType())();
        formData.append('preset_name', itemName);
        appendMeasurementConvolverExtras(formData);
        formData.append('file', blob, `${filenameBase}-${side === 'right' ? 'R' : 'L'}.wav`);
        deps.appendBankBindingFields(formData);
        formData.append('source_measurement_id', deps.measurementCommitSourceId());
        const resp = await deps.fetch('/api/dsp/presets/create-with-ir', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Convolver preset creation failed'));
        void deps.fetchOutputSystemCatalog(true);
        return data;
    }

    function takeMeasurementConvolverToDraft(mode = 'both') {
        const selection = getMeasurementConvolverSourceSelectionState();
        if (!selection.take[mode]) {
            const warning = selection.warning || getMeasurementConvolverMultiSourceWarning();
            showMeasurementConvolverFeedback(warning);
            deps.showToast(warning, 'warning');
            return;
        }
        if (mode === 'both' && deps.measurementBankSumsBothInputs()) {
            const warning = 'This area is fed by both inputs: its bank needs a mono IR, so take a single side.';
            showMeasurementConvolverFeedback(warning);
            deps.showToast(warning, 'warning');
            return;
        }
        const sides = mode === 'left' ? ['left'] : (mode === 'right' ? ['right'] : ['left', 'right']);
        const analyses = sides.map((side) => analyzeMeasurementConvolverSide(side));
        if (analyses.some((analysis) => !analysis)) {
            deps.showToast('Run or show a measurement with points in the selected correction range first', 'warning');
            return;
        }
        const conv = ensureMeasurementConvolverState();
        if (ui.measurementConvolverAlignedPhaseModes.includes(conv.phaseMode) && sides.length === 2) {
            const sourceTimings = sides.map((side) => getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide(side)));
            const leftTimingOk = sourceTimings[0]?.available === true;
            const rightTimingOk = sourceTimings[1]?.available === true;
            if (!leftTimingOk || !rightTimingOk) {
                showMeasurementConvolverFeedback('Timing align needs single L/R measurements.');
                deps.showToast('Timing align needs single L/R measurements.', 'warning');
                return;
            }
            const timingDelta = getMeasurementConvolverTimingDelta(sourceTimings[0], sourceTimings[1]);
            const safetyMessage = getMeasurementConvolverTimingSafetyMessage(timingDelta);
            if (safetyMessage) {
                showMeasurementConvolverFeedback('Filter not created because timing offset exceeds safety limit.');
                deps.showToast(safetyMessage, 'warning');
                return;
            }
        }
        sides.forEach((side, index) => {
            const analysis = analyses[index];
            const sourceMeasurement = getMeasurementConvolverMeasurementForSide(side);
            const timing = getMeasurementDirectArrivalTiming(sourceMeasurement);
            conv.draft[side] = {
                side,
                phaseMode: conv.phaseMode,
                createdAt: new Date().toISOString(),
                analysis,
                timing,
                metadata: {
                    targetCurve: conv.targetCurve,
                    rangeStartHz: conv.rangeStartHz,
                    rangeEndHz: conv.rangeEndHz,
                    maxBoostDb: conv.maxBoostDb,
                    maxCutDb: conv.maxCutDb,
                    dipGuard: conv.dipGuard,
                    safetyMarginDb: conv.safetyMarginDb,
                    autoGainDb: analysis.autoGainDb,
                    sampleRate: getMeasurementConvolverSampleRate(),
                    quality: conv.quality,
                    phaseMode: conv.phaseMode,
                    irLength: getMeasurementConvolverFirLength(),
                    sourceMeasurementId: sourceMeasurement?.id || '',
                    sourceMeasurementName: sourceMeasurement?.name || '',
                    sourceMeasurementCreatedAt: sourceMeasurement?.created_at || '',
                    sourceChannel: sourceMeasurement?.channel || side,
                },
            };
        });
        conv.draft.notice = '';
        const sharedAutoGainDb = Math.min(...analyses.map((analysis) => analysis.autoGainDb));
        const effectiveMode = conv.draft.left && conv.draft.right ? 'both' : mode;
        if (!conv.draft.nameTouched) conv.draft.presetName = getMeasurementConvolverItemName(effectiveMode, sharedAutoGainDb, { unique: true, phaseMode: conv.phaseMode });
        deps.renderMeasurementPanel();
        const label = mode === 'both' ? 'Left + Right staged' : (mode === 'right' ? 'Right staged' : 'Left staged');
        showMeasurementConvolverFeedback(label);
        deps.showToast(`Convolver draft updated: ${label}`, 'success');
    }

    function resolveMeasurementConvolverItemName(conv, fieldValue, mode, sharedAutoGainDb) {
        // The visible field is authoritative: whatever stands in the Preset
        // Name input at click time is saved, so the stored preset can never
        // diverge from what the user saw (e.g. a render between the last
        // keystroke and the click, or an in-flight draft reset). An untouched
        // field still holds the staged auto name, preserving the default flow.
        const fieldName = String(fieldValue ?? '').trim();
        if (fieldName) return fieldName;
        const draftName = String(conv?.draft?.presetName || '').trim();
        if (draftName) return draftName;
        return getMeasurementConvolverItemName(mode, sharedAutoGainDb, { unique: true });
    }

    async function createMeasurementConvolverPresetFromDraft() {
        if (!deps.requireConcreteFilterBank()) return;
        if (deps.isConvolverCreateInFlight()) {
            deps.showToast('Convolver preset creation already in progress', 'warning');
            return;
        }
        const conv = ensureMeasurementConvolverState();
        const leftDraft = conv.draft?.left || null;
        const rightDraft = conv.draft?.right || null;
        const mode = leftDraft && rightDraft ? 'both' : (rightDraft ? 'right' : (leftDraft ? 'left' : null));
        if (!mode) {
            deps.showToast('Take L, R or Both into the convolver draft first', 'warning');
            return;
        }
        const drafts = mode === 'both' ? [leftDraft, rightDraft] : [mode === 'right' ? rightDraft : leftDraft];
        const phaseMismatch = getMeasurementConvolverDraftPhaseMismatch(conv);
        if (phaseMismatch) {
            conv.draft.notice = 'Draft phase does not match the selected phase type. Take L/R again.';
            showMeasurementConvolverFeedback(conv.draft.notice);
            deps.showToast(conv.draft.notice, 'warning');
            deps.renderMeasurementPanel();
            return;
        }
        if (mode === 'both') {
            const [leftMeta, rightMeta] = drafts.map((draft) => draft?.metadata || {});
            const sameGeneration = ['sampleRate', 'quality', 'phaseMode', 'irLength'].every((key) => String(leftMeta[key] || '') === String(rightMeta[key] || ''));
            if (!sameGeneration) {
                showMeasurementConvolverFeedback('Retake L/R with matching Convolver type and sample rate');
                deps.showToast('Left and Right convolver drafts use different FIR settings. Retake Both for a matched comparison preset.', 'warning');
                return;
            }
            const draftPhaseMode = leftMeta.phaseMode || rightMeta.phaseMode || conv.phaseMode;
            if (ui.measurementConvolverAlignedPhaseModes.includes(draftPhaseMode)) {
                const leftTimingOk = leftDraft?.timing?.available === true;
                const rightTimingOk = rightDraft?.timing?.available === true;
                if (!leftTimingOk || !rightTimingOk) {
                    showMeasurementConvolverFeedback('Timing align needs single L/R measurements.');
                    deps.showToast('Timing align needs single L/R measurements.', 'warning');
                    return;
                }
                const timingDelta = getMeasurementConvolverTimingDelta(leftDraft?.timing, rightDraft?.timing);
                const safetyMessage = getMeasurementConvolverTimingSafetyMessage(timingDelta);
                if (safetyMessage) {
                    showMeasurementConvolverFeedback('Filter not created because timing offset exceeds safety limit.');
                    deps.showToast(safetyMessage, 'warning');
                    return;
                }
            }
        }
        const analyses = drafts.map((draft) => draft.analysis);
        const sharedAutoGainDb = Math.min(...analyses.map((analysis) => analysis.autoGainDb));
        const itemName = resolveMeasurementConvolverItemName(conv, deps.getElements().measurementConvolverPresetName?.value, mode, sharedAutoGainDb);
        conv.draft.presetName = itemName;
        deps.setConvolverCreateInFlight(true);
        conv.creatingPreset = true;
        showMeasurementConvolverFeedback('Creating convolver preset...');
        deps.renderMeasurementPanel();
        await deps.waitForNextAnimationFrame();
        try {
            const draftMetadata = drafts[0]?.metadata || {};
            const generationOptions = {
                sampleRate: draftMetadata.sampleRate || getMeasurementConvolverSampleRate(),
                quality: draftMetadata.quality || conv.quality,
                phaseMode: draftMetadata.phaseMode || conv.phaseMode,
                irLength: draftMetadata.irLength || getMeasurementConvolverFirLength(),
                rangeStartHz: draftMetadata.rangeStartHz || conv.rangeStartHz,
                rangeEndHz: draftMetadata.rangeEndHz || conv.rangeEndHz,
                leftTiming: leftDraft?.timing || null,
                rightTiming: rightDraft?.timing || null,
                timingSafetyLimitMs: deps.getTimingSafetyLimitMs(),
            };
            const created = await createMeasurementConvolverPreset(mode, analyses, sharedAutoGainDb, itemName, generationOptions);
            const item = {
                type: 'convolver',
                mode,
                name: itemName,
                createdAt: new Date().toISOString(),
                preset: created.preset || null,
                ir: created.ir || null,
                metadata: {
                    targetCurve: draftMetadata.targetCurve || conv.targetCurve,
                    rangeStartHz: draftMetadata.rangeStartHz || conv.rangeStartHz,
                    rangeEndHz: draftMetadata.rangeEndHz || conv.rangeEndHz,
                    maxBoostDb: draftMetadata.maxBoostDb ?? conv.maxBoostDb,
                    maxCutDb: draftMetadata.maxCutDb ?? conv.maxCutDb,
                    dipGuard: draftMetadata.dipGuard ?? conv.dipGuard,
                    safetyMarginDb: draftMetadata.safetyMarginDb ?? conv.safetyMarginDb,
                    autoGainDb: sharedAutoGainDb,
                    sampleRate: generationOptions.sampleRate,
                    quality: generationOptions.quality,
                    phaseMode: generationOptions.phaseMode,
                    irLength: generationOptions.irLength,
                    generatedIr: true,
                },
                analyses: analyses.map((analysis) => ({ side: analysis.side, points: analysis.points, maxPositive: analysis.maxPositive, minCorrection: analysis.minCorrection, energyGainDb: analysis.energyGainDb, autoGainDb: analysis.autoGainDb, dipGuardReductionMaxDb: analysis.dipGuardReductionMaxDb })),
            };
            deps.getState().dsp = deps.getState().dsp || {};
            deps.getState().dsp.assistStack = deps.getState().dsp.assistStack || [];
            deps.getState().dsp.assistStack.push(item);
            conv.draft.left = null;
            conv.draft.right = null;
            conv.draft.presetName = '';
            conv.draft.nameTouched = false;
            await deps.fetchEffects();
            showMeasurementConvolverFeedback(`${itemName} created`);
            deps.showToast(`Created convolver preset: ${created.preset?.name || itemName}`, 'success');
        } catch (e) {
            showMeasurementConvolverFeedback('Convolver preset creation failed');
            deps.showToast(e.message || 'Convolver preset creation failed', 'error');
        } finally {
            deps.setConvolverCreateInFlight(false);
            conv.creatingPreset = false;
            deps.renderMeasurementPanel();
        }
    }

    return {
        init,
        clampMeasurementConvolverFrequency, getDefaultMeasurementConvolverState, ensureMeasurementConvolverState, getMeasurementConvolverCurveOptions, getMeasurementConvolverCurve, getMeasurementConvolverCurveDb, getMeasurementConvolverDraftPhaseMode, getMeasurementConvolverDrafts, getMeasurementConvolverDraftPhaseMismatch, clearMeasurementConvolverDraftForPhaseChange, clearMeasurementConvolverDraftForSettingsChange, updateMeasurementConvolverField, getMeasurementConvolverSelectedSourceEntries, getMeasurementConvolverSourceEntries, getMeasurementConvolverSourceSelectionState, getMeasurementConvolverMeasurementForSide, getMeasurementConvolverTracePoints, getMeasurementConvolverAdaptiveDipGuardStrength, applyMeasurementConvolverDipGuard, analyzeMeasurementConvolverSide, getMeasurementConvolverSelectedSourceCount, getMeasurementConvolverMultiSourceWarning, buildMeasurementConvolverWarnings, formatMeasurementConvolverGain, getMeasurementConvolverNameSuffix, getMeasurementConvolverItemName, getMeasurementConvolverPreviewMode, getMeasurementConvolverPreviewGain, showMeasurementConvolverFeedback, getMeasurementConvolverSampleRate, getMeasurementConvolverTypeOption, getMeasurementConvolverTypeKeys, getMeasurementConvolverPhaseModeForType, getMeasurementConvolverPhaseLabel, getMeasurementConvolverPhaseTag, getMeasurementConvolverFirLengthForType, getMeasurementConvolverFirLength, getMeasurementConvolverTypeLabel, interpolateMeasurementConvolverCorrection, buildMeasurementConvolverMagnitudeBins, buildMeasurementConvolverLinearImpulseFromMagnitudes, fftMeasurementConvolverComplex, buildMeasurementConvolverMinimumSpectrum, buildMeasurementConvolverImpulseFromSpectrum, buildMeasurementConvolverImpulse, getMeasurementConvolverTimingMs, getMeasurementConvolverTimingDelta, getMeasurementConvolverTimingPairDebug, formatMeasurementConvolverTimingRelation, getMeasurementConvolverTimingSafetyMessage, alignStereoImpulsesForMinimumAligned, getMeasurementAnalysisSampleRate, getMeasurementDirectArrivalTiming, writeMeasurementConvolverWav, appendMeasurementConvolverExtras, createMeasurementConvolverPreset, takeMeasurementConvolverToDraft, resolveMeasurementConvolverItemName, createMeasurementConvolverPresetFromDraft,
    };
});
