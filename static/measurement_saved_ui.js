// SPDX-License-Identifier: AGPL-3.0-only
/** Saved measurement list and frozen-area labels. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementSavedUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const ui = root.FXRouteMeasurementUI || {};
    let deps = {
        getState: () => ({ measurement: {} }),
        getElements: () => ({}),
        getCurrentMeasurementEntry: () => null,
        getVisibleMeasurementColorById: () => ({}),
        getOutputSystemModule: () => null,
        getCompactDisplayName: (name) => String(name || ''),
        escapeHtml: (value) => String(value == null ? '' : value),
        getDetailsElementType: () => root.HTMLDetailsElement,
        renderMeasurementPanel: () => {},
        mergeSelectedMeasurements: () => {},
        deleteSelectedMeasurements: () => {},
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function measurementAreaBadge(measurement) {
        /* The frozen area a saved result was captured in, from its measurement
         * target.  Legacy results (and demo data) carry no target and stay
         * unlabelled rather than being silently called "Global". */
        const target = measurement?.measurement_target;
        if (!target || target.legacy || target.schema !== 'fxroute.measurement-target') return null;
        const mod = deps.getOutputSystemModule();
        const label = (mod && typeof mod.roleLabel === 'function')
            ? mod.roleLabel(String(target.bank_id || 'global'))
            : String(target.bank_id || 'global');
        const mode = String(target.mode || '');
        const stale = !!measurement.measurement_target_stale;
        return {
            label,
            mode,
            stale,
            title: mode
                ? `${label} · ${(mod && typeof mod.modeLabel === 'function' ? mod.modeLabel(mode) : mode)} · measured ${label === 'Global' ? 'whole system' : 'area only'}`
                : label,
        };
    }

    function renderMeasurementPanelSavedListSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const selectedSavedCount = measurements.filter(measurement => measurementState.visibilityById?.[measurement.id]).length;
        const allSavedSelected = measurements.length > 0 && selectedSavedCount === measurements.length;
        const visibleMeasurementColorById = deps.getVisibleMeasurementColorById();

        const savedItemsHtml = measurements.map((measurement) => {
            const pointsLabel = ui.summarizeMeasurementEntry(measurement);
            const traceColor = visibleMeasurementColorById[measurement.id] || '';
            const isSelected = !!measurementState.visibilityById?.[measurement.id];
            const isVisibleInGraph = !!traceColor;
            const qualitySummary = ui.getMeasurementQualitySummary(measurement);
            const qualityTitle = ui.getMeasurementQualityTitle(measurement);
            const timingInfo = ui.getMeasurementTimingInfo(measurement);
            const micInputChannel = measurement.input_channels?.mic ? ` · Mic In ${measurement.input_channels.mic}` : '';
            const referenceInputChannel = measurement.input_channels?.electrical_reference ? ` · Ref In ${measurement.input_channels.electrical_reference}` : '';
            const areaBadge = measurementAreaBadge(measurement);
            const areaBadgeHtml = areaBadge
                ? `<span class="measurement-area-badge${areaBadge.stale ? ' is-stale' : ''}" data-tooltip="${deps.escapeHtml(areaBadge.title)}">${deps.escapeHtml(areaBadge.label)}</span>`
                : '';
            return `
                <div class="measurement-list-item" style="${isVisibleInGraph ? `border-color:${traceColor}; box-shadow: inset 0 0 0 1px ${traceColor}33; background: linear-gradient(180deg, rgba(255,255,255,0.03), ${traceColor}12);` : ''}">
                    <div class="measurement-list-row">
                        <span class="measurement-toggle">
                            <input type="checkbox" data-measurement-toggle="${deps.escapeHtml(measurement.id)}" ${isSelected ? 'checked' : ''}>
                            <span class="measurement-swatch ${isVisibleInGraph ? '' : 'measurement-swatch-inactive'}" ${isVisibleInGraph ? `style="background:${deps.escapeHtml(traceColor)}"` : ''}></span>
                            <span class="measurement-list-title"><a href="${deps.escapeHtml(ui.measurementFileUrl(measurement.id))}" data-tooltip="${deps.escapeHtml(measurement.name)}">${deps.escapeHtml(deps.getCompactDisplayName(measurement.name, 24))}</a></span>
                            ${areaBadgeHtml}
                        </span>
                        <span class="measurement-list-meta measurement-list-date">${deps.escapeHtml(ui.formatMeasurementDate(measurement.created_at))}</span>
                    </div>
                    <div class="measurement-list-row">
                        <span class="measurement-list-meta">${deps.escapeHtml(measurement.input_device?.label || 'Capture input')} · ${deps.escapeHtml(String(measurement.channel || 'left'))}${deps.escapeHtml(micInputChannel)}${deps.escapeHtml(referenceInputChannel)}</span>
                        <span class="measurement-list-points">${deps.escapeHtml(pointsLabel)}</span>
                    </div>
                    <div class="measurement-list-row">
                        <span class="measurement-list-meta" data-tooltip="${deps.escapeHtml(timingInfo.detail)}">${deps.escapeHtml(timingInfo.line)}</span>
                    </div>
                    <div class="measurement-list-row">
                        <span class="measurement-list-meta" data-tooltip="${deps.escapeHtml(qualityTitle)}">${deps.escapeHtml(qualitySummary)} · ${isVisibleInGraph ? 'visible, dashed compare trace' : 'hidden compare trace'}</span>
                    </div>
                </div>
            `;
        }).join('');

        const savedHtml = measurements.length
            ? `
                <details class="measurement-saved-group" ${measurementState.savedGroupOpen ? 'open' : ''}>
                    <summary>${measurementState.savedGroupOpen ? 'Close saved' : 'Open saved'} (${measurements.length})</summary>
                    <div class="measurement-saved-list">
                        <div class="measurement-saved-toolbar">
                            <label class="measurement-list-meta measurement-select-all-toggle"><input type="checkbox" data-measurement-select-all ${allSavedSelected ? 'checked' : ''} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''}>Select all</label>
                            <div class="measurement-saved-toolbar-selection">
                                <button type="button" class="btn-danger measurement-saved-delete-action ${selectedSavedCount ? '' : 'is-inert'}" data-measurement-delete-selected ${selectedSavedCount ? '' : 'disabled'} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''} aria-hidden="${selectedSavedCount ? 'false' : 'true'}" aria-label="Delete selected measurements"><span class="label-full">Delete selected</span><span class="label-compact" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></span></button>
                                <button type="button" class="btn-secondary measurement-saved-merge-action ${selectedSavedCount >= 2 ? '' : 'is-inert'}" data-measurement-merge-selected ${selectedSavedCount >= 2 ? '' : 'disabled'} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''} aria-hidden="${selectedSavedCount >= 2 ? 'false' : 'true'}" aria-label="Merge selected measurements"><span class="label-full">Merge selected</span><span class="label-compact" aria-hidden="true">⇄</span></button>
                            </div>
                        </div>
                        ${savedItemsHtml}
                    </div>
                </details>
            `
            : '';

        deps.getElements().measurementList.innerHTML = savedHtml;
    }

    function getSavedListMeasurements() {
        const measurementState = deps.getState().measurement || {};
        const current = deps.getCurrentMeasurementEntry();
        return (measurementState.measurements || []).filter(measurement => measurement.id !== current?.id);
    }

    let savedListDelegationBound = false;
    function bindMeasurementSavedListDelegation() {
        if (savedListDelegationBound) return;
        savedListDelegationBound = true;
        const list = deps.getElements().measurementList;
        // <details> toggle does not bubble; capture phase still reaches ancestors.
        list?.addEventListener('toggle', (event) => {
            const details = event.target;
            if (!(details instanceof (deps.getDetailsElementType()))) return;
            deps.getState().measurement.savedGroupOpen = !!details.open;
            const summary = details.querySelector('summary');
            if (summary) {
                summary.textContent = `${deps.getState().measurement.savedGroupOpen ? 'Close saved' : 'Open saved'} (${getSavedListMeasurements().length})`;
            }
        }, true);
        list?.addEventListener('change', (event) => {
            const input = event.target;
            if (input.matches('[data-measurement-toggle]')) {
                deps.getState().measurement.visibilityById[input.dataset.measurementToggle] = !!input.checked;
                deps.getState().measurement.savedGroupOpen = true;
                deps.renderMeasurementPanel();
                return;
            }
            if (input.matches('[data-measurement-select-all]')) {
                getSavedListMeasurements().forEach((measurement) => {
                    deps.getState().measurement.visibilityById[measurement.id] = !!input.checked;
                });
                deps.getState().measurement.savedGroupOpen = true;
                deps.renderMeasurementPanel();
            }
        });
        list?.addEventListener('click', (event) => {
            const mergeButton = event.target.closest('[data-measurement-merge-selected]');
            if (mergeButton) {
                deps.mergeSelectedMeasurements();
                return;
            }
            const deleteButton = event.target.closest('[data-measurement-delete-selected]');
            if (deleteButton) {
                deps.deleteSelectedMeasurements();
            }
        });
    }

    return { init, measurementAreaBadge, renderMeasurementPanelSavedListSection, getSavedListMeasurements, bindMeasurementSavedListDelegation };
});
