// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute output-system controller: catalog fetch and revisioned mutation
 * orchestration for the multichannel output state. output_state.js stays the
 * domain/state module (fetch/build/apply with 409 refresh and retry); this
 * controller owns call order and side effects, with every downstream refresh
 * (output section, banks, compare, crossover, speaker align, subwoofer,
 * auto-sub) behind explicit callbacks. Bank/import UI is not moved.
 * Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteOutputSystemController = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ outputSystem: { catalog: null, busy: false } }),
        showToast: () => {},
        ensureOutputBoxes: () => {},
        outputSystemModule: () => (root && root.FXRouteOutputState) || null,
        renderOutputSection: () => {},
        renderBankSelector: () => {},
        renderCompare: () => {},
        syncCrossover: () => {},
        syncSpeakerAlign: () => {},
        renderSubwoofer: () => {},
        syncAutoSub: () => {},
        reportMutationError: () => {},
        syncCompareBusy: () => {},
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    async function fetchOutputSystemCatalog(force = false) {
        deps.ensureOutputBoxes();
        const mod = deps.outputSystemModule();
        if (!mod) return null;
        if (deps.getState().outputSystem.catalog && !force) return deps.getState().outputSystem.catalog;
        try {
            deps.getState().outputSystem.catalog = await mod.fetchCatalog(fetch);
        } catch (e) {
            deps.getState().outputSystem.catalog = null;
        }
        deps.renderOutputSection();
        deps.renderCompare();
        // The crossover tile follows the catalog: a mode with crossover fetches
        // the response, otherwise the stale response clears. Both live behind
        // one callback so the order stays fixed here.
        const refreshedCatalog = deps.getState().outputSystem.catalog;
        deps.syncCrossover(!!refreshedCatalog?.modes?.[refreshedCatalog.active_mode]?.crossover_enabled);
        deps.syncSpeakerAlign();
        deps.renderSubwoofer();
        deps.syncAutoSub();
        return deps.getState().outputSystem.catalog;
    }

    async function applyOutputSystemMutation(kind, fields, successMessage, options = {}) {
        deps.ensureOutputBoxes();
        const mod = deps.outputSystemModule();
        if (!mod || !deps.getState().outputSystem.catalog || deps.getState().outputSystem.busy) return null;
        deps.getState().outputSystem.busy = true;
        deps.renderOutputSection();
        deps.renderBankSelector();
        try {
            const mutation = mod.buildMutation(kind, fields);
            const getCatalog = async () => {
                const fresh = await mod.fetchCatalog(fetch);
                deps.getState().outputSystem.catalog = fresh;
                return fresh;
            };
            const { data, catalog } = await mod.applyMutation(
                fetch, deps.getState().outputSystem.catalog, getCatalog, mutation);
            deps.getState().outputSystem.catalog = catalog;
            // The apply response carries no catalog: refetch so renders use
            // the committed revision instead of the pre-apply snapshot. A
            // failed refetch must not mask the successful apply.
            try {
                await fetchOutputSystemCatalog(true);
            } catch (e) {
                deps.renderOutputSection();
                deps.renderBankSelector();
            }
            if (successMessage) deps.showToast(successMessage, 'success');
            if (!options.quiet && data && data.live_applied === false && data.live_reason && data.live_reason !== 'nothing-to-apply') {
                deps.showToast(`Saved (revision ${data.revision}); live apply: ${data.live_reason}`, 'info');
            }
            return data;
        } catch (e) {
            deps.reportMutationError(mod.esc(e.message || 'Output system update failed'));
            deps.showToast(e.message || 'Output system update failed', 'error');
            return null;
        } finally {
            deps.getState().outputSystem.busy = false;
            deps.renderOutputSection();
            deps.renderBankSelector();
            // The busy disable in setEffectsCompareLoadBusy must be lifted here:
            // this finally is the only path that runs after every mutation, and
            // the bank selector render above does not touch the compare selects.
            deps.syncCompareBusy();
        }
    }

    return {
        init,
        fetchOutputSystemCatalog,
        applyOutputSystemMutation,
    };
});
