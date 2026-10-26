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

    // Official TIDAL marks (static/tidal-*.svg: white, unmodified artwork for
    // dark backgrounds). The full logo is used wherever there is room; the
    // diamond alone only in compact spots (track rows, list view, tab button)
    // where the word TIDAL stands nearby. Never placed over cover art.
    const TIDAL_LOGO_URL = '/fxroute/static/tidal-logo-white.svg?v=1';
    const TIDAL_DIAMOND_URL = '/fxroute/static/tidal-diamond-white.svg?v=1';

    // Web page of a TIDAL item; without a usable kind/id the TIDAL home page.
    function tidalWebUrl(kind, id) {
        const knownKind = kind === 'album' || kind === 'artist' || kind === 'playlist' || kind === 'track';
        const hasId = id !== null && id !== undefined && String(id) !== '';
        return knownKind && hasId
            ? 'https://tidal.com/browse/' + kind + '/' + encodeURIComponent(String(id))
            : 'https://tidal.com';
    }

    // fetchpriority: a 1 KB file shared by every card. Offscreen images queue
    // behind slow cover requests otherwise and pop in seconds after scrolling.
    function tidalMarkImg(kind) {
        const url = kind === 'logo' ? TIDAL_LOGO_URL : TIDAL_DIAMOND_URL;
        return '<img class="tidal-mark tidal-mark--' + kind + '" src="' + url + '" alt="" draggable="false" fetchpriority="high">';
    }

    // Source mark for streaming items shown next to local music.
    // options.variant: 'diamond' (default, inline row marker), 'card' (brand
    // row of a Library card: full logo, diamond in list view) or 'heading'
    // (full logo). options.link ({kind, id} or true) makes it a link back to
    // TIDAL; options.decorative hides it from assistive tech when a visible
    // heading text already names it. Brand links stay out of the focus order
    // so keyboard and remote navigation never land on an external link.
    function sourceGlyphHtml(source, extraClass, options) {
        if (source !== 'tidal') return '';
        const opts = options || {};
        const variant = opts.variant || 'diamond';
        const classes = ['source-glyph', 'source-glyph--tidal', 'source-glyph--' + variant];
        if (extraClass) classes.push(extraClass);
        const marks = variant === 'card' ? tidalMarkImg('logo') + tidalMarkImg('diamond')
            : tidalMarkImg(variant === 'heading' ? 'logo' : 'diamond');
        if (!opts.link) {
            return '<span class="' + classes.join(' ') + '" role="img" aria-label="TIDAL" data-tooltip="TIDAL">' + marks + '</span>';
        }
        const target = opts.link === true ? {} : opts.link;
        classes.push('tidal-brand-link');
        // stopPropagation: the link sits inside clickable cards and must not open them.
        return '<a class="' + classes.join(' ') + '" href="' + escapeHtml(tidalWebUrl(target.kind, target.id)) + '"'
            + ' target="_blank" rel="noopener noreferrer" tabindex="-1" data-tooltip="Open on TIDAL"'
            + (opts.decorative ? ' aria-hidden="true"' : ' aria-label="TIDAL"')
            + ' onclick="event.stopPropagation()">' + marks + '</a>';
    }

    // Accent-folded, punctuation-free key for comparing artist/album names
    // across sources ("Beyoncé" == "beyonce", "AC/DC" == "ac dc").
    function nameKey(value) {
        return String(value == null ? '' : value).normalize('NFKD').replace(/\p{M}+/gu, '').toLowerCase()
            .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
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

    // Value fields of the Tone, Crossover and Subwoofer steppers carry at most
    // two decimals; a field marked data-decimals="0" takes whole numbers only.
    const NUMBER_FIELD_MAX_DECIMALS = 2;

    function numberFieldDecimals(input) {
        const declared = input && typeof input.getAttribute === 'function'
            ? input.getAttribute('data-decimals') : null;
        const decimals = declared === null || declared === '' ? NaN : Number(declared);
        return Number.isInteger(decimals) && decimals >= 0 && decimals <= NUMBER_FIELD_MAX_DECIMALS
            ? decimals : NUMBER_FIELD_MAX_DECIMALS;
    }

    // Cuts the text after the allowed decimals (typing: the extra digit is not
    // taken). The text is kept as typed otherwise, no number round trip.
    function limitDecimalText(text, decimals) {
        const value = String(text);
        const point = value.indexOf('.');
        if (point < 0) return value;
        return decimals === 0 ? value.slice(0, point) : value.slice(0, point + 1 + decimals);
    }

    // Rounds half away from zero on the decimal text, so 1.005 gives 1.01.
    function roundToDecimals(value, decimals) {
        const number = Number(value);
        if (!Number.isFinite(number)) return number;
        const text = String(Math.abs(number));
        const scaled = text.includes('e') ? Math.abs(number) * 10 ** decimals : Number(`${text}e${decimals}`);
        const rounded = Number(`${Math.round(scaled)}e-${decimals}`);
        return number < 0 && rounded !== 0 ? -rounded : rounded;
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

    // One toggle chip per {id, short, label}; `attribute` carries the id.
    // Fewer than two chips leave nothing to choose: the row is hidden and
    // empty. The buttons are rebuilt only when a chip changes, so a click
    // updates them in place (a replaced button reads as an outside click and
    // drops keyboard focus). Returns whether the row is shown.
    function renderChipRow(row, chips, { attribute, selected = '', disabled = false } = {}) {
        if (!row) return false;
        const list = Array.isArray(chips) ? chips : [];
        const shown = list.length > 1;
        row.classList.toggle('hidden', !shown);
        const signature = shown ? list.map(chip => [chip.id, chip.short, chip.label].join('=')).join('|') : '';
        if (row.dataset.chips !== signature || (!shown && row.innerHTML !== '')) {
            row.dataset.chips = signature;
            row.innerHTML = shown ? list.map(chip => `<button type="button" class="measurement-chip" `
                + `${attribute}="${escapeHtml(chip.id)}" aria-pressed="false" `
                + `aria-label="${escapeHtml(chip.label)}" data-tooltip="${escapeHtml(chip.label)}">`
                + `${escapeHtml(chip.short)}</button>`).join('') : '';
        }
        if (!shown) return false;
        row.querySelectorAll(`[${attribute}]`).forEach((button) => {
            const active = button.getAttribute(attribute) === selected;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
            button.disabled = !!disabled;
        });
        return true;
    }

    return {
        escapeHtml,
        formatTime,
        sleep,
        favoriteHeartSvg,
        artworkPlaceholderUrl,
        albumArtFallbackSvg,
        sourceGlyphHtml,
        tidalWebUrl,
        nameKey,
        setRangeProgress,
        setContentState,
        formatRateKhz,
        formatSampleRateKhz,
        isSelectFocused,
        numberFieldDecimals,
        limitDecimalText,
        roundToDecimals,
        getDownloadFilenameFromResponse,
        triggerBlobDownload,
        renderChipRow,
    };
});
