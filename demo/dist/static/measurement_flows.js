// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute measurement flows: Auto-Sub optimize and Advanced measurement
 * (hybrid) wizard.
 *
 * State/DOM access goes through injected getters, UI feedback through
 * injected callbacks, and every backend call through the injected `api`
 * object (streaming.js convention). No app.js globals are touched directly.
 */
(function () {
    'use strict';

    const ui = window.FXRouteMeasurementUI || {};
    const HybridMeasurement = window.FXRouteHybridMeasurement || {};
    const SpeakerAlign = (typeof window !== 'undefined' && window.FXRouteSpeakerAlign)
        || (typeof globalThis !== 'undefined' && globalThis.FXRouteSpeakerAlign) || {};

    let deps = {
        getState: () => ({ measurement: {}, settings: {} }),
        getElements: () => ({}),
        api: {},
        showToast: () => {},
        renderMeasurementPanel: () => {},
        renderMeasurementPanelDefensively: () => {},
        isSubwooferModeName: () => false,
        isSubwoofer22Mode: () => false,
        getActiveMeasurementKind: () => '',
        hasActiveMeasurementJob: () => false,
        measurementModeReady: () => false,
        normalizeMeasurementInputChannelSelections: () => {},
        getSelectedMeasurementInputChannelCount: () => 1,
        getAutoSubTargetCurveSnapshot: () => null,
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        postRuntimeDebugSnapshot: async () => {},
        formatTransitionErrorDetail: (d, m) => m,
        fetchAudioOutputOverview: async () => {},
        getMeasurementReferenceWarning: () => '',
        appendMeasurementReferenceFields: (formData) => {
            deps.normalizeMeasurementInputChannelSelections();
            formData.append('reference_input_channel', deps.getMeasurementReferenceWarning() ? '' : (deps.getState().measurement.selectedReferenceInputChannel || ''));
        },
        normalizeOutputModeName: (m) => m || 'stereo',
        // Selected measurement area (bank id): every wizard step is an internal
        // way sweep of the same frozen area.
        measurementAreaBank: () => '',
        // Speaker group of a Surround Global area ('' elsewhere).
        measurementAreaGroup: () => '',
        // Selected measuring area (the area object of output_state.measurementArea).
        measurementArea: () => null,
        // Routed speaker groups of the Surround Global area ({options, selected})
        // or null; the area of one such group; and the shared group selection
        // the sweep menu's speaker chips write as well.
        measurementGroupChoice: () => null,
        measurementAreaForGroup: () => null,
        selectMeasurementGroup: () => {},
        getMeasurementJobStatus: () => 'unknown',
        normalizeMeasurementEntry: (m) => m,
        getMeasurementJobResultMeasurement: () => null,
        setMeasurementAssistMode: () => {},
        setMeasurementGraphView: (view) => { deps.getState().measurement.measurementView = view; },
        escapeHtml: (v) => String(v == null ? '' : v),
        sleep: async () => {},
    };
    let api = {};
    let autoSubRecoveryPending = false;
    let lastAutoSubHandledJobId = '';

    function init(overrides) {
        const cfg = Object.assign({}, overrides || {});
        api = cfg.api || {};
        delete cfg.api;
        deps = Object.assign(deps, cfg);
    }

function syncSubwooferControlsDuringAutoSub() {
    const autoSubActive = !!(deps.getState().measurement?.autoSubInFlight);
    if (deps.getElements().effectsSubwooferDelay) deps.getElements().effectsSubwooferDelay.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferFrequencyNumber) deps.getElements().effectsSubwooferFrequencyNumber.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferLevel) deps.getElements().effectsSubwooferLevel.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferPolarity) deps.getElements().effectsSubwooferPolarity.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferSub2Level) deps.getElements().effectsSubwooferSub2Level.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferSub2Delay) deps.getElements().effectsSubwooferSub2Delay.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferSub2Polarity) deps.getElements().effectsSubwooferSub2Polarity.disabled = autoSubActive;
    if (deps.getElements().effectsSubwooferMainHighpass) deps.getElements().effectsSubwooferMainHighpass.disabled = autoSubActive;
}


// Status contract shared by Sweep, Auto Sub and Speaker Align: the feature
// line next to the action shows live progress (or its idle note), the panel
// status line (statusText) only the outcome, warning or error. Toasts carry a
// short headline, never the full status line.
const NO_CAPTURE_INPUT_TEXT = 'No usable capture input. Select one in Setup.';

function setFeatureLine(element, text) {
    if (!element) return;
    // The static note in the page shell is the idle text; keep it once.
    if (element.dataset && element.dataset.idleText === undefined) element.dataset.idleText = element.textContent;
    element.textContent = text || element.dataset?.idleText || '';
}

function progressFor(kind) {
    const measurementState = deps.getState().measurement || {};
    return measurementState.progressKind === kind ? String(measurementState.progressText || '') : '';
}

function renderAutoSubStatusLine() {
    setFeatureLine(deps.getElements().measurementAutoSubStatus, progressFor('auto_sub'));
}

function renderSpeakerAlignStatusLine() {
    setFeatureLine(deps.getElements().measurementSpeakerAlignStatus, progressFor('speaker_align'));
}

function renderStatusLine(kind) {
    if (kind === 'auto_sub') renderAutoSubStatusLine();
    if (kind === 'speaker_align') renderSpeakerAlignStatusLine();
}

function setProgress(kind, text) {
    const measurementState = deps.getState().measurement;
    measurementState.progressKind = kind;
    measurementState.progressText = String(text || '');
    renderStatusLine(kind);
}

function clearProgress(kind) {
    const measurementState = deps.getState().measurement;
    if (measurementState.progressKind === kind) {
        measurementState.progressKind = '';
        measurementState.progressText = '';
    }
    renderStatusLine(kind);
}

function syncAutoSubButton() {
    renderAutoSubStatusLine();
    if (!deps.getElements().measurementAutoSubStartBtn || !deps.getElements().measurementAutoSubGroup) return;
    const measurementState = deps.getState().measurement || {};
    const catalog = deps.getState().outputSystem?.catalog;
    const topology = catalog?.modes?.[catalog.active_mode]?.topology;
    const isSubwooferMode = ['mono', 'dual-mono', 'stereo'].includes(topology?.sub_mode);
    if (!isSubwooferMode) {
        deps.getElements().measurementAutoSubGroup.classList.add('hidden');
        return;
    }
    deps.getElements().measurementAutoSubGroup.classList.remove('hidden');
    const activeKind = deps.getActiveMeasurementKind();
    const autoSubActive = activeKind === 'auto_sub';
    const autoSubReadyToCancel = autoSubActive && !!measurementState.autoSubJobId;
    deps.getElements().measurementAutoSubStartBtn.disabled = autoSubActive
        ? !autoSubReadyToCancel
        : (measurementState.startInFlight || deps.hasActiveMeasurementJob() || !deps.measurementModeReady());
    deps.getElements().measurementAutoSubStartBtn.textContent = autoSubActive ? 'Cancel Auto Sub' : 'Auto Sub Optimize';

    // Sync subwoofer controls lock
    syncSubwooferControlsDuringAutoSub();
}


const AUTO_SUB_STAGE_LABELS = {
    balance_check: 'Level check',
    main_reference: 'Main reference',
    coarse: 'Coarse scan',
    fine: 'Fine scan',
    sub1_coarse: 'Optimizing Sub 1',
    sub2_coarse: 'Optimizing Sub 2',
    left_sub: 'Optimizing Sub 1',
    right_sub: 'Optimizing Sub 2',
    left_fine: 'Sub 1 fine scan',
    right_fine: 'Sub 2 fine scan',
    left_tiebreak: 'Sub 1 tie-break',
    right_tiebreak: 'Sub 2 tie-break',
    combined_matrix: 'Combined matrix',
    polarity_check: 'Polarity check',
    left_polarity_check: 'Sub 1 polarity check',
    right_polarity_check: 'Sub 2 polarity check',
    polarity_fine: 'Polarity fine-tune',
    polarity_refine: 'Polarity fine-tune',
    left_polarity_fine: 'Sub 1 polarity fine-tune',
    right_polarity_fine: 'Sub 2 polarity fine-tune',
    gain_after: 'Level match',
    gain_correction_after: 'Level match',
    deep_bass_check: 'Deep-bass check',
    confirmation_recheck: 'Final check',
    final_commit_confirmation: 'Final check',
};

function autoSubProgressText(job) {
    const status = job.status || '';
    if (status === 'cancelling') return 'Cancelling…';
    const progress = job.progress || {};
    const stageKey = String(progress.stage || job.stage || '');
    const stageLabel = AUTO_SUB_STAGE_LABELS[stageKey] || stageKey.replace(/_/g, ' ');
    const candidateCur = progress.candidate_current;
    const candidateTot = progress.candidate_total;
    const sweepCur = progress.sweep_current ?? progress.current;
    const sweepTot = progress.sweep_total ?? progress.total;
    const hasSweeps = Number.isFinite(sweepCur) && Number.isFinite(sweepTot);
    let counts = '';
    if (Number.isFinite(candidateCur) && Number.isFinite(candidateTot)) {
        counts = `${candidateCur}/${candidateTot} candidates${hasSweeps ? ` (${sweepCur}/${sweepTot} sweeps)` : ''}`;
    } else if (hasSweeps) {
        counts = `${sweepCur}/${sweepTot} sweeps`;
    }
    if (!counts) return stageLabel ? `${stageLabel}…` : 'Preparing…';
    const targetLabel = String(job.target_curve?.label || '');
    return `${stageLabel || 'Measuring'}: ${counts}${targetLabel ? ` · Target: ${targetLabel}` : ''}`;
}

// Backend failure messages repeat the action name; the status line adds it once.
function autoSubFailureText(message) {
    const reason = String(message || '').replace(/^Auto Sub Optimize[^:]*failed:\s*/i, '').trim();
    return reason ? `Auto Sub failed: ${reason}` : 'Auto Sub failed.';
}


