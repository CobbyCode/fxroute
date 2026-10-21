// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute remote-control / keyboard navigation.
 *
 * Device-independent spatial focus navigation for a TV-style remote or a
 * normal keyboard:
 *   Arrow keys    move focus to the nearest control in that direction
 *   Enter/Space   activate the focused control (native handling first)
 *   Escape        leave a text field or dropdown that owns the arrow keys
 *   PageUp/Down   next/previous tab of the tab strip that owns the focus
 *                 (Up goes right, Down goes left)
 *   Backspace     close the top dialog, else return within the current view
 *   BrowserBack   the remote's Home key, one tier per press: dialogs
 *                 collapse/close, then the page's open panels and detail
 *                 views, else the settings open; may also arrive as
 *                 Alt+ArrowLeft. A history guard keeps the browser from
 *                 leaving the app on it.
 *   ContextMenu   audio-input picker open/close. A remote's menu key often
 *                 arrives as Compose/Unidentified; Firefox then emits a
 *                 contextmenu event with button 0, which is handled too.
 *
 * Sliders reached by the remote are navigation stops: OK arms them, then
 * Left/Right change the value; OK again, Back or Up/Down ends the adjustment.
 * The footer is its own zone: Left/Right/Down never leave it, Up leaves it
 * from the top row.
 *
 * No device-specific mapping is used: the remote just sends normal keyboard
 * events. Pointer-opened dropdowns and text editing keep native handling.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteTvNav = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const FOCUS_CLASS = 'tv-nav-focus';
    const EDITING_CLASS = 'tv-nav-editing';
    const FOOTER_SELECTOR = '.playback-bar';
    // The seek slider sits in its own row below the transport controls.
    const FOOTER_SEEK_SELECTOR = '.seek-row';
    const CARD_SELECTOR = '.album-card, .station-card';
    const CARD_FAVORITE_SELECTOR = '.album-card-fav, .station-card-fav, .streaming-fav';
    // Rows with a round Play button (album/playlist details, track,
    // playlist and folder lists): Up/Down step from row to row on the Play
    // buttons, Left/Right stay inside one row (Play, Plus, heart, actions).
    const TRACK_ROW_SELECTOR = '.track-item, .streaming-result';
    const TRACK_PLAY_SELECTOR = '.track-play';
    const DETAIL_VIEW_SELECTOR = '.album-detail, .streaming-detail';
    // DSP cards and their effect groups (Upmixer, Loudness, ... and the
    // Protection rows): Left/Right follow the group's control order.
    const EFFECTS_CARD_SELECTOR = '.effects-card';
    const EFFECT_GROUP_SELECTOR = '.effects-effect, .effects-extras-row';
    // Set by rows that handle Left/Right themselves (see app.js, measurement_panel_ui.js).
    const OWN_ARROWS_SELECTOR = '[data-own-arrows]';

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
        // Some remotes map their back button to the desktop shortcut.
        if (event.altKey && key === 'ArrowLeft') return 'browser-back';
        if (key === 'ContextMenu' || key === 'Compose' || event.keyCode === 93) return 'source-picker';
        if (NAV_KEYS[key]) return 'nav-' + NAV_KEYS[key];
        // Up goes right to the next page, Down goes left to the previous one.
        if (key === 'PageUp') return 'tabs-next';
        if (key === 'PageDown') return 'tabs-previous';
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

    function isRangeInput(element) {
        if (!element || element.nodeType !== 1 || element.tagName !== 'INPUT') return false;
        return String(element.getAttribute('type') || 'text').toLowerCase() === 'range';
    }

    // One control at a time is being edited by the remote: a text field after
    // OK, or a slider armed with OK. A slider also shows it with a class.
    function setEditing(ctx, element) {
        if (ctx.editing === element) return;
        ctx.editing?.classList?.remove(EDITING_CLASS);
        ctx.editing = element;
        if (isRangeInput(element)) element.classList.add(EDITING_CLASS);
    }

    function isArmedSlider(ctx, element) {
        return isRangeInput(element) && ctx.editing === element && ctx.navFocus === element;
    }

    // Controls whose native arrow handling must win. Text entry owns all
    // arrows; a horizontal slider owns left/right so up/down can still move
    // the focus away from it (a remote has no Tab). A closed <select> does
    // not own the arrows: Enter opens its remote selection dialog. Letting
    // a closed select keep the
    // arrows traps the remote there (e.g. "Preset 3 optional").
    function isNativeArrowOwner(element, direction) {
        if (!element || element.nodeType !== 1) return false;
        if (isTextEntry(element)) return true;
        // A row that binds its own Left/Right (tab strip, chip rows) marks
        // itself so the focus is not moved twice for one key press.
        if ((direction === 'left' || direction === 'right') && element.closest?.(OWN_ARROWS_SELECTOR)) return true;
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

    /** True when two rectangles share a row (half of the smaller height overlaps). */
    function sharesRow(a, b) {
        const overlap = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        return overlap >= Math.min(a.bottom - a.top, b.bottom - b.top) * 0.5;
    }

    /**
     * Pick the best candidate ({ element, rect }) in a direction. Left and
     * Right stay in the row while it has a neighbour; the nearest control of
     * another row only wins when the row is empty in that direction.
     */
    function pickSpatial(from, candidates, direction) {
        const horizontal = direction === 'left' || direction === 'right';
        let best = null;
        let bestScore = Infinity;
        let bestInRow = null;
        let bestInRowScore = Infinity;
        for (const candidate of candidates || []) {
            const score = spatialScore(from, candidate.rect, direction);
            if (score === null) continue;
            if (score < bestScore) {
                best = candidate;
                bestScore = score;
            }
            if (horizontal && score < bestInRowScore && sharesRow(from, candidate.rect)) {
                bestInRow = candidate;
                bestInRowScore = score;
            }
        }
        const picked = bestInRow || best;
        return picked ? picked.element : null;
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
        for (let parent = element.parentElement; parent; parent = parent.parentElement) {
            if (parent.matches('details:not([open])') && parent.firstElementChild !== element) return false;
        }
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
        const card = navigationCard(element);
        const anchor = card?.parentElement.closest('[id]');
        ctx.cardFocus = card ? {
            anchor,
            anchorParent: anchor?.parentElement,
            key: cardKey(card),
            index: Array.from(card.parentElement.children).filter(item => item.matches(CARD_SELECTOR)).indexOf(card),
            favorite: element.matches(CARD_FAVORITE_SELECTOR),
        } : null;
        const row = trackRow(element);
        ctx.trackFocus = row ? {
            anchorId: row.parentElement.closest('[id]')?.id,
            key: rowKey(row),
            control: focusablesIn(row).indexOf(element),
        } : null;
        if (ctx.observer && ctx.doc.body) {
            ctx.observer.observe(ctx.doc.body, { childList: true, subtree: true });
            // Footer controls hide and disable themselves by attribute.
            const footer = element.closest?.(FOOTER_SELECTOR);
            if (footer) {
                ctx.observer.observe(footer, {
                    attributes: true, attributeFilter: ['class', 'hidden', 'disabled'], subtree: true,
                });
            }
        }
    }

    function clearNavFocus(ctx) {
        ctx.navFocus = null;
        ctx.navRect = null;
        ctx.cardFocus = null;
        ctx.trackFocus = null;
        ctx.observer?.disconnect();
    }

    function nearestToRect(rect, scope, exclude = null) {
        const candidates = focusablesIn(scope);
        if (!rect) return candidates.find(element => element !== exclude) || null;
        const cx = (rect.left + rect.right) / 2;
        const cy = (rect.top + rect.bottom) / 2;
        let best = null;
        let bestDistance = Infinity;
        for (const element of candidates) {
            if (element === exclude) continue;
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

    // A footer control that vanishes (cleared queue) or stays disabled hands
    // the focus to its nearest enabled neighbour; dropped into the page, the
    // next key would jump into the main view.
    function footerFallback(ctx, control) {
        const footer = control?.closest?.(FOOTER_SELECTOR);
        if (!footer || !footer.isConnected) return null;
        return nearestToRect(ctx.navRect, footer, control);
    }

    // Status polls re-render lists (settings providers, sources) while the
    // remote stays on one of their buttons. When that button disappears, the
    // browser drops the focus to <body>; continue on the nearest control of
    // the same dialog instead of making the user start over.
    function restoreLostFocus(ctx) {
        if (!ctx.navFocus || ctx.restoring) return;
        const doc = ctx.doc;
        const active = doc.activeElement;
        const lost = !active || active === doc.body || active === doc.documentElement;
        if (ctx.navFocus.isConnected) {
            // A control disabled during its request (e.g. a track heart) drops
            // the focus; it takes it back once it is enabled again.
            const control = ctx.navFocus;
            if (lost && isVisible(control) && !isDisabled(control) && resolveScope(doc).contains(control)) {
                ctx.restoring = true;
                try {
                    focusElement(control, doc);
                } finally {
                    ctx.restoring = false;
                }
            } else if (lost && !isVisible(control) && resolveScope(doc) === doc.body) {
                const neighbour = footerFallback(ctx, control);
                if (neighbour) {
                    clearNavFocus(ctx);
                    ctx.restoring = true;
                    try {
                        focusElement(neighbour, doc);
                    } finally {
                        ctx.restoring = false;
                    }
                }
            }
            return;
        }
        if (!lost) {
            clearNavFocus(ctx);
            return;
        }
        const scope = resolveScope(doc);
        const listTarget = scope === doc.body
            ? restoredCardTarget(ctx.cardFocus, ctx.navRect) || restoredTrackTarget(ctx.trackFocus, doc)
            : null;
        if (scope === doc.body && !listTarget) {
            // The dialog closed: the modal manager restores the opener.
            clearNavFocus(ctx);
            return;
        }
        const target = listTarget || nearestToRect(ctx.navRect, scope);
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
        revealFocus(element, doc);
        const ctx = docStates.get(doc);
        if (ctx) setNavFocus(ctx, element);
        return true;
    }

    function revealFocus(element, doc) {
        element.scrollIntoView?.({ block: 'nearest', inline: 'nearest', behavior: 'instant' });
        // scrollIntoView knows nested scrollports, but not the fixed player.
        const footer = doc.querySelector('.playback-bar');
        if (!footer || footer.contains(element) || resolveScope(doc) !== doc.body) return;
        const rect = rectOf(element);
        const bar = rectOf(footer);
        if (!rect || !bar || rect.right <= bar.left || rect.left >= bar.right) return;
        if (rect.bottom > bar.top - 6) {
            doc.defaultView?.scrollBy({ top: rect.bottom - bar.top + 6, behavior: 'instant' });
        }
    }

    // A sibling scrollport's off-screen children have misleading page-space
    // coordinates. Only its exposed controls compete with the current tile;
    // children in a shared scrollport remain eligible for scroll-to-focus.
    function inIndependentScrollport(element, current, doc) {
        let rect = rectOf(element);
        for (let parent = element.parentElement; parent && !parent.contains(current); parent = parent.parentElement) {
            const style = doc.defaultView.getComputedStyle(parent);
            const box = rectOf(parent);
            if (!rect || !box) continue;
            if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
                if (rect.bottom <= box.top || rect.top >= box.bottom) return true;
            }
            if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
                if (rect.right <= box.left || rect.left >= box.right) return true;
            }
        }
        return false;
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

    function navigationCard(element) {
        const card = element?.closest(CARD_SELECTOR);
        return card && card.tabIndex >= 0 && !isDisabled(card) ? card : null;
    }

    function cardKey(card) {
        for (const key of ['albumId', 'playlistId', 'smartFavorite', 'stationId']) {
            if (card.dataset[key] !== undefined) return JSON.stringify([key, card.dataset[key]]);
        }
        const favorite = card.querySelector(CARD_FAVORITE_SELECTOR);
        return favorite?.dataset.favId !== undefined
            ? JSON.stringify([favorite.dataset.favType, favorite.dataset.favId]) : null;
    }

    function restoredCardTarget(snapshot, rect) {
        let anchor = snapshot?.anchor;
        if (anchor && !anchor.isConnected && snapshot.anchorParent?.isConnected) {
            // TIDAL playlists replace the results node, not just its children.
            anchor = Array.from(snapshot.anchorParent.querySelectorAll('[id]'))
                .find(element => element.id === anchor.id);
        }
        if (!anchor?.isConnected || !isVisible(anchor)) return null;
        const cards = focusablesIn(anchor).filter(element => element.matches(CARD_SELECTOR));
        const same = snapshot.key !== null && cards.find(card => cardKey(card) === snapshot.key);
        if (same) {
            const favorite = snapshot.favorite && focusablesIn(same).find(element => element.matches(CARD_FAVORITE_SELECTOR));
            return favorite || same;
        }
        // A removed favorite yields its slot to the next card, not its heart.
        return cards[Math.min(snapshot.index, cards.length - 1)]
            || nearestToRect(rect, anchor.closest('.radio-panel, .library-panel, .streaming-panel'));
    }

    function isCardFavorite(element) {
        return element.matches(CARD_FAVORITE_SELECTOR) && navigationCard(element);
    }

    function trackPlay(row) {
        return focusablesIn(row).find(element => element.matches(TRACK_PLAY_SELECTOR)) || null;
    }

    function trackRow(element) {
        const row = element?.closest(TRACK_ROW_SELECTOR);
        return row && trackPlay(row) ? row : null;
    }

    // Siblings only: track lists can be long, so no full-list scan per key.
    function adjacentTrackRow(row, direction) {
        const step = direction === 'down' ? 'nextElementSibling' : 'previousElementSibling';
        for (let item = row[step]; item; item = item[step]) {
            if (trackRow(item) === item) return item;
        }
        return null;
    }

    function firstTrackRow(list) {
        for (let item = list.firstElementChild; item; item = item.nextElementSibling) {
            if (trackRow(item) === item) return item;
        }
        return null;
    }

    // An opened album/playlist detail starts on its first track.
    function detailTrackEntry(doc) {
        for (const view of doc.querySelectorAll(DETAIL_VIEW_SELECTOR)) {
            if (!isVisible(view)) continue;
            const row = Array.from(view.querySelectorAll(TRACK_ROW_SELECTOR)).find(item => trackRow(item) === item);
            if (row) return trackPlay(row);
        }
        return null;
    }

    function rowKey(row) {
        for (const key of ['trackId', 'playlistId', 'folder']) {
            if (row.dataset[key] !== undefined) return JSON.stringify([key, row.dataset[key]]);
        }
        return null;
    }

    // A re-rendered list keeps the same row and control.
    function restoredTrackTarget(snapshot, doc) {
        if (!snapshot?.anchorId || snapshot.key === null) return null;
        const anchor = doc.getElementById(snapshot.anchorId);
        if (!anchor || !isVisible(anchor)) return null;
        const row = Array.from(anchor.querySelectorAll(TRACK_ROW_SELECTOR))
            .find(item => rowKey(item) === snapshot.key && trackRow(item) === item);
        if (!row) return null;
        return focusablesIn(row)[snapshot.control] || trackPlay(row);
    }

    function overlapsAxis(from, to, direction) {
        return direction === 'left' || direction === 'right'
            ? Math.min(from.bottom, to.bottom) - Math.max(from.top, to.top) > 1
            : Math.min(from.right, to.right) - Math.max(from.left, to.left) > 1;
    }

    function effectsTarget(doc, scope, current, effectsCard, direction) {
        const group = current.closest(EFFECT_GROUP_SELECTOR);
        if (group) {
            const items = focusablesIn(group);
            const index = items.indexOf(current);
            const next = index < 0 ? null : items[index + (direction === 'right' ? 1 : -1)];
            if (next) return next;
        }
        const from = rectOf(current);
        const band = rectOf(effectsCard);
        if (!from || !band) return null;
        const candidates = focusablesIn(scope)
            .filter(element => {
                if (element === current || group?.contains(element)) return false;
                // From a group edge, never into another effect group.
                if (group && element.closest(EFFECT_GROUP_SELECTOR)) return false;
                const card = element.closest(EFFECTS_CARD_SELECTOR);
                const rect = card && rectOf(card);
                return rect && Math.min(rect.bottom, band.bottom) - Math.max(rect.top, band.top) > 1
                    && !inIndependentScrollport(element, current, doc);
            })
            .map(element => ({ element, rect: rectOf(element) }))
            .filter(candidate => candidate.rect);
        const local = candidates.filter(candidate => effectsCard.contains(candidate.element));
        return pickSpatial(from, local, direction) || pickSpatial(from, candidates, direction);
    }

    // DSP cards: Left/Right stay among the cards beside each other (no jump
    // to the header or footer), and the opposite key retraces the last steps.
    function effectsStep(doc, scope, current, effectsCard, direction) {
        const ctx = docStates.get(doc);
        const trail = ctx ? ctx.effectsTrail : [];
        const last = trail[trail.length - 1];
        if (last && last.to === current && last.direction !== direction && last.from.isConnected
                && isVisible(last.from) && !isDisabled(last.from) && scope.contains(last.from)) {
            trail.pop();
            return last.from;
        }
        const target = effectsTarget(doc, scope, current, effectsCard, direction);
        if (last && (last.to !== current || last.direction !== direction)) trail.length = 0;
        if (target) trail.push({ from: current, to: target, direction });
        return target;
    }

    function centerY(rect) {
        return (rect.top + rect.bottom) / 2;
    }

    // The footer is one zone with the same rules for every source: Left and
    // Right stay in the row, Up and Down stay in the column (the seek slider
    // is a row of its own), and a missing neighbour is an edge, not a jump
    // into the page.
    function footerStep(footer, current, direction) {
        const from = rectOf(current);
        if (!from) return null;
        const horizontal = direction === 'left' || direction === 'right';
        const inSeekRow = !!current.closest(FOOTER_SEEK_SELECTOR);
        const candidates = focusablesIn(footer)
            .filter(element => element !== current && !element.contains(current)
                && (!horizontal || !!element.closest(FOOTER_SEEK_SELECTOR) === inSeekRow))
            .map(element => ({ element, rect: rectOf(element) }))
            .filter(candidate => candidate.rect && overlapsAxis(from, candidate.rect, direction));
        return pickSpatial(from, candidates, direction);
    }

    function moveFocus(doc, direction) {
        const scope = resolveScope(doc);
        const active = doc.activeElement;
        const current = active && active !== doc.body && scope.contains(active) && isVisible(active)
            ? active
            : null;
        const ctx = docStates.get(doc);
        const effectsCard = current && (direction === 'left' || direction === 'right')
            && current.closest(EFFECTS_CARD_SELECTOR);
        if (effectsCard) {
            const target = effectsStep(doc, scope, current, effectsCard, direction);
            return target ? focusElement(target, doc) : false;
        }
        if (ctx) ctx.effectsTrail = [];
        if (!current) {
            const stray = scope === doc.body && ctx?.navFocus ? footerFallback(ctx, ctx.navFocus) : null;
            const initial = stray || (scope === doc.body && detailTrackEntry(doc)) || firstVisibleCandidate(scope, doc);
            return initial ? focusElement(initial, doc) : false;
        }
        const footer = scope === doc.body ? current.closest(FOOTER_SELECTOR) : null;
        if (footer) {
            const stop = footerStep(footer, current, direction);
            if (stop) return focusElement(stop, doc);
            // Only Up leaves the footer, and only into the page above it.
            if (direction !== 'up') return false;
        }
        const row = trackRow(current);
        if (row && (direction === 'left' || direction === 'right')) {
            const from = rectOf(current);
            const controls = focusablesIn(row).filter(element => element !== current)
                .map(element => ({ element, rect: rectOf(element) }))
                .filter(candidate => candidate.rect);
            const control = from && pickSpatial(from, controls, direction);
            return control ? focusElement(control, doc) : false;
        }
        const nextRow = row && adjacentTrackRow(row, direction);
        if (nextRow) return focusElement(trackPlay(nextRow), doc);
        const card = navigationCard(current);
        const favorite = card && focusablesIn(card).find(element => element.matches(CARD_FAVORITE_SELECTOR));
        // Cards are the primary grid stops. Up explicitly enters the heart;
        // Down returns, while another Up leaves from the whole card's column.
        if (current === card && direction === 'up' && favorite) return focusElement(favorite, doc);
        if (card && current === favorite && direction === 'down') return focusElement(card, doc);
        // Leaving a track list starts from the track's Play button.
        const origin = row ? trackPlay(row) : (card && current === favorite ? card : current);
        const from = rectOf(origin);
        if (!from) {
            const initial = firstVisibleCandidate(scope, doc);
            return initial ? focusElement(initial, doc) : false;
        }
        // Track lists are left and entered vertically only (e.g. Left from
        // a detail's Back button must reach the header, not a track).
        const horizontal = direction === 'left' || direction === 'right';
        // Page content behind the fixed footer is not "above" it.
        const footerTop = footer ? rectOf(footer)?.top : null;
        let candidates = focusablesIn(scope)
            .filter(element => element !== origin && !element.contains(current) && !isCardFavorite(element)
                && !(row && row.parentElement.contains(element))
                && !(horizontal && element.closest(TRACK_ROW_SELECTOR))
                && !(footer && footer.contains(element))
                && !inIndependentScrollport(element, current, doc))
            .map(element => ({ element, rect: rectOf(element) }))
            .filter(candidate => candidate.rect
                && !(footerTop !== null && centerY(candidate.rect) >= footerTop));
        if (footerTop !== null) {
            // Up from the footer lands on what is visible above it, not on a
            // control far up the page that merely shares its column.
            const visible = candidates.filter(candidate => centerY(candidate.rect) > 0);
            if (visible.length) candidates = visible;
        }
        // A detail view keeps its header (favorite, Back) and list together.
        const tile = current.closest(DETAIL_VIEW_SELECTOR
            + ', .effects-card, .radio-panel, .library-panel, .streaming-panel, .manage-dialog');
        const local = tile ? candidates.filter(candidate => tile.contains(candidate.element)) : [];
        const peers = card ? candidates.filter(candidate => candidate.element.matches(CARD_SELECTOR)
            && candidate.element.parentElement === card.parentElement
            && overlapsAxis(from, candidate.rect, direction)) : [];
        if (card && (direction === 'left' || direction === 'right')) {
            const peer = pickSpatial(from, peers, direction);
            return peer ? focusElement(peer, doc) : false;
        }
        // Down walks the rest of the page first; the fixed footer, which is
        // always close, only wins when nothing is left below.
        const pageCandidates = direction === 'down' && !footer
            ? candidates.filter(candidate => !candidate.element.closest(FOOTER_SELECTOR))
            : candidates;
        const target = pickSpatial(from, peers, direction)
            || pickSpatial(from, local, direction) || pickSpatial(from, pageCandidates, direction)
            || pickSpatial(from, candidates, direction);
        // Entering a track list lands on a Play button, from above on the first.
        const entered = !row && target && trackRow(target);
        if (entered) {
            return focusElement(trackPlay(direction === 'down' ? firstTrackRow(entered.parentElement) : entered), doc);
        }
        return target ? focusElement(target, doc) : false;
    }

    // Escape can release a native editor without closing the page.
    function isNativeKeyOwner(element) {
        if (!element || element.nodeType !== 1) return false;
        if (isTextEntry(element)) return true;
        if (element.tagName === 'SELECT') return true;
        if (element.tagName !== 'INPUT') return false;
        return String(element.getAttribute('type') || 'text').toLowerCase() === 'range';
    }

    function releaseNativeFocus(doc) {
        const active = doc.activeElement;
        if (!isNativeKeyOwner(active)) return false;
        const target = nearestToRect(rectOf(active), resolveScope(doc), active);
        // The released field must not take the focus back after a re-render.
        const ctx = docStates.get(doc);
        if (ctx) clearNavFocus(ctx);
        active.blur();
        if (target) focusElement(target, doc);
        return true;
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
        // Every detail view keeps its back button in the DOM; only the one of
        // the visible view counts. The first match in document order is often
        // a hidden button of another tab.
        for (const control of doc.querySelectorAll(BACK_CONTROL_SELECTOR)) {
            if (isVisible(control)) return control;
        }
        return null;
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

    // An inline panel opened by a toggle (Library/DSP Import).
    function openPanelToggle(doc) {
        for (const toggle of doc.querySelectorAll('button[aria-expanded="true"][aria-controls]')) {
            if (toggle.hasAttribute('aria-haspopup') || toggle.closest('[role="dialog"], [role="menu"]')) continue;
            const panel = doc.getElementById(toggle.getAttribute('aria-controls'));
            if (isVisible(toggle) && panel && isVisible(panel)) return toggle;
        }
        return null;
    }

    // Home (the remote sends it as BrowserBack) steps back one tier per press:
    //  1. in a dialog: dialogs stacked over the first one close together, else
    //     the first dialog collapses to its main view (measurement setup,
    //     menus), else it closes;
    //  2. on the page: the power menu, open panels and detail views close;
    //  3. with nothing left to close the Technical settings open, and the
    //     next press closes them again.
    function homeAction(doc) {
        const modal = root.FXRouteModal;
        if (modal?.topRoot?.()) {
            if (modal.depth?.() > 1) {
                for (let guard = 0; guard < 8 && modal.depth() > 1 && closeTopDialog(doc); guard++);
                return true;
            }
            return modal.collapseTop?.() || closeTopDialog(doc);
        }
        let closed = closePowerMenu(doc);
        for (let guard = 0; guard < 4; guard++) {
            const control = openPanelToggle(doc) || findBackControl(doc);
            if (!control) break;
            control.click();
            closed = true;
        }
        if (closed) return true;
        const open = doc.getElementById('open-settings');
        if (!open) return false;
        open.click();
        return true;
    }

    function closeSourcePicker(doc) {
        const panel = doc.getElementById('source-picker');
        if (!panel) return false;
        const wasOpen = !panel.classList.contains('hidden');
        // Close through the modal manager even if the panel was already
        // hidden: a stale stack entry would keep the page inert.
        root.FXRouteModal?.close?.(panel);
        panel.classList.add('hidden');
        doc.getElementById('source-select')?.setAttribute('aria-expanded', 'false');
        return wasOpen;
    }

    function openSourcePicker(doc) {
        const panel = doc.getElementById('source-picker');
        const list = doc.getElementById('source-picker-list');
        const select = doc.getElementById('source-select');
        const switcher = doc.getElementById('source-switcher');
        if (!panel || !list || !select || !switcher) return false;
        // Do not stack the picker on another dialog (settings, power menu).
        const top = root.FXRouteModal?.topRoot?.();
        if (top && top !== panel) return false;
        // During app playback the footer has no line-input entries. The
        // settings select still owns the available input modes and handlers.
        if (switcher.classList.contains('hidden') || select.disabled || !select.options.length) {
            const source = doc.getElementById('settings-source-select');
            const ctx = docStates.get(doc);
            if (!source || !ctx || !openSelect(source, ctx)) return false;
            doc.getElementById('tv-select-picker').dataset.sourcePicker = 'true';
            return true;
        }
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
            const opener = isVisible(select) ? select : undefined;
            root.FXRouteModal.open(panel, {
                initialFocus: initial || list,
                onEscape: () => closeSourcePicker(doc),
                opener,
            });
            focusElement(doc.activeElement, doc);
        } else if (initial) {
            focusElement(initial, doc);
        }
        return true;
    }

    function toggleSourcePicker(doc) {
        if (root.FXRouteModal?.topRoot?.()?.dataset.sourcePicker === 'true') return closeTopDialog(doc);
        const panel = doc.getElementById('source-picker');
        if (panel && !panel.classList.contains('hidden')) return closeSourcePicker(doc);
        return openSourcePicker(doc);
    }

    // Remote OK uses a DOM dialog rather than a platform popup: Firefox's
    // native select popup does not consistently open on Enter under Wayland.
    // Pointer activation is untouched and continues to use the native select.
    function openSelect(element, ctx) {
        const doc = ctx.doc;
        if (element.disabled || !element.options.length || !root.FXRouteModal?.open) return false;
        const panel = doc.createElement('div');
        panel.id = 'tv-select-picker';
        panel.className = 'manage-overlay';
        panel.innerHTML = '<div class="manage-overlay-backdrop"></div>'
            + '<section class="manage-dialog source-picker-dialog" role="dialog" aria-modal="true" aria-labelledby="tv-select-title">'
            + '<div class="manage-dialog-header"><h2 id="tv-select-title" class="section-title"></h2>'
            + '<button type="button" class="btn-ghost" aria-label="Close selection">✕</button></div>'
            + '<div class="source-picker-list" role="listbox"></div></section>';
        panel.querySelector('h2').textContent = element.labels?.[0]?.textContent?.trim()
            || element.getAttribute('aria-label') || 'Select';
        const expanded = element.getAttribute('aria-expanded');
        const active = doc.activeElement;
        const opener = isVisible(element) ? element
            : (active !== doc.body && isVisible(active) ? active : firstVisibleCandidate(resolveScope(doc), doc));
        const close = () => {
            root.FXRouteModal.close(panel);
            panel.remove();
            if (expanded === null) element.removeAttribute('aria-expanded');
            else element.setAttribute('aria-expanded', expanded);
            if (isVisible(opener) && !isDisabled(opener)) focusElement(opener, doc);
        };
        const list = panel.querySelector('[role="listbox"]');
        let initial = null;
        for (const option of element.options) {
            if (option.hidden) continue;
            // Polls replace <option> nodes; a detached option's index is 0.
            const value = option.value;
            const index = option.index;
            const button = doc.createElement('button');
            button.type = 'button';
            button.className = 'source-picker-option';
            button.setAttribute('role', 'option');
            button.setAttribute('aria-selected', String(option.selected));
            button.textContent = option.label;
            button.disabled = option.disabled || option.parentElement?.disabled === true;
            button.addEventListener('click', () => {
                const options = Array.from(element.options);
                let selected = options[index];
                if (!selected || selected.value !== value) selected = options.find(item => item.value === value);
                if (!element.isConnected || element.disabled || !selected
                    || selected.disabled || selected.parentElement?.disabled === true) {
                    close();
                    return;
                }
                const changed = element.selectedIndex !== selected.index;
                element.selectedIndex = selected.index;
                close();
                if (changed) {
                    element.dispatchEvent(new root.Event('input', { bubbles: true }));
                    element.dispatchEvent(new root.Event('change', { bubbles: true }));
                }
            });
            list.append(button);
            if (option.selected && !button.disabled) initial = button;
        }
        panel.querySelector('button').addEventListener('click', close);
        panel.querySelector('.manage-overlay-backdrop').addEventListener('click', close);
        doc.body.append(panel);
        element.setAttribute('aria-expanded', 'true');
        root.FXRouteModal.open(panel, { opener, initialFocus: initial || list.querySelector('button:not(:disabled)'), onEscape: close });
        focusElement(doc.activeElement, doc);
        return true;
    }

    // ------------------------------------------------------------------
    // Key handling
    // ------------------------------------------------------------------

    function inputType(element) {
        return String(element.getAttribute?.('type') || 'text').toLowerCase();
    }

    // Enter/Space must behave like a click on the focused element. Native
    // controls keep their own activation (buttons, links, summaries, picker
    // inputs); everything else that accepts a click gets one. This covers
    // list rows, cards and ARIA controls, including rows without a role.
    function activateFocused(element, event) {
        if (!element || element.nodeType !== 1) return false;
        const tag = element.tagName;
        // Nothing focused: Space must keep scrolling the page.
        if (tag === 'BODY' || tag === 'HTML') return false;
        if (isTextEntry(element) || tag === 'SELECT' || tag === 'TEXTAREA') return false;
        const space = event.key === ' ';
        if (tag === 'BUTTON' || tag === 'SUMMARY') return false;
        if (tag === 'A' && element.hasAttribute('href')) {
            if (!space) return false; // Enter is native for links
            event.preventDefault();
            element.click();
            return true;
        }
        if (tag === 'INPUT') {
            const type = inputType(element);
            // Checkboxes and radios do not activate on Enter natively.
            if (type === 'checkbox' || type === 'radio') {
                if (space) return false; // Space toggles natively
                event.preventDefault();
                element.click();
                return true;
            }
            return false; // submit/button/picker inputs are native
        }
        if (typeof element.click !== 'function') return false;
        event.preventDefault();
        element.click();
        return true;
    }

    // A text field the user types into keeps the keys it uses natively:
    // Option/Alt+Left (word jump) and PageUp/PageDown. A field the remote only
    // rests on (D-pad focus, OK not pressed yet) hands them to navigation, and
    // the real BrowserBack key always navigates.
    function typedTextOwnsKey(ctx, target, action, event) {
        if (!isTextEntry(target)) return false;
        if (ctx.navFocus === target && ctx.editing !== target) return false;
        if (action === 'tabs-previous' || action === 'tabs-next') return true;
        return action === 'browser-back' && event.altKey === true;
    }

    function handleKeyDown(event, ctx) {
        if (event.defaultPrevented || event.isComposing) return;
        if (event.key === 'Tab') {
            ctx.remote = false;
            setEditing(ctx, null);
            ctx.effectsTrail = [];
            ctx.navFocus?.classList?.remove(FOCUS_CLASS);
            clearNavFocus(ctx);
            return;
        }
        const action = keyAction(event);
        if (!action) {
            if (event.key?.length === 1 && isTextEntry(event.target)) setEditing(ctx, event.target);
            // Escape is the only way out of a control that owns all arrows.
            // An open dialog keeps Escape for the modal manager.
            if (event.key === 'Escape' && !root.FXRouteModal?.topRoot?.()) {
                if (isArmedSlider(ctx, event.target)) {
                    setEditing(ctx, null);
                    event.preventDefault();
                } else if (releaseNativeFocus(ctx.doc)) {
                    event.preventDefault();
                }
            }
            return;
        }
        if (event.ctrlKey || event.metaKey) return;
        if (event.altKey && action !== 'browser-back') return;
        // Activation runs after the page's own key handlers (bubble), so a
        // control that already handles Enter/Space is not clicked twice.
        if (action === 'activate') return;
        const doc = ctx.doc;
        const target = event.target && event.target.nodeType === 1 ? event.target : doc.activeElement;
        if (typedTextOwnsKey(ctx, target, action, event)) return;
        ctx.remote = true;

        if (action === 'tabs-previous' || action === 'tabs-next') {
            event.preventDefault();
            if (root.FXRouteModal?.topRoot?.()?.id === 'tv-select-picker') closeTopDialog(doc);
            stepTab(doc, action === 'tabs-next' ? 1 : -1);
            return;
        }

        if (action === 'browser-back') {
            event.preventDefault();
            event.stopPropagation();
            if (event.repeat) return;
            // Back first ends a slider adjustment, like Backspace does.
            if (isArmedSlider(ctx, target)) setEditing(ctx, null);
            else homeAction(doc);
            return;
        }
        if (action === 'source-picker') {
            event.preventDefault();
            event.stopPropagation();
            if (event.repeat) return;
            // The same physical menu key also emits a contextmenu event;
            // suppress its picker toggle instead of closing again.
            ctx.sourcePickerHandledAt = Date.now();
            toggleSourcePicker(doc);
            return;
        }
        // A text field reached by D-pad is a navigation stop until OK enters
        // editing. Up/Down can always leave a single-line remote editor.
        const remoteText = isTextEntry(target) && ctx.navFocus === target;
        const remoteSlider = isRangeInput(target) && ctx.navFocus === target;
        const adjusting = remoteSlider && ctx.editing === target;
        if (isTextEntry(target) && action === 'back') return;
        if (isTextEntry(target) && !remoteText) return;
        if (remoteText && ctx.editing === target
            && (target.tagName === 'TEXTAREA' || target.isContentEditable
                || action === 'nav-left' || action === 'nav-right' || action === 'back')) return;
        if (action === 'back') {
            event.preventDefault();
            if (adjusting) setEditing(ctx, null);
            else goBack(doc);
            return;
        }
        if (action.startsWith('nav-')) {
            const direction = action.slice(4);
            // A text field or slider reached by D-pad is a navigation stop;
            // a slider owns Left/Right only after OK armed it.
            const remoteStop = remoteText || (remoteSlider && !adjusting);
            if (!remoteStop && isNativeArrowOwner(target, direction)) return;
            event.preventDefault();
            setEditing(ctx, null);
            moveFocus(doc, direction);
        }
    }

    function handleActivation(event, ctx) {
        if (event.defaultPrevented || event.isComposing) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (keyAction(event) !== 'activate' || event.repeat) return;
        const target = event.target && event.target.nodeType === 1
            ? event.target
            : ctx.doc.activeElement;
        ctx.remote = true;
        if (target.tagName === 'SELECT' && event.key === 'Enter') {
            event.preventDefault();
            openSelect(target, ctx);
            return;
        }
        if (isTextEntry(target)) {
            if (ctx.navFocus === target && event.key === 'Enter') {
                setEditing(ctx, ctx.editing === target ? null : target);
            }
            return;
        }
        // OK arms a slider reached by the remote; OK again releases it.
        if (isRangeInput(target) && ctx.navFocus === target) {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                setEditing(ctx, ctx.editing === target ? null : target);
            }
            return;
        }
        activateFocused(target, event);
    }

    // Firefox turns the menu/Compose key (and Shift+F10) into a contextmenu
    // event with button 0 (-1 in Chromium). Under Wayland keydown can be `Unidentified`,
    // so this event is the reliable way to open the picker. Mouse right-clicks
    // use button 2 and keep the browser menu.
    function handleContextMenu(event, ctx) {
        if (event.button > 0) return;
        event.preventDefault();
        event.stopPropagation();
        ctx.remote = true;
        const now = Date.now();
        // Firefox fires this event on both the press and the release of the
        // menu/Compose key. Toggling twice would open and immediately close
        // the picker, so one press may only toggle once.
        if (now - (ctx.contextMenuAt || 0) < 600) return;
        ctx.contextMenuAt = now;
        if (now - (ctx.sourcePickerHandledAt || 0) < 700) return;
        toggleSourcePicker(ctx.doc);
    }

    // Firefox runs its history Back for the remote's BrowserBack key even when
    // the page handles and cancels it, which sent Home to the browser start
    // page. A guard entry keeps Back (also Alt+Left and the mouse button)
    // inside the app: popstate puts the guard back.
    const HISTORY_GUARD = 'fxrouteBackGuard';

    function guardHistory(win) {
        const history = win?.history;
        if (!history || typeof history.pushState !== 'function') return;
        // The public web demo must not trap its visitors.
        if (win.FXROUTE_DEMO_BOOTED) return;
        const push = () => {
            try {
                history.pushState({ [HISTORY_GUARD]: true }, '', win.location.href);
            } catch (_error) { /* sandboxed documents may refuse */ }
        };
        if (!history.state?.[HISTORY_GUARD]) push();
        win.addEventListener('popstate', event => {
            if (!event.state?.[HISTORY_GUARD]) push();
        });
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
            cardFocus: null,
            trackFocus: null,
            effectsTrail: [],
            restoring: false,
            remote: false,
            editing: null,
            sourcePickerHandledAt: 0,
            contextMenuAt: 0,
            observer: typeof MutationObserver === 'function'
                ? new MutationObserver(() => restoreLostFocus(ctx))
                : null,
        };
        docStates.set(doc, ctx);
        // Window capture runs before page handlers that stop key propagation;
        // the modal manager still receives Escape at document capture.
        const win = doc.defaultView || (typeof window !== 'undefined' ? window : null);
        const keyTarget = win && typeof win.addEventListener === 'function' ? win : doc;
        guardHistory(win);
        keyTarget.addEventListener('keydown', event => handleKeyDown(event, ctx), true);
        // Bubble phase: page handlers run first and can consume the key.
        keyTarget.addEventListener('keydown', event => handleActivation(event, ctx), false);
        doc.addEventListener('contextmenu', event => handleContextMenu(event, ctx), true);
        doc.addEventListener('focusin', event => {
            if (ctx.navFocus && event.target !== ctx.navFocus) clearNavFocus(ctx);
            if (ctx.remote) {
                event.target.classList?.add(FOCUS_CLASS);
                revealFocus(event.target, doc);
                setNavFocus(ctx, event.target);
            }
        }, true);
        doc.addEventListener('pointerdown', () => {
            ctx.remote = false;
            setEditing(ctx, null);
            ctx.effectsTrail = [];
            ctx.navFocus?.classList?.remove(FOCUS_CLASS);
            clearNavFocus(ctx);
        }, true);
        doc.addEventListener('input', event => {
            if (isTextEntry(event.target)) setEditing(ctx, event.target);
        }, true);
        doc.addEventListener('focusout', event => {
            event.target?.classList?.remove(FOCUS_CLASS);
            if (event.target === ctx.editing && isRangeInput(event.target)) setEditing(ctx, null);
        }, true);
        doc.getElementById('source-picker-close')
            ?.addEventListener('click', () => closeSourcePicker(doc));
        doc.querySelector('#source-picker .manage-overlay-backdrop')
            ?.addEventListener('click', () => closeSourcePicker(doc));
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
