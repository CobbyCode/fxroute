// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute shared JSON fetch helpers.
 * Canonical owner of the transition-error formatter plus the thin
 * fetch wrappers used by app.js playback and settings paths.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteApi = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    function formatTransitionErrorDetail(detail, fallback = 'Request failed') {
        if (typeof detail === 'string') {
            const trimmedDetail = detail.trim();
            if (trimmedDetail) return trimmedDetail;
        } else if (detail && typeof detail === 'object') {
            const message = typeof detail.message === 'string' ? detail.message.trim() : '';
            if (message) {
                const stage = typeof detail.stage === 'string' ? detail.stage.trim() : '';
                if (stage && !message.toLowerCase().includes(stage.toLowerCase())) {
                    return `${message} (stage: ${stage})`;
                }
                return message;
            }
            // Message-less structured details (FastAPI validation lists, status
            // objects without a message) stay out of the UI: keep them for
            // diagnosis via console.warn and fall through to the generic fallback
            // so no internal fields leak into toasts or error states.
            try {
                if (typeof console !== 'undefined' && typeof console.warn === 'function') {
                    console.warn('Suppressed message-less error detail:', detail);
                }
            } catch (_warnError) {
                // Logging must never break error rendering.
            }
        }
        return typeof fallback === 'string' ? fallback : 'Request failed';
    }

    // Shared JSON fetch helpers: non-2xx responses throw with the server's
    // detail message (when present) instead of silently resolving to null.
    async function apiFetchJson(url, options = {}) {
        const resp = await fetch(url, options);
        const data = await resp.json().catch(() => null);
        if (!resp.ok) {
            // A transition failure carries a structured detail object
            // ({ok, transition_id, stage, failure_latched, message}). Rendering it
            // through String() would surface the useless "[object Object]"; the
            // shared formatter keeps the backend message and stage visible, and
            // maps message-less details to this generic fallback (diagnosed via
            // console.warn) instead of leaking raw JSON into the UI.
            const detail = data && (data.detail || data.error || data.message);
            throw new Error(formatTransitionErrorDetail(detail, `HTTP ${resp.status} ${url}`));
        }
        return data;
    }

    function apiPostJson(url, body) {
        return apiFetchJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body ?? {}),
        });
    }

    return {
        formatTransitionErrorDetail,
        apiFetchJson,
        apiPostJson,
    };
});