async function startAutoSubOptimize() {
    const measurementState = deps.getState().measurement || {};
    if (measurementState.autoSubInFlight || measurementState.startInFlight || measurementState.activeJobId) return;

    if (!measurementState.selectedInputId || !deps.measurementModeReady()) {
        measurementState.statusText = NO_CAPTURE_INPUT_TEXT;
        deps.renderMeasurementPanel();
        deps.showToast('No capture input', 'error');
        return;
    }
    const inputId = measurementState.selectedInputId;

    // Claim the start before yielding: a duplicate must not clear this run's
    // shared state when the backend rejects its extra request.
    measurementState.autoSubInFlight = true;
    measurementState.startInFlight = true;
    measurementState.autoSubCancelRequested = false;
    measurementState.activeMeasurementKind = 'auto_sub';
    measurementState.autoSubJobId = '';
    measurementState.autoSubResult = null;
    measurementState.autoSubMeasurements = [];
    measurementState.statusText = '';
    syncSubwooferControlsDuringAutoSub();
    setProgress('auto_sub', 'Starting…');
    deps.renderMeasurementPanel();

    try {
        await deps.flushSubwooferSettingsBeforeMeasurement();
        const formData = new FormData();
        formData.append('input_id', inputId);
        formData.append('input_key', measurementState.selectedInputKey || '');
        // No explicit channel: Auto-Sub sweeps the whole system, so the
        // endpoint default of a left-side sweep applies.
        deps.normalizeMeasurementInputChannelSelections();
        formData.append('mic_input_channel', measurementState.selectedMicInputChannel || '1');
        deps.appendMeasurementReferenceFields(formData);
        formData.append('calibration_ref', measurementState.selectedCalibrationRef || '');
        const targetCurveSnapshot = deps.getAutoSubTargetCurveSnapshot();
        formData.append('target_curve_snapshot', targetCurveSnapshot ? JSON.stringify(targetCurveSnapshot) : '');
        const calibrationFile = deps.getElements().measurementCalibrationFile?.files?.[0];
        if (calibrationFile) {
            formData.append('calibration_file', calibrationFile);
        }

        await deps.postRuntimeDebugSnapshot('ui-before-auto-sub-start', {});

        const resp = await api.startAutoSubOptimize(formData);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'could not start'));
        const job = data.job || {};
        measurementState.autoSubJobId = String(job.id || '');
        if (!measurementState.autoSubCancelRequested) setProgress('auto_sub', autoSubProgressText(job));
        deps.renderMeasurementPanel();
        if (measurementState.autoSubCancelRequested) await cancelAutoSubOptimize();
        if (!measurementState.autoSubJobId) throw new Error('no job id returned');
        await pollAutoSubJob(measurementState.autoSubJobId);
    } catch (error) {
        console.error('startAutoSubOptimize failed', error);
        measurementState.statusText = autoSubFailureText(error.message);
        deps.showToast('Auto Sub failed', 'error');
    } finally {
        measurementState.autoSubInFlight = false;
        measurementState.startInFlight = false;
        measurementState.activeMeasurementKind = '';
        measurementState.autoSubJobId = '';
        measurementState.autoSubCancelRequested = false;
        clearProgress('auto_sub');
        // Refresh audio outputs to pick up new sub_alignment_ms
        deps.fetchAudioOutputOverview().catch(() => {});
        deps.renderMeasurementPanel();
    }
}


async function recoverAutoSubJob() {
    const measurementState = deps.getState().measurement || {};
    if (autoSubRecoveryPending || measurementState.autoSubInFlight || measurementState.startInFlight
            || measurementState.activeJobId || measurementState.speakerAlignInFlight
            || measurementState.hybridWizard?.running) return;
    const generation = Number(measurementState.jobGeneration || 0);
    autoSubRecoveryPending = true;
    try {
        const resp = await api.getCurrentAutoSubJob();
        if (!resp.ok) throw new Error('Auto Sub job discovery failed');
        const data = await resp.json();
        const job = data.job;
        if (!job?.id || String(job.id) === lastAutoSubHandledJobId) return;
        const live = deps.getState().measurement || {};
        if (Number(live.jobGeneration || 0) !== generation || live.autoSubInFlight
                || live.startInFlight || live.activeJobId || live.speakerAlignInFlight
                || live.hybridWizard?.running) return;

        live.autoSubInFlight = true;
        live.activeMeasurementKind = 'auto_sub';
        live.autoSubJobId = String(job.id);
        live.autoSubCancelRequested = job.status === 'cancelling';
        live.autoSubResult = null;
        live.autoSubMeasurements = [];
        live.statusText = '';
        setProgress('auto_sub', autoSubProgressText(job));
        deps.renderMeasurementPanel();
        try {
            if (['completed', 'failed', 'cancelled'].includes(job.status)) {
                await handleAutoSubResult(job);
                lastAutoSubHandledJobId = String(job.id);
            } else {
                await pollAutoSubJob(live.autoSubJobId);
            }
        } finally {
            if (isCurrentAutoSubPoll(job.id, generation)) {
                live.autoSubInFlight = false;
                live.activeMeasurementKind = '';
                live.autoSubJobId = '';
                live.autoSubCancelRequested = false;
                clearProgress('auto_sub');
                deps.fetchAudioOutputOverview().catch(() => {});
                deps.renderMeasurementPanel();
            }
        }
    } catch (error) {
        console.warn('recoverAutoSubJob failed', error);
    } finally {
        autoSubRecoveryPending = false;
    }
}


async function cancelAutoSubOptimize() {
    const measurementState = deps.getState().measurement;
    const jobId = String(measurementState.autoSubJobId || '');
    if (!jobId) {
        if (measurementState.autoSubInFlight) {
            measurementState.autoSubCancelRequested = true;
            setProgress('auto_sub', 'Cancelling…');
            deps.renderMeasurementPanel();
        }
        return;
    }
    const previousProgress = progressFor('auto_sub');
    setProgress('auto_sub', 'Cancelling…');
    deps.renderMeasurementPanel();
    try {
        const resp = await api.cancelAutoSubJob(jobId);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'request rejected'));
    } catch (error) {
        console.error('cancelAutoSubOptimize failed', error);
        if (measurementState.autoSubInFlight) setProgress('auto_sub', previousProgress);
        measurementState.statusText = `Could not cancel Auto Sub: ${error.message || 'request failed'}`;
        deps.showToast('Cancel failed', 'error');
    } finally {
        deps.renderMeasurementPanel();
    }
}


function isCurrentAutoSubPoll(jobId, generation) {
    const live = deps.getState().measurement || {};
    return Number(live.jobGeneration || 0) === Number(generation || 0)
        && live.activeMeasurementKind === 'auto_sub'
        && String(live.autoSubJobId || '') === String(jobId);
}


function isCurrentHybridPoll(jobId, generation) {
    const live = deps.getState().measurement || {};
    return Number(live.jobGeneration || 0) === Number(generation || 0)
        && live.activeMeasurementKind === 'hybrid'
        && String(live.activeJobId || '') === String(jobId);
}


async function pollAutoSubJob(jobId) {
    const measurementState = deps.getState().measurement || {};
    const pollGeneration = Number(measurementState.jobGeneration || 0);
    const startedAt = Date.now();
    const longRunningAfterMs = 10 * 60 * 1000;
    const maxRunningMs = 30 * 60 * 1000;
    let consecutiveErrors = 0;
    const maxConsecutiveErrors = 40;
    while (true) {
        if (!isCurrentAutoSubPoll(jobId, pollGeneration)) return;
        await deps.sleep(500);
        if (Date.now() - startedAt >= maxRunningMs) {
            measurementState.statusText = 'Auto Sub timed out waiting for the result.';
            deps.showToast('Auto Sub timed out', 'error');
            return;
        }
        try {
            const resp = await api.pollAutoSubJob(jobId);
            const data = await resp.json().catch(() => ({}));
            if (!isCurrentAutoSubPoll(jobId, pollGeneration)) return;
            if (!resp.ok) {
                if (resp.status === 404 || resp.status === 410) {
                    measurementState.statusText = 'Auto Sub interrupted: the run is no longer available.';
                    deps.showToast('Auto Sub interrupted', 'error');
                    return;
                }
                throw new Error(deps.formatTransitionErrorDetail(data.detail, 'connection lost'));
            }
            consecutiveErrors = 0;
            const job = data.job || {};
            const status = job.status || 'unknown';

            // A requested cancel keeps "Cancelling…" until the job ends.
            if (progressFor('auto_sub') !== 'Cancelling…') {
                const progressText = autoSubProgressText(job);
                const stillRunning = Date.now() - startedAt >= longRunningAfterMs
                    && (status === 'queued' || status === 'preparing' || status === 'running');
                setProgress('auto_sub', stillRunning ? `Still running · ${progressText}` : progressText);
            }

            // Live: push baseline measurement data to graph as soon as available
            if (job.baseline_measurement && !measurementState.autoSubMeasurements.length) {
                measurementState.autoSubMeasurements = [job.baseline_measurement];
                measurementState.currentMeasurementSaved = false;
            }

            if (status === 'completed' || status === 'failed' || status === 'cancelled') {
                await handleAutoSubResult(job);
                lastAutoSubHandledJobId = String(jobId);
                return;
            }
        } catch (error) {
            if (!isCurrentAutoSubPoll(jobId, pollGeneration)) return;
            consecutiveErrors += 1;
            console.warn('pollAutoSubJob error', error);
            if (consecutiveErrors >= maxConsecutiveErrors) {
                measurementState.statusText = `Auto Sub interrupted: ${error?.message || 'connection lost'}`;
                deps.showToast('Auto Sub interrupted', 'error');
                return;
            }
        }
        deps.renderMeasurementPanel();
    }
}


