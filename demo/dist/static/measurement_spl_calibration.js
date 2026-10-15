// SPDX-License-Identifier: AGPL-3.0-only
/** SPL calibration panel: noise, automatic measurement and apply. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementSplCalibration = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getElements: () => ({}),
        fetch: (...args) => root.fetch(...args),
        fetchEffects: async () => {},
        openModal: (panel, options) => root.FXRouteModal?.open(panel, options),
        closeModal: (panel) => root.FXRouteModal?.close(panel),
        setTimeout: (callback, ms) => root.setTimeout(callback, ms),
        clearTimeout: (timer) => root.clearTimeout(timer),
    };
    let splCalibrationNoiseActive = false;
    let splCalibrationAutomaticAvailable = false;
    let splCalibrationAutomaticRunning = false;
    let splCalibrationStopFailed = false;
    let splCalibrationRestorePending = null;
    let splCalibrationStartRequest = null;
    let splCalibrationStopRequest = null;
    let splCalibrationApplying = false;
    let splCalibrationApplyController = null;
    let splCalibrationOperationGeneration = 0;
    let splCalibrationModeText = '';

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    // Same contract as the measurement panel: the line next to the noise
    // button shows the mode or the running action, the bottom status line
    // only the outcome, warning or error.
    function setSplCalibrationActionLine(text = '') {
        const element = deps.getElements().splCalibrationAutoStatus;
        if (element) element.textContent = text || splCalibrationModeText;
    }

    function setSplCalibrationStatus(text = '') {
        const element = deps.getElements().splCalibrationStatus;
        if (element) element.textContent = text;
    }

    function formatSignedDb(value) {
        const number = Number(value);
        return `${number >= 0 ? '+' : ''}${number.toFixed(1)} dB`;
    }

    function splCalibrationModeLabel(data) {
        return data.automatic?.available
            ? `Automatic SPL measurement: ${data.automatic.microphone_model} detected`
            : 'Manual SPL measurement';
    }

    function resetSplCalibrationNoiseButton() {
        if (!deps.getElements().splCalibrationNoise) return;
        deps.getElements().splCalibrationNoise.disabled = !!splCalibrationStopRequest || splCalibrationApplying;
        deps.getElements().splCalibrationNoise.textContent = splCalibrationStopFailed
            ? 'Retry stop'
            : (splCalibrationRestorePending ? 'Retry restore' : (splCalibrationNoiseActive ? 'Stop noise' : 'Start noise'));
    }

    // The operation ended, but its audio restore is still pending.
    function splCalibrationRestorePendingLine() {
        return splCalibrationRestorePending?.output_muted === false
            ? 'Audio not restored. Retry restore.'
            : 'Audio not restored. Output muted. Retry restore.';
    }

    function applySplCalibrationServerState(data) {
        splCalibrationNoiseActive = !!data.noise_active;
        splCalibrationStopFailed = !!data.operation_active && !data.noise_active;
        splCalibrationRestorePending = !data.operation_active && data.restore_pending ? data.restore_pending : null;
    }

    function setSplCalibrationRestoreActionLine() {
        setSplCalibrationActionLine(splCalibrationRestorePending ? splCalibrationRestorePendingLine() : '');
    }

    async function runSplCalibrationNoiseCountdown(generation) {
        for (const count of [3, 2, 1]) {
            if (generation !== splCalibrationOperationGeneration) return false;
            if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = `Starting noise: ${count}`;
            await new Promise((resolve) => deps.setTimeout(resolve, 1000));
        }
        return generation === splCalibrationOperationGeneration;
    }

    async function requestSplCalibration(url, options = {}, controller = new root.AbortController()) {
        let timer;
        try {
            return await Promise.race([
                deps.fetch(url, Object.assign({}, options, { signal: controller.signal })).then(async (response) => {
                    const data = await response.json().catch((error) => {
                        if (response.ok) throw error;
                        return {};
                    });
                    return { response, data };
                }),
                new Promise((_, reject) => {
                    timer = deps.setTimeout(() => {
                        controller.abort();
                        reject(new Error('SPL request timed out. Retry stop.'));
                    }, 45000);
                }),
            ]);
        } finally {
            deps.clearTimeout(timer);
        }
    }

    async function requestSplCalibrationStart(url, options) {
        const request = requestSplCalibration(url, options);
        splCalibrationStartRequest = request;
        try {
            return await request;
        } finally {
            if (splCalibrationStartRequest === request) {
                splCalibrationStartRequest = null;
            }
        }
    }

    async function requestSplCalibrationStop() {
        const { response, data } = await requestSplCalibration('/api/measurements/spl-calibration/noise', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: false }),
        });
        if (!response.ok) {
            throw new Error(data.detail || 'Failed to stop SPL calibration');
        }
    }

    async function stopSplCalibrationOperation(statusText = '') {
        if (splCalibrationStopRequest) return splCalibrationStopRequest;
        ++splCalibrationOperationGeneration;
        splCalibrationAutomaticRunning = false;
        if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.disabled = true;
        const startRequest = splCalibrationStartRequest;
        const stopRequest = (async () => {
            if (startRequest) {
                // Interrupt a running capture, then cover a start arriving after that stop.
                await requestSplCalibrationStop().catch(() => null);
                const failure = await startRequest.then(() => null, (error) => error);
                await requestSplCalibrationStop();
                if (failure) throw failure;
                return;
            }
            await requestSplCalibrationStop();
        })();
        splCalibrationStopRequest = stopRequest;
        try {
            await stopRequest;
            splCalibrationNoiseActive = false;
            splCalibrationStopFailed = false;
            splCalibrationRestorePending = null;
            setSplCalibrationActionLine();
            if (statusText) setSplCalibrationStatus(statusText);
        } catch (error) {
            // A failed stop may still have ended the operation with only its
            // audio restore pending (which also refuses a late start).
            splCalibrationStopFailed = true;
            try {
                const { response, data } = await requestSplCalibration('/api/measurements/spl-calibration');
                if (response.ok && !data.operation_active && data.restore_pending) applySplCalibrationServerState(data);
            } catch (_) {
                // Keep a stop retry available while server state is unknown.
            }
            if (splCalibrationStopFailed) setSplCalibrationActionLine('Cleanup pending. Retry stop.');
            else setSplCalibrationRestoreActionLine();
            throw error;
        } finally {
            splCalibrationStopRequest = null;
            resetSplCalibrationNoiseButton();
        }
    }

    async function openSplCalibration() {
        const generation = splCalibrationOperationGeneration;
        setSplCalibrationStatus();
        deps.getElements().splCalibrationPanel?.classList.remove('hidden');
        deps.openModal(deps.getElements().splCalibrationPanel, {
            initialFocus: deps.getElements().splCalibrationNoise,
            onEscape: () => { void closeSplCalibration(); },
        });
        try {
            const { response, data } = await requestSplCalibration('/api/measurements/spl-calibration');
            if (generation !== splCalibrationOperationGeneration) return;
            if (!response.ok) throw new Error(data.detail || 'Failed to load SPL calibration');
            applySplCalibrationServerState(data);
            splCalibrationAutomaticAvailable = !!data.automatic?.available;
            resetSplCalibrationNoiseButton();
            splCalibrationModeText = splCalibrationModeLabel(data);
            setSplCalibrationRestoreActionLine();
            if (splCalibrationRestorePending?.detail) setSplCalibrationStatus(splCalibrationRestorePending.detail);
        } catch (error) {
            if (generation === splCalibrationOperationGeneration) setSplCalibrationStatus(error.message);
        }
    }

    async function closeSplCalibration() {
        splCalibrationApplyController?.abort();
        splCalibrationApplyController = null;
        splCalibrationApplying = false;
        if (deps.getElements().splCalibrationSave) deps.getElements().splCalibrationSave.disabled = false;
        try {
            await stopSplCalibrationOperation();
        } catch (error) {
            setSplCalibrationStatus(error.message);
            return;
        }
        deps.getElements().splCalibrationPanel?.classList.add('hidden');
        deps.closeModal(deps.getElements().splCalibrationPanel);
    }

    async function toggleSplCalibrationNoise() {
        if (splCalibrationApplying) return;
        if (splCalibrationStopRequest) {
            await splCalibrationStopRequest.catch((error) => setSplCalibrationStatus(error.message));
            return;
        }
        if (splCalibrationAutomaticRunning) {
            await stopSplCalibrationOperation('Automatic SPL measurement cancelled.').catch((error) => {
                setSplCalibrationStatus(error.message);
            });
            return;
        }
        const next = !splCalibrationNoiseActive && !splCalibrationStopFailed && !splCalibrationRestorePending;
        if (!next) {
            const stoppedText = splCalibrationRestorePending ? 'Audio restored.' : 'Noise stopped. Volume restored.';
            await stopSplCalibrationOperation(stoppedText).catch((error) => {
                setSplCalibrationStatus(error.message);
            });
            return;
        }
        const generation = ++splCalibrationOperationGeneration;
        if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.disabled = true;
        setSplCalibrationStatus();
        try {
            if (!await runSplCalibrationNoiseCountdown(generation)) return;
            if (splCalibrationAutomaticAvailable) {
                splCalibrationAutomaticRunning = true;
                if (deps.getElements().splCalibrationNoise) {
                    deps.getElements().splCalibrationNoise.disabled = false;
                    deps.getElements().splCalibrationNoise.textContent = 'Cancel measurement';
                }
                setSplCalibrationActionLine('Measuring SPL…');
                const { response, data } = await requestSplCalibrationStart('/api/measurements/spl-calibration/automatic', { method: 'POST' });
                if (generation !== splCalibrationOperationGeneration) return;
                if (!response.ok) throw new Error(data.detail || 'Automatic UMIK SPL measurement failed');
                if (deps.getElements().splCalibrationMeasured) {
                    deps.getElements().splCalibrationMeasured.value = Number(data.measured_spl_db).toFixed(1);
                }
                splCalibrationNoiseActive = false;
                if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = 'Start noise';
                setSplCalibrationActionLine();
                setSplCalibrationStatus(`${data.microphone_model} measured ${Number(data.measured_spl_db).toFixed(1)} dB SPL · Loudness offset ${formatSignedDb(data.required_adjustment_db)}. Save / Apply to use it.`);
                return;
            }
            const { response, data } = await requestSplCalibrationStart('/api/measurements/spl-calibration/noise', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: true }),
            });
            if (generation !== splCalibrationOperationGeneration) return;
            if (!response.ok) throw new Error(data.detail || 'Calibration noise failed');
            splCalibrationNoiseActive = true;
            if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = 'Stop noise';
            setSplCalibrationActionLine('Noise playing. Read the C/Slow meter after about 1 s, average over about 3 s.');
        } catch (error) {
            if (generation === splCalibrationOperationGeneration) {
                // A lost start response does not prove the server stayed idle.
                splCalibrationStopFailed = true;
                try {
                    const { response, data } = await requestSplCalibration('/api/measurements/spl-calibration');
                    if (generation !== splCalibrationOperationGeneration) return;
                    if (response.ok) applySplCalibrationServerState(data);
                } catch (_) {
                    // Keep a stop retry available while server state is unknown.
                }
                if (generation !== splCalibrationOperationGeneration) return;
                setSplCalibrationRestoreActionLine();
                setSplCalibrationStatus(error.message);
            }
        } finally {
            if (generation === splCalibrationOperationGeneration) {
                splCalibrationAutomaticRunning = false;
                resetSplCalibrationNoiseButton();
            }
        }
    }

    async function saveSplCalibration() {
        if (splCalibrationApplying) return;
        const measured = Number(deps.getElements().splCalibrationMeasured?.value);
        if (!Number.isFinite(measured)) {
            setSplCalibrationStatus('Enter the measured C/Slow SPL value.');
            return;
        }
        splCalibrationApplying = true;
        const controller = new root.AbortController();
        splCalibrationApplyController = controller;
        resetSplCalibrationNoiseButton();
        if (deps.getElements().splCalibrationSave) deps.getElements().splCalibrationSave.disabled = true;
        let generation;
        try {
            await stopSplCalibrationOperation();
            if (controller.signal.aborted) return;
            generation = splCalibrationOperationGeneration;
            const { response, data } = await requestSplCalibration('/api/measurements/spl-calibration/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ measured_spl_db: measured }),
            }, controller);
            if (generation !== splCalibrationOperationGeneration) return;
            if (!response.ok) throw new Error(data.detail || 'Failed to apply SPL calibration');
            splCalibrationNoiseActive = false;
            if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = 'Start noise';
            setSplCalibrationActionLine();
            const offsetText = `Saved: ${measured.toFixed(1)} dB SPL measured · Loudness offset ${formatSignedDb(data.required_adjustment_db)}`;
            setSplCalibrationStatus(data.calibrated
                ? `${offsetText} · within 1 dB of target.`
                : `${offsetText}. Only affects playback with Loudness on.`);
            await deps.fetchEffects();
        } catch (error) {
            if (splCalibrationApplyController === controller && (generation === undefined || generation === splCalibrationOperationGeneration)) setSplCalibrationStatus(error.message);
        } finally {
            if (splCalibrationApplyController === controller) {
                splCalibrationApplyController = null;
                splCalibrationApplying = false;
                resetSplCalibrationNoiseButton();
                if (deps.getElements().splCalibrationSave) deps.getElements().splCalibrationSave.disabled = false;
            }
        }
    }

    return {
        init,
        splCalibrationModeLabel, resetSplCalibrationNoiseButton, runSplCalibrationNoiseCountdown, stopSplCalibrationOperation, openSplCalibration, closeSplCalibration, toggleSplCalibrationNoise, saveSplCalibration,
    };
});
