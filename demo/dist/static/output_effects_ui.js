// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute output effects panel: effects fetch/render, output PEQ band model
 * and editor, preset creation/switching, effects extras incl. persistence and
 * the effects action wiring. Compare/combine/bank/import UI lives in
 * static/output_bank_ui.js (window.FXRouteBankUI) and is called directly.
 *
 * State/DOM/toast go through injected getters; crossover repaint, catalog
 * refresh and the shared upload-area wiring stay in app.js behind explicit
 * callbacks. Browser-loadable UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteEffectsUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({}),
        getElements: () => ({}),
        showToast: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
        fetchOutputSystemCatalog: async () => {},
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        measurementBankSumsBothInputs: () => false,
        renderCrossoverTile: () => {},
        repaintCrossoverGraph: () => {},
        setupUploadArea: () => {},
        getActiveEditing: () => new Set(),
        isPeqCreateInFlight: () => false,
        setPeqCreateInFlight: () => {},
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    const EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS = 800;
    // Mirrors dsp.native_config.PEQ_Q_MIN/PEQ_Q_MAX (LSP para_equalizer Q port bound).
    const PEQ_Q_MIN = 0.1;
    const PEQ_Q_MAX = 100;
const EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS = 2000;

function updateEffectsPeqDisclosureLabel() {
    if (!deps.getElements().effectsPeqDisclosureMeta || !deps.getElements().effectsPeqDisclosure) return;
    const leftCount = deps.getState().dsp?.peqDraft?.leftBands?.length || 0;
    const rightCount = deps.getState().dsp?.peqDraft?.rightBands?.length || 0;
    const actionLabel = deps.getElements().effectsPeqDisclosure.open ? 'Collapse' : 'Expand';
    deps.getElements().effectsPeqDisclosureMeta.textContent = `L${leftCount} · R${rightCount} · ${actionLabel}`;
    updateEffectsPeqSummary();
}

function formatPeqSummaryBand(band) {
    if (!band) return null;
    const fixed = (value) => {
        const n = Number(value);
        return Number.isFinite(n) ? Math.round(n * 10) / 10 : 0;
    };
    if (band.filterType === 'delay') return `${fixed(band.delayMs)} ms`;
    if (band.filterType === 'gain') {
        const gain = fixed(band.gainDb);
        return `${gain > 0 ? '+' : ''}${gain} dB`;
    }
    const hz = Number(band.frequencyHz);
    const freq = Number.isFinite(hz) && hz >= 1000 ? `${Math.round(hz / 100) / 10} kHz` : `${Math.round(hz)} Hz`;
    const gain = fixed(band.gainDb);
    return `${freq} ${gain > 0 ? '+' : ''}${gain} dB Q${fixed(band.q)}`;
}

function updateEffectsPeqSummary() {
    if (!deps.getElements().effectsPeqSummary) return;
    const formatSide = (bands) => {
        const items = (bands || []).map(formatPeqSummaryBand).filter(Boolean);
        if (!items.length) return null;
        const shown = items.slice(0, 3).join(' · ');
        return items.length > 3 ? `${shown} · +${items.length - 3}` : shown;
    };
    const left = formatSide(deps.getState().dsp?.peqDraft?.leftBands);
    const right = formatSide(deps.getState().dsp?.peqDraft?.rightBands);
    const lines = [];
    if (left) lines.push(`L  ${left}`);
    if (right) lines.push(`R  ${right}`);
    if (!lines.length) {
        deps.getElements().effectsPeqSummary.textContent = '';
        deps.getElements().effectsPeqSummary.hidden = true;
        return;
    }
    deps.getElements().effectsPeqSummary.textContent = lines.join('\n');
    deps.getElements().effectsPeqSummary.hidden = false;
}

function clearEffectsPeqStatusOnCollapse() {
    if (!deps.getElements().effectsPeqDisclosure || deps.getElements().effectsPeqDisclosure.open) return;
    if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
}

