// SPDX-License-Identifier: AGPL-3.0-only
/** Custom input mapping dialog: physical capture channels of the selected device -> program channels. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteCustomMapping = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const ENDPOINT = '/api/audio/custom-mapping';
    const REQUIRED_POSITIONS = ['FL', 'FR'];
    const OFF = '';

    let deps = {
        getElements: () => ({}),
        fetch: (...args) => root.fetch(...args),
        openModal: (panel, options) => root.FXRouteModal?.open(panel, options),
        closeModal: (panel) => root.FXRouteModal?.close(panel),
        confirm: (message) => root.confirm(message),
        showToast: () => {},
        refreshSources: async () => {},
    };
    let context = null;
    let selection = {};
    let busy = false;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, (char) => (
            { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
    }

    function positionLabel(positions, id) {
        return (positions || []).find((position) => position.id === id)?.label || id;
    }

    // Pure model ----------------------------------------------------------

    /** Options of one position: "Off" (not for required positions) and every capture channel. */
    function channelOptions(capture, position) {
        const options = REQUIRED_POSITIONS.includes(position) ? [] : [{ value: OFF, label: 'Off' }];
        for (const channel of capture?.channels || []) {
            options.push({ value: String(channel.index), label: `Input ${channel.index} · ${channel.suffix}` });
        }
        return options;
    }

    /** Stored mapping within the live channel count, else the first two inputs on Front L/R. */
    function initialSelection(data) {
        const count = (data?.capture?.channels || []).length;
        const stored = data?.mapping?.channels;
        if (!stored) return count >= 2 ? { FL: '1', FR: '2' } : {};
        const result = {};
        for (const [position, index] of Object.entries(stored)) {
            if (Number.isInteger(index) && index >= 1 && index <= count) result[position] = String(index);
        }
        return result;
    }

    /** Message for the first problem of a selection, or '' when it can be saved. */
    function validateSelection(current, positions) {
        for (const required of REQUIRED_POSITIONS) {
            if (!current[required]) return `${positionLabel(positions, required)} must be mapped.`;
        }
        const owner = {};
        for (const position of (positions || []).map((entry) => entry.id)) {
            const value = current[position];
            if (!value) continue;
            if (owner[value]) {
                return `Input ${value} is used for both ${positionLabel(positions, owner[value])} and ${positionLabel(positions, position)}.`;
            }
            owner[value] = position;
        }
        return '';
    }

    function selectionPayload(name, current) {
        const channels = {};
        for (const [position, value] of Object.entries(current)) {
            if (value) channels[position] = Number(value);
        }
        return { name: String(name || '').trim(), channels };
    }

    function renderGridHtml(positions, capture, current) {
        return (positions || []).map((position) => {
            const options = channelOptions(capture, position.id).map((option) => (
                `<option value="${escapeHtml(option.value)}"${(current[position.id] || OFF) === option.value ? ' selected' : ''}>${escapeHtml(option.label)}</option>`
            )).join('');
            const id = `custom-mapping-select-${position.id}`;
            return `<div class="settings-routing-cell"><label for="${id}">${escapeHtml(position.label)}</label>`
                + `<select id="${id}" class="url-input" data-custom-mapping-position="${escapeHtml(position.id)}">${options}</select></div>`;
        }).join('');
    }

    // Dialog --------------------------------------------------------------

    function setStatus(text = '') {
        const element = deps.getElements().customMappingStatus;
        if (element) element.textContent = text;
    }

    function setBusy(value) {
        busy = !!value;
        updateControls();
    }

    function updateControls() {
        const elements = deps.getElements();
        const usable = !!context?.capture && !busy;
        const problem = context ? validateSelection(selection, context.positions) : '';
        if (elements.customMappingSave) elements.customMappingSave.disabled = !usable || !!problem;
        if (elements.customMappingDelete) {
            elements.customMappingDelete.classList.toggle('hidden', !context?.mapping);
            elements.customMappingDelete.disabled = busy;
        }
        if (elements.customMappingName) elements.customMappingName.disabled = !usable;
        elements.customMappingGrid?.querySelectorAll('select').forEach((select) => { select.disabled = !usable; });
        return problem;
    }

    function render() {
        const elements = deps.getElements();
        if (!context) return;
        const device = context.device?.label || context.device?.card_base || 'the selected audio device';
        if (elements.customMappingSubtitle) {
            elements.customMappingSubtitle.textContent = `Route the input channels of ${device} to FXRoute channels.`;
        }
        if (elements.customMappingName) elements.customMappingName.value = context.mapping?.name || '';
        if (elements.customMappingGrid) {
            elements.customMappingGrid.innerHTML = context.capture
                ? renderGridHtml(context.positions, context.capture, selection) : '';
        }
        const problem = updateControls();
        if (!context.device) setStatus('No audio output device is selected.');
        else if (!context.capture) setStatus('The selected audio device has no input channels.');
        else setStatus(problem);
    }

    async function request(method, body) {
        const options = { method };
        if (body !== undefined) {
            options.headers = { 'Content-Type': 'application/json' };
            options.body = JSON.stringify(body);
        }
        const response = await deps.fetch(ENDPOINT, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            const detail = typeof data.detail === 'string' ? data.detail : '';
            throw new Error(detail || `Request failed (${response.status})`);
        }
        return data;
    }

    async function open() {
        const elements = deps.getElements();
        if (!elements.customMappingPanel) return;
        context = null;
        selection = {};
        setStatus('Loading…');
        elements.customMappingPanel.classList.remove('hidden');
        deps.openModal(elements.customMappingPanel, {
            initialFocus: elements.customMappingName,
            onEscape: () => close(),
        });
        try {
            context = await request('GET');
            selection = initialSelection(context);
            render();
        } catch (error) {
            setStatus(error.message);
        }
    }

    function close() {
        const panel = deps.getElements().customMappingPanel;
        if (!panel) return;
        panel.classList.add('hidden');
        deps.closeModal(panel);
    }

    async function finish(message) {
        deps.showToast(message, 'success');
        close();
        await deps.refreshSources();
    }

    async function save() {
        if (busy || !context) return;
        const problem = validateSelection(selection, context.positions);
        if (problem) {
            setStatus(problem);
            return;
        }
        setBusy(true);
        try {
            context = await request('PUT', selectionPayload(deps.getElements().customMappingName?.value, selection));
            await finish('Custom mapping saved');
        } catch (error) {
            setStatus(error.message);
        } finally {
            setBusy(false);
        }
    }

    async function remove() {
        if (busy || !context?.mapping) return;
        if (!deps.confirm('Delete the custom mapping? Its input disappears from the Input list.')) return;
        setBusy(true);
        try {
            context = await request('DELETE');
            await finish('Custom mapping deleted');
        } catch (error) {
            setStatus(error.message);
        } finally {
            setBusy(false);
        }
    }

    function bind() {
        const elements = deps.getElements();
        elements.customMappingClose?.addEventListener('click', () => close());
        elements.customMappingPanel?.querySelector('.manage-overlay-backdrop')
            ?.addEventListener('click', () => close());
        elements.customMappingSave?.addEventListener('click', () => { void save(); });
        elements.customMappingDelete?.addEventListener('click', () => { void remove(); });
        elements.customMappingGrid?.addEventListener('change', (event) => {
            const position = event.target?.dataset?.customMappingPosition;
            if (!position) return;
            selection = { ...selection, [position]: event.target.value };
            setStatus(updateControls());
        });
    }

    return {
        init, bind, open, close, save, remove,
        channelOptions, initialSelection, validateSelection, selectionPayload, renderGridHtml,
    };
});
