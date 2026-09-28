// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement job lifecycle: status, cancel and polling. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementJob = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const ui = root.FXRouteMeasurementUI || {};
    let deps = {
        getState: () => ({ measurement: {} }),
        getElements: () => ({}),
        fetch: (...args) => root.fetch(...args),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        normalizeMeasurementKind: (kind) => kind,
        formatMeasurementInputLevelText: () => '',
        getMeasurementJobStatus: () => '',
        getMeasurementJobResultMeasurement: () => null,
        getMeasurementTimingInfo: () => ({}),
        normalizeMeasurementEntry: (entry) => entry,
        setMeasurementGraphView: () => {},
        measurementModeReady: () => false,
        measurementRepeatBlockedReason: () => '',
        syncMeasurementRepeatNote: () => {},
        setMeasurementSweepMenuOpen: () => {},
        syncAutoSubButton: () => {},
        syncSpeakerAlignButton: () => {},
        cancelHybridWizardMeasurement: async () => {},
        cancelAutoSubOptimize: async () => {},
        cancelSpeakerAlign: async () => {},
        postRuntimeDebugSnapshot: async () => {},
        formatTransitionErrorDetail: (_detail, fallback) => fallback,
        sleep: (ms) => new Promise((resolve) => root.setTimeout(resolve, ms)),
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    // Status contract shared by Sweep, Auto Sub and Speaker Align: the feature
    // line next to the action shows live progress (or its idle note), the
    // panel status line (statusText) only the outcome, warning or error.
    function setFeatureLine(element, text) {
        if (!element) return;
        // The static note in the page shell is the idle text; keep it once.
        if (element.dataset && element.dataset.idleText === undefined) element.dataset.idleText = element.textContent;
        element.textContent = text || element.dataset?.idleText || '';
    }

    function renderSweepStatusLine() {
        const measurementState = deps.getState().measurement || {};
        setFeatureLine(deps.getElements().measurementSweepStatus,
            measurementState.progressKind === 'sweep' ? measurementState.progressText : '');
    }

    function setSweepProgress(text) {
        const measurementState = deps.getState().measurement;
        measurementState.progressKind = 'sweep';
        measurementState.progressText = String(text || '');
        renderSweepStatusLine();
    }

    function clearSweepProgress() {
        const measurementState = deps.getState().measurement;
        if (measurementState.progressKind === 'sweep') {
            measurementState.progressKind = '';
            measurementState.progressText = '';
        }
        renderSweepStatusLine();
    }

    function sweepLabel(job = {}) {
        const measurementState = deps.getState().measurement || {};
        const lrRepeat = job.job_kind === 'lr-repeat' || !!measurementState.repeatJobActive
            || measurementState.activeMeasurementKind === 'lr_repeat';
        return lrRepeat ? 'L/R repeat' : 'Sweep';
    }

    // Finish a sweep with its outcome on the panel status line.
    function finishSweep(outcome) {
        deps.getState().measurement.statusText = outcome;
        clearSweepProgress();
    }

    function getActiveMeasurementKind() {
        const measurementState = deps.getState().measurement || {};
        if (measurementState.autoSubInFlight) return 'auto_sub';
        if (measurementState.speakerAlignInFlight) return 'speaker_align';
        if (measurementState.hybridWizard?.running) return 'hybrid';
        const normalized = deps.normalizeMeasurementKind(measurementState.activeMeasurementKind);
        if (normalized && measurementState.activeJobId) return normalized;
        if (measurementState.repeatJobActive && measurementState.activeJobId) return 'lr_repeat';
        if (measurementState.activeJobId) return 'single';
        return '';
    }

    function hasActiveMeasurementJob() {
        const measurementState = deps.getState().measurement || {};
        return !!(measurementState.activeJobId || measurementState.autoSubInFlight || measurementState.speakerAlignInFlight || measurementState.hybridWizard?.running);
    }

    function formatMeasurementJobStatusText(job = {}, fallback = 'Measurement running…') {
        const message = String(job.message || fallback);
        const levelText = deps.formatMeasurementInputLevelText(job.input_level);
        const isLrRepeat = job.job_kind === 'lr-repeat' || !!deps.getState().measurement?.repeatJobActive;
        if (!isLrRepeat || !levelText || /\b(CLIP|dBFS)\b/.test(message)) return message;
        return `${message} · ${levelText}`;
    }

    function syncMeasurementSweepButton() {
        if (!deps.getElements().measurementSweepToggleBtn) return;
        const measurementState = deps.getState().measurement || {};
        const calibrationBusy = measurementState.calibrationUpdating || measurementState.calibrationDeleting;
        const measurementActive = !!measurementState.startInFlight || hasActiveMeasurementJob();
        const hybridWizard = measurementState.hybridWizard || {};
        const hybridButtonHost = deps.getElements().measurementHybridHeaderActions;
        const sweepMenuHost = deps.getElements().measurementSweepMenu?.parentElement;
        const shouldShowHybridCancel = !!hybridWizard.running && !!hybridWizard.open;

        if (shouldShowHybridCancel && hybridButtonHost && !hybridButtonHost.contains(deps.getElements().measurementSweepToggleBtn)) {
            hybridButtonHost.append(deps.getElements().measurementSweepToggleBtn);
        } else if (!shouldShowHybridCancel && sweepMenuHost && !sweepMenuHost.contains(deps.getElements().measurementSweepToggleBtn)) {
            sweepMenuHost.insertBefore(deps.getElements().measurementSweepToggleBtn, deps.getElements().measurementSweepMenu);
        }

        deps.getElements().measurementSweepToggleBtn.disabled = !!calibrationBusy;
        deps.getElements().measurementSweepToggleBtn.textContent = measurementActive ? 'Cancel' : 'Start Sweep';
        deps.getElements().measurementSweepToggleBtn.setAttribute('aria-expanded', measurementActive ? 'false' : (deps.getElements().measurementSweepMenu?.classList.contains('hidden') ? 'false' : 'true'));
        if (measurementActive) deps.setMeasurementSweepMenuOpen(false);
        renderSweepStatusLine();
    }

    function syncMeasurementStartButtonFallback() {
        syncMeasurementSweepButton();
        const measurementState = deps.getState().measurement || {};
        const activeKind = getActiveMeasurementKind();
        const activeJobRunning = hasActiveMeasurementJob();
        const lrActive = activeKind === 'lr_repeat';
        const calibrationBusy = measurementState.calibrationUpdating || measurementState.calibrationDeleting;
        const repeatBlockedReason = deps.measurementRepeatBlockedReason();
        if (deps.getElements().measurementRepeatStartBtn) {
            deps.getElements().measurementRepeatStartBtn.disabled = repeatBlockedReason && !lrActive
                ? true
                : (calibrationBusy
                    ? true
                    : (activeJobRunning ? !lrActive
                    : (measurementState.startInFlight || measurementState.inputsLoading || !deps.measurementModeReady())));
            deps.getElements().measurementRepeatStartBtn.textContent = lrActive
                ? 'Cancel measurement'
                : 'Start LR Repeat';
        }
        deps.syncMeasurementRepeatNote(lrActive, repeatBlockedReason);
        deps.syncAutoSubButton();
        deps.syncSpeakerAlignButton();
    }

    async function cancelMeasurement() {
        const jobId = String(deps.getState().measurement.activeJobId || '');
        if (!jobId) return;
        const jobGeneration = deps.getState().measurement.jobGeneration;
        const label = sweepLabel();
        const previousProgress = deps.getState().measurement.progressText || '';
        setSweepProgress('Cancelling…');
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                // The job is already gone server-side: resolve into a cleared
                // state so Cancel itself cannot leave the UI stuck.
                if (resp.status === 404 || resp.status === 410) {
                    if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== jobId) return;
                    finishSweep(`${label} interrupted: the run is no longer available.`);
                    deps.getState().measurement.activeJobId = '';
                    deps.getState().measurement.startInFlight = false;
                    deps.getState().measurement.activeMeasurementKind = '';
                    deps.getState().measurement.cancelRequested = false;
                    deps.getState().measurement.repeatJobActive = false;
                    syncMeasurementStartButtonFallback();
                    return;
                }
                throw new Error(deps.formatTransitionErrorDetail(data.detail, 'request rejected'));
            }
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== jobId) return;
            // Until the job reports cancelled, polling keeps "Cancelling…".
            if (ui.MEASUREMENT_JOB_CANCELLED_STATES.has(deps.getMeasurementJobStatus(data.job || {}))) {
                finishSweep(`${label} cancelled.`);
                deps.getState().measurement.activeJobId = '';
                deps.getState().measurement.startInFlight = false;
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.cancelRequested = false;
                deps.getState().measurement.repeatJobActive = false;
                syncMeasurementStartButtonFallback();
            }
        } catch (error) {
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== jobId) return;
            console.error('cancelMeasurement failed', error);
            setSweepProgress(previousProgress);
            deps.getState().measurement.statusText = `Could not cancel the ${label.toLowerCase()}: ${error.message || 'request failed'}`;
            deps.showToast('Cancel failed', 'error');
        } finally {
            renderMeasurementPanelDefensively('measurement cancel render');
        }
    }

    function requestMeasurementCancellation() {
        const activeKind = getActiveMeasurementKind();
        if (activeKind === 'hybrid') return deps.cancelHybridWizardMeasurement();
        if (activeKind === 'auto_sub') return deps.cancelAutoSubOptimize();
        if (activeKind === 'speaker_align') return deps.cancelSpeakerAlign();
        if (deps.getState().measurement.activeJobId) return cancelMeasurement();
        if (deps.getState().measurement.startInFlight) {
            deps.getState().measurement.cancelRequested = true;
            setSweepProgress('Cancelling…');
            deps.renderMeasurementPanel();
        }
        return Promise.resolve();
    }

    async function pollMeasurementJob(jobId, jobGeneration = deps.getState().measurement.jobGeneration) {
        if (!jobId) return;
        // Transport blips must not brick the sweep UI: tolerate consecutive
        // fetch failures like the AutoSub/SpeakerAlign polls, and resolve a
        // gone job (404/410) into a cleared state instead of a stuck one.
        let consecutiveErrors = 0;
        const maxConsecutiveErrors = 40;
        const label = sweepLabel();
        for (let attempt = 0; attempt < 360; attempt += 1) {
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
            let resp;
            let data;
            try {
                resp = await deps.fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}`);
                data = await resp.json().catch(() => ({}));
            } catch (error) {
                if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
                consecutiveErrors += 1;
                console.warn('pollMeasurementJob error', error);
                if (consecutiveErrors >= maxConsecutiveErrors) {
                    finishSweep(`${label} interrupted: ${error?.message || 'connection lost'}`);
                    deps.getState().measurement.activeJobId = '';
                    deps.getState().measurement.startInFlight = false;
                    deps.getState().measurement.cancelRequested = false;
                    deps.getState().measurement.activeMeasurementKind = '';
                    deps.getState().measurement.repeatJobActive = false;
                    syncMeasurementStartButtonFallback();
                    renderMeasurementPanelDefensively('measurement polling failure sync');
                    deps.showToast(`${label} interrupted`, 'error');
                    return;
                }
                await deps.sleep(800);
                continue;
            }
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
            if (!resp.ok) {
                if (resp.status === 404 || resp.status === 410) {
                    finishSweep(`${label} interrupted: the run is no longer available.`);
                    deps.getState().measurement.activeJobId = '';
                    deps.getState().measurement.startInFlight = false;
                    deps.getState().measurement.cancelRequested = false;
                    deps.getState().measurement.activeMeasurementKind = '';
                    deps.getState().measurement.repeatJobActive = false;
                    syncMeasurementStartButtonFallback();
                    renderMeasurementPanelDefensively('measurement gone-state sync');
                    deps.showToast(`${label} interrupted`, 'error');
                    return;
                }
                consecutiveErrors += 1;
                console.warn('pollMeasurementJob error', deps.formatTransitionErrorDetail(data.detail, 'Failed to fetch measurement job'));
                if (consecutiveErrors >= maxConsecutiveErrors) {
                    finishSweep(`${label} interrupted: ${deps.formatTransitionErrorDetail(data.detail, 'connection lost')}`);
                    deps.getState().measurement.activeJobId = '';
                    deps.getState().measurement.startInFlight = false;
                    deps.getState().measurement.cancelRequested = false;
                    deps.getState().measurement.activeMeasurementKind = '';
                    deps.getState().measurement.repeatJobActive = false;
                    syncMeasurementStartButtonFallback();
                    renderMeasurementPanelDefensively('measurement polling failure sync');
                    deps.showToast(`${label} interrupted`, 'error');
                    return;
                }
                await deps.sleep(800);
                continue;
            }
            consecutiveErrors = 0;
            const job = data.job || {};
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
            const jobStatus = deps.getMeasurementJobStatus(job);
            // A requested cancel keeps "Cancelling…" until the job ends.
            if (deps.getState().measurement.progressText !== 'Cancelling…') {
                setSweepProgress(formatMeasurementJobStatusText(job, deps.getState().measurement.progressText || 'Running…'));
            }
            if (ui.MEASUREMENT_JOB_SUCCESS_STATES.has(jobStatus)) {
                finishSweep(`${label} finished.`);
                deps.getState().measurement.activeJobId = '';
                deps.getState().measurement.startInFlight = false;
                deps.getState().measurement.cancelRequested = false;
                const repeatMeasurements = Array.isArray(job?.result?.measurements) ? job.result.measurements : [];
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.repeatJobActive = false;
                syncMeasurementStartButtonFallback();
                renderMeasurementPanelDefensively('measurement completion state sync');
                await deps.postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                    jobId,
                    jobStatus,
                    measurementKind: repeatMeasurements.length ? 'lr-repeat' : 'single',
                });
                if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
                if (repeatMeasurements.length) {
                    deps.getState().measurement.pendingRepeatMeasurements = repeatMeasurements.map((measurement, index) => deps.normalizeMeasurementEntry(measurement, index));
                    deps.getState().measurement.currentMeasurement = deps.getState().measurement.pendingRepeatMeasurements[0] || null;
                    deps.getState().measurement.currentMeasurementName = String(job?.result?.base_name || '').trim();
                    deps.getState().measurement.currentMeasurementSaved = false;
                    deps.getState().measurement.pendingRepeatMeasurements.forEach((measurement) => {
                        deps.getState().measurement.reviewVisibilityById[measurement.id] = !!measurement.review_traces?.length;
                    });
                    // A sweep result opens in the frequency view, a Speaker
                    // Align result in the IR view.
                    deps.setMeasurementGraphView('freq');
                    deps.getState().measurement.statusText = 'L/R repeat finished. Review the L and R results and save them together.';
                    renderMeasurementPanelDefensively('L/R repeat completion render');
                    deps.showToast('L/R repeat finished', 'success');
                    return;
                }
                const resultMeasurement = deps.getMeasurementJobResultMeasurement(job);
                if (resultMeasurement) {
                    try {
                        deps.getState().measurement.currentMeasurement = deps.normalizeMeasurementEntry(resultMeasurement, 0);
                        deps.getState().measurement.currentMeasurementName = deps.getState().measurement.currentMeasurement.name || '';
                        deps.getState().measurement.currentMeasurementSaved = false;
                        deps.getState().measurement.reviewVisibilityById[deps.getState().measurement.currentMeasurement.id] = !!deps.getState().measurement.currentMeasurement.review_traces?.length;
                        deps.setMeasurementGraphView('freq');
                        const timingInfo = deps.getMeasurementTimingInfo(deps.getState().measurement.currentMeasurement);
                        if (timingInfo.line) deps.getState().measurement.statusText = `${label} finished · ${timingInfo.line}`;
                    } catch (error) {
                        console.error('measurement result normalization failed', error, job);
                        deps.getState().measurement.statusText = error?.message
                            ? `${label} finished, but the result could not be shown: ${error.message}`
                            : `${label} finished, but the result could not be shown.`;
                        renderMeasurementPanelDefensively('measurement completion render after normalization failure');
                        deps.showToast('Result could not be shown', 'error');
                        return;
                    }
                } else {
                    deps.getState().measurement.statusText = `${label} finished, but no result data was returned.`;
                }
                const rendered = renderMeasurementPanelDefensively('measurement completion render');
                if (resultMeasurement && rendered) deps.showToast(`${label} finished`, 'success');
                if (!resultMeasurement) deps.showToast('No result data', 'warning');
                return;
            }
            if (ui.MEASUREMENT_JOB_FAILED_STATES.has(jobStatus)) {
                deps.getState().measurement.activeJobId = '';
                deps.getState().measurement.startInFlight = false;
                deps.getState().measurement.cancelRequested = false;
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.repeatJobActive = false;
                syncMeasurementStartButtonFallback();
                renderMeasurementPanelDefensively('measurement failure state sync');
                await deps.postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                    jobId,
                    jobStatus,
                    failed: true,
                });
                if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
                throw new Error(deps.formatTransitionErrorDetail(job.error?.detail, job.message || ''));
            }
            if (ui.MEASUREMENT_JOB_CANCELLED_STATES.has(jobStatus)) {
                deps.getState().measurement.activeJobId = '';
                deps.getState().measurement.startInFlight = false;
                deps.getState().measurement.cancelRequested = false;
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.repeatJobActive = false;
                finishSweep(`${label} cancelled.`);
                renderMeasurementPanelDefensively('measurement cancellation state sync');
                await deps.postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                    jobId,
                    jobStatus,
                    cancelled: true,
                });
                if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
                renderMeasurementPanelDefensively('measurement cancellation render');
                deps.showToast(`${label} cancelled`, 'success');
                return;
            }
            deps.getState().measurement.activeJobId = String(job.id || jobId);
            renderMeasurementPanelDefensively('measurement polling render');
            await deps.sleep(800);
        }
        if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
        deps.getState().measurement.activeJobId = '';
        deps.getState().measurement.startInFlight = false;
        deps.getState().measurement.cancelRequested = false;
        deps.getState().measurement.activeMeasurementKind = '';
        deps.getState().measurement.repeatJobActive = false;
        syncMeasurementStartButtonFallback();
        throw new Error('timed out waiting for the result');
    }

    function renderMeasurementPanelDefensively(context = 'measurement render') {
        try {
            deps.renderMeasurementPanel();
            return true;
        } catch (error) {
            console.error(`${context} failed`, error);
            deps.getState().measurement.activeJobId = '';
            deps.getState().measurement.startInFlight = false;
            deps.getState().measurement.statusText = error?.message
                ? `Measurement finished, but the result could not be rendered: ${error.message}`
                : 'Measurement finished, but the result could not be rendered.';
            syncMeasurementStartButtonFallback();
            deps.showToast('Result could not be shown', 'error');
            return false;
        }
    }

    return {
        init,
        getActiveMeasurementKind, hasActiveMeasurementJob, formatMeasurementJobStatusText, syncMeasurementSweepButton, syncMeasurementStartButtonFallback, cancelMeasurement, requestMeasurementCancellation, pollMeasurementJob, renderMeasurementPanelDefensively,
    };
});
