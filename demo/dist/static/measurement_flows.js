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
        getMeasurementJobStatus: () => 'unknown',
        normalizeMeasurementEntry: (m) => m,
        getMeasurementJobResultMeasurement: () => null,
        setMeasurementAssistMode: () => {},
        escapeHtml: (v) => String(v == null ? '' : v),
        sleep: async () => {},
    };
    let api = {};

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


function syncAutoSubButton() {
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


async function startAutoSubOptimize() {
    const measurementState = deps.getState().measurement || {};
    if (measurementState.autoSubInFlight || measurementState.startInFlight || measurementState.activeJobId) return;

    const inputId = measurementState.selectedInputId;
    if (!inputId) {
        deps.showToast('No capture input selected', 'error');
        return;
    }
    if (!deps.measurementModeReady()) {
        deps.showToast('No usable host capture source is available', 'error');
        return;
    }

    await deps.flushSubwooferSettingsBeforeMeasurement();

    measurementState.autoSubInFlight = true;
    measurementState.startInFlight = true;
    measurementState.autoSubCancelRequested = false;
    measurementState.activeMeasurementKind = 'auto_sub';
    measurementState.autoSubJobId = '';
    measurementState.autoSubResult = null;
    measurementState.autoSubMeasurements = [];
    syncSubwooferControlsDuringAutoSub();
    deps.renderMeasurementPanel();

    try {
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

        measurementState.statusText = 'Auto Sub Optimize: starting…';
        deps.renderMeasurementPanel();
        await deps.postRuntimeDebugSnapshot('ui-before-auto-sub-start', {});

        const resp = await api.startAutoSubOptimize(formData);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to start Auto Sub Optimize'));
        const job = data.job || {};
        measurementState.autoSubJobId = String(job.id || '');
        measurementState.statusText = job.message || 'Auto Sub Optimize: queued';
        deps.renderMeasurementPanel();
        if (measurementState.autoSubCancelRequested) await cancelAutoSubOptimize();
        if (!measurementState.autoSubJobId) throw new Error('Auto Sub Optimize did not return a job id');
        await pollAutoSubJob(measurementState.autoSubJobId);
    } catch (error) {
        console.error('startAutoSubOptimize failed', error);
        measurementState.statusText = error.message || 'Auto Sub Optimize failed';
        deps.showToast(measurementState.statusText, 'error');
    } finally {
        measurementState.autoSubInFlight = false;
        measurementState.startInFlight = false;
        measurementState.activeMeasurementKind = '';
        measurementState.autoSubJobId = '';
        measurementState.autoSubCancelRequested = false;
        // Refresh audio outputs to pick up new sub_alignment_ms
        deps.fetchAudioOutputOverview().catch(() => {});
        deps.renderMeasurementPanel();
    }
}


async function cancelAutoSubOptimize() {
    const jobId = String(deps.getState().measurement.autoSubJobId || '');
    if (!jobId) {
        if (deps.getState().measurement.autoSubInFlight) {
            deps.getState().measurement.autoSubCancelRequested = true;
            deps.getState().measurement.statusText = 'Cancelling Auto Sub Optimize…';
            deps.renderMeasurementPanel();
        }
        return;
    }
    deps.getState().measurement.statusText = 'Cancelling Auto Sub Optimize…';
    deps.renderMeasurementPanel();
    try {
        const resp = await api.cancelAutoSubJob(jobId);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to cancel Auto Sub Optimize'));
        deps.getState().measurement.statusText = String(data.job?.message || 'Cancelling Auto Sub Optimize…');
    } catch (error) {
        console.error('cancelAutoSubOptimize failed', error);
        deps.getState().measurement.statusText = error.message || 'Failed to cancel Auto Sub Optimize';
        deps.showToast(deps.getState().measurement.statusText, 'error');
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
    const statusEl = deps.getElements().measurementAutoSubStatus;
    const startedAt = Date.now();
    const longRunningAfterMs = 10 * 60 * 1000;
    const maxRunningMs = 30 * 60 * 1000;
    let consecutiveErrors = 0;
    const maxConsecutiveErrors = 40;
    while (true) {
        if (!isCurrentAutoSubPoll(jobId, pollGeneration)) return;
        await deps.sleep(500);
        if (Date.now() - startedAt >= maxRunningMs) {
            measurementState.statusText = 'Auto Sub Optimize timed out while waiting for the job';
            deps.showToast(measurementState.statusText, 'error');
            return;
        }
        try {
            const resp = await api.pollAutoSubJob(jobId);
            const data = await resp.json().catch(() => ({}));
            if (!isCurrentAutoSubPoll(jobId, pollGeneration)) return;
            if (!resp.ok) {
                if (resp.status === 404 || resp.status === 410) {
                    measurementState.statusText = 'Auto Sub Optimize job is no longer available';
                    deps.showToast(measurementState.statusText, 'error');
                    return;
                }
                throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to poll Auto Sub job'));
            }
            consecutiveErrors = 0;
            const job = data.job || {};
            const status = job.status || 'unknown';
            const fineScan = job.fine_scan || {};
            const targetLabel = String(job.target_curve?.label || '');

            measurementState.statusText = job.message || 'Auto Sub Optimize: running';
            if (job.progress) {
                const cur = job.progress.current;
                const tot = job.progress.total;
                if (Number.isFinite(cur) && Number.isFinite(tot)) {
                    measurementState.statusText += ` (${cur}/${tot})`;
                }
            }
            if (Date.now() - startedAt >= longRunningAfterMs
                    && (status === 'queued' || status === 'preparing' || status === 'running' || status === 'cancelling')) {
                measurementState.statusText = `AutoSub still running… ${measurementState.statusText}`;
            }

            // Update inline status element
            if (statusEl) {
                if (job.progress) {
                    const progress = job.progress || {};
                    const stageLabels = {
                        fine: 'Fine-Scan',
                        coarse: 'Coarse',
                        sub1_coarse: 'Optimizing Sub 1',
                        sub2_coarse: 'Optimizing Sub 2',
                        left_sub: 'Optimizing Sub 1',
                        right_sub: 'Optimizing Sub 2',
                        combined_matrix: 'Combined Matrix',
                    };
                    const stageLabel = stageLabels[progress.stage] || String(progress.stage || 'Coarse');
                    const candidateCur = progress.candidate_current;
                    const candidateTot = progress.candidate_total;
                    const sweepCur = progress.sweep_current ?? progress.current;
                    const sweepTot = progress.sweep_total ?? progress.total;
                    if (Number.isFinite(candidateCur) && Number.isFinite(candidateTot)) {
                        const sweepText = (Number.isFinite(sweepCur) && Number.isFinite(sweepTot)) ? ` (${sweepCur}/${sweepTot} sweeps)` : '';
                        statusEl.textContent = `${stageLabel}: ${candidateCur}/${candidateTot} candidates${sweepText}${targetLabel ? ` · Target: ${targetLabel}` : ''}`;
                    } else if (Number.isFinite(sweepCur) && Number.isFinite(sweepTot)) {
                        statusEl.textContent = `${sweepCur}/${sweepTot} sweeps${targetLabel ? ` · Target: ${targetLabel}` : ''}`;
                    }
                }
            }

            // Live: push baseline measurement data to graph as soon as available
            if (job.baseline_measurement && !measurementState.autoSubMeasurements.length) {
                measurementState.autoSubMeasurements = [job.baseline_measurement];
                measurementState.currentMeasurementSaved = false;
            }

            if (status === 'completed' || status === 'failed' || status === 'cancelled') {
                if (statusEl) {
                    if (status === 'cancelled') {
                        statusEl.textContent = job.message || 'Auto Sub Optimize cancelled.';
                    } else if (status === 'failed') {
                        statusEl.textContent = '';
                    }
                }
                await handleAutoSubResult(job);
                return;
            }
        } catch (error) {
            if (!isCurrentAutoSubPoll(jobId, pollGeneration)) return;
            consecutiveErrors += 1;
            console.warn('pollAutoSubJob error', error);
            if (consecutiveErrors >= maxConsecutiveErrors) {
                measurementState.statusText = error?.message || 'Auto Sub Optimize polling failed';
                deps.showToast(measurementState.statusText, 'error');
                return;
            }
        }
        deps.renderMeasurementPanel();
    }
}


async function handleAutoSubResult(job) {
    const measurementState = deps.getState().measurement || {};
    const statusEl = deps.getElements().measurementAutoSubStatus;
    const result = job.result;
    if (job.status === 'cancelled') {
        measurementState.statusText = job.message || 'Auto Sub Optimize cancelled.';
        measurementState.autoSubResult = null;
        measurementState.autoSubMeasurements = [];
        syncSubwooferControlsDuringAutoSub();
        deps.showToast('Auto Sub Optimize cancelled', 'success');
        return;
    }
    if (job.status === 'failed') {
        measurementState.statusText = job.message || 'Auto Sub Optimize failed';
        measurementState.autoSubResult = null;
        measurementState.autoSubMeasurements = [];
        syncSubwooferControlsDuringAutoSub();
        deps.showToast(measurementState.statusText, 'error');
        return;
    }
    if (!result) {
        measurementState.statusText = 'Auto Sub Optimize completed with no result';
        measurementState.autoSubMeasurements = [];
        syncSubwooferControlsDuringAutoSub();
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
    measurementState.currentMeasurementName = 'AutoSub';
    const winner = result.winner || {};
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
        measurementState.statusText = `AutoSub ${modeLabel} applied: ${sub1Label} ${sub1Text} · ${sub2Label} ${sub2Text} · ${scorePart}`;

        if (statusEl) {
            const detailParts = [];
            const sub1Coarse = result.sub1_coarse_winner || result.left_winner || {};
            const sub2Coarse = result.sub2_coarse_winner || result.right_winner || {};
            if (Number.isFinite(sub1Coarse.delay_ms)) {
                const sub1Score = Number.isFinite(sub1Coarse.score_pct) ? ` (${sub1Coarse.score_pct.toFixed(1)} %)` : '';
                detailParts.push(`${sub1Label}: ${sub1Coarse.delay_ms.toFixed(2)} ms${sub1Score}`);
            }
            if (Number.isFinite(sub2Coarse.delay_ms)) {
                const sub2Score = Number.isFinite(sub2Coarse.score_pct) ? ` (${sub2Coarse.score_pct.toFixed(1)} %)` : '';
                detailParts.push(`${sub2Label}: ${sub2Coarse.delay_ms.toFixed(2)} ms${sub2Score}`);
            }
            if (
                Number.isFinite(result.derived_main_delay_ms)
                && Number.isFinite(result.derived_sub1_delay_ms)
                && Number.isFinite(result.derived_sub2_delay_ms)
            ) {
                detailParts.push(`Derived: Main ${result.derived_main_delay_ms.toFixed(2)} ms / ${sub1Label} ${result.derived_sub1_delay_ms.toFixed(2)} ms / ${sub2Label} ${result.derived_sub2_delay_ms.toFixed(2)} ms`);
            }
            statusEl.textContent = detailParts.join(' · ');
        }

        syncSubwooferControlsDuringAutoSub();
        deps.showToast(`Applied ${modeLabel}: ${sub1Label} ${appliedSub1 !== null ? appliedSub1.toFixed(2) : '?'} ms · ${sub2Label} ${appliedSub2 !== null ? appliedSub2.toFixed(2) : '?'} ms · ${scorePart}`, 'success');
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

    const mainParts = [];
    if (gateIncumbentKept) {
        mainParts.push(`AutoSub applied: ${appliedText} ms (was ${originalText} ms)`);
    } else if (wasApplied) {
        mainParts.push(`AutoSub applied: ${appliedText} ms (was ${originalText} ms)`);
    } else if (incumbentKept) {
        mainParts.push(`AutoSub kept current alignment: ${originalText} ms`);
    } else {
        mainParts.push(`AutoSub suggested: ${suggestedText} ms (was ${originalText} ms, not applied${notAppliedReason ? ` · ${notAppliedReason}` : ''})`);
    }
    const displayWinner = gateIncumbentKept && fineW && Number.isFinite(fineW.score_pct) ? fineW : winner;
    const displayScorePct = Number.isFinite(displayWinner.score_pct) ? displayWinner.score_pct : scorePct;
    const displayScorePctText = Number.isFinite(displayScorePct) ? displayScorePct.toFixed(1) : '?';
    const displayHasLR = Number.isFinite(displayWinner.score_L_pct) && Number.isFinite(displayWinner.score_R_pct);
    if (gateIncumbentKept) {
        mainParts.push(`Score ${displayScorePctText} %${displayHasLR ? ` · L ${displayWinner.score_L_pct.toFixed(1)} % / R ${displayWinner.score_R_pct.toFixed(1)} %` : ''}`);
        mainParts.push(`suggested ${suggestedText} ms (${Number.isFinite(displayWinner.score_pct) ? displayWinner.score_pct.toFixed(1) : '?'} %) kept after local-dip check`);
    } else if (hasWinnerLRScores) {
        mainParts.push(`Score ${scorePctText} % · L ${winner.score_L_pct.toFixed(1)} % / R ${winner.score_R_pct.toFixed(1)} %`);
    } else {
        mainParts.push(`Score ${scorePctText} %`);
    }
    if (gateIncumbentKept) {
        measurementState.statusText = mainParts.join(' · ');
    } else if (isWeak) {
        measurementState.statusText = `AutoSub result weak. Check with a normal 2.1 measurement. (${mainParts[0]}, Score ${scorePctText} %)`;
    } else {
        measurementState.statusText = mainParts.join(' · ');
    }

    if (statusEl) {
        const detailParts = [];
        if (fineScan.triggered && fineScan.status === 'completed') {
            const cDelay = Number.isFinite(coarseW.delay_ms) ? coarseW.delay_ms.toFixed(2) : '?';
            const cScore = Number.isFinite(coarseW.score_pct) ? coarseW.score_pct.toFixed(1) : '?';
            detailParts.push(`Coarse: ${cDelay} ms (${cScore} %)`);
            if (fineW) {
                const fDelay = Number.isFinite(fineW.delay_ms) ? fineW.delay_ms.toFixed(2) : '?';
                const fScore = Number.isFinite(fineW.score_pct) ? fineW.score_pct.toFixed(1) : '?';
                detailParts.push(`Fine checked: ${fDelay} ms (${fScore} %)`);
            }
        } else if (result.runner_up) {
            const rDelay = Number.isFinite(result.runner_up.delay_ms) ? result.runner_up.delay_ms.toFixed(2) : '?';
            const rScore = Number.isFinite(result.runner_up.score_pct) ? result.runner_up.score_pct.toFixed(1) : '?';
            detailParts.push(`Runner-up: ${rDelay} ms (${rScore} %)`);
        }
        if (detailParts.length > 0) {
            statusEl.textContent = detailParts.join(' · ');
        } else {
            statusEl.textContent = '';
        }
    }

    let toastText;
    if (gateIncumbentKept) {
        const gScore = Number.isFinite(displayWinner.score_pct) ? displayWinner.score_pct.toFixed(1) : '?';
        const gLR = Number.isFinite(displayWinner.score_L_pct) && Number.isFinite(displayWinner.score_R_pct)
            ? ` · L ${displayWinner.score_L_pct.toFixed(1)} % / R ${displayWinner.score_R_pct.toFixed(1)} %` : '';
        toastText = `Applied: ${appliedText} ms (was ${originalText} ms) · Score ${gScore} %${gLR} · suggested ${suggestedText} ms kept after local-dip check`;
    } else {
        const outcomeToastPart = wasApplied
            ? `Applied: ${appliedText} ms (was ${originalText} ms)`
            : incumbentKept
                ? `Kept current alignment: ${originalText} ms`
                : `Suggested: ${suggestedText} ms (was ${originalText} ms, not applied${notAppliedReason ? ` · ${notAppliedReason}` : ''})`;
        if (hasWinnerLRScores) {
            toastText = `${outcomeToastPart} · Combined ${scorePctText} % · L ${winner.score_L_pct.toFixed(1)} % / R ${winner.score_R_pct.toFixed(1)} %`;
        } else {
            toastText = `${outcomeToastPart} · Score ${scorePctText} %`;
        }
    }
    if (conf !== 'unknown') {
        toastText += ` · confidence ${conf}`;
    }
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

function formatSpeakerStatus(job) {
    if (typeof SpeakerAlign.formatSpeakerAlignStatus === 'function') {
        return SpeakerAlign.formatSpeakerAlignStatus(job);
    }
    const record = job || {};
    return String(record.message || `Speaker Align ${record.side || 'speaker'}: ${record.status || 'unknown'}.`);
}

function syncSpeakerAlignButton() {
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
    // The static note below the buttons already carries the one-line
    // description; the status line stays empty while idle so the sentence
    // never renders twice. Progress, results and errors keep writing here.
    if (!speakerActive && elements.measurementSpeakerAlignStatus && !measurementState.speakerAlignResult) {
        elements.measurementSpeakerAlignStatus.textContent = '';
    }
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
    const inputId = measurementState.selectedInputId;
    if (!inputId) {
        deps.showToast('No capture input selected', 'error');
        return;
    }
    if (!deps.measurementModeReady()) {
        deps.showToast('No usable host capture source is available', 'error');
        return;
    }
    let payload;
    try {
        payload = readSpeakerAlignPayload(side);
    } catch (error) {
        measurementState.statusText = error?.message || 'Speaker Align failed to start';
        deps.showToast(measurementState.statusText, 'error');
        deps.renderMeasurementPanel();
        return;
    }
    measurementState.speakerAlignInFlight = true;
    measurementState.startInFlight = true;
    measurementState.speakerAlignCancelRequested = false;
    measurementState.activeMeasurementKind = 'speaker_align';
    measurementState.speakerAlignJobId = '';
    measurementState.speakerAlignResult = null;
    measurementState.speakerAlignResultSaved = false;
    renderSpeakerAlignResultActions();
    syncSpeakerAlignButton();
    deps.renderMeasurementPanel();
    try {
        measurementState.statusText = 'Speaker Align: starting…';
        deps.renderMeasurementPanel();
        await deps.postRuntimeDebugSnapshot('ui-before-speaker-align-start', {});
        const resp = await api.startSpeakerAlign(payload);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to start Speaker Align'));
        const job = data.job || {};
        measurementState.speakerAlignJobId = String(job.id || '');
        measurementState.statusText = job.message || 'Speaker Align: queued';
        deps.renderMeasurementPanel();
        if (measurementState.speakerAlignCancelRequested) await cancelSpeakerAlign();
        if (!measurementState.speakerAlignJobId) throw new Error('Speaker Align did not return a job id');
        await pollSpeakerAlignJob(measurementState.speakerAlignJobId);
    } catch (error) {
        console.error('startSpeakerAlign failed', error);
        measurementState.statusText = error.message || 'Speaker Align failed';
        deps.showToast(measurementState.statusText, 'error');
    } finally {
        measurementState.speakerAlignInFlight = false;
        measurementState.startInFlight = false;
        measurementState.activeMeasurementKind = '';
        measurementState.speakerAlignJobId = '';
        measurementState.speakerAlignCancelRequested = false;
        // The result arrived while the run was still in flight, which hides
        // the Save action; render it again now that the run is over.
        renderSpeakerAlignResultActions();
        deps.fetchAudioOutputOverview().catch(() => {});
        deps.renderMeasurementPanel();
    }
}

async function cancelSpeakerAlign() {
    const jobId = String(deps.getState().measurement.speakerAlignJobId || '');
    if (!jobId) {
        if (deps.getState().measurement.speakerAlignInFlight) {
            deps.getState().measurement.speakerAlignCancelRequested = true;
            deps.getState().measurement.statusText = 'Cancelling Speaker Align…';
            deps.renderMeasurementPanel();
        }
        return;
    }
    deps.getState().measurement.statusText = 'Cancelling Speaker Align…';
    deps.renderMeasurementPanel();
    try {
        const resp = await api.cancelSpeakerAlignJob(jobId);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to cancel Speaker Align'));
        deps.getState().measurement.statusText = String(data.job?.message || 'Cancelling Speaker Align…');
    } catch (error) {
        console.error('cancelSpeakerAlign failed', error);
        deps.getState().measurement.statusText = error.message || 'Failed to cancel Speaker Align';
        deps.showToast(deps.getState().measurement.statusText, 'error');
    } finally {
        deps.renderMeasurementPanel();
    }
}

// Giving up on polling must not leave the backend job running: it would keep
// the measurement owner and the output mutes. Cancel it before local cleanup.
async function abandonSpeakerAlignJob(jobId, reason) {
    let cancelled = false;
    try {
        const resp = await api.cancelSpeakerAlignJob(jobId);
        cancelled = !!resp?.ok;
    } catch (error) {
        console.warn('Speaker Align cancel after polling gave up failed', error);
    }
    const measurementState = deps.getState().measurement || {};
    discardSpeakerAlignResults();
    measurementState.statusText = cancelled
        ? `${reason}; the job was cancelled`
        : `${reason}; cancelling the job failed`;
    deps.showToast(measurementState.statusText, 'error');
}

function isCurrentSpeakerAlignPoll(jobId, generation) {
    const live = deps.getState().measurement || {};
    return Number(live.jobGeneration || 0) === Number(generation || 0)
        && (live.activeMeasurementKind === 'speaker_align' || !!live.speakerAlignInFlight)
        && String(live.speakerAlignJobId || '') === String(jobId);
}

async function pollSpeakerAlignJob(jobId) {
    const measurementState = deps.getState().measurement || {};
    const pollGeneration = Number(measurementState.jobGeneration || 0);
    const statusEl = deps.getElements().measurementSpeakerAlignStatus;
    const startedAt = Date.now();
    const longRunningAfterMs = 10 * 60 * 1000;
    const maxRunningMs = 30 * 60 * 1000;
    let consecutiveErrors = 0;
    const maxConsecutiveErrors = 40;
    while (true) {
        if (!isCurrentSpeakerAlignPoll(jobId, pollGeneration)) return;
        await deps.sleep(500);
        if (Date.now() - startedAt >= maxRunningMs) {
            await abandonSpeakerAlignJob(jobId, 'Speaker Align timed out while waiting for the job');
            return;
        }
        try {
            const resp = await api.pollSpeakerAlignJob(jobId);
            const data = await resp.json().catch(() => ({}));
            if (!isCurrentSpeakerAlignPoll(jobId, pollGeneration)) return;
            if (!resp.ok) {
                if (resp.status === 404 || resp.status === 410) {
                    measurementState.statusText = 'Speaker Align job is no longer available';
                    deps.showToast(measurementState.statusText, 'error');
                    return;
                }
                throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to poll Speaker Align job'));
            }
            consecutiveErrors = 0;
            const job = data.job || {};
            const status = job.status || 'unknown';
            measurementState.statusText = job.message || formatSpeakerStatus(job);
            if (Date.now() - startedAt >= longRunningAfterMs
                && (status === 'queued' || status === 'acquiring' || status === 'confirming' || status === 'cancelling')) {
                measurementState.statusText = `Speaker Align still running… ${measurementState.statusText}`;
            }
            if (statusEl) {
                statusEl.textContent = measurementState.statusText;
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
                await abandonSpeakerAlignJob(jobId, error?.message || 'Speaker Align polling failed');
                return;
            }
        }
        deps.renderMeasurementPanel();
    }
}

function speakerAlignRunLabel(measurement) {
    const run = measurement?.speaker_align || measurement?.analysis?.speaker_align;
    const side = run?.side || measurement?.channel || '';
    const name = measurement?.name || '';
    const created = measurement?.created_at || run?.created_at || '';
    const verified = run?.confirmed ? 'verified' : 'not verified';
    const sideLabel = side === 'right' ? 'Right' : side === 'left' ? 'Left' : 'Speaker';
    return `${name || `${sideLabel} align run`} · ${verified}${created ? ` · ${created}` : ''}`;
}

function listSpeakerAlignRuns() {
    const measurements = deps.getState().measurement?.measurements || [];
    return measurements.filter(measurement => measurement
        && (measurement.measurement_kind === 'speaker-align-run-v1'
            || measurement.speaker_align
            || measurement.analysis?.speaker_align));
}

function isSpeakerAlignRunEntry(measurement) {
    return !!measurement && (measurement.measurement_kind === 'speaker-align-run-v1'
        || !!measurement.speaker_align || !!measurement.analysis?.speaker_align);
}

function renderSpeakerAlignResultActions() {
    const elements = deps.getElements();
    const actionsEl = elements.measurementSpeakerAlignActions;
    if (!actionsEl) return;
    const measurementState = deps.getState().measurement || {};
    const results = measurementState.speakerAlignResults || {};
    const result = measurementState.speakerAlignResult
        || results.left || results.right || null;
    const busy = !!(measurementState.speakerAlignInFlight || measurementState.startInFlight
        || deps.hasActiveMeasurementJob());
    if (!result?.proposal || !result?.check || busy) {
        actionsEl.innerHTML = '';
        return;
    }
    const side = result.side === 'right' ? 'right' : result.side === 'left' ? 'left' : '';
    if (measurementState.speakerAlignResultSaved) {
        actionsEl.innerHTML = `<div class="measurement-workflow-speaker-row speaker-align-result-actions">`
            + `<div class="measurement-workflow-speaker-buttons">`
            + `<button type="button" class="btn-secondary" disabled>Saved</button>`
            + `</div><span class="measurement-repeat-help">Align run is in the saved measurements.</span></div>`;
        return;
    }
    const escape = (value) => deps.escapeHtml(String(value ?? ''));
    actionsEl.innerHTML = `<div class="measurement-workflow-speaker-row speaker-align-result-actions">`
        + `<div class="measurement-workflow-speaker-buttons">`
        + `<button type="button" class="btn-secondary" data-speaker-align-save="${escape(side)}">Save align run</button>`
        + `</div><span class="measurement-repeat-help">Before: planning take · After: verification take · no extra sweep.</span></div>`;
}

async function saveSpeakerAlignRun(side) {
    const measurementState = deps.getState().measurement || {};
    const results = measurementState.speakerAlignResults || {};
    const wanted = side === 'left' || side === 'right' ? side : null;
    const availableSides = ['left', 'right'].filter(key => results[key]?.proposal && results[key]?.check);
    const chosen = wanted && results[wanted] ? wanted
        : (measurementState.speakerAlignResult && availableSides.length === 1 ? availableSides[0]
            : (results.right ? 'right' : results.left ? 'left' : null));
    const result = (chosen && results[chosen]) || measurementState.speakerAlignResult;
    if (!result?.proposal || !result?.check) {
        deps.showToast('No align run to save yet', 'warning');
        return;
    }
    const runSide = chosen || result.side || 'left';
    if (typeof SpeakerAlign.buildSpeakerAlignRun !== 'function'
        || typeof SpeakerAlign.runToMeasurement !== 'function') {
        deps.showToast('Speaker Align save support is unavailable', 'error');
        return;
    }
    let payload;
    try {
        const run = SpeakerAlign.buildSpeakerAlignRun(result, {
            side: runSide,
            jobId: measurementState.speakerAlignJobId || result.job_id || '',
            sampleRateHz: Number.isInteger(result.sample_rate_hz) ? result.sample_rate_hz : null,
            committedRevision: result.committed_revision ?? null,
            confirmed: result.confirmed,
            params: result.provenance ? {} : {},
        });
        payload = SpeakerAlign.runToMeasurement(run);
    } catch (error) {
        deps.showToast(error?.message || 'Speaker Align run is not saveable', 'error');
        return;
    }
    try {
        const resp = await api.saveSpeakerAlignMeasurement(payload);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to save align run'));
        if (typeof deps.fetchSavedMeasurements === 'function') {
            await deps.fetchSavedMeasurements().catch(() => {});
        }
        measurementState.speakerAlignResultSaved = true;
        renderSpeakerAlignResultActions();
        deps.showToast('Align run saved', 'success');
    } catch (error) {
        deps.showToast(error?.message || 'Failed to save align run', 'error');
    } finally {
        deps.renderMeasurementPanel();
    }
}

async function openSpeakerAlignRunById(measurementId) {
    const elements = deps.getElements();
    const measurementState = deps.getState().measurement || {};
    const trimmedId = String(measurementId || '').trim();
    if (!trimmedId) {
        deps.showToast('Select a saved align run first', 'warning');
        return;
    }
    const measurement = listSpeakerAlignRuns().find(item => String(item.id) === trimmedId);
    if (!measurement) {
        deps.showToast('Saved align run is no longer available', 'error');
        return;
    }
    if (typeof SpeakerAlign.measurementToRun !== 'function') {
        deps.showToast('Speaker Align open support is unavailable', 'error');
        return;
    }
    let run;
    try {
        run = SpeakerAlign.measurementToRun(measurement);
    } catch (error) {
        deps.showToast(error?.message || 'Saved align run could not be opened', 'error');
        return;
    }
    const result = {
        confirmed: !!run.confirmed,
        committed_revision: run.committed_revision ?? null,
        dry_run: !!run.dry_run,
        side: run.side,
        sample_rate_hz: run.sample_rate_hz,
        provenance: run.metadata?.provenance || {},
        proposal: {
            start_revision: run.metadata.start_revision,
            processing_fingerprint: run.metadata.processing_fingerprint,
            arrival_ms: { ...run.before.arrival_ms },
            added_delay_ms: { ...run.corrections.added_delay_ms },
            added_gain_db: { ...run.corrections.added_gain_db },
            way_levels_db: { ...(run.before.way_levels_db || {}) },
            planning_isolation_db: { ...(run.before.way_isolation_db || {}) },
            reference_role: run.reference_role,
            arrival_source: run.arrival_source || 'shared-planning-take',
        },
        check: {
            confirmed: !!run.confirmed,
            reasons: [...(run.qc.reasons || [])],
            warnings: [...(run.qc.warnings || [])],
            max_residual_ms: run.qc.max_residual_ms,
            before_spread_ms: run.qc.before_spread_ms,
            after_arrival_ms: { ...run.after.arrival_ms },
            tolerance_ms: run.qc.tolerance_ms,
            pairs: run.qc.pairs || [],
            gain_spread_db: run.qc.gain_spread_db ?? null,
            before_gain_spread_db: run.qc.before_gain_spread_db ?? null,
            gain_tolerance_db: run.qc.gain_tolerance_db ?? null,
            after_way_levels_db: { ...(run.after.way_levels_db || {}) },
            way_isolation_db: { ...(run.after.way_isolation_db || {}) },
            isolation_margin_db: run.qc.isolation_margin_db ?? null,
        },
    };
    if (run.frequency) result.way_frequency = JSON.parse(JSON.stringify(run.frequency));
    measurementState.speakerAlignResult = result;
    measurementState.speakerAlignResults = { ...measurementState.speakerAlignResults, [run.side]: result };
    const resultsEl = elements.measurementSpeakerAlignResults;
    if (resultsEl) {
        resultsEl.innerHTML = ['left', 'right'].map(side =>
            SpeakerAlign.renderSpeakerAlignResult(measurementState.speakerAlignResults?.[side], side)).join('');
    }
    const statusEl = elements.measurementSpeakerAlignStatus;
    const pseudoJob = {
        side: run.side,
        status: run.committed_revision !== null && run.committed_revision !== undefined ? 'committed'
            : run.confirmed ? 'trial-done' : 'unconfirmed',
        result,
    };
    const text = formatSpeakerStatus(pseudoJob);
    measurementState.statusText = `${text} (saved run ${run.id})`;
    if (statusEl) statusEl.textContent = measurementState.statusText;
    measurementState.speakerAlignResultSaved = true;
    renderSpeakerAlignResultActions();
    deps.showToast(`Opened align run ${run.id}`, 'success');
    deps.renderMeasurementPanel();
}


// A failed or cancelled run leaves no result behind: an earlier Verified
// table must not stay on screen as if it belonged to this run.
function discardSpeakerAlignResults() {
    const measurementState = deps.getState().measurement || {};
    measurementState.speakerAlignResult = null;
    measurementState.speakerAlignResults = null;
    measurementState.speakerAlignResultSaved = false;
    const resultsEl = deps.getElements().measurementSpeakerAlignResults;
    if (resultsEl) resultsEl.innerHTML = '';
}

async function handleSpeakerAlignResult(job) {
    const measurementState = deps.getState().measurement || {};
    const statusEl = deps.getElements().measurementSpeakerAlignStatus;
    const text = formatSpeakerStatus(job);
    if (job.status === 'cancelled' || job.status === 'cancelling') {
        measurementState.statusText = text;
        discardSpeakerAlignResults();
        if (statusEl) statusEl.textContent = text;
        renderSpeakerAlignResultActions();
        deps.showToast(text, 'success');
        return;
    }
    if (job.status === 'failed') {
        measurementState.statusText = text;
        discardSpeakerAlignResults();
        if (statusEl) statusEl.textContent = '';
        renderSpeakerAlignResultActions();
        deps.showToast(text, 'error');
        return;
    }
    if (!job.result) {
        measurementState.statusText = 'Speaker Align completed with no result';
        if (statusEl) statusEl.textContent = '';
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
    if (statusEl) statusEl.textContent = text;
    measurementState.speakerAlignResultSaved = false;
    renderSpeakerAlignResultActions();
    if (job.status === 'committed') {
        deps.showToast(text, 'success');
    } else if (job.status === 'trial-done') {
        deps.showToast(text, job.result?.confirmed ? 'success' : 'warning');
    } else {
        deps.showToast(text, 'warning');
    }
    deps.renderMeasurementPanel();
}


function clearSpeakerAlignResult() {
    const measurementState = deps.getState().measurement || {};
    if (measurementState.speakerAlignInFlight || measurementState.speakerAlignJobId
        || measurementState.activeMeasurementKind === 'speaker_align') return;
    measurementState.speakerAlignResult = null;
    measurementState.speakerAlignResults = null;
    measurementState.speakerAlignResultSaved = false;
    const elements = deps.getElements();
    if (elements.measurementSpeakerAlignStatus) elements.measurementSpeakerAlignStatus.textContent = '';
    if (elements.measurementSpeakerAlignResults) elements.measurementSpeakerAlignResults.innerHTML = '';
    renderSpeakerAlignResultActions();
}


function getHybridWizardState() {
    if (!deps.getState().measurement.hybridWizard || typeof deps.getState().measurement.hybridWizard !== 'object') {
        deps.getState().measurement.hybridWizard = {
            open: false, running: false, jobId: '', stepIndex: 0, mode: 'stereo',
            sequence: [], captures: [], status: '', phase: '', cancelRequested: false, quality: null, profile: null,
        };
    }
    return deps.getState().measurement.hybridWizard;
}


function getCurrentOutputModeName() {
    const catalog = deps.getState().outputSystem?.catalog;
    const topology = catalog?.modes?.[catalog.active_mode]?.topology;
    return { mono: 'subwoofer-2.1', 'dual-mono': 'subwoofer-2.2', stereo: 'subwoofer-2.2-stereo' }[topology?.sub_mode] || 'stereo';
}


function openHybridMeasurementWizard() {
    const wizard = getHybridWizardState();
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
    const sequence = HybridMeasurement.buildSequence(getCurrentOutputModeName());
    Object.assign(wizard, {
        open: true,
        running: false,
        jobId: '',
        stepIndex: 0,
        mode: sequence.mode,
        sequence: sequence.steps,
        captures: [],
        status: deps.measurementModeReady() ? 'Ready for the first measurement.' : 'Complete Measurement Setup before starting.',
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


function renderHybridRoomDiagram(step = {}, mode = 'stereo', complete = false) {
    const panel = deps.getElements().measurementHybridPanel;
    if (!panel) return;
    panel.querySelectorAll('[data-hybrid-position]').forEach(node => {
        node.classList.toggle('is-target', node.getAttribute('data-hybrid-position') === step.position);
    });
    const diagram = HybridMeasurement.getDiagramState(mode, step.channel, complete);
    panel.querySelectorAll('.hybrid-speaker[data-hybrid-speaker]').forEach(node => {
        const speaker = node.getAttribute('data-hybrid-speaker');
        node.classList.toggle('is-active', !!diagram.speakers[speaker]);
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


function renderHybridMeasurementWizard() {
    const wizard = getHybridWizardState();
    if (!wizard.open || !deps.getElements().measurementHybridPanel) return;
    const complete = !!wizard.profile;
    const current = wizard.sequence[wizard.stepIndex] || null;
    if (deps.getElements().measurementHybridMode) deps.getElements().measurementHybridMode.textContent = `${HybridMeasurement.MODE_LABELS[wizard.mode] || wizard.mode} output mode`;
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
        deps.getElements().measurementHybridPrimaryBtn.textContent = complete ? 'Open filter generator' : (wizard.running ? 'Cancel measurement' : (wizard.quality?.retry ? 'Repeat measurement' : 'Start measurement'));
        deps.getElements().measurementHybridPrimaryBtn.disabled = !wizard.running && !complete && !deps.measurementModeReady();
    }
    if (deps.getElements().measurementHybridBackBtn) deps.getElements().measurementHybridBackBtn.disabled = wizard.running || wizard.stepIndex === 0 || complete;
    if (deps.getElements().measurementHybridSummary) {
        deps.getElements().measurementHybridSummary.classList.toggle('hidden', !complete);
        if (complete) {
            const left = wizard.profile.left.modelBlend;
            const right = wizard.profile.right.modelBlend;
            const integration = wizard.profile.integration;
            deps.getElements().measurementHybridSummary.innerHTML = [
                'Left and right speaker response captured',
                'Main listening position measured',
                'Left and right listening positions measured',
                'Speaker and room measurements combined',
                wizard.mode === 'stereo' ? 'Left and right responses compared' : 'L/R response and subwoofer routing checked',
                `<strong>Gated direct lower limit:</strong><br>Left: ${Math.round(left.gatedDirectLowerLimitHz)} Hz<br>Right: ${Math.round(right.gatedDirectLowerLimitHz)} Hz`,
            ].map(item => `<div class="hybrid-summary-item">${item}</div>`).join('') + `
                <details class="hybrid-advanced-details">
                    <summary>Advanced details</summary>
                    <p>${wizard.mode === 'stereo'
                        ? 'The L/R complex sum was predicted from the main-position captures; no separate integration sweep was performed.'
                        : `Consistency status: ${deps.escapeHtml(integration.status)} · band ${integration.validationBandHz.join('–')} Hz · magnitude ${Number.isFinite(integration.magnitudeRmsErrorDb) ? `${integration.magnitudeRmsErrorDb.toFixed(1)} dB` : 'unavailable'} · phase ${Number.isFinite(integration.phaseRmsErrorDeg) ? `${integration.phaseRmsErrorDeg.toFixed(1)}°` : 'unavailable'} · complex residual ${Number.isFinite(integration.complexResidualRms) ? integration.complexResidualRms.toFixed(2) : 'unavailable'} · ${integration.comparedPoints} points`}</p>
                    ${wizard.mode === 'stereo' ? '' : `<p>${deps.escapeHtml(integration.limitation)}</p>`}
                </details>`;
        }
    }
    renderHybridRoomDiagram(current || {}, wizard.mode, complete);
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
    wizard.status = `Measuring ${ui.hybridSpeakerName(step.channel)}…`;
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
            : (processing ? `Processing ${step.channel === 'stereo' ? 'measurement' : step.channel}…` : `Measuring ${ui.hybridSpeakerName(step.channel)}…`);
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
                    wizard.captures,
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
            wizard.captures.push({ stepId: step.id, role: step.role, position: step.position, channel: step.channel, measurement });
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


async function runHybridWizardSweep() {
    const wizard = getHybridWizardState();
    if (wizard.running || wizard.profile) return;
    const step = wizard.sequence[wizard.stepIndex];
    if (!step) return;
    if (getCurrentOutputModeName() !== wizard.mode) {
        wizard.status = 'Output mode changed. Close and restart the wizard so measurements are not mixed.';
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
        } else if (wizard.stepIndex >= wizard.sequence.length) {
            wizard.profile = HybridMeasurement.buildProfile(wizard.captures, wizard.mode);
            wizard.status = 'All measurements were completed successfully.';
        } else if (!wizard.quality?.retry) {
            wizard.status = 'Position measured successfully. Move the microphone as shown for the next measurement.';
        }
    } catch (error) {
        if (error.retryRole === 'integration') {
            const integrationIndex = wizard.sequence.findIndex(step => step.role === 'integration');
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


function openHybridProfileInConvolver() {
    const wizard = getHybridWizardState();
    if (!wizard.profile) return;
    const buildSide = (side) => {
        const model = wizard.profile[side];
        const timing = model.timingMeasurement;
        const modelPoints = Array.isArray(model.points) ? model.points : [];
        const modelMinDb = modelPoints.length ? Math.min(...modelPoints.map((point) => Number(point[1]) || 0)) : 0;
        const modelMaxDb = modelPoints.length ? Math.max(...modelPoints.map((point) => Number(point[1]) || 0)) : 0;
        const modelMinHz = modelPoints.length ? Number(modelPoints[0][0]) || 20 : 20;
        const modelMaxHz = modelPoints.length ? Number(modelPoints[modelPoints.length - 1][0]) || 20000 : 20000;
        const modelSummary = { trace_count: modelPoints.length ? 1 : 0, point_count: modelPoints.length, min_db: modelMinDb, max_db: modelMaxDb, min_hz: modelMinHz, max_hz: modelMaxHz };
        return deps.normalizeMeasurementEntry({
            id: `hybrid-${side}-${Date.now()}`,
            name: `Advanced ${HybridMeasurement.MODE_LABELS[wizard.mode]} ${side === 'left' ? 'L' : 'R'}`,
            created_at: new Date().toISOString(),
            channel: side,
            measurement_kind: 'hybrid-correction-model-v1',
            measurement_role: 'hybrid-model',
            input_device: timing.input_device,
            input_channels: timing.input_channels,
            calibration: timing.calibration,
            audio_output_context: timing.audio_output_context,
            traces: [{ kind: 'hybrid-response', role: 'trusted', label: `Hybrid ${side}`, points: model.points }],
            summary: modelSummary,
            review_summary: modelSummary,
            analysis: {
                ...timing.analysis,
                hybrid_constraints: model.constraints,
                hybrid_model_blend: model.modelBlend,
                hybrid_source_roles: ['direct', 'mlp', 'secondary', 'integration'],
                integration_validation: wizard.profile.integration,
            },
        }, 0);
    };
    const pair = [buildSide('left'), buildSide('right')];
    deps.getState().measurement.pendingRepeatMeasurements = pair;
    deps.getState().measurement.currentMeasurement = pair[0];
    deps.getState().measurement.currentMeasurementName = `Advanced ${HybridMeasurement.MODE_LABELS[wizard.mode]}`;
    deps.getState().measurement.currentMeasurementSaved = false;
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
        cancelAutoSubOptimize,
        pollAutoSubJob,
        handleAutoSubResult,
        syncSpeakerAlignButton,
        startSpeakerAlign,
        cancelSpeakerAlign,
        pollSpeakerAlignJob,
        handleSpeakerAlignResult,
        clearSpeakerAlignResult,
        listSpeakerAlignRuns,
        isSpeakerAlignRunEntry,
        renderSpeakerAlignResultActions,
        saveSpeakerAlignRun,
        openSpeakerAlignRunById,
        getHybridWizardState,
        getCurrentOutputModeName,
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
