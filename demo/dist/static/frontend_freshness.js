// SPDX-License-Identifier: AGPL-3.0-only
/**
 * Keeps a long-running page on the assets the server serves.
 *
 * A kiosk or TV browser keeps one page open for days, so a deploy never
 * reaches it: it runs the JS it booted with, including every bug fixed since
 * (a connection banner that never clears, a stale "Loading…" line). The shell
 * lists each asset with a version, so a page is stale exactly when the shell
 * now lists other versions than the ones it booted with. A stale page reloads
 * itself once nobody is using it. Browser-loadable UMD, no build step;
 * Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteFrontendFreshness = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const CHECK_INTERVAL_MS = 15 * 60 * 1000;
    // Several triggers (visibility, interval) must not each fetch the shell.
    const MIN_CHECK_GAP_MS = 60 * 1000;
    const IDLE_BEFORE_RELOAD_MS = 60 * 1000;
    const RELOAD_RETRY_MS = 30 * 1000;
    const RELOADED_FOR_KEY = 'fxroute-frontend-reloaded-for';
    const VERSIONED_ASSET = /^\/static\/[^?#]+\.(?:js|css)\?v=/;
    const ASSET_ATTRIBUTE = /\b(?:src|href)="(\/static\/[^"?#]+\.(?:js|css)\?v=[^"#]+)"/g;
    const INPUT_EVENTS = ['pointerdown', 'keydown', 'wheel', 'touchstart'];

    let deps = {
        fetch: (...args) => root.fetch(...args),
        now: () => Date.now(),
        setTimeout: (...args) => root.setTimeout(...args),
        clearTimeout: (id) => root.clearTimeout(id),
        readBootAssets: () => Array.from(root.document.querySelectorAll('script[src], link[rel="stylesheet"]'))
            .map(el => el.getAttribute('src') || el.getAttribute('href') || ''),
        listenForInput: (handler) => {
            INPUT_EVENTS.forEach(name => root.document.addEventListener(name, handler, { capture: true, passive: true }));
        },
        isBusy: () => false,
        reload: () => root.location.reload(),
        storage: () => root.sessionStorage,
    };
    let started = false;
    let bootSignature = '';
    let staleSignature = '';
    let lastInputAt = 0;
    let lastCheckAt = -Infinity;
    let checking = false;
    let reloadTimer = null;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    // Versioned JS/CSS of a page, as one comparable string.
    function signature(urls) {
        return Array.from(new Set(urls.filter(url => VERSIONED_ASSET.test(url)))).sort().join('\n');
    }

    function markupAssets(markup) {
        return Array.from(String(markup).matchAll(ASSET_ATTRIBUTE), match => match[1]);
    }

    function noteInput() {
        lastInputAt = deps.now();
    }

    function reloadedFor() {
        try { return deps.storage().getItem(RELOADED_FOR_KEY) || ''; } catch (e) { return ''; }
    }

    function rememberReloadedFor(value) {
        try { deps.storage().setItem(RELOADED_FOR_KEY, value); } catch (e) { /* the guard degrades to the check interval */ }
    }

    function cancelReload() {
        if (reloadTimer !== null) deps.clearTimeout(reloadTimer);
        reloadTimer = null;
    }

    function reloadWhenIdle() {
        cancelReload();
        // A reload that did not make the page current must not repeat forever.
        if (!staleSignature || staleSignature === reloadedFor()) return;
        if (deps.isBusy() || deps.now() - lastInputAt < IDLE_BEFORE_RELOAD_MS) {
            reloadTimer = deps.setTimeout(reloadWhenIdle, RELOAD_RETRY_MS);
            return;
        }
        rememberReloadedFor(staleSignature);
        deps.reload();
    }

    // reason 'reconnect' bypasses the gap: a restarted server is the usual deploy.
    async function check(reason) {
        if (!started || checking) return;
        const now = deps.now();
        if (reason !== 'reconnect' && now - lastCheckAt < MIN_CHECK_GAP_MS) return;
        checking = true;
        lastCheckAt = now;
        try {
            const resp = await deps.fetch('/', { cache: 'no-store' });
            if (!resp.ok) return;
            const served = signature(markupAssets(await resp.text()));
            // An unreadable shell says nothing about the assets: never reload on it.
            if (!served || !bootSignature || served === bootSignature) {
                staleSignature = '';
                cancelReload();
                return;
            }
            staleSignature = served;
            reloadWhenIdle();
        } catch (e) {
            // Offline or restarting: the next trigger checks again.
        } finally {
            checking = false;
        }
    }

    function schedulePeriodicCheck() {
        deps.setTimeout(async () => {
            await check('interval');
            schedulePeriodicCheck();
        }, CHECK_INTERVAL_MS);
    }

    function start() {
        // The demo serves a transformed shell and a fake socket: nothing to follow.
        if (started || root.FXROUTE_DEMO_BOOTED) return;
        started = true;
        bootSignature = signature(deps.readBootAssets());
        lastInputAt = deps.now();
        deps.listenForInput(noteInput);
        schedulePeriodicCheck();
    }

    return { init, start, check, noteInput, signature, markupAssets };
});