function setupEffectsActions() {
    if (deps.getElements().splCalibrationOpen) deps.getElements().splCalibrationOpen.addEventListener('click', () => window.FXRouteMeasurementSplCalibration.openSplCalibration());
    if (deps.getElements().splCalibrationClose) deps.getElements().splCalibrationClose.addEventListener('click', () => window.FXRouteMeasurementSplCalibration.closeSplCalibration());
    if (deps.getElements().splCalibrationPanel?.querySelector('.manage-overlay-backdrop')) {
        deps.getElements().splCalibrationPanel.querySelector('.manage-overlay-backdrop').addEventListener('click', () => window.FXRouteMeasurementSplCalibration.closeSplCalibration());
    }
    if (deps.getElements().splCalibrationNoise) deps.getElements().splCalibrationNoise.addEventListener('click', () => window.FXRouteMeasurementSplCalibration.toggleSplCalibrationNoise());
    if (deps.getElements().splCalibrationSave) deps.getElements().splCalibrationSave.addEventListener('click', () => window.FXRouteMeasurementSplCalibration.saveSplCalibration());
    root.FXRouteBankUI.wireBankUi();
    if (deps.getElements().effectsPeqPresetName) deps.getElements().effectsPeqPresetName.addEventListener('input', (event) => {
        if (!deps.getState().dsp?.peqDraft) return;
        deps.getState().dsp.peqDraft.presetName = event.target.value;
    });
    if (deps.getElements().effectsPeqModeSelect) deps.getElements().effectsPeqModeSelect.addEventListener('change', (event) => {
        if (!deps.getState().dsp?.peqDraft) return;
        deps.getState().dsp.peqDraft.eqMode = normalizePeqEqMode(event.target.value);
        event.target.value = deps.getState().dsp.peqDraft.eqMode;
    });
    if (deps.getElements().effectsPeqAddBandBtn) deps.getElements().effectsPeqAddBandBtn.addEventListener('click', addPeqBandPair);
    if (deps.getElements().effectsPeqCreatePresetBtn) deps.getElements().effectsPeqCreatePresetBtn.addEventListener('click', createPeqPreset);
    /* Compact − / value / + steppers: nudge the paired number input by its
       own step/min/max and let the existing input/change listeners apply
       the value (draft update + debounced save, unchanged semantics). */
    function nudgeStepperControl(button) {
        const control = button.closest('.stepper-control');
        const input = control ? control.querySelector('input[type="number"]') : null;
        if (!input || input.disabled) return;
        const direction = button.dataset.stepper === 'dec' ? -1 : 1;
        const step = Number(input.step);
        const stepValue = Number.isFinite(step) && step > 0 ? step : 1;
        const decimals = Math.min(Math.max((String(input.step).split('.')[1] || '').length,
            (String(input.value).split('.')[1] || '').length), 4);
        const min = input.min === '' ? -Infinity : Number(input.min);
        const max = input.max === '' ? Infinity : Number(input.max);
        // Crossover trim displays a rounded number while keeping its exact
        // value for the next nudge. Typed edits invalidate that display match.
        const precise = input.value === input._fxroutePreciseDisplay
            ? Number(input._fxroutePreciseValue) : NaN;
        const hasPreciseValue = Number.isFinite(precise);
        const current = hasPreciseValue ? precise : Number(input.value);
        if (!Number.isFinite(current)) return;
        let next = current + direction * stepValue;
        if (!hasPreciseValue) next = Number(next.toFixed(decimals));
        if (Number.isFinite(min)) next = Math.max(min, next);
        if (Number.isFinite(max)) next = Math.min(max, next);
        if (next === current) return;
        input.focus({ preventScroll: true });
        input.value = String(next);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
    }

    document.addEventListener('mousedown', (event) => {
        /* Keep focus on the number input when its stepper buttons are
           pressed: losing focus would drop the input from deps.getActiveEditing()
           and let the pending-save echo overwrite the user's value. */
        const button = event.target instanceof Element ? event.target.closest('.stepper-btn') : null;
        if (button) event.preventDefault();
    });

    document.addEventListener('click', (event) => {
        const button = event.target instanceof Element ? event.target.closest('.stepper-btn') : null;
        if (button) nudgeStepperControl(button);
    });

    root.FXRouteSubwooferUI.wireSubwooferControls();
    if (deps.getElements().effectsSubwooferPreview && deps.getElements().effectsCrossoverGraph) {
        if (deps.getElements().effectsCrossoverGraph) {
            if ('ResizeObserver' in window) {
                const crossoverResizeObserver = new ResizeObserver(() => deps.repaintCrossoverGraph());
                crossoverResizeObserver.observe(deps.getElements().effectsCrossoverGraph);
                deps.getElements().effectsCrossoverGraph._fxrouteResizeObserver = crossoverResizeObserver;
            } else {
                window.addEventListener('resize', deps.repaintCrossoverGraph);
            }
        }
    }
    // Track focus to avoid resetting input values while user is typing.
    // SELECTs are discrete choices (no typing to protect): they save with
    // the short toggle debounce so live A/B switching applies promptly
    // instead of restarting the 2000 ms typing debounce on every flip.
    // Numeric inputs keep the long typing debounce.
    [
        deps.getElements().effectsHeadroomGainDb,
        deps.getElements().effectsAutogainTargetDb,
        deps.getElements().effectsLoudnessStrength,
        deps.getElements().effectsLoudnessFftSize,
        deps.getElements().effectsBassAmount,
        deps.getElements().effectsToneEffectMode,
    ].forEach(el => {
        if (!el) return;
        const valueDebounceMs = el.tagName === 'SELECT'
            ? EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS
            : EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS;
        el.addEventListener('focus', () => deps.getActiveEditing().add(el));
        el.addEventListener('input', () => saveEffectsExtrasDebounced(valueDebounceMs));
        el.addEventListener('change', () => saveEffectsExtrasDebounced(valueDebounceMs));
        el.addEventListener('blur', () => {
            deps.getActiveEditing().delete(el);
            saveEffectsExtrasDebounced(0); // commit immediately on blur
        });
    });

    if (deps.getElements().effectsLimiterEnabled) deps.getElements().effectsLimiterEnabled.addEventListener('change', () => saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS));
    if (deps.getElements().effectsHeadroomEnabled) deps.getElements().effectsHeadroomEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (deps.getElements().effectsAutogainEnabled) deps.getElements().effectsAutogainEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (deps.getElements().effectsLoudnessEnabled) deps.getElements().effectsLoudnessEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (deps.getElements().effectsBassEnabled) deps.getElements().effectsBassEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (deps.getElements().effectsToneEffectEnabled) deps.getElements().effectsToneEffectEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    loadSavedEffectsExtras();
    // Compare controls bind through wireBankUi() above; binding them here a
    // second time would duplicate every Compare/A/B listener (two identical
    // POSTs per change, the second racing the first commit's revision).
    if (deps.getElements().effectsPeqDisclosure) {
        deps.getElements().effectsPeqDisclosure.addEventListener('toggle', () => {
            updateEffectsPeqDisclosureLabel();
            clearEffectsPeqStatusOnCollapse();
        });
        updateEffectsPeqDisclosureLabel();
    }
    deps.setupUploadArea('effects-import-area', 'effects-import-file', () => {
        root.FXRouteBankUI.submitEffectsImport();
    });
    deps.setupUploadArea('effects-rew-left-area', 'effects-rew-left-file', (file) => {
        void root.FXRouteBankUI.populateDualFilterTextareaFromFile('left', file);
    });
    deps.setupUploadArea('effects-rew-right-area', 'effects-rew-right-file', (file) => {
        void root.FXRouteBankUI.populateDualFilterTextareaFromFile('right', file);
    });
    root.FXRouteBankUI.updateEffectsImportUi();
    root.FXRouteBankUI.setEffectsImportPanelOpen(false);
    resetPeqDraft();
    root.FXRouteBankUI.renderEffectsCombine();
}