async function handleAutoSubResult(job) {
    const measurementState = deps.getState().measurement || {};
    const result = job.result;
    if (job.status === 'cancelled') {
        measurementState.statusText = /window was closed/i.test(String(job.message || ''))
            ? 'Auto Sub cancelled: the measurement window was closed.'
            : 'Auto Sub cancelled.';
        measurementState.autoSubResult = null;
        measurementState.autoSubMeasurements = [];
        syncSubwooferControlsDuringAutoSub();
        deps.showToast('Auto Sub cancelled', 'success');
        return;
    }
    if (job.status === 'failed') {
        measurementState.statusText = autoSubFailureText(job.message);
        measurementState.autoSubResult = null;
        measurementState.autoSubMeasurements = [];
        syncSubwooferControlsDuringAutoSub();
        deps.showToast('Auto Sub failed', 'error');
        return;
    }
    if (!result) {
        measurementState.statusText = 'Auto Sub finished without a result.';
        measurementState.autoSubMeasurements = [];
        syncSubwooferControlsDuringAutoSub();
        deps.showToast('Auto Sub: no result', 'warning');
        return;
    }

    measurementState.autoSubResult = result;

    // Extract baseline and confirmation measurement curves for graph display
    const autoSubMeasurements = [];
    const baselineMeas = result.baseline_measurement;
    const confirmMeas = result.confirmation_measurement;
    if (baselineMeas && Array.isArray(baselineMeas.traces) && baselineMeas.traces.length) {
        autoSubMeasurements.push(baselineMeas);
    }
    if (confirmMeas && Array.isArray(confirmMeas.traces) && confirmMeas.traces.length) {
        autoSubMeasurements.push(confirmMeas);
    }
    // Preserve live-pushed measurements if result didn't provide any (defensive fallback)
    if (!autoSubMeasurements.length && Array.isArray(measurementState.autoSubMeasurements) && measurementState.autoSubMeasurements.length) {
        // Keep existing live-pushed measurements
    } else {
        measurementState.autoSubMeasurements = autoSubMeasurements;
    }
    measurementState.currentMeasurementSaved = false;
    // The result is saved as AutoSub while the field is blank (the field's
    // placeholder says so); a stored "AutoSub" would be taken over by the next
    // sweep or L/R repeat as its base name.  A typed name is kept.
    measurementState.currentMeasurementName = ui.getTypedMeasurementName(measurementState);
    const winner = result.winner || {};
    // The outcome line comes first; scan details follow on a second line.
    const withDetail = (outcome, detailParts) => (detailParts.length ? `${outcome}\n${detailParts.join(' · ')}` : outcome);
    if (deps.isSubwoofer22Mode(result.mode) || Number.isFinite(result.applied_sub1_alignment_ms) || Number.isFinite(result.applied_sub2_alignment_ms)) {
        const isStereoBassResult = result.mode === 'subwoofer-2.2-stereo';
        const sub1Label = 'Sub 1';
        const sub2Label = 'Sub 2';
        const modeLabel = isStereoBassResult ? '2.2 Stereo Bass' : '2.2';
        const originalSub1 = Number.isFinite(result.original_sub1_alignment_ms) ? result.original_sub1_alignment_ms : null;
        const originalSub2 = Number.isFinite(result.original_sub2_alignment_ms) ? result.original_sub2_alignment_ms : null;
        const appliedSub1 = Number.isFinite(result.applied_sub1_alignment_ms) ? result.applied_sub1_alignment_ms : null;
        const appliedSub2 = Number.isFinite(result.applied_sub2_alignment_ms) ? result.applied_sub2_alignment_ms : null;
        const finiteNumber = (value) => {
            const num = Number(value);
            return Number.isFinite(num) ? num : null;
        };
        const scorePctFromScore = (value) => {
            const num = finiteNumber(value);
            return num !== null ? num * 100 : null;
        };
        const leftScorePct = finiteNumber(winner.score_L_pct) ?? finiteNumber(result.left_score_pct) ?? scorePctFromScore(result.left_score);
        const rightScorePct = finiteNumber(winner.score_R_pct) ?? finiteNumber(result.right_score_pct) ?? scorePctFromScore(result.right_score);
        const overallScorePct = finiteNumber(winner.overall_score_pct)
            ?? finiteNumber(result.overall_score_pct)
            ?? scorePctFromScore(winner.overall_score)
            ?? scorePctFromScore(result.overall_score)
            ?? (leftScorePct !== null && rightScorePct !== null
                ? (0.6 * Math.min(leftScorePct, rightScorePct)) + (0.4 * ((leftScorePct + rightScorePct) / 2))
                : null);
        const combinedScorePct = finiteNumber(winner.score_pct);
        const scorePct = isStereoBassResult ? overallScorePct : combinedScorePct;
        const sub1Text = `${appliedSub1 !== null ? appliedSub1.toFixed(2) : '?'} ms (was ${originalSub1 !== null ? originalSub1.toFixed(2) : '?'} ms)`;
        const sub2Text = `${appliedSub2 !== null ? appliedSub2.toFixed(2) : '?'} ms (was ${originalSub2 !== null ? originalSub2.toFixed(2) : '?'} ms)`;
        const lrScoreText = leftScorePct !== null && rightScorePct !== null
            ? ` · L ${leftScorePct.toFixed(1)} % / R ${rightScorePct.toFixed(1)} %`
            : '';
        const scorePart = isStereoBassResult
            ? `Overall ${scorePct !== null ? `${scorePct.toFixed(1)} %` : 'unavailable'}${lrScoreText}`
            : `Combined ${scorePct !== null ? `${scorePct.toFixed(1)} %` : 'unavailable'}${lrScoreText}`;

        const detailParts = [];
        const sub1Coarse = result.sub1_coarse_winner || result.left_winner || {};
        const sub2Coarse = result.sub2_coarse_winner || result.right_winner || {};
        if (Number.isFinite(sub1Coarse.delay_ms)) {
            const sub1Score = Number.isFinite(sub1Coarse.score_pct) ? ` (${sub1Coarse.score_pct.toFixed(1)} %)` : '';
            detailParts.push(`${sub1Label} scan ${sub1Coarse.delay_ms.toFixed(2)} ms${sub1Score}`);
        }
        if (Number.isFinite(sub2Coarse.delay_ms)) {
            const sub2Score = Number.isFinite(sub2Coarse.score_pct) ? ` (${sub2Coarse.score_pct.toFixed(1)} %)` : '';
            detailParts.push(`${sub2Label} scan ${sub2Coarse.delay_ms.toFixed(2)} ms${sub2Score}`);
        }
        if (
            Number.isFinite(result.derived_main_delay_ms)
            && Number.isFinite(result.derived_sub1_delay_ms)
            && Number.isFinite(result.derived_sub2_delay_ms)
        ) {
            detailParts.push(`Output delays: Main ${result.derived_main_delay_ms.toFixed(2)} ms / ${sub1Label} ${result.derived_sub1_delay_ms.toFixed(2)} ms / ${sub2Label} ${result.derived_sub2_delay_ms.toFixed(2)} ms`);
        }
        measurementState.statusText = withDetail(
            `Auto Sub ${modeLabel} applied: ${sub1Label} ${sub1Text} · ${sub2Label} ${sub2Text} · ${scorePart}`,
            detailParts,
        );

        syncSubwooferControlsDuringAutoSub();
        deps.showToast(`Auto Sub ${modeLabel} applied`, 'success');
        deps.renderMeasurementPanel();
        return;
    }
    const original = Number.isFinite(result.original_alignment_ms) ? result.original_alignment_ms : null;
    const applied = Number.isFinite(result.applied_alignment_ms) ? result.applied_alignment_ms : null;
    const suggested = Number.isFinite(result.suggested_alignment_ms) ? result.suggested_alignment_ms : applied;
    const confirmationGate = result.confirmation_gate || null;
    const gateReverted = confirmationGate && confirmationGate.action === 'alignment_reverted_balance_kept';
    const gateIncumbentKept = gateReverted && Number.isFinite(original) && Number.isFinite(applied)
        && Math.abs(applied - original) < 0.005 && Number.isFinite(suggested) && Math.abs(suggested - original) >= 0.005;
    const wasApplied = result.applied !== false;
    const scorePct = Number.isFinite(winner.score_pct) ? winner.score_pct : '?';
    const scorePctText = Number.isFinite(scorePct) ? scorePct.toFixed(1) : '?';
    const hasWinnerLRScores = Number.isFinite(winner.score_L_pct) && Number.isFinite(winner.score_R_pct);
    const conf = result.confidence || 'unknown';
    const fineScan = result.fine_scan || {};

    const coarseW = result.coarse_winner || {};
    const fineW = result.fine_winner;
    const originalText = original !== null ? original.toFixed(2) : '?';
    const appliedText = applied !== null ? applied.toFixed(2) : '?';
    const suggestedText = suggested !== null ? suggested.toFixed(2) : '?';
    const isWeak = conf === 'uncertain';
    const applyDecision = typeof result.apply_decision === 'string' ? result.apply_decision : null;
    const incumbentKept = !wasApplied && (
        applyDecision === 'not_applied_incumbent_better'
        || (suggested !== null && original !== null && Math.abs(suggested - original) < 0.005)
    ) && !gateIncumbentKept;
    const notAppliedReason = !wasApplied && !incumbentKept && !gateIncumbentKept
        ? ({
            'reverted_to_original_state': 'final check failed, original restored',
            'not_applied_close_margin_below_2pp': 'advantage too small',
            'not_applied_close_gain_below_3pp': 'advantage too small',
            'not_applied_uncertain_confidence': 'uncertain confidence',
            'applied_fine_scan_winner': 'fine-scan winner',
        }[applyDecision] || '')
        : '';

    const scoreText = (entry, fallbackPct) => {
        const pct = Number.isFinite(entry.score_pct) ? entry.score_pct.toFixed(1) : fallbackPct;
        const lr = Number.isFinite(entry.score_L_pct) && Number.isFinite(entry.score_R_pct)
            ? ` · L ${entry.score_L_pct.toFixed(1)} % / R ${entry.score_R_pct.toFixed(1)} %` : '';
        return `Score ${pct} %${lr}`;
    };
    let outcome;
    if (gateIncumbentKept) {
        // The confirmation gate kept the start alignment (level balance still
        // applied); the score belongs to the rejected suggestion, not to it.
        const gateWinner = fineW && Number.isFinite(fineW.score_pct) ? fineW : winner;
        outcome = `Auto Sub applied ${appliedText} ms (was ${originalText} ms). `
            + `Suggested ${suggestedText} ms (${scoreText(gateWinner, scorePctText)}) was rejected by the local-dip check.`;
    } else {
        let action;
        if (wasApplied) {
            action = `applied ${appliedText} ms (was ${originalText} ms)`;
        } else if (incumbentKept) {
            action = `kept the current alignment: ${originalText} ms`;
        } else {
            // A weak verdict already explains an uncertain-confidence skip.
            const reason = isWeak && applyDecision === 'not_applied_uncertain_confidence' ? '' : notAppliedReason;
            action = `suggested ${suggestedText} ms (was ${originalText} ms), not applied${reason ? `: ${reason}` : ''}`;
        }
        const scorePart = hasWinnerLRScores ? scoreText(winner, scorePctText) : `Score ${scorePctText} %`;
        outcome = isWeak
            ? `Auto Sub result weak: ${action} · ${scorePart}. Check with a normal sweep.`
            : `Auto Sub ${action} · ${scorePart}`;
    }

    const detailParts = [];
    if (['clear', 'close', 'uncertain'].includes(conf)) detailParts.push(`Confidence ${conf}`);
    if (fineScan.triggered && fineScan.status === 'completed') {
        const cDelay = Number.isFinite(coarseW.delay_ms) ? coarseW.delay_ms.toFixed(2) : '?';
        const cScore = Number.isFinite(coarseW.score_pct) ? coarseW.score_pct.toFixed(1) : '?';
        detailParts.push(`Coarse ${cDelay} ms (${cScore} %)`);
        if (fineW) {
            const fDelay = Number.isFinite(fineW.delay_ms) ? fineW.delay_ms.toFixed(2) : '?';
            const fScore = Number.isFinite(fineW.score_pct) ? fineW.score_pct.toFixed(1) : '?';
            detailParts.push(`Fine ${fDelay} ms (${fScore} %)`);
        }
    } else if (result.runner_up) {
        const rDelay = Number.isFinite(result.runner_up.delay_ms) ? result.runner_up.delay_ms.toFixed(2) : '?';
        const rScore = Number.isFinite(result.runner_up.score_pct) ? result.runner_up.score_pct.toFixed(1) : '?';
        detailParts.push(`Runner-up ${rDelay} ms (${rScore} %)`);
    }
    measurementState.statusText = withDetail(outcome, detailParts);

    let toastText;
    if (isWeak) toastText = 'Auto Sub result weak';
    else if (wasApplied) toastText = 'Auto Sub applied';
    else if (incumbentKept) toastText = 'Auto Sub kept the current alignment';
    else toastText = 'Auto Sub result not applied';
    syncSubwooferControlsDuringAutoSub();
    const toastType = isWeak ? 'error' : (wasApplied ? 'success' : 'warning');
    deps.showToast(toastText, toastType);
    deps.renderMeasurementPanel();
}


