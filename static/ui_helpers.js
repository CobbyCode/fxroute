// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute shared UI helpers.
 * Canonical owner of the leaf HTML/time/DOM formatters used across app.js
 * and injected into radio.js/streaming.js via init(). No app state access;
 * all helpers take explicit arguments. Browser-loadable UMD, no build step;
 * Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteUiHelpers = api;
    // Shared compact content-state renderer keeps one Loading/Empty/Error
    // vocabulary across Library, Radio and TIDAL browse. Exposed globally
    // because radio.js and streaming.js may load before app.js but only call
    // it at runtime, after this module has defined it.
    if (root && !root.FXRouteContentState) {
        root.FXRouteContentState = {
            set: api.setContentState,
            loading(el, msg) { api.setContentState(el, 'loading', msg); },
            empty(el, msg) { api.setContentState(el, 'empty', msg); },
            error(el, msg) { api.setContentState(el, 'error', msg); },
            hide(el) { api.setContentState(el, 'none', ''); },
        };
    }
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    function escapeHtml(text) {
        if (text === null || text === undefined) return '';
        // Escapes quotes too so the result is safe inside double-quoted attributes.
        // Any other value (including numeric 0) is stringified as-is.
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Format seconds as m:ss.
    function formatTime(sec) {
        if (!sec || !isFinite(sec) || sec < 0) return '0:00';
        const m = Math.floor(sec / 60);
        const s = Math.floor(sec % 60);
        return `${m}:${s < 10 ? '0' : ''}${s}`;
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    // Favorite hearts render as one inline SVG instead of the Unicode hearts
    // (U+2665 / U+2661). In some browser/OS combinations those fall back to a
    // colour-emoji font, which paints an active heart red no matter what the
    // button's CSS colour says. The SVG is painted from currentColor, so the
    // existing muted / accent button states stay the single source of truth.
    function favoriteHeartSvg() {
        return '<svg class="fav-heart" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
            + '<path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';
    }

    function artworkPlaceholderUrl() {
        return '/fxroute/static/artwork-placeholder.svg?v=2';
    }

    function albumArtFallbackSvg() {
        return artworkPlaceholderUrl();
    }

    function setRangeProgress(input, fraction) {
        if (!input) return;
        const percent = Math.max(0, Math.min(100, Number(fraction) * 100));
        input.style.setProperty('--range-progress', `${percent}%`);
    }

    function setContentState(el, state, message) {
        if (!el) return;
        if (state === 'none' || state === 'hidden') {
            el.classList.add('hidden');
            el.textContent = '';
            return;
        }
        el.classList.remove('hidden');
        el.hidden = false;
        el.style.display = '';
        el.className = 'content-state content-state--' + state;
        el.textContent = message || '';
    }

    function formatRateKhz(rate) {
        const numericRate = Number(rate);
        if (!Number.isFinite(numericRate) || numericRate <= 0) return '';
        // Compact unit spelling (44.1kHz): shared with the footer pill, where
        // every saved pixel counts against badge truncation.
        return `${(numericRate / 1000).toFixed(1).replace(/\.0$/, '')}kHz`;
    }

    function formatSampleRateKhz(rate) {
        const numeric = Number(rate);
        if (!Number.isFinite(numeric) || numeric <= 0) return 'Auto';
        return `${(numeric / 1000).toFixed(1).replace(/\.0$/, '')} kHz`;
    }

    function isSelectFocused(selectEl) {
        return !!selectEl && typeof document !== 'undefined' && document.activeElement === selectEl;
    }

    function getDownloadFilenameFromResponse(resp, fallbackName = 'download') {
        const header = resp.headers.get('Content-Disposition') || '';
        const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
        if (utf8Match && utf8Match[1]) {
            try {
                return decodeURIComponent(utf8Match[1]);
            } catch (_) {
                return utf8Match[1];
            }
        }
        const plainMatch = header.match(/filename="?([^";]+)"?/i);
        if (plainMatch && plainMatch[1]) return plainMatch[1];
        return fallbackName;
    }

    function triggerBlobDownload(blob, filename) {
        const objectUrl = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = filename || 'download';
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }

    return {
        escapeHtml,
        formatTime,
        sleep,
        favoriteHeartSvg,
        artworkPlaceholderUrl,
        albumArtFallbackSvg,
        setRangeProgress,
        setContentState,
        formatRateKhz,
        formatSampleRateKhz,
        isSelectFocused,
        getDownloadFilenameFromResponse,
        triggerBlobDownload,
    };
});