async function fetchEffects() {
    try {
        const resp = await fetch('/api/dsp/presets');
        if (!resp.ok) throw new Error('Failed to fetch DSP presets');
        const data = await resp.json();
        const prev = deps.getState().dsp?.compare;
        const presetNames = (data.presets || []).map(p => p.name);
        deps.getState().dsp = {
            ...data,
            combineDraft: deps.getState().dsp?.combineDraft || root.FXRouteBankUI.getDefaultEffectsCombineDraft(),
            peqDraft: deps.getState().dsp?.peqDraft || {
                presetName: '',
                eqMode: 'IIR',
                loadAfterCreate: false,
                leftBands: [defaultPeqBand()],
                rightBands: [defaultPeqBand()],
            },
            assistStack: Array.isArray(deps.getState().dsp?.assistStack) ? deps.getState().dsp.assistStack : [],
            compare: root.FXRouteBankUI.resolveEffectsCompareState(data.compare || prev, presetNames, data.active_preset || ''),
        };
        deps.getState().dsp.combineDraft = root.FXRouteBankUI.normalizeEffectsCombineDraft(deps.getState().dsp.combineDraft, presetNames);
        if (!Array.isArray(deps.getState().dsp.peqDraft?.leftBands) || !deps.getState().dsp.peqDraft.leftBands.length) deps.getState().dsp.peqDraft.leftBands = [defaultPeqBand()];
        if (!Array.isArray(deps.getState().dsp.peqDraft?.rightBands) || !deps.getState().dsp.peqDraft.rightBands.length) deps.getState().dsp.peqDraft.rightBands = [defaultPeqBand()];
        deps.getState().dsp.peqDraft.eqMode = normalizePeqEqMode(deps.getState().dsp.peqDraft?.eqMode);
        if (data.global_extras) {
            applyEffectsExtras({
                limiterEnabled: !!data.global_extras?.limiter?.enabled,
                headroomEnabled: !!data.global_extras?.headroom?.enabled,
                headroomGainDb: Number(data.global_extras?.headroom?.params?.gainDb ?? -3),
                autogainEnabled: !!data.global_extras?.autogain?.enabled,
                autogainTargetDb: Number(data.global_extras?.autogain?.params?.targetDb ?? -12),
                loudnessEnabled: !!data.global_extras?.loudness?.enabled,
                loudnessStrength: data.global_extras?.loudness?.params?.strength ?? 10,
                loudnessFftSize: Number(data.global_extras?.loudness?.params?.fftSize ?? 4096),
                loudnessVolumeDb: Number(data.global_extras?.loudness?.params?.volumeDb ?? 0),
                delayEnabled: !!data.global_extras?.delay?.enabled,
                delayLeftMs: Number(data.global_extras?.delay?.params?.leftMs || 0),
                delayRightMs: Number(data.global_extras?.delay?.params?.rightMs || 0),
                bassEnabled: !!data.global_extras?.bass_enhancer?.enabled,
                bassAmount: Number(data.global_extras?.bass_enhancer?.params?.amount || 0),
                toneEffectEnabled: !!data.global_extras?.tone_effect?.enabled,
                toneEffectMode: String(data.global_extras?.tone_effect?.mode || 'crystalizer'),
            });
        }
        renderEffects();
        void deps.fetchOutputSystemCatalog();
    } catch (e) {
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">DSP presets are unavailable</div>';
    }
}

function defaultPeqBand() {
    return {
        filterType: 'bell',
        frequencyHz: 1000,
        gainDb: 0,
        q: 1,
        delayMs: 0,
    };
}

function isPeqGainBand(band = {}) {
    return String(band?.filterType || '').toLowerCase() === 'gain';
}

function isPeqDelayBand(band = {}) {
    return String(band?.filterType || '').toLowerCase() === 'delay';
}

function getPeqBandFallback(field, band = {}) {
    if (field === 'frequencyHz') return Number.isFinite(Number(band?.frequencyHz)) ? Number(band.frequencyHz) : 1000;
    if (field === 'q') return Number.isFinite(Number(band?.q)) ? Number(band.q) : 1;
    if (field === 'gainDb') return Number.isFinite(Number(band?.gainDb)) ? Number(band.gainDb) : 0;
    if (field === 'delayMs') return Number.isFinite(Number(band?.delayMs)) ? Number(band.delayMs) : 0;
    return 0;
}

function normalizePeqEqMode(value, fallback = 'IIR') {
    const normalized = String(value || fallback).trim().toUpperCase();
    return ['IIR', 'FIR', 'FFT', 'SPM'].includes(normalized) ? normalized : fallback;
}

function getDefaultPeqDraft() {
    return {
        presetName: '',
        eqMode: 'IIR',
        loadAfterCreate: false,
        leftBands: [defaultPeqBand()],
        rightBands: [defaultPeqBand()],
    };
}

function resetPeqDraft() {
    deps.getState().dsp.peqDraft = getDefaultPeqDraft();
    if (deps.getElements().effectsPeqPresetName) deps.getElements().effectsPeqPresetName.value = '';
    if (deps.getElements().effectsPeqModeSelect) deps.getElements().effectsPeqModeSelect.value = 'IIR';
    renderPeqBands();
}

function addPeqBandPair() {
    if (!deps.getState().dsp?.peqDraft) {
        deps.getState().dsp.peqDraft = getDefaultPeqDraft();
    }
    const leftBands = deps.getState().dsp.peqDraft.leftBands || (deps.getState().dsp.peqDraft.leftBands = []);
    const rightBands = deps.getState().dsp.peqDraft.rightBands || (deps.getState().dsp.peqDraft.rightBands = []);
    if (leftBands.length >= 20 || rightBands.length >= 20) {
        deps.showToast('Maximum 20 Left and Right PEQ bands supported', 'error');
        return;
    }
    leftBands.push(defaultPeqBand());
    rightBands.push(defaultPeqBand());
    renderPeqBands();
}

function removePeqBand(side, index) {
    if (!deps.getState().dsp?.peqDraft) return;
    const key = side === 'right' ? 'rightBands' : 'leftBands';
    const bands = deps.getState().dsp.peqDraft[key] || [];
    if (index < 0 || index >= bands.length) return;
    bands.splice(index, 1);
    renderPeqBands();
}

function ensurePeqBandExists(side, index) {
    if (!deps.getState().dsp?.peqDraft) return null;
    const key = side === 'right' ? 'rightBands' : 'leftBands';
    deps.getState().dsp.peqDraft[key] = deps.getState().dsp.peqDraft[key] || [];
    while (deps.getState().dsp.peqDraft[key].length <= index) {
        deps.getState().dsp.peqDraft[key].push(defaultPeqBand());
    }
    return deps.getState().dsp.peqDraft[key][index] || null;
}

function getOtherPeqSide(side) {
    return side === 'right' ? 'left' : 'right';
}

function getPeqLinkedSpecialType(leftBand = {}, rightBand = {}) {
    if (isPeqGainBand(leftBand) && isPeqGainBand(rightBand)) return 'gain';
    if (isPeqDelayBand(leftBand) && isPeqDelayBand(rightBand)) return 'delay';
    return '';
}

function getPeqBandPair(side, index) {
    if (!deps.getState().dsp?.peqDraft) return { sourceBand: null, otherBand: null };
    const sourceBand = ensurePeqBandExists(side, index);
    const otherBand = ensurePeqBandExists(getOtherPeqSide(side), index);
    return { sourceBand, otherBand };
}

function syncLinkedPeqSpecialBand(side, index) {
    const { sourceBand, otherBand } = getPeqBandPair(side, index);
    if (!sourceBand || !otherBand) return;
    const linkedType = getPeqLinkedSpecialType(sourceBand, otherBand);
    if (linkedType === 'gain') otherBand.gainDb = sourceBand.gainDb;
    if (linkedType === 'delay') otherBand.delayMs = sourceBand.delayMs;
}

