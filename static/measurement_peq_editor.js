// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement PEQ state, graph editing and preset staging. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementPeqEditor = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    const ui = root.FXRouteMeasurementUI || {};
    const dsp = root.FXRouteMeasurementDsp || {};
    let deps = {
        getState: () => ({ measurement: {}, dsp: {} }),
        getElements: () => ({}),
        getElementType: () => root.Element,
        getInputElementType: () => root.HTMLInputElement,
        getEventType: () => root.Event,
        fetch: (...args) => root.fetch(...args),
        showToast: () => {},
        renderMeasurementPanel: () => {},
        scheduleMeasurementGraphRender: () => {},
        getMeasurementActiveEditor: () => 'peq',
        setMeasurementActiveEditor: () => {},
        measurementBankSumsBothInputs: () => false,
        requireConcreteFilterBank: () => false,
        validatePeqBands: () => null,
        normalizePeqEqMode: (mode) => mode,
        collectEffectsExtras: () => ({}),
        bankBindingJson: () => ({}),
        measurementCommitSourceId: () => '',
        measurementPeqParams: () => ({}),
        formatTransitionErrorDetail: (_detail, fallback) => fallback,
        fetchEffects: async () => {},
        fetchOutputSystemCatalog: async () => {},
        isPeqCreateInFlight: () => false,
        setPeqCreateInFlight: () => {},
        getGraphPointerId: () => null,
        setGraphPointerId: () => {},
        getMeasurementGraphPointerPosition: () => null,
        getPeqHandleHitRadiusPx: () => 14,
        getPeqTouchHandleHitRadiusPx: () => 24,
        getPeqTouchCreateCooldownMs: () => 350,
        setTimeout: (...args) => root.setTimeout(...args),
        clearTimeout: (...args) => root.clearTimeout(...args),
    };
    let measurementPeqTakeFeedbackTimer = null;
    let measurementPeqLastTouchCreateAt = 0;

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function getDefaultMeasurementPeqFilter(index = 0) {
        return ui.getDefaultMeasurementPeqFilter(index);
    }

    function getDefaultMeasurementPeqState() {
        return ui.getDefaultMeasurementPeqState();
    }

    function ensureMeasurementPeqState() {
        if (!deps.getState().measurement) deps.getState().measurement = {};
        if (!deps.getState().measurement.peqAssistant || typeof deps.getState().measurement.peqAssistant !== 'object') {
            deps.getState().measurement.peqAssistant = getDefaultMeasurementPeqState();
        }
        const peq = deps.getState().measurement.peqAssistant;
        if (!Array.isArray(peq.filters)) peq.filters = [];
        if (typeof peq.enabled !== 'boolean') peq.enabled = peq.filters.length > 0;
        if (!peq.draft || typeof peq.draft !== 'object') peq.draft = { leftBands: [], rightBands: [], presetName: '', nameTouched: false };
        if (!Array.isArray(peq.draft.leftBands)) peq.draft.leftBands = [];
        if (!Array.isArray(peq.draft.rightBands)) peq.draft.rightBands = [];
        if (typeof peq.draft.presetName !== 'string') peq.draft.presetName = '';
        peq.draft.nameTouched = !!peq.draft.nameTouched;
        return peq;
    }

    function getMeasurementPeqFilters() {
        return ensureMeasurementPeqState().filters;
    }

    function getMeasurementPeqActiveFilter() {
        const peq = ensureMeasurementPeqState();
        return peq.filters.find((filter) => filter.id === peq.activeFilterId) || null;
    }

    function focusMeasurementPeqPanelContext() {
        if (!deps.getElements().measurementPeqPanel || deps.getElements().measurementPeqPanel.classList.contains('hidden')) return;
        deps.getElements().measurementPeqPanel.focus({ preventScroll: true });
    }

    function isEditableMeasurementPeqTarget(target) {
        if (!(target instanceof (deps.getElementType()))) return false;
        return !!target.closest('input, select, textarea, [contenteditable="true"]');
    }

    function handleMeasurementPeqNumberInputArrowKey(event) {
        const input = event.currentTarget;
        if (!(input instanceof (deps.getInputElementType())) || input.type !== 'number') return;
        if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
        event.preventDefault();
        event.stopPropagation();
        if (event.key === 'ArrowUp') {
            input.stepUp();
        } else {
            input.stepDown();
        }
        input.dispatchEvent(new (deps.getEventType())('input', { bubbles: true }));
    }

    function syncMeasurementPeqQInput(value) {
        const input = deps.getElements().measurementPeqEditor?.querySelector('#measurement-peq-q');
        if (input) input.value = Number(value).toFixed(2);
    }

    function stepActiveMeasurementPeqQ(direction = 1, step = 0.1) {
        const activeFilter = getMeasurementPeqActiveFilter();
        if (!activeFilter) return null;
        const nextValue = stepMeasurementPeqQ(activeFilter.id, direction, step);
        if (nextValue === null) return null;
        syncMeasurementPeqQInput(nextValue);
        deps.scheduleMeasurementGraphRender();
        return nextValue;
    }

    function handleMeasurementPeqGraphWheel(event) {
        if (!deps.getElements().measurementPanel || deps.getElements().measurementPanel.classList.contains('hidden')) return;
        if (!deps.getElements().measurementPeqPanel || deps.getElements().measurementPeqPanel.classList.contains('hidden')) return;
        if (event.ctrlKey || event.metaKey || event.altKey) return;
        if (ensureMeasurementPeqState().dragFilterId) return;
        if (Math.abs(Number(event.deltaY) || 0) < 1) return;
        const direction = event.deltaY > 0 ? -1 : 1;
        const nextValue = stepActiveMeasurementPeqQ(direction, 0.1);
        if (nextValue === null) return;
        event.preventDefault();
        focusMeasurementPeqPanelContext();
    }

    function selectMeasurementPeqFilter(filterId) {
        const peq = ensureMeasurementPeqState();
        peq.activeFilterId = peq.filters.some((filter) => filter.id === filterId) ? filterId : (peq.filters[0]?.id || null);
    }

    function clampMeasurementPeqFrequency(value) {
        return dsp.clampMeasurementPeqFrequency(value);
    }

    function clampMeasurementPeqGain(value) {
        return dsp.clampMeasurementPeqGain(value);
    }

    function clampMeasurementPeqQ(value) {
        return dsp.clampMeasurementPeqQ(value);
    }

    function addMeasurementPeqFilter(defaults = {}) {
        if (deps.getMeasurementActiveEditor() === 'houseCurve') return null;
        deps.setMeasurementActiveEditor('peq');
        const peq = ensureMeasurementPeqState();
        if (peq.filters.length >= 12) {
            deps.showToast('Measurement assistant supports up to 12 filters', 'warning');
            return null;
        }
        const filter = {
            ...getDefaultMeasurementPeqFilter(peq.filters.length),
            ...defaults,
        };
        filter.freqHz = Math.round(clampMeasurementPeqFrequency(filter.freqHz));
        filter.gainDb = Number(clampMeasurementPeqGain(filter.gainDb).toFixed(1));
        filter.q = Number(clampMeasurementPeqQ(filter.q).toFixed(2));
        peq.filters.push(filter);
        peq.enabled = true;
        peq.activeFilterId = filter.id;
        return filter;
    }

    function createMeasurementPeqFilterFromPoint({ x, y, bounds, range }) {
        return addMeasurementPeqFilter({
            freqHz: dsp.measurementXToFrequency(x, bounds),
            gainDb: dsp.measurementYToDb(y, bounds, range),
        });
    }

    function updateMeasurementPeqFilter(filterId, updates = {}) {
        const filter = getMeasurementPeqFilters().find((item) => item.id === filterId);
        if (!filter) return;
        if (updates.type) filter.type = ui.measurementPeqTypes.includes(updates.type) ? updates.type : filter.type;
        if (updates.freqHz !== undefined) filter.freqHz = Math.round(clampMeasurementPeqFrequency(updates.freqHz));
        if (updates.gainDb !== undefined) filter.gainDb = Number(clampMeasurementPeqGain(updates.gainDb).toFixed(1));
        if (updates.q !== undefined) filter.q = Number(clampMeasurementPeqQ(updates.q).toFixed(2));
    }

    function stepMeasurementPeqQ(filterId, direction = 1, step = 0.1) {
        const filter = getMeasurementPeqFilters().find((item) => item.id === filterId);
        if (!filter) return null;
        const nextValue = clampMeasurementPeqQ((Number(filter.q) || 0) + (direction * step));
        updateMeasurementPeqFilter(filterId, { q: nextValue });
        return Number(nextValue.toFixed(2));
    }

    function stepMeasurementPeqGain(filterId, direction = 1, step = 0.1) {
        const filter = getMeasurementPeqFilters().find((item) => item.id === filterId);
        if (!filter) return null;
        const nextValue = clampMeasurementPeqGain((Number(filter.gainDb) || 0) + (direction * step));
        updateMeasurementPeqFilter(filterId, { gainDb: nextValue });
        return Number(nextValue.toFixed(1));
    }

    function stepMeasurementPeqFrequency(filterId, direction = 1, step = 1) {
        const filter = getMeasurementPeqFilters().find((item) => item.id === filterId);
        if (!filter) return null;
        const nextValue = clampMeasurementPeqFrequency((Number(filter.freqHz) || 20) + (direction * step));
        updateMeasurementPeqFilter(filterId, { freqHz: nextValue });
        return Math.round(nextValue);
    }

    function deleteMeasurementPeqFilter(filterId) {
        const peq = ensureMeasurementPeqState();
        peq.filters = peq.filters.filter((filter) => filter.id !== filterId);
        peq.activeFilterId = peq.filters.some((filter) => filter.id === peq.activeFilterId) ? peq.activeFilterId : (peq.filters[0]?.id || null);
        peq.enabled = peq.filters.length > 0;
        peq.dragFilterId = null;
    }

    function measurementPeqFilterToBand(filter = {}) {
        return {
            filterType: filter.type || 'bell',
            frequencyHz: Math.round(clampMeasurementPeqFrequency(filter.freqHz)),
            gainDb: Number(clampMeasurementPeqGain(filter.gainDb).toFixed(1)),
            q: Number(clampMeasurementPeqQ(filter.q).toFixed(2)),
            delayMs: 0,
        };
    }

    function showMeasurementPeqTakeFeedback(message) {
        if (!deps.getElements().measurementPeqTakeFeedback) return;
        deps.getElements().measurementPeqTakeFeedback.textContent = message;
        deps.getElements().measurementPeqTakeFeedback.classList.add('is-visible');
        if (measurementPeqTakeFeedbackTimer) deps.clearTimeout(measurementPeqTakeFeedbackTimer);
        measurementPeqTakeFeedbackTimer = deps.setTimeout(() => {
            deps.getElements().measurementPeqTakeFeedback?.classList.remove('is-visible');
            measurementPeqTakeFeedbackTimer = null;
        }, 2200);
    }

    function getMeasurementPeqNameSuffix(date = new Date()) {
        return ui.getMeasurementPeqNameSuffix(date);
    }

    function getMeasurementPeqDraftMode(peq = ensureMeasurementPeqState()) {
        const hasLeft = !!peq.draft?.leftBands?.length;
        const hasRight = !!peq.draft?.rightBands?.length;
        if (hasLeft && hasRight) return 'both';
        if (hasRight) return 'right';
        if (hasLeft) return 'left';
        return null;
    }

    function getMeasurementPeqPresetName(mode = 'both', options = {}) {
        const prefix = mode === 'both' ? 'PEQ LR' : (mode === 'right' ? 'PEQ R' : 'PEQ L');
        const count = ensureMeasurementPeqState().filters.length || 0;
        const base = `${prefix} Measurement ${count}f`;
        return options.unique ? `${base} ${getMeasurementPeqNameSuffix()}` : base;
    }

    function takeMeasurementPeqToPreset(mode = 'both') {
        const peq = ensureMeasurementPeqState();
        if (!peq.filters.length) {
            deps.showToast('Add at least one measurement PEQ filter first', 'warning');
            return;
        }
        if (mode !== 'both' && deps.measurementBankSumsBothInputs()) {
            const warning = 'This area is fed by both inputs: take Both to stage the identical L/R correction.';
            showMeasurementPeqTakeFeedback(warning);
            deps.showToast(warning, 'warning');
            return;
        }
        const mappedBands = peq.filters.map((filter) => measurementPeqFilterToBand(filter));
        if (mode === 'left') {
            peq.draft.leftBands = mappedBands.map((band) => ({ ...band }));
        } else if (mode === 'right') {
            peq.draft.rightBands = mappedBands.map((band) => ({ ...band }));
        } else {
            peq.draft.leftBands = mappedBands.map((band) => ({ ...band }));
            peq.draft.rightBands = mappedBands.map((band) => ({ ...band }));
        }
        const effectiveMode = getMeasurementPeqDraftMode(peq) || mode;
        if (!peq.draft.nameTouched) peq.draft.presetName = getMeasurementPeqPresetName(effectiveMode, { unique: true });
        deps.getState().dsp = deps.getState().dsp || {};
        deps.getState().dsp.assistStack = deps.getState().dsp.assistStack || [];
        deps.getState().dsp.assistStack.push({ type: 'peq', mode, createdAt: new Date().toISOString(), bands: mappedBands.map((band) => ({ ...band })) });
        deps.renderMeasurementPanel();
        const successMessage = mode === 'left'
            ? 'Measurement PEQ staged Left bands'
            : (mode === 'right' ? 'Measurement PEQ staged Right bands' : 'Measurement PEQ staged Left and Right bands');
        showMeasurementPeqTakeFeedback(mode === 'left' ? 'Left staged' : (mode === 'right' ? 'Right staged' : 'Left + Right staged'));
        deps.showToast(successMessage, 'success');
    }

    function resolveMeasurementPeqPresetName(peq, fieldValue, mode) {
        // Mirrors the convolver field: the visible input is authoritative, so
        // the stored preset can never diverge from what the user saw. An
        // untouched field still holds the staged auto name.
        const fieldName = String(fieldValue ?? '').trim();
        if (fieldName) return fieldName;
        const draftName = String(peq?.draft?.presetName || '').trim();
        if (draftName) return draftName;
        return getMeasurementPeqPresetName(mode || 'both', { unique: true });
    }

    async function createMeasurementPeqPresetFromDraft() {
        if (!deps.requireConcreteFilterBank()) return;
        if (deps.isPeqCreateInFlight()) {
            deps.showToast('PEQ preset creation already in progress', 'warning');
            return;
        }
        const peq = ensureMeasurementPeqState();
        const leftBands = (peq.draft?.leftBands || []).map((band) => ({ ...band }));
        const rightBands = (peq.draft?.rightBands || []).map((band) => ({ ...band }));
        if (!leftBands.length && !rightBands.length) {
            deps.showToast('Take L, R or Both into the PEQ draft first', 'warning');
            return;
        }
        const validationError = deps.validatePeqBands('Left', leftBands) || deps.validatePeqBands('Right', rightBands);
        if (validationError) {
            showMeasurementPeqTakeFeedback(validationError);
            deps.showToast(validationError, 'error');
            return;
        }
        const presetName = resolveMeasurementPeqPresetName(peq, deps.getElements().measurementPeqPresetName?.value, getMeasurementPeqDraftMode(peq) || 'both');
        peq.draft.presetName = presetName;
        const eqMode = deps.normalizePeqEqMode(deps.getState().dsp?.peqDraft?.eqMode || deps.getElements().effectsPeqModeSelect?.value || 'IIR');
        deps.setPeqCreateInFlight(true);
        if (deps.getElements().measurementPeqCreateBtn) deps.getElements().measurementPeqCreateBtn.disabled = true;
        showMeasurementPeqTakeFeedback(`Creating ${presetName}…`);
        try {
            const resp = await deps.fetch('/api/dsp/presets/create-peq', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    presetName,
                    loadAfterCreate: false,
                    ...deps.collectEffectsExtras(),
                    ...deps.bankBindingJson(),
                    source_measurement_id: deps.measurementCommitSourceId(),
                    peq: {
                        enabled: true,
                        params: deps.measurementPeqParams(leftBands, rightBands, eqMode),
                    },
                }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'PEQ preset creation failed'));
            await deps.fetchEffects();
            void deps.fetchOutputSystemCatalog(true);
            peq.draft.leftBands = [];
            peq.draft.rightBands = [];
            peq.draft.presetName = '';
            peq.draft.nameTouched = false;
            showMeasurementPeqTakeFeedback(`${presetName} created`);
            deps.showToast(`Created PEQ preset: ${data.preset?.name || presetName}`, 'success');
        } catch (e) {
            showMeasurementPeqTakeFeedback('PEQ preset creation failed');
            deps.showToast(e.message || 'PEQ preset creation failed', 'error');
        } finally {
            deps.setPeqCreateInFlight(false);
            deps.renderMeasurementPanel();
        }
    }

    function getMeasurementPeqHandlePosition(filter, bounds, range) {
        return {
            x: dsp.measurementFrequencyToX(filter.freqHz || 1000, bounds),
            y: dsp.measurementDbToY(filter.gainDb || 0, bounds, range),
        };
    }

    function getMeasurementPeqHandleHitRadius(pointerType = '') {
        return pointerType === 'touch'
            ? deps.getPeqTouchHandleHitRadiusPx()
            : deps.getPeqHandleHitRadiusPx();
    }

    function findMeasurementPeqFilterHandleAtPosition(x, y, bounds, range, pointerType = '') {
        const hitRadius = getMeasurementPeqHandleHitRadius(pointerType);
        const filters = getMeasurementPeqFilters();
        for (let index = filters.length - 1; index >= 0; index -= 1) {
            const filter = filters[index];
            const handle = getMeasurementPeqHandlePosition(filter, bounds, range);
            const distance = Math.hypot(handle.x - x, handle.y - y);
            if (distance <= hitRadius) return filter;
        }
        return null;
    }

    function measurementPeqWorkingLineHit(y, bounds, range) {
        const zeroY = dsp.measurementDbToY(0, bounds, range);
        return Math.abs(y - zeroY) <= 40;
    }

    function measurementPeqTouchCreateCoolingDown(pointerType = '') {
        return pointerType === 'touch' && (Date.now() - measurementPeqLastTouchCreateAt) < deps.getPeqTouchCreateCooldownMs();
    }

    function markMeasurementPeqTouchCreate(pointerType = '') {
        if (pointerType === 'touch') measurementPeqLastTouchCreateAt = Date.now();
    }

    function handleMeasurementPeqPointerDown(event, pointer, pointerType) {
        const { x, y, bounds, range } = pointer;
        const peq = ensureMeasurementPeqState();
        const hitFilter = findMeasurementPeqFilterHandleAtPosition(x, y, bounds, range, pointerType);
        if (hitFilter) {
            if (pointerType === 'touch') event.preventDefault();
            peq.enabled = true;
            peq.activeFilterId = hitFilter.id;
            peq.dragFilterId = hitFilter.id;
            deps.setGraphPointerId(event.pointerId);
            deps.getElements().measurementGraph?.setPointerCapture?.(event.pointerId);
            deps.renderMeasurementPanel();
            focusMeasurementPeqPanelContext();
            return;
        }
        if (!measurementPeqWorkingLineHit(y, bounds, range)) return;
        if (measurementPeqTouchCreateCoolingDown(pointerType)) return;
        const created = createMeasurementPeqFilterFromPoint({ x, y, bounds, range });
        if (!created) return;
        if (pointerType === 'touch') event.preventDefault();
        markMeasurementPeqTouchCreate(pointerType);
        peq.dragFilterId = created.id;
        deps.setGraphPointerId(event.pointerId);
        deps.getElements().measurementGraph?.setPointerCapture?.(event.pointerId);
        deps.renderMeasurementPanel();
        focusMeasurementPeqPanelContext();
    }

    function handleMeasurementPeqPointerMove(event) {
        const peq = ensureMeasurementPeqState();
        if (!peq.dragFilterId || deps.getGraphPointerId() !== event.pointerId) return;
        if (event.pointerType === 'touch') event.preventDefault();
        const pointer = deps.getMeasurementGraphPointerPosition(event);
        if (!pointer) return;
        const { x, y, bounds, range } = pointer;
        updateMeasurementPeqFilter(peq.dragFilterId, {
            freqHz: dsp.measurementXToFrequency(x, bounds),
            gainDb: dsp.measurementYToDb(y, bounds, range),
        });
        deps.scheduleMeasurementGraphRender();
        deps.renderMeasurementPanel();
    }

    function clearMeasurementPeqPointerDrag() {
        ensureMeasurementPeqState().dragFilterId = null;
    }

    return {
        init,
        getDefaultMeasurementPeqFilter, getDefaultMeasurementPeqState,
        ensureMeasurementPeqState, getMeasurementPeqFilters, getMeasurementPeqActiveFilter,
        focusMeasurementPeqPanelContext, isEditableMeasurementPeqTarget,
        handleMeasurementPeqNumberInputArrowKey, syncMeasurementPeqQInput,
        stepActiveMeasurementPeqQ, handleMeasurementPeqGraphWheel,
        selectMeasurementPeqFilter, clampMeasurementPeqFrequency,
        clampMeasurementPeqGain, clampMeasurementPeqQ, addMeasurementPeqFilter,
        createMeasurementPeqFilterFromPoint, updateMeasurementPeqFilter,
        stepMeasurementPeqQ, stepMeasurementPeqGain, stepMeasurementPeqFrequency,
        deleteMeasurementPeqFilter, measurementPeqFilterToBand,
        showMeasurementPeqTakeFeedback, getMeasurementPeqNameSuffix,
        getMeasurementPeqDraftMode, getMeasurementPeqPresetName,
        takeMeasurementPeqToPreset, resolveMeasurementPeqPresetName,
        createMeasurementPeqPresetFromDraft, getMeasurementPeqHandlePosition,
        getMeasurementPeqHandleHitRadius, findMeasurementPeqFilterHandleAtPosition,
        measurementPeqWorkingLineHit, measurementPeqTouchCreateCoolingDown,
        markMeasurementPeqTouchCreate,
        handleMeasurementPeqPointerDown, handleMeasurementPeqPointerMove, clearMeasurementPeqPointerDrag,
    };
});
