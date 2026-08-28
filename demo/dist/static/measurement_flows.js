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
        getAutoSubTargetCurveSnapshot: () => null,
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        postRuntimeDebugSnapshot: async () => {},
        formatTransitionErrorDetail: (d, m) => m,
        fetchAudioOutputOverview: async () => {},
        getMeasurementReferenceWarning: () => '',
        normalizeOutputModeName: (m) => m || 'stereo',
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
    const outputMode = deps.getState().settings?.audioOutputs?.output_mode;
    const isSubwooferMode = deps.isSubwooferModeName(outputMode?.mode || '');
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
        formData.append('channel', measurementState.selectedChannel || 'left');
        deps.normalizeMeasurementInputChannelSelections();
        formData.append('mic_input_channel', measurementState.selectedMicInputChannel || '1');
        formData.append('reference_input_channel', measurementState.selectedReferenceInputChannel || '');
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
        if (!measurementState.autoSubJobId) return;
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


async function pollAutoSubJob(jobId) {
    const measurementState = deps.getState().measurement || {};
    const statusEl = deps.getElements().measurementAutoSubStatus;
    const startedAt = Date.now();
    const longRunningAfterMs = 10 * 60 * 1000;
    while (true) {
        await deps.sleep(500);
        try {
            const resp = await api.pollAutoSubJob(jobId);
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to poll Auto Sub job'));
            const job = data.job || {};
            const status = job.status || 'unknown';
            const fineScan = job.fine_scan || {};
            const targetLabel = String(job.target_curve?.label || '');

            measurementState.statusText = job.message || 'Auto Sub Optimize: running';
            if (job.progress) {
                measurementState.statusText += ` (${job.progress.current}/${job.progress.total})`;
            }
            if (Date.now() - startedAt >= longRunningAfterMs
                    && (status === 'queued' || status === 'preparing' || status === 'running' || status === 'cancelling')) {
                measurementState.statusText = `AutoSub läuft weiterhin … ${measurementState.statusText}`;
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
                        left_sub: 'Optimizing Left Sub',
                        right_sub: 'Optimizing Right Sub',
                        combined_matrix: 'Combined Matrix',
                    };
                    const stageLabel = stageLabels[progress.stage] || 'Coarse';
                    const candidateCur = progress.candidate_current;
                    const candidateTot = progress.candidate_total;
                    const sweepCur = progress.sweep_current ?? progress.current;
                    const sweepTot = progress.sweep_total ?? progress.total;
                    if (Number.isFinite(candidateCur) && Number.isFinite(candidateTot)) {
                        statusEl.textContent = `${stageLabel}: ${candidateCur}/${candidateTot} candidates (${sweepCur}/${sweepTot} sweeps)${targetLabel ? ` · Target: ${targetLabel}` : ''}`;
                    } else {
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
            console.warn('pollAutoSubJob error', error);
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
        const sub1Label = isStereoBassResult ? 'Left Sub' : 'Sub 1';
        const sub2Label = isStereoBassResult ? 'Right Sub' : 'Sub 2';
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
    const isWeak = !wasApplied || (Number.isFinite(scorePct) && scorePct < 50);

    const mainParts = [];
    if (wasApplied) {
        mainParts.push(`AutoSub applied: ${appliedText} ms (was ${originalText} ms)`);
    } else {
        mainParts.push(`AutoSub suggested: ${suggestedText} ms (was ${originalText} ms, not applied)`);
    }
    if (hasWinnerLRScores) {
        mainParts.push(`Score ${scorePctText} % · L ${winner.score_L_pct.toFixed(1)} % / R ${winner.score_R_pct.toFixed(1)} %`);
    } else {
        mainParts.push(`Score ${scorePctText} %`);
    }
    if (isWeak) {
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
    if (hasWinnerLRScores) {
        toastText = wasApplied
            ? `Applied: ${appliedText} ms (was ${originalText} ms) · Combined ${scorePctText} % · L ${winner.score_L_pct.toFixed(1)} % / R ${winner.score_R_pct.toFixed(1)} %`
            : `Suggested: ${suggestedText} ms (was ${originalText} ms, not applied) · Combined ${scorePctText} % · L ${winner.score_L_pct.toFixed(1)} % / R ${winner.score_R_pct.toFixed(1)} %`;
    } else {
        toastText = wasApplied
            ? `Applied: ${appliedText} ms (was ${originalText} ms) · Score ${scorePctText} %`
            : `Suggested: ${suggestedText} ms (was ${originalText} ms, not applied) · Score ${scorePctText} %`;
    }
    if (isWeak) {
        toastText += ` · ${conf}`;
    }
    syncSubwooferControlsDuringAutoSub();
    const toastType = isWeak ? 'error' : (wasApplied ? 'success' : 'warning');
    deps.showToast(toastText, toastType);
    deps.renderMeasurementPanel();
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
    return deps.normalizeOutputModeName(deps.getState().settings.audioOutputs.output_mode?.mode || 'stereo');
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
    formData.append('input_id', deps.getState().measurement.selectedInputId);
    formData.append('input_key', deps.getState().measurement.selectedInputKey || '');
    formData.append('channel', step.channel);
    formData.append('measurement_role', step.role);
    formData.append('mic_input_channel', deps.getState().measurement.selectedMicInputChannel || '1');
    formData.append('reference_input_channel', deps.getMeasurementReferenceWarning() ? '' : (deps.getState().measurement.selectedReferenceInputChannel || ''));
    const calibrationFile = deps.getElements().measurementCalibrationFile?.files?.[0];
    if (calibrationFile) formData.append('calibration_file', calibrationFile);
    else if (deps.getState().measurement.selectedCalibrationRef) formData.append('calibration_ref', deps.getState().measurement.selectedCalibrationRef);
    return formData;
}


async function runHybridWizardStep(step) {
    const wizard = getHybridWizardState();
    wizard.quality = null;
    wizard.phase = 'measuring';
    wizard.status = `Measuring ${hybridSpeakerName(step.channel)}…`;
    renderHybridMeasurementWizard();
    const response = await api.startMeasurement(buildHybridMeasurementForm(step));
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to start advanced measurement'));
    const jobId = String(data.job?.id || '');
    wizard.jobId = jobId;
    deps.getState().measurement.activeJobId = jobId;
    deps.getState().measurement.activeMeasurementKind = 'hybrid';
    if (wizard.cancelRequested) {
        await api.cancelMeasurementJob(jobId).catch(() => null);
    }

    for (let attempt = 0; attempt < 360; attempt += 1) {
        const poll = await api.pollMeasurementJob(jobId);
        const payload = await poll.json().catch(() => ({}));
        if (!poll.ok) throw new Error(deps.formatTransitionErrorDetail(payload.detail, 'Failed to fetch advanced measurement'));
        const job = payload.job || {};
        const status = deps.getMeasurementJobStatus(job);
        const processing = String(job.message || '').toLowerCase().startsWith('processing');
        wizard.phase = processing ? 'processing' : (wizard.cancelRequested ? 'cancelling' : 'measuring');
        wizard.status = wizard.cancelRequested
            ? 'Cancelling measurement…'
            : (processing ? `Processing ${step.channel === 'stereo' ? 'measurement' : step.channel}…` : `Measuring ${hybridSpeakerName(step.channel)}…`);
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