function speakerAlignCatalog() {
    return deps.getState().outputSystem?.catalog || null;
}

function speakerAlignVisible() {
    if (typeof SpeakerAlign.speakerAlignVisible === 'function') {
        try {
            return !!SpeakerAlign.speakerAlignVisible(speakerAlignCatalog());
        } catch (e) {
            return false;
        }
    }
    return false;
}

function speakerSideLabel(side) {
    return String(side) === 'right' ? 'Right' : 'Left';
}

function formatSpeakerStatus(job) {
    if (typeof SpeakerAlign.formatSpeakerAlignStatus === 'function') {
        return SpeakerAlign.formatSpeakerAlignStatus(job);
    }
    const record = job || {};
    return String(record.message || `Speaker Align ${speakerSideLabel(record.side)}: ${record.status || 'unknown'}.`);
}

function speakerAlignProgressText(job) {
    const status = String(job.status || '');
    if (status === 'cancelling') return 'Cancelling…';
    if (status === 'queued') return 'Starting…';
    return String(job.message || 'Running…');
}

function syncSpeakerAlignButton() {
    renderSpeakerAlignStatusLine();
    const elements = deps.getElements();
    if (!elements.measurementSpeakerAlignLeftBtn || !elements.measurementSpeakerAlignGroup) return;
    const measurementState = deps.getState().measurement || {};
    if (!speakerAlignVisible()) {
        elements.measurementSpeakerAlignGroup.classList.add('hidden');
        return;
    }
    elements.measurementSpeakerAlignGroup.classList.remove('hidden');
    const activeKind = deps.getActiveMeasurementKind();
    const speakerActive = activeKind === 'speaker_align' || !!measurementState.speakerAlignInFlight;
    const speakerReadyToCancel = speakerActive && !!measurementState.speakerAlignJobId;
    const disabled = speakerActive || measurementState.startInFlight || deps.hasActiveMeasurementJob() || !deps.measurementModeReady();
    elements.measurementSpeakerAlignLeftBtn.disabled = disabled;
    elements.measurementSpeakerAlignRightBtn.disabled = disabled;
    elements.measurementSpeakerAlignCancelBtn.classList.toggle('hidden', !speakerActive);
    elements.measurementSpeakerAlignCancelBtn.disabled = !speakerReadyToCancel;
}

function readSpeakerAlignPayload(side) {
    const measurementState = deps.getState().measurement || {};
    deps.normalizeMeasurementInputChannelSelections();
    const referenceChannel = deps.getMeasurementReferenceWarning() ? ''
        : (measurementState.selectedReferenceInputChannel || '');
    // Per-side loopback references, mirroring manual sweeps: a right sweep
    // must record the right loopback (e.g. input 8), never the shared/left
    // one, or the reference comes back silent and the run cannot qualify.
    const splitReferences = typeof deps.getSelectedMeasurementInputChannelCount === 'function'
        && deps.getSelectedMeasurementInputChannelCount() >= 3;
    const referenceChannelLeft = splitReferences
        ? (measurementState.selectedReferenceInputChannelLeft || '') : '';
    const referenceChannelRight = splitReferences
        ? (measurementState.selectedReferenceInputChannelRight || '') : '';
    // The identity names the channel actually recorded for this side.
    const sideReferenceChannel = (splitReferences && (referenceChannelLeft || referenceChannelRight))
        ? (String(side) === 'right' ? referenceChannelRight : referenceChannelLeft)
        : referenceChannel;
    if (typeof SpeakerAlign.buildSpeakerAlignPayload !== 'function') {
        throw new Error('Speaker Align support is unavailable');
    }
    return SpeakerAlign.buildSpeakerAlignPayload({
        side,
        inputId: measurementState.selectedInputId,
        micChannel: measurementState.selectedMicInputChannel || '1',
        referenceChannel,
        referenceChannelLeft,
        referenceChannelRight,
        referenceId: SpeakerAlign.defaultReferenceId(measurementState.selectedInputId, sideReferenceChannel),
        microphonePositionId: `${side}-fixed-${Date.now()}`,
        dryRun: false,
    });
}

async function startSpeakerAlign(side) {
    if (!speakerAlignVisible()) return;
    const measurementState = deps.getState().measurement || {};
    if (measurementState.speakerAlignInFlight || measurementState.startInFlight
        || measurementState.activeJobId || measurementState.autoSubInFlight) return;
    const name = `Speaker Align ${speakerSideLabel(side)}`;
    if (!measurementState.selectedInputId || !deps.measurementModeReady()) {
        measurementState.statusText = NO_CAPTURE_INPUT_TEXT;
        deps.renderMeasurementPanel();
        deps.showToast('No capture input', 'error');
        return;
    }
    let payload;
    try {
        payload = readSpeakerAlignPayload(side);
    } catch (error) {
        measurementState.statusText = `${name} could not start: ${error?.message || 'invalid setup'}`;
        deps.showToast(`${name} failed`, 'error');
        deps.renderMeasurementPanel();
        return;
    }
    measurementState.speakerAlignInFlight = true;
    measurementState.startInFlight = true;
    measurementState.speakerAlignCancelRequested = false;
    measurementState.activeMeasurementKind = 'speaker_align';
    measurementState.speakerAlignJobId = '';
    measurementState.speakerAlignResult = null;
    measurementState.statusText = '';
    setProgress('speaker_align', 'Starting…');
    syncSpeakerAlignButton();
    deps.renderMeasurementPanel();
    try {
        await deps.postRuntimeDebugSnapshot('ui-before-speaker-align-start', {});
        const resp = await api.startSpeakerAlign(payload);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'could not start'));
        const job = data.job || {};
        measurementState.speakerAlignJobId = String(job.id || '');
        if (!measurementState.speakerAlignCancelRequested) setProgress('speaker_align', speakerAlignProgressText(job));
        deps.renderMeasurementPanel();
        if (measurementState.speakerAlignCancelRequested) await cancelSpeakerAlign();
        if (!measurementState.speakerAlignJobId) throw new Error('no job id returned');
        await pollSpeakerAlignJob(measurementState.speakerAlignJobId, side);
    } catch (error) {
        console.error('startSpeakerAlign failed', error);
        measurementState.statusText = `${name} failed: ${error.message || 'unknown error'}`;
        deps.showToast(`${name} failed`, 'error');
    } finally {
        measurementState.speakerAlignInFlight = false;
        measurementState.startInFlight = false;
        measurementState.activeMeasurementKind = '';
        measurementState.speakerAlignJobId = '';
        measurementState.speakerAlignCancelRequested = false;
        clearProgress('speaker_align');
        deps.fetchAudioOutputOverview().catch(() => {});
        deps.renderMeasurementPanel();
    }
}

async function cancelSpeakerAlign() {
    const measurementState = deps.getState().measurement;
    const jobId = String(measurementState.speakerAlignJobId || '');
    if (!jobId) {
        if (measurementState.speakerAlignInFlight) {
            measurementState.speakerAlignCancelRequested = true;
            setProgress('speaker_align', 'Cancelling…');
            deps.renderMeasurementPanel();
        }
        return;
    }
    const previousProgress = progressFor('speaker_align');
    setProgress('speaker_align', 'Cancelling…');
    deps.renderMeasurementPanel();
    try {
        const resp = await api.cancelSpeakerAlignJob(jobId);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'request rejected'));
    } catch (error) {
        console.error('cancelSpeakerAlign failed', error);
        if (measurementState.speakerAlignInFlight) setProgress('speaker_align', previousProgress);
        measurementState.statusText = `Could not cancel Speaker Align: ${error.message || 'request failed'}`;
        deps.showToast('Cancel failed', 'error');
    } finally {
        deps.renderMeasurementPanel();
    }
}

// Giving up on polling must not leave the backend job running: it would keep
// the measurement owner and the output mutes. Cancel it before local cleanup.
async function abandonSpeakerAlignJob(jobId, side, reason) {
    let cancelled = false;
    try {
        const resp = await api.cancelSpeakerAlignJob(jobId);
        cancelled = !!resp?.ok;
    } catch (error) {
        console.warn('Speaker Align cancel after polling gave up failed', error);
    }
    const measurementState = deps.getState().measurement || {};
    const name = `Speaker Align ${speakerSideLabel(side)}`;
    discardSpeakerAlignResults();
    measurementState.statusText = cancelled
        ? `${name} interrupted: ${reason}. The run was cancelled.`
        : `${name} interrupted: ${reason}. Cancelling the run failed.`;
    deps.showToast(`${name} interrupted`, 'error');
}

