// SPDX-License-Identifier: AGPL-3.0-only
(function (root, factory) {
    const api = factory(root);
    root.FXRouteUsbGadget = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';
    let deps = {
        getElements: () => Object.fromEntries(['panel', 'close', 'select', 'status'].map(
            key => [key, root.document.getElementById(`usb-gadget-${key}`)])),
        fetch: (...args) => root.fetch(...args),
        openModal: (panel, options) => root.FXRouteModal?.open(panel, options),
        closeModal: panel => root.FXRouteModal?.close(panel),
        refreshSources: async () => {},
    };
    let state = null;
    let busy = false;
    let generation = 0;

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => (
            {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
    }

    function configurationOption(status) {
        return status?.supported ? '<option value="usb-gadget">USB Audio Gadget…</option>' : '';
    }

    function inputOptions(inputs) {
        const groups = new Map();
        const option = input => `<option value="external-input::${escapeHtml(input.key)}">External input: ${escapeHtml(input.label || input.name || 'Unknown input')}</option>`;
        const normal = [];
        for (const input of inputs) {
            if (!input.usb_gadget) { normal.push(option(input)); continue; }
            const device = input.device_label || 'USB Audio Gadget';
            if (!groups.has(device)) groups.set(device, []);
            groups.get(device).push(option(input));
        }
        return normal.join('') + [...groups].map(([device, options]) => (
            `<optgroup label="External input: ${escapeHtml(device)}">${options.join('')}</optgroup>`)).join('');
    }

    function render(message) {
        const elements = deps.getElements();
        elements.select.value = state?.enabled ? 'on' : 'off';
        elements.select.disabled = busy || !state || (!state.enabled && !state.can_enable) || !state.helper_installed;
        elements.status.textContent = message ?? state?.message ?? 'Loading…';
    }

    async function request(method, body) {
        const options = {method};
        if (body !== undefined) {
            options.headers = {'Content-Type': 'application/json'};
            options.body = JSON.stringify(body);
        }
        const response = await deps.fetch('/api/audio/usb-gadget', options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not configure USB audio.');
        return data;
    }

    async function open() {
        const token = ++generation;
        const elements = deps.getElements();
        state = null;
        render();
        elements.panel.classList.remove('hidden');
        deps.openModal(elements.panel, {initialFocus: elements.close, onEscape: close});
        try {
            const loaded = await request('GET');
            if (token !== generation) return;
            state = loaded;
            render();
        } catch (error) {
            if (token === generation) render(error.message);
        }
    }

    function close() {
        generation++;
        const {panel} = deps.getElements();
        panel.classList.add('hidden');
        deps.closeModal(panel);
    }

    async function save(enabled) {
        if (busy || !state || (enabled && !state.can_enable)) return;
        busy = true;
        const token = generation;
        render('Applying…');
        let errorMessage;
        try {
            const saved = await request('POST', {enabled});
            if (token === generation) state = saved;
            await deps.refreshSources();
        } catch (error) {
            errorMessage = error.message;
        } finally {
            busy = false;
            if (token === generation) render(errorMessage);
            else if (!deps.getElements().panel.classList.contains('hidden')) {
                const reopened = generation;
                try {
                    const loaded = await request('GET');
                    if (reopened === generation) { state = loaded; render(); }
                } catch (error) {
                    if (reopened === generation) render(error.message);
                }
            }
        }
    }

    function bind() {
        const elements = deps.getElements();
        elements.close?.addEventListener('click', close);
        elements.panel?.querySelector('.manage-overlay-backdrop')?.addEventListener('click', close);
        elements.select?.addEventListener('change', event => { void save(event.target.value === 'on'); });
    }

    return {init: overrides => { deps = {...deps, ...overrides}; }, bind, open, close, save,
        configurationOption, inputOptions};
});
