// SPDX-License-Identifier: AGPL-3.0-only
/** Per-mode electronic AV timing, independent of the selected filter bank. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteAvSync = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';
    const modeLabels = { stereo: 'Stereo', 'stereo-sub': 'Stereo + Sub', surround: 'Surround' };
    let deps = { getElements: () => ({}), fetch: (...args) => root.fetch(...args),
        openModal: (panel, options) => root.FXRouteModal?.open(panel, options),
        closeModal: panel => root.FXRouteModal?.close(panel), refreshCatalog: async () => {},
        closeCalibrationMenu() {}, hasActiveMeasurementJob: () => false,
        roleLabel: role => role, setTimeout: (...args) => root.setTimeout(...args),
        clearTimeout: id => root.clearTimeout(id) };
    let opened = false, dirty = false, saving = false, sequence = 0, timer = null;
    let mode = '', revision = -1;
    let feedback = '';
    const ms = value => Number.isFinite(value) ? `${value.toFixed(2)} ms` : '—';
    const escape = value => String(value).replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

    function init(overrides) { deps = { ...deps, ...overrides }; }
    function status(text) { const element = deps.getElements().avSyncStatus; if (element) element.textContent = text; }
    function render(data) {
        const elements = deps.getElements(), latency = data.latency || {};
        elements.avSyncMode.textContent = modeLabels[data.active_mode] || data.active_mode;
        elements.avSyncActual.textContent = latency.ready ? ms(latency.actual_ms) : 'Waiting for audio engine…';
        elements.avSyncLimits.textContent = latency.ready
            ? `Available range: ${ms(latency.minimum_ms)} – ${ms(latency.maximum_ms)}` : '';
        elements.avSyncBasis.textContent = ({ calibrated: 'Measured calibration basis stored.',
            adopted: 'Existing alignment preserved; current DSP timing adopted. Recalibrate for a measured basis.',
            unavailable: 'Calibration basis unavailable. Existing alignment is preserved without guessing its DSP timing.' })[data.settings?.basis_origin] || '';
        let warning = '';
        if (!latency.ready) warning = 'The target is stored, but live timing is unavailable.';
        else if (latency.balance_unavailable) warning = 'IR timing is unknown. Automatic speaker balancing is unavailable; the reported delay is not guaranteed.';
        else if (latency.balance_limited) warning = 'Speaker compensation exceeds the shared 500 ms delay capacity. Recalibrate or reduce DSP latency.';
        else if (data.settings.target_ms < latency.minimum_ms - 0.02) warning = 'No additional delay applied – target is below the minimum electronic delay.';
        else if (data.settings.target_ms > latency.maximum_ms + 0.02) warning = 'Target exceeds the shared 500 ms delay capacity. The effective delay shows the achievable value.';
        else if (latency.outputs?.some(row => row.mixed)) warning = 'Mixed paths have different timing; one output delay cannot align every send.';
        status(feedback || warning);
        const rows = latency.ready ? latency.outputs || [] : [];
        elements.avSyncDetails.innerHTML = latency.ready ? `<p class="av-sync-summary">Input ${ms(latency.input_ms)} · Common ${ms(latency.common_ms)}</p>
            <div class="av-sync-table-wrap"><table class="av-sync-table"><thead><tr><th>Speaker</th><th>DSP</th><th>Intentional</th><th>Align</th><th>Balance</th><th>Total</th></tr></thead><tbody>${rows.map(row =>
                `<tr><th>${escape(deps.roleLabel(row.role, mode))}</th>${['dsp', 'intentional', 'align', 'balance', 'total'].map(key => `<td>${ms(row[`${key}_ms`])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : '';
        elements.avSyncSave.disabled = saving || revision < 0 || deps.hasActiveMeasurementJob();
    }
    async function refresh() {
        if (!opened || saving) return;
        const request = ++sequence;
        try {
            const response = await deps.fetch('/api/audio/av-sync');
            const data = await response.json();
            if (request !== sequence || !opened) return;
            if (!response.ok) throw new Error(data.detail?.message || data.detail || 'AV Sync is unavailable');
            const changedMode = mode && mode !== data.active_mode;
            if (changedMode) { dirty = false; feedback = ''; }
            mode = data.active_mode; revision = data.revision;
            if (!dirty) deps.getElements().avSyncTarget.value = String(data.settings.target_ms);
            render(data);
            if (changedMode) status('Output mode changed. The target for this mode is shown.');
        } catch (error) {
            if (request === sequence && opened) status(error.message);
        } finally {
            if (opened && request === sequence) {
                deps.clearTimeout(timer);
                timer = deps.setTimeout(() => { void refresh(); }, 1000);
            }
        }
    }
    async function open() {
        deps.closeCalibrationMenu();
        opened = true; dirty = false; mode = ''; revision = -1; feedback = '';
        const elements = deps.getElements();
        elements.avSyncSave.disabled = true;
        elements.avSyncPanel.classList.remove('hidden');
        deps.openModal(elements.avSyncPanel, { initialFocus: elements.avSyncTarget, onEscape: close });
        await refresh();
    }
    function close() {
        opened = false; sequence += 1; deps.clearTimeout(timer);
        const panel = deps.getElements().avSyncPanel;
        panel.classList.add('hidden'); deps.closeModal(panel);
    }
    async function save() {
        const raw = deps.getElements().avSyncTarget.value.trim(), target = Number(raw);
        if (!raw || !Number.isFinite(target) || target < 0 || target > 20000) {
            feedback = 'Enter a target from 0 to 20000 ms.';
            status(feedback); return;
        }
        if (saving || revision < 0 || deps.hasActiveMeasurementJob()) return;
        const savedMode = mode;
        feedback = '';
        saving = true; sequence += 1; deps.getElements().avSyncSave.disabled = true;
        try {
            const response = await deps.fetch('/api/audio/output-state/apply', { method: 'POST',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: revision,
                    mutation: { kind: 'set_av_sync', mode: savedMode, target_ms: target } }) });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail?.message || data.detail || 'AV Sync target could not be saved');
            dirty = false;
            await deps.refreshCatalog();
            saving = false;
            await refresh();
            if (opened && mode === savedMode && !data.live_applied) status('Target saved. It will apply when this output mode is active.');
        } catch (error) {
            saving = false;
            feedback = error.message;
            await refresh();
            if (opened) status(error.message);
        } finally {
            saving = false;
            deps.getElements().avSyncSave.disabled = !opened || revision < 0 || deps.hasActiveMeasurementJob();
        }
    }
    function bindEvents() {
        const elements = deps.getElements();
        elements.avSyncOpen?.addEventListener('click', () => { void open(); });
        elements.avSyncClose?.addEventListener('click', close);
        elements.avSyncSave?.addEventListener('click', () => { void save(); });
        elements.avSyncTarget?.addEventListener('input', () => { dirty = true; feedback = ''; });
        elements.avSyncPanel?.querySelector('.manage-overlay-backdrop')?.addEventListener('click', close);
    }
    return { init, bindEvents, open, close, refresh, save };
});