function isCurrentSpeakerAlignPoll(jobId, generation) {
    const live = deps.getState().measurement || {};
    return Number(live.jobGeneration || 0) === Number(generation || 0)
        && (live.activeMeasurementKind === 'speaker_align' || !!live.speakerAlignInFlight)
        && String(live.speakerAlignJobId || '') === String(jobId);
}

async function pollSpeakerAlignJob(jobId, side = '') {
    const measurementState = deps.getState().measurement || {};
    const pollGeneration = Number(measurementState.jobGeneration || 0);
    const startedAt = Date.now();
    const longRunningAfterMs = 10 * 60 * 1000;
    const maxRunningMs = 30 * 60 * 1000;
    let consecutiveErrors = 0;
    const maxConsecutiveErrors = 40;
    let jobSide = side;
    while (true) {
        if (!isCurrentSpeakerAlignPoll(jobId, pollGeneration)) return;
        await deps.sleep(500);
        if (Date.now() - startedAt >= maxRunningMs) {
            await abandonSpeakerAlignJob(jobId, jobSide, 'timed out waiting for the result');
            return;
        }
        try {
            const resp = await api.pollSpeakerAlignJob(jobId);
            const data = await resp.json().catch(() => ({}));
            if (!isCurrentSpeakerAlignPoll(jobId, pollGeneration)) return;
            if (!resp.ok) {
                if (resp.status === 404 || resp.status === 410) {
                    measurementState.statusText = `Speaker Align ${speakerSideLabel(jobSide)} interrupted: the run is no longer available.`;
                    deps.showToast('Speaker Align interrupted', 'error');
                    return;
                }
                throw new Error(deps.formatTransitionErrorDetail(data.detail, 'connection lost'));
            }
            consecutiveErrors = 0;
            const job = data.job || {};
            const status = job.status || 'unknown';
            jobSide = job.side || jobSide;
            // A requested cancel keeps "Cancelling…" until the job ends.
            if (progressFor('speaker_align') !== 'Cancelling…') {
                const progressText = speakerAlignProgressText(job);
                const stillRunning = Date.now() - startedAt >= longRunningAfterMs
                    && (status === 'queued' || status === 'acquiring' || status === 'confirming');
                setProgress('speaker_align', stillRunning ? `Still running · ${progressText}` : progressText);
            }
            if (status === 'committed' || status === 'trial-done' || status === 'unconfirmed'
                || status === 'failed' || status === 'cancelled') {
                await handleSpeakerAlignResult(job);
                return;
            }
        } catch (error) {
            if (!isCurrentSpeakerAlignPoll(jobId, pollGeneration)) return;
            consecutiveErrors += 1;
            console.warn('pollSpeakerAlignJob error', error);
            if (consecutiveErrors >= maxConsecutiveErrors) {
                await abandonSpeakerAlignJob(jobId, jobSide, error?.message || 'connection lost');
                return;
            }
        }
        deps.renderMeasurementPanel();
    }
}

// A failed or cancelled run leaves no result behind: an earlier Verified
// table must not stay on screen as if it belonged to this run.
function discardSpeakerAlignResults() {
    const measurementState = deps.getState().measurement || {};
    measurementState.speakerAlignResult = null;
    measurementState.speakerAlignResults = null;
    const resultsEl = deps.getElements().measurementSpeakerAlignResults;
    if (resultsEl) resultsEl.innerHTML = '';
}

// Before (planning take) and After (verification take) are normal sweeps:
// they become the pending pair of the normal measurement flow, drawn in the
// IR view first and stored through "Save current" like any other pair.
function adoptSpeakerAlignMeasurements(job) {
    const takes = typeof SpeakerAlign.takeMeasurements === 'function'
        ? SpeakerAlign.takeMeasurements(job.result) : [];
    if (!takes.length) return;
    const measurementState = deps.getState().measurement || {};
    const pending = takes.map((measurement, index) => deps.normalizeMeasurementEntry(measurement, index));
    measurementState.autoSubMeasurements = [];
    measurementState.pendingRepeatMeasurements = pending;
    measurementState.currentMeasurement = pending[0];
    // A name typed into the field is kept; the side stays in the entries' channel.
    measurementState.currentMeasurementName = ui.getTypedMeasurementName(measurementState)
        || `Speaker Align ${speakerSideLabel(job.side)}`;
    measurementState.currentMeasurementSaved = false;
    measurementState.reviewVisibilityById = measurementState.reviewVisibilityById || {};
    pending.forEach((measurement) => {
        measurementState.reviewVisibilityById[measurement.id] = !!measurement.review_traces?.length;
    });
    deps.setMeasurementGraphView('ir');
}

// The outcome goes to the panel status line; the numbers stay in the result
// table next to the buttons.
async function handleSpeakerAlignResult(job) {
    const measurementState = deps.getState().measurement || {};
    const name = `Speaker Align ${speakerSideLabel(job.side)}`;
    const text = formatSpeakerStatus(job);
    if (job.status === 'cancelled' || job.status === 'cancelling') {
        measurementState.statusText = text;
        discardSpeakerAlignResults();
        deps.showToast(`${name} cancelled`, 'success');
        return;
    }
    if (job.status === 'failed') {
        measurementState.statusText = text;
        discardSpeakerAlignResults();
        deps.showToast(`${name} failed`, 'error');
        return;
    }
    if (!job.result) {
        measurementState.statusText = `${name} finished without a result.`;
        deps.showToast(`${name}: no result`, 'warning');
        return;
    }
    measurementState.speakerAlignResult = job.result;
    measurementState.speakerAlignResults = { ...measurementState.speakerAlignResults, [job.side]: job.result };
    const resultsEl = deps.getElements().measurementSpeakerAlignResults;
    if (resultsEl) {
        resultsEl.innerHTML = ['left', 'right'].map(side =>
            SpeakerAlign.renderSpeakerAlignResult(measurementState.speakerAlignResults[side], side)).join('');
    }
    measurementState.statusText = text;
    adoptSpeakerAlignMeasurements(job);
    if (job.status === 'committed') {
        deps.showToast(`${name} applied`, 'success');
    } else if (job.status === 'trial-done') {
        const confirmed = job.result?.confirmed === true;
        deps.showToast(confirmed ? `${name} trial verified` : `${name} trial not verified`, confirmed ? 'success' : 'warning');
    } else {
        deps.showToast(`${name} not verified`, 'warning');
    }
    deps.renderMeasurementPanel();
}


function clearSpeakerAlignResult() {
    const measurementState = deps.getState().measurement || {};
    if (measurementState.speakerAlignInFlight || measurementState.speakerAlignJobId
        || measurementState.activeMeasurementKind === 'speaker_align') return;
    measurementState.speakerAlignResult = null;
    measurementState.speakerAlignResults = null;
    const elements = deps.getElements();
    if (elements.measurementSpeakerAlignResults) elements.measurementSpeakerAlignResults.innerHTML = '';
}


function getHybridWizardState() {
    if (!deps.getState().measurement.hybridWizard || typeof deps.getState().measurement.hybridWizard !== 'object') {
        deps.getState().measurement.hybridWizard = {
            open: false, running: false, jobId: '', stepIndex: 0, mode: 'stereo', scope: null,
            sequence: [], captures: [], groupProfiles: {}, status: '', phase: '', cancelRequested: false, quality: null, profile: null,
        };
    }
    return deps.getState().measurement.hybridWizard;
}


// A series measures every routed group at shared microphone positions.
function isHybridSeries(wizard) {
    return wizard.scope?.series === true;
}


function getCurrentOutputModeName() {
    const catalog = deps.getState().outputSystem?.catalog;
    const topology = catalog?.modes?.[catalog.active_mode]?.topology;
    return { mono: 'subwoofer-2.1', 'dual-mono': 'subwoofer-2.2', stereo: 'subwoofer-2.2-stereo' }[topology?.sub_mode] || 'stereo';
}


// What the next Advanced run measures: the selected area's speakers (a pair or
// one channel) or subwoofers, derived from the area and the active topology.
function getCurrentHybridScope() {
    const catalog = deps.getState().outputSystem?.catalog;
    const topology = catalog?.modes?.[catalog.active_mode]?.topology;
    const area = deps.measurementArea();
    return area ? HybridMeasurement.buildScope(area, topology) : HybridMeasurement.normalizeScope(getCurrentOutputModeName());
}


// One scope per routed speaker group of the Surround Global area, in the group
// row's order; null where there is no group choice (a series needs two).
function getCurrentHybridGroupScopes() {
    const choice = deps.measurementGroupChoice();
    if (!choice || choice.options.length < 2) return null;
    const catalog = deps.getState().outputSystem?.catalog;
    const topology = catalog?.modes?.[catalog.active_mode]?.topology;
    const scopes = choice.options
        .map(option => deps.measurementAreaForGroup(option.id))
        .filter(Boolean)
        .map(area => HybridMeasurement.buildScope(area, topology));
    return scopes.length > 1 ? scopes : null;
}


// The scope id the open run has to match: a series follows the routed groups,
// a single run the selected area. Empty when a series has no groups to follow.
function getLiveHybridScopeId(series) {
    if (!series) return getCurrentHybridScope().id;
    const liveScopes = getCurrentHybridGroupScopes();
    return liveScopes ? HybridMeasurement.buildSeriesScope(liveScopes).id : '';
}


// Regular selected-group sequence, or all groups ordered by microphone position.
function buildHybridWizardSequence(series) {
    const scopes = series ? getCurrentHybridGroupScopes() : null;
    return scopes
        ? HybridMeasurement.buildSeriesSequence(scopes)
        : HybridMeasurement.buildSequence(getCurrentHybridScope());
}


function hybridReadyStatus() {
    return deps.measurementModeReady() ? 'Ready for the first measurement.' : 'Complete Measurement Setup before starting.';
}