function normalizeLinkedPeqSpecialBands() {
    if (!deps.getState().dsp?.peqDraft) return;
    const leftBands = deps.getState().dsp.peqDraft.leftBands || [];
    const rightBands = deps.getState().dsp.peqDraft.rightBands || [];
    const count = Math.max(leftBands.length, rightBands.length);
    for (let index = 0; index < count; index += 1) {
        const leftBand = leftBands[index] || null;
        const rightBand = rightBands[index] || null;
        if (getPeqLinkedSpecialType(leftBand, rightBand)) {
            syncLinkedPeqSpecialBand('left', index);
        }
    }
}

function updatePeqBand(side, index, field, value) {
    if (!deps.getState().dsp?.peqDraft) return;
    const band = ensurePeqBandExists(side, index);
    if (!band) return;
    band[field] = value;

    if (field === 'gainDb' || field === 'delayMs') {
        syncLinkedPeqSpecialBand(side, index);
    }
}

function syncLinkedPeqSpecialBandValueInDom(side, index, field) {
    const { sourceBand, otherBand } = getPeqBandPair(side, index);
    const otherSide = getOtherPeqSide(side);
    const otherInput = document.querySelector(`[data-peq-side="${otherSide}"][data-peq-index="${index}"][data-peq-field="${field}"]`);
    if (!sourceBand || !otherBand || !otherInput || !getPeqLinkedSpecialType(sourceBand, otherBand)) return;
    if (field === 'gainDb') otherInput.value = String(sourceBand.gainDb);
    if (field === 'delayMs') otherInput.value = String(sourceBand.delayMs);
}

function renderPeqBandColumn(container, side, bands) {
    if (!container) return;
    const filterTypeLabels = {
        bell: 'Bell',
        notch: 'Notch',
        gain: 'Gain',
        delay: 'Delay',
        low_shelf: 'Low shelf',
        high_shelf: 'High shelf',
        low_pass: 'Low pass',
        high_pass: 'High pass',
    };
    if (!bands.length) {
        container.innerHTML = '<div class="effects-peq-empty">No bands yet.</div>';
        return;
    }
    container.innerHTML = bands.map((band, index) => {
        const isGain = isPeqGainBand(band);
        const isDelay = isPeqDelayBand(band);
        const otherBands = side === 'right' ? (deps.getState().dsp?.peqDraft?.leftBands || []) : (deps.getState().dsp?.peqDraft?.rightBands || []);
        const isLinkedSpecialPair = !!getPeqLinkedSpecialType(band, otherBands[index] || null);
        const showRemove = true;
        const fieldIdPrefix = `effects-peq-${side}-${index}`;
        return `
        <div class="effects-peq-band" data-peq-side="${side}" data-peq-band="${index}">
            <div class="effects-peq-band-header">
                <div>
                    <div class="effects-peq-band-title">Band ${index + 1}</div>
                    ${isLinkedSpecialPair ? '<div class="effects-peq-band-subtitle">L/R linked</div>' : ''}
                </div>
                ${showRemove ? `<button type="button" class="btn-danger btn-inline" data-peq-remove-side="${side}" data-peq-remove-index="${index}">Remove</button>` : '<span class="effects-peq-remove-spacer"></span>'}
            </div>
            <div class="effects-peq-band-fields${isGain ? ' effects-peq-band-fields-gain' : ''}">
                <div class="field-group">
                    <label for="${fieldIdPrefix}-type">Type</label>
                    <select id="${fieldIdPrefix}-type" name="${fieldIdPrefix}-type" class="url-input" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="filterType">
                        ${['bell', 'notch', 'gain', 'delay', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass'].map(type => `<option value="${type}" ${band.filterType === type ? 'selected' : ''}>${filterTypeLabels[type]}</option>`).join('')}
                    </select>
                </div>
                ${isDelay ? `
                <div class="field-group">
                    <label for="${fieldIdPrefix}-delay">Delay (ms)</label>
                    <input id="${fieldIdPrefix}-delay" name="${fieldIdPrefix}-delay" type="number" class="url-input" min="0" max="500" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="delayMs" value="${Number.isFinite(Number(band.delayMs)) ? band.delayMs : 0}">
                </div>` : `
                <div class="field-group">
                    <label for="${fieldIdPrefix}-gain">Gain (dB)</label>
                    <input id="${fieldIdPrefix}-gain" name="${fieldIdPrefix}-gain" type="number" class="url-input" min="-24" max="24" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="gainDb" value="${band.gainDb}">
                </div>
                ${isGain ? '' : `
                <div class="field-group">
                    <label for="${fieldIdPrefix}-frequency">Freq (Hz)</label>
                    <input id="${fieldIdPrefix}-frequency" name="${fieldIdPrefix}-frequency" type="number" class="url-input" min="20" max="20000" step="1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="frequencyHz" value="${band.frequencyHz}">
                </div>
                <div class="field-group">
                    <label for="${fieldIdPrefix}-q">Q</label>
                    <input id="${fieldIdPrefix}-q" name="${fieldIdPrefix}-q" type="number" class="url-input" min="${PEQ_Q_MIN}" max="${PEQ_Q_MAX}" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="q" value="${band.q}">
                </div>`}`}
            </div>
        </div>
    `;
    }).join('');
    container.querySelectorAll('[data-peq-remove-side][data-peq-remove-index]').forEach(button => {
        button.addEventListener('click', () => {
            removePeqBand(button.dataset.peqRemoveSide, Number(button.dataset.peqRemoveIndex));
        });
    });
    container.querySelectorAll('[data-peq-field]').forEach(input => {
        const handleFieldUpdate = (live = false) => {
            const sideName = input.dataset.peqSide;
            const index = Number(input.dataset.peqIndex);
            const field = input.dataset.peqField;
            const value = field === 'filterType' ? input.value : Number(input.value);
            updatePeqBand(sideName, index, field, value);
            const currentBand = ensurePeqBandExists(sideName, index);
            if (field === 'filterType') {
                renderPeqBands();
                return;
            }
            if (field === 'gainDb' && isPeqGainBand(currentBand)) {
                if (live) {
                    syncLinkedPeqSpecialBandValueInDom(sideName, index, 'gainDb');
                } else {
                    renderPeqBands();
                }
            }
            if (field === 'delayMs' && isPeqDelayBand(currentBand)) {
                if (live) {
                    syncLinkedPeqSpecialBandValueInDom(sideName, index, 'delayMs');
                } else {
                    renderPeqBands();
                }
            }
        };
        input.addEventListener('change', () => handleFieldUpdate(false));
        if (input.dataset.peqField === 'gainDb' || input.dataset.peqField === 'delayMs') {
            input.addEventListener('input', () => handleFieldUpdate(true));
        }
    });
}

