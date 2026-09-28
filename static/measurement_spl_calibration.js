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
    };
    let splCalibrationNoiseActive = false;
    let splCalibrationAutomaticAvailable = false;
    let splCalibrationAutomaticRunning = false;
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
        deps.getElements().splCalibrationNoise.disabled = false;
        deps.getElements().splCalibrationNoise.textContent = splCalibrationNoiseActive ? 'Stop noise' : 'Start noise';
    }

    async function runSplCalibrationNoiseCountdown(generation) {
        for (const count of [3, 2, 1]) {
            if (generation !== splCalibrationOperationGeneration) return false;
            if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = `Starting noise: ${count}`;
            await new Promise((resolve) => deps.setTimeout(resolve, 1000));
        }
        return generation === splCalibrationOperationGeneration;
    }

    async function stopSplCalibrationOperation(statusText = '') {
        const generation = ++splCalibrationOperationGeneration;
        splCalibrationAutomaticRunning = false;
        if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.disabled = true;
        try {
            const response = await deps.fetch('/api/measurements/spl-calibration/noise', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: false }),
            });
            if (!response.ok) throw new Error('Failed to stop SPL calibration');
        } finally {
            if (generation !== splCalibrationOperationGeneration) return;
            splCalibrationNoiseActive = false;
            resetSplCalibrationNoiseButton();
            setSplCalibrationActionLine();
            if (statusText) setSplCalibrationStatus(statusText);
        }
    }

    async function openSplCalibration() {
        setSplCalibrationStatus();
        deps.getElements().splCalibrationPanel?.classList.remove('hidden');
        deps.openModal(deps.getElements().splCalibrationPanel, {
            initialFocus: deps.getElements().splCalibrationNoise,
            onEscape: () => { void closeSplCalibration(); },
        });
        try {
            const response = await deps.fetch('/api/measurements/spl-calibration');
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || 'Failed to load SPL calibration');
            splCalibrationNoiseActive = !!data.noise_active;
            splCalibrationAutomaticAvailable = !!data.automatic?.available;
            if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = splCalibrationNoiseActive ? 'Stop noise' : 'Start noise';
            splCalibrationModeText = splCalibrationModeLabel(data);
            setSplCalibrationActionLine();
        } catch (error) {
            setSplCalibrationStatus(error.message);
        }
    }

    async function closeSplCalibration() {
        await stopSplCalibrationOperation().catch(() => null);
        deps.getElements().splCalibrationPanel?.classList.add('hidden');
        deps.closeModal(deps.getElements().splCalibrationPanel);
    }

    async function toggleSplCalibrationNoise() {
        if (splCalibrationAutomaticRunning) {
            await stopSplCalibrationOperation('Automatic SPL measurement cancelled.').catch((error) => {
                setSplCalibrationStatus(error.message);
            });
            return;
        }
        const next = !splCalibrationNoiseActive;
        if (!next) {
            await stopSplCalibrationOperation('Noise stopped. Volume restored.').catch((error) => {
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
                const response = await deps.fetch('/api/measurements/spl-calibration/automatic', { method: 'POST' });
                const data = await response.json();
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
            const response = await deps.fetch('/api/measurements/spl-calibration/noise', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: true }),
            });
            const data = await response.json();
            if (generation !== splCalibrationOperationGeneration) return;
            if (!response.ok) throw new Error(data.detail || 'Calibration noise failed');
            splCalibrationNoiseActive = true;
            if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.textContent = 'Stop noise';
            setSplCalibrationActionLine('Noise playing. Read the C/Slow meter after about 1 s, average over about 3 s.');
        } catch (error) {
            if (generation === splCalibrationOperationGeneration) {
                setSplCalibrationActionLine();
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
        const measured = Number(deps.getElements().splCalibrationMeasured?.value);
        if (!Number.isFinite(measured)) {
            setSplCalibrationStatus('Enter the measured C/Slow SPL value.');
            return;
        }
        try {
            const response = await deps.fetch('/api/measurements/spl-calibration/apply', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ measured_spl_db: measured }),
            });
            const data = await response.json();
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
            setSplCalibrationStatus(error.message);
        }
    }

    function isSplCalibrationNoiseActive() {
        return splCalibrationNoiseActive;
    }

    function isSplCalibrationAutomaticRunning() {
        return splCalibrationAutomaticRunning;
    }

    function getSplCalibrationOperationGeneration() {
        return splCalibrationOperationGeneration;
    }

    return {
        init,
        splCalibrationModeLabel, resetSplCalibrationNoiseButton, runSplCalibrationNoiseCountdown, stopSplCalibrationOperation, openSplCalibration, closeSplCalibration, toggleSplCalibrationNoise, saveSplCalibration,
        isSplCalibrationNoiseActive, isSplCalibrationAutomaticRunning, getSplCalibrationOperationGeneration,
    };
});
