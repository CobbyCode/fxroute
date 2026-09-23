// SPDX-License-Identifier: AGPL-3.0-only
/** Measurement panel setup, capture-input and file/status rendering. */
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
        measurementAreaBadge: () => null,
        syncMeasurementSweepButton: () => {},
        getActiveMeasurementKind: () => '',
        hasActiveMeasurementJob: () => false,
        measurementRepeatBlockedReason: () => '',
        syncMeasurementRepeatNote: () => {},
        ensureCustomHouseCurveState: () => ({}),
        getMeasurementConvolverCurveOptions: () => [],
        getDefaultMeasurementConvolverState: () => ({}),
        measurementSetupStatusText: () => '',
        buildMeasurementIrDiagnostics: () => [],
        buildMeasurementIrSummary: () => '',
        buildMeasurementIrDiagnosticsTooltip: () => '',
        renderMeasurementIrDiagnostics: () => {},
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

    function renderMeasurementPanelCalibrationSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementCalibrationSelect) {
            const options = [{ id: '', filename: 'No calibration file' }, ...(measurementState.calibrationOptions || [])];
            elements.measurementCalibrationSelect.innerHTML = options.map(option => `<option value="${deps.escapeHtml(option.id || '')}" ${(option.id || '') === (measurementState.selectedCalibrationRef || '') ? 'selected' : ''}>${deps.escapeHtml(option.filename || 'Calibration')}</option>`).join('');
            elements.measurementCalibrationSelect.disabled = measurementState.startInFlight || measurementState.calibrationUpdating || measurementState.calibrationDeleting;
        }
        if (elements.measurementCalibrationDeleteBtn) {
            const canDeleteCalibration = !!measurementState.selectedCalibrationRef && !measurementState.startInFlight && !measurementState.activeJobId && !measurementState.calibrationUpdating && !measurementState.calibrationDeleting && !measurementState.calibrationExporting;
            elements.measurementCalibrationDeleteBtn.disabled = !canDeleteCalibration;
            elements.measurementCalibrationDeleteBtn.textContent = measurementState.calibrationDeleting ? 'Deleting…' : 'Delete';
        }
        if (elements.measurementCalibrationExportBtn) {
            const selectedCalibration = (measurementState.calibrationOptions || []).some(option => option.id === measurementState.selectedCalibrationRef);
            const canExportCalibration = selectedCalibration && !measurementState.startInFlight && !measurementState.calibrationUpdating && !measurementState.calibrationDeleting && !measurementState.calibrationExporting;
            elements.measurementCalibrationExportBtn.disabled = !canExportCalibration;
            elements.measurementCalibrationExportBtn.textContent = measurementState.calibrationExporting ? 'Exporting…' : 'Export';
        }
        if (elements.measurementCalibrationUploadName) {
            elements.measurementCalibrationUploadName.textContent = measurementState.calibrationFilename || 'No calibration file selected.';
        }
        if (elements.measurementCalibrationName) {
            const selectedCalibration = (measurementState.calibrationOptions || []).find(option => option.id === measurementState.selectedCalibrationRef);
            const activeCalibrationLabel = measurementState.calibrationFilename
                ? measurementState.calibrationFilename
                : (selectedCalibration ? selectedCalibration.filename : '');
            elements.measurementCalibrationName.textContent = activeCalibrationLabel;
            elements.measurementCalibrationName.classList.toggle('hidden', !activeCalibrationLabel);
        }
    }

    function renderMeasurementPanelHouseCurveSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementHouseCurveSelect) {
            const houseCurveOptions = measurementState.houseCurveOptions || [];
            const selectedHouseCurveId = String(conv.targetCurve || '').startsWith('house:') ? String(conv.targetCurve).slice(6) : '';
            const options = houseCurveOptions.length
                ? houseCurveOptions
                : [{ id: '', filename: 'Built-in target curves only' }];
            elements.measurementHouseCurveSelect.innerHTML = options.map(option => `<option value="${deps.escapeHtml(option.id || '')}" ${(option.id || '') === selectedHouseCurveId ? 'selected' : ''}>${deps.escapeHtml(option.filename || 'House curve')}</option>`).join('');
            elements.measurementHouseCurveSelect.disabled = measurementState.houseCurveUpdating || measurementState.houseCurveDeleting;
        }
        const selectedHouseCurveId = elements.measurementHouseCurveSelect ? (elements.measurementHouseCurveSelect.value || '') : '';
        const hasSelectedHouseCurve = !!selectedHouseCurveId && (measurementState.houseCurveOptions || []).some(option => option.id === selectedHouseCurveId);
        if (elements.measurementHouseCurveDeleteBtn) {
            const canDeleteHouseCurve = hasSelectedHouseCurve && !measurementState.houseCurveUpdating && !measurementState.houseCurveDeleting && !measurementState.houseCurveExporting;
            elements.measurementHouseCurveDeleteBtn.disabled = !canDeleteHouseCurve;
            elements.measurementHouseCurveDeleteBtn.textContent = measurementState.houseCurveDeleting ? 'Deleting…' : 'Delete';
        }
        if (elements.measurementHouseCurveExportBtn) {
            const canExportHouseCurve = hasSelectedHouseCurve && !measurementState.houseCurveUpdating && !measurementState.houseCurveDeleting && !measurementState.houseCurveExporting;
            elements.measurementHouseCurveExportBtn.disabled = !canExportHouseCurve;
            elements.measurementHouseCurveExportBtn.textContent = measurementState.houseCurveExporting ? 'Exporting…' : 'Export';
        }
        if (elements.measurementHouseCurveUploadName) {
            elements.measurementHouseCurveUploadName.textContent = measurementState.houseCurveFilename || 'No house curve file selected.';
        }
        if (elements.measurementHouseCurveName) {
            const selectedHouseCurveId = String(conv.targetCurve || '').startsWith('house:') ? String(conv.targetCurve).slice(6) : '';
            const selectedHouseCurve = (measurementState.houseCurveOptions || []).find(option => option.id === selectedHouseCurveId);
            const activeHouseCurveLabel = measurementState.houseCurveFilename
                ? measurementState.houseCurveFilename
                : (selectedHouseCurve ? selectedHouseCurve.filename : '');
            elements.measurementHouseCurveName.textContent = activeHouseCurveLabel;
            elements.measurementHouseCurveName.classList.toggle('hidden', !activeHouseCurveLabel);
        }
    }

    function renderMeasurementPanelActionsSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementNameInput) {
            elements.measurementNameInput.value = measurementState.currentMeasurementName || '';
            elements.measurementNameInput.disabled = measurementState.startInFlight || measurementState.saveInFlight || !!measurementState.activeJobId;
            // The area badge next to the name shows where the current result was
            // captured; the sweep menu's own indicator shows where the next one goes.
            const areaBadge = deps.measurementAreaBadge(current);
            elements.measurementNameInput.title = areaBadge
                ? `Measured area: ${areaBadge.title}`
                : 'Measurement name';
            elements.measurementNameInput.placeholder = 'Measurement name';
        }
        deps.syncMeasurementSweepButton();
        if (elements.measurementRepeatStartBtn) {
            const activeKind = deps.getActiveMeasurementKind();
            const activeJobRunning = deps.hasActiveMeasurementJob();
            const lrActive = activeKind === 'lr_repeat';
            const repeatBlockedReason = deps.measurementRepeatBlockedReason();
            elements.measurementRepeatStartBtn.disabled = repeatBlockedReason && !lrActive
                ? true
                : ((measurementState.calibrationUpdating || measurementState.calibrationDeleting)
                    ? true
                    : (activeJobRunning ? !lrActive
                    : (measurementState.startInFlight || measurementState.inputsLoading || !deps.measurementModeReady())));
            elements.measurementRepeatStartBtn.textContent = lrActive
                ? 'Cancel measurement'
                : 'Start LR Repeat';
            deps.syncMeasurementRepeatNote(lrActive, repeatBlockedReason);
        }
        if (elements.measurementSaveBtn) {
            const hasAutoSubMeas = Array.isArray(measurementState.autoSubMeasurements) && measurementState.autoSubMeasurements.length > 0;
            const hasUnsavedContent = hasAutoSubMeas || (current && !measurementState.currentMeasurementSaved);
            elements.measurementSaveBtn.disabled = !hasUnsavedContent || measurementState.saveInFlight || measurementState.startInFlight;
            elements.measurementSaveBtn.textContent = measurementState.saveInFlight ? 'Working…' : (measurementState.currentMeasurementSaved && !hasAutoSubMeas ? 'Saved' : 'Save current');
        }
    }

    function renderMeasurementPanelViewSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementAssistMode) {
            elements.measurementAssistMode.value = assistMode;
            elements.measurementAssistMode.disabled = !frequencyView;
            elements.measurementAssistMode.title = frequencyView ? '' : 'Only available in frequency view.';
        }
        if (elements.measurementTargetCurve) {
            const editingCustomHouseCurve = activeEditor === 'houseCurve' && deps.ensureCustomHouseCurveState().displayTarget === 'editing-custom-house-curve';
            const editingOption = editingCustomHouseCurve ? '<option value="editing-custom-house-curve">Editing Custom House Curve…</option>' : '';
            elements.measurementTargetCurve.innerHTML = editingOption + deps.getMeasurementConvolverCurveOptions()
                .map((curve) => `<option value="${deps.escapeHtml(curve.key)}" ${!editingCustomHouseCurve && conv.targetCurve === curve.key ? 'selected' : ''}>${deps.escapeHtml(curve.label || curve.shortLabel || curve.key)}</option>`)
                .join('') + '<option value="create-custom-house-curve">Create Custom House Curve…</option>';
            elements.measurementTargetCurve.value = editingCustomHouseCurve ? 'editing-custom-house-curve' : conv.targetCurve;
            elements.measurementTargetCurve.disabled = !frequencyView;
            elements.measurementTargetCurve.title = frequencyView ? '' : 'Only available in frequency view.';
            elements.measurementTargetCurve.classList.remove('hidden');
        }
        if (elements.measurementClearBtn) {
            const defaultConv = deps.getDefaultMeasurementConvolverState();
            const hasConvolverResettableState = assistMode === 'convolver' && (
                conv.targetCurve !== defaultConv.targetCurve
                || Math.round(conv.rangeStartHz) !== defaultConv.rangeStartHz
                || Math.round(conv.rangeEndHz) !== defaultConv.rangeEndHz
                || Number(conv.maxBoostDb) !== defaultConv.maxBoostDb
                || Number(conv.maxCutDb) !== defaultConv.maxCutDb
                || String(conv.dipGuard) !== defaultConv.dipGuard
                || String(conv.quality) !== defaultConv.quality
            );
            const hasResettableGraphState = !!current || !!peq.filters.length || hasConvolverResettableState || activeEditor === 'houseCurve';
            elements.measurementClearBtn.disabled = !frequencyView || !hasResettableGraphState || measurementState.startInFlight || !!measurementState.activeJobId;
            elements.measurementClearBtn.title = frequencyView ? '' : 'Only available in frequency view.';
        }
    }

    function renderMeasurementPanelStatusSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
        const elements = deps.getElements();
        if (elements.measurementSetupStatus) {
            elements.measurementSetupStatus.textContent = deps.measurementSetupStatusText();
        }
        if (elements.measurementSummary) {
            if (!frequencyView) {
                elements.measurementSummary.textContent = 'IR -2–30 ms';
            } else if (assistMode === 'convolver') {
                elements.measurementSummary.textContent = `${Math.round(conv.rangeStartHz)}–${Math.round(conv.rangeEndHz)} Hz`;
            } else {
                elements.measurementSummary.textContent = peq.filters.length ? `${peq.filters.length}/12 assistant filters` : '';
            }
        }
        if (elements.measurementGraphSubtitle) {
            elements.measurementGraphSubtitle.textContent = frequencyView
                ? 'Frequency view: 20 Hz to 20 kHz.'
                : 'Impulse response view: -2 ms to +30 ms.';
        }
        if (elements.measurementEmpty) {
            elements.measurementEmpty.textContent = frequencyView
                ? 'No current or saved measurements yet.'
                : 'No IR previews available for the visible measurements.';
            elements.measurementEmpty.classList.toggle('hidden', graphEntries.length > 0);
        }
        if (elements.measurementGraphControls) {
            const irDiagnostics = deps.buildMeasurementIrDiagnostics(graphEntries, frequencyView);
            const irSummary = deps.buildMeasurementIrSummary(irDiagnostics);
            const irTooltip = deps.buildMeasurementIrDiagnosticsTooltip(irDiagnostics);
            elements.measurementGraphControls.textContent = !frequencyView
                ? (irSummary || (graphEntries.length ? 'IR: previews aligned to 0 ms' : 'Run a new sweep to capture an IR preview.'))
                : current
                ? (assistMode === 'convolver' ? 'Drag the blue range block or its edges to set the FIR correction range.' : 'Tap/click near 0 dB to add a filter, drag handles for freq/gain.')
                : 'Run a sweep to see the graph.';
            elements.measurementGraphControls.title = !frequencyView ? irTooltip : '';
        }
        if (elements.measurementGraph && frequencyView) {
            elements.measurementGraph.title = '';
        }
        deps.renderMeasurementIrDiagnostics(graphEntries, frequencyView);
    }

    return {
        init,
        renderMeasurementPanelSetupSection,
        renderMeasurementPanelInputsSection,
        renderMeasurementPanelCalibrationSection,
        renderMeasurementPanelHouseCurveSection,
        renderMeasurementPanelActionsSection,
        renderMeasurementPanelViewSection,
        renderMeasurementPanelStatusSection,
    };
});