function renderPeqBands() {
    if (!deps.getState().dsp?.peqDraft) {
        deps.getState().dsp = deps.getState().dsp || {};
        deps.getState().dsp.peqDraft = getDefaultPeqDraft();
    }
    normalizeLinkedPeqSpecialBands();
    const draft = deps.getState().dsp.peqDraft;
    draft.eqMode = normalizePeqEqMode(draft.eqMode);
    if (deps.getElements().effectsPeqModeSelect) deps.getElements().effectsPeqModeSelect.value = draft.eqMode;
    renderPeqBandColumn(deps.getElements().effectsPeqLeftBands, 'left', draft.leftBands || []);
    renderPeqBandColumn(deps.getElements().effectsPeqRightBands, 'right', draft.rightBands || []);
    updateEffectsPeqDisclosureLabel();
}

function readPeqNumberInput(input, fallback) {
    if (!input) return fallback;
    const raw = String(input.value ?? '').trim();
    if (!raw) return fallback;
    const value = Number(raw);
    return Number.isFinite(value) ? value : fallback;
}

function collectPeqBandsFromDom(side) {
    const container = side === 'right' ? deps.getElements().effectsPeqRightBands : deps.getElements().effectsPeqLeftBands;
    if (!container) return [];
    const draftBands = side === 'right' ? (deps.getState().dsp?.peqDraft?.rightBands || []) : (deps.getState().dsp?.peqDraft?.leftBands || []);
    return Array.from(container.querySelectorAll('[data-peq-band]')).map((bandEl, index) => {
        const draftBand = draftBands[index] || defaultPeqBand();
        const filterType = bandEl.querySelector(`[data-peq-side="${side}"][data-peq-index="${index}"][data-peq-field="filterType"]`)?.value || 'bell';
        return {
            filterType,
            frequencyHz: readPeqNumberInput(bandEl.querySelector(`[data-peq-side="${side}"][data-peq-index="${index}"][data-peq-field="frequencyHz"]`), getPeqBandFallback('frequencyHz', draftBand)),
            gainDb: readPeqNumberInput(bandEl.querySelector(`[data-peq-side="${side}"][data-peq-index="${index}"][data-peq-field="gainDb"]`), getPeqBandFallback('gainDb', draftBand)),
            q: readPeqNumberInput(bandEl.querySelector(`[data-peq-side="${side}"][data-peq-index="${index}"][data-peq-field="q"]`), getPeqBandFallback('q', draftBand)),
            delayMs: readPeqNumberInput(bandEl.querySelector(`[data-peq-side="${side}"][data-peq-index="${index}"][data-peq-field="delayMs"]`), getPeqBandFallback('delayMs', draftBand)),
        };
    });
}

function validatePeqBands(side, bands) {
    for (let index = 0; index < bands.length; index += 1) {
        const band = bands[index] || {};
        const isGain = isPeqGainBand(band);
        const isDelay = isPeqDelayBand(band);
        if (isDelay) {
            if (!Number.isFinite(band.delayMs) || band.delayMs < 0 || band.delayMs > 500) {
                return `${side} band ${index + 1}: delay must be between 0 and 500 ms`;
            }
            continue;
        }
        if (!isGain && (!Number.isFinite(band.frequencyHz) || band.frequencyHz < 20 || band.frequencyHz > 20000)) {
            return `${side} band ${index + 1}: frequency must be between 20 and 20000 Hz`;
        }
        if (!Number.isFinite(band.gainDb) || band.gainDb < -24 || band.gainDb > 24) {
            return `${side} band ${index + 1}: gain must be between -24 and 24 dB`;
        }
        if (!isGain && (!Number.isFinite(band.q) || band.q < PEQ_Q_MIN || band.q > PEQ_Q_MAX)) {
            return `${side} band ${index + 1}: Q must be between ${PEQ_Q_MIN} and ${PEQ_Q_MAX}`;
        }
    }
    return null;
}

function getPeqGainTotal(bands = []) {
    return bands.reduce((sum, band) => {
        if (!isPeqGainBand(band) || band?.enabled === false) return sum;
        const value = Number(band?.gainDb);
        return Number.isFinite(value) ? sum + value : sum;
    }, 0);
}