function openHybridMeasurementWizard() {
    const wizard = getHybridWizardState();
    const area = deps.measurementArea();
    if (!wizard.running && (area?.available === false || area?.importable === false)) {
        deps.showToast('Advanced builds a filter model; select a filter bank to use it.', 'warning');
        return;
    }
    if (wizard.running) {
        wizard.open = true;
        deps.getElements().measurementHybridPanel?.classList.remove('hidden');
        renderHybridMeasurementWizard();
        deps.renderMeasurementPanel();
        window.FXRouteModal?.open(deps.getElements().measurementHybridPanel, {
            opener: deps.getElements().measurementSweepToggleBtn,
            initialFocus: deps.getElements().measurementHybridPrimaryBtn,
            onEscape: () => { void closeHybridMeasurementWizard(); },
        });
        return;
    }
    const sequence = buildHybridWizardSequence(false);
    Object.assign(wizard, {
        open: true,
        running: false,
        jobId: '',
        stepIndex: 0,
        mode: sequence.mode,
        scope: sequence.scope,
        sequence: sequence.steps,
        captures: [],
        groupProfiles: {},
        status: hybridReadyStatus(),
        phase: '',
        cancelRequested: false,
        quality: null,
        profile: null,
    });
    deps.getElements().measurementHybridPanel?.classList.remove('hidden');
    renderHybridMeasurementWizard();
    window.FXRouteModal?.open(deps.getElements().measurementHybridPanel, {
        opener: deps.getElements().measurementSweepToggleBtn,
        initialFocus: deps.getElements().measurementHybridPrimaryBtn,
        onEscape: () => { void closeHybridMeasurementWizard(); },
    });
}


async function closeHybridMeasurementWizard() {
    const wizard = getHybridWizardState();
    const wasRunning = wizard.running;
    if (wasRunning) await cancelHybridWizardMeasurement();
    wizard.open = false;
    if (!wasRunning) {
        wizard.running = false;
        wizard.jobId = '';
        deps.getState().measurement.activeJobId = '';
        deps.getState().measurement.activeMeasurementKind = '';
    }
    deps.renderMeasurementPanelDefensively('hybrid wizard close');
    deps.getElements().measurementHybridPanel?.classList.add('hidden');
    window.FXRouteModal?.close(deps.getElements().measurementHybridPanel);
}


// The room view places speakers per group; pair labels follow the group.
const HYBRID_ROOM_GROUPS = ['front', 'center', 'side', 'rear'];
const HYBRID_PAIR_LABELS = {
    front: { left: 'L', right: 'R' },
    side: { left: 'SL', right: 'SR' },
    rear: { left: 'RL', right: 'RR' },
};


function renderHybridRoomDiagram(step = {}, scopeOrMode = 'stereo', complete = false) {
    const panel = deps.getElements().measurementHybridPanel;
    if (!panel) return;
    panel.querySelectorAll('[data-hybrid-position]').forEach(node => {
        node.classList.toggle('is-target', node.getAttribute('data-hybrid-position') === step.position);
    });
    const scope = HybridMeasurement.normalizeScope(scopeOrMode);
    const diagram = HybridMeasurement.getDiagramState(scope, step.channel, complete);
    const pairLabels = HYBRID_PAIR_LABELS[diagram.group] || HYBRID_PAIR_LABELS.front;
    // The room view shows a speaker pair, one speaker, or only the subwoofers,
    // placed for the speaker group (front wall, side walls, rear wall).
    const room = panel.querySelector('.hybrid-room-view');
    room?.setAttribute('data-hybrid-layout', diagram.layout);
    room?.setAttribute('data-hybrid-group', HYBRID_ROOM_GROUPS.includes(diagram.group) ? diagram.group : 'front');
    panel.querySelectorAll('.hybrid-speaker[data-hybrid-speaker]').forEach(node => {
        const speaker = node.getAttribute('data-hybrid-speaker');
        node.classList.toggle('is-active', !!diagram.speakers[speaker]);
        if (speaker === 'stereo') node.textContent = (scope.speaker || 'C').charAt(0).toUpperCase();
        else if (pairLabels[speaker]) node.textContent = pairLabels[speaker];
    });
    panel.querySelectorAll('[data-hybrid-sub]').forEach(node => {
        const branch = node.getAttribute('data-hybrid-sub');
        const sub = diagram.subs[branch] || {};
        node.classList.toggle('hidden', !sub.visible);
        node.classList.toggle('is-active', !!sub.active);
        node.classList.toggle('is-single', !!sub.single);
        node.textContent = sub.label || 'SUB';
    });
}


// The gated direct lower limit of a model; a response without a gate has none.
function formatHybridLowerLimit(profile, channel) {
    const limitHz = profile.models[channel].modelBlend.gatedDirectLowerLimitHz;
    return Number.isFinite(limitHz) ? `${Math.round(limitHz)} Hz` : 'unavailable';
}


// Collapsed numbers of a pair scope's L/R check (a single channel has none).
function buildHybridDetailsHtml(scope, profile) {
    if (scope.channels.length !== 2) return '';
    const integration = profile.integration;
    const stereoPrediction = scope.mode === 'stereo';
    return `
        <details class="hybrid-advanced-details">
            <summary>Advanced details</summary>
            <p>${stereoPrediction
                ? 'The L/R complex sum was predicted from the main-position captures; no separate integration sweep was performed.'
                : `Consistency status: ${deps.escapeHtml(integration.status)} · band ${integration.validationBandHz.join('–')} Hz · magnitude ${Number.isFinite(integration.magnitudeRmsErrorDb) ? `${integration.magnitudeRmsErrorDb.toFixed(1)} dB` : 'unavailable'} · phase ${Number.isFinite(integration.phaseRmsErrorDeg) ? `${integration.phaseRmsErrorDeg.toFixed(1)}°` : 'unavailable'} · complex residual ${Number.isFinite(integration.complexResidualRms) ? integration.complexResidualRms.toFixed(2) : 'unavailable'} · ${integration.comparedPoints} points`}</p>
            ${stereoPrediction ? '' : `<p>${deps.escapeHtml(integration.limitation)}</p>`}
        </details>`;
}


// Result lines of one finished scope (a group of a series, or the single run).
function buildHybridSummaryHtml(scope, profile) {
    const pair = scope.channels.length === 2;
    const items = [
        scope.direct ? (pair ? 'Left and right speaker response captured' : `${deps.escapeHtml(scope.speaker)} response captured`) : null,
        'Main listening position measured',
        pair ? 'Left and right listening positions measured' : 'Listening positions measured',
        scope.direct ? 'Speaker and room measurements combined' : 'Listening-position measurements combined',
        !pair ? null : (scope.mode === 'stereo' ? 'Left and right responses compared' : 'L/R response and subwoofer routing checked'),
        !scope.direct ? null : (pair
            ? `<strong>Gated direct lower limit:</strong><br>Left: ${formatHybridLowerLimit(profile, 'left')}<br>Right: ${formatHybridLowerLimit(profile, 'right')}`
            : `<strong>Gated direct lower limit:</strong><br>${formatHybridLowerLimit(profile, 'stereo')}`),
    ].filter(Boolean);
    return items.map(item => `<div class="hybrid-summary-item">${item}</div>`).join('') + buildHybridDetailsHtml(scope, profile);
}


// One short entry per group (a series has too many groups for the full
// lines); the numbers of its L/R check stay one click away.
function buildHybridSeriesSummaryHtml(parts) {
    return parts.map(({ scope, profile }) => {
        const pair = scope.channels.length === 2;
        const lines = [
            !scope.direct ? null : (pair
                ? `Gated direct lower limit: Left ${formatHybridLowerLimit(profile, 'left')} · Right ${formatHybridLowerLimit(profile, 'right')}`
                : `Gated direct lower limit: ${formatHybridLowerLimit(profile, 'stereo')}`),
            !pair ? null : (scope.mode === 'stereo'
                ? 'Left and right responses compared'
                : `L/R response and subwoofer routing checked (${deps.escapeHtml(profile.integration.status)})`),
        ].filter(Boolean);
        return `<div class="hybrid-summary-item"><strong>${deps.escapeHtml(scope.label)}</strong>`
            + lines.map(line => `<br>${line}`).join('') + buildHybridDetailsHtml(scope, profile) + '</div>';
    }).join('');
}


// Speaker choice of the open dialog: each routed group, or all of them in turn.
// It is the sweep menu's speaker selection; only "All" belongs to the wizard.
function isHybridScopeSelectable(wizard) {
    return !wizard.running && !wizard.profile && wizard.stepIndex === 0 && wizard.captures.length === 0
        && !deps.getState().measurement.startInFlight;
}


function renderHybridScopeRow(wizard) {
    const row = deps.getElements().measurementHybridScopeRow;
    if (!row) return;
    const choice = deps.measurementGroupChoice();
    const groupChips = choice ? choice.options : [];
    const chips = groupChips.length > 1
        ? [...groupChips, { id: 'all', short: 'All', label: 'All speakers, one after another' }]
        : [];
    window.FXRouteUiHelpers.renderChipRow(row, chips, {
        attribute: 'data-hybrid-scope',
        selected: isHybridSeries(wizard) ? 'all' : (choice ? choice.selected : ''),
        disabled: !isHybridScopeSelectable(wizard),
    });
}


function selectHybridWizardScope(selection) {
    const wizard = getHybridWizardState();
    if (!selection || !isHybridScopeSelectable(wizard)) return;
    const series = selection === 'all';
    if (!series) deps.selectMeasurementGroup(selection);
    const sequence = buildHybridWizardSequence(series);
    Object.assign(wizard, {
        mode: sequence.mode,
        scope: sequence.scope,
        sequence: sequence.steps,
        stepIndex: 0,
        captures: [],
        groupProfiles: {},
        quality: null,
        profile: null,
        status: hybridReadyStatus(),
    });
    renderHybridMeasurementWizard();
}


// Header line of the dialog: a series names the group of the current step and
// how far it is; a single run names its measuring area or output mode.
function buildHybridHeaderText(scope, stepScope, complete) {
    if (!scope.series) return scope.wholeSystem ? `${scope.label} output mode` : `${scope.label} · measuring area`;
    if (complete) return `${scope.label} · measuring area`;
    const groupNumber = scope.scopes.indexOf(stepScope) + 1;
    return `${stepScope.label} · measuring area · ${groupNumber} / ${scope.scopes.length}`;
}


