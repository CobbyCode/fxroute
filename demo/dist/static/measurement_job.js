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
        deps.getState().measurement.statusText = 'Cancelling measurement…';
        deps.renderMeasurementPanel();
        try {
            const resp = await deps.fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to cancel measurement'));
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== jobId) return;
            deps.getState().measurement.statusText = String(data.job?.message || 'Measurement cancelled.');
            if (ui.MEASUREMENT_JOB_CANCELLED_STATES.has(deps.getMeasurementJobStatus(data.job || {}))) {
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
            deps.getState().measurement.statusText = error.message || 'Failed to cancel measurement';
            deps.showToast(deps.getState().measurement.statusText, 'error');
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
            deps.getState().measurement.statusText = 'Cancelling measurement…';
            deps.renderMeasurementPanel();
        }
        return Promise.resolve();
    }

    async function pollMeasurementJob(jobId, jobGeneration = deps.getState().measurement.jobGeneration) {
        if (!jobId) return;
        for (let attempt = 0; attempt < 360; attempt += 1) {
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
            const resp = await deps.fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}`);
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Failed to fetch measurement job'));
            const job = data.job || {};
            if (deps.getState().measurement.jobGeneration !== jobGeneration || String(deps.getState().measurement.activeJobId || '') !== String(jobId)) return;
            const jobStatus = deps.getMeasurementJobStatus(job);
            deps.getState().measurement.statusText = formatMeasurementJobStatusText(job, deps.getState().measurement.statusText || 'Measurement running…');
            if (ui.MEASUREMENT_JOB_SUCCESS_STATES.has(jobStatus)) {
                deps.getState().measurement.statusText = String(job.message || 'Measurement finished.');
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
                    deps.getState().measurement.statusText = String(job.message || 'L/R repeat finished.');
                    renderMeasurementPanelDefensively('L/R repeat completion render');
                    deps.showToast('L/R repeat finished. Review and save when ready.', 'success');
                    return;
                }
                const resultMeasurement = deps.getMeasurementJobResultMeasurement(job);
                if (resultMeasurement) {
                    try {
                        deps.getState().measurement.currentMeasurement = deps.normalizeMeasurementEntry(resultMeasurement, 0);
                        deps.getState().measurement.currentMeasurementName = deps.getState().measurement.currentMeasurement.name || '';
                        deps.getState().measurement.currentMeasurementSaved = false;
                        deps.getState().measurement.reviewVisibilityById[deps.getState().measurement.currentMeasurement.id] = !!deps.getState().measurement.currentMeasurement.review_traces?.length;
                        const timingInfo = deps.getMeasurementTimingInfo(deps.getState().measurement.currentMeasurement);
                        if (timingInfo.line) deps.getState().measurement.statusText = timingInfo.line;
                    } catch (error) {
                        console.error('measurement result normalization failed', error, job);
                        deps.getState().measurement.statusText = error?.message
                            ? `Measurement finished, but the result could not be displayed: ${error.message}`
                            : 'Measurement finished, but the result could not be displayed.';
                        renderMeasurementPanelDefensively('measurement completion render after normalization failure');
                        deps.showToast(deps.getState().measurement.statusText, 'error');
                        return;
                    }
                } else {
                    deps.getState().measurement.statusText = String(job.message || 'Measurement finished, but no result data was returned.');
                }
                const rendered = renderMeasurementPanelDefensively('measurement completion render');
                if (resultMeasurement && rendered) deps.showToast('Measurement finished', 'success');
                if (!resultMeasurement) deps.showToast(deps.getState().measurement.statusText, 'warning');
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
                throw new Error(deps.formatTransitionErrorDetail(job.error?.detail, job.message || 'Measurement failed'));
            }
            if (ui.MEASUREMENT_JOB_CANCELLED_STATES.has(jobStatus)) {
                deps.getState().measurement.activeJobId = '';
                deps.getState().measurement.startInFlight = false;
                deps.getState().measurement.cancelRequested = false;
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.repeatJobActive = false;
                deps.getState().measurement.statusText = String(job.message || 'Measurement cancelled.');
                renderMeasurementPanelDefensively('measurement cancellation state sync');
                await deps.postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                    jobId,
                    jobStatus,
                    cancelled: true,
                });
                if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
                renderMeasurementPanelDefensively('measurement cancellation render');
                deps.showToast('Measurement cancelled', 'success');
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
        throw new Error('Measurement job timed out while waiting for completion');
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
            deps.showToast(deps.getState().measurement.statusText, 'error');
            return false;
        }
    }

    return {
        init,
        getActiveMeasurementKind, hasActiveMeasurementJob, formatMeasurementJobStatusText, syncMeasurementSweepButton, syncMeasurementStartButtonFallback, cancelMeasurement, requestMeasurementCancellation, pollMeasurementJob, renderMeasurementPanelDefensively,
    };
});
