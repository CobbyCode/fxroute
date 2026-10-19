// SPDX-License-Identifier: AGPL-3.0-only
/**
 * HDMI channel layout (Stereo / 5.1 / 7.1) of the selected HDMI output.
 * The layouts come from the receiver's ELD; the chosen one is restored after
 * a lost connection. Browser-loadable UMD, no build step; Node-testable via
 * require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteHdmiTransport = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => (
            {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
    }

    function khz(rate) {
        return `${(Number(rate) / 1000).toFixed(1).replace(/\.0$/, '')}`;
    }

    function rateRange(rates) {
        const values = (Array.isArray(rates) ? rates : []).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
        if (!values.length) return '';
        return values.length === 1 ? `${khz(values[0])} kHz` : `${khz(values[0])}–${khz(values[values.length - 1])} kHz`;
    }

    // Select model of one output; hidden for every non-HDMI output.
    function view(output, pending) {
        const info = output && output.hdmi;
        if (!info) return { visible: false };
        const transports = Array.isArray(info.transports) ? info.transports : [];
        const labelOf = (id) => (transports.find(item => item.id === id) || {}).label || id;
        const options = transports.map(item => ({
            value: item.id,
            label: item.available || item.id === info.transport ? item.label : `${item.label} (unavailable)`,
            disabled: !item.available && item.id !== info.transport,
            title: item.reason || '',
        }));
        const receiver = info.receiver || null;
        const parts = [];
        if (receiver && receiver.name) parts.push(receiver.name);
        if (receiver && receiver.lpcm_channels) {
            const range = rateRange(receiver.lpcm_rates);
            parts.push(`LPCM up to ${receiver.lpcm_channels} ch${range ? `, ${range}` : ''}`);
        }
        if (!receiver) parts.push('No receiver information (ELD)');
        const desired = info.desired_transport;
        if (desired && desired !== info.transport) {
            parts.push(`${labelOf(desired)} chosen; plays ${labelOf(info.transport)} while the receiver does not take it`);
        }
        return { visible: true, value: pending || info.transport, options, hint: parts.join(' · '), disabled: !!pending };
    }

    function render(elements, output, options = {}) {
        const model = view(output, options.pending);
        if (elements.group) elements.group.classList.toggle('hidden', !model.visible);
        if (!model.visible) return model;
        const select = elements.select;
        if (select && !options.focused) {
            const html = model.options.map(item =>
                `<option value="${escapeHtml(item.value)}"${item.disabled ? ' disabled' : ''}${item.title ? ` title="${escapeHtml(item.title)}"` : ''}>${escapeHtml(item.label)}</option>`).join('');
            if (select.innerHTML !== html) select.innerHTML = html;
            select.value = model.value;
            select.disabled = model.disabled || !!options.busy;
        }
        if (elements.hint) elements.hint.textContent = model.hint;
        return model;
    }

    async function save(fetchImpl, key, transport) {
        const response = await fetchImpl('/api/audio/outputs/transport', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, transport }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not change the HDMI layout.');
        return data;
    }

    return { view, render, save, rateRange };
});
