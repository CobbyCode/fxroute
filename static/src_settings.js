// SPDX-License-Identifier: AGPL-3.0-only
(function (root, factory) {
    const api = factory();
    root.FXRouteSrcSettings = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';
    let deps = { fetchFn: (...args) => fetch(...args), onError: () => {}, onApplied: () => {} };
    let status = {};
    let saving = false;
    const guidance = 'Low: ~70% / 0.45× CPU · Standard: ~81,9% / 1.0× CPU · High: ~99.6% / 1.5× CPU';

    function init(overrides) { deps = { ...deps, ...overrides }; }
    function render() {
        if (deps.select) {
            deps.select.value = status.mode || 'standard';
            deps.select.disabled = saving || status.available !== true;
        }
        if (deps.hint) {
            deps.hint.textContent = guidance;
        }
    }
    function update(data) {
        if (saving) return;
        status = data || {};
        render();
    }
    async function refresh() {
        if (saving) return;
        try {
            const response = await deps.fetchFn('/api/audio/src');
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Cannot read SRC mode');
            status = data;
        } catch (error) { status = { ...status, available: false, error: error.message }; }
        render();
    }
    async function save(mode) {
        if (saving) return;
        saving = true;
        render();
        try {
            const response = await deps.fetchFn('/api/audio/src', {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(data.detail || 'Cannot apply SRC mode');
            status = data;
            await deps.onApplied();
        } catch (error) { deps.onError(error.message); }
        finally { saving = false; render(); }
    }
    return { init, refresh, save, render, update };
});
