// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute shared modal manager.
 * Canonical owner of the dialog focus trap, background inerting and opener
 * focus restore used by settings, radio management and measurement panels.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    // Eager singleton preserves the previous app.js timing: the Escape/Tab
    // listener is registered at script load in the browser. In Node (no
    // document) only the factory is exposed and no listener is registered.
    if (typeof document !== 'undefined') {
        try {
            root.FXRouteModal = api.createFxrouteModalManager();
        } catch (_error) {
            root.FXRouteModal = api;
        }
    } else if (root && !root.FXRouteModal) {
        root.FXRouteModal = api;
    }
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function () {
    'use strict';

    function createFxrouteModalManager() {
        const stack = [];
        const managedInertElements = new Set();

        function focusElement(element) {
            if (!element || typeof element.focus !== 'function') return false;
            try {
                element.focus({ preventScroll: true });
            } catch (error) {
                element.focus();
            }
            return document.activeElement === element;
        }

        function isVisible(element) {
            const closedDetails = element.closest('details:not([open])');
            if (closedDetails && element.tagName !== 'SUMMARY') return false;
            return !element.hidden
                && element.getAttribute('aria-hidden') !== 'true'
                && (element.offsetParent !== null || element === document.activeElement);
        }

        function getFocusableElements(root) {
            if (!root) return [];
            const selector = [
                'a[href]',
                'area[href]',
                'button:not([disabled])',
                'input:not([disabled]):not([type="hidden"])',
                'select:not([disabled])',
                'textarea:not([disabled])',
                'summary',
                '[tabindex]:not([tabindex="-1"])',
            ].join(',');
            return Array.from(root.querySelectorAll(selector)).filter(isVisible);
        }

        function clearManagedInert() {
            managedInertElements.forEach(element => {
                element.inert = false;
            });
            managedInertElements.clear();
        }

        function containsProtectedRoot(element, protectedRoots) {
            return protectedRoots.some(root => element === root || element.contains(root));
        }

        function applyBackgroundInert(entry) {
            clearManagedInert();
            if (!entry) return;

            const protectedRoots = [entry.root, ...(entry.siblingRoots || [])]
                .filter(Boolean);
            protectedRoots.forEach(root => {
                let node = root;
                while (node && node.parentElement) {
                    const parent = node.parentElement;
                    Array.from(parent.children).forEach(sibling => {
                        if (sibling === node || containsProtectedRoot(sibling, protectedRoots)) return;
                        if (!(sibling instanceof HTMLElement) || sibling.inert) return;
                        sibling.inert = true;
                        managedInertElements.add(sibling);
                    });
                    node = parent;
                }
            });
        }

        function focusInitial(entry) {
            const focusables = getFocusableElements(entry.root);
            if (entry.initialFocus && isVisible(entry.initialFocus) && focusElement(entry.initialFocus)) return;
            if (focusElement(focusables[0])) return;
            focusElement(entry.dialog || entry.root);
        }

        function open(root, options = {}) {
            if (!root) return;
            const existingIndex = stack.findIndex(entry => entry.root === root);
            if (existingIndex >= 0) {
                const existing = stack.splice(existingIndex, 1)[0];
                stack.push(existing);
                applyBackgroundInert(existing);
                focusInitial(existing);
                return;
            }

            const dialog = options.dialog
                || (root.matches?.('[role="dialog"]') ? root : root.querySelector?.('[role="dialog"]'))
                || root;
            const opener = options.opener
                || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
            const entry = {
                root,
                dialog,
                opener,
                initialFocus: options.initialFocus || null,
                siblingRoots: options.siblingRoots || [],
                onEscape: options.onEscape,
            };
            stack.push(entry);
            applyBackgroundInert(entry);
            focusInitial(entry);
        }

        function close(root) {
            const index = stack.findIndex(entry => entry.root === root);
            if (index < 0) return;
            const wasTop = index === stack.length - 1;
            const [entry] = stack.splice(index, 1);
            applyBackgroundInert(stack[stack.length - 1]);
            if (!wasTop) return;

            const replacement = stack[stack.length - 1];
            if (replacement) {
                if (replacement.root.contains(entry.opener)) {
                    focusElement(entry.opener);
                } else {
                    focusInitial(replacement);
                }
                return;
            }
            focusElement(entry.opener);
        }

        function isOpen(root) {
            return stack.some(entry => entry.root === root);
        }

        document.addEventListener('keydown', event => {
            const entry = stack[stack.length - 1];
            if (!entry) return;

            if (event.key === 'Escape' && typeof entry.onEscape === 'function') {
                event.preventDefault();
                event.stopImmediatePropagation();
                void entry.onEscape(event);
                return;
            }
            if (event.key !== 'Tab') return;

            const focusables = getFocusableElements(entry.root);
            if (!focusables.length) {
                event.preventDefault();
                focusElement(entry.dialog || entry.root);
                return;
            }

            const active = document.activeElement;
            if (!entry.root.contains(active)) {
                event.preventDefault();
                focusElement(event.shiftKey ? focusables[focusables.length - 1] : focusables[0]);
                return;
            }

            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            if (event.shiftKey && active === first) {
                event.preventDefault();
                focusElement(last);
            } else if (!event.shiftKey && active === last) {
                event.preventDefault();
                focusElement(first);
            }
        }, true);

        return { open, close, isOpen };
    }

    function open(root, options) {
        if (typeof document === 'undefined') return;
        if (!open.shared) open.shared = createFxrouteModalManager();
        return open.shared.open(root, options);
    }

    function close(root) {
        if (typeof document === 'undefined') return;
        if (!close.shared) close.shared = createFxrouteModalManager();
        return close.shared.close(root);
    }

    function isOpen(root) {
        if (typeof document === 'undefined') return false;
        if (!isOpen.shared) isOpen.shared = createFxrouteModalManager();
        return isOpen.shared.isOpen(root);
    }

    return {
        createFxrouteModalManager,
        open,
        close,
        isOpen,
    };
});
