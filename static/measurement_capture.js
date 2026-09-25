// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement capture start flow (single sweep and L/R repeat). */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementCapture = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ measurement: {} }),
        getElements: () => ({}),
        getFormDataType: () => root.FormData,
        fetch: (...args) => root.fetch(...args),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        requireConcreteFilterBank: () => false,
        measurementModeReady: () => false,
        measurementRepeatBlockedReason: () => '',
        flushSubwooferSettingsBeforeMeasurement: async () => {},
        measurementAreaFromCatalog: () => null,
        appendMeasurementReferenceFields: () => {},
        postRuntimeDebugSnapshot: () => {},
        formatTransitionErrorDetail: (_detail, fallback) => fallback,
        normalizeMeasurementKind: (kind) => kind,
        formatMeasurementJobStatusText: (job) => '',
        pollMeasurementJob: async () => {},
        cancelMeasurement: async () => {},
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    // Live progress goes to the sweep feature line; the panel status line
    // (statusText) only gets the outcome.
    function setProgress(text) {
        const measurementState = deps.getState().measurement;
        // A cancel requested before the job id arrived keeps "Cancelling…".
        if (measurementState.cancelRequested) return;
        measurementState.progressKind = 'sweep';
        measurementState.progressText = text;
    }

    function clearProgress() {
        const measurementState = deps.getState().measurement;
        if (measurementState.progressKind !== 'sweep') return;
        measurementState.progressKind = '';
        measurementState.progressText = '';
    }

    function failureText(label, error) {
        const reason = String(error?.message || '').replace(/^Measurement failed\.?:?\s*/i, '').trim();
        return reason ? `${label} failed: ${reason}` : `${label} failed.`;
    }

    async function startHostMeasurement(jobGeneration = deps.getState().measurement.jobGeneration) {
        if (!deps.requireConcreteFilterBank()) return;
        if (!deps.getState().measurement.hostCaptureAvailable || !deps.getState().measurement.selectedInputId) {
            deps.getState().measurement.statusText = 'No usable capture input. Select one in Setup.';
            deps.renderMeasurementPanel();
            deps.showToast('No capture input', 'error');
            return;
        }

        await deps.flushSubwooferSettingsBeforeMeasurement();

        const formData = new (deps.getFormDataType())();
        formData.append('input_id', deps.getState().measurement.selectedInputId);
        formData.append('input_key', deps.getState().measurement.selectedInputKey || '');
        // The selected area decides the sweep side and the frozen measurement
        // target. A stereo area (an L/R pair bank or Global) sweeps the chosen
        // side; mono areas always sweep both inputs at once. There is no
        // separate channel selector beyond the area side choice.
        const area = deps.measurementAreaFromCatalog();
        if (area) {
            const side = Array.isArray(area.sides) && area.sides.includes(deps.getState().measurement.sweepSide)
                ? deps.getState().measurement.sweepSide
                : 'stereo';
            formData.append('channel', side);
            formData.append('measurement_bank', area.bank_id);
        }
        formData.append('mic_input_channel', deps.getState().measurement.selectedMicInputChannel || '1');
        deps.appendMeasurementReferenceFields(formData);
        const calibrationFile = deps.getElements().measurementCalibrationFile?.files?.[0];
        if (calibrationFile) {
            formData.append('calibration_file', calibrationFile);
        } else if (deps.getState().measurement.selectedCalibrationRef) {
            formData.append('calibration_ref', deps.getState().measurement.selectedCalibrationRef);
        }

        deps.getState().measurement.activeJobId = '';
        deps.getState().measurement.activeMeasurementKind = 'single';
        deps.getState().measurement.pendingRepeatMeasurements = [];
        deps.getState().measurement.currentMeasurementSaved = false;
        setProgress('Starting…');
        deps.renderMeasurementPanel();
        void deps.postRuntimeDebugSnapshot('ui-before-measurement-start', { measurementKind: 'single' });

        const resp = await deps.fetch('/api/measurements/start', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'could not start'));
        const job = data.job || {};
        deps.getState().measurement.activeJobId = String(job.id || '');
        deps.getState().measurement.activeMeasurementKind = deps.normalizeMeasurementKind(job.job_kind || 'single') || 'single';
        setProgress(deps.formatMeasurementJobStatusText(job, 'Preparing…'));
        deps.renderMeasurementPanel();
        if (deps.getState().measurement.cancelRequested) await deps.cancelMeasurement();
        if (!deps.getState().measurement.activeJobId) return;
        await deps.pollMeasurementJob(deps.getState().measurement.activeJobId, jobGeneration);
    }

    async function startLrRepeatMeasurement(jobGeneration = deps.getState().measurement.jobGeneration) {
        if (!deps.requireConcreteFilterBank()) return;
        if (!deps.getState().measurement.hostCaptureAvailable || !deps.getState().measurement.selectedInputId) {
            deps.getState().measurement.statusText = 'No usable capture input. Select one in Setup.';
            deps.renderMeasurementPanel();
            deps.showToast('No capture input', 'error');
            return;
        }
        await deps.flushSubwooferSettingsBeforeMeasurement();
        const formData = new (deps.getFormDataType())();
        formData.append('input_id', deps.getState().measurement.selectedInputId);
        formData.append('input_key', deps.getState().measurement.selectedInputKey || '');
        formData.append('base_name', deps.getState().measurement.currentMeasurementName || '');
        // The repeat freezes the selected area and gives each of its internal
        // two-sided sweeps the matching output mask.
        const area = deps.measurementAreaFromCatalog();
        if (area) formData.append('measurement_bank', area.bank_id);
        formData.append('mic_input_channel', deps.getState().measurement.selectedMicInputChannel || '1');
        deps.appendMeasurementReferenceFields(formData);
        const calibrationFile = deps.getElements().measurementCalibrationFile?.files?.[0];
        if (calibrationFile) {
            formData.append('calibration_file', calibrationFile);
        } else if (deps.getState().measurement.selectedCalibrationRef) {
            formData.append('calibration_ref', deps.getState().measurement.selectedCalibrationRef);
        }
        deps.getState().measurement.activeJobId = '';
        deps.getState().measurement.activeMeasurementKind = 'lr_repeat';
        deps.getState().measurement.pendingRepeatMeasurements = [];
        deps.getState().measurement.repeatJobActive = true;
        setProgress('Starting…');
        deps.renderMeasurementPanel();
        void deps.postRuntimeDebugSnapshot('ui-before-measurement-start', { measurementKind: 'lr-repeat' });
        const resp = await deps.fetch('/api/measurements/lr-repeat/start', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'could not start'));
        const job = data.job || {};
        deps.getState().measurement.activeJobId = String(job.id || '');
        deps.getState().measurement.activeMeasurementKind = deps.normalizeMeasurementKind(job.job_kind || 'lr-repeat') || 'lr_repeat';
        setProgress(deps.formatMeasurementJobStatusText(job, 'Preparing…'));
        deps.renderMeasurementPanel();
        if (deps.getState().measurement.cancelRequested) await deps.cancelMeasurement();
        if (!deps.getState().measurement.activeJobId) return;
        await deps.pollMeasurementJob(deps.getState().measurement.activeJobId, jobGeneration);
    }

    async function startMeasurement() {
        if (deps.getState().measurement.startInFlight || deps.getState().measurement.activeJobId) return;
        if (!deps.measurementModeReady()) {
            deps.getState().measurement.statusText = 'No usable capture input. Select one in Setup.';
            deps.renderMeasurementPanel();
            deps.showToast('No capture input', 'error');
            return;
        }

        const jobGeneration = Number(deps.getState().measurement.jobGeneration || 0) + 1;
        deps.getState().measurement.jobGeneration = jobGeneration;
        deps.getState().measurement.startInFlight = true;
        deps.getState().measurement.cancelRequested = false;
        deps.getState().measurement.repeatJobActive = false;
        deps.getState().measurement.activeMeasurementKind = '';
        deps.getState().measurement.activeJobId = '';
        deps.getState().measurement.currentMeasurementSaved = false;
        deps.getState().measurement.statusText = '';
        deps.renderMeasurementPanel();

        try {
            await startHostMeasurement(jobGeneration);
        } catch (error) {
            if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
            console.error('startMeasurement failed', error);
            deps.getState().measurement.statusText = failureText('Sweep', error);
            deps.showToast('Sweep failed', 'error');
        } finally {
            if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
            clearProgress();
            deps.getState().measurement.startInFlight = false;
            if (deps.getState().measurement.jobGeneration === jobGeneration && !deps.getState().measurement.activeJobId) {
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.repeatJobActive = false;
                deps.getState().measurement.cancelRequested = false;
            }
            deps.renderMeasurementPanel();
        }
    }

    async function startLrRepeat() {
        if (deps.getState().measurement.startInFlight || deps.getState().measurement.activeJobId) return;
        const repeatBlockedReason = deps.measurementRepeatBlockedReason();
        if (repeatBlockedReason) {
            deps.getState().measurement.statusText = repeatBlockedReason;
            deps.renderMeasurementPanel();
            deps.showToast('L/R repeat unavailable', 'warning');
            return;
        }
        if (!deps.measurementModeReady()) {
            deps.getState().measurement.statusText = 'No usable capture input. Select one in Setup.';
            deps.renderMeasurementPanel();
            deps.showToast('No capture input', 'error');
            return;
        }
        const jobGeneration = Number(deps.getState().measurement.jobGeneration || 0) + 1;
        deps.getState().measurement.jobGeneration = jobGeneration;
        deps.getState().measurement.startInFlight = true;
        deps.getState().measurement.cancelRequested = false;
        deps.getState().measurement.activeMeasurementKind = '';
        deps.getState().measurement.repeatJobActive = true;
        deps.getState().measurement.statusText = '';
        deps.renderMeasurementPanel();
        try {
            await startLrRepeatMeasurement(jobGeneration);
        } catch (error) {
            if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
            console.error('startLrRepeat failed', error);
            deps.getState().measurement.statusText = failureText('L/R repeat', error);
            deps.showToast('L/R repeat failed', 'error');
        } finally {
            if (deps.getState().measurement.jobGeneration !== jobGeneration) return;
            clearProgress();
            deps.getState().measurement.startInFlight = false;
            if (deps.getState().measurement.jobGeneration === jobGeneration && !deps.getState().measurement.activeJobId) {
                deps.getState().measurement.activeMeasurementKind = '';
                deps.getState().measurement.repeatJobActive = false;
                deps.getState().measurement.cancelRequested = false;
            }
            deps.renderMeasurementPanel();
        }
    }

    return {
        init,
        startHostMeasurement, startLrRepeatMeasurement, startMeasurement, startLrRepeat,
    };
});
