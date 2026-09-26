// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute app tooltip layer.
 *
 * `data-tooltip` on any element is a visual hint; the accessible name stays
 * in aria-label or the element's own content. The bubble is one element on
 * <body> with `position: fixed`, not a pseudo-element of the host: a host's
 * ::after is clipped by every overflow/scroll container around it and adds
 * scrollable overflow to that container even while hidden.
 *
 * Opens on mouse hover and on keyboard focus (:focus-visible), never on
 * touch. Below the host by default, above it when only that side has room,
 * always clamped into the viewport. `data-tooltip-anchor="end"` right-aligns
 * the bubble to the host. A `.select-tooltip-wrap` shows the full name of
 * the selected option, read when the bubble opens, so programmatic value
 * changes need no sync call. The text is re-read whenever the host's
 * attributes change, so runtime label updates show at once.
 *
 * Browser-loadable UMD, no build step; the placement and text rules are
 * Node-testable via require().
 */
(function (root, factory) {
    const api = factory();
    root.FXRouteTooltip = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
    const doc = root.document;
    if (doc && typeof doc.addEventListener === 'function') {
        if (doc.readyState === 'loading') {
            doc.addEventListener('DOMContentLoaded', () => api.install(doc), { once: true });
        } else {
            api.install(doc);
        }
    }
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    const HOST_SELECTOR = '[data-tooltip], .select-tooltip-wrap';
    // Gap between host and bubble, and the minimum distance to the viewport.
    const GAP_PX = 7;
    const EDGE_PX = 8;

    function placeTooltip(hostRect, size, viewport, anchor) {
        const preferredLeft = anchor === 'end' ? hostRect.right - size.width : hostRect.left;
        const maxLeft = viewport.width - EDGE_PX - size.width;
        const left = Math.max(EDGE_PX, Math.min(preferredLeft, maxLeft));
        const belowTop = hostRect.bottom + GAP_PX;
        const aboveTop = hostRect.top - GAP_PX - size.height;
        const fitsBelow = belowTop + size.height <= viewport.height - EDGE_PX;
        const fitsAbove = aboveTop >= EDGE_PX;
        const moreRoomBelow = viewport.height - hostRect.bottom >= hostRect.top;
        const side = fitsBelow || (!fitsAbove && moreRoomBelow) ? 'below' : 'above';
        const top = side === 'below' ? belowTop : Math.max(EDGE_PX, aboveTop);
        return { left: Math.round(left), top: Math.round(top), side };
    }

    // The option label may be compacted; the option's aria-label (or its
    // text) carries the full name. The placeholder option (empty value) has
    // nothing to reveal.
    function selectTooltipText(wrap) {
        const select = wrap.querySelector('select');
        const option = select && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
        if (!option || option.value === '') return '';
        const name = String(option.getAttribute('aria-label') || option.textContent || '').trim();
        if (!name) return '';
        const prefix = wrap.getAttribute('data-tooltip-prefix');
        return prefix ? `${prefix}: ${name}` : name;
    }

    function tooltipText(host) {
        if (!host) return '';
        // An expanded host (open power menu, open settings) covers the
        // bubble's area with its own surface.
        if (host.getAttribute('aria-expanded') === 'true') return '';
        if (host.classList && host.classList.contains('select-tooltip-wrap')) return selectTooltipText(host);
        return String(host.getAttribute('data-tooltip') || '').trim();
    }

    function install(doc) {
        if (!doc || !doc.body || doc.body.querySelector(':scope > .app-tooltip')) return null;
        const win = doc.defaultView || globalThis;
        const bubble = doc.createElement('div');
        bubble.className = 'app-tooltip';
        bubble.setAttribute('aria-hidden', 'true');
        bubble.hidden = true;
        doc.body.appendChild(bubble);

        let host = null;
        let openedBy = null;
        let frame = 0;
        const observer = typeof win.MutationObserver === 'function' ? new win.MutationObserver(schedule) : null;

        function hostOf(target) {
            return target && typeof target.closest === 'function' ? target.closest(HOST_SELECTOR) : null;
        }

        function isKeyboardFocus(el) {
            try { return el.matches(':focus-visible'); } catch (_) { return true; }
        }

        function hideBubble() {
            bubble.hidden = true;
            bubble.classList.remove('is-visible');
        }

        function render() {
            if (!host) return;
            if (!host.isConnected) { close(); return; }
            const text = tooltipText(host);
            const rect = host.getBoundingClientRect();
            if (!text || (rect.width === 0 && rect.height === 0)) { hideBubble(); return; }
            if (bubble.textContent !== text) bubble.textContent = text;
            const wasHidden = bubble.hidden;
            bubble.hidden = false;
            const size = { width: bubble.offsetWidth, height: bubble.offsetHeight };
            const viewport = { width: doc.documentElement.clientWidth, height: doc.documentElement.clientHeight };
            const place = placeTooltip(rect, size, viewport, host.getAttribute('data-tooltip-anchor'));
            bubble.style.left = `${place.left}px`;
            bubble.style.top = `${place.top}px`;
            bubble.setAttribute('data-side', place.side);
            // Fade in from the next frame so the transition has a start state.
            if (wasHidden) win.requestAnimationFrame(() => { if (!bubble.hidden) bubble.classList.add('is-visible'); });
        }

        function schedule() {
            if (frame || !host) return;
            frame = win.requestAnimationFrame(() => { frame = 0; render(); });
        }

        function open(el, source) {
            if (host !== el) {
                hideBubble();
                host = el;
                if (observer) {
                    observer.disconnect();
                    observer.observe(el, { attributes: true, attributeFilter: ['data-tooltip', 'aria-expanded', 'class', 'hidden'] });
                    // Re-renders replace hosts and select options.
                    observer.observe(doc.body, { childList: true, subtree: true });
                }
            }
            openedBy = source;
            render();
        }

        function close() {
            if (observer) observer.disconnect();
            host = null;
            openedBy = null;
            hideBubble();
        }

        doc.addEventListener('pointerover', (event) => {
            if (event.pointerType !== 'mouse') return;
            const el = hostOf(event.target);
            if (el && (el !== host || openedBy !== 'pointer')) open(el, 'pointer');
        });
        doc.addEventListener('pointerout', (event) => {
            if (openedBy !== 'pointer' || !host) return;
            if (event.relatedTarget && host.contains(event.relatedTarget)) return;
            close();
        });
        doc.addEventListener('focusin', (event) => {
            const el = hostOf(event.target);
            if (el && isKeyboardFocus(event.target)) open(el, 'focus');
        });
        doc.addEventListener('focusout', () => {
            if (openedBy === 'focus') close();
        });
        // A select changed from the keyboard keeps focus: refresh its name.
        doc.addEventListener('change', (event) => {
            if (host && host.contains(event.target)) schedule();
        }, true);
        win.addEventListener('scroll', schedule, true);
        win.addEventListener('resize', schedule);

        return { bubble, close };
    }

    return { install, placeTooltip, tooltipText, HOST_SELECTOR };
});
