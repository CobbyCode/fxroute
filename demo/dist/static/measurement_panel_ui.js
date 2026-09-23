// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement panel setup and capture-input rendering. */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteMeasurementPanelUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getElements: () => ({}),
        getDocument: () => root.document,
        renderMeasurementArea: () => {},
        isSelectFocused: () => false,
        getSelectedMeasurementInput: () => null,
        getSelectedMeasurementInputChannelCount: () => 1,
        getMeasurementReferenceWarning: () => '',
        measurementModeReady: () => false,
        escapeHtml: (value) => String(value == null ? '' : value),
        formatRateKhz: (rate) => String(rate),
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

    function renderMeasurementPanelSetupSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        elements.measurementMain?.classList.toggle('is-setup', measurementState.setupOpen);
        const statusParent = measurementState.setupOpen
            ? elements.measurementSetupCard
            : elements.measurementSetupToggleBtn?.closest('.measurement-card-controls');
        if (elements.measurementSetupStatus && statusParent && elements.measurementSetupStatus.parentElement !== statusParent) {
            statusParent.appendChild(elements.measurementSetupStatus);
        }
        if (elements.measurementSetupCard) {
            elements.measurementSetupCard.classList.toggle('hidden', !measurementState.setupOpen);
        }
        if (elements.measurementSetupToggleBtn) {
            elements.measurementSetupToggleBtn.disabled = measurementState.startInFlight;
        }
        if (elements.measurementModeNote) {
            elements.measurementModeNote.textContent = measurementState.modeNote || '';
        }
        deps.renderMeasurementArea();
        deps.getDocument().querySelectorAll('[data-measurement-smoothing]').forEach((button) => {
            const active = (button.getAttribute('data-measurement-smoothing') || '') === (measurementState.displaySmoothing || '1/6-oct');
            button.classList.toggle('is-active', active);
            button.disabled = !frequencyView;
            button.title = frequencyView ? '' : 'Only available in frequency view.';
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
        deps.getDocument().querySelectorAll('[data-measurement-view]').forEach((button) => {
            const active = (button.getAttribute('data-measurement-view') || 'freq') === graphView;
            button.classList.toggle('is-active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }

    function renderMeasurementPanelInputsSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementInputGroup) {
            elements.measurementInputGroup.classList.remove('hidden');
        }
        if (elements.measurementInputSelect && !deps.isSelectFocused(elements.measurementInputSelect)) {
            const availableInputs = measurementState.inputs && measurementState.inputs.length
                ? measurementState.inputs
                : [{ id: '', label: measurementState.inputsLoading ? 'Loading…' : 'No host capture inputs available' }];
            const inputs = measurementState.selectedInputUnavailable
                ? [{ id: '', label: 'Previously selected microphone (unavailable)' }, ...availableInputs]
                : availableInputs;
            elements.measurementInputSelect.innerHTML = inputs.map(input => `<option value="${deps.escapeHtml(input.id)}" ${input.id === measurementState.selectedInputId ? 'selected' : ''}>${deps.escapeHtml(input.label)}</option>`).join('');
            elements.measurementInputSelect.disabled = measurementState.inputsLoading || !measurementState.hostCaptureAvailable;
        }
        if (elements.measurementConvolverSampleRate && !deps.isSelectFocused(elements.measurementConvolverSampleRate)) {
            const selectedInput = deps.getSelectedMeasurementInput();
            const rates = selectedInput?.supportedRates?.length ? selectedInput.supportedRates : [48000];
            elements.measurementConvolverSampleRate.innerHTML = rates.map(rate => `<option value="${deps.escapeHtml(rate)}">${deps.formatRateKhz(rate)}</option>`).join('');
            elements.measurementConvolverSampleRate.value = String(measurementState.measurementSampleRate || 48000);
            elements.measurementConvolverSampleRate.disabled = measurementState.startInFlight || !deps.measurementModeReady();
        }
        if (elements.measurementInputRefreshBtn) {
            elements.measurementInputRefreshBtn.disabled = measurementState.startInFlight || measurementState.inputsLoading;
            elements.measurementInputRefreshBtn.textContent = measurementState.inputsLoading ? 'Detecting…' : 'Detect / refresh host microphones';
        }
        const inputChannelCount = deps.getSelectedMeasurementInputChannelCount();
        const inputChannelOptions = Array.from({ length: inputChannelCount }, (_, index) => String(index + 1));
        const splitReferences = inputChannelCount >= 3;
        const renderReferenceSelect = (select, selectedValue) => {
            if (!select || deps.isSelectFocused(select)) return;
            select.innerHTML = [''].concat(inputChannelOptions)
                .map(value => `<option value="${value}" ${value === (selectedValue || '') ? 'selected' : ''}>${value ? `Input ${value}` : 'None'}</option>`)
                .join('');
            select.disabled = measurementState.startInFlight || !deps.measurementModeReady();
        };
        if (elements.measurementInputChannelGrid) {
            elements.measurementInputChannelGrid.classList.toggle('is-split', splitReferences);
        }
        if (elements.measurementReferenceSingleGroup) {
            elements.measurementReferenceSingleGroup.classList.toggle('hidden', splitReferences);
        }
        if (elements.measurementReferenceLeftGroup) {
            elements.measurementReferenceLeftGroup.classList.toggle('hidden', !splitReferences);
        }
        if (elements.measurementReferenceRightGroup) {
            elements.measurementReferenceRightGroup.classList.toggle('hidden', !splitReferences);
        }
        if (elements.measurementMicInputChannelSelect && !deps.isSelectFocused(elements.measurementMicInputChannelSelect)) {
            elements.measurementMicInputChannelSelect.innerHTML = inputChannelOptions
                .map(value => `<option value="${value}" ${value === measurementState.selectedMicInputChannel ? 'selected' : ''}>Input ${value}</option>`)
                .join('');
            elements.measurementMicInputChannelSelect.disabled = measurementState.startInFlight || !deps.measurementModeReady();
        }
        renderReferenceSelect(elements.measurementReferenceInputChannelSelect, measurementState.selectedReferenceInputChannel);
        renderReferenceSelect(elements.measurementReferenceInputChannelLeftSelect, measurementState.selectedReferenceInputChannelLeft);
        renderReferenceSelect(elements.measurementReferenceInputChannelRightSelect, measurementState.selectedReferenceInputChannelRight);
        if (elements.measurementReferenceWarning) {
            const referenceWarning = deps.getMeasurementReferenceWarning();
            elements.measurementReferenceWarning.textContent = referenceWarning;
            elements.measurementReferenceWarning.classList.toggle('hidden', !referenceWarning);
        }
    }

    return { init, renderMeasurementPanelSetupSection, renderMeasurementPanelInputsSection };
});