function renderHybridMeasurementWizard() {
    const wizard = getHybridWizardState();
    if (!wizard.open || !deps.getElements().measurementHybridPanel) return;
    const complete = !!wizard.profile;
    const current = wizard.sequence[wizard.stepIndex] || null;
    const scope = HybridMeasurement.normalizeScope(wizard.scope || wizard.mode);
    // Header and room view follow the active speaker group at this position.
    const stepScope = HybridMeasurement.scopeForStep(scope, current || wizard.sequence[wizard.sequence.length - 1]);
    if (deps.getElements().measurementHybridMode) {
        deps.getElements().measurementHybridMode.textContent = buildHybridHeaderText(scope, stepScope, complete);
    }
    if (deps.getElements().measurementHybridProgress) {
        const done = complete ? wizard.sequence.length : wizard.stepIndex;
        deps.getElements().measurementHybridProgress.textContent = `${done} / ${wizard.sequence.length}`;
    }
    if (deps.getElements().measurementHybridTitle) deps.getElements().measurementHybridTitle.textContent = complete ? 'Measurement complete' : (current?.title || 'Advanced');
    if (deps.getElements().measurementHybridInstruction) {
        const instruction = complete ? 'All measurements were completed successfully.' : (current?.instruction || '');
        deps.getElements().measurementHybridInstruction.textContent = instruction;
        deps.getElements().measurementHybridInstruction.classList.toggle('is-move', !!current?.move && !complete);
        deps.getElements().measurementHybridInstruction.classList.toggle('is-complete', complete);
    }
    if (deps.getElements().measurementHybridActive) deps.getElements().measurementHybridActive.textContent = complete ? '' : `Measuring: ${current?.active || ''}`;
    if (deps.getElements().measurementHybridStatus) {
        deps.getElements().measurementHybridStatus.textContent = wizard.status || '';
        deps.getElements().measurementHybridStatus.dataset.level = wizard.quality?.level || '';
        deps.getElements().measurementHybridStatus.classList.toggle('is-busy', wizard.running && !!wizard.phase);
    }
    if (deps.getElements().measurementHybridPrimaryBtn) {
        const finished = scope.series ? 'Show results' : 'Open filter generator';
        deps.getElements().measurementHybridPrimaryBtn.textContent = complete ? finished : (wizard.running ? 'Cancel measurement' : (wizard.quality?.retry ? 'Repeat measurement' : 'Start measurement'));
        deps.getElements().measurementHybridPrimaryBtn.disabled = !wizard.running && !complete && !deps.measurementModeReady();
    }
    if (deps.getElements().measurementHybridBackBtn) deps.getElements().measurementHybridBackBtn.disabled = wizard.running || wizard.stepIndex === 0 || complete;
    if (deps.getElements().measurementHybridSummary) {
        deps.getElements().measurementHybridSummary.classList.toggle('hidden', !complete);
        if (complete) {
            deps.getElements().measurementHybridSummary.innerHTML = scope.series
                ? buildHybridSeriesSummaryHtml(wizard.profile.parts)
                : buildHybridSummaryHtml(scope, wizard.profile);
        }
    }
    renderHybridScopeRow(wizard);
    renderHybridRoomDiagram(current || {}, stepScope, complete);
}


function buildHybridMeasurementForm(step) {
    deps.normalizeMeasurementInputChannelSelections();
    const formData = new FormData();
    const selectedInputId = deps.getState().measurement.selectedInputId;
    if (!selectedInputId) throw new Error('Select a measurement input first');
    formData.append('input_id', selectedInputId);
    formData.append('input_key', deps.getState().measurement.selectedInputKey || '');
    formData.append('channel', step.channel);
    formData.append('measurement_role', step.role);
    const areaBank = deps.measurementAreaBank();
    if (areaBank) formData.append('measurement_bank', areaBank);
    // A series step carries its own group; the speaker selection may differ.
    const areaGroup = step.group || deps.measurementAreaGroup();
    if (areaGroup) formData.append('measurement_group', areaGroup);
    formData.append('mic_input_channel', deps.getState().measurement.selectedMicInputChannel || '1');
    deps.appendMeasurementReferenceFields(formData);
    const calibrationFile = deps.getElements().measurementCalibrationFile?.files?.[0];
    if (calibrationFile) formData.append('calibration_file', calibrationFile);
    else if (deps.getState().measurement.selectedCalibrationRef) formData.append('calibration_ref', deps.getState().measurement.selectedCalibrationRef);
    return formData;
}


async function runHybridWizardStep(step) {
    const wizard = getHybridWizardState();
    wizard.quality = null;
    wizard.phase = 'measuring';
    const stepScope = HybridMeasurement.scopeForStep(wizard.scope, step);
    const speakerLabel = () => (stepScope.channels.length === 1 && step.role !== 'integration'
        ? step.active.toLowerCase() : ui.hybridSpeakerName(step.channel));
    wizard.status = `Measuring ${speakerLabel()}…`;
    renderHybridMeasurementWizard();
    const response = await api.startMeasurement(buildHybridMeasurementForm(step));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to start advanced measurement'));
    const jobId = String(data.job?.id || '');
    if (!jobId) throw new Error('Advanced measurement did not return a job id');
    wizard.jobId = jobId;
    deps.getState().measurement.activeJobId = jobId;
    deps.getState().measurement.activeMeasurementKind = 'hybrid';
    const pollGeneration = Number(deps.getState().measurement?.jobGeneration || 0);
    if (wizard.cancelRequested) {
        await api.cancelMeasurementJob(jobId).catch(() => null);
    }

    for (let attempt = 0; attempt < 360; attempt += 1) {
        if (!isCurrentHybridPoll(jobId, pollGeneration)) return false;
        let poll;
        let payload = {};
        try {
            poll = await api.pollMeasurementJob(jobId);
            payload = await poll.json().catch(() => ({}));
        } catch (_pollError) {
            if (!isCurrentHybridPoll(jobId, pollGeneration)) return false;
            poll = { ok: false, status: 0 };
        }
        if (!isCurrentHybridPoll(jobId, pollGeneration)) return false;
        if (!poll.ok) {
            if (poll.status === 404 || poll.status === 410) {
                throw new Error(deps.formatTransitionErrorDetail(payload.detail, 'Failed to fetch advanced measurement'));
            }
            // Transient network/JSON blip: retry a few polls before failing
            // the step (a completed server-side measurement must survive).
            wizard.pollErrors = (wizard.pollErrors || 0) + 1;
            if (wizard.pollErrors >= 5) {
                throw new Error(deps.formatTransitionErrorDetail(payload.detail, 'Failed to fetch advanced measurement'));
            }
            await deps.sleep(800);
            continue;
        }
        wizard.pollErrors = 0;
        const job = payload.job || {};
        const status = deps.getMeasurementJobStatus(job);
        const processing = String(job.message || '').toLowerCase().startsWith('processing');
        wizard.phase = processing ? 'processing' : (wizard.cancelRequested ? 'cancelling' : 'measuring');
        wizard.status = wizard.cancelRequested
            ? 'Cancelling measurement…'
            : (processing ? `Processing ${step.channel === 'stereo' ? 'measurement' : step.channel}…` : `Measuring ${speakerLabel()}…`);
        renderHybridMeasurementWizard();
        if (ui.MEASUREMENT_JOB_SUCCESS_STATES.has(status)) {
            if (wizard.cancelRequested) return false;
            const measurement = deps.normalizeMeasurementEntry(deps.getMeasurementJobResultMeasurement(job), 0);
            if (step.role === 'direct' && !HybridMeasurement.isUsableDirectMeasurement(measurement)) {
                wizard.quality = { level: 'error', retry: true };
                wizard.status = measurement.analysis?.direct_response?.retry_reason
                    || 'The direct speaker response could not be measured reliably. Check that the microphone is about 1 m from the speaker and not close to a wall or other reflecting surface, then repeat the measurement.';
                return false;
            }
            if (step.role === 'direct') {
                const positionCheck = HybridMeasurement.validateDirectMicrophonePosition(
                    measurement,
                    HybridMeasurement.groupCaptures(wizard.captures, step.group),
                    step.channel,
                );
                measurement.analysis.direct_response.position_check = positionCheck;
                if (positionCheck.available && !positionCheck.plausible) {
                    wizard.quality = { level: 'error', retry: true };
                    wizard.status = positionCheck.reason;
                    return false;
                }
            }
            wizard.captures = wizard.captures.filter(item => item.stepId !== step.id);
            wizard.captures.push({ stepId: step.id, role: step.role, position: step.position, channel: step.channel, group: step.group || '', measurement });
            if (step.role === 'integration' && isHybridSeries(wizard)) {
                HybridMeasurement.validateIntegration(HybridMeasurement.groupCaptures(wizard.captures, step.group), stepScope);
            }
            wizard.quality = { level: 'ok', retry: false };
            wizard.stepIndex += 1;
            return true;
        }
        if (ui.MEASUREMENT_JOB_FAILED_STATES.has(status)) throw new Error(deps.formatTransitionErrorDetail(job.error?.detail, job.message || 'Measurement failed'));
        if (ui.MEASUREMENT_JOB_CANCELLED_STATES.has(status)) return false;
        await deps.sleep(800);
    }
    throw new Error('Measurement timed out.');
}


async function cancelHybridWizardMeasurement() {
    const wizard = getHybridWizardState();
    if (!wizard.running || wizard.cancelRequested) return;
    wizard.cancelRequested = true;
    wizard.phase = 'cancelling';
    wizard.status = 'Cancelling measurement…';
    renderHybridMeasurementWizard();
    if (wizard.jobId) {
        await api.cancelMeasurementJob(wizard.jobId).catch(() => null);
    }
}


// Profile a group only after its final scheduled capture, not a group boundary.
// Any non-integration model failure restarts that group's nearfield capture.
function buildFinishedHybridGroupProfile(wizard) {
    const last = wizard.sequence[wizard.stepIndex - 1];
    if (!last || wizard.sequence.slice(wizard.stepIndex).some(step => step.group === last.group)) return;
    try {
        wizard.groupProfiles[last.group] = HybridMeasurement.buildProfile(
            HybridMeasurement.groupCaptures(wizard.captures, last.group),
            HybridMeasurement.scopeForStep(wizard.scope, last));
    } catch (error) {
        error.retryGroup = last.group;
        if (error.retryRole !== 'integration') {
            wizard.stepIndex = Math.max(0, wizard.sequence.findIndex(step => step.group === last.group));
        }
        throw error;
    }
}


