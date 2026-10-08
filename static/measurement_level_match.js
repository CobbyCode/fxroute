// SPDX-License-Identifier: AGPL-3.0-only
/** Level Matching panel: mode-wide logical speaker levels and output trims. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementLevelMatch = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const POLL_MS = 600;

    let deps = {
        getElements: () => ({}),
        getState: () => ({ measurement: {} }),
        fetch: (...args) => root.fetch(...args),
        openModal: (panel, options) => root.FXRouteModal?.open(panel, options),
        closeModal: (panel) => root.FXRouteModal?.close(panel),
        measurementArea: () => null,
        ensureAreaCatalog: async () => {},
        normalizeMeasurementInputChannelSelections: () => {},
        applyMutation: async () => null,
        hasActiveMeasurementJob: () => false,
        escapeHtml: (value) => String(value),
        roleLabel: (role) => String(role),
        sleep: (ms) => new Promise((resolve) => root.setTimeout(resolve, ms)),
    };
    let busy = false;
    let resetting = false;
    let jobId = '';
    let generation = 0;
    let lastStatus = null;
    let refreshSerial = 0;

    function init(overrides) {
        refreshSerial += 1;
        deps = Object.assign(deps, overrides || {});
    }

    function setActionLine(text = '') {
        const element = deps.getElements().levelMatchActionStatus;
        if (element) element.textContent = text;
    }

    function setStatus(text = '') {
        const element = deps.getElements().levelMatchStatus;
        if (element) element.textContent = text;
    }

    function formatTrim(value) {
        if (!Number.isFinite(value)) return '—';
        const rounded = Math.round(value * 10) / 10;
        return rounded === 0 ? '0.0 dB' : `${rounded.toFixed(1)} dB`;
    }

    function renderResults(data) {
        const element = deps.getElements().levelMatchResults;
        if (!element) return;
        const rows = Array.isArray(data?.entries) ? data.entries : [];
        if (!rows.length) {
            element.innerHTML = '';
            return;
        }
        const mode = data.mode || '';
        element.innerHTML = rows.map((row) => {
            const state = row.valid ? '' : ' <span class="level-match-outdated">outdated</span>';
            return `<div class="level-match-row"><span class="level-match-role">${deps.escapeHtml(deps.roleLabel(row.role, mode))}${state}</span>`
                + `<span class="level-match-trim">${row.valid ? formatTrim(row.trim_db) : '—'}</span></div>`;
        }).join('');
    }

    function currentArea() {
        const area = deps.measurementArea();
        return area && area.available !== false ? area : null;
    }

    function renderControls() {
        const elements = deps.getElements();
        const area = deps.measurementArea();
        const mode = lastStatus?.mode || deps.getState().outputSystem?.catalog?.active_mode;
        if (elements.levelMatchArea) elements.levelMatchArea.textContent = `Output mode · ${root.FXRouteOutputState?.modeLabel(mode) || mode || 'Stereo'}`;
        const measuring = busy;
        const blocked = !measuring && deps.hasActiveMeasurementJob();
        const waiting = !!lastStatus && lastStatus.requires_reference && !lastStatus.reference_ready;
        if (elements.levelMatchStart) {
            elements.levelMatchStart.textContent = measuring ? 'Cancel' : 'Match levels';
            elements.levelMatchStart.disabled = !measuring && (!currentArea() || waiting || blocked || resetting);
        }
        if (elements.levelMatchReset) elements.levelMatchReset.disabled = measuring || resetting || blocked || !currentArea();
        if (!measuring) {
            if (!currentArea()) setActionLine('Select a speaker bank for level matching.');
            else if (waiting) setActionLine('Measure Front L/R first to set the level reference.');
            else if (blocked) setActionLine('Another measurement is running.');
            else setActionLine(lastStatus?.units?.length ? `Measures ${lastStatus.units.join(' and ')} one after the other.` : '');
        }
    }

    async function refresh() {
        const serial = ++refreshSerial;
        const area = currentArea();
        const mode = deps.getState().outputSystem?.catalog?.active_mode;
        const panel = deps.getElements().levelMatchPanel;
        const current = () => {
            const liveArea = currentArea();
            return serial === refreshSerial && panel === deps.getElements().levelMatchPanel
                && mode === deps.getState().outputSystem?.catalog?.active_mode
                && area?.bank_id === liveArea?.bank_id && area?.group === liveArea?.group;
        };
        renderControls();
        if (!area) {
            lastStatus = null;
            renderResults(null);
            renderControls();
            return;
        }
        try {
            const response = await deps.fetch(`/api/measurements/level-match?bank_id=${encodeURIComponent(area.bank_id)}`);
            const data = await response.json().catch(() => ({}));
            if (!current()) return;
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Level Matching is unavailable');
            lastStatus = data;
            renderResults(data);
        } catch (error) {
            if (!current()) return;
            lastStatus = null;
            renderResults(null);
            setStatus(error.message);
        }
        renderControls();
    }

    async function openLevelMatch() {
        setStatus();
        await deps.ensureAreaCatalog();
        deps.getElements().levelMatchPanel?.classList.remove('hidden');
        deps.openModal(deps.getElements().levelMatchPanel, {
            initialFocus: deps.getElements().levelMatchStart,
            onEscape: () => { void closeLevelMatch(); },
        });
        await refresh();
    }

    async function closeLevelMatch() {
        refreshSerial += 1;
        if (busy) await cancelLevelMatch();
        deps.getElements().levelMatchPanel?.classList.add('hidden');
        deps.closeModal(deps.getElements().levelMatchPanel);
    }

    function buildForm(area) {
        deps.normalizeMeasurementInputChannelSelections();
        const measurement = deps.getState().measurement;
        if (!measurement.selectedInputId) throw new Error('Select a measurement input first');
        const form = new FormData();
        form.append('input_id', measurement.selectedInputId);
        form.append('input_key', measurement.selectedInputKey || '');
        form.append('mic_input_channel', measurement.selectedMicInputChannel || '1');
        form.append('calibration_ref', measurement.selectedCalibrationRef || '');
        form.append('bank_id', area.bank_id);
        return form;
    }

    function progressText(job) {
        const progress = job?.progress || {};
        if (!progress.total) return 'Starting…';
        return `Measuring ${progress.label || ''} (${progress.step} of ${progress.total})…`.replace('  ', ' ');
    }

    async function pollJob(id, token) {
        while (token === generation) {
            const response = await deps.fetch(`/api/measurements/level-match/jobs/${encodeURIComponent(id)}`);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Level Matching job lost');
            const job = data.job || {};
            if (job.status !== 'running') return job;
            setActionLine(progressText(job));
            await deps.sleep(POLL_MS);
        }
        return null;
    }

    async function startLevelMatch() {
        const area = currentArea();
        if (!area || busy) return;
        const token = ++generation;
        busy = true;
        setStatus();
        renderControls();
        try {
            const response = await deps.fetch('/api/measurements/level-match/start', { method: 'POST', body: buildForm(area) });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Failed to start Level Matching');
            jobId = String(data.job?.id || '');
            setActionLine(progressText(data.job));
            const job = await pollJob(jobId, token);
            if (!job) return;
            if (job.status === 'cancelled') {
                setStatus('Level Matching cancelled. Nothing was changed.');
            } else if (job.status !== 'completed') {
                throw new Error(job.error || 'Level Matching failed');
            } else {
                setActionLine('Applying…');
                const applied = await deps.applyMutation('set_level_calibration',
                    { mode: job.result.mode, values: job.result.values }, false, { quiet: true });
                setStatus(applied
                    ? 'Level Calibration applied. Re-measuring overwrites it.'
                    : 'The measurement finished but the levels could not be applied.');
            }
        } catch (error) {
            setStatus(error.message);
        } finally {
            if (token === generation) {
                busy = false;
                jobId = '';
                await refresh();
            }
        }
    }

    async function resetLevelMatch() {
        const area = currentArea();
        if (!area || busy || resetting) return;
        resetting = true;
        setStatus();
        renderControls();
        try {
            const form = new FormData();
            form.append('bank_id', area.bank_id);
            const response = await deps.fetch('/api/measurements/level-match/reset', { method: 'POST', body: form });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Failed to reset Level Matching');
            const applied = await deps.applyMutation('set_level_calibration',
                { mode: data.mode, values: data.values }, false, { quiet: true });
            setStatus(applied
                ? 'Level Calibration reset to 0 dB.'
                : 'The stored levels were cleared but 0 dB could not be applied. Press Reset again.');
        } catch (error) {
            setStatus(error.message);
        } finally {
            resetting = false;
            await refresh();
        }
    }

    async function cancelLevelMatch() {
        if (!busy || !jobId) return;
        setActionLine('Cancelling…');
        try {
            await deps.fetch(`/api/measurements/level-match/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
        } catch (error) {
            setStatus(error.message);
        }
    }

    function bindEvents() {
        const elements = deps.getElements();
        elements.levelMatchOpen?.addEventListener('click', () => { void openLevelMatch(); });
        elements.levelMatchClose?.addEventListener('click', () => { void closeLevelMatch(); });
        elements.levelMatchPanel?.querySelector('.manage-overlay-backdrop')
            ?.addEventListener('click', () => { void closeLevelMatch(); });
        elements.levelMatchStart?.addEventListener('click', () => {
            if (busy) void cancelLevelMatch();
            else void startLevelMatch();
        });
        elements.levelMatchReset?.addEventListener('click', () => { void resetLevelMatch(); });
    }

    return { init, bindEvents, openLevelMatch, closeLevelMatch, startLevelMatch, cancelLevelMatch, resetLevelMatch, refresh, formatTrim };
});