async function createPeqPreset() {
    if (!root.FXRouteBankUI.requireConcreteFilterBank()) return;
    if (deps.isPeqCreateInFlight()) {
        deps.showToast('PEQ preset creation already in progress', 'warning');
        return;
    }
    deps.setPeqCreateInFlight(true);
    if (!deps.getState().dsp?.peqDraft) {
        deps.getState().dsp = deps.getState().dsp || {};
        deps.getState().dsp.peqDraft = getDefaultPeqDraft();
    }
    const presetName = deps.getElements().effectsPeqPresetName?.value?.trim() || '';
    if (!presetName) {
        deps.setPeqCreateInFlight(false);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please enter a PEQ preset name.</div>';
        if (deps.getElements().effectsPeqPresetName) deps.getElements().effectsPeqPresetName.focus();
        deps.showToast('Please enter a PEQ preset name', 'error');
        return;
    }
    const leftBands = collectPeqBandsFromDom('left');
    const rightBands = deps.measurementBankSumsBothInputs() ? leftBands : collectPeqBandsFromDom('right');
    if (!leftBands.length && !rightBands.length) {
        deps.setPeqCreateInFlight(false);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '<div style="color: var(--danger);">Please add at least one Left or Right band.</div>';
        deps.showToast('Please add at least one Left or Right band', 'error');
        return;
    }
    const validationError = validatePeqBands('Left', leftBands) || validatePeqBands('Right', rightBands);
    if (validationError) {
        deps.setPeqCreateInFlight(false);
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(validationError)}</div>`;
        deps.showToast(validationError, 'error');
        return;
    }
    const leftGainTotal = getPeqGainTotal(leftBands);
    const rightGainTotal = getPeqGainTotal(rightBands);
    const dualGainMismatch = Math.abs(leftGainTotal) > 1e-9 && Math.abs(rightGainTotal) > 1e-9 && Math.abs(leftGainTotal - rightGainTotal) > 1e-9;
    if (dualGainMismatch) {
        deps.setPeqCreateInFlight(false);
        const gainError = 'Gain currently works as shared stereo trim; use the same Gain on both sides or only one shared Gain value.';
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">${deps.escapeHtml(gainError)}</div>`;
        deps.showToast(gainError, 'error');
        return;
    }
    const eqMode = normalizePeqEqMode(deps.getElements().effectsPeqModeSelect?.value || deps.getState().dsp.peqDraft?.eqMode);
    deps.getState().dsp.peqDraft.leftBands = leftBands;
    deps.getState().dsp.peqDraft.rightBands = rightBands;
    deps.getState().dsp.peqDraft.eqMode = eqMode;
    if (deps.getElements().effectsPeqCreatePresetBtn) deps.getElements().effectsPeqCreatePresetBtn.disabled = true;
    if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = `<div>Creating PEQ preset: <strong>${deps.escapeHtml(presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/dsp/presets/create-peq', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                presetName,
                loadAfterCreate: false,
                ...collectEffectsExtras(),
                ...root.FXRouteBankUI.bankBindingJson(),
                peq: {
                    enabled: true,
                    params: root.FXRouteBankUI.measurementPeqParams(leftBands, rightBands, eqMode),
                },
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'PEQ preset creation failed'));
        await fetchEffects();
        void deps.fetchOutputSystemCatalog(true);
        if (deps.getElements().effectsPeqDisclosure) deps.getElements().effectsPeqDisclosure.open = false;
        updateEffectsPeqDisclosureLabel();
        resetPeqDraft();
        if (deps.getElements().effectsStatus) deps.getElements().effectsStatus.innerHTML = '';
        deps.showToast(`Created PEQ preset: ${data.preset.name}`, 'success');
    } catch (e) {
        deps.getElements().effectsStatus.innerHTML = `<div style="color: var(--danger);">PEQ preset creation failed: ${deps.escapeHtml(e.message)}</div>`;
        deps.showToast(e.message || 'PEQ preset creation failed', 'error');
    } finally {
        if (deps.getElements().effectsPeqCreatePresetBtn) deps.getElements().effectsPeqCreatePresetBtn.disabled = false;
        deps.setPeqCreateInFlight(false);
    }
}

function renderEffects() {
    const fx = deps.getState().dsp;
    const presets = fx.presets || [];
    const presetNames = presets.map(p => p.name);
    deps.getElements().effectsInfo.textContent = fx.available
        ? `${fx.preset_count} presets`
        : 'DSP is not available';
    if (fx.combineDraft) {
        fx.combineDraft = root.FXRouteBankUI.normalizeEffectsCombineDraft(fx.combineDraft, presetNames);
    }
    if (!fx.available) {
        deps.getElements().effectsDeleteBtn.disabled = true;
        if (deps.getElements().effectsToggleImportBtn) deps.getElements().effectsToggleImportBtn.disabled = true;
        if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.disabled = true;
        if (deps.getElements().effectsCombineSaveBtn) deps.getElements().effectsCombineSaveBtn.disabled = true;
        if (deps.getElements().effectsPeqAddBandBtn) deps.getElements().effectsPeqAddBandBtn.disabled = true;
        if (deps.getElements().effectsPeqModeSelect) deps.getElements().effectsPeqModeSelect.disabled = true;
        if (deps.getElements().effectsPeqCreatePresetBtn) deps.getElements().effectsPeqCreatePresetBtn.disabled = true;
        deps.getElements().effectsStatus.innerHTML = '';
        return;
    }
    // renderEffectsCompare below owns the Delete button (selected bank's active preset).
    if (deps.getElements().effectsToggleImportBtn) deps.getElements().effectsToggleImportBtn.disabled = false;
    if (deps.getElements().effectsRewDualCreatePresetBtn) deps.getElements().effectsRewDualCreatePresetBtn.disabled = false;
    if (deps.getElements().effectsPeqAddBandBtn) deps.getElements().effectsPeqAddBandBtn.disabled = false;
    if (deps.getElements().effectsPeqModeSelect) deps.getElements().effectsPeqModeSelect.disabled = false;
    if (deps.getElements().effectsPeqCreatePresetBtn) deps.getElements().effectsPeqCreatePresetBtn.disabled = false;
    renderPeqBands();
    root.FXRouteBankUI.renderEffectsCompare();
    root.FXRouteBankUI.renderEffectsCombine();
    deps.renderCrossoverTile();
}

async function switchEffectsPreset() {
    // Preset switching now goes exclusively through root.FXRouteBankUI.toggleComparePreset
    // (or the A/B dropdowns) — this function is kept only as a harmless stub
    // for older callers / delete-from-active flow remnants.
    return null;
}

// Offered values when headroom is enabled. 0 dB is deliberately NOT offered:
// "no headroom" is the checkbox's job. 0 stays in the set anyway so a
// previously stored 0 round-trips through render/collect instead of being
// silently rewritten to the -3 dB fallback.
const EFFECTS_HEADROOM_ALLOWED_GAIN_DB = new Set([-9, -8, -7, -6, -5, -4, -3, -2, -1, 0]);
const EFFECTS_AUTOGAIN_ALLOWED_TARGET_DB = new Set([-12, -15, -18, -23]);
const EFFECTS_LOUDNESS_ALLOWED_FFT_SIZE = new Set([256, 512, 1024, 2048, 4096, 8192, 16384]);
const EFFECTS_LOUDNESS_LEGACY_STRENGTHS = new Map([
    ['full', 10],
    ['med', 7],
    ['light', 4],
    ['min', 1],
]);
const EFFECTS_TONE_EFFECT_MODES = new Set(['crystalizer', 'maximizer']);

function normalizeEffectsHeadroomGainDb(value, fallback = -3) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return fallback;
    const rounded = Math.round(numeric);
    return EFFECTS_HEADROOM_ALLOWED_GAIN_DB.has(rounded) ? rounded : fallback;
}

function normalizeEffectsAutogainTargetDb(value, fallback = -12) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return fallback;
    const rounded = Math.round(numeric);
    return EFFECTS_AUTOGAIN_ALLOWED_TARGET_DB.has(rounded) ? rounded : fallback;
}

function normalizeEffectsLoudnessFftSize(value, fallback = 4096) {
    const numeric = Math.round(Number(value));
    return EFFECTS_LOUDNESS_ALLOWED_FFT_SIZE.has(numeric) ? numeric : fallback;
}

function normalizeEffectsLoudnessStrength(value, fallback = 10) {
    const legacy = EFFECTS_LOUDNESS_LEGACY_STRENGTHS.get(String(value).trim().toLowerCase());
    if (legacy !== undefined) return legacy;
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return fallback;
    return Math.max(1, Math.min(10, Math.round(numeric)));
}

function normalizeEffectsToneEffectMode(value, fallback = 'crystalizer') {
    const normalized = String(value || fallback).trim().toLowerCase();
    return EFFECTS_TONE_EFFECT_MODES.has(normalized) ? normalized : fallback;
}

function applyEffectsExtras(extras = {}) {
    if (deps.getElements().effectsLimiterEnabled) deps.getElements().effectsLimiterEnabled.checked = !!extras.limiterEnabled;
    if (deps.getElements().effectsHeadroomEnabled) deps.getElements().effectsHeadroomEnabled.checked = !!extras.headroomEnabled;
    if (deps.getElements().effectsHeadroomGainDb && !deps.getActiveEditing().has(deps.getElements().effectsHeadroomGainDb)) {
        deps.getElements().effectsHeadroomGainDb.value = String(normalizeEffectsHeadroomGainDb(extras.headroomGainDb, -3));
    }
    if (deps.getElements().effectsAutogainEnabled) deps.getElements().effectsAutogainEnabled.checked = !!extras.autogainEnabled;
    if (deps.getElements().effectsAutogainTargetDb && !deps.getActiveEditing().has(deps.getElements().effectsAutogainTargetDb)) {
        deps.getElements().effectsAutogainTargetDb.value = String(normalizeEffectsAutogainTargetDb(extras.autogainTargetDb, -12));
    }
    if (deps.getElements().effectsLoudnessEnabled) deps.getElements().effectsLoudnessEnabled.checked = !!extras.loudnessEnabled;
    if (deps.getElements().effectsLoudnessStrength && !deps.getActiveEditing().has(deps.getElements().effectsLoudnessStrength)) {
        deps.getElements().effectsLoudnessStrength.value = normalizeEffectsLoudnessStrength(extras.loudnessStrength, 10);
    }
    if (deps.getElements().effectsLoudnessFftSize && !deps.getActiveEditing().has(deps.getElements().effectsLoudnessFftSize)) {
        deps.getElements().effectsLoudnessFftSize.value = String(normalizeEffectsLoudnessFftSize(extras.loudnessFftSize, 4096));
    }
    if (deps.getElements().effectsBassEnabled) deps.getElements().effectsBassEnabled.checked = !!extras.bassEnabled;
    // Only update amount when bass is enabled — otherwise keep field value (user may re-enable)
    if (deps.getElements().effectsBassAmount && !!extras.bassEnabled && !deps.getActiveEditing().has(deps.getElements().effectsBassAmount)) {
        deps.getElements().effectsBassAmount.value = String(Number(extras.bassAmount || 0));
    }
    if (deps.getElements().effectsToneEffectEnabled) deps.getElements().effectsToneEffectEnabled.checked = !!extras.toneEffectEnabled;
    if (deps.getElements().effectsToneEffectMode && !deps.getActiveEditing().has(deps.getElements().effectsToneEffectMode)) {
        deps.getElements().effectsToneEffectMode.value = normalizeEffectsToneEffectMode(extras.toneEffectMode, 'crystalizer');
    }
    updateEffectsExtrasUi();
}

function updateEffectsExtrasUi() {
    if (deps.getElements().effectsHeadroomGainWrap) {
        deps.getElements().effectsHeadroomGainWrap.classList.toggle('hidden', !deps.getElements().effectsHeadroomEnabled?.checked);
    }
    if (deps.getElements().effectsAutogainTargetWrap) {
        deps.getElements().effectsAutogainTargetWrap.classList.toggle('hidden', !deps.getElements().effectsAutogainEnabled?.checked);
    }
    if (deps.getElements().effectsLoudnessFftWrap) {
        deps.getElements().effectsLoudnessFftWrap.classList.toggle('hidden', !deps.getElements().effectsLoudnessEnabled?.checked);
    }
    if (deps.getElements().effectsLoudnessStrengthWrap) {
        deps.getElements().effectsLoudnessStrengthWrap.classList.toggle('hidden', !deps.getElements().effectsLoudnessEnabled?.checked);
    }
    if (deps.getElements().effectsBassControlsWrap) {
        deps.getElements().effectsBassControlsWrap.classList.toggle('hidden', !deps.getElements().effectsBassEnabled?.checked);
    }
    if (deps.getElements().effectsToneEffectWrap) {
        deps.getElements().effectsToneEffectWrap.classList.toggle('hidden', !deps.getElements().effectsToneEffectEnabled?.checked);
    }
}

function loadSavedEffectsExtras() {
    const fx = deps.getState().dsp;
    if (!fx?.global_extras) return;
    applyEffectsExtras({
        limiterEnabled: !!fx.global_extras?.limiter?.enabled,
        headroomEnabled: !!fx.global_extras?.headroom?.enabled,
        headroomGainDb: Number(fx.global_extras?.headroom?.params?.gainDb ?? -3),
        autogainEnabled: !!fx.global_extras?.autogain?.enabled,
        autogainTargetDb: Number(fx.global_extras?.autogain?.params?.targetDb ?? -12),
        loudnessEnabled: !!fx.global_extras?.loudness?.enabled,
        loudnessStrength: fx.global_extras?.loudness?.params?.strength ?? 10,
        loudnessFftSize: Number(fx.global_extras?.loudness?.params?.fftSize ?? 4096),
        loudnessVolumeDb: Number(fx.global_extras?.loudness?.params?.volumeDb ?? 0),
        delayEnabled: !!fx.global_extras?.delay?.enabled,
        delayLeftMs: Number(fx.global_extras?.delay?.params?.leftMs || 0),
        delayRightMs: Number(fx.global_extras?.delay?.params?.rightMs || 0),
        bassEnabled: !!fx.global_extras?.bass_enhancer?.enabled,
        bassAmount: Number(fx.global_extras?.bass_enhancer?.params?.amount || 0),
        toneEffectEnabled: !!fx.global_extras?.tone_effect?.enabled,
        toneEffectMode: String(fx.global_extras?.tone_effect?.mode || 'crystalizer'),
    });
}

function describeEffectsExtras(extras) {
    const parts = [];
    parts.push(extras.limiterEnabled ? 'Limiter ON (-1.0 dB)' : 'Limiter OFF');
    parts.push(extras.headroomEnabled ? `Headroom ON (${Number(extras.headroomGainDb || 0).toFixed(0)} dB)` : 'Headroom OFF');
    parts.push(extras.autogainEnabled ? `Autogain ON (${extras.autogainTargetDb} LUFS)` : 'Autogain OFF');
    parts.push(extras.loudnessEnabled ? `Loudness ON (FFT ${extras.loudnessFftSize})` : 'Loudness OFF');
    parts.push(extras.toneEffectEnabled ? `Tone ON (${normalizeEffectsToneEffectMode(extras.toneEffectMode, 'crystalizer')})` : 'Tone OFF');
    return parts.join(' • ');
}

let _extrasDebounceTimer = null;
let effectsExtrasSaveInFlight = false;
let effectsExtrasPendingResave = false;

function saveEffectsExtrasDebounced(delayMs = EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS) {
    window.clearTimeout(_extrasDebounceTimer);
    if (delayMs <= 0) {
        _doSaveEffectsExtras();
        return;
    }
    _extrasDebounceTimer = window.setTimeout(() => {
        _doSaveEffectsExtras();
    }, delayMs);
}

function collectEffectsExtras() {
    return {
        limiterEnabled: deps.getElements().effectsLimiterEnabled?.checked || false,
        headroomEnabled: deps.getElements().effectsHeadroomEnabled?.checked || false,
        headroomGainDb: normalizeEffectsHeadroomGainDb(deps.getElements().effectsHeadroomGainDb?.value, -3),
        autogainEnabled: deps.getElements().effectsAutogainEnabled?.checked || false,
        autogainTargetDb: normalizeEffectsAutogainTargetDb(deps.getElements().effectsAutogainTargetDb?.value, -12),
        loudnessEnabled: deps.getElements().effectsLoudnessEnabled?.checked || false,
        loudnessStrength: normalizeEffectsLoudnessStrength(deps.getElements().effectsLoudnessStrength?.value, 10),
        loudnessFftSize: normalizeEffectsLoudnessFftSize(deps.getElements().effectsLoudnessFftSize?.value, 4096),
        bassEnabled: deps.getElements().effectsBassEnabled?.checked || false,
        bassAmount: parseFloat(deps.getElements().effectsBassAmount?.value || '0'),
        toneEffectEnabled: deps.getElements().effectsToneEffectEnabled?.checked || false,
        toneEffectMode: normalizeEffectsToneEffectMode(deps.getElements().effectsToneEffectMode?.value, 'crystalizer'),
    };
}

// The extras API merges only explicitly supplied fields, and a present
// loudnessEnabled is the canonical Loudness/Volume transition signal.
// Send it only on an actual enabled-state change (relative to the last
// known server state) so unrelated extras saves never take the canonical
// volume write path.  With unknown server state it is still sent.
function buildEffectsExtrasSaveBody(extras, serverExtras) {
    const body = { ...extras };
    if (serverExtras && !!body.loudnessEnabled === !!serverExtras.loudness?.enabled) {
        delete body.loudnessEnabled;
    }
    return body;
}

async function _doSaveEffectsExtras() {
    const compareBusy = (typeof window !== 'undefined' && window.FXRouteBankUI?.isCompareLoadBusy?.())
        ?? (typeof globalThis !== 'undefined' && globalThis.FXRouteBankUI?.isCompareLoadBusy?.())
        ?? false;
    if (compareBusy || effectsExtrasSaveInFlight) {
        effectsExtrasPendingResave = true;
        return;
    }
    effectsExtrasSaveInFlight = true;
    effectsExtrasPendingResave = false;
    const extras = collectEffectsExtras();
    const body = buildEffectsExtrasSaveBody(extras, deps.getState().dsp?.global_extras);
    try {
        const resp = await fetch('/api/dsp/extras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save output extras');
        deps.getState().dsp = deps.getState().dsp || {};
        deps.getState().dsp.global_extras = data.extras || {
            limiter: { enabled: !!extras.limiterEnabled, params: { thresholdDb: -1.0, attackMs: 5.0, releaseMs: 20.0, lookaheadMs: 5.0, stereoLinkPercent: 100.0 } },
            headroom: { enabled: !!extras.headroomEnabled, params: { gainDb: extras.headroomGainDb } },
            autogain: { enabled: !!extras.autogainEnabled, params: { targetDb: extras.autogainTargetDb } },
            loudness: {
                enabled: !!extras.loudnessEnabled,
                params: {
                    ...(deps.getState().dsp?.global_extras?.loudness?.params || {}),
                    strength: extras.loudnessStrength,
                    fftSize: extras.loudnessFftSize,
                },
            },
            delay: { enabled: !!extras.delayEnabled, params: { leftMs: extras.delayLeftMs, rightMs: extras.delayRightMs } },
            bass_enhancer: { enabled: !!extras.bassEnabled, params: { amount: extras.bassAmount, harmonics: 8.5, scope: 100.0, blend: 0.0 } },
            tone_effect: { enabled: !!extras.toneEffectEnabled, mode: extras.toneEffectMode },
        };
        renderEffects();
        // Success clears a previous failure label: the tile must never keep
        // reporting "Failed" after the latest commit already fixed it.
        setEffectsExtrasFeedback('', '');
    } catch (error) {
        setEffectsExtrasFeedback('Failed', 'error');
        deps.showToast(error.message || 'Failed to save output extras', 'error');
    } finally {
        effectsExtrasSaveInFlight = false;
        if (effectsExtrasPendingResave) {
            effectsExtrasPendingResave = false;
            saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
        }
    }
}

function setEffectsExtrasFeedback(message, cls) {
    if (!deps.getElements().effectsExtrasFeedback) return;
    deps.getElements().effectsExtrasFeedback.textContent = message;
    deps.getElements().effectsExtrasFeedback.className = 'effects-extras-feedback' + (cls ? ' ' + cls : '');
}

    return {
        init,
        _doSaveEffectsExtras,
        addPeqBandPair,
        applyEffectsExtras,
        buildEffectsExtrasSaveBody,
        clearEffectsPeqStatusOnCollapse,
        collectEffectsExtras,
        collectPeqBandsFromDom,
        createPeqPreset,
        defaultPeqBand,
        describeEffectsExtras,
        ensurePeqBandExists,
        fetchEffects,
        formatPeqSummaryBand,
        getDefaultPeqDraft,
        getOtherPeqSide,
        getPeqBandFallback,
        getPeqBandPair,
        getPeqGainTotal,
        getPeqLinkedSpecialType,
        isPeqDelayBand,
        isPeqGainBand,
        loadSavedEffectsExtras,
        normalizeEffectsAutogainTargetDb,
        normalizeEffectsHeadroomGainDb,
        normalizeEffectsLoudnessFftSize,
        normalizeEffectsLoudnessStrength,
        normalizeEffectsToneEffectMode,
        normalizeLinkedPeqSpecialBands,
        normalizePeqEqMode,
        readPeqNumberInput,
        removePeqBand,
        renderEffects,
        renderPeqBandColumn,
        renderPeqBands,
        resetPeqDraft,
        saveEffectsExtrasDebounced,
        setEffectsExtrasFeedback,
        setupEffectsActions,
        switchEffectsPreset,
        syncLinkedPeqSpecialBand,
        syncLinkedPeqSpecialBandValueInDom,
        updateEffectsExtrasUi,
        updateEffectsPeqDisclosureLabel,
        updateEffectsPeqSummary,
        updatePeqBand,
        validatePeqBands,
    };
});