async function runHybridWizardSweep() {
    const wizard = getHybridWizardState();
    if (wizard.running || wizard.profile) return;
    const step = wizard.sequence[wizard.stepIndex];
    if (!step) return;
    // A series compares the routed groups, not the speaker selection.
    const series = isHybridSeries(wizard);
    if (getLiveHybridScopeId(series) !== wizard.scope?.id) {
        wizard.status = 'Measuring area or output mode changed. Close and restart the wizard so measurements are not mixed.';
        wizard.quality = { level: 'error', retry: false };
        renderHybridMeasurementWizard();
        return;
    }
    const seriesEnd = HybridMeasurement.getPositionSeriesEnd(wizard.sequence, wizard.stepIndex);
    wizard.running = true;
    wizard.cancelRequested = false;
    wizard.quality = null;
    wizard.phase = 'preparing';
    wizard.status = 'Preparing measurement…';
    renderHybridMeasurementWizard();
    deps.renderMeasurementPanel();
    try {
        await deps.flushSubwooferSettingsBeforeMeasurement();
        while (wizard.stepIndex <= seriesEnd && !wizard.cancelRequested) {
            const current = wizard.sequence[wizard.stepIndex];
            const completed = await runHybridWizardStep(current);
            wizard.jobId = '';
            deps.getState().measurement.activeJobId = '';
            if (!completed || wizard.cancelRequested) break;
            if (series) buildFinishedHybridGroupProfile(wizard);
            if (wizard.stepIndex <= seriesEnd) {
                const next = wizard.sequence[wizard.stepIndex];
                wizard.phase = 'preparing';
                wizard.status = `Preparing ${next.channel}…`;
                renderHybridMeasurementWizard();
                await deps.sleep(400);
            }
        }
        if (wizard.cancelRequested) {
            wizard.status = 'Measurement cancelled. The microphone position is ready to measure again.';
            wizard.quality = null;
        } else {
            if (wizard.stepIndex >= wizard.sequence.length) {
                wizard.profile = series
                    ? HybridMeasurement.buildSeriesProfile(wizard.scope, wizard.groupProfiles)
                    : HybridMeasurement.buildProfile(wizard.captures, wizard.scope);
                wizard.status = 'All measurements were completed successfully.';
            } else if (!wizard.quality?.retry) {
                wizard.status = 'Position measured successfully. Move the microphone as shown for the next measurement.';
            }
        }
    } catch (error) {
        if (error.retryRole === 'integration') {
            const integrationIndex = wizard.sequence.findIndex(step => step.role === 'integration'
                && (step.group || '') === (error.retryGroup || ''));
            if (integrationIndex >= 0) wizard.stepIndex = integrationIndex;
        }
        wizard.status = wizard.cancelRequested
            ? 'Measurement cancelled. The microphone position is ready to measure again.'
            : (error.message || 'Advanced measurement failed.');
        wizard.quality = wizard.cancelRequested ? null : { level: 'error', retry: true };
    } finally {
        wizard.running = false;
        wizard.phase = '';
        wizard.cancelRequested = false;
        wizard.jobId = '';
        deps.getState().measurement.activeJobId = '';
        deps.getState().measurement.activeMeasurementKind = '';
        renderHybridMeasurementWizard();
        deps.renderMeasurementPanelDefensively('hybrid wizard sweep completion');
    }
}


// "L"/"R" of a pair's side; the single channel of a mono scope has none.
function hybridSideLetter(scope, side) {
    if (scope.channels.length !== 2) return '';
    return side === 'left' ? 'L' : 'R';
}


// One correction-model entry of a finished scope (one side, or the single
// channel).  A series qualifies the id with the group: all entries are built
// in the same millisecond and one save rejects duplicate ids.
function buildHybridModelEntry(profile, scope, side, series = false) {
    const letter = hybridSideLetter(scope, side);
    const model = profile.models[side];
    const timing = model.timingMeasurement;
    const modelPoints = Array.isArray(model.points) ? model.points : [];
    const modelMinDb = modelPoints.length ? Math.min(...modelPoints.map((point) => Number(point[1]) || 0)) : 0;
    const modelMaxDb = modelPoints.length ? Math.max(...modelPoints.map((point) => Number(point[1]) || 0)) : 0;
    const modelMinHz = modelPoints.length ? Number(modelPoints[0][0]) || 20 : 20;
    const modelMaxHz = modelPoints.length ? Number(modelPoints[modelPoints.length - 1][0]) || 20000 : 20000;
    const modelSummary = { trace_count: modelPoints.length ? 1 : 0, point_count: modelPoints.length, min_db: modelMinDb, max_db: modelMaxDb, min_hz: modelMinHz, max_hz: modelMaxHz };
    return deps.normalizeMeasurementEntry({
        id: `hybrid-${series && scope.group ? `${scope.group}-` : ''}${side}-${Date.now()}`,
        name: letter ? `Advanced ${scope.label} ${letter}` : `Advanced ${scope.label}`,
        created_at: new Date().toISOString(),
        // A single channel is taken like the left side of a mono area.
        channel: letter ? side : 'left',
        measurement_kind: 'hybrid-correction-model-v1',
        measurement_role: 'hybrid-model',
        input_device: timing.input_device,
        input_channels: timing.input_channels,
        calibration: timing.calibration,
        audio_output_context: timing.audio_output_context,
        // The model keeps the area and processing its captures were taken in.
        measurement_target: timing.measurement_target,
        traces: [{ kind: 'hybrid-response', role: 'trusted', label: `Hybrid ${side}`, points: model.points }],
        summary: modelSummary,
        review_summary: modelSummary,
        analysis: {
            ...timing.analysis,
            hybrid_constraints: model.constraints,
            hybrid_model_blend: model.modelBlend,
            hybrid_source_roles: ['direct', 'mlp', 'secondary', 'integration'].filter(role => (role !== 'direct' || scope.direct) && (role !== 'integration' || scope.integration)),
            integration_validation: profile.integration,
        },
    }, 0);
}


// The model entries become the pending result of the normal flow.  An unsaved
// Auto Sub result is dropped like a Speaker Align result drops it: it would
// take precedence in the graph and in "Save current" over these entries.  A
// name typed into the field is kept; `name` is only the proposal.
function adoptHybridModelEntries(entries, name) {
    const measurement = deps.getState().measurement;
    measurement.autoSubMeasurements = [];
    measurement.pendingRepeatMeasurements = entries;
    measurement.currentMeasurement = entries[0];
    measurement.currentMeasurementName = ui.getTypedMeasurementName(measurement) || name;
    measurement.currentMeasurementSaved = false;
}


// A series result is the correction model of every group.  Like an LR Repeat it
// stays in the current assistant for review and one common save: the convolver
// assistant takes a single L/R pair, not the models of several groups.
function showHybridSeriesResults(profile) {
    const measurement = deps.getState().measurement;
    const entries = profile.parts.flatMap(part => part.scope.channels.map((side) => {
        const entry = buildHybridModelEntry(part.profile, part.scope, side, true);
        // Saved as "<name> · <group> · L/R"; the group keeps entries of equal
        // sides apart.
        const letter = hybridSideLetter(part.scope, side);
        entry.save_suffix = letter ? `${part.scope.label} · ${letter}` : part.scope.label;
        return entry;
    }));
    adoptHybridModelEntries(entries, 'Advanced');
    deps.setMeasurementGraphView('freq');
    measurement.statusText = 'Advanced series finished. Review all results and save them together.';
    void closeHybridMeasurementWizard();
    deps.renderMeasurementPanel();
}


function openHybridProfileInConvolver() {
    const wizard = getHybridWizardState();
    if (!wizard.profile) return;
    if (wizard.profile.series) {
        showHybridSeriesResults(wizard.profile);
        return;
    }
    const scope = HybridMeasurement.normalizeScope(wizard.scope || wizard.mode);
    const entries = scope.channels.map(side => buildHybridModelEntry(wizard.profile, scope, side));
    adoptHybridModelEntries(entries, `Advanced ${scope.label}`);
    deps.setMeasurementAssistMode('convolver');
    void closeHybridMeasurementWizard();
    deps.renderMeasurementPanel();
    deps.getElements().measurementConvolverPanel?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}


function setupHybridMeasurementWizard() {
    if (!deps.getElements().measurementHybridPanel || !deps.getElements().measurementHybridOpenBtn) return;
    deps.getElements().measurementHybridOpenBtn.addEventListener('click', openHybridMeasurementWizard);
    deps.getElements().measurementHybridCloseBtn?.addEventListener('click', () => { void closeHybridMeasurementWizard(); });
    deps.getElements().measurementHybridPrimaryBtn?.addEventListener('click', () => {
        if (getHybridWizardState().running) void cancelHybridWizardMeasurement();
        else if (getHybridWizardState().profile) openHybridProfileInConvolver();
        else void runHybridWizardSweep();
    });
    deps.getElements().measurementHybridScopeRow?.addEventListener('click', (event) => {
        const button = event.target.closest?.('[data-hybrid-scope]');
        if (button) selectHybridWizardScope(button.getAttribute('data-hybrid-scope') || '');
    });
    deps.getElements().measurementHybridBackBtn?.addEventListener('click', () => {
        const wizard = getHybridWizardState();
        if (!wizard.running && wizard.stepIndex > 0) {
            wizard.stepIndex = HybridMeasurement.getPreviousPositionIndex(wizard.sequence, wizard.stepIndex);
            wizard.quality = null;
            wizard.status = 'Previous microphone position selected. Existing results will be replaced when measured again.';
            renderHybridMeasurementWizard();
        }
    });
    deps.getElements().measurementHybridPanel.querySelector('.manage-overlay-backdrop')?.addEventListener('click', () => {
        if (!getHybridWizardState().running) void closeHybridMeasurementWizard();
    });
}


    window.FXRouteMeasurementFlows = {
        init,
        syncSubwooferControlsDuringAutoSub,
        syncAutoSubButton,
        startAutoSubOptimize,
        recoverAutoSubJob,
        cancelAutoSubOptimize,
        pollAutoSubJob,
        handleAutoSubResult,
        syncSpeakerAlignButton,
        startSpeakerAlign,
        cancelSpeakerAlign,
        pollSpeakerAlignJob,
        handleSpeakerAlignResult,
        clearSpeakerAlignResult,
        getHybridWizardState,
        getCurrentOutputModeName,
        getCurrentHybridScope,
        getCurrentHybridGroupScopes,
        selectHybridWizardScope,
        openHybridMeasurementWizard,
        closeHybridMeasurementWizard,
        renderHybridRoomDiagram,
        renderHybridMeasurementWizard,
        buildHybridMeasurementForm,
        runHybridWizardStep,
        cancelHybridWizardMeasurement,
        runHybridWizardSweep,
        openHybridProfileInConvolver,
        setupHybridMeasurementWizard,
    };
})();
