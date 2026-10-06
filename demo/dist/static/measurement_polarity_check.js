// SPDX-License-Identifier: AGPL-3.0-only
/** Polarity Check panel: measure the selected area, show a verdict per speaker. Diagnostic only. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementPolarityCheck = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const POLL_MS = 600;
    const VERDICT_TEXT = { correct: 'Correct', reversed: 'Reversed', inconclusive: 'Inconclusive' };
    const REASON_TEXT = {
        no_arrival: 'No sweep heard',
        no_signal: 'No sweep heard',
        weak_signal: 'Too quiet',
        unclear_phase: 'No clear peak',
    };
    const REASON_HINT = {
        no_arrival: 'The sweep did not reach the microphone. Check the amplifier, the speaker and the microphone, then repeat.',
        no_signal: 'The sweep did not reach the microphone. Check the amplifier, the speaker and the microphone, then repeat.',
        weak_signal: 'The sweep was too quiet to judge. Raise the volume and repeat.',
        unclear_phase: 'The direct sound has no clear polarity, as when a crossover or panel rotates its phase.',
    };

    let deps = {
        getElements: () => ({}),
        getState: () => ({ measurement: {} }),
        fetch: (...args) => root.fetch(...args),
        openModal: (panel, options) => root.FXRouteModal?.open(panel, options),
        closeModal: (panel) => root.FXRouteModal?.close(panel),
        measurementArea: () => null,
        ensureAreaCatalog: async () => {},
        normalizeMeasurementInputChannelSelections: () => {},
        hasActiveMeasurementJob: () => false,
        escapeHtml: (value) => String(value),
        roleLabel: (role) => String(role),
        sleep: (ms) => new Promise((resolve) => root.setTimeout(resolve, ms)),
    };
    let busy = false;
    let jobId = '';
    let generation = 0;
    let lastStatus = null;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function setActionLine(text = '') {
        const element = deps.getElements().polarityCheckActionStatus;
        if (element) element.textContent = text;
    }

    function setStatus(text = '') {
        const element = deps.getElements().polarityCheckStatus;
        if (element) element.textContent = text;
    }

    function rowLabel(row, mode) {
        if (row.roles.length === 1) return deps.roleLabel(row.roles[0], mode);
        const side = String(row.label || '');
        return `${side.charAt(0).toUpperCase()}${side.slice(1)} (all ways)`;
    }

    function verdictText(row) {
        if (row.verdict === 'inconclusive') return REASON_TEXT[row.reason] || VERDICT_TEXT.inconclusive;
        return VERDICT_TEXT[row.verdict] || VERDICT_TEXT.inconclusive;
    }

    /** One line that tells the user what a result means for the wiring, or why there is none. */
    function summarize(result) {
        const rows = Array.isArray(result?.rows) ? result.rows : [];
        if (result?.all_reversed) {
            return 'All checked speakers read reversed. An inverted microphone or input does that too: check a speaker you know is correct, such as the Front pair, to tell.';
        }
        const hints = [];
        const unclear = rows.filter((row) => row.verdict === 'inconclusive');
        for (const row of unclear) {
            const hint = REASON_HINT[row.reason];
            if (hint && !hints.includes(hint)) hints.push(hint);
        }
        if (rows.some((row) => row.verdict === 'reversed')) {
            hints.unshift('A reversed speaker plays its first pulse inverted. Swap its + and − wires or invert its output polarity, then check again.');
        }
        if (!hints.length) return 'All checked speakers are in phase.';
        return hints.join(' ');
    }

    function renderResults(job, area) {
        const element = deps.getElements().polarityCheckResults;
        if (!element) return;
        const result = job && job.status === 'completed' && job.bank_id === area?.bank_id ? job.result : null;
        const rows = Array.isArray(result?.rows) ? result.rows : [];
        if (!rows.length) {
            element.innerHTML = '';
            return;
        }
        element.innerHTML = rows.map((row) => {
            const note = row.inverted ? ' <span class="polarity-check-note">output inverted</span>' : '';
            return `<div class="polarity-check-row"><span class="polarity-check-role">${deps.escapeHtml(rowLabel(row, result.mode))}${note}</span>`
                + `<span class="polarity-check-verdict polarity-check-verdict-${deps.escapeHtml(row.verdict)}">${deps.escapeHtml(verdictText(row))}</span></div>`;
        }).join('');
    }

    function currentArea() {
        const area = deps.measurementArea();
        return area && area.available !== false ? area : null;
    }

    function renderControls() {
        const elements = deps.getElements();
        const area = deps.measurementArea();
        if (elements.polarityCheckArea) elements.polarityCheckArea.textContent = area?.label || 'Global';
        const measuring = busy;
        const blocked = !measuring && deps.hasActiveMeasurementJob();
        if (elements.polarityCheckStart) {
            elements.polarityCheckStart.textContent = measuring ? 'Cancel' : 'Check polarity';
            elements.polarityCheckStart.disabled = !measuring && (!currentArea() || blocked);
        }
        if (!measuring) {
            if (!currentArea()) setActionLine('Select a speaker bank to check polarity.');
            else if (blocked) setActionLine('Another measurement is running.');
            else setActionLine(lastStatus?.units?.length ? `Measures ${lastStatus.units.join(' and ')} one after the other.` : '');
        }
    }

    async function refresh() {
        const area = currentArea();
        renderControls();
        if (!area) {
            lastStatus = null;
            renderResults(null, null);
            renderControls();
            return;
        }
        try {
            const response = await deps.fetch(`/api/measurements/polarity-check?bank_id=${encodeURIComponent(area.bank_id)}`);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Polarity Check is unavailable');
            lastStatus = data;
            renderResults(data.job, area);
        } catch (error) {
            lastStatus = null;
            renderResults(null, null);
            setStatus(error.message);
        }
        renderControls();
    }

    async function openPolarityCheck() {
        setStatus();
        await deps.ensureAreaCatalog();
        deps.getElements().polarityCheckPanel?.classList.remove('hidden');
        deps.openModal(deps.getElements().polarityCheckPanel, {
            initialFocus: deps.getElements().polarityCheckStart,
            onEscape: () => { void closePolarityCheck(); },
        });
        await refresh();
    }

    async function closePolarityCheck() {
        if (busy) await cancelPolarityCheck();
        deps.getElements().polarityCheckPanel?.classList.add('hidden');
        deps.closeModal(deps.getElements().polarityCheckPanel);
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
            const response = await deps.fetch(`/api/measurements/polarity-check/jobs/${encodeURIComponent(id)}`);
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Polarity Check job lost');
            const job = data.job || {};
            if (job.status !== 'running') return job;
            setActionLine(progressText(job));
            await deps.sleep(POLL_MS);
        }
        return null;
    }

    async function startPolarityCheck() {
        const area = currentArea();
        if (!area || busy) return;
        const token = ++generation;
        busy = true;
        setStatus();
        renderResults(null, null);
        renderControls();
        try {
            const response = await deps.fetch('/api/measurements/polarity-check/start', { method: 'POST', body: buildForm(area) });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Failed to start Polarity Check');
            jobId = String(data.job?.id || '');
            setActionLine(progressText(data.job));
            const job = await pollJob(jobId, token);
            if (!job) return;
            if (job.status === 'cancelled') {
                setStatus('Polarity Check cancelled.');
            } else if (job.status !== 'completed') {
                throw new Error(job.error || 'Polarity Check failed');
            } else {
                renderResults(job, area);
                setStatus(summarize(job.result));
            }
        } catch (error) {
            setStatus(error.message);
        } finally {
            if (token === generation) {
                busy = false;
                jobId = '';
                renderControls();
            }
        }
    }

    async function cancelPolarityCheck() {
        if (!busy || !jobId) return;
        setActionLine('Cancelling…');
        try {
            await deps.fetch(`/api/measurements/polarity-check/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
        } catch (error) {
            setStatus(error.message);
        }
    }

    function bindEvents() {
        const elements = deps.getElements();
        elements.polarityCheckOpen?.addEventListener('click', () => { void openPolarityCheck(); });
        elements.polarityCheckClose?.addEventListener('click', () => { void closePolarityCheck(); });
        elements.polarityCheckPanel?.querySelector('.manage-overlay-backdrop')
            ?.addEventListener('click', () => { void closePolarityCheck(); });
        elements.polarityCheckStart?.addEventListener('click', () => {
            if (busy) void cancelPolarityCheck();
            else void startPolarityCheck();
        });
    }

    return { init, bindEvents, openPolarityCheck, closePolarityCheck, startPolarityCheck, cancelPolarityCheck, refresh, summarize, verdictText };
});
