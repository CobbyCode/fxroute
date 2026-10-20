// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute remote-control / keyboard navigation.
 *
 * Device-independent spatial focus navigation for a TV-style remote or a
 * normal keyboard:
 *   Arrow keys    move focus to the nearest control in that direction
 *   Enter/Space   activate a focused custom (ARIA) control
 *   PageUp/Down   previous/next tab of the tab strip that owns the focus
 *   Backspace     close the top dialog, else return within the current view
 *   BrowserBack   settings open/close (closes an open dialog first)
 *   ContextMenu   audio-input picker open/close
 *
 * No device-specific mapping is used: the remote just sends normal keyboard
 * events. Text fields, selects and sliders keep their native key handling.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteTvNav = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const FOCUS_CLASS = 'tv-nav-focus';

    // Anything a remote can land on. tabIndex >= 0 is checked separately so
    // inactive tab stops and click-only decoration stay out of the path.
    const FOCUSABLE_SELECTOR = [
        'a[href]',
        'button',
        'input:not([type="hidden"])',
        'select',
        'textarea',
        'summary',
        '[tabindex]',
        '[role="button"]',
        '[role="tab"]',
        '[role="menuitem"]',
        '[role="option"]',
        '[role="switch"]',
        '[role="checkbox"]',
        '[role="link"]',
        '[role="slider"]',
    ].join(',');

    const TEXT_ENTRY_TYPES = new Set([
        'text', 'search', 'url', 'tel', 'email', 'password', 'number',
        'date', 'time', 'datetime-local', 'month', 'week',
    ]);

    const ACTIVATE_ROLES = new Set([
        'button', 'tab', 'menuitem', 'option', 'switch', 'checkbox', 'link',
    ]);

    // Per-document navigation state: the element the remote focused and the
    // observer that restores it when a status-driven re-render replaces it.
    const docStates = new WeakMap();

    // Back controls of detail views inside a tab. Backspace returns to the
    // list these views were opened from.
    const BACK_CONTROL_SELECTOR = [
        '.album-detail-back',
        '.library-folder-back',
        '.detail-back',
        '[data-nav-back]',
    ].join(',');

    const NAV_KEYS = {
        ArrowUp: 'up',
        ArrowDown: 'down',
        ArrowLeft: 'left',
        ArrowRight: 'right',
    };

    // ------------------------------------------------------------------
    // Pure helpers (Node-testable without a DOM)
    // ------------------------------------------------------------------

    /** Map a key event to one navigation intent, or null. */
    function keyAction(event) {
        if (!event) return null;
        const key = event.key;
        if (key === 'BrowserBack' || event.keyCode === 166) return 'browser-back';
        if (key === 'ContextMenu' || event.keyCode === 93) return 'source-picker';
        if (NAV_KEYS[key]) return 'nav-' + NAV_KEYS[key];
        if (key === 'PageUp') return 'tabs-previous';
        if (key === 'PageDown') return 'tabs-next';
        if (key === 'Backspace') return 'back';
        if (key === 'Enter' || key === ' ') return 'activate';
        return null;
    }

    function isTextEntry(element) {
        if (!element || element.nodeType !== 1) return false;
        if (element.isContentEditable) return true;
        const tag = element.tagName;
        if (tag === 'TEXTAREA') return true;
        if (tag !== 'INPUT') return false;
        const type = String(element.getAttribute('type') || 'text').toLowerCase();
        return TEXT_ENTRY_TYPES.has(type);
    }

    // Controls whose native arrow handling must win. Text entry and native
    // dropdowns own all four arrows; a horizontal slider owns left/right so
    // up/down can still move the focus away from it (a remote has no Tab).
    function isNativeArrowOwner(element, direction) {
        if (!element || element.nodeType !== 1) return false;
        if (isTextEntry(element)) return true;
        if (element.tagName === 'SELECT') return true;
        if (element.tagName !== 'INPUT') return false;
        const type = String(element.getAttribute('type') || 'text').toLowerCase();
        return type === 'range' && (direction === 'left' || direction === 'right');
    }

    /**
     * Score one candidate rectangle for a direction. Lower is better.
     * Primary distance dominates; perpendicular drift and missing overlap
     * make a candidate less attractive. A candidate behind the current
     * element scores null.
     */
    function spatialScore(from, to, direction) {
        if (!from || !to) return null;
        const fromCx = (from.left + from.right) / 2;
        const fromCy = (from.top + from.bottom) / 2;
        const toCx = (to.left + to.right) / 2;
        const toCy = (to.top + to.bottom) / 2;
        const horizontal = direction === 'left' || direction === 'right';
        const primary = horizontal
            ? (direction === 'right' ? toCx - fromCx : fromCx - toCx)
            : (direction === 'down' ? toCy - fromCy : fromCy - toCy);
        if (!(primary > 0.5)) return null;
        const secondary = horizontal ? Math.abs(toCy - fromCy) : Math.abs(toCx - fromCx);
        // Overlap along the perpendicular axis keeps navigation inside a row
        // or column instead of jumping diagonally across the layout.
        const fromLo = horizontal ? from.top : from.left;
        const fromHi = horizontal ? from.bottom : from.right;
        const toLo = horizontal ? to.top : to.left;
        const toHi = horizontal ? to.bottom : to.right;
        const overlap = Math.max(0, Math.min(fromHi, toHi) - Math.max(fromLo, toLo));
        const overlapRatio = overlap / Math.max(1, Math.min(fromHi - fromLo, toHi - toLo));
        return primary + secondary * 1.6 - overlapRatio * Math.min(primary, 160) * 0.45;
    }

    /** Pick the best candidate ({ element, rect }) in a direction. */
    function pickSpatial(from, candidates, direction) {
        let best = null;
        let bestScore = Infinity;
        for (const candidate of candidates || []) {
            const score = spatialScore(from, candidate.rect, direction);
            if (score === null || score >= bestScore) continue;
            best = candidate;
            bestScore = score;
        }
        return best ? best.element : null;
    }

    /** Next tab index in a strip, wrapping around. */
    function stepTabIndex(current, count, delta) {
        if (!Number.isInteger(count) || count <= 0) return -1;
        const base = Number.isInteger(current) && current >= 0 && current < count ? current : 0;
        return ((base + delta) % count + count) % count;
    }

    // ------------------------------------------------------------------
    // DOM helpers
    // ------------------------------------------------------------------

    function isVisible(element) {
        if (!element || element.nodeType !== 1) return false;
        if (element.hidden) return false;
        if (element.closest('[inert]')) return false;
        const hiddenAncestor = element.closest('[aria-hidden="true"]');
        if (hiddenAncestor) return false;
        const closedDetails = element.closest('details:not([open])');
        if (closedDetails && element.tagName !== 'SUMMARY') return false;
        if (typeof element.checkVisibility === 'function'
            && !element.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true })) {
            return false;
        }
        return typeof element.getClientRects !== 'function' || element.getClientRects().length > 0;
    }

    function isDisabled(element) {
        if (!element) return true;
        if (element.disabled === true) return true;
        if (element.getAttribute && element.getAttribute('aria-disabled') === 'true') return true;
        try {
            return element.matches(':disabled');
        } catch (_error) {
            return false;
        }
    }

    function rectOf(element) {
        if (!element || typeof element.getBoundingClientRect !== 'function') return null;
        const rect = element.getBoundingClientRect();
        if (!rect || rect.width <= 0 || rect.height <= 0) return null;
        return rect;
    }

    function focusablesIn(scope) {
        if (!scope || typeof scope.querySelectorAll !== 'function') return [];
        return Array.from(scope.querySelectorAll(FOCUSABLE_SELECTOR))
            .filter(element => element.tabIndex >= 0 && !isDisabled(element) && isVisible(element));
    }

    function resolveScope(doc) {
        const modal = root.FXRouteModal;
        if (modal && typeof modal.topRoot === 'function') {
            const top = modal.topRoot();
            if (top && isVisible(top)) return top;
        }
        const dialogs = Array.from(doc.querySelectorAll('[role="dialog"], [role="menu"]'))
            .filter(isVisible);
        return dialogs.length ? dialogs[dialogs.length - 1] : doc.body;
    }

    function setNavFocus(ctx, element) {
        ctx.navFocus = element;
        ctx.navRect = rectOf(element);
        if (ctx.observer && ctx.doc.body) {
            ctx.observer.observe(ctx.doc.body, { childList: true, subtree: true });
        }
    }

    function clearNavFocus(ctx) {
        ctx.navFocus = null;
        ctx.navRect = null;
        ctx.observer?.disconnect();
    }

    function nearestToRect(rect, scope) {
        const candidates = focusablesIn(scope);
        if (!rect) return candidates[0] || null;
        const cx = (rect.left + rect.right) / 2;
        const cy = (rect.top + rect.bottom) / 2;
        let best = null;
        let bestDistance = Infinity;
        for (const element of candidates) {
            const candidate = rectOf(element);
            if (!candidate) continue;
            const dx = (candidate.left + candidate.right) / 2 - cx;
            const dy = (candidate.top + candidate.bottom) / 2 - cy;
            const distance = dx * dx + dy * dy;
            if (distance < bestDistance) {
                best = element;
                bestDistance = distance;
            }
        }
        return best;
    }

    // Status polls re-render lists (settings providers, sources) while the
    // remote stays on one of their buttons. When that button disappears, the
    // browser drops the focus to <body>; continue on the nearest control of
    // the same dialog instead of making the user start over.
    function restoreLostFocus(ctx) {
        if (!ctx.navFocus || ctx.restoring || ctx.navFocus.isConnected) return;
        const doc = ctx.doc;
        const active = doc.activeElement;
        if (active && active !== doc.body && active !== doc.documentElement) {
            clearNavFocus(ctx);
            return;
        }
        const scope = resolveScope(doc);
        if (scope === doc.body) {
            // The dialog closed: the modal manager restores the opener.
            clearNavFocus(ctx);
            return;
        }
        const target = nearestToRect(ctx.navRect, scope);
        clearNavFocus(ctx);
        if (!target) return;
        ctx.restoring = true;
        try {
            focusElement(target, doc);
        } finally {
            ctx.restoring = false;
        }
    }

    function focusElement(element, doc) {
        if (!element || typeof element.focus !== 'function') return false;
        try {
            element.focus({ preventScroll: true });
        } catch (_error) {
            element.focus();
        }
        if (doc.activeElement !== element) return false;
        element.classList?.add(FOCUS_CLASS);
        if (typeof element.scrollIntoView === 'function') {
            element.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        }
        const ctx = docStates.get(doc);
        if (ctx) setNavFocus(ctx, element);
        return true;
    }

    function firstVisibleCandidate(scope, doc) {
        const candidates = focusablesIn(scope);
        if (!candidates.length) return null;
        if (scope === doc.body) {
            const activeTab = doc.querySelector('.tabs-list [role="tab"][aria-selected="true"]');
            if (activeTab && isVisible(activeTab)) return activeTab;
            const inViewport = candidates.find(element => {
                const rect = rectOf(element);
                return rect && rect.bottom > 0 && rect.top < (doc.defaultView?.innerHeight || 0);
            });
            if (inViewport) return inViewport;
        }
        return candidates[0];
    }

    function moveFocus(doc, direction) {
        const scope = resolveScope(doc);
        const active = doc.activeElement;
        const current = active && active !== doc.body && scope.contains(active) && isVisible(active)
            ? active
            : null;
        if (!current) {
            const initial = firstVisibleCandidate(scope, doc);
            return initial ? focusElement(initial, doc) : false;
        }
        const from = rectOf(current);
        if (!from) {
            const initial = firstVisibleCandidate(scope, doc);
            return initial ? focusElement(initial, doc) : false;
        }
        const candidates = focusablesIn(scope)
            .filter(element => element !== current)
            .map(element => ({ element, rect: rectOf(element) }))
            .filter(candidate => candidate.rect);
        const target = pickSpatial(from, candidates, direction);
        return target ? focusElement(target, doc) : false;
    }

    // ------------------------------------------------------------------
    // Tab strips
    // ------------------------------------------------------------------

    function tabsIn(strip) {
        if (!strip) return [];
        let tabs = Array.from(strip.querySelectorAll('[role="tab"]'));
        if (!tabs.length) tabs = Array.from(strip.querySelectorAll('button'));
        return tabs.filter(element => !isDisabled(element) && isVisible(element));
    }

    function tabStripFor(doc) {
        let node = doc.activeElement;
        while (node && node !== doc.body) {
            if (node.matches?.('[role="tablist"]') && isVisible(node)) return node;
            node = node.parentElement;
        }
        const scope = resolveScope(doc);
        if (scope !== doc.body) {
            // A dialog owns its tabs; PageUp/Down never reach the page behind.
            const scoped = Array.from(scope.querySelectorAll('[role="tablist"]')).filter(isVisible);
            return scoped[0] || null;
        }
        const primary = doc.querySelector('.tabs-list[role="tablist"]');
        if (primary && isVisible(primary)) return primary;
        const lists = Array.from(doc.querySelectorAll('[role="tablist"]')).filter(isVisible);
        return lists[0] || null;
    }

    function tabIdentity(tab) {
        const data = tab.dataset || {};
        for (const key of ['tab', 'crossoverWay', 'subSide', 'subGroup', 'browse', 'view']) {
            if (data[key] !== undefined) return [key, data[key]];
        }
        return null;
    }

    function activateTab(tab, strip, doc) {
        const identity = tabIdentity(tab);
        const before = tabsIn(strip);
        const index = before.indexOf(tab);
        tab.click();
        // A click may re-render the strip (crossover ways, sub sides); find
        // the freshly rendered tab again so the focus ring stays on it.
        const after = tabsIn(strip);
        let next = null;
        if (identity) {
            next = after.find(candidate => {
                const other = tabIdentity(candidate);
                return other && other[0] === identity[0] && other[1] === identity[1];
            });
        }
        if (!next) next = after[Math.min(index, after.length - 1)] || (tab.isConnected ? tab : null);
        if (next) focusElement(next, doc);
    }

    function stepTab(doc, delta) {
        const strip = tabStripFor(doc);
        if (!strip) return false;
        const tabs = tabsIn(strip);
        if (tabs.length < 2) return false;
        const active = doc.activeElement;
        let index = tabs.indexOf(active);
        if (index < 0) {
            index = tabs.findIndex(tab => tab.getAttribute('aria-selected') === 'true'
                || tab.classList.contains('is-active') || tab.classList.contains('active'));
        }
        const target = tabs[stepTabIndex(index, tabs.length, delta)];
        if (!target || target === active) return false;
        activateTab(target, strip, doc);
        return true;
    }

    // ------------------------------------------------------------------
    // Dialogs, settings and the input picker
    // ------------------------------------------------------------------

    function closeTopDialog(doc) {
        const modal = root.FXRouteModal;
        if (!modal || typeof modal.topRoot !== 'function') return false;
        const top = modal.topRoot();
        if (!top) return false;
        if (typeof modal.closeTop === 'function' && modal.closeTop()) return true;
        // A dialog without an escape handler still needs a way out: use its
        // own close control instead of guessing at module internals.
        const close = Array.from(top.querySelectorAll('button')).find(button =>
            isVisible(button) && /^close\b/i.test(
                String(button.getAttribute('aria-label') || button.textContent || '').trim()));
        if (!close) return false;
        close.click();
        return true;
    }

    function closePowerMenu(doc) {
        const menu = doc.getElementById('power-menu');
        const toggle = doc.getElementById('power-menu-toggle');
        if (!menu || !toggle || menu.classList.contains('hidden')) return false;
        toggle.click();
        return true;
    }

    function findBackControl(doc) {
        const control = doc.querySelector(BACK_CONTROL_SELECTOR);
        return control && isVisible(control) ? control : null;
    }

    function goBack(doc) {
        if (closeTopDialog(doc)) return true;
        if (closePowerMenu(doc)) return true;
        const back = findBackControl(doc);
        if (back) {
            back.click();
            return true;
        }
        return false;
    }

    function toggleSettings(doc) {
        if (closeTopDialog(doc)) return true;
        if (closePowerMenu(doc)) return true;
        const open = doc.getElementById('open-settings');
        if (!open) return false;
        open.click();
        return true;
    }

    function closeSourcePicker(doc) {
        const panel = doc.getElementById('source-picker');
        if (!panel || panel.classList.contains('hidden')) return false;
        root.FXRouteModal?.close(panel);
        panel.classList.add('hidden');
        doc.getElementById('source-select')?.setAttribute('aria-expanded', 'false');
        return true;
    }

    function openSourcePicker(doc) {
        const panel = doc.getElementById('source-picker');
        const list = doc.getElementById('source-picker-list');
        const select = doc.getElementById('source-select');
        const switcher = doc.getElementById('source-switcher');
        if (!panel || !list || !select || !switcher) return false;
        // The switcher only exists for line inputs; elsewhere the context
        // menu key has no selection to offer.
        if (switcher.classList.contains('hidden') || select.disabled) return false;
        const options = Array.from(select.options || []);
        if (!options.length) return false;

        list.replaceChildren();
        let initial = null;
        for (const option of options) {
            const button = doc.createElement('button');
            button.type = 'button';
            button.className = 'source-picker-option';
            button.setAttribute('role', 'option');
            button.setAttribute('aria-selected', option.value === select.value ? 'true' : 'false');
            button.disabled = option.disabled;
            button.textContent = option.textContent || option.value;
            button.addEventListener('click', () => {
                if (select.value !== option.value) {
                    select.value = option.value;
                    select.dispatchEvent(new root.Event('change', { bubbles: true }));
                }
                closeSourcePicker(doc);
            });
            list.appendChild(button);
            if (!initial && option.value === select.value && !option.disabled) initial = button;
        }
        if (!initial) initial = list.querySelector('button:not(:disabled)');

        panel.classList.remove('hidden');
        select.setAttribute('aria-expanded', 'true');
        if (root.FXRouteModal) {
            root.FXRouteModal.open(panel, {
                initialFocus: initial || list,
                onEscape: () => closeSourcePicker(doc),
            });
        } else if (initial) {
            focusElement(initial, doc);
        }
        return true;
    }

    function toggleSourcePicker(doc) {
        const panel = doc.getElementById('source-picker');
        if (panel && !panel.classList.contains('hidden')) return closeSourcePicker(doc);
        return openSourcePicker(doc);
    }

    // ------------------------------------------------------------------
    // Key handling
    // ------------------------------------------------------------------

    function activateCustom(element, event) {
        if (!element || element.nodeType !== 1) return false;
        if (['BUTTON', 'A', 'SUMMARY', 'INPUT', 'SELECT', 'TEXTAREA'].includes(element.tagName)) {
            return false; // native activation already runs
        }
        const role = element.getAttribute('role');
        if (!role || !ACTIVATE_ROLES.has(role)) return false;
        event.preventDefault();
        element.click();
        return true;
    }

    function handleKeyDown(event, ctx) {
        if (event.defaultPrevented || event.isComposing) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        const action = keyAction(event);
        if (!action) return;
        const doc = ctx.doc;
        const target = event.target && event.target.nodeType === 1 ? event.target : doc.activeElement;

        if (action === 'browser-back') {
            event.preventDefault();
            toggleSettings(doc);
            return;
        }
        if (action === 'source-picker') {
            event.preventDefault();
            toggleSourcePicker(doc);
            return;
        }
        // Typing owns every key.
        if (isTextEntry(target)) return;
        if (action === 'activate') {
            activateCustom(target, event);
            return;
        }
        if (action === 'back') {
            event.preventDefault();
            goBack(doc);
            return;
        }
        if (action === 'tabs-previous' || action === 'tabs-next') {
            event.preventDefault();
            stepTab(doc, action === 'tabs-next' ? 1 : -1);
            return;
        }
        if (action.startsWith('nav-')) {
            const direction = action.slice(4);
            if (isNativeArrowOwner(target, direction)) return;
            event.preventDefault();
            moveFocus(doc, direction);
        }
    }

    function init(options = {}) {
        const doc = options.document
            || (typeof document !== 'undefined' ? document : null);
        if (!doc || doc.__fxrouteTvNavBound) return null;
        doc.__fxrouteTvNavBound = true;
        const ctx = {
            doc,
            navFocus: null,
            navRect: null,
            restoring: false,
            observer: typeof MutationObserver === 'function'
                ? new MutationObserver(() => restoreLostFocus(ctx))
                : null,
        };
        docStates.set(doc, ctx);
        doc.addEventListener('keydown', event => handleKeyDown(event, ctx), false);
        doc.addEventListener('focusin', event => {
            if (ctx.navFocus && event.target !== ctx.navFocus) clearNavFocus(ctx);
        }, true);
        doc.addEventListener('focusout', event => {
            event.target?.classList?.remove(FOCUS_CLASS);
        }, true);
        const closeButton = doc.getElementById('source-picker-close');
        if (closeButton) closeButton.addEventListener('click', () => closeSourcePicker(doc));
        return ctx;
    }

    if (typeof document !== 'undefined') {
        const start = () => init({ document });
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', start, { once: true });
        } else {
            start();
        }
    }

    return {
        FOCUS_CLASS,
        init,
        keyAction,
        isTextEntry,
        isNativeArrowOwner,
        spatialScore,
        pickSpatial,
        stepTabIndex,
    };
});
