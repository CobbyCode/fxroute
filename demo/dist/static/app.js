// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute - Frontend JavaScript
 * Vanilla JS, no dependencies
 */
const CONFIG = {
    wsUrl: null, // will be set dynamically
    reconnectInterval: 3000,
    offlineIndicatorDelay: 1500,
    maxReconnectAttempts: 10,
};
const MeasurementDsp = window.FXRouteMeasurementDsp || {};
const HybridMeasurement = window.FXRouteHybridMeasurement || {};
const MeasurementUI = window.FXRouteMeasurementUI || {};
const MeasurementGraph = window.FXRouteMeasurementGraph || {};
// Graph module: injected state readers and overlay painters (all hoisted
// app.js functions; element getters resolve lazily after `elements` is built).
window.FXRouteMeasurementGraph?.init({
    getCanvas: () => elements.measurementGraph,
    getPanel: () => elements.measurementPanel,
    getMeasurementGraphView,
    getDisplaySmoothing: () => state.measurement.displaySmoothing || '1/6-oct',
    getCurrentMeasurementEntries,
    getVisibleMeasurementEntries,
    getVisibleMeasurementColorById,
    getMeasurementDisplayTraces,
    getMeasurementTargetCurvePreview,
    getAutoSubDisplayReferenceEntries,
    buildMeasurementIrGraphEntry,
    drawMeasurementIrGraph,
    drawMeasurementTargetCurve,
    drawMeasurementConvolverRangeOverlay,
    drawMeasurementPeqOverlay,
    drawCustomHouseCurveHandles,
});
const MeasurementFlows = window.FXRouteMeasurementFlows || {};
const SettingsSystem = window.FXRouteSettingsSystem || {};
const EffectsUI = window.FXRouteEffectsUI || {};
// Flow module (auto-sub + hybrid wizard): backend calls via injected api,
// state/dom getters plus ui callbacks injected as hoisted references.
window.FXRouteMeasurementFlows?.init({
    api: {
        startAutoSubOptimize: (formData) => fetch('/api/measurements/auto-sub-optimize/start', { method: 'POST', body: formData }),
        cancelAutoSubJob: (jobId) => fetch(`/api/measurements/auto-sub-optimize/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
        pollAutoSubJob: (jobId) => fetch(`/api/measurements/auto-sub-optimize/jobs/${encodeURIComponent(jobId)}`),
        startSpeakerAlign: (payload) => fetch('/api/speaker-align/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
        cancelSpeakerAlignJob: (jobId) => fetch(`/api/speaker-align/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
        pollSpeakerAlignJob: (jobId) => fetch(`/api/speaker-align/jobs/${encodeURIComponent(jobId)}`),
        saveSpeakerAlignMeasurement: (payload) => fetch('/api/measurements/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
        startMeasurement: (formData) => fetch('/api/measurements/start', { method: 'POST', body: formData }),
        cancelMeasurementJob: (jobId) => fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
        pollMeasurementJob: (jobId) => fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}`),
    },
    getState: () => state,
    getElements: () => elements,
    showToast,
    renderMeasurementPanel,
    renderMeasurementPanelDefensively: (...args) => window.FXRouteMeasurementJob.renderMeasurementPanelDefensively(...args),
    isSubwooferModeName,
    isSubwoofer22Mode,
    getActiveMeasurementKind: (...args) => window.FXRouteMeasurementJob.getActiveMeasurementKind(...args),
    hasActiveMeasurementJob: (...args) => window.FXRouteMeasurementJob.hasActiveMeasurementJob(...args),
    measurementModeReady,
    normalizeMeasurementInputChannelSelections: (...args) => window.FXRouteMeasurementSetup.normalizeMeasurementInputChannelSelections(...args),
    getSelectedMeasurementInputChannelCount: (...args) => window.FXRouteMeasurementSetup.getSelectedMeasurementInputChannelCount(...args),
    getAutoSubTargetCurveSnapshot,
    flushSubwooferSettingsBeforeMeasurement,
    postRuntimeDebugSnapshot,
    formatTransitionErrorDetail,
    fetchAudioOutputOverview,
    measurementAreaBank: () => {
        const area = measurementAreaFromCatalog();
        return area ? area.bank_id : '';
    },
    getMeasurementReferenceWarning: (...args) => window.FXRouteMeasurementSetup.getMeasurementReferenceWarning(...args),
    normalizeOutputModeName,
    getMeasurementJobStatus: (job) => MeasurementUI.getMeasurementJobStatus(job),
    normalizeMeasurementEntry: (measurement, index) => MeasurementUI.normalizeMeasurementEntry(measurement, index),
    getMeasurementJobResultMeasurement: (job) => MeasurementUI.getMeasurementJobResultMeasurement(job),
    setMeasurementAssistMode,
    escapeHtml,
    sleep,
    fetchSavedMeasurements: () => fetchMeasurements(),
});
// Provider settings module: state/DOM through lazy getters, app-owned rows
// (device name) and streaming refreshes through explicit callbacks. Runtime
// polling and playback stay in app.js.
window.FXRouteProviderSettings?.init({
    getState: () => state,
    getElements: () => elements,
    showToast,
    escapeHtml,
    onAdminPayload: (data) => {
        // Device name rides the admin payload so one fetch fills both rows;
        // the row itself is owned by the settings system module.
        if (typeof data.device_name === 'string' && data.device_name) {
            state.settings.deviceName.value = data.device_name;
            state.settings.deviceName.loaded = true;
        }
        state.settings.deviceName.canChange = data.device_name_can_change === true;
        SettingsSystem.renderDeviceNameSettings();
    },
    applyProviderEnabledToStreaming: (providerId, enabled) => window.FXRouteStreaming?.applyProviderEnabled(providerId, enabled),
    refreshStreamingFlags: () => { void window.FXRouteStreaming?.refreshEnabledFlags?.(); },
    refreshStreamingTab: () => window.FXRouteStreaming?.refreshActiveTab?.(),
});
// Settings system module (static/settings_system.js): maintenance/update,
// device name, music libraries, hardware controller and the power menu.
// State/DOM through lazy getters; panel and library refreshes stay in
// app.js behind explicit callbacks.
window.FXRouteSettingsSystem?.init({
    getState: () => state,
    getElements: () => elements,
    showToast,
    escapeHtml,
    renderSettingsPanel: () => renderSettingsPanel(),
    renderLibraryView: () => renderLibraryView(),
    fetchLibraryStatus: () => fetchLibraryStatus(),
    confirmDialog: (message) => confirm(message),
});

// Output effects module (static/output_effects_ui.js): effects fetch/render,
// output PEQ band model and editor, preset creation/switching and effects
// extras incl. persistence. Compare/combine/bank/import delegation points at
// window.FXRouteBankUI directly; crossover repaint, catalog refresh and the
// shared upload-area wiring stay in app.js behind explicit callbacks.
window.FXRouteEffectsUI?.init({
    getState: () => state,
    getElements: () => elements,
    showToast,
    escapeHtml: (value) => escapeHtml(value),
    fetchOutputSystemCatalog: (force) => fetchOutputSystemCatalog(force),
    formatTransitionErrorDetail: (detail, fallback) => formatTransitionErrorDetail(detail, fallback),
    measurementBankSumsBothInputs: () => measurementBankSumsBothInputs(),
    renderCrossoverTile: () => renderCrossoverTile(),
    repaintCrossoverGraph: () => repaintCrossoverGraph(),
    setupUploadArea: (areaId, fileInputId, onFile) => setupUploadArea(areaId, fileInputId, onFile),
    getActiveEditing: () => _activeEditing,
    isPeqCreateInFlight: () => peqCreateInFlight,
    setPeqCreateInFlight: (active) => { peqCreateInFlight = active; },
});
// Crossover tile UI: state/DOM through lazy getters; output mutations and
// box ownership stay in app.js behind explicit callbacks. Bank, routing and
// measurement logic are not moved.
window.FXRouteCrossoverUI?.init({
    getState: () => state,
    getElements: () => elements,
    showToast,
    ensureOutputBoxes: () => ensureOutputSystemBoxes(),
    applyMutation: (kind, fields, message, options) => applyOutputSystemMutation(kind, fields, message, options),
});
// Subwoofer tile UI: state/DOM through lazy getters, mutations through the
// app path, and the shared edit guard through an accessor (owned by app.js).
window.FXRouteSubwooferUI?.init({
    getState: () => state,
    getElements: () => elements,
    getActiveEditing: () => _activeEditing,
    applyMutation: (kind, fields, message, options) => applyOutputSystemMutation(kind, fields, message, options),
});
// Output-system controller: catalog fetch plus revisioned mutations with a
// fixed downstream refresh order. State boxes and all renders stay in app.js.
window.FXRouteOutputSystemController?.init({
    getState: () => state,
    showToast,
    ensureOutputBoxes: () => ensureOutputSystemBoxes(),
    renderOutputSection: () => renderOutputSystemSection(),
    renderBankSelector: () => window.FXRouteBankUI.renderEffectsBankSelector(),
    renderCompare: () => window.FXRouteBankUI.renderEffectsCompare(),
    syncCrossover: (enabled) => {
        if (enabled) {
            void fetchCrossoverResponse();
        } else {
            state.crossover.response = null;
            renderCrossoverTile();
        }
    },
    syncSpeakerAlign: () => MeasurementFlows.syncSpeakerAlignButton(),
    renderSubwoofer: () => renderSubwooferPanel(),
    syncAutoSub: () => MeasurementFlows.syncAutoSubButton(),
    reportMutationError: (html) => {
        if (elements.osFeedback) elements.osFeedback.innerHTML = html;
    },
    syncCompareBusy: () => window.FXRouteBankUI.setEffectsCompareLoadBusy(
        (typeof window !== 'undefined' && window.FXRouteBankUI?.isCompareLoadBusy?.())
        ?? (typeof globalThis !== 'undefined' && globalThis.FXRouteBankUI?.isCompareLoadBusy?.())
        ?? false),
});
// Bank/compare/import UI: state/DOM through lazy getters; effects editors,
// measurement editor events/takes and the main effects render stay in app.js
// behind explicit callbacks, mutations run through the controller-owned app path.
window.FXRouteBankUI?.init({
    getState: () => state,
    getElements: () => elements,
    showToast,
    escapeHtml,
    fetchEffects: () => EffectsUI.fetchEffects(),
    refreshCatalog: (force) => fetchOutputSystemCatalog(force),
    collectEffectsExtras: () => EffectsUI.collectEffectsExtras(),
    measurementBankSumsBothInputs: () => measurementBankSumsBothInputs(),
    presetFileUrl: (name) => MeasurementUI.presetFileUrl(name),
    renderEffects: () => EffectsUI.renderEffects(),
    renderMeasurementArea: () => renderMeasurementArea(),
    measurementArea: () => measurementAreaFromCatalog(),
    applyMutation: (kind, fields, message, options) => applyOutputSystemMutation(kind, fields, message, options),
    confirmDialog: (message) => confirm(message),
});
window.FXRouteMeasurementPanelUI?.init({
    getElements: () => elements,
    renderMeasurementArea: () => renderMeasurementArea(),
    isSelectFocused: (select) => isSelectFocused(select),
    getSelectedMeasurementInput: () => window.FXRouteMeasurementSetup.getSelectedMeasurementInput(),
    getSelectedMeasurementInputChannelCount: () => window.FXRouteMeasurementSetup.getSelectedMeasurementInputChannelCount(),
    getMeasurementReferenceWarning: () => window.FXRouteMeasurementSetup.getMeasurementReferenceWarning(),
    measurementModeReady: () => measurementModeReady(),
    escapeHtml: (value) => escapeHtml(value),
    formatRateKhz: (rate) => formatRateKhz(rate),
    measurementAreaBadge: (measurement) => window.FXRouteMeasurementSavedUI.measurementAreaBadge(measurement),
    syncMeasurementSweepButton: () => window.FXRouteMeasurementJob.syncMeasurementSweepButton(),
    getActiveMeasurementKind: () => window.FXRouteMeasurementJob.getActiveMeasurementKind(),
    hasActiveMeasurementJob: () => window.FXRouteMeasurementJob.hasActiveMeasurementJob(),
    measurementRepeatBlockedReason: () => measurementRepeatBlockedReason(),
    syncMeasurementRepeatNote: (lrActive, reason) => syncMeasurementRepeatNote(lrActive, reason),
    ensureCustomHouseCurveState: () => window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState(),
    getMeasurementConvolverCurveOptions: () => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverCurveOptions(),
    getDefaultMeasurementConvolverState: () => window.FXRouteMeasurementConvolverEditor.getDefaultMeasurementConvolverState(),
    measurementSetupStatusText: () => measurementSetupStatusText(),
    buildMeasurementIrDiagnostics: (entries, frequencyView) => MeasurementUI.buildMeasurementIrDiagnostics(entries, frequencyView),
    buildMeasurementIrSummary: (diagnostics) => MeasurementUI.buildMeasurementIrSummary(diagnostics),
    buildMeasurementIrDiagnosticsTooltip: (diagnostics) => MeasurementUI.buildMeasurementIrDiagnosticsTooltip(diagnostics),
    renderMeasurementIrDiagnostics: (entries, frequencyView) => renderMeasurementIrDiagnostics(entries, frequencyView),
});
window.FXRouteMeasurementSavedUI?.init({
    getState: () => state,
    getElements: () => elements,
    getCurrentMeasurementEntry: () => getCurrentMeasurementEntry(),
    getVisibleMeasurementColorById: () => getVisibleMeasurementColorById(),
    getOutputSystemModule: () => outputSystemModule(),
    getCompactDisplayName: (name, maxChars) => window.FXRouteBankUI.getCompactDisplayName(name, maxChars),
    escapeHtml: (value) => escapeHtml(value),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    mergeSelectedMeasurements: () => window.FXRouteMeasurementSavedActions.mergeSelectedMeasurements(),
    deleteSelectedMeasurements: () => window.FXRouteMeasurementSavedActions.deleteSelectedMeasurements(),
});
window.FXRouteMeasurementEditorsUI?.init({
    getState: () => state,
    getElements: () => elements,
    getDocument: () => document,
    escapeHtml: (value) => escapeHtml(value),
    ensureCustomHouseCurveState: () => window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState(),
    getCustomHouseCurvePointSlot: (id) => getCustomHouseCurvePointSlot(id),
    getCustomHouseCurvePointColor: (point, slot) => getCustomHouseCurvePointColor(point, slot),
    getMeasurementPeqPresetName: (mode) => window.FXRouteMeasurementPeqEditor.getMeasurementPeqPresetName(mode),
    getMeasurementPeqDraftMode: (peq) => window.FXRouteMeasurementPeqEditor.getMeasurementPeqDraftMode(peq),
    isPeqCreateInFlight: () => peqCreateInFlight,
    measurementBankSumsBothInputs: () => measurementBankSumsBothInputs(),
    getMeasurementConvolverCurveOptions: () => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverCurveOptions(),
    getMeasurementConvolverSourceSelectionState: () => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverSourceSelectionState(),
    analyzeMeasurementConvolverSide: (side) => window.FXRouteMeasurementConvolverEditor.analyzeMeasurementConvolverSide(side),
    getMeasurementConvolverDraftPhaseMismatch: (conv) => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverDraftPhaseMismatch(conv),
    isConvolverCreateInFlight: () => convolverCreateInFlight,
    getMeasurementConvolverCurve: (key) => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverCurve(key),
    getMeasurementConvolverTimingDelta: (left, right) => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverTimingDelta(left, right),
    getMeasurementDirectArrivalTiming: (measurement) => window.FXRouteMeasurementConvolverEditor.getMeasurementDirectArrivalTiming(measurement),
    getMeasurementConvolverMeasurementForSide: (side) => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverMeasurementForSide(side),
    formatMeasurementConvolverTimingRelation: (timing) => window.FXRouteMeasurementConvolverEditor.formatMeasurementConvolverTimingRelation(timing),
    getMeasurementConvolverItemName: (mode, gain, options) => window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverItemName(mode, gain, options),
    buildMeasurementConvolverWarnings: (analyses) => window.FXRouteMeasurementConvolverEditor.buildMeasurementConvolverWarnings(analyses),
    getMeasurementPeqActiveFilter: () => window.FXRouteMeasurementPeqEditor.getMeasurementPeqActiveFilter(),
    updateMeasurementPeqFilter: (id, patch) => window.FXRouteMeasurementPeqEditor.updateMeasurementPeqFilter(id, patch),
    updateCustomHouseCurvePoint: (id, patch) => window.FXRouteMeasurementCalibration.updateCustomHouseCurvePoint(id, patch),
    stepMeasurementPeqFrequency: (id, direction, step) => window.FXRouteMeasurementPeqEditor.stepMeasurementPeqFrequency(id, direction, step),
    stepMeasurementPeqGain: (id, direction, step) => window.FXRouteMeasurementPeqEditor.stepMeasurementPeqGain(id, direction, step),
    stepMeasurementPeqQ: (id, direction, step) => window.FXRouteMeasurementPeqEditor.stepMeasurementPeqQ(id, direction, step),
    selectMeasurementPeqFilter: (id) => window.FXRouteMeasurementPeqEditor.selectMeasurementPeqFilter(id),
    addMeasurementPeqFilter: () => window.FXRouteMeasurementPeqEditor.addMeasurementPeqFilter(),
    addCustomHouseCurvePoint: (point) => window.FXRouteMeasurementCalibration.addCustomHouseCurvePoint(point),
    deleteCustomHouseCurvePoint: (id) => window.FXRouteMeasurementCalibration.deleteCustomHouseCurvePoint(id),
    deleteMeasurementPeqFilter: (id) => window.FXRouteMeasurementPeqEditor.deleteMeasurementPeqFilter(id),
    handleMeasurementPeqNumberInputArrowKey: (event) => window.FXRouteMeasurementPeqEditor.handleMeasurementPeqNumberInputArrowKey(event),
    focusMeasurementPeqPanelContext: () => window.FXRouteMeasurementPeqEditor.focusMeasurementPeqPanelContext(),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    scheduleMeasurementGraphRender: () => MeasurementGraph.scheduleMeasurementGraphRender(),
});
window.FXRouteMeasurementSetup?.init({
    getState: () => state,
    fetch: (...args) => fetch(...args),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    measurementModeNoteText: () => measurementModeNoteText(),
    describeMeasurementScope: (note) => describeMeasurementScope(note),
});
window.FXRouteMeasurementCalibration?.init({
    getState: () => state,
    getElements: () => elements,
    fetch: (...args) => fetch(...args),
    showToast: (message, kind) => showToast(message, kind),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    scheduleMeasurementGraphRender: () => MeasurementGraph.scheduleMeasurementGraphRender(),
    fetchMeasurements: () => fetchMeasurements(),
    updateMeasurementConvolverField: (field, value) => window.FXRouteMeasurementConvolverEditor.updateMeasurementConvolverField(field, value),
    ensureMeasurementConvolverState: () => window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState(),
    setMeasurementActiveEditor: (editor) => setMeasurementActiveEditor(editor),
    measurementXToFrequency: (x, bounds) => MeasurementDsp.measurementXToFrequency(x, bounds),
    measurementYToDb: (y, bounds, range) => MeasurementDsp.measurementYToDb(y, bounds, range),
    triggerBlobDownload: (blob, name) => triggerBlobDownload(blob, name),
    getDownloadFilenameFromResponse: (response, fallback) => getDownloadFilenameFromResponse(response, fallback),
});
window.FXRouteMeasurementPeqEditor?.init({
    getState: () => state,
    getElements: () => elements,
    fetch: (...args) => fetch(...args),
    showToast: (message, kind) => showToast(message, kind),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    scheduleMeasurementGraphRender: () => MeasurementGraph.scheduleMeasurementGraphRender(),
    getMeasurementActiveEditor: () => getMeasurementActiveEditor(),
    setMeasurementActiveEditor: (editor) => setMeasurementActiveEditor(editor),
    measurementBankSumsBothInputs: () => measurementBankSumsBothInputs(),
    requireConcreteFilterBank: () => window.FXRouteBankUI.requireConcreteFilterBank(),
    validatePeqBands: (side, bands) => EffectsUI.validatePeqBands(side, bands),
    normalizePeqEqMode: (mode) => EffectsUI.normalizePeqEqMode(mode),
    collectEffectsExtras: () => EffectsUI.collectEffectsExtras(),
    bankBindingJson: () => window.FXRouteBankUI.bankBindingJson(),
    measurementCommitSourceId: () => measurementCommitSourceId(),
    measurementPeqParams: (left, right, mode) => window.FXRouteBankUI.measurementPeqParams(left, right, mode),
    formatTransitionErrorDetail: (detail, fallback) => formatTransitionErrorDetail(detail, fallback),
    fetchEffects: () => EffectsUI.fetchEffects(),
    fetchOutputSystemCatalog: (force) => fetchOutputSystemCatalog(force),
    isPeqCreateInFlight: () => peqCreateInFlight,
    setPeqCreateInFlight: (active) => { peqCreateInFlight = active; },
    getGraphPointerId: () => measurementGraphPointerId,
    setGraphPointerId: (id) => { measurementGraphPointerId = id; },
    getMeasurementGraphPointerPosition: (event) => getMeasurementGraphPointerPosition(event),
    getPeqHandleHitRadiusPx: () => MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX,
    getPeqTouchHandleHitRadiusPx: () => MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX,
    getPeqTouchCreateCooldownMs: () => MEASUREMENT_PEQ_TOUCH_CREATE_COOLDOWN_MS,
});
window.FXRouteMeasurementConvolverEditor?.init({
    getState: () => state,
    getElements: () => elements,
    fetch: (...args) => fetch(...args),
    showToast: (message, kind) => showToast(message, kind),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    scheduleMeasurementGraphRender: () => MeasurementGraph.scheduleMeasurementGraphRender(),
    saveMeasurementSetupSettings: (patch) => window.FXRouteMeasurementSetup.saveMeasurementSetupSettings(patch),
    collectEffectsExtras: () => EffectsUI.collectEffectsExtras(),
    appendBankBindingFields: (formData) => window.FXRouteBankUI.appendBankBindingFields(formData),
    measurementCommitSourceId: () => measurementCommitSourceId(),
    requireConcreteFilterBank: () => window.FXRouteBankUI.requireConcreteFilterBank(),
    measurementBankSumsBothInputs: () => measurementBankSumsBothInputs(),
    fetchEffects: () => EffectsUI.fetchEffects(),
    fetchOutputSystemCatalog: (force) => fetchOutputSystemCatalog(force),
    formatTransitionErrorDetail: (detail, fallback) => formatTransitionErrorDetail(detail, fallback),
    getVisibleMeasurementEntries: () => getVisibleMeasurementEntries(),
    getCurrentMeasurementEntries: () => getCurrentMeasurementEntries(),
    getMeasurementDisplayTraces: (measurement) => getMeasurementDisplayTraces(measurement),
    smoothMeasurementTracePoints: (points, mode) => MeasurementDsp.smoothMeasurementTracePoints(points, mode),
    waitForNextAnimationFrame: () => waitForNextAnimationFrame(),
    isConvolverCreateInFlight: () => convolverCreateInFlight,
    setConvolverCreateInFlight: (active) => { convolverCreateInFlight = active; },
    getTimingSafetyLimitMs: () => MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS,
});
window.FXRouteMeasurementCapture?.init({
    getState: () => state,
    getElements: () => elements,
    fetch: (...args) => fetch(...args),
    showToast: (message, kind) => showToast(message, kind),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    requireConcreteFilterBank: () => window.FXRouteBankUI.requireConcreteFilterBank(),
    measurementModeReady: () => measurementModeReady(),
    measurementRepeatBlockedReason: () => measurementRepeatBlockedReason(),
    flushSubwooferSettingsBeforeMeasurement: () => flushSubwooferSettingsBeforeMeasurement(),
    measurementAreaFromCatalog: () => measurementAreaFromCatalog(),
    appendMeasurementReferenceFields: (formData) => window.FXRouteMeasurementSetup.appendMeasurementReferenceFields(formData),
    postRuntimeDebugSnapshot: (label, extra) => postRuntimeDebugSnapshot(label, extra),
    formatTransitionErrorDetail: (detail, fallback) => formatTransitionErrorDetail(detail, fallback),
    normalizeMeasurementKind: (kind) => MeasurementUI.normalizeMeasurementKind(kind),
    formatMeasurementJobStatusText: (job, fallback) => window.FXRouteMeasurementJob.formatMeasurementJobStatusText(job, fallback),
    pollMeasurementJob: (jobId, generation) => window.FXRouteMeasurementJob.pollMeasurementJob(jobId, generation),
    cancelMeasurement: () => window.FXRouteMeasurementJob.cancelMeasurement(),
});
window.FXRouteMeasurementJob?.init({
    getState: () => state,
    getElements: () => elements,
    fetch: (...args) => fetch(...args),
    showToast: (message, kind) => showToast(message, kind),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    normalizeMeasurementKind: (kind) => MeasurementUI.normalizeMeasurementKind(kind),
    formatMeasurementInputLevelText: (level) => MeasurementUI.formatMeasurementInputLevelText(level),
    getMeasurementJobStatus: (job) => MeasurementUI.getMeasurementJobStatus(job),
    getMeasurementJobResultMeasurement: (job) => MeasurementUI.getMeasurementJobResultMeasurement(job),
    getMeasurementTimingInfo: (measurement) => MeasurementUI.getMeasurementTimingInfo(measurement),
    normalizeMeasurementEntry: (measurement, index) => MeasurementUI.normalizeMeasurementEntry(measurement, index),
    measurementModeReady: () => measurementModeReady(),
    measurementRepeatBlockedReason: () => measurementRepeatBlockedReason(),
    syncMeasurementRepeatNote: (lrActive, reason) => syncMeasurementRepeatNote(lrActive, reason),
    setMeasurementSweepMenuOpen: (open) => setMeasurementSweepMenuOpen(open),
    syncAutoSubButton: () => MeasurementFlows.syncAutoSubButton(),
    syncSpeakerAlignButton: () => MeasurementFlows.syncSpeakerAlignButton(),
    cancelHybridWizardMeasurement: () => MeasurementFlows.cancelHybridWizardMeasurement(),
    cancelAutoSubOptimize: () => MeasurementFlows.cancelAutoSubOptimize(),
    cancelSpeakerAlign: () => MeasurementFlows.cancelSpeakerAlign(),
    postRuntimeDebugSnapshot: (label, extra) => postRuntimeDebugSnapshot(label, extra),
    formatTransitionErrorDetail: (detail, fallback) => formatTransitionErrorDetail(detail, fallback),
    sleep: (ms) => sleep(ms),
});
window.FXRouteMeasurementSavedActions?.init({
    getState: () => state,
    fetch: (...args) => fetch(...args),
    showToast: (message, kind) => showToast(message, kind),
    renderMeasurementPanel: () => renderMeasurementPanel(),
    fetchMeasurements: () => fetchMeasurements(),
    formatTransitionErrorDetail: (detail, fallback) => formatTransitionErrorDetail(detail, fallback),
    normalizeMeasurementEntry: (measurement, index) => MeasurementUI.normalizeMeasurementEntry(measurement, index),
    getVisibleMeasurementEntries: () => getVisibleMeasurementEntries(),
    confirm: (message) => window.confirm(message),
    prompt: (message, defaultValue) => window.prompt(message, defaultValue),
});
window.FXRouteMeasurementSplCalibration?.init({
    getElements: () => elements,
    fetch: (...args) => fetch(...args),
    fetchEffects: () => EffectsUI.fetchEffects(),
    openModal: (panel, options) => window.FXRouteModal?.open(panel, options),
    closeModal: (panel) => window.FXRouteModal?.close(panel),
});
// Leaf modules (api.js, ui_helpers.js, modal.js) own the canonical
// implementations below; app.js keeps thin delegating wrappers so existing
// call sites stay unchanged.
const MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS = MeasurementUI.MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS;
const MEASUREMENT_JOB_CANCELLED_STATES = MeasurementUI.MEASUREMENT_JOB_CANCELLED_STATES;
const MEASUREMENT_JOB_FAILED_STATES = MeasurementUI.MEASUREMENT_JOB_FAILED_STATES;
const MEASUREMENT_JOB_SUCCESS_STATES = MeasurementUI.MEASUREMENT_JOB_SUCCESS_STATES;
const measurementComparePalette = MeasurementUI.measurementComparePalette;
const measurementCurrentColor = MeasurementUI.measurementCurrentColor;
const measurementPeqPalette = MeasurementUI.measurementPeqPalette;
const measurementPeqTypeLabels = MeasurementUI.measurementPeqTypeLabels;
const measurementConvolverPhaseModes = MeasurementUI.measurementConvolverPhaseModes;
const measurementConvolverAlignedPhaseModes = MeasurementUI.measurementConvolverAlignedPhaseModes;
const measurementConvolverCurves = MeasurementUI.measurementConvolverCurves;
const measurementConvolverTapOptions = MeasurementUI.measurementConvolverTapOptions;
let radioModule = null;
// Grid/list layout persistence (Library albums + TIDAL tile surfaces), read
// before state init. Same localStorage mechanism as fx-debug-footer; grid is
// the default when the key is absent or storage is unavailable.
const VIEW_MODE_STORAGE_KEY = 'fx-view-mode-';
function readStoredViewMode(surface) {
    try {
        return localStorage.getItem(VIEW_MODE_STORAGE_KEY + surface) === 'list' ? 'list' : 'grid';
    } catch (e) {
        // Storage may be unavailable (private mode); grid stays the default.
        return 'grid';
    }
}
// State
let state = {
    playback: {
        state: 'stopped',
        current_track: null,
        current_file: null,
        position: 0,
        duration: 0,
        volume: 100,
        ended: false,
        error: null,
        live_title: null,
        radio_metadata: null,
        output_peak_warning: {
            available: false,
            detected: false,
            hold_ms: 0,
            threshold: 1.0,
            vu_db: null,
            vu_db_l: null,
            vu_db_r: null,
            detected_l: false,
            detected_r: false,
            hold_ms_l: 0,
            hold_ms_r: 0,
            last_over_at_l: null,
            last_over_at_r: null,
            vu_fresh: false,
            vu_age_ms: null,
            target: null,
            last_over_at: null,
            last_error: null,
        },
    },
    library: {
        tracks: [],
        scanning: false,
        scanStatus: null,
        viewMode: 'albums',
        currentFolder: '',
        selectedTrackIds: [],
        searchQuery: '',
        shuffle: false,
        loop: false,
        selectionDownloadPending: false,
        albums: [],
        albumsLoaded: false,
        albumLayout: readStoredViewMode('library-albums'),
        showFavoriteAlbums: false,
        albumDetail: null,
        playlistDetail: null,
    },
    playlists: [],
    stations: [],
    download: null,
    outputSystem: {
        catalog: null,
        busy: false,
    },
    crossover: {
        activeWay: null,
        response: null,
        busy: false,
        linkLR: false,
    },
    dsp: {
        available: false,
        preset_count: 0,
        active_preset: null,
        presets: [],
        irs: [],
        combineDraft: {
            preset1: '',
            preset2: '',
            preset3: '',
            presetName: '',
        },
        peqDraft: {
            presetName: '',
            loadAfterCreate: false,
            leftBands: [],
            rightBands: [],
        },
    },
    measurement: {
        open: false,
        loading: false,
        inputsLoading: false,
        startInFlight: false,
        saveInFlight: false,
        measurements: [],
        currentMeasurement: null,
        pendingRepeatMeasurements: [],
        currentMeasurementSaved: false,
        currentMeasurementName: '',
        inputs: [],
        selectedInputId: '',
        selectedInputLegacyId: '',
        selectedInputKey: '',
        selectedInputConfigured: false,
        selectedInputUnavailable: false,
        selectedMicInputChannel: '1',
        selectedReferenceInputChannel: '',
        selectedReferenceInputChannelLeft: '',
        selectedReferenceInputChannelRight: '',
        sweepSide: 'stereo',
        cancelRequested: false,
        repeatJobActive: false,
        displaySmoothing: '1/6-oct',
        measurementView: 'freq',
        hostCaptureAvailable: false,
        modeNote: '',
        calibrationFilename: '',
        calibrationOptions: [],
        selectedCalibrationRef: '',
        calibrationUpdating: false,
        calibrationDeleting: false,
        calibrationExporting: false,
        autoSubCancelRequested: false,
        houseCurveFilename: '',
        houseCurveOptions: [],
        houseCurveUpdating: false,
        houseCurveDeleting: false,
        houseCurveExporting: false,
        activeEditor: 'none',
        customHouseCurve: {
            open: false,
            displayTarget: 'actual',
            points: [],
            activePointId: null,
            dragPointId: null,
            name: '',
            nameTouched: false,
            saving: false,
        },
        visibilityById: {},
        reviewVisibilityById: {},
        savedGroupOpen: false,
        setupOpen: false,
        storage: null,
        captureAvailable: false,
        activeJobId: '',
        jobGeneration: 0,
        activeMeasurementKind: '',
        autoSubJobId: '',
        autoSubProgress: null,
        autoSubInFlight: false,
        autoSubResult: null,
        autoSubMeasurements: [],
        speakerAlignJobId: '',
        speakerAlignInFlight: false,
        speakerAlignCancelRequested: false,
        speakerAlignResult: null,
        speakerAlignResults: null,
        statusText: 'Sweep ready. Calibration file is optional.',
        measurementSampleRate: '48000',
        assistMode: 'peq',
        convolverAssistant: {
            targetCurve: 'neutral',
            rangeStartHz: 20,
            rangeEndHz: 250,
            maxBoostDb: 6,
            maxCutDb: -9,
            dipGuard: 'off',
            safetyMarginDb: 1,
            autoGainEnabled: true,
            quality: 'linear_8192',
            phaseMode: 'minimum',
            irLength: '8192',
            dragMode: null,
            draft: { left: null, right: null, presetName: '', nameTouched: false, notice: '' },
        },
        hybridWizard: {
            open: false,
            running: false,
            jobId: '',
            stepIndex: 0,
            mode: 'stereo',
            sequence: [],
            captures: [],
            status: '',
            phase: '',
            cancelRequested: false,
            quality: null,
            profile: null,
        },
        peqAssistant: {
            enabled: false,
            filters: [],
            activeFilterId: null,
            dragFilterId: null,
            draft: { leftBands: [], rightBands: [], presetName: '', nameTouched: false },
        },
    },
    samplerate: {
        available: false,
        active_rate: null,
        mode: null,
        force_rate: null,
        policy: { mode: 'auto', rate: null },
        pending: false,
    },
    settings: {
        audioOutputs: {
            loaded: false,
            available: false,
            default_output: null,
            selected_output: null,
            current_output: null,
            outputs: [],
            notes: [],
            pendingSelectionKey: null,
            output_mode: {
                mode: 'stereo',
                available: false,
                required_channels: 4,
                effective_output_channels: null,
                routing: {
                    main_pair: [1, 2],
                    sub_pair: [3, 4],
                    status: 'Out 1/2 Main · Out 3/4 Sub',
                },
                subwoofer: {
                    crossover_frequency_hz: 80,
                    slope: 'LR24',
                    main_highpass_enabled: true,
                    sub_level_db: 0,
                    sub_alignment_ms: 0,
                    sub_polarity: 'normal',
                },
                subwoofers: {
                    sub1: { level_db: 0, alignment_ms: 0, polarity: 'normal' },
                    sub2: { level_db: 0, alignment_ms: 0, polarity: 'normal' },
                },
            },
        },
        sourceMode: {
            mode: 'app-playback',
            modes: [],
            default_input: null,
            selected_input: null,
            current_input: null,
            inputs: [],
            bluetooth: {},
            notes: [],
            pending: false,
        },
        musicLibrary: {
            active_id: 'local',
            active_type: 'local',
            libraries: [],
            pending: false,
            loading: false,
        },
        providers: {
            loaded: false,
            list: [],
            pendingOperation: null,
            operationLog: '',
        },
        deviceName: {
            loaded: false,
            value: '',
            canChange: false,
            pending: false,
        },
        hardware: {
            available: true,
            connected: false,
            device: null,
            status: {},
            raw: null,
            input: null,
            power: null,
            trigger: null,
            auto: null,
            notes: [],
            pending: false,
        },
        maintenance: {
            installedVersion: '',
            latestSummary: 'Version status not checked yet.',
            detail: 'Use the existing installer path for safe GitHub updates.',
            currentVersion: '',
            latestVersion: '',
            updateAvailable: null,
            log: '',
            pending: false,
            restartPending: false,
            operation: '',
            detailsExpanded: false,
            userCollapsedDetails: false,
            hasError: false,
        },
    },
    wsConnected: false,
    powerCapabilities: null,
};
// WebSocket
let ws = null;
let reconnectAttempts = 0;
let reconnectTimer = null;
let offlineIndicatorTimer = null;
let wsConnectSerial = 0;
let wsReconnectSyncGeneration = 0;
let playbackActionInFlight = false;
let pendingPlaybackRequestId = 0;
let nowPlayingCueTimer = null;
let nowPlayingCueCoverAbort = null;
let pendingFooterSingleTrackStart = null;
let pendingOptimisticTrack = null;
let lastRadioTrack = null;
let pauseActionRequestId = 0;
const FOOTER_SINGLE_TRACK_START_LOCK_MS = 5000;
let volumeTimer = null;
let volumeDisplayTimer = null;
let volumeRequestInFlight = false;
let pendingVolume = null;
let volumeGestureActive = false;
let trackFavoriteRequestInFlight = false;
let tidalFavoriteRequestInFlight = false;
let peqCreateInFlight = false;
let convolverCreateInFlight = false;
let optimisticVolume = null;
let lastConfirmedVolume = state.playback.volume;
let volumeSyncGraceUntil = 0;
let downloadStatusPollTimer = null;
let lastDownloadStatus = null;
let libraryModeSyncArmed = false;
let lastLibraryPlaybackContextSignature = null;
let libraryModeRequestInFlight = false;
let settingsStatusPollTimer = null;
let settingsOutputScanOnFocusDone = false;
let measurementInputScanOnFocusDone = false;
let measurementGraphResizeObserver = null;
let playbackFooterResizeObserver = null;
let playbackFooterSpaceFrame = null;
let measurementGraphPointerId = null;
let measurementWindowHeartbeatTimer = null;
// Seek - globals
let seekDragging = false;
let seekPendingPos = null;
let playbackPositionPollTimer = null;
const VOLUME_SEND_DEBOUNCE_MS = 120;
const VOLUME_SYNC_GRACE_MS = 700;
const VOLUME_CURVE_GAMMA = 1.0;
const MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX = 14;
const MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX = 24;
const MEASUREMENT_PEQ_TOUCH_CREATE_COOLDOWN_MS = 350;
const SPOTIFY_POLL_INTERVAL_MS = 1000;
const SAMPLERATE_POLL_INTERVAL_MS = 5000;
const SAMPLERATE_BURST_POLL_DELAYS_MS = [0, 120, 280, 520, 900, 1400, 2200, 3200];
const LIBRARY_SCAN_POLL_INTERVAL_MS = 1200;
const DOWNLOAD_STATUS_POLL_INTERVAL_MS = 1500;
const PEAK_STATUS_POLL_INTERVAL_MS = 1200;
const MEASUREMENT_WINDOW_HEARTBEAT_INTERVAL_MS = 10000;
// DOM elements
const elements = {
    offlineIndicator: document.getElementById('offline-indicator'),
    liveBanner: document.getElementById('live-banner'),
    settingsOpenBtn: document.getElementById('open-settings'),
    settingsPanel: document.getElementById('settings-panel'),
    settingsCloseBtn: document.getElementById('close-settings'),
    settingsOutputSummary: document.getElementById('settings-output-summary'),
    settingsOutputSelect: document.getElementById('settings-output-select'),
    settingsOutputModeSelect: document.getElementById('settings-output-mode-select'),
    settingsModeGroup: document.getElementById('settings-mode-group'),
    settingsOutputModeHint: document.getElementById('settings-output-mode-hint'),
    settingsSamplerateSelect: document.getElementById('settings-samplerate-select'),
    settingsSamplerateHint: document.getElementById('settings-samplerate-hint'),
    settingsRoutingGroup: document.getElementById('settings-routing-group'),
    settingsRoutingGrid: document.getElementById('settings-routing-grid'),
    settingsRoutingHint: document.getElementById('settings-routing-hint'),
    settingsCrossoverSelect: document.getElementById('settings-crossover-select'),
    settingsCrossoverGroup: document.getElementById('settings-crossover-group'),
    osTopology: document.getElementById('os-topology'),
    osFeedback: document.getElementById('os-feedback'),
    effectsBankSelect: document.getElementById('effects-bank-select'),
    effectsCrossoverCard: document.getElementById('effects-crossover-card'),
    effectsCrossoverSummary: document.getElementById('effects-crossover-summary'),
    effectsCrossoverTabs: document.getElementById('effects-crossover-tabs'),
    effectsCrossoverGraph: document.getElementById('effects-crossover-graph'),
    effectsCrossoverFrequencyHighpass: document.getElementById('effects-crossover-frequency-highpass'),
    effectsCrossoverFrequencyLowpass: document.getElementById('effects-crossover-frequency-lowpass'),
    effectsCrossoverHighpassGroup: document.getElementById('effects-crossover-highpass-group'),
    effectsCrossoverLowpassGroup: document.getElementById('effects-crossover-lowpass-group'),
    effectsCrossoverFrequencyHighpassGroup: document.getElementById('effects-crossover-frequency-highpass-group'),
    effectsCrossoverSlopeHighpassGroup: document.getElementById('effects-crossover-slope-highpass-group'),
    effectsCrossoverFrequencyLowpassGroup: document.getElementById('effects-crossover-frequency-lowpass-group'),
    effectsCrossoverSlopeLowpassGroup: document.getElementById('effects-crossover-slope-lowpass-group'),
    effectsCrossoverTrimGroup: document.getElementById('effects-crossover-trim-group'),
    effectsCrossoverFamilyHighpass: document.getElementById('effects-crossover-family-highpass'),
    effectsCrossoverSlopeHighpass: document.getElementById('effects-crossover-slope-highpass'),
    effectsCrossoverFamilyLowpass: document.getElementById('effects-crossover-family-lowpass'),
    effectsCrossoverSlopeLowpass: document.getElementById('effects-crossover-slope-lowpass'),
    effectsCrossoverLevel: document.getElementById('effects-crossover-level'),
    effectsCrossoverDelay: document.getElementById('effects-crossover-delay'),
    effectsCrossoverPolarity: document.getElementById('effects-crossover-polarity'),
    effectsCrossoverLink: document.getElementById('effects-crossover-link'),
    settingsSourceSelect: document.getElementById('settings-source-select'),
    settingsSourceModeHint: document.getElementById('settings-source-mode-hint'),
    settingsBluetoothStatus: document.getElementById('settings-bluetooth-status'),
    settingsMusicLibrarySelect: document.getElementById('settings-music-library-select'),
    settingsMusicLibraryHint: document.getElementById('settings-music-library-hint'),
    settingsProvidersList: document.getElementById('settings-providers-list'),
    settingsProvidersSummary: document.getElementById('settings-providers-summary'),
    settingsProviderOperation: document.getElementById('settings-provider-operation'),
    settingsProviderOperationLog: document.getElementById('settings-provider-operation-log'),
    settingsDeviceNameInput: document.getElementById('settings-device-name-input'),
    settingsDeviceNameApply: document.getElementById('settings-device-name-apply'),
    settingsDeviceNameHint: document.getElementById('settings-device-name-hint'),
    settingsHardwareSummary: document.getElementById('settings-hardware-summary'),
    settingsHardwareDetail: document.getElementById('settings-hardware-detail'),
    settingsHardwareRcaBtn: document.getElementById('settings-hardware-rca'),
    settingsHardwareXlrBtn: document.getElementById('settings-hardware-xlr'),
    settingsHardwarePressBtn: document.getElementById('settings-hardware-press'),
    settingsHardwareAutoOnBtn: document.getElementById('settings-hardware-auto-on'),
    settingsHardwareAutoOffBtn: document.getElementById('settings-hardware-auto-off'),
    settingsCertificateLink: document.getElementById('settings-certificate-link'),
    qobuzLoginPanel: document.getElementById('qobuz-login-panel'),
    qobuzLoginUrl: document.getElementById('qobuz-login-url'),
    qobuzLoginOpen: document.getElementById('qobuz-login-open'),
    qobuzLoginCopy: document.getElementById('qobuz-login-copy'),
    qobuzLoginRedirect: document.getElementById('qobuz-login-redirect'),
    qobuzLoginStatus: document.getElementById('qobuz-login-status'),
    qobuzLoginFinishBtn: document.getElementById('qobuz-login-finish'),
    qobuzLoginCancelBtn: document.getElementById('qobuz-login-cancel'),
    qobuzLoginCloseBtn: document.getElementById('qobuz-login-close'),
    tidalLoginPanel: document.getElementById('tidal-login-panel'),
    tidalLoginUrl: document.getElementById('tidal-login-url'),
    tidalLoginOpen: document.getElementById('tidal-login-open'),
    tidalLoginCopy: document.getElementById('tidal-login-copy'),
    tidalLoginRedirect: document.getElementById('tidal-login-redirect'),
    tidalLoginStatus: document.getElementById('tidal-login-status'),
    tidalLoginFinishBtn: document.getElementById('tidal-login-finish'),
    tidalLoginCancelBtn: document.getElementById('tidal-login-cancel'),
    tidalLoginCloseBtn: document.getElementById('tidal-login-close'),
    settingsMaintenanceStatus: document.getElementById('settings-maintenance-status'),
    settingsMaintenanceCurrent: document.getElementById('settings-maintenance-current'),
    settingsMaintenanceLatestRow: document.getElementById('settings-maintenance-latest-row'),
    settingsMaintenanceLatest: document.getElementById('settings-maintenance-latest'),
    settingsMaintenanceDetail: document.getElementById('settings-maintenance-detail'),
    settingsUpdateCheckBtn: document.getElementById('settings-update-check'),
    settingsUpdateRunBtn: document.getElementById('settings-update-run'),
    settingsRestoreSection: document.getElementById('settings-restore-section'),
    settingsRestoreRunBtn: document.getElementById('settings-restore-run'),
    settingsUpdateDetailsToggle: document.getElementById('settings-update-details-toggle'),
    settingsUpdateDetails: document.getElementById('settings-update-details'),
    settingsUpdateLog: document.getElementById('settings-update-log'),
    tabs: document.querySelectorAll('.tab-btn'),
    tabPanels: document.querySelectorAll('.tab-panel'),
    toggleImportBtn: document.getElementById('toggle-import'),
    libraryImportPanel: document.getElementById('library-import-panel'),
    refreshLibraryBtn: document.getElementById('refresh-library'),
    libraryViewTracksBtn: document.getElementById('library-view-tracks'),
    libraryViewFoldersBtn: document.getElementById('library-view-folders'),
    libraryViewFavoritesBtn: document.getElementById('library-view-favorites'),
    libraryViewAlbumsBtn: document.getElementById('library-view-albums'),
    libraryFolderPath: document.getElementById('library-folder-path'),
    librarySearchInput: document.getElementById('library-search'),
    librarySearchClear: document.getElementById('library-search-clear'),
    albumsGrid: document.getElementById('albums-grid'),
    libraryViewModeToggle: document.getElementById('library-view-mode-toggle'),
    libraryViewModeGridBtn: document.getElementById('library-view-mode-grid'),
    libraryViewModeListBtn: document.getElementById('library-view-mode-list'),
    albumDetail: document.getElementById('album-detail'),
    albumDetailBack: document.getElementById('album-detail-back'),
    albumDetailCover: document.getElementById('album-detail-cover'),
    albumDetailBackdrop: document.getElementById('album-detail-backdrop'),
    albumDetailName: document.getElementById('album-detail-name'),
    albumDetailArtist: document.getElementById('album-detail-artist'),
    albumDetailCount: document.getElementById('album-detail-count'),
    albumFavoriteToggle: document.getElementById('album-favorite-toggle'),
    albumDetailTracks: document.getElementById('album-detail-tracks'),
    albumDiscover: document.getElementById('album-discover'),
    playlistDetail: document.getElementById('playlist-detail'),
    playlistDetailBack: document.getElementById('playlist-detail-back'),
    playlistDetailCover: document.getElementById('playlist-detail-cover'),
    playlistDetailBackdrop: document.getElementById('playlist-detail-backdrop'),
    playlistDetailName: document.getElementById('playlist-detail-name'),
    playlistDetailCount: document.getElementById('playlist-detail-count'),
    playlistDetailInfo: document.getElementById('playlist-detail-info'),
    playlistDetailTracks: document.getElementById('playlist-detail-tracks'),
    deletePlaylistBtn: document.getElementById('delete-playlist'),
    selectAllTracksBtn: document.getElementById('select-all-tracks'),
    playlistName: document.getElementById('playlist-name'),
    savePlaylistBtn: document.getElementById('save-playlist'),
    cancelPlaylistSelectionBtn: document.getElementById('cancel-playlist-selection'),
    playlistSaveRow: document.getElementById('playlist-save-row'),
    playlistSaveControls: document.querySelector('.playlist-save-controls'),
    libraryInfo: document.getElementById('library-info'),
    downloadSelectedTracksBtn: document.getElementById('download-selected-tracks'),
    deleteSelectedTracksBtn: document.getElementById('delete-selected-tracks'),
    tracksList: document.getElementById('tracks-list'),
    downloadUrlDropArea: document.getElementById('download-url-drop-area'),
    downloadUrlHint: document.getElementById('download-url-detail'),
    downloadUrl: document.getElementById('download-url'),
    cancelDownloadBtn: document.getElementById('cancel-download'),
    uploadTrackFile: document.getElementById('upload-track-file'),
    downloadStatus: document.getElementById('download-status'),
    effectsInfo: document.getElementById('effects-info'),
    // elements.effectsPresetStatus removed — preset status is now shown in the compare row
    effectsDeleteBtn: document.getElementById('effects-delete'),
    effectsCompareA: document.getElementById('effects-compare-a'),
    effectsCompareB: document.getElementById('effects-compare-b'),
    effectsCompareToggle: document.getElementById('effects-compare-toggle'),
    effectsMeasureOpenBtn: document.getElementById('effects-measure-open'),
    measurementPanel: document.getElementById('measurement-panel'),
    measurementCloseBtn: document.getElementById('measurement-close'),
    measurementSetupCard: document.getElementById('measurement-setup-card'),
    measurementMain: document.getElementById('measurement-main'),
    measurementSetupBackBtn: document.getElementById('measurement-setup-back'),
    measurementSetupToggleBtn: document.getElementById('measurement-setup-toggle'),
    measurementModeNote: document.getElementById('measurement-mode-note'),
    measurementInputGroup: document.getElementById('measurement-input-group'),
    measurementInputSelect: document.getElementById('measurement-input-select'),
    measurementInputRefreshBtn: document.getElementById('measurement-input-refresh'),
    measurementInputChannelGrid: document.getElementById('measurement-input-channel-grid'),
    measurementMicInputChannelSelect: document.getElementById('measurement-mic-input-channel-select'),
    measurementReferenceSingleGroup: document.getElementById('measurement-reference-single'),
    measurementReferenceInputChannelSelect: document.getElementById('measurement-reference-input-channel-select'),
    measurementReferenceLeftGroup: document.getElementById('measurement-reference-left'),
    measurementReferenceRightGroup: document.getElementById('measurement-reference-right'),
    measurementReferenceInputChannelLeftSelect: document.getElementById('measurement-reference-input-channel-left-select'),
    measurementReferenceInputChannelRightSelect: document.getElementById('measurement-reference-input-channel-right-select'),
    measurementReferenceWarning: document.getElementById('measurement-reference-warning'),
    measurementCalibrationSelect: document.getElementById('measurement-calibration-select'),
    measurementCalibrationFile: document.getElementById('measurement-calibration-file'),
    measurementCalibrationExportBtn: document.getElementById('measurement-calibration-export'),
    measurementCalibrationDeleteBtn: document.getElementById('measurement-calibration-delete'),
    measurementCalibrationUploadName: document.getElementById('measurement-calibration-upload-name'),
    measurementCalibrationName: document.getElementById('measurement-calibration-name'),
    measurementHouseCurveSelect: document.getElementById('measurement-house-curve-select'),
    measurementHouseCurveFile: document.getElementById('measurement-house-curve-file'),
    measurementHouseCurveExportBtn: document.getElementById('measurement-house-curve-export'),
    measurementHouseCurveDeleteBtn: document.getElementById('measurement-house-curve-delete'),
    measurementHouseCurveUploadName: document.getElementById('measurement-house-curve-upload-name'),
    measurementHouseCurveName: document.getElementById('measurement-house-curve-name'),
    measurementCustomHouseCurvePanel: document.getElementById('measurement-custom-house-curve-panel'),
    measurementCustomHouseCurveChips: document.getElementById('measurement-custom-house-curve-chips'),
    measurementCustomHouseCurveEditor: document.getElementById('measurement-custom-house-curve-editor'),
    measurementCustomHouseCurveName: document.getElementById('measurement-custom-house-curve-name'),
    measurementCustomHouseCurveCreateBtn: document.getElementById('measurement-custom-house-curve-create'),
    measurementNameInput: document.getElementById('measurement-name'),
    measurementSweepToggleBtn: document.getElementById('measurement-sweep-toggle'),
    measurementSweepMenu: document.getElementById('measurement-sweep-menu'),
    measurementSweepStartBtn: document.getElementById('measurement-sweep-start'),
    measurementSweepSideRow: document.getElementById('measurement-sweep-side-row'),
    measurementAreaIndicator: document.getElementById('measurement-area-indicator'),
    measurementAreaNote: document.getElementById('measurement-area-note'),
    measurementRepeatStartBtn: document.getElementById('measurement-repeat-start'),
    measurementRepeatNote: document.getElementById('measurement-repeat-note'),
    measurementAutoSubStartBtn: document.getElementById('measurement-auto-sub-start'),
    measurementAutoSubGroup: document.getElementById('measurement-auto-sub-group'),
    measurementAutoSubStatus: document.getElementById('measurement-auto-sub-status'),
    measurementSpeakerAlignLeftBtn: document.getElementById('measurement-speaker-align-left'),
    measurementSpeakerAlignRightBtn: document.getElementById('measurement-speaker-align-right'),
    measurementSpeakerAlignCancelBtn: document.getElementById('measurement-speaker-align-cancel'),
    measurementSpeakerAlignGroup: document.getElementById('measurement-speaker-align-group'),
    measurementSpeakerAlignStatus: document.getElementById('measurement-speaker-align-status'),
    measurementSpeakerAlignResults: document.getElementById('measurement-speaker-align-results'),
    measurementSpeakerAlignSaveBtn: document.getElementById('measurement-speaker-align-save'),
    measurementSpeakerAlignSavedSelect: document.getElementById('measurement-speaker-align-saved'),
    measurementSpeakerAlignOpenBtn: document.getElementById('measurement-speaker-align-open'),
    measurementHybridOpenBtn: document.getElementById('measurement-hybrid-open'),
    measurementHybridPanel: document.getElementById('measurement-hybrid-panel'),
    measurementHybridCloseBtn: document.getElementById('measurement-hybrid-close'),
    measurementHybridHeaderActions: document.getElementById('measurement-hybrid-header-actions'),
    measurementHybridMode: document.getElementById('measurement-hybrid-mode'),
    measurementHybridProgress: document.getElementById('measurement-hybrid-progress'),
    measurementHybridTitle: document.getElementById('measurement-hybrid-step-title'),
    measurementHybridInstruction: document.getElementById('measurement-hybrid-instruction'),
    measurementHybridActive: document.getElementById('measurement-hybrid-active'),
    measurementHybridStatus: document.getElementById('measurement-hybrid-status'),
    measurementHybridPrimaryBtn: document.getElementById('measurement-hybrid-primary'),
    measurementHybridBackBtn: document.getElementById('measurement-hybrid-back'),
    measurementHybridSummary: document.getElementById('measurement-hybrid-summary'),
    measurementSaveBtn: document.getElementById('measurement-save'),
    measurementClearBtn: document.getElementById('measurement-clear'),
    measurementAssistMode: document.getElementById('measurement-assist-mode'),
    measurementTargetCurve: document.getElementById('measurement-target-curve'),
    measurementSetupStatus: document.getElementById('measurement-setup-status'),
    measurementSummary: document.getElementById('measurement-summary'),
    measurementGraphSubtitle: document.getElementById('measurement-graph-subtitle'),
    measurementGraphControls: document.getElementById('measurement-graph-controls'),
    measurementGraph: document.getElementById('measurement-graph'),
    measurementIrDiagnostics: document.getElementById('measurement-ir-diagnostics'),
    measurementEmpty: document.getElementById('measurement-empty'),
    measurementPeqPanel: document.getElementById('measurement-peq-panel'),
    measurementPeqChips: document.getElementById('measurement-peq-chips'),
    measurementPeqEditor: document.getElementById('measurement-peq-editor'),
    measurementPeqDraftSummary: document.getElementById('measurement-peq-draft-summary'),
    measurementPeqPresetName: document.getElementById('measurement-peq-preset-name'),
    measurementPeqTakeLeftBtn: document.getElementById('measurement-peq-take-left'),
    measurementPeqTakeRightBtn: document.getElementById('measurement-peq-take-right'),
    measurementPeqTakeBothBtn: document.getElementById('measurement-peq-take-both'),
    measurementPeqCreateBtn: document.getElementById('measurement-peq-create'),
    measurementPeqTakeFeedback: document.getElementById('measurement-peq-take-feedback'),
    measurementConvolverPanel: document.getElementById('measurement-convolver-panel'),
    measurementConvolverTarget: document.getElementById('measurement-convolver-target'),
    measurementConvolverRangeStart: document.getElementById('measurement-convolver-range-start'),
    measurementConvolverRangeEnd: document.getElementById('measurement-convolver-range-end'),
    measurementConvolverMaxBoost: document.getElementById('measurement-convolver-max-boost'),
    measurementConvolverMaxCut: document.getElementById('measurement-convolver-max-cut'),
    measurementConvolverDipGuard: document.getElementById('measurement-convolver-dip-guard'),
    measurementConvolverSampleRate: document.getElementById('measurement-convolver-sample-rate'),
    measurementConvolverPhaseMode: document.getElementById('measurement-convolver-phase-mode'),
    measurementConvolverIrLength: document.getElementById('measurement-convolver-ir-length'),
    measurementConvolverPresetName: document.getElementById('measurement-convolver-preset-name'),
    measurementConvolverSummary: document.getElementById('measurement-convolver-summary'),
    measurementConvolverWarnings: document.getElementById('measurement-convolver-warnings'),
    measurementConvolverTakeLeftBtn: document.getElementById('measurement-convolver-take-left'),
    measurementConvolverTakeRightBtn: document.getElementById('measurement-convolver-take-right'),
    measurementConvolverTakeBothBtn: document.getElementById('measurement-convolver-take-both'),
    measurementConvolverCreateBtn: document.getElementById('measurement-convolver-create'),
    measurementConvolverFeedback: document.getElementById('measurement-convolver-feedback'),
    measurementList: document.getElementById('measurement-list'),
    effectsCompareActive: document.getElementById('effects-compare-active'),
    effectsCompareChain: document.getElementById('effects-compare-chain'),
    effectsCompareRow: document.getElementById('effects-compare-row'),
    effectsToggleImportBtn: document.getElementById('effects-toggle-import'),
    effectsImportPanel: document.getElementById('effects-import-panel'),
    effectsImportFile: document.getElementById('effects-import-file'),
    effectsImportFilename: document.getElementById('effects-import-filename'),
    effectsLimiterEnabled: document.getElementById('effects-limiter-enabled'),
    effectsHeadroomEnabled: document.getElementById('effects-headroom-enabled'),
    effectsHeadroomGainDb: document.getElementById('effects-headroom-gain-db'),
    effectsHeadroomGainWrap: document.getElementById('effects-headroom-gain-wrap'),
    effectsAutogainEnabled: document.getElementById('effects-autogain-enabled'),
    effectsAutogainTargetDb: document.getElementById('effects-autogain-target-db'),
    effectsAutogainTargetWrap: document.getElementById('effects-autogain-target-wrap'),
    effectsLoudnessEnabled: document.getElementById('effects-loudness-enabled'),
    effectsLoudnessStrength: document.getElementById('effects-loudness-strength'),
    effectsLoudnessStrengthWrap: document.getElementById('effects-loudness-strength-wrap'),
    effectsLoudnessFftSize: document.getElementById('effects-loudness-fft-size'),
    effectsLoudnessFftWrap: document.getElementById('effects-loudness-fft-wrap'),
    effectsBassEnabled: document.getElementById('effects-bass-enabled'),
    effectsBassAmount: document.getElementById('effects-bass-amount'),
    effectsBassControlsWrap: document.getElementById('effects-bass-controls-wrap'),
    effectsToneEffectEnabled: document.getElementById('effects-tone-effect-enabled'),
    effectsToneEffectWrap: document.getElementById('effects-tone-effect-wrap'),
    effectsToneEffectMode: document.getElementById('effects-tone-effect-mode'),
    effectsExtrasFeedback: document.getElementById('effects-extras-feedback'),
    effectsSubwooferCard: document.querySelector('.effects-card-subwoofer'),
    effectsSubwooferRouting: document.getElementById('effects-subwoofer-routing'),
    effectsSubwooferModeBadge: document.getElementById('effects-subwoofer-mode-badge'),
    effectsSubwooferPreview: document.getElementById('effects-subwoofer-preview'),
    effectsSubwooferGlobalLabel: document.getElementById('effects-subwoofer-global-label'),
    effectsSubwooferTabRow: document.getElementById('effects-subwoofer-tabrow'),
    effectsSubwooferSideTabs: document.getElementById('effects-subwoofer-side-tabs'),
    effectsSubwooferTabLeft: document.getElementById('effects-subwoofer-tab-left'),
    effectsSubwooferTabRight: document.getElementById('effects-subwoofer-tab-right'),
    effectsSubwooferTabBoth: document.getElementById('effects-subwoofer-tab-both'),
    effectsSubwooferSharedCrossover: document.getElementById('effects-subwoofer-shared-crossover'),
    effectsSubwooferLeftCrossover: document.getElementById('effects-subwoofer-left-crossover'),
    effectsSubwooferRightCrossover: document.getElementById('effects-subwoofer-right-crossover'),
    effectsSubwooferLinkWrap: document.getElementById('effects-subwoofer-link-wrap'),
    effectsSubwooferLink: document.getElementById('effects-subwoofer-link'),
    effectsSubwooferFrequencyNumber: document.getElementById('effects-subwoofer-frequency-number'),
    effectsSubwooferFamily: document.getElementById('effects-subwoofer-family'),
    effectsSubwooferSlope: document.getElementById('effects-subwoofer-slope'),
    effectsSubwooferMainHighpass: document.getElementById('effects-subwoofer-main-highpass'),
    effectsSubwooferLeftFrequency: document.getElementById('effects-subwoofer-left-frequency'),
    effectsSubwooferLeftFamily: document.getElementById('effects-subwoofer-left-family'),
    effectsSubwooferLeftSlope: document.getElementById('effects-subwoofer-left-slope'),
    effectsSubwooferLeftMainHighpass: document.getElementById('effects-subwoofer-left-main-highpass'),
    effectsSubwooferRightFrequency: document.getElementById('effects-subwoofer-right-frequency'),
    effectsSubwooferRightFamily: document.getElementById('effects-subwoofer-right-family'),
    effectsSubwooferRightSlope: document.getElementById('effects-subwoofer-right-slope'),
    effectsSubwooferRightMainHighpass: document.getElementById('effects-subwoofer-right-main-highpass'),
    effectsSubwooferLevelLabel: document.getElementById('effects-subwoofer-level-label'),
    effectsSubwooferLevel: document.getElementById('effects-subwoofer-level'),
    effectsSubwooferDelayLabel: document.getElementById('effects-subwoofer-delay-label'),
    effectsSubwooferDelay: document.getElementById('effects-subwoofer-delay'),
    effectsSubwooferPolarityLabel: document.getElementById('effects-subwoofer-polarity-label'),
    effectsSubwooferPolarity: document.getElementById('effects-subwoofer-polarity'),
    effectsSubwooferSub1GroupLabel: document.getElementById('effects-subwoofer-sub1-group-label'),
    effectsSubwooferSub2Fields: Array.from(document.querySelectorAll('.effects-subwoofer-sub2-field')),
    effectsSubwooferSub2GroupLabel: document.querySelector('.effects-subwoofer-sub2-group .effects-subwoofer-group-label'),
    effectsSubwooferSub2LevelLabel: document.querySelector('label[for="effects-subwoofer-sub2-level"]'),
    effectsSubwooferSub2DelayLabel: document.querySelector('label[for="effects-subwoofer-sub2-delay"]'),
    effectsSubwooferSub2PolarityLabel: document.querySelector('label[for="effects-subwoofer-sub2-polarity"]'),
    effectsSubwooferSub2Level: document.getElementById('effects-subwoofer-sub2-level'),
    effectsSubwooferSub2Delay: document.getElementById('effects-subwoofer-sub2-delay'),
    effectsSubwooferSub2Polarity: document.getElementById('effects-subwoofer-sub2-polarity'),
    effectsSubwooferDerivedDelays: document.getElementById('effects-subwoofer-derived-delays'),
    effectsSubwooferDdMain: document.getElementById('effects-subwoofer-dd-main'),
    effectsSubwooferDdSub1: document.getElementById('effects-subwoofer-dd-sub1'),
    effectsSubwooferDdSub2: document.getElementById('effects-subwoofer-dd-sub2'),
    effectsSubwooferFeedback: document.getElementById('effects-subwoofer-feedback'),
    effectsRewDualPresetName: document.getElementById('effects-rew-dual-preset-name'),
    effectsCombinePreset1: document.getElementById('effects-combine-preset-1'),
    effectsCombinePreset2: document.getElementById('effects-combine-preset-2'),
    effectsCombinePreset3: document.getElementById('effects-combine-preset-3'),
    effectsCombinePresetName: document.getElementById('effects-combine-preset-name'),
    effectsCombineSaveBtn: document.getElementById('effects-combine-save'),
    effectsRewLeftFile: document.getElementById('effects-rew-left-file'),
    effectsRewRightFile: document.getElementById('effects-rew-right-file'),
    effectsRewLeftText: document.getElementById('effects-rew-left-text'),
    effectsRewRightText: document.getElementById('effects-rew-right-text'),
    effectsRewDualCreatePresetBtn: document.getElementById('effects-rew-dual-create-preset'),
    effectsPeqDisclosure: document.getElementById('effects-peq-disclosure'),
    effectsPeqDisclosureMeta: document.querySelector('#effects-peq-disclosure .effects-disclosure-meta'),
    effectsPeqSummary: document.getElementById('effects-peq-summary'),
    effectsPeqPresetName: document.getElementById('effects-peq-preset-name'),
    effectsPeqModeSelect: document.getElementById('effects-peq-mode-select'),
    effectsPeqAddBandBtn: document.getElementById('effects-peq-add-band'),
    effectsPeqLeftBands: document.getElementById('effects-peq-left-bands'),
    effectsPeqRightBands: document.getElementById('effects-peq-right-bands'),
    effectsPeqCreatePresetBtn: document.getElementById('effects-peq-create-preset'),
    effectsStatus: document.getElementById('effects-status'),
    playbackBar: document.getElementById('playback-bar'),
    playbackCover: document.getElementById('playback-cover'),
    coverDetailBackdrop: document.getElementById('cover-detail-backdrop'),
    coverDetailCard: document.getElementById('cover-detail-card'),
    coverDetailCover: document.getElementById('cover-detail-cover'),
    coverDetailSource: document.getElementById('cover-detail-source'),
    coverDetailTitle: document.getElementById('cover-detail-title'),
    coverDetailArtist: document.getElementById('cover-detail-artist'),
    coverDetailAlbum: document.getElementById('cover-detail-album'),
    coverDetailTech: document.getElementById('cover-detail-tech'),
    coverDetailHistory: document.getElementById('cover-detail-history'),
    coverDetailHistoryList: document.getElementById('cover-detail-history-list'),
    coverDetailQueue: document.getElementById('cover-detail-queue'),
    coverDetailQueueList: document.getElementById('cover-detail-queue-list'),
    coverDetailExtra: document.getElementById('cover-detail-extra'),
    coverDetailExtraLine1: document.getElementById('cover-detail-extra-line1'),
    coverDetailExtraLine2: document.getElementById('cover-detail-extra-line2'),
    trackTitle: document.getElementById('track-title'),
    trackArtist: document.getElementById('track-artist'),
    trackFavoriteBtn: document.getElementById('track-favorite-btn'),
    playbackMeter: document.getElementById('playback-meter'),
    meterLeft: document.getElementById('meter-l'),
    meterRight: document.getElementById('meter-r'),
    footerShuffleBtn: document.getElementById('footer-shuffle'),
    btnPrevious: document.getElementById('btn-previous'),
    btnPlayPause: document.getElementById('btn-play-pause'),
    btnNext: document.getElementById('btn-next'),
    footerLoopBtn: document.getElementById('footer-loop'),
    btnClearQueue: document.getElementById('btn-clear-queue'),
    transportControls: document.querySelector('.playback-center .transport-controls'),
    sourceSwitcher: document.getElementById('source-switcher'),
    sourcePrev: document.getElementById('source-prev'),
    sourceSelect: document.getElementById('source-select'),
    sourceNext: document.getElementById('source-next'),
    queueStatus: document.getElementById('queue-status'),
    samplerateStatus: document.getElementById('samplerate-status'),
    outputLevelBadge: document.getElementById('output-level-badge'),
    seekSlider: document.getElementById('seek-slider'),
    seekRow: document.querySelector('.seek-row'),
    seekCurrent: document.getElementById('seek-current'),
    seekDuration: document.getElementById('seek-duration'),
    volumeSlider: document.getElementById('volume-slider'),
    volumeDisplay: document.getElementById('volume-display'),
    splCalibrationOpen: document.getElementById('measurement-spl-calibration-open'),
    splCalibrationPanel: document.getElementById('spl-calibration-panel'),
    splCalibrationClose: document.getElementById('spl-calibration-close'),
    splCalibrationNoise: document.getElementById('spl-calibration-noise'),
    splCalibrationMeasured: document.getElementById('spl-calibration-measured'),
    splCalibrationAutoStatus: document.getElementById('spl-calibration-auto-status'),
    splCalibrationSave: document.getElementById('spl-calibration-save'),
    splCalibrationStatus: document.getElementById('spl-calibration-status'),
    toastContainer: document.getElementById('toast-container'),
    powerMenuRoot: document.getElementById('power-menu-root'),
    powerMenuToggle: document.getElementById('power-menu-toggle'),
    powerMenu: document.getElementById('power-menu'),
    powerSuspend: document.getElementById('power-suspend'),
    powerShutdown: document.getElementById('power-shutdown'),
};

// Modal manager lives in static/modal.js (window.FXRouteModal).
// app.js uses the shared singleton directly; no local factory remains.

/* Shared compact content-state renderer (Library, Radio, TIDAL browse).
   One vocabulary for Loading / Empty / Error text slots so content areas
   never fall back to ad-hoc bare text. Exposed globally because radio.js
   and streaming.js load before app.js but only call it at runtime, after
   this module has defined it. */
// Shared content-state renderer lives in static/ui_helpers.js
// (window.FXRouteContentState). Thin wrapper keeps existing call sites unchanged.
function setContentState(el, state, message) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    if (mod && typeof mod.setContentState === 'function') return mod.setContentState(el, state, message);
    if (typeof window !== 'undefined' && window.FXRouteContentState) return window.FXRouteContentState.set(el, state, message);
}

// Initialization
document.addEventListener('DOMContentLoaded', () => {
    try { SettingsSystem.updatePowerButtonConnectionState(); } catch(e) { console.error('updatePowerButtonConnectionState crashed:', e); }
    try { SettingsSystem.setupPowerMenu(); } catch(e) { console.error('setupPowerMenu crashed:', e); }
    try { setupWebSocket(); } catch(e) { console.error('setupWebSocket crashed:', e); }
    try { setupTabNavigation(); } catch(e) { console.error('setupTabNavigation crashed:', e); }
    try {
        updateTabsScrollAffordance();
        window.addEventListener('resize', updateTabsScrollAffordance);
    } catch(e) { console.error('updateTabsScrollAffordance crashed:', e); }
    try { setupPlaybackControls(); } catch(e) { console.error('setupPlaybackControls crashed:', e); }
    try { initPlaybackFooterLayout(); } catch(e) { console.error('initPlaybackFooterLayout crashed:', e); }
    try { setupSettingsActions(); } catch(e) { console.error('setupSettingsActions crashed:', e); }
    try { initSeek(); } catch(e) { console.error('initSeek crashed:', e); }
    try {
        radioModule = window.FXRouteRadio.init({
            getStations: () => state.stations,
            setStations: stations => { state.stations = stations; },
            playStation: stationId => playRadio(stationId),
            showToast,
            escapeHtml,
            favoriteHeartSvg,
            highlightActiveTrack,
            extractDroppedUrl,
        });
    } catch(e) { console.error('radio module initialization crashed:', e); }
    try {
        window.FXRouteStreaming.init({
            showToast,
            maybeShowNativeTrackCue,
            maybeShowStreamingQueueCue,
            escapeHtml,
            favoriteHeartSvg,
            formatTime,
            artworkPlaceholderUrl,
            formatTransitionErrorDetail,
            trackRowHtml: detailTrackRowHtml,
            factsHtml: detailFactsHtml,
            aboutHtml: detailAboutHtml,
            spotifyCommand,
            spotifySeek,
            openQobuzLogin: () => void beginQobuzLogin(),
            openTidalLogin: () => void beginTidalLogin(),
        });
    } catch(e) { console.error('streaming module initialization crashed:', e); }
    try { setupLibraryActions(); } catch(e) { console.error('setupLibraryActions crashed:', e); }
    try { setupDownloadActions(); } catch(e) { console.error('setupDownloadActions crashed:', e); }
    try { EffectsUI.setupEffectsActions(); } catch(e) { console.error('EffectsUI.setupEffectsActions crashed:', e); }
    try { setupMeasurementActions(); } catch(e) { console.error('setupMeasurementActions crashed:', e); }
    try { MeasurementFlows.setupHybridMeasurementWizard(); } catch(e) { console.error('setupHybridMeasurementWizard crashed:', e); }
    try { primeSubwooferPreview(); } catch(e) { console.error('primeSubwooferPreview crashed:', e); }
    try { window.addEventListener('load', requestSubwooferPreviewRedrawFromState, { once: true }); } catch(e) { console.error('subwoofer preview load hook crashed:', e); }
    try { fetchInitialData(); } catch(e) { console.error('fetchInitialData crashed:', e); }
});


function setMeasurementSweepMenuOpen(shouldOpen) {
    if (!elements.measurementSweepMenu || !elements.measurementSweepToggleBtn) return;
    elements.measurementSweepMenu.classList.toggle('hidden', !shouldOpen);
    elements.measurementSweepToggleBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    if (shouldOpen) {
        elements.measurementSweepMenu.focus({ preventScroll: true });
    } else if (elements.measurementSweepMenu.contains(document.activeElement)) {
        elements.measurementSweepToggleBtn.focus({ preventScroll: true });
    }
}

function setupWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    CONFIG.wsUrl = `${protocol}//${window.location.host}/ws`;
    connectWebSocket();
}
function connectWebSocket() {
    if (reconnectAttempts >= CONFIG.maxReconnectAttempts) {
        showToast('WebSocket reconnection failed. Please refresh.', 'error');
        return;
    }
    if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
    }
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }
    const serial = ++wsConnectSerial;
    const socket = new WebSocket(CONFIG.wsUrl);
    ws = socket;
    socket.onopen = () => {
        if (serial !== wsConnectSerial || ws !== socket) return;
        console.log('WebSocket connected');
        state.wsConnected = true;
        reconnectAttempts = 0;
        if (offlineIndicatorTimer) {
            clearTimeout(offlineIndicatorTimer);
            offlineIndicatorTimer = null;
        }
        SettingsSystem.updatePowerButtonConnectionState();
        elements.offlineIndicator.classList.add('hidden');
        stopMetadataPolling();
        startPeakStatusPolling();
        void resyncPlaybackAfterReconnect();
    };
    socket.onclose = (event) => {
        if (ws === socket) ws = null;
        console.log('WebSocket disconnected', { code: event.code, reason: event.reason, wasClean: event.wasClean, serial });
        if (serial !== wsConnectSerial) return;
        state.wsConnected = false;
        scheduleOfflineIndicator();
        stopPeakStatusPolling();
        startMetadataPolling();
        scheduleReconnect();
    };
    socket.onerror = (err) => {
        if (serial !== wsConnectSerial) return;
        console.error('WebSocket error:', err);
    };
    socket.onmessage = (event) => {
        if (serial !== wsConnectSerial) return;
        try {
            const message = JSON.parse(event.data);
            handleWebSocketMessage(message);
        } catch (e) {
            console.error('Failed to parse WS message:', e);
        }
    };
}
function scheduleReconnect() {
    if (reconnectTimer) return;
    const delay = reconnectAttempts === 0 ? 0 : CONFIG.reconnectInterval;
    reconnectAttempts++;
    console.log(`Reconnecting in ${delay}ms (attempt ${reconnectAttempts})`);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWebSocket();
    }, delay);
}
function scheduleOfflineIndicator() {
    if (offlineIndicatorTimer) return;
    offlineIndicatorTimer = setTimeout(() => {
        offlineIndicatorTimer = null;
        if (state.wsConnected) return;
        SettingsSystem.updatePowerButtonConnectionState();
        elements.offlineIndicator.classList.remove('hidden');
    }, CONFIG.offlineIndicatorDelay);
}
// Volatile Try session hint, styled like the web-demo banner (demo/boot.js):
// a compact pill parked above the playback footer. data.live (top-level)
// or data.system.live. Dismissible via close button (session-scoped) and
// auto-hidden after a few minutes so it never covers controls.
const LIVE_BANNER_AUTOHIDE_MS = 3 * 60 * 1000;
const LIVE_BANNER_STORAGE_KEY = 'fxroute-live-banner-hidden';
let liveBannerWired = false;
let liveBannerTimer = null;
function positionLiveBanner() {
    const banner = elements.liveBanner;
    if (!banner || typeof banner.getBoundingClientRect !== 'function') return false;
    const footer = document.getElementById('playback-bar');
    if (!footer) return false;
    const rect = footer.getBoundingClientRect();
    const viewportH = window.innerHeight || document.documentElement.clientHeight;
    banner.style.bottom = Math.max(10, viewportH - rect.top + 12) + 'px';
    return true;
}
function updateLiveBanner(data) {
    const banner = elements.liveBanner;
    if (!banner) return;
    const live = !!(data && (data.live === true || (data.system && data.system.live === true)));
    if (!live) {
        banner.classList.add('is-hidden');
        return;
    }
    if (!liveBannerWired) {
        liveBannerWired = true;
        const closeBtn = banner.querySelector('.live-banner-close');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                banner.classList.add('is-hidden');
                try { sessionStorage.setItem(LIVE_BANNER_STORAGE_KEY, '1'); } catch (e) {}
            });
        }
        if (typeof window.addEventListener === 'function') {
            window.addEventListener('resize', () => { positionLiveBanner(); });
            window.addEventListener('load', () => { positionLiveBanner(); });
        }
        (function pollBanner() {
            if (!positionLiveBanner() && typeof window.setTimeout === 'function') {
                window.setTimeout(pollBanner, 120);
            }
        })();
    }
    let dismissed = false;
    try { dismissed = sessionStorage.getItem(LIVE_BANNER_STORAGE_KEY) === '1'; } catch (e) {}
    if (dismissed) return;
    positionLiveBanner();
    banner.classList.remove('is-hidden');
    if (liveBannerTimer) return;
    liveBannerTimer = setTimeout(() => {
        liveBannerTimer = null;
        banner.classList.add('is-hidden');
    }, LIVE_BANNER_AUTOHIDE_MS);
}
async function resyncPlaybackAfterReconnect() {
    const generation = ++wsReconnectSyncGeneration;
    try {
        const [playback, spotify, qobuz] = await Promise.all([
            fetch('/api/status')
                .then(resp => resp.ok ? resp.json() : null)
                .catch(() => null),
            fetchSpotifyStatus(),
            fetchQobuzStatus(),
        ]);
        if (generation !== wsReconnectSyncGeneration) return;

        if (playback) {
            mergePlaybackState(playback);
            // Reconnect: adopt the running track silently, never cue it.
            seedNativeTrackCueKey(playback.current_track);
            updateLiveBanner(playback);
            syncFooterOwnershipFromPlayback(playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        if (spotify) {
            handleIncomingSpotifyState(spotify, { renderTab: true, renderFooter: true });
        }
        if (qobuz) {
            handleIncomingQobuzState(qobuz, { renderFooter: true });
        }
        reconcileFooterSource();
        updatePlaybackUI();
        if (shouldPollSpotify()) {
            startSpotifyPoll();
        } else {
            stopSpotifyPoll();
        }
        if (shouldPollQobuz()) {
            startQobuzPoll();
        } else {
            stopQobuzPoll();
        }
    } catch (e) {
        console.debug('Reconnect state sync failed', e);
    }
}
function handleWebSocketMessage(msg) {
    const { type, data } = msg;
    switch (type) {
        case 'init':
            // Initial state
            if (data.player) {
                mergePlaybackState(data.player.state);
                // Page load: whatever already plays is the session track, not a
                // track change, so attaching never cues.
                seedNativeTrackCueKey(data.player.state.current_track);
                syncFooterOwnershipFromPlayback(data.player.state);
                syncLibraryStateFromPlaybackContext(true);
                updatePlaybackUI();
            }
            if (data.library) {
                state.library.tracks = [];
                renderLibraryView();
            }
            if (data.stations) {
                radioModule.setStations(data.stations);
            }
            if (data.catalog) {
                radioModule.setCatalogStations(data.catalog);
            }
            if (data.spotify) {
                handleIncomingSpotifyState(data.spotify, { renderTab: true, renderFooter: true });
            }
            if (data.qobuz) {
                handleIncomingQobuzState(data.qobuz, { renderFooter: true });
            }
            if (data.player && data.player.state && data.player.state.dsp) {
                state.dsp = data.player.state.dsp;
                if (state.dsp?.global_extras) {
                    EffectsUI.applyEffectsExtras({
                        limiterEnabled: !!state.dsp.global_extras?.limiter?.enabled,
                        headroomEnabled: !!state.dsp.global_extras?.headroom?.enabled,
                        headroomGainDb: Number(state.dsp.global_extras?.headroom?.params?.gainDb ?? -3),
                        autogainEnabled: !!state.dsp.global_extras?.autogain?.enabled,
                        autogainTargetDb: Number(state.dsp.global_extras?.autogain?.params?.targetDb ?? -12),
                        loudnessEnabled: !!state.dsp.global_extras?.loudness?.enabled,
                        loudnessStrength: state.dsp.global_extras?.loudness?.params?.strength ?? 10,
                        loudnessFftSize: Number(state.dsp.global_extras?.loudness?.params?.fftSize ?? 4096),
                        loudnessVolumeDb: Number(state.dsp.global_extras?.loudness?.params?.volumeDb ?? 0),
                        delayEnabled: !!state.dsp.global_extras?.delay?.enabled,
                        delayLeftMs: Number(state.dsp.global_extras?.delay?.params?.leftMs || 0),
                        delayRightMs: Number(state.dsp.global_extras?.delay?.params?.rightMs || 0),
                        bassEnabled: !!state.dsp.global_extras?.bass_enhancer?.enabled,
                        bassAmount: Number(state.dsp.global_extras?.bass_enhancer?.params?.amount || 0),
                        toneEffectEnabled: !!state.dsp.global_extras?.tone_effect?.enabled,
                        toneEffectMode: String(state.dsp.global_extras?.tone_effect?.mode || 'crystalizer'),
                    });
                }
                EffectsUI.renderEffects();
            }
            break;
        case 'playback': {
            const nextTrackId = data?.current_track?.id || null;
            const nextSamplerateSignature = JSON.stringify({
                trackId: nextTrackId,
                source: data?.current_track?.source || null,
                file: data?.current_file || null,
                playing: !!data?.playing,
                paused: !!data?.paused,
                ended: !!data?.ended,
            });
            const previousSamplerateSignature = lastSampleratePlaybackSignature;
            footerDebug('ws-playback', {
                payload: {
                    source: data?.current_track?.source || null,
                    title: data?.current_track?.title || null,
                    liveTitle: data?.live_title || null,
                    playing: !!data?.playing,
                    paused: !!data?.paused,
                },
            });
            // Always process WebSocket state updates — they are the authoritative source of truth.
            // playActionInFlight guards are only for local fetch responses (see playRadio/playLocal).
            const previousNativePlayback = { ...state.playback };
            mergePlaybackState(data);
            maybeCueNativePlaybackTrack(data, previousNativePlayback);
            const clearFooterSingleTrackLockAfterSync = footerSingleTrackStartLockSatisfied(state.playback);
            syncFooterOwnershipFromPlayback(data);
            if (clearFooterSingleTrackLockAfterSync) {
                clearPendingFooterSingleTrackStart();
            }
            syncLibraryStateFromPlaybackContext();
            // Reset action guard so this client doesn't block its own UI from server state.
            playbackActionInFlight = false;
            updatePlaybackUI();
            window.FXRouteStreaming?.notifyPlayback(data);
            if (data?.current_track?.source === 'local' && nextSamplerateSignature !== previousSamplerateSignature) {
                triggerSamplerateBurstPolling();
            }
            lastSampleratePlaybackSignature = nextSamplerateSignature;
            break;
        }
        case 'spotify':
            footerDebug('ws-spotify', {
                payload: {
                    title: data?.title || null,
                    artist: data?.artist || null,
                    status: data?.status || null,
                    available: !!data?.available,
                },
            });
            handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
            if (data && data.available && (data.status === 'Playing' || data.status === 'Paused' || data.title)) {
                reconcileFooterSource();
                if (window.__footerSource === 'spotify') {
                    startSpotifyPoll();
                }
            }
            break;
        case 'qobuz':
            handleIncomingQobuzState(data, { renderFooter: true });
            if (data && data.available && (data.status === 'Playing' || data.status === 'Paused' || data.title)) {
                reconcileFooterSource();
                if (window.__footerSource === 'qobuz') {
                    startQobuzPoll();
                }
            }
            break;
        case 'playback_peak_warning':
            state.playback.output_peak_warning = data || state.playback.output_peak_warning;
            renderPeakWarningBadge();
            break;
        case 'download':
            state.download = data;
            updateDownloadUI();
            handleDownloadStatusTransition(data);
            if (['starting', 'downloading'].includes(data.status)) {
                startDownloadStatusPolling();
            } else {
                stopDownloadStatusPolling();
            }
            break;
        case 'dsp':
            const prev = state.dsp?.compare;
            const presetNames = (data.presets || []).map(p => p.name);
            state.dsp = {
                ...data,
                combineDraft: state.dsp?.combineDraft || window.FXRouteBankUI.getDefaultEffectsCombineDraft(),
                peqDraft: state.dsp?.peqDraft || { presetName: '', eqMode: 'IIR', loadAfterCreate: false, leftBands: [EffectsUI.defaultPeqBand()], rightBands: [EffectsUI.defaultPeqBand()] },
                compare: window.FXRouteBankUI.resolveEffectsCompareState(data.compare || prev, presetNames, data.active_preset || ''),
            };
            state.dsp.combineDraft = window.FXRouteBankUI.normalizeEffectsCombineDraft(state.dsp.combineDraft, presetNames);
            EffectsUI.renderEffects();
            break;
        case 'download_complete':
            showToast(`Download complete: ${data.filename}`, 'success');
            refreshLibrary();
            break;
        case 'download_error':
            showToast(`Download error: ${data.error}`, 'error');
            break;
        case 'error':
            showToast(`Error: ${data.message || data}`, 'error');
            break;
        default:
            console.log('Unknown WS message type:', type);
    }
}
function sendWS(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(data));
    }
}
// Tab navigation
function setupTabNavigation() {
    elements.tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const tabId = tab.dataset.tab;
            switchTab(tabId);
        });
        // ARIA tabs pattern: Left/Right move focus and activate the previous/
        // next visible tab (Home/End jump to the ends).
        tab.addEventListener('keydown', (event) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            const visible = Array.from(document.querySelectorAll('.tab-btn'))
                .filter(btn => !btn.hidden && btn.style.display !== 'none');
            if (visible.length === 0) return;
            const currentIndex = visible.indexOf(tab);
            let targetIndex = -1;
            if (event.key === 'ArrowLeft') targetIndex = Math.max(0, currentIndex - 1);
            else if (event.key === 'ArrowRight') targetIndex = Math.min(visible.length - 1, currentIndex + 1);
            else if (event.key === 'Home') targetIndex = 0;
            else targetIndex = visible.length - 1;
            event.preventDefault();
            const target = visible[targetIndex];
            if (target && target !== tab) {
                target.focus();
                switchTab(target.dataset.tab);
            }
        });
    });
}

function updateTabsScrollAffordance() {
    const nav = document.querySelector('.tabs');
    if (!nav) return;
    const canScroll = nav.scrollWidth > nav.clientWidth + 2;
    nav.classList.toggle('can-scroll', canScroll);
}

function wireCrossoverTile() {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps the settings wiring unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.wireCrossoverTile();
}

function setupSettingsActions() {
    if (!elements.settingsOpenBtn || !elements.settingsPanel || !elements.settingsCloseBtn) return;
    elements.settingsOpenBtn.addEventListener('click', () => toggleSettingsPanel(true));
    elements.settingsCloseBtn.addEventListener('click', () => toggleSettingsPanel(false));
    if (elements.settingsOutputSelect) {
        const scanOutputsOnceForSelect = () => {
            if (settingsOutputScanOnFocusDone) return;
            settingsOutputScanOnFocusDone = true;
            void fetchAudioOutputOverview();
        };
        elements.settingsOutputSelect.addEventListener('pointerdown', scanOutputsOnceForSelect);
        elements.settingsOutputSelect.addEventListener('focus', scanOutputsOnceForSelect);
        elements.settingsOutputSelect.addEventListener('change', (event) => {
            const outputKey = event.target.value || '';
            if (outputKey) void saveAudioOutputSelection(outputKey);
        });
    }
    if (elements.settingsOutputModeSelect) {
        elements.settingsOutputModeSelect.addEventListener('change', (event) => {
            const mode = event.target.value || 'stereo';
            event.target.value = state.outputSystem.catalog?.active_mode || 'stereo';
            void applyOutputSystemMutation('switch_mode', { mode }, 'Output mode updated').then((data) => {
                if (data) void maybeApplyCrossoverStarters();
            });
        });
    }
    if (elements.settingsSamplerateSelect) {
        elements.settingsSamplerateSelect.addEventListener('change', (event) => {
            void saveSampleRatePolicy(event.target.value || 'auto');
        });
    }
    if (elements.settingsCrossoverSelect) {
        elements.settingsCrossoverSelect.addEventListener('change', (event) => {
            const catalog = state.outputSystem.catalog;
            if (!catalog) return;
            void applyOutputSystemMutation('set_crossover',
                { mode: catalog.active_mode, enabled: event.target.value === 'on' }, 'Crossover updated').then((data) => {
                if (data) void maybeApplyCrossoverStarters();
            });
        });
    }
    if (elements.settingsRoutingGrid) {
        elements.settingsRoutingGrid.addEventListener('change', (event) => {
            if (!event.target || event.target.tagName !== 'SELECT') return;
            const catalog = state.outputSystem.catalog;
            if (!catalog) return;
            const selects = elements.settingsRoutingGrid.querySelectorAll('select');
            const assignments = Array.from(selects).map((sel) => sel.value || 'off');
            void applyOutputSystemMutation('set_routing',
                { mode: catalog.active_mode, assignments }, 'Output routing updated').then((data) => {
                if (data) void maybeApplyCrossoverStarters();
            });
        });
    }
    if (elements.effectsBankSelect) {
        elements.effectsBankSelect.addEventListener('change', (event) => {
            const catalog = state.outputSystem.catalog;
            if (!catalog) return;
            const bankId = event.target.value || 'global';
            event.target.value = catalog.modes[catalog.active_mode]?.selected_bank || 'global';
            void applyOutputSystemMutation('select_bank',
                { mode: catalog.active_mode, bank_id: bankId }, false);
        });
    }
    wireCrossoverTile();
    if (elements.settingsSourceSelect) {
        elements.settingsSourceSelect.addEventListener('change', (event) => {
            const value = event.target.value || 'app-playback';
            if (value === 'app-playback') {
                void saveAudioSourceSelection('app-playback');
            } else if (value === 'bluetooth-input') {
                void saveAudioSourceSelection('bluetooth-input');
            } else if (value.startsWith('external-input::')) {
                void saveAudioSourceSelection('external-input', value.slice('external-input::'.length));
            }
        });
    }
    if (elements.settingsDeviceNameApply) {
        elements.settingsDeviceNameApply.addEventListener('click', () => {
            void SettingsSystem.applyDeviceName(elements.settingsDeviceNameInput?.value || '');
        });
    }
    if (elements.settingsDeviceNameInput) {
        elements.settingsDeviceNameInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                void SettingsSystem.applyDeviceName(elements.settingsDeviceNameInput.value || '');
            }
        });
    }
    if (elements.settingsMusicLibrarySelect) {
        elements.settingsMusicLibrarySelect.addEventListener('change', (event) => {
            const libraryId = event.target.value;
            if (libraryId === 'manual') {
                const url = prompt('SMB share URL', 'smb://server/share');
                if (url) void SettingsSystem.addManualMusicLibrary(url);
                else renderSettingsPanel();
            } else if (libraryId) {
                void SettingsSystem.selectMusicLibrary(libraryId);
            }
        });
    }
    elements.settingsHardwareRcaBtn?.addEventListener('click', () => SettingsSystem.runHardwareCommand('/api/hardware/input/rca', 'RCA selected'));
    elements.settingsHardwareXlrBtn?.addEventListener('click', () => SettingsSystem.runHardwareCommand('/api/hardware/input/xlr', 'XLR selected'));
    elements.settingsHardwarePressBtn?.addEventListener('click', () => SettingsSystem.runHardwareCommand('/api/hardware/input/press', 'Input button pressed'));
    elements.settingsHardwareAutoOnBtn?.addEventListener('click', () => SettingsSystem.runHardwareCommand('/api/hardware/auto/on', 'Auto mode enabled'));
    elements.settingsHardwareAutoOffBtn?.addEventListener('click', () => SettingsSystem.runHardwareCommand('/api/hardware/auto/off', 'Auto mode disabled'));
    elements.settingsUpdateCheckBtn?.addEventListener('click', () => SettingsSystem.checkFxrouteUpdate());
    elements.settingsUpdateRunBtn?.addEventListener('click', () => SettingsSystem.runFxrouteUpdate());
    elements.settingsRestoreRunBtn?.addEventListener('click', () => SettingsSystem.restoreFxrouteToPublic());
    elements.settingsUpdateDetailsToggle?.addEventListener('click', () => {
        const maintenance = state.settings.maintenance;
        const isOpen = !!maintenance.detailsExpanded
            || (!!maintenance.pending && maintenance.operation === 'update')
            || !!maintenance.restartPending
            || (!!maintenance.hasError && !maintenance.userCollapsedDetails);
        maintenance.detailsExpanded = !isOpen;
        maintenance.userCollapsedDetails = isOpen;
        SettingsSystem.renderMaintenancePanel();
    });
    const backdrop = elements.settingsPanel.querySelector('.manage-overlay-backdrop');
    if (backdrop) backdrop.addEventListener('click', () => toggleSettingsPanel(false));
    setupQobuzLoginModal();
    setupTidalLoginModal();
    renderSettingsPanel();
}


function defaultSubCrossoverSlope(family) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.defaultSubCrossoverSlope(family);
}

function clampSubCrossoverFrequency(value) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.clampSubCrossoverFrequency(value);
}

// Normalize one sub crossover shape (shared or per-side) against the optional
// fallback: unknown families and non-6-dB slopes fall back instead of reaching
// the backend, which rejects them fail-closed.
function normalizeSubCrossoverShape(input = {}, fallback = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.normalizeSubCrossoverShape(input, fallback);
}

// Shared crossover plus the per-side view the subwoofer tile renders.
function subCrossoverSettings(view, fallback = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.subCrossoverSettings(view, fallback);
}

function normalizeSubwooferSettings(input = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.normalizeSubwooferSettings(input);
}

function familyPrefix(family) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.familyPrefix(family);
}

function subCrossoverLabel(shape) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.subCrossoverLabel(shape);
}

function normalizeSingleSubwooferSettings(input = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.normalizeSingleSubwooferSettings(input);
}

function subwoofer21ToSub22Sub(subwoofer = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.subwoofer21ToSub22Sub(subwoofer);
}

function getSubwooferGlobalSettings(outputMode = {}, fallback = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.getSubwooferGlobalSettings(outputMode, fallback);
}

function normalizeSubwoofersSettings(subwoofers = {}, fallbackSubwoofer = {}) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.normalizeSubwoofersSettings(subwoofers, fallbackSubwoofer);
}

function readSubCrossoverShape(frequencyEl, familyEl, slopeEl, fallback) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.readSubCrossoverShape(frequencyEl, familyEl, slopeEl, fallback);
}

// Crossover draft of the current mode: the shared shape, or one shape per side
// while a Stereo sub pair is unlinked. A coupled pair mirrors the shared values
// into both side overrides so a later unlink starts from what is audible now.
function collectSubCrossoverDraft() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.collectSubCrossoverDraft();
}

function collectSubwooferSettings() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.collectSubwooferSettings();
}

function collectSubwoofer22Settings() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.collectSubwoofer22Settings();
}

function isSubwoofer22Mode(mode) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.isSubwoofer22Mode(mode);
}

function isSubwooferModeName(mode) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.isSubwooferModeName(mode);
}

function normalizeOutputModeName(mode) {
    return isSubwooferModeName(mode) ? mode : 'stereo';
}

function formatSampleRateKhz(rate) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.formatSampleRateKhz(rate);
}

function formatTransitionErrorDetail(detail, fallback = 'Request failed') {
    const mod = (typeof window !== 'undefined' && window.FXRouteApi) || (typeof globalThis !== 'undefined' && globalThis.FXRouteApi) || null;
    return mod.formatTransitionErrorDetail(detail, fallback);
}

function formatRadioStreamLine(streamInfo, effectiveOutputRate = null) {
    if (!streamInfo || typeof streamInfo !== 'object') streamInfo = {};
    const parts = [];
    const codec = streamInfo.codec ? String(streamInfo.codec) : '';
    // Lossless codecs: the decoded bitrate is content-dependent and the
    // `Lossless` profile label is redundant, so neither is shown for them —
    // the meaningful facts are bit depth and sample rate (FLAC · 24bit ·
    // 44.1kHz). Lossy codecs keep the profile/bitrate line (AAC · 320kbps
    // · 44.1kHz). Units stay glued to their values (no inner spaces) so the
    // footer pill needs less width.
    const lossless = !!codec && ['FLAC', 'ALAC', 'APE', 'WAVPACK', 'TTA', 'PCM'].includes(codec.toUpperCase());
    if (codec) parts.push(codec);
    if (!lossless) {
        if (streamInfo.profile) {
            parts.push(String(streamInfo.profile));
        } else if (Number.isFinite(Number(streamInfo.bitrate_kbps)) && Number(streamInfo.bitrate_kbps) > 0) {
            parts.push(`${Math.round(Number(streamInfo.bitrate_kbps))}kbps`);
        }
    }
    if (Number.isFinite(Number(streamInfo.bit_depth)) && Number(streamInfo.bit_depth) > 0) {
        parts.push(`${Math.round(Number(streamInfo.bit_depth))}bit`);
    }
    const displayedRate = Number(effectiveOutputRate) || Number(streamInfo.samplerate_hz);
    if (Number.isFinite(displayedRate) && displayedRate > 0) {
        parts.push(`${(displayedRate / 1000).toFixed(1).replace(/\.0$/, '')}kHz`);
    }
    return parts.join(' · ');
}

function formatStreamingMetaLine(data) {
    // Streaming owners render through the same meta-tag renderer as
    // library/radio: only facts the provider actually delivered are shown.
    // Qobuz/TIDAL contribute real stream facts (audio_format/bit_depth/
    // sample_rate); Spotify delivers none, so its tag is the resolved rate
    // alone — never an invented codec or bit depth.
    const info = data || {};
    if (info.audio_format || info.bit_depth || info.sample_rate) {
        return formatRadioStreamLine({
            codec: info.audio_format ? String(info.audio_format).toUpperCase() : '',
            bit_depth: info.bit_depth,
            samplerate_hz: info.sample_rate,
        });
    }
    const samplerate = state.samplerate || {};
    return samplerate.available && samplerate.active_rate
        ? formatRateKhz(samplerate.active_rate)
        : '';
}

/**
 * Await only a real pending/running debounced subwoofer save before a
 * measurement. A committed mode must not be posted again just because the
 * measurement panel was opened.
 */
async function flushSubwooferSettingsBeforeMeasurement() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.flushSubwooferSettingsBeforeMeasurement();
}

function collectRuntimeDebugUiState(extra = {}) {
    return {
        visibleTab: window.__visibleTab || '',
        outputMode: state.settings.audioOutputs?.output_mode?.mode || '',
        sourceMode: state.settings.sourceMode?.mode || '',
        measurement: {
            activeJobId: state.measurement.activeJobId || '',
            repeatJobActive: !!state.measurement.repeatJobActive,
            startInFlight: !!state.measurement.startInFlight,
            statusText: state.measurement.statusText || '',
        },
        playback: {
            playing: !!state.playback.playing,
            paused: !!state.playback.paused,
            currentTitle: state.playback.current_track?.title || '',
            currentSource: state.playback.current_track?.source || '',
        },
        ...extra,
    };
}

async function postRuntimeDebugSnapshot(label, extra = {}) {
    // Runtime snapshots are a debugging aid, not telemetry: they stay off
    // unless explicitly enabled in the console via this flag.
    if (window.__fxDebugRuntimeSnapshots !== true) return;
    try {
        await fetch('/api/debug/21-runtime-state', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                label,
                ui_state: collectRuntimeDebugUiState(extra),
            }),
        });
    } catch (error) {
        console.debug('2.1 runtime debug snapshot failed', label, error);
    }
}

function stopSettingsStatusPolling() {
    if (settingsStatusPollTimer) {
        clearInterval(settingsStatusPollTimer);
        settingsStatusPollTimer = null;
    }
    SettingsSystem.stopMusicLibraryRefresh();
}

function startSettingsStatusPolling() {
    stopSettingsStatusPolling();
    settingsStatusPollTimer = setInterval(() => {
        if (!elements.settingsPanel || elements.settingsPanel.classList.contains('hidden')) {
            stopSettingsStatusPolling();
            return;
        }
        // Music library discovery is refreshed on dialog open and after
        // selection; re-polling it here every 2.5s only re-scans the SMB
        // network and adds pointless requests while the dialog stays open.
        void Promise.all([fetchAudioSourceOverview(), SettingsSystem.fetchHardwareStatus()]);
    }, 2500);
}

function toggleSettingsPanel(forceOpen = null) {
    if (!elements.settingsPanel) return;
    const shouldOpen = forceOpen === null
        ? elements.settingsPanel.classList.contains('hidden')
        : !!forceOpen;
    elements.settingsPanel.classList.toggle('hidden', !shouldOpen);
    if (elements.settingsOpenBtn) {
        elements.settingsOpenBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    }
    if (shouldOpen) {
        settingsOutputScanOnFocusDone = false;
        renderSettingsPanel();
        void Promise.all([fetchAudioOutputOverview(), fetchAudioSourceOverview(), SettingsSystem.fetchHardwareStatus(), SettingsSystem.fetchMusicLibraries(), fetchProviderAdmin(), SettingsSystem.checkFxrouteUpdate({ silent: true })]);
        startSettingsStatusPolling();
        window.FXRouteModal?.open(elements.settingsPanel, {
            initialFocus: elements.settingsCloseBtn,
            onEscape: () => toggleSettingsPanel(false),
        });
    } else {
        stopSettingsStatusPolling();
        window.FXRouteModal?.close(elements.settingsPanel);
    }
}

function formatRateKhz(rate) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.formatRateKhz(rate);
}

function formatBluetoothModeStatus(bluetooth = {}) {
    const detailParts = [];
    if (bluetooth.active_codec) detailParts.push(String(bluetooth.active_codec).toUpperCase());
    const rateLabel = formatRateKhz(bluetooth.active_rate);
    if (rateLabel) detailParts.push(rateLabel);
    const detailSuffix = detailParts.length ? ` (${detailParts.join(' · ')})` : '';
    if (bluetooth.connected_device) return `${bluetooth.connected_device}${detailSuffix}`;
    switch (bluetooth.state) {
        case 'streaming':
            return `streaming${detailSuffix}`;
        case 'connected':
            return `connected${detailSuffix}`;
        case 'discoverable':
        case 'pairing':
            return 'discoverable, waiting for device';
        case 'idle':
            return bluetooth.receiver_enabled ? 'receiver ready' : 'available';
        case 'error':
            return 'error';
        default:
            return bluetooth.available ? 'unavailable' : 'not detected';
    }
}

function settingsCertificateUrl() {
    // Same-origin relative URL: the page may be served over HTTP or HTTPS
    // (Caddy proxies both to the backend). An absolute http:// rewrite turns
    // the link into mixed content on HTTPS pages, where browsers silently
    // block the insecure download and the click appears to do nothing.
    return '/api/certificate/local-root';
}

function isSelectFocused(selectEl) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    if (mod && typeof mod.isSelectFocused === 'function') return mod.isSelectFocused(selectEl);
    return !!selectEl && typeof document !== 'undefined' && document.activeElement === selectEl;
}


// ---------------------------------------------------------------------------
// Settings -> Providers (static/provider_settings.js)
// ---------------------------------------------------------------------------
// Install/update/service, visibility toggle and the Qobuz/TIDAL login
// dialogs live in the provider module; app.js keeps thin wrappers and
// owns the device-name row plus streaming refreshes via callbacks.
async function fetchProviderAdmin() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.fetchProviderAdmin();
}

async function setProviderEnabled(providerId, enabled) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setProviderEnabled(providerId, enabled);
}

function renderProviderOperation(providerId, detail, log) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.renderProviderOperation(providerId, detail, log);
}

function isProviderOpBusy(providerId) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.isProviderOpBusy(providerId);
}

function providerBusyAttribute(providerId) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.providerBusyAttribute(providerId);
}

function wireProviderActionButtons() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.wireProviderActionButtons();
}

async function runProviderInstall(providerId) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.runProviderInstall(providerId);
}

async function runProviderUninstall(providerId) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.runProviderUninstall(providerId);
}

async function runProviderServiceAction(providerId, action) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.runProviderServiceAction(providerId, action);
}

async function beginQobuzLogin() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.beginQobuzLogin();
}

function setQobuzLoginStatus(message, mode = '') {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setQobuzLoginStatus(message, mode);
}

function setQobuzLoginBusy(busy) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setQobuzLoginBusy(busy);
}

function openQobuzLoginModal(loginUrl) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.openQobuzLoginModal(loginUrl);
}

function closeQobuzLoginModal() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.closeQobuzLoginModal();
}

async function cancelQobuzLogin() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.cancelQobuzLogin();
}

async function copyQobuzLoginUrl() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.copyQobuzLoginUrl();
}

async function finishQobuzLoginFromModal() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.finishQobuzLoginFromModal();
}

function setupQobuzLoginModal() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setupQobuzLoginModal();
}

async function beginTidalLogin() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.beginTidalLogin();
}

function setTidalLoginStatus(message, mode = '') {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setTidalLoginStatus(message, mode);
}

function setTidalLoginBusy(busy) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setTidalLoginBusy(busy);
}

function openTidalLoginModal(loginUrl) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.openTidalLoginModal(loginUrl);
}

function closeTidalLoginModal() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.closeTidalLoginModal();
}

async function copyTidalLoginUrl() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.copyTidalLoginUrl();
}

async function finishTidalLoginFromModal() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.finishTidalLoginFromModal();
}

function setupTidalLoginModal() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.setupTidalLoginModal();
}

async function qobuzLogout() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.qobuzLogout();
}

async function tidalLogout() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.tidalLogout();
}

function providerAdminButtonHtml(provider) {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.providerAdminButtonHtml(provider);
}

function renderProviderSettings() {
    // Provider settings live in static/provider_settings.js
    // (window.FXRouteProviderSettings). Thin wrapper keeps existing
    // call sites and the streaming login callbacks unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteProviderSettings) || (typeof globalThis !== 'undefined' && globalThis.FXRouteProviderSettings) || null;
    return mod.renderProviderSettings();
}


function renderSettingsPanel() {
    if (elements.settingsCertificateLink) {
        const certUrl = settingsCertificateUrl();
        elements.settingsCertificateLink.href = certUrl;
        elements.settingsCertificateLink.title = certUrl;
    }

    const overview = state.settings?.audioOutputs || {};
    const defaultOutput = overview.default_output || null;
    const selectedOutput = overview.selected_output || defaultOutput || null;
    const currentOutput = overview.current_output || null;
    const outputs = Array.isArray(overview.outputs) ? overview.outputs : [];
    const pendingSelectionKey = overview.pendingSelectionKey || null;
    const selectableOutputs = outputs.filter((output) => !!output.selectable);
    const effectiveSelectedKey = selectedOutput?.key || currentOutput?.key || defaultOutput?.target_name || '';
    if (elements.settingsOutputSummary) {
        if (!overview.available) {
            elements.settingsOutputSummary.textContent = 'Outputs unavailable.';
        } else {
            elements.settingsOutputSummary.textContent = `Current: ${currentOutput?.label || defaultOutput?.target_label || 'Unknown output'}`;
        }
    }

    if (elements.settingsOutputSelect && !isSelectFocused(elements.settingsOutputSelect)) {
        const options = selectableOutputs.map((output) => {
            const label = output.label || output.name || 'Unknown output';
            return `<option value="${escapeHtml(output.key || '')}">${escapeHtml(label)}</option>`;
        });
        elements.settingsOutputSelect.innerHTML = options.join('') || '<option value="">No outputs available</option>';
        // Never leave the field blank on an unmatchable key (e.g. a stale
        // non-selectable sink): prefer the computed key, then the PipeWire
        // default, then the first selectable output.
        const optionKeys = new Set(selectableOutputs.map((output) => output.key || ''));
        let selectKey = effectiveSelectedKey && optionKeys.has(effectiveSelectedKey) ? effectiveSelectedKey : '';
        if (!selectKey) {
            const defaultKey = overview.default_output?.key || overview.default_output?.target_name || '';
            selectKey = defaultKey && optionKeys.has(defaultKey) ? defaultKey : '';
        }
        if (!selectKey && selectableOutputs.length) selectKey = selectableOutputs[0].key || '';
        if (selectKey) elements.settingsOutputSelect.value = selectKey;
        elements.settingsOutputSelect.disabled = !overview.available || !!pendingSelectionKey || !selectableOutputs.length;
    }

    const deviceProfile = selectedOutput?.device_profile || null;
    const tiers = Array.isArray(deviceProfile?.tiers) ? deviceProfile.tiers : [];
    const activeTier = tiers.find((tier) => tier.id === deviceProfile?.active_tier) || null;

    const sampleRatePolicy = state.samplerate?.policy || { mode: 'auto', rate: null };
    const supportedRates = Array.isArray(selectedOutput?.supported_rates) ? selectedOutput.supported_rates : [];
    if (elements.settingsSamplerateSelect && !isSelectFocused(elements.settingsSamplerateSelect)) {
        elements.settingsSamplerateSelect.innerHTML = [
            '<option value="auto">Auto</option>',
            ...supportedRates.map((rate) => `<option value="${rate}">${formatSampleRateKhz(rate)}</option>`),
        ].join('');
        elements.settingsSamplerateSelect.value = sampleRatePolicy.mode === 'fixed'
            ? String(sampleRatePolicy.rate || '')
            : 'auto';
        elements.settingsSamplerateSelect.disabled = !!state.samplerate?.pending || !overview.available;
    }
    if (elements.settingsSamplerateHint) {
        const tierNote = activeTier ? ` · ${activeTier.channels} channels active` : '';
        elements.settingsSamplerateHint.textContent = sampleRatePolicy.mode === 'fixed'
            ? `Playback graph and hardware output are fixed at ${formatSampleRateKhz(sampleRatePolicy.rate)}${tierNote}.`
            : `Follows the effective playback sample rate${activeTier ? '; the device switches channel inventory automatically' : ''}.`;
    }

    const sourceOverview = state.settings?.sourceMode || {};
    const sourceInputs = Array.isArray(sourceOverview.inputs) ? sourceOverview.inputs : [];
    const bluetooth = sourceOverview.bluetooth || {};
    const currentSourceInput = sourceOverview.current_input || sourceOverview.default_input || null;
    const selectedSourceInput = sourceOverview.selected_input || currentSourceInput || null;
    const currentMode = sourceOverview.mode || 'app-playback';
    const bluetoothSelectable = !!bluetooth.selectable;

    if (elements.settingsSourceSelect) {
        const inputOptions = [
            '<option value="app-playback">App playback</option>',
            ...sourceInputs.map((input) => `<option value="external-input::${escapeHtml(input.key || '')}">External input: ${escapeHtml(input.label || input.name || 'Unknown input')}</option>`),
            `<option value="bluetooth-input"${bluetoothSelectable ? '' : ' disabled'}>Bluetooth input</option>`,
        ];
        elements.settingsSourceSelect.innerHTML = inputOptions.join('');
        if (currentMode === 'external-input') {
            elements.settingsSourceSelect.value = `external-input::${selectedSourceInput?.key || currentSourceInput?.key || ''}`;
        } else if (currentMode === 'bluetooth-input') {
            elements.settingsSourceSelect.value = 'bluetooth-input';
        } else {
            elements.settingsSourceSelect.value = 'app-playback';
        }
        elements.settingsSourceSelect.disabled = !!sourceOverview.pending;
    }
    if (elements.settingsSourceModeHint) {
        if (currentMode === 'external-input') {
            elements.settingsSourceModeHint.textContent = `Current: ${selectedSourceInput?.label || currentSourceInput?.label || 'No inputs detected'}`;
        } else if (currentMode === 'bluetooth-input') {
            elements.settingsSourceModeHint.textContent = 'Current: Bluetooth input';
        } else {
            elements.settingsSourceModeHint.textContent = 'Current: App playback';
        }
    }
    if (elements.settingsBluetoothStatus) {
        const bluetoothNote = Array.isArray(bluetooth.notes) && bluetooth.notes.length ? ` · ${bluetooth.notes[0]}` : '';
        elements.settingsBluetoothStatus.textContent = `Bluetooth: ${formatBluetoothModeStatus(bluetooth)}${bluetoothNote}`;
    }
    const musicLibrary = state.settings?.musicLibrary || {};
    if (elements.settingsMusicLibrarySelect && !isSelectFocused(elements.settingsMusicLibrarySelect)) {
        const model = SettingsSystem.musicLibrarySelectModel(musicLibrary);
        elements.settingsMusicLibrarySelect.innerHTML = model.html;
        elements.settingsMusicLibrarySelect.value = model.value;
        elements.settingsMusicLibrarySelect.disabled = model.disabled;
    }
    if (elements.settingsMusicLibraryHint) {
        const cachedCount = Array.isArray(musicLibrary.libraries) ? musicLibrary.libraries.length : 0;
        elements.settingsMusicLibraryHint.textContent =
            musicLibrary.scanning && cachedCount > 0 ? 'Scanning…' : '';
    }
    renderProviderSettings();
    SettingsSystem.renderDeviceNameSettings();
    SettingsSystem.renderHardwareController();
    SettingsSystem.renderMaintenancePanel();
    renderSubwooferPanel();
    renderOutputSystemSection();
    applySourceModeUiState();
}

// Select-model for the Music Library selector in Technical settings.
// While a discovery fetch is in flight and no list is cached yet, the
// selector shows an explicit disabled loading state instead of a bare
// "Local" option that reads like a finished, share-less result. A cached
// list stays visible (stale-while-revalidate) and the select is disabled
// until the response lands. While a background SMB rescan is running
// (scanning) and no SMB entry is known yet, the selector shows a disabled
// "Scanning network shares…" placeholder instead of the local-only list;
// already-cached SMB entries keep rendering immediately.

function externalInputModeActive() {
    return state.settings?.sourceMode?.mode === 'external-input';
}

function nonAppSourceModeActive() {
    return ['external-input', 'bluetooth-input'].includes(state.settings?.sourceMode?.mode);
}

// The footer meter/badge show the live post-DSP output level. App playback
// pauses when a line source takes over, but the DSP path stays live then,
// so source modes count as active signal too.
function isFooterSignalActive() {
    if (isStreamingFooterSource(window.__footerSource)) {
        return streamingFooterData()?.status === 'Playing';
    }
    if (!!state.playback.playing && !state.playback.paused) return true;
    return nonAppSourceModeActive();
}

// Compact footer source switcher for bluetooth-input / external-input modes.
// Entries reuse the audio source overview, so only real, selectable sources
// appear: Bluetooth first (when available), then every external stereo pair
// in overview order. Labels stay short ("Input 1/2"); the human device name
// is option text only, never a PipeWire/ALSA node name.
function shortSourcePairLabel(pairLabel) {
    const text = String(pairLabel || '').trim();
    const match = /^Input\s+(\d+)\s*[–—-]\s*(\d+)$/.exec(text);
    if (match) return `Input ${match[1]}/${match[2]}`;
    return text;
}

function buildSourceSwitcherEntries(sourceMode) {
    const mode = sourceMode || {};
    const entries = [];
    const bluetooth = mode.bluetooth || {};
    if (bluetooth.selectable) {
        const session = bluetooth.active_session || {};
        const detail = [session.device_name, session.active_codec].filter(Boolean).join(' · ');
        entries.push({ kind: 'bluetooth', key: 'bluetooth-input', label: 'Bluetooth', sub: detail, optionLabel: detail ? `Bluetooth — ${detail}` : 'Bluetooth' });
    }
    const inputs = Array.isArray(mode.inputs) ? mode.inputs : [];
    inputs.forEach((input) => {
        if (!input || !input.key) return;
        const label = shortSourcePairLabel(input.pair_label) || shortSourcePairLabel(input.label) || 'Input';
        const sub = String(input.device_label || input.label || '');
        entries.push({
            kind: 'external',
            key: String(input.key),
            label,
            sub,
            optionLabel: (sub && sub !== label) ? `${label} — ${sub}` : label,
        });
    });
    return entries;
}

function findSourceSwitcherIndex(entries, sourceMode) {
    const mode = sourceMode || {};
    if (!Array.isArray(entries) || entries.length === 0) return -1;
    if ((mode.mode || '') === 'bluetooth-input') {
        return entries.findIndex((entry) => entry.kind === 'bluetooth');
    }
    const key = mode.selected_input?.key || mode.current_input?.key || '';
    if (!key) return -1;
    return entries.findIndex((entry) => entry.kind === 'external' && entry.key === key);
}

function cycleSourceSwitcherIndex(entries, currentIndex, delta) {
    if (!Array.isArray(entries) || entries.length === 0) return -1;
    const step = (delta || 0) >= 0 ? 1 : -1;
    const base = currentIndex >= 0 ? currentIndex : (step > 0 ? -1 : 0);
    return (((base + step) % entries.length) + entries.length) % entries.length;
}

function activateSourceSwitcherEntry(entry) {
    if (!entry) return;
    if (entry.kind === 'bluetooth') {
        void saveAudioSourceSelection('bluetooth-input');
    } else {
        void saveAudioSourceSelection('external-input', entry.key);
    }
}

function stepSourceSwitcher(delta) {
    if (sourceSwitcherGuardReason()) return;
    const sourceMode = state.settings?.sourceMode || {};
    const entries = buildSourceSwitcherEntries(sourceMode);
    const current = findSourceSwitcherIndex(entries, sourceMode);
    const next = cycleSourceSwitcherIndex(entries, current, delta);
    if (next < 0 || next === current) return;
    activateSourceSwitcherEntry(entries[next]);
}

// Measurement and AutoSub capture own their signal path: while a job is
// active the footer switcher is parked instead of rewiring the graph.
function sourceSwitcherGuardReason() {
    if (window.FXRouteMeasurementJob.hasActiveMeasurementJob()) {
        return 'Unavailable while a measurement is running';
    }
    if (state.settings?.sourceMode?.pending) {
        return 'Switching sources…';
    }
    return '';
}

// Signature of the last rendered source <select>: option writes only happen
// when the list actually changes, so status polls never rebuild the popup
// while it is open (which would instantly close the native dropdown).
let _sourceSelectSignature = null;

function renderSourceModeFooter() {
    const bar = elements.playbackBar;
    if (!bar) return;
    // Line sources own the footer outright: stale streaming ownership (e.g.
    // a retained Spotify Paused context) must not suppress the switcher.
    // reconcileFooterSource() already forces 'local' in these modes; this
    // stays order-independent so no poll interleaving can flash the app
    // footer back.
    const active = nonAppSourceModeActive();
    bar.classList.toggle('source-mode', active);
    if (elements.sourceSwitcher) elements.sourceSwitcher.classList.toggle('hidden', !active);
    if (elements.transportControls) elements.transportControls.classList.toggle('hidden', active);
    if (!active) return;
    const sourceMode = state.settings?.sourceMode || {};
    const entries = buildSourceSwitcherEntries(sourceMode);
    const current = findSourceSwitcherIndex(entries, sourceMode);
    const currentEntry = current >= 0 ? entries[current] : null;
    const guard = sourceSwitcherGuardReason();
    // The track/metadata block is hidden by CSS in these modes; only the
    // queue pill and seek row need explicit parking here.
    if (elements.queueStatus) elements.queueStatus.classList.add('hidden');
    setFooterProgressState(false);
    const signature = JSON.stringify([
        entries.map((entry) => [entry.key, entry.optionLabel]),
        currentEntry ? currentEntry.key : '',
    ]);
    if (elements.sourceSelect && signature !== _sourceSelectSignature
        && document.activeElement !== elements.sourceSelect) {
        elements.sourceSelect.innerHTML = entries.map((entry) =>
            `<option value="${escapeHtml(entry.key)}">${escapeHtml(entry.optionLabel)}</option>`).join('');
        elements.sourceSelect.value = currentEntry ? currentEntry.key : '';
        _sourceSelectSignature = signature;
    }
    if (elements.sourceSelect) {
        elements.sourceSelect.disabled = !!guard || entries.length === 0;
        elements.sourceSelect.title = guard || 'Choose audio source';
    }
    const arrowsDisabled = !!guard || entries.length < 2;
    for (const button of [elements.sourcePrev, elements.sourceNext]) {
        if (!button) continue;
        button.disabled = arrowsDisabled;
        button.title = guard || button.getAttribute('aria-label') || '';
    }
}

function applySourceModeUiState() {
    const nonAppSourceActive = nonAppSourceModeActive();
    ['radio', 'spotify', 'qobuz', 'tidal', 'library'].forEach((tabId) => {
        const tabButton = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
        const tabPanel = document.getElementById(`tab-${tabId}`);
        if (tabButton) tabButton.classList.toggle('hidden', nonAppSourceActive);
        if (tabPanel) tabPanel.classList.toggle('hidden', nonAppSourceActive);
    });
    if (elements.playbackBar) {
        elements.playbackBar.classList.remove('hidden');
    }
    renderSourceModeFooter();
    if (nonAppSourceActive && ['radio', 'spotify', 'qobuz', 'tidal', 'library'].includes(window.__visibleTab)) {
        switchTab('effects');
    }
}

async function saveAudioOutputSelection(key) {
    if (!key) return;
    state.settings.audioOutputs.pendingSelectionKey = key;
    renderSettingsPanel();
    try {
        const resp = await fetch('/api/audio/outputs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save audio output');
        state.settings.audioOutputs = {
            loaded: true,
            available: !!data.available,
            default_output: data.default_output || null,
            selected_output: data.selected_output || null,
            current_output: data.current_output || null,
            outputs: Array.isArray(data.outputs) ? data.outputs : [],
            notes: Array.isArray(data.notes) ? data.notes : [],
            pendingSelectionKey: null,
            output_mode: data.output_mode || state.settings.audioOutputs.output_mode,
        };
        renderSettingsPanel();
        const modeAdjustment = data.output_mode?.mode_adjustment;
        await fetchOutputSystemCatalog(true);
        if (modeAdjustment?.message) {
            showToast(modeAdjustment.message, 'info');
        } else {
            showToast('Audio output updated', 'success');
        }
    } catch (error) {
        state.settings.audioOutputs.pendingSelectionKey = null;
        renderSettingsPanel();
        showToast(error.message || 'Failed to save audio output', 'error');
    }
}

async function fetchAudioOutputOverview() {
    try {
        const resp = await fetch('/api/audio/outputs');
        if (!resp.ok) throw new Error('Failed to fetch audio outputs');
        const data = await resp.json();
        if (state.settings.audioOutputs.pendingSelectionKey) return;
        state.settings.audioOutputs = {
            loaded: true,
            available: !!data.available,
            default_output: data.default_output || null,
            selected_output: data.selected_output || null,
            current_output: data.current_output || null,
            outputs: Array.isArray(data.outputs) ? data.outputs : [],
            notes: Array.isArray(data.notes) ? data.notes : [],
            pendingSelectionKey: null,
            output_mode: data.output_mode || state.settings.audioOutputs.output_mode,
        };
        renderSettingsPanel();
        renderSubwooferPanel();
        MeasurementFlows.syncAutoSubButton();
        MeasurementFlows.syncSpeakerAlignButton();
        void fetchOutputSystemCatalog(true);
    } catch (e) {
        state.settings.audioOutputs = {
            loaded: true,
            available: false,
            default_output: null,
            selected_output: null,
            current_output: null,
            outputs: [],
            notes: [e.message || 'Failed to fetch audio outputs'],
            pendingSelectionKey: null,
            output_mode: state.settings.audioOutputs.output_mode,
        };
        renderSettingsPanel();
        renderSubwooferPanel();
        MeasurementFlows.syncAutoSubButton();
        MeasurementFlows.syncSpeakerAlignButton();
    }
}

async function saveSampleRatePolicy(value) {
    const rate = Number(value);
    const policy = value === 'auto' ? { mode: 'auto', rate: null } : { mode: 'fixed', rate };
    state.samplerate.pending = true;
    renderSettingsPanel();
    try {
        const resp = await fetch('/api/audio/samplerate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(policy),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to save sample-rate policy'));
        state.samplerate = { ...state.samplerate, ...data, pending: false };
        renderSamplerateUI();
        // A rate change can switch the channel inventory server-side; pull
        // the fresh output overview so channel count, tier note and routing
        // matrix update without reopening settings.
        await fetchAudioOutputOverview();
        triggerSamplerateBurstPolling();
        showToast('Sample-rate policy updated', 'success');
    } catch (error) {
        state.samplerate.pending = false;
        renderSettingsPanel();
        showToast(error.message || 'Failed to save sample-rate policy', 'error');
    }
}

function applyAudioOutputOverview(data) {
    state.settings.audioOutputs = {
        loaded: true,
        available: !!data.available,
        default_output: data.default_output || null,
        selected_output: data.selected_output || null,
        current_output: data.current_output || null,
        outputs: Array.isArray(data.outputs) ? data.outputs : [],
        notes: Array.isArray(data.notes) ? data.notes : [],
        pendingSelectionKey: null,
        output_mode: data.output_mode || state.settings.audioOutputs.output_mode,
    };
    renderSettingsPanel();
}

function outputSystemModule() {
    return (typeof window !== 'undefined' && window.FXRouteOutputState) || null;
}

function routedSubwooferView() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.routedSubwooferView();
}

function ensureOutputSystemBoxes() {
    if (!state.outputSystem) state.outputSystem = { catalog: null, busy: false };
    if (!state.crossover) state.crossover = { activeWay: null, response: null, busy: false, linkLR: false };
    if (state.crossover.linkLR === undefined) state.crossover.linkLR = false;
}

async function fetchOutputSystemCatalog(force = false) {
    // Output-system orchestration lives in static/output_system_controller.js
    // (window.FXRouteOutputSystemController). Thin wrapper keeps existing
    // call sites and the mutation fan-out order unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteOutputSystemController) || (typeof globalThis !== 'undefined' && globalThis.FXRouteOutputSystemController) || null;
    return mod.fetchOutputSystemCatalog(force);
}

function measurementAreaFromCatalog() {
    /* Read-only measurement area: the single A/B selector owns the scope, so
     * the sweep menu shows it instead of offering its own channel chips. */
    const mod = outputSystemModule();
    const catalog = (state.outputSystem || {}).catalog;
    if (!mod || !catalog || typeof mod.measurementArea !== 'function') return null;
    return mod.measurementArea(catalog);
}

function measurementRepeatBlockedReason() {
    /* A one-sided area has no second side to compare: its opposite-side reason
     * would capture the same physical way again, so tell the user instead of
     * running a repeat that measures nothing new. */
    const area = measurementAreaFromCatalog();
    if (!area || area.repeat_supported !== false) return '';
    return area.repeat_note || `${area.label} is fed by one input only. Use a single sweep.`;
}

function syncMeasurementRepeatNote(lrActive, blockedReason) {
    /* A running repeat stays cancellable, so its reason never disables it. */
    if (elements.measurementRepeatStartBtn) {
        elements.measurementRepeatStartBtn.title = lrActive ? '' : blockedReason;
    }
    if (elements.measurementRepeatNote) {
        elements.measurementRepeatNote.textContent = lrActive || !blockedReason
            ? 'Repeated L/R sweeps for more precision.'
            : blockedReason;
    }
}

async function ensureMeasurementAreaCatalog() {
    if ((state.outputSystem || {}).catalog) {
        renderMeasurementArea();
        return state.outputSystem.catalog;
    }
    const catalog = await fetchOutputSystemCatalog();
    renderMeasurementArea();
    return catalog;
}

function renderMeasurementArea() {
    ensureOutputSystemBoxes();
    const area = measurementAreaFromCatalog();
    if (elements.measurementAreaIndicator) {
        elements.measurementAreaIndicator.textContent = area ? area.label : 'Global';
    }
    if (elements.measurementAreaNote) {
        elements.measurementAreaNote.textContent = area
            ? area.note
            : 'Whole system: Global plus every area bank stay audible for this sweep.';
    }
    if (elements.measurementSweepStartBtn) {
        elements.measurementSweepStartBtn.disabled = !!state.measurement.startInFlight || area?.available === false;
    }
    syncSweepSideRow(area);
}

function syncSweepSideRow(area) {
    /* The side chips only narrow a stereo area (an L/R pair bank or Global)
     * to one side for the single sweep. Mono areas hide the row and always
     * sweep both inputs at once. */
    const row = elements.measurementSweepSideRow;
    if (!row) return;
    const resolved = area || measurementAreaFromCatalog();
    const sides = Array.isArray(resolved?.sides) ? resolved.sides : [];
    row.classList.toggle('hidden', sides.length === 0);
    if (!sides.includes(state.measurement.sweepSide)) {
        state.measurement.sweepSide = 'stereo';
    }
    const active = state.measurement.sweepSide || 'stereo';
    row.querySelectorAll('[data-sweep-side]').forEach((button) => {
        const selected = button.getAttribute('data-sweep-side') === active;
        button.classList.toggle('is-active', selected);
        button.setAttribute('aria-pressed', selected ? 'true' : 'false');
        button.disabled = !!state.measurement.startInFlight;
    });
    MeasurementFlows.syncSpeakerAlignButton();
}

function outputSystemDevice() {
    const catalog = state.outputSystem.catalog;
    return (catalog && catalog.device) || { key: '', channels: 0, routing: {} };
}

function renderOutputSystemSection() {
    ensureOutputSystemBoxes();
    const mod = outputSystemModule();
    const catalog = state.outputSystem.catalog;
    if (!mod || !catalog) {
        if (elements.settingsOutputModeHint) elements.settingsOutputModeHint.textContent = 'Output configuration unavailable.';
        if (elements.settingsOutputModeSelect) elements.settingsOutputModeSelect.disabled = true;
        if (elements.settingsModeGroup) elements.settingsModeGroup.classList.add('hidden');
        if (elements.settingsCrossoverGroup) elements.settingsCrossoverGroup.classList.add('hidden');
        if (elements.settingsCrossoverSelect) elements.settingsCrossoverSelect.disabled = true;
        if (elements.settingsRoutingGrid) elements.settingsRoutingGrid.innerHTML = '';
        if (elements.osTopology) elements.osTopology.textContent = '';
        if (elements.osFeedback) elements.osFeedback.innerHTML = '';
        return;
    }
    const device = outputSystemDevice();
    const mode = catalog.active_mode || 'stereo';
    const modeConfig = catalog.modes[mode] || {};
    const busy = state.outputSystem.busy;
    if (elements.settingsModeGroup) {
        elements.settingsModeGroup.classList.toggle('hidden', !mod.modeSelectorVisible(device.channels || 0));
    }
    if (elements.settingsCrossoverGroup) {
        elements.settingsCrossoverGroup.classList.toggle('hidden', !mod.modeSelectorVisible(device.channels || 0));
    }
    mod.renderModeSelect(elements.settingsOutputModeSelect, catalog, mode);
    if (elements.settingsOutputModeSelect) elements.settingsOutputModeSelect.disabled = busy;
    if (elements.settingsCrossoverSelect) {
        elements.settingsCrossoverSelect.value = modeConfig.crossover_enabled ? 'on' : 'off';
        elements.settingsCrossoverSelect.disabled = busy;
    }
    const assignments = (device.routing && device.routing[mode]) || [];
    mod.renderRoutingGrid(elements.settingsRoutingGrid, catalog, mode, assignments, device.channels || 0, busy);
    const topology = modeConfig.topology || {};
    if (elements.osTopology) {
        elements.osTopology.textContent = mod.topologySummary(topology);
    }
    if (elements.settingsOutputModeHint) {
        elements.settingsOutputModeHint.textContent = `${mod.modeLabel(mode)} · ${device.channels || 0} hardware outputs`;
    }
    if (elements.settingsRoutingHint) {
        elements.settingsRoutingHint.textContent = 'Assign a role to each hardware output. Off leaves an output silent.';
    }
}

async function applyOutputSystemMutation(kind, fields, successMessage, options = {}) {
    // Output-system orchestration lives in static/output_system_controller.js
    // (window.FXRouteOutputSystemController). Thin wrapper keeps existing
    // call sites and the mutation fan-out order unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteOutputSystemController) || (typeof globalThis !== 'undefined' && globalThis.FXRouteOutputSystemController) || null;
    return mod.applyOutputSystemMutation(kind, fields, successMessage, options);
}


function crossoverModule() {
    return (typeof window !== 'undefined' && window.FXRouteCrossover) || null;
}

async function fetchCrossoverResponse() {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.fetchCrossoverResponse();
}

function renderCrossoverTile() {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.renderCrossoverTile();
}

function familyLabel(family) {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.familyLabel(family);
}

function collectCrossoverWayMutation() {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.collectCrossoverWayMutation();
}

async function saveCrossoverWay() {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.saveCrossoverWay();
}

async function maybeApplyCrossoverStarters() {
    // Crossover tile UI lives in static/crossover_ui.js
    // (window.FXRouteCrossoverUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverUI) || null;
    return mod.maybeApplyCrossoverStarters();
}

async function saveAudioSourceSelection(mode, inputKey = '') {
    const nextMode = ['external-input', 'bluetooth-input'].includes(mode) ? mode : 'app-playback';
    state.settings.sourceMode.pending = true;
    state.settings.sourceMode.mode = nextMode;
    renderSettingsPanel();
    try {
        const resp = await fetch('/api/audio/source-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: nextMode, inputKey: inputKey || null }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save source mode');
        state.settings.sourceMode = {
            mode: data.mode || 'app-playback',
            modes: Array.isArray(data.modes) ? data.modes : [],
            default_input: data.default_input || null,
            selected_input: data.selected_input || null,
            current_input: data.current_input || null,
            inputs: Array.isArray(data.inputs) ? data.inputs : [],
            bluetooth: data.bluetooth || {},
            notes: Array.isArray(data.notes) ? data.notes : [],
            pending: false,
        };
        renderSettingsPanel();
        const successMessage = nextMode === 'external-input'
            ? 'External input mode enabled'
            : (nextMode === 'bluetooth-input' ? 'Bluetooth input mode enabled' : 'App playback mode enabled');
        showToast(successMessage, 'success');
    } catch (error) {
        state.settings.sourceMode.pending = false;
        renderSettingsPanel();
        showToast(error.message || 'Failed to save source mode', 'error');
        void fetchAudioSourceOverview();
    }
}


async function fetchAudioSourceOverview() {
    try {
        const resp = await fetch('/api/audio/source-mode');
        if (!resp.ok) throw new Error('Failed to fetch source mode');
        const data = await resp.json();
        state.settings.sourceMode = {
            mode: data.mode || 'app-playback',
            modes: Array.isArray(data.modes) ? data.modes : [],
            default_input: data.default_input || null,
            selected_input: data.selected_input || null,
            current_input: data.current_input || null,
            inputs: Array.isArray(data.inputs) ? data.inputs : [],
            bluetooth: data.bluetooth || {},
            notes: Array.isArray(data.notes) ? data.notes : [],
            pending: false,
        };
        renderSettingsPanel();
    } catch (e) {
        state.settings.sourceMode = {
            mode: 'app-playback',
            modes: [{ key: 'app-playback', label: 'App playback', selectable: true }],
            default_input: null,
            selected_input: null,
            current_input: null,
            inputs: [],
            bluetooth: {},
            notes: [e.message || 'Failed to fetch source mode'],
            pending: false,
        };
        renderSettingsPanel();
    }
}

// ---------------------------------------------------------------------------
// Source model (three separate concepts)
// ---------------------------------------------------------------------------
// __visibleTab    = which tab the user is looking at ('radio','spotify','library','effects')
// __footerSource  = which source the footer displays ('local', 'spotify' or 'qobuz')
// Transport/volume controls route based on the effective playback owner, not the visible tab.
// Footer renders based on __footerSource. Tab switch does NOT change footer or playback.

window.__visibleTab = 'radio';
window.__footerSource = 'local';
window.__qobuzLastData = null;
window.__streamingSeeking = false;

function isStreamingFooterSource(source) {
    return source === 'spotify' || source === 'qobuz';
}

// Last normalized state for the external renderer (Spotify/Qobuz) currently
// shown in the footer, or null when a native source (local/radio/tidal) owns
// the footer. The footer renderer never branches on the provider identity
// beyond this data lookup.
function streamingFooterData() {
    if (window.__footerSource === 'spotify') return window.__spotifyLastData;
    if (window.__footerSource === 'qobuz') return window.__qobuzLastData;
    return null;
}

function clearLibraryImportFeedbackIfIdle() {
    const uploadActive = state.upload && state.upload.status === 'uploading';
    const downloadActive = state.download && ['starting', 'downloading'].includes(state.download.status);
    if (uploadActive || downloadActive) return;

    state.upload = null;
    if (state.download && ['complete', 'error', 'cancelled'].includes(state.download.status)) {
        state.download = null;
        lastDownloadStatus = 'idle';
    }
    updateDownloadUI();
}

function closeLibraryImportPanel() {
    if (!elements.libraryImportPanel || elements.libraryImportPanel.classList.contains('hidden')) {
        if (elements.toggleImportBtn) {
            elements.toggleImportBtn.textContent = 'Import';
            elements.toggleImportBtn.setAttribute('aria-expanded', 'false');
        }
        return;
    }
    const searchWrap = elements.librarySearchInput ? elements.librarySearchInput.closest('.library-search-wrap') : null;
    const selectionToolbar = elements.selectAllTracksBtn ? elements.selectAllTracksBtn.closest('.library-selection-toolbar') : null;
    clearLibraryImportFeedbackIfIdle();
    resetUploadAreaSelection('upload-track-file');
    elements.libraryImportPanel.classList.add('hidden');
    if (searchWrap) searchWrap.classList.remove('hidden');
    if (selectionToolbar) selectionToolbar.classList.remove('hidden');
    if (elements.playlistSaveRow) updatePlaylistSaveRowVisibility();
    if (elements.toggleImportBtn) {
        elements.toggleImportBtn.textContent = 'Import';
        elements.toggleImportBtn.setAttribute('aria-expanded', 'false');
    }
}

function switchTab(tabId) {
    closeLibraryImportPanel();
    elements.tabs.forEach(t => {
        const active = t.dataset.tab === tabId;
        t.classList.toggle('active', active);
        t.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    elements.tabPanels.forEach(p => p.classList.toggle('active', p.id === `tab-${tabId}`));
    window.__visibleTab = tabId;
    window.FXRouteStreaming?.onTabVisible(tabId);
    if (tabId === 'effects') {
        requestSubwooferPreviewRedrawFromState();
    }
    if (tabId !== 'measurement') {
        void postRuntimeDebugSnapshot('ui-after-click-playback', { clickedTab: tabId });
    }
    highlightActiveTrack();
    if (tabId === 'spotify') {
        const d = window.__spotifyLastData;
        if (d) renderSpotify(d);
        startSpotifyPoll();
        void fetchSpotifyStatus().then(data => {
            handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
        }).catch(() => {});
    } else if (window.__footerSource !== 'spotify') {
        stopSpotifyPoll();
    }
    if (tabId === 'qobuz') {
        startQobuzPoll();
        void fetchQobuzStatus().then(data => {
            handleIncomingQobuzState(data, { renderFooter: true });
        }).catch(() => {});
    } else if (window.__footerSource !== 'qobuz' && window.__visibleTab !== 'qobuz' && getBackendFooterOwner() !== 'qobuz') {
        stopQobuzPoll();
    }
}

function getBackendFooterOwner(playback = state.playback) {
    // Single authoritative owner truth: the backend publishes playback_owner
    // on the playback broadcast after every successful source commit.
    // Provider status payloads deliver metadata/transport state only; their
    // playback_owner copies are never consulted here, so the footer never
    // has to choose between copies of different ages.
    const owner = playback?.playback_owner || null;
    if (owner === 'local' || owner === 'radio' || owner === 'tidal') return 'local';
    if (owner === 'spotify' || owner === 'qobuz') return owner;
    return null;
}

function getEffectivePlaybackControlSource() {
    const backendOwner = getBackendFooterOwner();
    // Transport follows the same stale-commit override as the footer itself:
    // with live qbzd playback and no live spotify playback, the controls
    // must drive qobuz even when the cached commit still names spotify.
    if (backendOwner === 'spotify' && qobuzPlayingOwnsFooter() && !spotifyPlayingOwnsFooter()) {
        return 'qobuz';
    }
    if (backendOwner) return backendOwner;
    if (spotifyPlayingOwnsFooter()) return 'spotify';
    if (localPlaybackHasFooterContext(state.playback) || localEndedPlaybackHasFooterContext(state.playback)) return 'local';
    if (spotifyPausedHasFooterContext()) return 'spotify';
    return isStreamingFooterSource(window.__footerSource) ? window.__footerSource : 'local';
}

function globalTogglePlayback() {
    const source = getEffectivePlaybackControlSource();
    if (source === 'spotify') {
        spotifyCommand('toggle');
    } else if (source === 'qobuz') {
        qobuzCommand('toggle');
    } else {
        togglePlayback();
    }
}

function globalPrevious() {
    const source = getEffectivePlaybackControlSource();
    if (source === 'spotify') {
        spotifyCommand('previous');
    } else if (source === 'qobuz') {
        qobuzCommand('previous');
    } else {
        previousInQueue();
    }
}

function globalNext() {
    const source = getEffectivePlaybackControlSource();
    if (source === 'spotify') {
        spotifyCommand('next');
    } else if (source === 'qobuz') {
        qobuzCommand('next');
    } else {
        nextInQueue();
    }
}

function globalSeekChange() {
    if (isStreamingFooterSource(window.__footerSource)) {
        const streamingData = streamingFooterData();
        if (streamingData && streamingData.duration) window.__streamingSeeking = true;
    }
}

function globalSeekEnd() {
    if (isStreamingFooterSource(window.__footerSource)) {
        window.__streamingSeeking = false;
        const streamingData = streamingFooterData();
        if (streamingData && streamingData.duration) {
            const posSec = (parseFloat(elements.seekSlider.value) / 1000) * streamingData.duration;
            if (window.__footerSource === 'qobuz') qobuzSeek(posSec);
            else spotifySeek(posSec);
        }
    }
}

function syncPlaybackFooterSpace() {
    playbackFooterSpaceFrame = null;
    const bar = elements.playbackBar;
    if (!bar || getComputedStyle(bar).display === 'none') {
        document.documentElement.style.setProperty('--playback-footer-space', '1.5rem');
        return;
    }
    const rect = bar.getBoundingClientRect();
    const bottomInset = Math.max(0, window.innerHeight - rect.bottom);
    const clearance = Math.ceil(rect.height + bottomInset + 16);
    document.documentElement.style.setProperty('--playback-footer-space', `${clearance}px`);
}

function schedulePlaybackFooterSpaceSync() {
    if (playbackFooterSpaceFrame !== null) return;
    playbackFooterSpaceFrame = requestAnimationFrame(syncPlaybackFooterSpace);
}

function initPlaybackFooterLayout() {
    if (!elements.playbackBar) return;
    if (typeof ResizeObserver === 'function') {
        playbackFooterResizeObserver = new ResizeObserver(schedulePlaybackFooterSpaceSync);
        playbackFooterResizeObserver.observe(elements.playbackBar);
    }
    window.addEventListener('resize', schedulePlaybackFooterSpaceSync);
    schedulePlaybackFooterSpaceSync();
}

function setFooterProgressState(available, readonly = false) {
    const showProgress = !!available;
    elements.playbackBar?.classList.toggle('progress-readonly', showProgress && !!readonly);
    elements.seekRow?.classList.toggle('hidden', !showProgress);
}

function showVolumeDisplayTemporarily() {
    const controls = elements.volumeSlider?.closest('.controls');
    if (!controls) return;
    controls.classList.add('is-adjusting-volume');
    clearTimeout(volumeDisplayTimer);
    volumeDisplayTimer = setTimeout(() => {
        controls.classList.remove('is-adjusting-volume');
        volumeDisplayTimer = null;
    }, 900);
}

function setupPlaybackControls() {
    if (!elements.btnPlayPause || !elements.volumeSlider) {
        console.error('Playback controls are missing in the DOM');
        return;
    }
    if (elements.footerShuffleBtn) elements.footerShuffleBtn.addEventListener('click', toggleFooterShuffle);
    if (elements.btnPrevious) elements.btnPrevious.addEventListener('click', globalPrevious);
    elements.btnPlayPause.addEventListener('click', globalTogglePlayback);
    if (elements.btnNext) elements.btnNext.addEventListener('click', globalNext);
    if (elements.sourcePrev) elements.sourcePrev.addEventListener('click', () => stepSourceSwitcher(-1));
    if (elements.sourceNext) elements.sourceNext.addEventListener('click', () => stepSourceSwitcher(1));
    if (elements.sourceSelect) elements.sourceSelect.addEventListener('change', (event) => {
        const key = event.target.value || '';
        const sourceMode = state.settings?.sourceMode || {};
        const entry = buildSourceSwitcherEntries(sourceMode).find((item) => item.key === key);
        if (entry) {
            activateSourceSwitcherEntry(entry);
        } else {
            renderSourceModeFooter();
        }
    });
    if (elements.footerLoopBtn) elements.footerLoopBtn.addEventListener('click', toggleFooterLoop);
    if (elements.btnClearQueue) elements.btnClearQueue.addEventListener('click', clearQueue);
    if (elements.trackFavoriteBtn) elements.trackFavoriteBtn.addEventListener('click', toggleCurrentTrackFavorite);
    window.addEventListener('fxroute:tidal-favorites', renderFooterFavoriteFromTidalChange);
    elements.volumeSlider.addEventListener('input', handleVolumeChange);
    if (elements.playbackCover) {
        elements.playbackCover.addEventListener('click', toggleCoverDetailCard);
        elements.playbackCover.addEventListener('keydown', event => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            toggleCoverDetailCard();
        });
    }
    if (elements.coverDetailBackdrop) elements.coverDetailBackdrop.addEventListener('click', closeCoverDetailCard);
    if (elements.coverDetailQueueList) {
        elements.coverDetailQueueList.addEventListener('click', (event) => {
            const row = event.target.closest('[data-queue-index]');
            if (!row) return;
            event.preventDefault();
            event.stopPropagation();
            playCoverQueueIndex(Number(row.dataset.queueIndex));
        });
        elements.coverDetailQueueList.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            const row = event.target.closest('[data-queue-index]');
            if (!row) return;
            event.preventDefault();
            event.stopPropagation();
            playCoverQueueIndex(Number(row.dataset.queueIndex));
        });
    }
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && isCoverDetailOpen()) closeCoverDetailCard();
    });
    elements.volumeSlider.addEventListener('change', (e) => {
        const sliderValue = parseInt(e.target.value, 10);
        const actualVolume = sliderVolumeToActualVolume(sliderValue);
        volumeGestureActive = false;
        queueVolumeSend(actualVolume, true);
    });
    updatePlaybackUI();
    renderLibraryModeButtons();
}

async function stopPlayback() {
    try {
        const resp = await fetch('/api/stop', { method: 'POST' });
        if (!resp.ok) throw new Error('Stop failed');
    } catch (e) {
        showToast('Failed to stop playback', 'error');
    }
}
function getTrackIdsInLibraryOrder(trackIds = []) {
    const known = new Set((state.library.tracks || []).map(track => track?.id).filter(Boolean));
    return [...new Set((trackIds || []).filter(id => id && known.has(id)))];
}
function getSelectedPlayableTrackIds() {
    return getTrackIdsInLibraryOrder(state.library.selectedTrackIds || []);
}
function getSelectedDownloadTrackIds() {
    return getTrackIdsInLibraryOrder(state.library.selectedTrackIds || []);
}
function renderFooterModeButtons() {
    const shuffleBtn = elements.footerShuffleBtn;
    const loopBtn = elements.footerLoopBtn;
    if (!shuffleBtn && !loopBtn) return;

    if (isStreamingFooterSource(window.__footerSource)) {
        const data = streamingFooterData() || {};
        const caps = data.capabilities || {};
        const hasMedia = !!(data.available && (data.title || data.artist || data.album || data.status !== 'Stopped'));
        const showShuffle = hasMedia && !!caps.shuffle;
        const showLoop = hasMedia && !!caps.loop;
        const transportInFlight = window.__footerSource === 'spotify' && _spotifyCommandInFlight;
        if (shuffleBtn) {
            shuffleBtn.classList.toggle('hidden', !showShuffle);
            shuffleBtn.classList.toggle('active', showShuffle && !!data.shuffle);
            shuffleBtn.disabled = !showShuffle || transportInFlight;
            shuffleBtn.setAttribute('aria-pressed', showShuffle && data.shuffle ? 'true' : 'false');
            shuffleBtn.title = data.shuffle ? 'Shuffle on' : 'Shuffle off';
        }
        if (loopBtn) {
            const loopMode = String(data.loop || 'none');
            const loopActive = loopMode !== 'none';
            loopBtn.classList.toggle('hidden', !showLoop);
            loopBtn.classList.toggle('active', showLoop && loopActive);
            loopBtn.disabled = !showLoop || transportInFlight;
            loopBtn.setAttribute('aria-pressed', showLoop && loopActive ? 'true' : 'false');
            loopBtn.textContent = loopMode === 'track' ? '↻¹' : '↻';
            loopBtn.title = loopMode === 'track' ? 'Repeat track' : (loopMode === 'playlist' ? 'Repeat playlist' : 'Repeat off');
        }
        return;
    }

    const track = state.playback.current_track;
    const nativeQueueActive = !!(track && (track.source === 'local' || track.source === 'tidal'));
    const queue = state.playback.queue || {};
    const hasActiveQueue = Number(queue.count || 0) > 1;
    const showShuffle = nativeQueueActive && hasActiveQueue;
    const showLoop = nativeQueueActive && hasActiveQueue;
    if (shuffleBtn) {
        shuffleBtn.classList.toggle('hidden', !showShuffle);
        shuffleBtn.classList.toggle('active', showShuffle && !!state.library.shuffle);
        shuffleBtn.disabled = !showShuffle || libraryModeRequestInFlight;
        shuffleBtn.setAttribute('aria-pressed', showShuffle && state.library.shuffle ? 'true' : 'false');
        shuffleBtn.title = state.library.shuffle ? 'Shuffle on' : 'Shuffle off';
    }
    if (loopBtn) {
        loopBtn.classList.toggle('hidden', !showLoop);
        loopBtn.classList.toggle('active', showLoop && !!state.library.loop);
        loopBtn.disabled = !showLoop || libraryModeRequestInFlight;
        loopBtn.setAttribute('aria-pressed', showLoop && state.library.loop ? 'true' : 'false');
        loopBtn.textContent = '↻';
        loopBtn.title = state.library.loop ? 'Repeat on' : 'Repeat off';
    }
}

function toggleFooterShuffle() {
    if (window.__footerSource === 'spotify') {
        void spotifyCommand('shuffle');
        return;
    }
    if (window.__footerSource === 'qobuz') {
        void qobuzCommand('shuffle');
        return;
    }
    void toggleLibraryShuffle();
}

function toggleFooterLoop() {
    if (window.__footerSource === 'spotify') {
        void spotifyCommand('loop');
        return;
    }
    if (window.__footerSource === 'qobuz') {
        void qobuzCommand('repeat');
        return;
    }
    void toggleLibraryLoop();
}

function renderLibraryModeButtons() {
    renderFooterModeButtons();
}

async function toggleLibraryShuffle() {
    if (libraryModeRequestInFlight) return;
    libraryModeRequestInFlight = true;
    renderLibraryModeButtons();
    try {
        const resp = await fetch('/api/playback/shuffle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !state.library.shuffle }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(formatTransitionErrorDetail(data.detail, 'Shuffle update failed'));
        }
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        updatePlaybackUI();
    } catch (e) {
        showToast(e.message || 'Failed to update shuffle', 'error');
    } finally {
        libraryModeRequestInFlight = false;
        renderLibraryModeButtons();
    }
}

async function toggleLibraryLoop() {
    if (libraryModeRequestInFlight) return;
    libraryModeRequestInFlight = true;
    renderLibraryModeButtons();
    try {
        const resp = await fetch('/api/playback/loop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !state.library.loop }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Loop update failed'));
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        updatePlaybackUI();
    } catch (e) {
        showToast(e.message || 'Failed to update loop', 'error');
    } finally {
        libraryModeRequestInFlight = false;
        renderLibraryModeButtons();
    }
}
async function togglePlayback() {
    if (playbackActionInFlight) return;
    const previousPlaying = !!state.playback.playing;
    const previousPaused = !!state.playback.paused;
    const previousEnded = !!state.playback.ended;
    const previousTrack = state.playback.current_track ? { ...state.playback.current_track } : null;
    const replayRadioTrack = !state.playback.current_track ? getLastRadioTrack() : null;
    const canTogglePause = !!state.playback.current_track && !!state.playback.current_file && !previousEnded;
    if (!canTogglePause) {
        if (replayRadioTrack) {
            state.playback.current_track = replayRadioTrack;
        } else if (!state.playback.current_track) {
            return;
        }
    }
    const requestId = ++pauseActionRequestId;
    playbackActionInFlight = true;
    if (canTogglePause) {
        state.playback.playing = previousPaused;
        state.playback.paused = previousPlaying;
    } else {
        state.playback.playing = true;
        state.playback.paused = false;
        state.playback.ended = false;
    }
    window.__footerSource = 'local';
    _spotifyPollGeneration++;
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/playback/toggle', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(formatTransitionErrorDetail(data.detail, 'Playback toggle failed'));
        }
        if (requestId !== pauseActionRequestId) return;
        playbackActionInFlight = false;
        if (data.playback) {
            mergePlaybackState(data.playback);
        } else {
            state.playback.playing = data.status === 'playing';
            state.playback.paused = data.status === 'paused';
        }
        updatePlaybackUI();
        if (state.playback.playing && state.playback.current_track?.source === 'radio') {
            void fetchMetadata();
        }
    } catch (e) {
        if (requestId !== pauseActionRequestId) return;
        state.playback.playing = previousPlaying;
        state.playback.paused = previousPaused;
        state.playback.ended = previousEnded;
        state.playback.current_track = previousTrack;
        playbackActionInFlight = false;
        updatePlaybackUI();
        showToast(e.message || 'Failed to toggle playback', 'error');
    }
}
function clampVolumeValue(value) {
    return Math.max(0, Math.min(100, value));
}

function sliderVolumeToActualVolume(sliderValue) {
    const normalized = clampVolumeValue(sliderValue) / 100;
    return Math.round(Math.pow(normalized, VOLUME_CURVE_GAMMA) * 100);
}

function actualVolumeToSliderValue(actualVolume) {
    const normalized = clampVolumeValue(actualVolume) / 100;
    if (normalized <= 0) return 0;
    return Math.round(Math.pow(normalized, 1 / VOLUME_CURVE_GAMMA) * 100);
}

function setRangeProgress(input, fraction) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.setRangeProgress(input, fraction);
}

function renderVolumeControlsFromActualVolume(actualVolume) {
    const sliderValue = actualVolumeToSliderValue(actualVolume);
    elements.volumeSlider.value = sliderValue;
    elements.volumeDisplay.textContent = `${sliderValue}%`;
    setRangeProgress(elements.volumeSlider, sliderValue / 100);
}

function setLocalVolume(sliderValue) {
    const clampedSliderValue = clampVolumeValue(sliderValue);
    const actualVolume = sliderVolumeToActualVolume(clampedSliderValue);
    state.playback.volume = actualVolume;
    elements.volumeSlider.value = clampedSliderValue;
    elements.volumeDisplay.textContent = `${clampedSliderValue}%`;
    setRangeProgress(elements.volumeSlider, clampedSliderValue / 100);
}
function queueVolumeSend(volume, immediate = false) {
    pendingVolume = volume;
    clearTimeout(volumeTimer);
    if (immediate) {
        void sendVolume();
        return;
    }
    volumeTimer = setTimeout(() => {
        void sendVolume();
    }, VOLUME_SEND_DEBOUNCE_MS);
}
function mergePlaybackState(data) {
    if (!data) return;
    const incomingSeq = typeof data._seq === 'number' ? data._seq : null;
    const currentSeq = typeof state.playback?._seq === 'number' ? state.playback._seq : null;
    if (incomingSeq !== null && currentSeq !== null && incomingSeq < currentSeq) {
        footerDebug('ignore-stale-playback-state', { incomingSeq, currentSeq });
        return;
    }
    const nextPlayback = { ...data };
    const previousRadioMetadata = state.playback?.radio_metadata;
    if (nextPlayback.paused && nextPlayback.radio_metadata && previousRadioMetadata
        && nextPlayback.radio_metadata.track_id === previousRadioMetadata.track_id) {
        nextPlayback.radio_metadata = {
            ...nextPlayback.radio_metadata,
            progress_seconds: previousRadioMetadata.progress_seconds,
        };
    }
    const remoteVolume = typeof nextPlayback.volume === 'number' ? nextPlayback.volume : null;
    if (remoteVolume !== null) {
        delete nextPlayback.volume;
    }
    state.playback = { ...state.playback, ...nextPlayback };
    rememberLastRadioTrack(state.playback.current_track);
    if (remoteVolume !== null) {
        applyRemoteVolume(remoteVolume);
    }
}
function rememberLastRadioTrack(track) {
    if (!track || track.source !== 'radio' || !track.url) return;
    lastRadioTrack = { ...track };
}
function getLastRadioTrack() {
    return lastRadioTrack ? { ...lastRadioTrack } : null;
}
function buildOptimisticSingleTrackQueue(track) {
    return {
        active: false,
        index: 0,
        count: 1,
        mode: state.playback.queue?.mode || 'app_replace',
        tracks: track ? [track] : [],
        loop: !!state.library.loop,
        shuffle: false,
    };
}
function footerSingleTrackStartLockActive(playback = state.playback) {
    const pending = pendingFooterSingleTrackStart;
    if (!pending) return false;
    if (pending.expiresAt && Date.now() > pending.expiresAt) {
        pendingFooterSingleTrackStart = null;
        return false;
    }
    const track = playback?.current_track;
    return !!(track && track.source === 'local' && track.id === pending.trackId);
}

function activeLocalPlaybackBlocksSpotifyOwnership(playback = state.playback) {
    const track = playback?.current_track;
    // Every MPV source (local/radio/tidal) blocks a stale Spotify takeover:
    // TIDAL rides the same native engine, so live TIDAL playback keeps the
    // footer even while MPRIS still reports a stale Spotify Playing edge.
    if (!(track && (track.source === 'local' || track.source === 'radio' || track.source === 'tidal'))) return false;
    return !!(playback?.playing && !playback?.ended);
}

function footerSingleTrackStartLockSatisfied(playback) {
    const pending = pendingFooterSingleTrackStart;
    if (!pending) return false;
    const track = playback?.current_track;
    return !!(
        track
        && track.source === 'local'
        && track.id === pending.trackId
        && (playback?.playing || playback?.paused || playback?.current_file)
    );
}

function clearPendingFooterSingleTrackStart(requestId = null) {
    if (!pendingFooterSingleTrackStart) return;
    if (requestId !== null && pendingFooterSingleTrackStart.requestId !== requestId) return;
    pendingFooterSingleTrackStart = null;
}
function clearPendingOptimisticTrack(requestId = null) {
    if (!pendingOptimisticTrack) return;
    if (requestId !== null && pendingOptimisticTrack.requestId !== requestId) return;
    pendingOptimisticTrack = null;
}
function getLibraryPlaybackContext(playback = state.playback) {
    const currentTrack = playback?.current_track;
    const queue = playback?.queue || {};
    if (!currentTrack || currentTrack.source !== 'local') {
        return null;
    }
    return {
        shuffle: !!queue.shuffle,
        loop: !!queue.loop,
    };
}
function syncLibraryStateFromPlaybackContext(force = false) {
    const context = getLibraryPlaybackContext();
    const signature = JSON.stringify(context || { shuffle: false, loop: false });
    const changed = signature !== lastLibraryPlaybackContextSignature;
    lastLibraryPlaybackContextSignature = signature;
    if (!force && !changed) return;
    if (playbackActionInFlight) return;

    if (!context) {
        if (state.library.shuffle || state.library.loop) {
            state.library.shuffle = false;
            state.library.loop = false;
            renderLibraryModeButtons();
        }
        return;
    }

    state.library.shuffle = context.shuffle;
    state.library.loop = context.loop;
    renderLibraryModeButtons();
}
function applyRemoteVolume(remoteVolume) {
    const matchesOptimistic = optimisticVolume !== null && remoteVolume === optimisticVolume;
    const shouldHoldRemoteVolume = volumeGestureActive || volumeRequestInFlight || pendingVolume !== null || Date.now() < volumeSyncGraceUntil;
    if (shouldHoldRemoteVolume && !matchesOptimistic) {
        return;
    }
    lastConfirmedVolume = remoteVolume;
    state.playback.volume = remoteVolume;
    if (matchesOptimistic && !volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
        optimisticVolume = null;
    }
}
async function sendVolume() {
    if (volumeRequestInFlight || pendingVolume === null) return;
    volumeRequestInFlight = true;
    while (pendingVolume !== null) {
        const nextVolume = pendingVolume;
        pendingVolume = null;
        if (nextVolume === lastConfirmedVolume) {
            optimisticVolume = null;
            continue;
        }
        try {
            const resp = await fetch('/api/volume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ volume: nextVolume }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Volume change failed');
            lastConfirmedVolume = typeof data.volume === 'number' ? data.volume : nextVolume;
            state.playback.volume = lastConfirmedVolume;
            volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
            if (!volumeGestureActive && pendingVolume === null) {
                optimisticVolume = null;
            }
        } catch (e) {
            pendingVolume = null;
            volumeGestureActive = false;
            optimisticVolume = null;
            showToast(e.message || 'Failed to set volume', 'error');
            break;
        }
    }
    volumeRequestInFlight = false;
    updatePlaybackUI();
}

async function handleVolumeChange(e) {
    const sliderValue = parseInt(e.target.value, 10);
    const actualVolume = sliderVolumeToActualVolume(sliderValue);
    volumeGestureActive = true;
    optimisticVolume = actualVolume;
    volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
    setLocalVolume(sliderValue);
    showVolumeDisplayTemporarily();
    queueVolumeSend(actualVolume);
}
// Metadata polling for radio ICY tags
let metadataPollTimer = null;
let sampleratePollTimer = null;
let samplerateBurstPollTimers = [];
let lastSampleratePlaybackSignature = null;
let peakStatusPollTimer = null;
// Poll timers keep running while the tab is hidden but skip their network
// work, so a hidden tab stops hammering /api/status until it is visible again.
function isPageHidden() {
    return document.hidden === true;
}
function startMetadataPolling() {
    if (metadataPollTimer !== null) return;
    metadataPollTimer = setInterval(fetchMetadata, 10000);
}
function stopMetadataPolling() {
    if (metadataPollTimer === null) return;
    clearInterval(metadataPollTimer);
    metadataPollTimer = null;
}
function startPeakStatusPolling() {
    if (peakStatusPollTimer !== null) return;
    peakStatusPollTimer = setInterval(fetchMetadata, PEAK_STATUS_POLL_INTERVAL_MS);
}
function stopPeakStatusPolling() {
    if (peakStatusPollTimer === null) return;
    clearInterval(peakStatusPollTimer);
    peakStatusPollTimer = null;
}
function startSampleratePolling() {
    if (sampleratePollTimer !== null) return;
    sampleratePollTimer = setInterval(fetchSamplerateStatus, SAMPLERATE_POLL_INTERVAL_MS);
}
function stopSampleratePolling() {
    if (sampleratePollTimer === null) return;
    clearInterval(sampleratePollTimer);
    sampleratePollTimer = null;
}
function triggerSamplerateBurstPolling() {
    samplerateBurstPollTimers.forEach(timer => clearTimeout(timer));
    samplerateBurstPollTimers = [];
    SAMPLERATE_BURST_POLL_DELAYS_MS.forEach(delayMs => {
        const timer = setTimeout(async () => {
            try {
                await fetchSamplerateStatus();
            } finally {
                samplerateBurstPollTimers = samplerateBurstPollTimers.filter(id => id !== timer);
            }
        }, delayMs);
        samplerateBurstPollTimers.push(timer);
    });
}
async function fetchMetadata() {
    if (isPageHidden()) return;
    if (!state.playback.playing && !state.playback.paused && !isStreamingFooterSource(window.__footerSource)) return;
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) return;
        const data = await resp.json();
        let needsUiRefresh = false;
        if (data.metadata && Object.keys(data.metadata).length > 0) {
            const meta = data.metadata;
            const title = (meta['icy-title'] || meta['title'] || '').trim();
            if (title && state.playback.current_track && state.playback.current_track.source === 'radio') {
                state.playback.live_title = title;
                needsUiRefresh = true;
            }
        }
        if (data.output_peak_warning) {
            state.playback.output_peak_warning = data.output_peak_warning;
            needsUiRefresh = true;
        }
        // Update volume from state if changed
        if (data.volume !== undefined) {
            applyRemoteVolume(data.volume);
            if (!volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
                renderVolumeControlsFromActualVolume(state.playback.volume);
            }
        }
        if (data.current_track) {
            // The owner rides along so the peak poll heals a stale footer
            // owner (e.g. a missed playback broadcast after a TIDAL start);
            // VU/peak gating resolves the footer from this field.
            mergePlaybackState({ current_track: data.current_track, playing: data.playing, paused: data.paused, playback_owner: data.playback_owner, live_title: data.live_title, radio_metadata: data.radio_metadata, stream_info: data.stream_info });
            syncFooterOwnershipFromPlayback(data);
            needsUiRefresh = true;
        }
        if (needsUiRefresh) {
            updatePlaybackUI();
        }
    } catch (e) {}
}
function renderSamplerateUI() {
    if (!elements.samplerateStatus) return;
    // A streaming source (Spotify/Qobuz) owns the footer pill exclusively via
    // updateFooterForStreamingOwner. The general samplerate poll runs
    // independently (every SAMPLERATE_POLL_INTERVAL_MS) and must never
    // overwrite the provider's quality line with the bare hardware rate — that
    // is the Qobuz footer flicker between "FLAC · 16bit · 44.1kHz" and
    // "44.1kHz". No state is cached here: this path simply does not own the
    // pill while a streaming source does.
    if (isStreamingFooterSource(window.__footerSource)) {
        footerDebug('samplerate-ui-streaming-owner', {
            skipped: true,
            owner: window.__footerSource,
        });
        return;
    }
    // Keep source codec/bitrate facts, but the kHz value always describes the
    // effective graph/hardware output rate.
    const activeSource = state.playback.current_track?.source;
    if (activeSource === 'radio' || activeSource === 'local' || activeSource === 'tidal') {
        const streamLine = formatRadioStreamLine(state.playback.stream_info, state.samplerate?.active_rate);
        if (streamLine) {
            elements.samplerateStatus.textContent = streamLine;
            elements.samplerateStatus.classList.remove('hidden');
        } else {
            elements.samplerateStatus.textContent = '';
            elements.samplerateStatus.classList.add('hidden');
        }
        return;
    }
    const samplerate = state.samplerate || {};
    if (!samplerate.available || !samplerate.active_rate) {
        elements.samplerateStatus.textContent = 'Auto';
        elements.samplerateStatus.classList.add('hidden');
        return;
    }
    // The footer badge always shows the resolved/active hardware rate only;
    // the policy mode (Auto/Fixed) never belongs into this badge.
    elements.samplerateStatus.textContent = formatRateKhz(samplerate.active_rate);
    elements.samplerateStatus.classList.remove('hidden');
}

// Favorite hearts render as one inline SVG instead of the Unicode hearts
// (U+2665 / U+2661). In some browser/OS combinations those fall back to a
// colour-emoji font, which paints an active heart red no matter what the
// button's CSS colour says. The SVG is painted from currentColor, so the
// existing muted / accent button states stay the single source of truth.
function favoriteHeartSvg() {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.favoriteHeartSvg();
}

function renderTrackFavoriteButton(track = state.playback.current_track) {
    const button = elements.trackFavoriteBtn;
    if (!button) return;
    const source = track?.source;
    const isLocal = source === 'local';
    const isTidal = source === 'tidal';
    const hasId = !!(track && track.id);
    const available = !!((isLocal || isTidal) && hasId);
    button.classList.toggle('hidden', !available);
    if (!available) {
        button.disabled = true;
        button.innerHTML = favoriteHeartSvg();
        button.classList.remove('active');
        button.setAttribute('aria-pressed', 'false');
        return;
    }
    const inFlight = trackFavoriteRequestInFlight || tidalFavoriteRequestInFlight;
    if (isTidal) {
        // The footer heart favorites the current TIDAL track through the same
        // canonical state/API as the TIDAL tab. While the ids are still
        // loading the button stays disabled so it never guesses the state;
        // the fxroute:tidal-favorites event re-renders it once they arrive.
        const ready = !!(window.FXRouteStreaming && window.FXRouteStreaming.tidalFavoritesReady()
            && window.FXRouteStreaming.isTidalFavorite);
        const favorite = ready ? window.FXRouteStreaming.isTidalFavorite('tracks', track.id) : false;
        button.disabled = inFlight || !ready;
        button.innerHTML = favoriteHeartSvg();
        button.classList.toggle('active', favorite);
        button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
        button.setAttribute('aria-label', favorite ? 'Remove track from favorites' : 'Add track to favorites');
        button.title = favorite ? 'Remove track from favorites' : 'Add track to favorites';
        if (!ready) {
            const streaming = window.FXRouteStreaming;
            if (streaming && streaming.ensureTidalFavoritesLoaded) {
                void streaming.ensureTidalFavoritesLoaded().then(() => renderTrackFavoriteButton(state.playback.current_track));
            }
        }
        return;
    }
    // Local library track favorite (unchanged native path).
    button.disabled = inFlight;
    const favorite = !!track.favorite;
    button.innerHTML = favoriteHeartSvg();
    button.classList.toggle('active', favorite);
    button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
    button.setAttribute('aria-label', favorite ? 'Remove track from favorites' : 'Add track to favorites');
    button.title = favorite ? 'Remove track from favorites' : 'Add track to favorites';
}

function updateTrackFavoriteCaches(trackId, favorite) {
    const apply = (track) => {
        if (track && track.id === trackId) track.favorite = !!favorite;
    };
    apply(state.playback.current_track);
    (state.library.tracks || []).forEach(apply);
    (state.library.albumDetail?.tracks || []).forEach(apply);
}

function findTrackById(trackId) {
    if (!trackId) return null;
    if (state.playback.current_track?.id === trackId) return state.playback.current_track;
    return (state.library.albumDetail?.tracks || []).find(track => track?.id === trackId)
        || (state.library.tracks || []).find(track => track?.id === trackId)
        || null;
}

function syncTrackFavoriteRowButtons(trackId = null) {
    document.querySelectorAll('.track-row-favorite[data-track-favorite], .track-fav[data-track-favorite]').forEach(button => {
        const id = button.dataset.trackFavorite || '';
        if (trackId && id !== trackId) return;
        const track = findTrackById(id);
        const favorite = !!track?.favorite;
        button.innerHTML = favoriteHeartSvg();
        button.classList.toggle('active', favorite);
        button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
        button.setAttribute('aria-label', favorite ? 'Remove track from favorites' : 'Add track to favorites');
        button.title = favorite ? 'Remove from favorites' : 'Add to favorites';
        button.disabled = trackFavoriteRequestInFlight;
    });
}

function bindTrackFavoriteRowButtons(root) {
    root?.querySelectorAll('.track-row-favorite[data-track-favorite], .track-fav[data-track-favorite]').forEach(button => {
        button.addEventListener('click', async (event) => {
            event.preventDefault();
            event.stopPropagation();
            await toggleTrackFavoriteById(button.dataset.trackFavorite || '');
        });
    });
}

async function toggleTrackFavoriteById(trackId) {
    const track = findTrackById(trackId);
    if (!track || !track.id || trackFavoriteRequestInFlight) return;
    const nextFavorite = !track.favorite;
    trackFavoriteRequestInFlight = true;
    renderTrackFavoriteButton(state.playback.current_track);
    syncTrackFavoriteRowButtons();
    try {
        const resp = await fetch(`/api/tracks/${encodeURIComponent(track.id)}/favorite`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorite: nextFavorite }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to update track favorite');
        updateTrackFavoriteCaches(track.id, !!data.favorite);
        renderTrackFavoriteButton(state.playback.current_track);
        syncTrackFavoriteRowButtons(track.id);
    } catch (error) {
        showToast(error.message || 'Failed to update track favorite', 'error');
    } finally {
        trackFavoriteRequestInFlight = false;
        renderTrackFavoriteButton(state.playback.current_track);
        syncTrackFavoriteRowButtons();
    }
}

function renderFooterFavoriteFromTidalChange() {
    // The footer heart mirrors the current TIDAL track favorite. Called on
    // every canonical favorites change so the footer never drifts from the
    // state the TIDAL tab/detail rows use.
    renderTrackFavoriteButton(state.playback.current_track);
}

async function toggleTidalFooterFavorite(track) {
    const streaming = window.FXRouteStreaming;
    if (!streaming || !streaming.toggleTidalFavorite || !track?.id || tidalFavoriteRequestInFlight) return;
    if (!streaming.tidalFavoritesReady()) {
        // State not loaded yet — request it and let the state render path
        // re-enable the button; do not guess a half-hearted toggle.
        void streaming.ensureTidalFavoritesLoaded().then(() => renderTrackFavoriteButton(state.playback.current_track));
        return;
    }
    tidalFavoriteRequestInFlight = true;
    renderTrackFavoriteButton(track);
    try {
        await streaming.toggleTidalFavorite('tracks', track.id);
    } catch (error) {
        showToast((error && error.message) || 'Failed to update favorite', 'error');
    } finally {
        tidalFavoriteRequestInFlight = false;
        // Re-derive from canonical state: on success the toggle flipped the
        // ids, on failure they are unchanged, so the button never shows a
        // stale optimistic value.
        renderTrackFavoriteButton(state.playback.current_track);
    }
}

async function toggleCurrentTrackFavorite() {
    const track = state.playback.current_track;
    if (!track || !track.id) return;
    if (track.source === 'tidal') {
        await toggleTidalFooterFavorite(track);
        return;
    }
    if (track.source !== 'local') return;
    await toggleTrackFavoriteById(track.id);
}

function meterLitCount(db, segmentCount) {
    // A missing sample (null/undefined/'') is not 0 dB: Number(null) is 0
    // and would light the meter full-scale on every stale snapshot.
    if (db === null || db === undefined || db === '') return 0;
    const value = Number(db);
    if (!Number.isFinite(value)) return 0;
    const normalized = Math.max(0, Math.min(1, (value + 60) / 60));
    if (normalized <= 0) return 0;
    return Math.max(1, Math.min(segmentCount, Math.round(normalized * segmentCount)));
}

function responsiveMeterSegmentCount() {
    if (window.matchMedia('(max-width: 700px)').matches) return 6;
    if (window.matchMedia('(max-width: 1180px)').matches) return 8;
    return 12;
}

function renderMeterChannel(container, db, detected) {
    if (!container) return;
    const segments = Array.from(container.querySelectorAll('i'));
    const visibleCount = Math.min(segments.length, responsiveMeterSegmentCount());
    const visibleSegments = segments.slice(0, visibleCount);
    const lit = meterLitCount(db, visibleCount);
    segments.forEach((segment, index) => {
        const visible = index < visibleCount;
        segment.classList.toggle('meter-segment-hidden', !visible);
        if (!visible) {
            segment.classList.remove('is-lit', 'is-warn', 'is-hot', 'is-peak');
            return;
        }
        const isLit = index < lit;
        segment.classList.toggle('is-lit', isLit);
        segment.classList.toggle('is-warn', isLit && index >= Math.max(0, visibleCount - 3));
        segment.classList.toggle('is-hot', isLit && index >= Math.max(0, visibleCount - 1));
        segment.classList.toggle('is-peak', !!detected && index === visibleSegments.length - 1);
    });
}

// Last valid VU level: a single missing/invalid sample (stale poll,
// dropped WS frame, monitor rearm gap) must not blank the meter. The cache
// holds only the slow VU level, never the fast peak flags.
let lastValidVuSnapshot = null;
const VU_HOLDOVER_MS = 2000;

function isFiniteVuDb(value) {
    if (value === null || value === undefined || value === '') return false;
    return Number.isFinite(Number(value));
}

function rememberValidVu(warning, active) {
    if (!active || !warning?.available || warning?.vu_fresh !== true) return;
    if (!isFiniteVuDb(warning.vu_db) || !isFiniteVuDb(warning.vu_db_l) || !isFiniteVuDb(warning.vu_db_r)) return;
    lastValidVuSnapshot = {
        vu_db: Number(warning.vu_db),
        vu_db_l: Number(warning.vu_db_l),
        vu_db_r: Number(warning.vu_db_r),
        at: Date.now(),
    };
}

function heldVuSnapshot() {
    if (!lastValidVuSnapshot) return null;
    if (Date.now() - lastValidVuSnapshot.at > VU_HOLDOVER_MS) {
        lastValidVuSnapshot = null;
        return null;
    }
    return lastValidVuSnapshot;
}

function renderStereoMeter(warning, active) {
    if (!elements.playbackMeter) return;
    const playbackActive = !!active;
    rememberValidVu(warning, playbackActive);
    const liveFresh = !!warning?.available && warning?.vu_fresh === true && playbackActive;
    const held = !liveFresh && playbackActive ? heldVuSnapshot() : null;
    elements.playbackMeter.classList.toggle('is-active', liveFresh || !!held);
    elements.playbackMeter.classList.toggle('is-peak', !!(warning?.detected_l || warning?.detected_r || warning?.detected));
    renderMeterChannel(elements.meterLeft, liveFresh ? warning?.vu_db_l : (held ? held.vu_db_l : null), liveFresh && !!warning?.detected_l);
    renderMeterChannel(elements.meterRight, liveFresh ? warning?.vu_db_r : (held ? held.vu_db_r : null), liveFresh && !!warning?.detected_r);
}

function formatOutputLevelBadgeDb(level) {
    const rounded = Math.round(Number(level));
    if (!Number.isFinite(rounded)) return '';
    const sign = rounded < 0 ? '-' : '';
    const abs = Math.abs(rounded);
    const digits = abs < 10 ? `0${abs}` : String(abs);
    return `${sign}${digits} dB`;
}

function renderPeakWarningBadge(activeOverride = null) {
    const warning = state.playback.output_peak_warning || {};
    const title = warning.target?.description || warning.target?.source_name || 'DSP output monitor';
    const vuDb = isFiniteVuDb(warning.vu_db) ? Number(warning.vu_db) : null;
    const playbackActive = activeOverride === null ? isFooterSignalActive() : !!activeOverride;
    const showPeak = !!warning.detected && playbackActive;
    const liveShowVu = !!warning.available && warning.vu_fresh === true
        && playbackActive && vuDb !== null;
    rememberValidVu(warning, playbackActive);
    // Single invalid sample while playing: hold the last valid dB text
    // instead of hiding the badge. Peak keeps its live-only fallback.
    const held = !liveShowVu && playbackActive && !showPeak ? heldVuSnapshot() : null;
    const showVu = liveShowVu || !!held;
    const effVuDb = liveShowVu ? vuDb : (held ? held.vu_db : null);

    if (elements.outputLevelBadge) {
        elements.outputLevelBadge.classList.toggle('hidden', !(showPeak || showVu));
        elements.outputLevelBadge.style.visibility = '';
        elements.outputLevelBadge.classList.toggle('is-peak', showPeak);
        /* Peak only recolors the badge: keep the exact VU text/format so the
           display never changes width when the peak state toggles. */
        const vuText = showVu ? formatOutputLevelBadgeDb(effVuDb) : '';
        elements.outputLevelBadge.textContent = showPeak
            ? (vuText || formatOutputLevelBadgeDb(0))
            : vuText;
        elements.outputLevelBadge.title = showPeak
            ? `Post-DSP output peak detected on ${title}`
            : (showVu ? `Post-DSP output level (slow VU) on ${title}` : '');
    }

    renderStereoMeter(warning, playbackActive);
}
function renderQueueUI() {
    const queue = state.playback.queue || {};
    const footerSingleTrackOverride = footerSingleTrackStartLockActive();
    const hasQueue = footerSingleTrackOverride ? false : queue.count > 1;
    const queueIndex = footerSingleTrackOverride ? -1 : (typeof queue.index === 'number' ? queue.index : -1);
    const currentTrack = state.playback.current_track;
    const hasNativeQueueTrack = !!(currentTrack && (currentTrack.source === 'local' || currentTrack.source === 'tidal'));
    state.library.shuffle = hasNativeQueueTrack ? !!queue.shuffle : false;
    state.library.loop = hasNativeQueueTrack ? !!queue.loop : false;
    libraryModeSyncArmed = false;
    renderLibraryModeButtons();

    if (elements.queueStatus) {
        if (hasQueue && queueIndex >= 0) {
            elements.queueStatus.textContent = `${queueIndex + 1} / ${queue.count}`;
            elements.queueStatus.classList.remove('hidden');
        } else {
            elements.queueStatus.classList.add('hidden');
        }
    }

    if (elements.btnPrevious && !isStreamingFooterSource(window.__footerSource)) {
        elements.btnPrevious.classList.toggle('hidden', !hasQueue);
        elements.btnPrevious.disabled = playbackActionInFlight || !hasQueue || queueIndex <= 0;
    }
    if (elements.btnNext && !isStreamingFooterSource(window.__footerSource)) {
        elements.btnNext.classList.toggle('hidden', !hasQueue);
        elements.btnNext.disabled = playbackActionInFlight || !hasQueue || queueIndex < 0 || (queueIndex >= queue.count - 1 && !queue.loop && !queue.shuffle);
    }
    if (elements.btnClearQueue) {
        elements.btnClearQueue.classList.toggle('hidden', !hasQueue);
        elements.btnClearQueue.disabled = playbackActionInFlight || !hasQueue;
    }
}
function _isSpotifyActive() {
    return window.__footerSource === 'spotify';
}

window.__fxDebugFooter = localStorage.getItem('fx-debug-footer') === '1';

function footerDebug(event, details = {}) {
    if (!window.__fxDebugFooter) return;
    try {
        console.log('[footer-debug]', event, {
            footerSource: window.__footerSource,
            local: {
                source: state.playback?.current_track?.source || null,
                title: state.playback?.current_track?.title || null,
                liveTitle: state.playback?.live_title || null,
                playing: !!state.playback?.playing,
                paused: !!state.playback?.paused,
            },
            spotify: {
                title: window.__spotifyLastData?.title || null,
                artist: window.__spotifyLastData?.artist || null,
                status: window.__spotifyLastData?.status || null,
                available: !!window.__spotifyLastData?.available,
            },
            ...details,
        });
    } catch {}
}

function setFooterSource(nextSource, reason, details = {}) {
    const prevSource = window.__footerSource;
    window.__footerSource = nextSource;
    if (nextSource === 'spotify') {
        stopPlaybackPositionPoll();
    }
    footerDebug('footer-source', { reason, prevSource, nextSource, ...details });
}

function localPlaybackHasFooterContext(playback = state.playback) {
    const track = playback?.current_track;
    // Native MPV sources share one footer context: TIDAL rides the same
    // engine as local/radio, so live TIDAL playback owns the footer (and the
    // VU/peak gating derived from it) even when no backend commit is cached.
    if (!(track && (track.source === 'radio' || track.source === 'local' || track.source === 'tidal'))) return false;
    if (spotifyPlayingOwnsFooter()) return false;
    if (playback?.paused && window.__footerSource === 'spotify' && spotifyPausedHasFooterContext()) return false;
    return !!(playback?.playing || playback?.paused);
}

function localEndedPlaybackHasFooterContext(playback = state.playback) {
    const track = playback?.current_track;
    if (!(track && (track.source === 'local' || track.source === 'tidal'))) return false;
    if (spotifyPlayingOwnsFooter()) return false;
    return !!(playback?.ended && !playback?.playing && !playback?.paused);
}

function spotifyPlayingOwnsFooter(data = window.__spotifyLastData) {
    if (footerSingleTrackStartLockActive()) return false;
    if (activeLocalPlaybackBlocksSpotifyOwnership()) return false;
    return !!(data && data.available && data.status === 'Playing');
}

function spotifyPausedHasFooterContext(data = window.__spotifyLastData) {
    return !!(data && data.available && data.status === 'Paused');
}

// Live-qobuz counterpart to spotifyPlayingOwnsFooter: the shared footer must
// be able to reach qobuz from live provider truth, not only from the cached
// backend commit. The guards mirror Spotify's: an imminent local single-track
// start and actually-playing local MPV playback keep the footer.
function qobuzPlayingOwnsFooter(data = window.__qobuzLastData) {
    if (footerSingleTrackStartLockActive()) return false;
    if (activeLocalPlaybackBlocksSpotifyOwnership()) return false;
    return !!(data && data.available && data.status === 'Playing');
}

function localFooterHoldHasContext(playback = state.playback) {
    const track = playback?.current_track;
    if (!(track && (track.source === 'radio' || track.source === 'local' || track.source === 'tidal'))) return false;
    return Date.now() < _localFooterHoldUntil;
}

function reconcileFooterSource() {
    // Line-source modes own the footer exclusively with the source
    // switcher. Entering them pauses app playback backend-side, so any
    // retained streaming context (notably a Spotify Paused state) is stale
    // and must not pull the footer back to the app layout.
    if (nonAppSourceModeActive()) {
        setFooterSource('local', 'source-mode-owns-footer');
        return;
    }
    const backendOwner = getBackendFooterOwner();
    if (backendOwner === 'local') {
        setFooterSource('local', 'backend-footer-owner-local');
        return;
    }
    if (backendOwner === 'spotify') {
        // A cached spotify commit is stale when live qbzd playback runs while
        // spotify itself is not playing (missed owner broadcast while an
        // external renderer owned playback): the actually playing renderer
        // owns the shared footer. A live-playing spotify keeps commit
        // priority, mirroring the backend read-only order (spotify>qobuz).
        if (qobuzPlayingOwnsFooter() && !spotifyPlayingOwnsFooter()) {
            setFooterSource('qobuz', 'qobuz-playing-overrides-stale-spotify-commit');
            return;
        }
        setFooterSource('spotify', 'backend-footer-owner-spotify');
        return;
    }
    if (backendOwner === 'qobuz') {
        setFooterSource('qobuz', 'backend-footer-owner-qobuz');
        return;
    }
    if (spotifyPlayingOwnsFooter()) {
        setFooterSource('spotify', 'spotify-playing');
        return;
    }
    if (qobuzPlayingOwnsFooter()) {
        setFooterSource('qobuz', 'qobuz-playing');
        return;
    }
    if (Date.now() < _spotifyTakeoverUntil) {
        setFooterSource('spotify', 'spotify-takeover-window', { takeoverUntil: _spotifyTakeoverUntil });
        return;
    }
    if (localPlaybackHasFooterContext(state.playback)) {
        setFooterSource('local', 'local-playback-has-context');
        return;
    }
    if (localFooterHoldHasContext(state.playback)) {
        setFooterSource('local', 'local-footer-hold', { holdUntil: _localFooterHoldUntil });
        return;
    }
    if (localEndedPlaybackHasFooterContext(state.playback)) {
        setFooterSource('local', 'local-ended-has-context');
        return;
    }
    if (spotifyPausedHasFooterContext()) {
        setFooterSource('spotify', 'spotify-paused-context');
        return;
    }
    setFooterSource('local', 'fallback-local');
}

function spotifyIsInstalled(data = window.__spotifyLastData) {
    return data?.installed === true;
}

function shouldPollSpotify() {
    return spotifyIsInstalled() && (window.__visibleTab === 'spotify' || window.__footerSource === 'spotify');
}

function syncFooterOwnershipFromPlayback(playback = state.playback) {
    footerDebug('sync-from-playback', {
        playback: {
            source: playback?.current_track?.source || null,
            title: playback?.current_track?.title || null,
            liveTitle: playback?.live_title || null,
            playing: !!playback?.playing,
            paused: !!playback?.paused,
            playbackOwner: playback?.playback_owner || null,
        },
    });
    const backendOwner = getBackendFooterOwner(playback);
    if (backendOwner === 'local') {
        _spotifyTakeoverUntil = 0;
        setFooterSource('local', 'sync-playback-backend-owner-local');
        if (!shouldPollSpotify()) {
            _spotifyPollGeneration++;
            stopSpotifyPoll();
        }
        return;
    }
    if (backendOwner === 'spotify') {
        // Same stale-commit override as reconcileFooterSource: live qbzd
        // playback while spotify is not playing wins over the cached commit.
        if (qobuzPlayingOwnsFooter() && !spotifyPlayingOwnsFooter()) {
            setFooterSource('qobuz', 'sync-playback-qobuz-overrides-stale-spotify-commit');
            return;
        }
        setFooterSource('spotify', 'sync-playback-backend-owner-spotify');
        return;
    }
    if (backendOwner === 'qobuz') {
        setFooterSource('qobuz', 'sync-playback-backend-owner-qobuz');
        return;
    }
    if (spotifyPlayingOwnsFooter()) {
        setFooterSource('spotify', 'sync-playback-spotify-still-playing');
        return;
    }
    if (qobuzPlayingOwnsFooter()) {
        setFooterSource('qobuz', 'sync-playback-qobuz-playing');
        return;
    }
    if (localPlaybackHasFooterContext(playback)) {
        _spotifyTakeoverUntil = 0;
        if (window.__spotifyLastData && window.__spotifyLastData.status === 'Playing') {
            footerDebug('downgrade-spotify-from-playback', { reason: 'local-playback-context' });
            window.__spotifyLastData = { ...window.__spotifyLastData, status: 'Paused' };
        }
        setFooterSource('local', 'sync-playback-local-context');
        if (!shouldPollSpotify()) {
            _spotifyPollGeneration++;
            stopSpotifyPoll();
        }
        return;
    }
    if (localEndedPlaybackHasFooterContext(playback)) {
        _spotifyTakeoverUntil = 0;
        setFooterSource('local', 'sync-playback-local-ended-context');
        if (!shouldPollSpotify()) {
            _spotifyPollGeneration++;
            stopSpotifyPoll();
        }
        return;
    }
    reconcileFooterSource();
    if (!shouldPollSpotify()) {
        _spotifyPollGeneration++;
        stopSpotifyPoll();
    }
}

// Shared commit path for a native /api/play response (local/radio/tidal).
// All three starts commit the same authoritative payload through this helper:
// merge, footer-ownership resync, Spotify poll demotion and UI refresh.
// Without the merge the footer keeps a stale Spotify owner and the VU/peak
// gating derived from it hides the meter even though the backend already
// streams fresh peak values. Provider-specific reactions (library state,
// track cue, metadata refresh, samplerate burst polling) stay in the callers.
function applyNativePlayResponse(data) {
    if (data && data.playback) {
        mergePlaybackState(data.playback);
    }
    _spotifyTakeoverUntil = 0;
    if (window.__spotifyLastData && window.__spotifyLastData.status === 'Playing') {
        window.__spotifyLastData = { ...window.__spotifyLastData, status: 'Paused' };
    }
    syncFooterOwnershipFromPlayback(state.playback);
    if (!shouldPollSpotify()) {
        _spotifyPollGeneration++;
        stopSpotifyPoll();
    }
    updatePlaybackUI();
}

function footerContentFreezeActive() {
    return Date.now() < _footerContentFreezeUntil;
}

function armFooterContentFreeze(ms = 900) {
    _footerContentFreezeUntil = Date.now() + ms;
    if (_footerContentFreezeTimer) clearTimeout(_footerContentFreezeTimer);
    _footerContentFreezeTimer = setTimeout(() => {
        _footerContentFreezeTimer = null;
        updatePlaybackUI();
    }, ms + 20);
}

function coverDetailSections(playback) {
    // Returns { history: [...], queue: null | { tracks, index } } for the
    // cover detail card. Only uses data already present in the status
    // payload — never invents entries.
    const track = playback?.current_track || null;
    if (!track) return { history: [], queue: null };
    const isRadio = track.source === 'radio';
    let history = [];
    if (isRadio) {
        const raw = playback?.radio_metadata?.history;
        if (Array.isArray(raw)) {
            history = raw.filter(entry => entry && (entry.title || entry.artist));
        }
    }
    let queue = null;
    if (track.source === 'local') {
        const q = playback?.queue || {};
        const tracks = Array.isArray(q.tracks) ? q.tracks : [];
        const count = Number(q.count) || tracks.length;
        if (count > 1 && tracks.length > 1) {
            queue = { tracks, index: typeof q.index === 'number' ? q.index : -1 };
        }
    }
    return { history, queue };
}

function isCoverDetailOpen() {
    return !!(elements.coverDetailCard && !elements.coverDetailCard.classList.contains('hidden'));
}

function coverDetailMeta(playback) {
    // Returns { source, title, artist, album, tech } for the cover detail
    // card. Only uses data already present in the status payload — never
    // invents entries. The source label is the provider identity (Local /
    // Tidal) like the streaming labels, so all sources share one hierarchy;
    // radio keeps its station name as the source label.
    const track = playback?.current_track || null;
    if (!track) return { source: '', title: '', artist: '', album: '', tech: '' };
    const isRadio = track.source === 'radio';
    const radioMetadata = isRadio ? playback.radio_metadata : null;
    const providerFresh = radioMetadata && !radioMetadata.stale && radioMetadata.title;
    // Source label: radio shows the station, native playback sources show
    // their provider identity (Local for the library, Tidal for TIDAL).
    let source = '';
    if (isRadio) {
        source = track.title || '';
    } else if (track.source === 'tidal') {
        source = 'Tidal';
    } else if (track.source === 'local') {
        source = 'Local';
    }
    const title = providerFresh
        ? (radioMetadata.title || '')
        : (isRadio && playback.live_title ? playback.live_title : (track.title || ''));
    const artist = providerFresh
        ? (radioMetadata.artist || '')
        : (isRadio ? '' : (track.artist || ''));
    const album = providerFresh
        ? (radioMetadata.album || '')
        : (track.album || '');
    const tech = formatRadioStreamLine(playback.stream_info);
    return { source, title, artist, album, tech };
}

function coverDetailStreamingMeta(data, source = 'spotify') {
    // External-renderer detail card meta: same hierarchy as library/radio —
    // source label, title, artist, album and the shared audio facts line.
    // Only fields actually delivered by the provider are used; the tech line
    // renders through the same footer formatter (formatStreamingMetaLine), so
    // Qobuz/TIDAL show their real format/bitdepth/rate while Spotify shows
    // only the resolved rate — never invented technical values.
    if (!data) return { source: '', title: '', artist: '', album: '', tech: '' };
    const labels = { spotify: 'Spotify', qobuz: 'Qobuz', tidal: 'Tidal' };
    return {
        source: labels[source] || labels.spotify,
        title: data.title || '',
        artist: data.artist || '',
        album: data.album || '',
        tech: formatStreamingMetaLine(data),
    };
}

function setCoverDetailText(el, value) {
    if (!el) return;
    const text = (value || '').trim();
    el.textContent = text;
    el.classList.toggle('hidden', !text);
}

function coverDetailExtra(playback) {
    // Small tag-info block under the cover: two subtle lines using only
    // fields already present in the status payload (year, genre, track/disc
    // number). No new API lookups, no composer/label (not in the data path).
    // Returns { line1, line2 }; empty strings hide the line, both empty hide
    // the whole block. Example: "2004 · German Hip-Hop" / "Disc 1 · Track 3".
    const track = playback?.current_track || null;
    if (!track || track.source !== 'local') return { line1: '', line2: '' };
    const line1Parts = [];
    if (Number.isInteger(track.year) && track.year > 0) line1Parts.push(String(track.year));
    const genre = (track.genre || '').trim();
    if (genre) line1Parts.push(genre);
    const line2Parts = [];
    if (Number.isInteger(track.disc_number) && track.disc_number > 0) line2Parts.push(`Disc ${track.disc_number}`);
    if (Number.isInteger(track.track_number) && track.track_number > 0) line2Parts.push(`Track ${track.track_number}`);
    return { line1: line1Parts.join(' · '), line2: line2Parts.join(' · ') };
}

function coverQueuePlayTarget(playback, index) {
    // Canonical play payload for jumping to a queue index: the same /api/play
    // request playLocal uses (track_id + full queue in order). Returns null
    // when the index is invalid or already the active track.
    const queue = playback?.queue || {};
    const tracks = Array.isArray(queue.tracks) ? queue.tracks : [];
    if (!Number.isInteger(index) || index < 0 || index >= tracks.length) return null;
    if (index === queue.index) return null;
    const track = tracks[index];
    if (!track || !track.id) return null;
    return {
        source: 'local',
        track_id: track.id,
        queue_track_ids: tracks.map(item => item.id),
        shuffle: !!queue.shuffle,
        loop: !!queue.loop,
    };
}

async function playCoverQueueIndex(index) {
    const payload = coverQueuePlayTarget(state.playback, index);
    if (!payload) return;
    try {
        const resp = await fetch('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Play command failed'));
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        updatePlaybackUI();
    } catch (e) {
        console.warn('Cover queue play failed', e);
        showToast('Failed to play track', 'error');
    }
}

// Signature of the last rendered queue list. Rebuilding the <ol> on every
// status poll would replace the row under the cursor and drop its :hover
// state (visible flicker) and keyboard focus, so only rebuild on change.
let coverDetailQueueSignature = null;

function renderCoverDetailCard() {
    const playback = state.playback || {};
    const track = playback.current_track || null;
    // An external renderer (Spotify/Qobuz) owns the footer (and thus the
    // detail card) from its streaming state; /api/status carries no external
    // track. Mirror the footer source so the card shows the same data.
    const streamingSource = getEffectivePlaybackControlSource();
    const streamingData = streamingSource === 'spotify'
        ? (window.__spotifyLastData || null)
        : streamingSource === 'qobuz' ? (window.__qobuzLastData || null) : null;
    // Meta block: source/playlist, current title, artist, album, tech line.
    const meta = streamingData ? coverDetailStreamingMeta(streamingData, streamingSource) : coverDetailMeta(playback);
    setCoverDetailText(elements.coverDetailSource, meta.source);
    setCoverDetailText(elements.coverDetailTitle, meta.title);
    setCoverDetailText(elements.coverDetailArtist, meta.artist);
    setCoverDetailText(elements.coverDetailAlbum, meta.album);
    setCoverDetailText(elements.coverDetailTech, meta.tech);
    // Tag-info block under the cover: year/genre + disc/track position only
    // when present; hide the whole block when both lines are empty.
    const extra = coverDetailExtra(playback);
    setCoverDetailText(elements.coverDetailExtraLine1, extra.line1);
    setCoverDetailText(elements.coverDetailExtraLine2, extra.line2);
    if (elements.coverDetailExtra) {
        elements.coverDetailExtra.classList.toggle('hidden', !extra.line1 && !extra.line2);
    }
    // Cover: same artwork resolution as the footer (provider cover for radio,
    // track artwork for library).
    const isRadio = track && track.source === 'radio';
    const radioMetadata = isRadio ? playback.radio_metadata : null;
    const providerCover = radioMetadata && !radioMetadata.stale ? radioMetadata.cover_url : '';
    // Streaming artwork mirrors the footer artwork resolution
    // (streamingArtworkItem) with the actual provider as source; the
    // radio/library branches are unchanged.
    const coverItem = streamingData
        ? streamingArtworkItem(streamingData, streamingSource)
        : (providerCover
            ? { ...track, artwork_available: true, artwork_url: providerCover, artwork_fallback_url: track?.artwork_url || '' }
            : track);
    const coverUrl = playbackArtworkKnownAvailable(coverItem) ? playbackArtworkUrl(coverItem) : '';
    if (coverUrl) {
        elements.coverDetailCover.src = coverUrl;
        elements.coverDetailCover.classList.remove('hidden');
    } else {
        elements.coverDetailCover.removeAttribute('src');
        elements.coverDetailCover.classList.add('hidden');
    }
    const sections = coverDetailSections(playback);
    // Radio: provider-delivered history only (no empty headings, no invented entries).
    if (sections.history.length > 0) {
        elements.coverDetailHistoryList.innerHTML = sections.history.map((entry, index) => `
            <li class="cover-detail-row">
                <span class="cover-detail-row-index">${index + 1}</span>
                <span class="cover-detail-row-body">
                    <span class="cover-detail-row-title">${escapeHtml(entry.title || '')}</span>
                    ${entry.artist ? `<span class="cover-detail-row-artist">${escapeHtml(entry.artist)}</span>` : ''}
                </span>
            </li>`).join('');
        elements.coverDetailHistory.classList.remove('hidden');
    } else {
        elements.coverDetailHistoryList.innerHTML = '';
        elements.coverDetailHistory.classList.add('hidden');
    }
    // Library: active queue with current track highlighted (if any). Rows are
    // keyboard-accessible buttons that jump via the canonical play path.
    if (sections.queue) {
        const signature = sections.queue.tracks.map(item => item.id).join('|') + '|' + sections.queue.index;
        if (signature !== coverDetailQueueSignature) {
            coverDetailQueueSignature = signature;
            elements.coverDetailQueueList.innerHTML = sections.queue.tracks.map((item, index) => {
                const playable = coverQueuePlayTarget(playback, index) !== null;
                const attrs = playable
                    ? ` role="button" tabindex="0" data-queue-index="${index}" aria-label="Play ${escapeHtml(item.title || '')}"`
                    : '';
                return `<li class="cover-detail-row${index === sections.queue.index ? ' current' : ''}"${attrs}>
                    <span class="cover-detail-row-index">${index + 1}</span>
                    ${index === sections.queue.index ? '<span class="cover-detail-row-current-mark">▶</span>' : ''}
                    <span class="cover-detail-row-body">
                        <span class="cover-detail-row-title">${escapeHtml(item.title || '')}</span>
                        ${item.artist ? `<span class="cover-detail-row-artist">${escapeHtml(item.artist)}</span>` : ''}
                    </span>
                </li>`;
            }).join('');
        }
        elements.coverDetailQueue.classList.remove('hidden');
    } else {
        coverDetailQueueSignature = null;
        elements.coverDetailQueueList.innerHTML = '';
        elements.coverDetailQueue.classList.add('hidden');
    }
}

function openCoverDetailCard() {
    renderCoverDetailCard();
    elements.coverDetailBackdrop.classList.remove('hidden');
    elements.coverDetailCard.classList.remove('hidden');
    window.FXRouteModal?.open(elements.coverDetailCard, {
        dialog: elements.coverDetailCard,
        initialFocus: elements.coverDetailCard,
        siblingRoots: [elements.coverDetailBackdrop],
        onEscape: closeCoverDetailCard,
    });
}

function closeCoverDetailCard() {
    elements.coverDetailBackdrop.classList.add('hidden');
    elements.coverDetailCard.classList.add('hidden');
    window.FXRouteModal?.close(elements.coverDetailCard);
}

function toggleCoverDetailCard() {
    if (isCoverDetailOpen()) closeCoverDetailCard();
    else openCoverDetailCard();
}
function updatePlaybackUI() {
    const { current_track, volume, playing, paused, live_title } = state.playback;
    const freezeActive = footerContentFreezeActive();
    reconcileFooterSource();
    if (shouldPollSpotify()) {
        startSpotifyPoll();
    } else {
        _spotifyPollGeneration++;
        stopSpotifyPoll();
    }
    if (shouldPollQobuz()) {
        startQobuzPoll();
    } else {
        _qobuzPollGeneration++;
        stopQobuzPoll();
    }
    // When an external renderer (Spotify/Qobuz) owns the footer, local UI must
    // NOT touch footer elements at all. Refresh from the owner's normalized
    // state and return — the streaming state owns the footer exclusively.
    if (isStreamingFooterSource(window.__footerSource)) {
        stopPlaybackPositionPoll();
        const streamingData = streamingFooterData();
        if (!freezeActive && streamingData) updateFooterForStreamingOwner(streamingData);
        highlightActiveTrack();
        return;
    }
    // The footer is laid out exclusively from the data-backed visibility classes.
    const isRadio = current_track && current_track.source === 'radio';
    if (!freezeActive) {
        const radioMetadata = isRadio ? state.playback.radio_metadata : null;
        elements.playbackBar?.classList.toggle('has-media', !!current_track);
        // Track info
        if (current_track) {
            elements.trackTitle.textContent = isRadio && live_title ? live_title : current_track.title;
            elements.trackTitle.classList.remove('placeholder');
            elements.trackTitle.style.display = 'none';
            elements.trackTitle.classList.add('placeholder');
            const scArtist = document.getElementById('sc-artist');
            const scTitle = document.getElementById('sc-title');
            const scAlbum = document.getElementById('sc-album');
            const providerMetadata = isRadio && radioMetadata && !radioMetadata.stale && radioMetadata.title
                ? radioMetadata : null;
            if (scArtist) scArtist.textContent = providerMetadata
                ? (providerMetadata.artist || current_track.title)
                : (isRadio && live_title ? current_track.title : (current_track.artist || ''));
            if (scTitle) scTitle.textContent = providerMetadata ? providerMetadata.title : (isRadio && live_title ? live_title : current_track.title);
            if (scAlbum) {
                const album = providerMetadata?.album || (!isRadio ? current_track.album : '') || '';
                scAlbum.textContent = album;
                scAlbum.style.display = album ? '' : 'none';
            }
            elements.trackArtist.textContent = isRadio && live_title ? current_track.title : (current_track.artist || '');
            elements.trackArtist.style.display = 'none';
        } else {
            elements.trackTitle.textContent = 'Not playing';
            elements.trackTitle.classList.add('placeholder');
            elements.trackArtist.textContent = '';
            const scArtist = document.getElementById('sc-artist');
            const scTitle = document.getElementById('sc-title');
            const scAlbum = document.getElementById('sc-album');
            if (scArtist) scArtist.textContent = '';
            if (scTitle) scTitle.textContent = '';
            if (scAlbum) {
                scAlbum.textContent = '';
                scAlbum.style.display = 'none';
            }
            if (elements.trackTitle) elements.trackTitle.style.display = '';
            if (elements.trackArtist) elements.trackArtist.style.display = '';
        }
    }
    renderTrackFavoriteButton(current_track);
    const activeRadioMetadata = isRadio ? state.playback.radio_metadata : null;
    const providerCover = activeRadioMetadata && !activeRadioMetadata.stale ? activeRadioMetadata.cover_url : '';
    updatePlaybackCover(providerCover ? {
        ...current_track,
        artwork_available: true,
        artwork_url: providerCover,
        artwork_fallback_url: current_track?.artwork_url || '',
    } : current_track);
    document.body.classList.remove('is-playing', 'is-paused');
    if (playing) {
        document.body.classList.add('is-playing');
    } else if (paused) {
        document.body.classList.add('is-paused');
    }
    // Bar glow
    if (elements.playbackBar) {
        elements.playbackBar.classList.toggle('is-playing', !!playing);
        elements.playbackBar.classList.toggle('is-paused', !!paused && !playing);
    }
    // Play/pause + seek
    updatePlayPauseButton(playing ? 'playing' : (paused ? 'paused' : 'stopped'));
    updateSeekUI();
    renderQueueUI();
    renderSamplerateUI();
    renderPeakWarningBadge();
    // Volume
    if (!volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
        renderVolumeControlsFromActualVolume(volume);
    } else {
        elements.volumeDisplay.textContent = `${actualVolumeToSliderValue(volume)}%`;
    }
    // Bluetooth / external-input modes reuse this footer: transport is
    // replaced by the source switcher while volume and meter keep updating.
    // reconcileFooterSource() above pinned ownership to 'local' in these
    // modes, so no streaming gate is needed here — consulting it would delay
    // the switcher by one poll on first paint after entering a source mode.
    if (nonAppSourceModeActive()) {
        renderSourceModeFooter();
    }
    // Highlight active
    highlightActiveTrack();
    // Keep the cover detail card in sync while it is open
    if (isCoverDetailOpen()) renderCoverDetailCard();
    // Start/stop position polling for local playback
    if (playing && !isStreamingFooterSource(window.__footerSource)) {
        startPlaybackPositionPoll();
    } else {
        stopPlaybackPositionPoll();
    }
}
function startPlaybackPositionPoll() {
    if (isStreamingFooterSource(window.__footerSource)) return;
    if (playbackPositionPollTimer !== null) return;
    playbackPositionPollTimer = setInterval(async () => {
        try {
            if (isPageHidden()) return;
            if (isStreamingFooterSource(window.__footerSource)) {
                stopPlaybackPositionPoll();
                return;
            }
            const resp = await fetch('/api/status');
            if (!resp.ok) return;
            const data = await resp.json();
            const backendOwner = getBackendFooterOwner(data);
            if (isStreamingFooterSource(window.__footerSource) || isStreamingFooterSource(backendOwner)) {
                if (isStreamingFooterSource(backendOwner)) {
                    setFooterSource(backendOwner, 'local-poll-backend-owner-streaming');
                }
                stopPlaybackPositionPoll();
                return;
            }
            mergePlaybackState(data);
            if (isStreamingFooterSource(window.__footerSource)) {
                stopPlaybackPositionPoll();
                return;
            }
            updateSeekUI();
        } catch (_) {
            // ignore transient errors
        }
    }, 1000);
}
function stopPlaybackPositionPoll() {
    if (playbackPositionPollTimer !== null) {
        clearInterval(playbackPositionPollTimer);
        playbackPositionPollTimer = null;
    }
}
function updatePlayPauseButton(playbackState) {
    elements.btnPlayPause.textContent = playbackState === 'playing' ? '⏸' : '▶';
    const hasPlayableContext = !!(state.playback.current_track || getLastRadioTrack());
    elements.btnPlayPause.disabled = playbackActionInFlight || (!hasPlayableContext && playbackState === 'stopped');
}
function highlightActiveTrack() {
    if (window.__footerSource === 'spotify') {
        document.querySelectorAll('.station-card.active, .track-item.active, .streaming-result.active').forEach(item => item.classList.remove('active'));
        return;
    }
    // A play request holds its optimistic target until the server confirms the
    // commit, so a stale pre-commit WebSocket push cannot bounce the highlight
    // back to the previous station while the transition is still running.
    const displayTrack = pendingOptimisticTrack?.track || state.playback.current_track;
    // Radio stations
    document.querySelectorAll('.station-card').forEach(card => {
        const stationId = card.dataset.stationId;
        const activeStationId = displayTrack && displayTrack.source === 'radio'
            ? displayTrack.id.replace(/^radio_/, '')
            : null;
        if (activeStationId && activeStationId === stationId) {
            card.classList.add('active');
        } else {
            card.classList.remove('active');
        }
    });
    // Library tracks (TIDAL catalog rows share the same active language
    // and id namespace, so the running track highlights there as well).
    document.querySelectorAll('.track-item, .streaming-result[data-track-id]').forEach(item => {
        const trackId = item.dataset.trackId;
        if (displayTrack && displayTrack.id === trackId) {
            item.classList.add('active');
        } else {
            item.classList.remove('active');
        }
    });
}
// Library
async function fetchInitialData() {
    startSampleratePolling();
    void fetchProviderAdmin();
    await Promise.all([radioModule.fetchStations(), fetchTracks(), EffectsUI.fetchEffects(), fetchMeasurements(), fetchPlaybackStatus(), fetchSamplerateStatus(), fetchDownloadStatus(), fetchAudioOutputOverview(), fetchAudioSourceOverview()]);
    requestSubwooferPreviewRedrawFromState();
    await fetchPlaylists();
}
async function fetchPlaybackStatus() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error('Failed to fetch playback status');
        const data = await resp.json();
        mergePlaybackState(data);
        updateLiveBanner(data);
        syncFooterOwnershipFromPlayback(data);
        syncLibraryStateFromPlaybackContext(true);
        updatePlaybackUI();
    } catch (e) {
        console.debug('Playback status unavailable on load', e);
    }
}
async function fetchSamplerateStatus() {
    if (isPageHidden()) return;
    try {
        const resp = await fetch('/api/audio/samplerate');
        if (!resp.ok) throw new Error('Failed to fetch samplerate status');
        const data = await resp.json();
        state.samplerate = { ...state.samplerate, ...data };
        renderSamplerateUI();
        renderSettingsPanel();
    } catch (e) {
        console.debug('Samplerate status unavailable', e);
        state.samplerate = { ...state.samplerate, available: false, active_rate: null };
        renderSamplerateUI();
        renderSettingsPanel();
    }
}
async function previousInQueue() {
    if (playbackActionInFlight || !elements.btnPrevious || elements.btnPrevious.disabled) return;
    playbackActionInFlight = true;
    armFooterContentFreeze();
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/playback/previous', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Previous failed'));
        if (data.playback) mergePlaybackState(data.playback);
        updatePlaybackUI();
        triggerSamplerateBurstPolling();
    } catch (e) {
        showToast(e.message || 'Failed to jump to previous track', 'error');
    } finally {
        playbackActionInFlight = false;
        updatePlaybackUI();
    }
}
async function nextInQueue() {
    if (playbackActionInFlight || !elements.btnNext || elements.btnNext.disabled) return;
    playbackActionInFlight = true;
    armFooterContentFreeze();
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/playback/next', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Next failed'));
        if (data.playback) mergePlaybackState(data.playback);
        updatePlaybackUI();
        triggerSamplerateBurstPolling();
    } catch (e) {
        showToast(e.message || 'Failed to jump to next track', 'error');
    } finally {
        playbackActionInFlight = false;
        updatePlaybackUI();
    }
}
async function clearQueue() {
    if (playbackActionInFlight || !elements.btnClearQueue || elements.btnClearQueue.disabled) return;
    playbackActionInFlight = true;
    libraryModeSyncArmed = true;
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/playback/clear-queue', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Clear queue failed');
        if (data.playback) mergePlaybackState(data.playback);
        state.library.shuffle = false;
        state.library.loop = false;
        lastLibraryPlaybackContextSignature = JSON.stringify({ shuffle: false, loop: false });
        renderLibraryModeButtons();
        showToast('Queue cleared', 'info');
    } catch (e) {
        showToast(e.message || 'Failed to clear queue', 'error');
    } finally {
        playbackActionInFlight = false;
        updatePlaybackUI();
    }
}
async function fetchLibraryStatus() {
    try {
        const resp = await fetch('/api/library/status');
        if (!resp.ok) throw new Error('Failed to fetch library status');
        const status = await resp.json();
        const wasScanning = !!state.library.scanning;
        state.library.scanStatus = status;
        state.library.scanning = !!status.scanning;
        renderLibraryView();
        if (status.scanning) {
            setTimeout(fetchLibraryStatus, LIBRARY_SCAN_POLL_INTERVAL_MS);
        } else if (wasScanning) {
            await fetchTracks();
        }
        return status;
    } catch (e) {
        console.debug('Failed to fetch library status', e);
        return null;
    }
}
async function fetchTracks() {
    try {
        const resp = await fetch('/api/tracks');
        if (!resp.ok) throw new Error('Failed to fetch tracks');
        state.library.tracks = await resp.json();
        const status = await fetchLibraryStatus();
        state.library.scanning = !!status?.scanning;
        renderLibraryView();
        // Non-blocking: also load albums in background
        fetchAlbums();
    } catch (e) {
        state.library.scanning = false;
        showToast('Failed to load library', 'error');
    }
}
async function fetchPlaylists() {
    try {
        const resp = await fetch('/api/playlists');
        if (!resp.ok) throw new Error('Failed to fetch playlists');
        state.playlists = await resp.json();
        renderLibraryView();
    } catch (e) {
        console.debug('Failed to fetch playlists', e);
    }
}
function getTrackRelativePath(track) {
    const id = String(track?.id || '');
    if (id.startsWith('local_')) return id.slice(6);
    const path = String(track?.path || track?.url || track?.title || '');
    return path.split('/').filter(Boolean).slice(-1).join('/');
}
function getTrackFolder(track) {
    const rel = getTrackRelativePath(track);
    const parts = rel.split('/').filter(Boolean);
    parts.pop();
    return parts.join('/');
}
function getTrackFilename(track) {
    const rel = getTrackRelativePath(track);
    return rel.split('/').filter(Boolean).pop() || track?.title || '';
}
function trackMatchesLibraryQuery(track, query) {
    if (!query) return true;
    const haystack = [track.title, track.artist, track.album, track.album_artist, track.genre, track.year, track.path, track.url, track.id, getTrackRelativePath(track)]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
    return haystack.includes(query);
}
function playlistMatchesLibraryQuery(playlist, query) {
    if (!query) return true;
    return String(playlist?.name || '').toLowerCase().includes(query);
}
function getFilteredPlaylists() {
    if (state.library.viewMode === 'folders') return [];
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    return (state.playlists || []).filter(playlist => playlistMatchesLibraryQuery(playlist, query));
}
function isTrackInCurrentFolder(track) {
    if (state.library.viewMode !== 'folders') return true;
    return getTrackFolder(track) === (state.library.currentFolder || '');
}
function getFilteredTracks() {
    const tracks = state.library.tracks || [];
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    return tracks.filter(track => trackMatchesLibraryQuery(track, query) && isTrackInCurrentFolder(track));
}
function getTracksInFolder(folderPath) {
    const folder = folderPath || '';
    const prefix = folder ? `${folder}/` : '';
    return (state.library.tracks || []).filter(track => {
        const rel = getTrackRelativePath(track);
        return folder ? rel.startsWith(prefix) : !!rel;
    });
}
function getFolderChildren() {
    const tracks = state.library.tracks || [];
    const current = state.library.currentFolder || '';
    const prefix = current ? `${current}/` : '';
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    const folders = new Map();
    tracks.forEach(track => {
        const rel = getTrackRelativePath(track);
        if (!rel.startsWith(prefix)) return;
        const rest = rel.slice(prefix.length);
        const parts = rest.split('/').filter(Boolean);
        if (parts.length <= 1) return;
        const name = parts[0];
        const folderPath = current ? `${current}/${name}` : name;
        if (query && !folderPath.toLowerCase().includes(query) && !trackMatchesLibraryQuery(track, query)) return;
        const entry = folders.get(folderPath) || { path: folderPath, name, count: 0 };
        entry.count += 1;
        folders.set(folderPath, entry);
    });
    return Array.from(folders.values()).sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
}
function renderLibraryView() {
    if (state.library.viewMode === 'albums') {
        if (state.library.albumDetail) {
            // Re-open the album detail if we were viewing one
            const albumId = state.library.albumDetail.album.id;
            state.library.albumDetail = null;
            openAlbumDetail(albumId);
        } else if (state.library.playlistDetail) {
            const playlistId = state.library.playlistDetail.playlist.id;
            state.library.playlistDetail = null;
            openPlaylistDetail(playlistId);
        } else {
            renderAlbums();
        }
    } else {
        renderTracks();
    }
}

function renderLibraryViewButtons() {
    const mode = state.library.viewMode;
    const favActive = mode === 'albums' && !!state.library.showFavoriteAlbums;
    if (elements.libraryViewTracksBtn) {
        const active = mode === 'tracks';
        elements.libraryViewTracksBtn.classList.toggle('active', active);
        elements.libraryViewTracksBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    if (elements.libraryViewFoldersBtn) {
        const active = mode === 'folders';
        elements.libraryViewFoldersBtn.classList.toggle('active', active);
        elements.libraryViewFoldersBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    if (elements.libraryViewFavoritesBtn) {
        elements.libraryViewFavoritesBtn.classList.toggle('active', favActive);
        elements.libraryViewFavoritesBtn.setAttribute('aria-pressed', favActive ? 'true' : 'false');
    }
    if (elements.libraryViewAlbumsBtn) {
        const active = mode === 'albums' && !state.library.showFavoriteAlbums;
        elements.libraryViewAlbumsBtn.classList.toggle('active', active);
        elements.libraryViewAlbumsBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
}
function renderLibraryFolderPath() {
    if (!elements.libraryFolderPath) return;
    if (state.library.viewMode !== 'folders') {
        elements.libraryFolderPath.classList.add('hidden');
        elements.libraryFolderPath.innerHTML = '';
        return;
    }
    const current = state.library.currentFolder || '';
    const parts = current.split('/').filter(Boolean);
    let html = `<button type="button" data-folder="">Music root</button>`;
    let path = '';
    parts.forEach(part => {
        path = path ? `${path}/${part}` : part;
        html += `<span>/</span><button type="button" data-folder="${escapeHtml(path)}">${escapeHtml(part)}</button>`;
    });
    if (current) {
        html += '<button id="library-folder-back" class="library-folder-back" type="button" aria-label="Back to parent folder" title="Back to parent folder">← Back</button>';
    }
    elements.libraryFolderPath.innerHTML = html;
    elements.libraryFolderPath.classList.remove('hidden');
    elements.libraryFolderPath.querySelectorAll('button[data-folder]').forEach(btn => {
        btn.addEventListener('click', () => setLibraryFolder(btn.dataset.folder || ''));
    });
    const backButton = elements.libraryFolderPath.querySelector('#library-folder-back');
    if (backButton) {
        backButton.addEventListener('click', () => {
            const parentFolder = current.split('/').filter(Boolean).slice(0, -1).join('/');
            setLibraryFolder(parentFolder);
        });
    }
}
function formatLibraryScanStatus() {
    const status = state.library.scanStatus;
    if (!status) return '';
    if (status.scanning) {
        const found = status.tracks_found || status.audio_seen || 0;
        const seen = status.files_seen || 0;
        const dir = status.current_dir ? ` · ${status.current_dir}` : '';
        return `Scanning library… ${found} audio tracks found, ${seen} files checked${dir}`;
    }
    if (status.error) return `Library scan error: ${status.error}`;
    return '';
}
function renderTracks() {
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    renderLibraryFolderPath();
    // Hide album/playlist views when in tracks/folders mode
    if (elements.albumsGrid) elements.albumsGrid.classList.add('hidden');
    if (elements.albumDetail) elements.albumDetail.classList.add('hidden');
    if (elements.playlistDetail) elements.playlistDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    elements.tracksList.classList.remove('hidden');
    const allTracks = state.library.tracks || [];
    const filteredTracks = getFilteredTracks();
    const filteredPlaylists = getFilteredPlaylists();
    const validSelectedIds = allTracks.length > 0
        ? state.library.selectedTrackIds.filter(id => allTracks.some(track => track.id === id))
        : state.library.selectedTrackIds;
    const selectedIds = new Set(validSelectedIds);
    state.library.selectedTrackIds = Array.from(selectedIds);
    const loadingEl = document.querySelector('#tab-library .content-state');
    const scanText = formatLibraryScanStatus();
    const hasSearch = !!(state.library.searchQuery || '').trim();

    // Playlists count as library content: an empty track list must not wipe
    // them, because a running scan (or a library whose first scan is still
    // filling the cache) would otherwise read as "nothing here". The shared
    // scan status renders above the list until the scan finished.
    if (allTracks.length === 0 && filteredPlaylists.length === 0) {
        window.FXRouteContentState.set(loadingEl, scanText ? 'loading' : 'empty',
            scanText || (hasSearch
                ? 'No matching tracks or playlists. Try a broader search.'
                : 'No tracks yet. Import a file or URL to get started.'));
        elements.tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }
    if (scanText) {
        window.FXRouteContentState.set(loadingEl, 'loading', scanText);
    } else {
        window.FXRouteContentState.hide(loadingEl);
    }

    const folderMode = state.library.viewMode === 'folders';
    const childFolders = folderMode ? getFolderChildren() : [];
    if (filteredTracks.length === 0 && childFolders.length === 0 && filteredPlaylists.length === 0) {
        window.FXRouteContentState.set(loadingEl, 'empty',
            hasSearch ? 'No matching tracks or playlists. Try a broader search.' : 'No tracks in this folder.');
        elements.tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }

    let html = '';

    if (!folderMode && filteredPlaylists.length > 0) {
        html += filteredPlaylists.map(playlist => {
            const classes = ['track-item', 'playlist-item'];
            return `<div class="${classes.join(' ')}" data-playlist-id="${escapeHtml(playlist.id)}">
                <button class="track-play" data-playlist-id="${escapeHtml(playlist.id)}" type="button" title="Play ${escapeHtml(playlist.name)}" aria-label="Play playlist ${escapeHtml(playlist.name)}">▶</button>
                <div class="track-info">
                    <div class="track-title">${escapeHtml(playlist.name)}</div>
                    <div class="track-artist track-sub">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
                </div>
                <button class="playlist-download-btn" data-playlist-download="${escapeHtml(playlist.id)}" type="button" title="Export playlist as M3U8">⬇</button>
                <button class="playlist-delete-btn" data-playlist-delete="${escapeHtml(playlist.id)}" type="button" title="Delete playlist" aria-label="Delete playlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
            </div>`;
        }).join('');
    }

    if (folderMode) {
        html += childFolders.map(folder => `<div class="track-item folder-item" data-folder="${escapeHtml(folder.path)}">
            <button class="track-play" data-folder="${escapeHtml(folder.path)}" type="button" title="Open folder ${escapeHtml(folder.name)}" aria-label="Open folder ${escapeHtml(folder.name)}">▶</button>
            <div class="track-info">
                <div class="track-title">${escapeHtml(folder.name)}</div>
                <div class="track-artist track-sub">${folder.count} track${folder.count === 1 ? '' : 's'}</div>
            </div>
            <div class="folder-actions" aria-label="Folder actions">
                <button class="folder-action-btn" data-folder-play="${escapeHtml(folder.path)}" type="button" title="Play folder" aria-label="Play ${escapeHtml(folder.name)}">▶</button>
                <button class="folder-action-btn folder-action-btn--delete" data-folder-delete="${escapeHtml(folder.path)}" type="button" title="Delete folder" aria-label="Delete ${escapeHtml(folder.name)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
            </div>
        </div>`).join('');
    }

    html += filteredTracks.map(track => {
        const isSelected = selectedIds.has(track.id);
        const artist = (track.artist || '').trim();
        const album = (track.album || '').trim();
        const metadataLine = [artist, album].filter(Boolean).join(' · ');
        const subline = metadataLine || (folderMode ? getTrackFilename(track) : getTrackFolder(track));
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + escapeHtml(track.id) + '">' +
            detailTrackRowHtml({
                title: escapeHtml(track.title || 'Unknown'),
                sub: subline ? escapeHtml(subline) : '',
                thumb: trackThumbHtml(track),
                favoriteButton: libraryFavoriteButtonHtml(track.id, !!track.favorite),
                selectionButton: librarySelectionButtonHtml(track.id, isSelected),
                duration: formatTime(track.duration),
            }) +
        '</div>';
    }).join('');

    elements.tracksList.innerHTML = html;

    elements.tracksList.querySelectorAll('.track-play[data-folder]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            setLibraryFolder(item.dataset.folder || '');
        });
    });

    elements.tracksList.querySelectorAll('.folder-action-btn[data-folder-play]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await playLibraryFolder(btn.dataset.folderPlay || '');
        });
    });
    elements.tracksList.querySelectorAll('.folder-action-btn[data-folder-delete]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const folder = btn.dataset.folderDelete || '';
            await deleteLibraryFolder(folder);
        });
    });

    elements.tracksList.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        row.querySelector('.track-play').addEventListener('click', (e) => {
            e.stopPropagation();
            playLocal(row.dataset.trackId);
        });
    });
    bindTrackFavoriteRowButtons(elements.tracksList);

    elements.tracksList.querySelectorAll('.track-play[data-playlist-id]').forEach(item => {
        item.addEventListener('click', async (e) => {
            e.stopPropagation();
            await loadPlaylistById(item.dataset.playlistId, { autoplay: true });
        });
    });

    elements.tracksList.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });

    // Whole-row clicks start the row action like the album-detail rows do,
    // while selection, favorite and folder/playlist/detail actions keep their
    // own stopPropagation handlers.
    elements.tracksList.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.track-add, .track-row-favorite, .track-fav, .track-play')) return;
            playLocal(row.dataset.trackId);
        });
    });
    elements.tracksList.querySelectorAll('.folder-item[data-folder]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.folder-actions, .track-play')) return;
            setLibraryFolder(row.dataset.folder || '');
        });
    });
    elements.tracksList.querySelectorAll('.playlist-item[data-playlist-id]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.playlist-download-btn, .playlist-delete-btn, .track-play')) return;
            const rowId = row.dataset.playlistId;
            if (rowId) void loadPlaylistById(rowId, { autoplay: true });
        });
    });

    elements.tracksList.querySelectorAll('.playlist-download-btn[data-playlist-download]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const playlistId = btn.dataset.playlistDownload;
            await downloadPlaylistById(playlistId);
        });
    });
    elements.tracksList.querySelectorAll('.playlist-delete-btn[data-playlist-delete]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const playlistId = btn.dataset.playlistDelete;
            const playlist = state.playlists.find(p => p.id === playlistId);
            if (!playlist) return;
            if (!confirm(`Delete playlist "${playlist.name}"?`)) return;
            deletePlaylistById(playlistId);
        });
    });

    updateLibrarySelectionUI();
}
function setLibraryViewMode(mode) {
    // Favorites is a tab but reuses the albums view with the existing
    // favorites filter (same position as the Playlists tab in TIDAL: fourth tab).
    if (mode === 'favorites') {
        state.library.viewMode = 'albums';
        state.library.showFavoriteAlbums = true;
    } else {
        state.library.viewMode = mode === 'folders' ? 'folders' : mode === 'albums' ? 'albums' : 'tracks';
        if (mode === 'albums') state.library.showFavoriteAlbums = false;
    }
    if (state.library.viewMode === 'tracks') state.library.currentFolder = '';
    if (state.library.viewMode === 'albums') {
        state.library.albumDetail = null;
        state.library.playlistDetail = null;
        if (!state.library.albumsLoaded) {
            fetchAlbums();
        } else {
            renderAlbums();
        }
    } else {
        renderTracks();
    }
    updateLibrarySearchPlaceholder();
}
    updateLibrarySearchPlaceholder();
function setLibraryFolder(folder) {
    updateLibrarySearchPlaceholder();
    state.library.viewMode = 'folders';
    state.library.currentFolder = folder || '';
    renderTracks();
}
// ── Albums ──────────────────────────────────────────────────────

// Grid/list layout for tile collections (Library albums + TIDAL
// Albums/Artists/Playlists); storage helpers live above state init so the
// stored layout is available when state is first built.
function storeViewMode(surface, mode) {
    try {
        localStorage.setItem(VIEW_MODE_STORAGE_KEY + surface, mode);
    } catch (e) { /* keep working without persistence */ }
}
function setAlbumLayout(mode) {
    if (state.library.viewMode !== 'albums' || state.library.albumDetail || state.library.playlistDetail) return;
    const layout = mode === 'list' ? 'list' : 'grid';
    storeViewMode('library-albums', layout);
    state.library.albumLayout = layout;
    renderAlbums();
}
function updateLibraryViewModeToggle() {
    const active = state.library.viewMode === 'albums' && !state.library.albumDetail && !state.library.playlistDetail;
    if (elements.libraryViewModeToggle) elements.libraryViewModeToggle.classList.toggle('hidden', !active);
    const layout = state.library.albumLayout || 'grid';
    if (elements.libraryViewModeGridBtn) {
        elements.libraryViewModeGridBtn.classList.toggle('active', layout === 'grid');
        elements.libraryViewModeGridBtn.setAttribute('aria-pressed', layout === 'grid' ? 'true' : 'false');
    }
    if (elements.libraryViewModeListBtn) {
        elements.libraryViewModeListBtn.classList.toggle('active', layout === 'list');
        elements.libraryViewModeListBtn.setAttribute('aria-pressed', layout === 'list' ? 'true' : 'false');
    }
}

let albumsFetchInFlight = false;
async function fetchAlbums() {
    if (state.library.albumsLoaded && state.library.albums.length > 0) return;
    if (albumsFetchInFlight) return;
    albumsFetchInFlight = true;
    try {
        const res = await fetch('/api/albums');
        if (!res.ok) return;
        const albums = await res.json();
        state.library.albums = albums;
        // An empty list while the scan is still running is not an answer yet:
        // keeping the view in its scan/loading state until a fetch lands after
        // the scan finished stops the empty message from flashing while the
        // albums are still arriving.
        state.library.albumsLoaded = albums.length > 0 || !state.library.scanning;
        state.library.albumsCacheToken = Date.now();
        // Never clobber an open album/playlist detail with a background
        // re-render; the late init fetch would otherwise close the detail.
        if (state.library.viewMode === 'albums' && !state.library.albumDetail && !state.library.playlistDetail) {
            renderAlbums();
        }
    } catch (e) {
        console.warn('Failed to fetch albums', e);
    } finally {
        albumsFetchInFlight = false;
    }
}

function renderAlbums() {
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    updateLibrarySelectionUI();
    const loadingEl = document.querySelector('#tab-library .content-state');
    const query = (state.library.searchQuery || '').trim().toLowerCase();

    // Hide tracks list + detail views, show albums grid
    elements.tracksList.classList.add('hidden');
    elements.albumDetail.classList.add('hidden');
    if (elements.playlistDetail) elements.playlistDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    if (elements.libraryFolderPath) elements.libraryFolderPath.classList.add('hidden');

    if (!state.library.albumsLoaded) {
        // Albums not yet loaded — show loading state and trigger fetch. A
        // running scan reports its progress here for the same reason as in
        // the tracks view: the albums arrive when it is done.
        window.FXRouteContentState.set(loadingEl, 'loading', formatLibraryScanStatus() || 'Loading albums…');
        elements.albumsGrid.classList.add('hidden');
        fetchAlbums();
        return;
    }

    let albums = state.library.albums || [];
    if (query) {
        albums = albums.filter(albumMatchesLibraryQuery);
    }
    if (state.library.showFavoriteAlbums) {
        albums = albums.filter(album => !!album.favorite);
    }
    const showSmartFavorites = state.library.showFavoriteAlbums;
    // Playlists live with the personal collections (Favorites), not in the
    // plain albums overview.
    const playlists = showSmartFavorites ? getFilteredPlaylists() : [];

    if (albums.length === 0 && playlists.length === 0 && !showSmartFavorites) {
        // A running scan is not an empty library: the shared library scan
        // status stays visible until the scan actually finished, so the empty
        // message never reports "no albums" while the scan is still filling
        // the cache. The scan poll re-renders this view as tracks arrive.
        const scanText = formatLibraryScanStatus();
        if (scanText) {
            window.FXRouteContentState.set(loadingEl, 'loading', scanText);
        } else {
            window.FXRouteContentState.set(loadingEl, 'empty',
                query ? 'No matching albums.' : 'No albums found. Import music with album tags.');
        }
        elements.albumsGrid.innerHTML = '';
        elements.albumsGrid.classList.remove('hidden');
        return;
    }
    window.FXRouteContentState.hide(loadingEl);

    const smartHtml = showSmartFavorites ? `
        <div class="album-card album-card-smart" data-smart-favorite="top40" role="button" tabindex="0">
            <div class="album-art-wrap">
                <div class="album-smart-badge">Smart Mix</div>
                <img class="album-art" src="/static/Top40.png?v=${state.library.albumsCacheToken || ''}"
                     alt="Top 40"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${albumArtFallbackSvg('Top 40')}'" />
            </div>
            <div class="album-name">Top 40</div>
            <div class="album-artist">Most Played Tracks</div>
        </div>
    ` : '';
    // Grid and list show the same cards; only the container class changes.
    elements.albumsGrid.classList.toggle('is-list', (state.library.albumLayout || 'grid') === 'list');
    const playlistHtml = playlists.map(playlist => `
        <div class="album-card playlist-card" data-playlist-id="${escapeHtml(playlist.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">${playlistCoverHtml(playlist)}</div>
            <button type="button" class="album-card-fav is-active" data-playlist-fav="${escapeHtml(playlist.id)}" aria-label="Delete playlist" title="Delete playlist">${favoriteHeartSvg()}</button>
            <div class="album-name">${escapeHtml(playlist.name)}</div>
            <div class="album-artist">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
        </div>`).join('');
    const manualHtml = albums.length > 0 ? albums.map(album => {
        const coverUrl = albumCoverUrl(album);
        const fallbackSvg = albumArtFallbackSvg(album.name || album.artist || 'Album');
        const imageSrc = coverUrl || fallbackSvg;
        const favClass = album.favorite ? ' is-active' : '';
        return `
        <div class="album-card" data-album-id="${escapeHtml(album.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">
                <img class="album-art" src="${escapeHtml(imageSrc)}"
                     alt="${escapeHtml(album.name)}"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${fallbackSvg}'" />
            </div>
            <button type="button" class="album-card-fav${favClass}" data-fav-id="${escapeHtml(album.id)}" aria-label="${album.favorite ? 'Remove from favorites' : 'Add to favorites'}" title="${album.favorite ? 'Remove from favorites' : 'Add to favorites'}">${favoriteHeartSvg()}</button>
            <div class="album-name">${escapeHtml(album.name)}</div>
            <div class="album-artist">${escapeHtml(album.artist)}</div>
        </div>`;
    }).join('') : '';
    elements.albumsGrid.innerHTML = smartHtml + playlistHtml + manualHtml;
    elements.albumsGrid.classList.remove('hidden');

    const openAlbumCard = (card) => () => {
        if (card.dataset.playlistId) openPlaylistDetail(card.dataset.playlistId);
        else if (card.dataset.smartFavorite) openSmartTopTracks();
        else openAlbumDetail(card.dataset.albumId);
    };
    const handleAlbumCardKeydown = (card) => (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        openAlbumCard(card)();
    };
    elements.albumsGrid.querySelectorAll('.album-card').forEach(card => {
        card.addEventListener('click', openAlbumCard(card));
        card.addEventListener('keydown', handleAlbumCardKeydown(card));
    });
    // Heart toggle: reuse the same local album favorite endpoint the detail
    // view uses; the card stays visible in all views (favorite toggles don't
    // remove albums from the main grid).
    elements.albumsGrid.querySelectorAll('.album-card-fav').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleAlbumCardFavorite(btn.dataset.favId);
        });
    });
    // Local playlist heart: always active (own playlist); heart-off deletes
    // the playlist via the existing delete path.
    elements.albumsGrid.querySelectorAll('[data-playlist-fav]').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            const playlistId = btn.dataset.playlistFav;
            const playlist = state.playlists.find(p => p.id === playlistId);
            if (!playlist) return;
            if (!confirm(`Delete playlist "${playlist.name}"?`)) return;
            deletePlaylistById(playlistId);
        });
    });
}

function albumMatchesLibraryQuery(album) {
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    if (!query) return true;
    const haystack = [
        album.name,
        album.artist,
        album.release_type,
        album.country,
        album.label,
        ...(album.genres || []),
        ...(album.years || []),
        album.year,
    ]
        .filter(value => value !== null && value !== undefined && value !== '')
        .join(' ')
        .toLowerCase();
    return haystack.includes(query);
}

function albumHasCover(album) {
    return !!(album && (album.cover_source || album.has_external_cover));
}

function albumCoverUrl(album) {
    if (!albumHasCover(album) || !album.id) return '';
    if (album.demo_cover_url) return album.demo_cover_url;
    return `/api/albums/${encodeURIComponent(album.id)}/cover?v=${state.library.albumsCacheToken || ''}`;
}

function setAlbumCoverImage(img, coverUrl, fallbackText) {
    if (!img) return;
    const fallbackSvg = albumArtFallbackSvg(fallbackText || 'Album');
    img.onerror = function() {
        this.onerror = null;
        this.src = fallbackSvg;
    };
    img.src = coverUrl || fallbackSvg;
}

function setAlbumDetailBackdrop(coverUrl) {
    const image = elements.albumDetailBackdrop;
    const backdrop = image?.closest('.detail-header-backdrop');
    if (!image || !backdrop) return;
    if (!coverUrl) {
        image.removeAttribute('src');
        backdrop.hidden = true;
        return;
    }
    image.onerror = function() {
        this.onerror = null;
        this.removeAttribute('src');
        backdrop.hidden = true;
    };
    backdrop.hidden = false;
    image.src = coverUrl;
}

function setPlaylistDetailBackdrop(playlist) {
    const image = elements.playlistDetailBackdrop;
    const backdrop = image?.closest('.detail-header-backdrop');
    if (!image || !backdrop) return;
    // Reuse the first distinct album cover of the collage as the blurred
    // backdrop, mirroring how the album detail blurs its own cover.
    const albums = playlistDistinctAlbums(playlist).filter(a => a?.id);
    const coverUrl = albums.length ? albumCoverUrl(albums[0]) : '';
    if (!coverUrl) {
        image.removeAttribute('src');
        backdrop.hidden = true;
        return;
    }
    image.onerror = function() {
        this.onerror = null;
        this.removeAttribute('src');
        backdrop.hidden = true;
    };
    backdrop.hidden = false;
    image.src = coverUrl;
}

// Header info line for a local playlist: a single-artist playlist reuses the
// enriched MusicBrainz artist text (same source as the album About); a
// multi-artist playlist gets a compact featuring line from the actual artists.
// No artificial multi-artist biography is ever assembled.
function renderPlaylistDetailInfo(playlist, tracks) {
    const infoEl = elements.playlistDetailInfo;
    if (!infoEl) return;
    const names = [];
    const seen = new Set();
    for (const t of (tracks || [])) {
        const name = String(t.artist || '').trim();
        const key = name.toLowerCase();
        if (name && !seen.has(key)) {
            seen.add(key);
            names.push(name);
        }
    }
    if (names.length === 1) {
        const album = tracks && tracks.length ? findAlbumForTrack(tracks[0]) : null;
        const about = ((album && album.artist_description) || '').trim();
        infoEl.innerHTML = about
            ? detailAboutHtml('About this artist', about)
            : '';
        return;
    }
    const line = playlistFeaturingLine(names);
    infoEl.innerHTML = line ? `<div>${escapeHtml(line)}</div>` : '';
}

function playlistFeaturingLine(names) {
    if (!names || names.length < 2) return '';
    const shown = names.slice(0, 3);
    const more = names.length > 3;
    let body;
    if (more) {
        body = shown.join(', ') + ' and more';
    } else if (shown.length === 2) {
        body = shown[0] + ' and ' + shown[1];
    } else {
        body = shown[0] + ', ' + shown[1] + ' and ' + shown[2];
    }
    return 'Featuring ' + body + '.';
}

// Square cover thumbnail for the left of a track row. Resolves the track's
// album cover (same lookup the playlist collage uses) and falls back to the
// neutral :empty placeholder when there is no artwork.
function trackThumbHtml(track) {
    const album = findAlbumForTrack(track);
    const coverUrl = album ? albumCoverUrl(album) : '';
    const fallback = artworkPlaceholderUrl();
    if (!coverUrl) {
        return '<div class="track-thumb" aria-hidden="true"><img src="' + escapeHtml(fallback) +
            '" alt="" loading="lazy" /></div>';
    }
    return '<div class="track-thumb" aria-hidden="true"><img src="' + escapeHtml(coverUrl) +
        '" alt="" loading="lazy" onerror="this.onerror=null;this.src=\'' + escapeHtml(fallback) + '\'" /></div>';
}

function findAlbumForTrack(track) {
    const name = (track?.album || '').trim();
    if (!name) return null;
    const artist = (track.album_artist || track.artist || '').trim();
    const albums = state.library.albums || [];
    const byName = albums.filter(a => (a.name || '').trim().toLowerCase() === name.toLowerCase());
    // Exact artist + album match first (mirrors backend album grouping).
    const byArtist = byName.find(a => (a.artist || '').trim().toLowerCase() === artist.toLowerCase());
    if (byArtist) return byArtist;
    // Fall back to a unique album name (handles compilations / Various).
    if (byName.length === 1) return byName[0];
    return null;
}

function playlistDistinctAlbums(playlist) {
    const tracksById = new Map((state.library.tracks || []).map(t => [t.id, t]));
    const albums = [];
    const seen = new Set();
    for (const id of (playlist?.track_ids || [])) {
        const track = tracksById.get(id);
        if (!track) continue;
        const album = findAlbumForTrack(track);
        const key = album?.id
            || ('noalbum::' + (track.album || '').trim().toLowerCase() + '::' + (track.album_artist || track.artist || '').trim().toLowerCase());
        if (seen.has(key)) continue;
        seen.add(key);
        albums.push(album);
        if (albums.length >= 4) break;
    }
    return albums;
}

function playlistCoverHtml(playlist) {
    const albums = playlistDistinctAlbums(playlist).filter(a => a?.id);
    if (albums.length === 0) {
        return `<div class="playlist-collage-fallback" aria-hidden="true">${playlistFallbackMarkSvg()}</div>`;
    }
    const count = Math.min(albums.length, 4);
    const cells = albums.slice(0, count).map(album => {
        const coverUrl = albumCoverUrl(album);
        const isFallback = !coverUrl;
        return `<img class="playlist-collage-cell${isFallback ? ' is-fallback' : ''}"
            src="${escapeHtml(coverUrl || artworkPlaceholderUrl())}"
            alt="${escapeHtml(album.name || '')}"
            loading="lazy"
            onerror="this.onerror=null;this.classList.add('is-fallback');this.src='${escapeHtml(artworkPlaceholderUrl())}';" />`;
    }).join('');
    return `<div class="playlist-collage playlist-collage--${count}">${cells}</div>`;
}

function playlistFallbackMarkSvg() {
    return `<img class="playlist-collage-fallback-mark" src="${escapeHtml(artworkPlaceholderUrl())}" alt="" />`;
}

// The library tab has no inner scroll container (neither #tab-library nor
// #tab-content nor their ancestors set an overflow), so the grids and the
// detail views share the window scroll. Opening a detail must reset it,
// otherwise the detail inherits the grid position and starts mid-page at
// the tracks instead of at the cover/title hero.
function scrollLibraryDetailToTop() {
    window.scrollTo(0, 0);
}

async function openAlbumDetail(albumId) {
    const album = (state.library.albums || []).find(a => a.id === albumId);
    if (!album) return;

    try {
        const res = await fetch(`/api/albums/${encodeURIComponent(albumId)}/tracks`);
        if (!res.ok) return;
        const tracks = await res.json();
        state.library.albumDetail = { album, tracks };
        state.library.playlistDetail = null;
        updateLibraryViewModeToggle();

        // Update detail header
        const coverUrl = albumCoverUrl(album);
        setAlbumCoverImage(elements.albumDetailCover, coverUrl, album.name || album.artist || 'Album');
        setAlbumDetailBackdrop(coverUrl);
        elements.albumDetailName.textContent = album.name;
        elements.albumDetailArtist.textContent = album.artist;
        updateAlbumFavoriteButton(album);
        elements.albumDetail.querySelectorAll('.album-detail-facts, .album-detail-about').forEach(node => node.remove());
        const factsHtml = albumFactsHtml(album);
        if (factsHtml) {
            elements.albumDetailCount.insertAdjacentHTML('afterend', factsHtml);
        }
        const aboutHtml = albumAboutHtml(album);
        if (aboutHtml) {
            const anchor = elements.albumDetail.querySelector('.album-detail-facts') || elements.albumDetailCount;
            anchor.insertAdjacentHTML('afterend', aboutHtml);
        }

        renderAlbumDetailTracks();
        loadAlbumDiscover(albumId);

        // Show detail, hide grid
        elements.albumsGrid.classList.add('hidden');
        if (elements.playlistDetail) elements.playlistDetail.classList.add('hidden');
        elements.albumDetail.classList.remove('hidden');
        updatePlaylistSaveRowVisibility();
        scrollLibraryDetailToTop();
    } catch (e) {
        console.warn('Failed to load album tracks', e);
    }
}

async function openSmartTopTracks() {
    try {
        const res = await fetch('/api/smart/top-tracks?limit=40');
        const tracks = await res.json().catch(() => []);
        if (!res.ok) throw new Error('Failed to load Top 40');
        const album = {
            id: 'smart_top40',
            name: 'Top 40',
            artist: 'Most Played Tracks',
            smart: true,
            coverUrl: '/static/Top40.png',
        };
        state.library.albumDetail = { album, tracks: Array.isArray(tracks) ? tracks : [] };
        state.library.playlistDetail = null;
        updateLibraryViewModeToggle();
        const knownIds = new Set(state.library.tracks.map(t => t.id));
        for (const track of state.library.albumDetail.tracks) {
            if (track?.id && !knownIds.has(track.id)) {
                state.library.tracks.push(track);
                knownIds.add(track.id);
            }
        }
        setAlbumCoverImage(
            elements.albumDetailCover,
            `${album.coverUrl}?v=${state.library.albumsCacheToken || ''}`,
            album.name
        );
        setAlbumDetailBackdrop(`${album.coverUrl}?v=${state.library.albumsCacheToken || ''}`);
        elements.albumDetailName.textContent = album.name;
        elements.albumDetailArtist.textContent = album.artist;
        elements.albumDetailCount.textContent = `${state.library.albumDetail.tracks.length} track${state.library.albumDetail.tracks.length === 1 ? '' : 's'}`;
        updateAlbumFavoriteButton(album);
        elements.albumDetail.querySelectorAll('.album-detail-facts, .album-detail-about').forEach(node => node.remove());
        renderAlbumDetailTracks();
        if (elements.albumDiscover) {
            elements.albumDiscover.classList.add('hidden');
            elements.albumDiscover.innerHTML = '';
        }
        elements.albumsGrid.classList.add('hidden');
        if (elements.playlistDetail) elements.playlistDetail.classList.add('hidden');
        elements.albumDetail.classList.remove('hidden');
        updatePlaylistSaveRowVisibility();
        scrollLibraryDetailToTop();
    } catch (e) {
        console.warn('Failed to load Top 40', e);
        showToast('Failed to load Top 40', 'error');
    }
}

async function loadAlbumDiscover(albumId) {
    if (!elements.albumDiscover) return;
    elements.albumDiscover.classList.remove('hidden');
    elements.albumDiscover.innerHTML = albumDiscoverShellHtml('loading');
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(albumId)}/discover`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to load suggestions');
        renderAlbumDiscover(data.items || []);
    } catch (e) {
        console.warn('Failed to load album discover suggestions', e);
        elements.albumDiscover.classList.add('hidden');
        elements.albumDiscover.innerHTML = '';
    }
}

function renderAlbumDiscover(items) {
    if (!elements.albumDiscover) return;
    if (!Array.isArray(items) || items.length === 0) {
        elements.albumDiscover.classList.add('hidden');
        elements.albumDiscover.innerHTML = '';
        return;
    }
    const rows = items.slice(0, 6).map((item) => {
        const artist = escapeHtml(item.artist || 'Unknown artist');
        return `
            <li class="album-discover-item">
                <span class="album-discover-title">${artist}</span>
            </li>
        `;
    }).join('');
    elements.albumDiscover.classList.remove('hidden');
    elements.albumDiscover.innerHTML = `
        <details class="album-discover-panel">
            <summary>
                <span>Discover similar music</span>
            </summary>
            <ul class="album-discover-list">${rows}</ul>
        </details>
    `;
}

function albumDiscoverShellHtml(stateName) {
    const note = stateName === 'loading' ? 'Looking up similar artists…' : 'No suggestions yet.';
    return `
        <details class="album-discover-panel">
            <summary>
                <span>Discover similar music</span>
                <small>${escapeHtml(note)}</small>
            </summary>
        </details>
    `;
}

function renderAlbumDetailTracks() {
    const detail = state.library.albumDetail;
    if (!detail || !elements.albumDetailTracks) return;
    const albumId = detail.album.id;
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    const tracks = query
        ? (detail.tracks || []).filter(track => trackMatchesLibraryQuery(track, query))
        : (detail.tracks || []);
    const total = (detail.tracks || []).length;
    const trackCount = query
        ? `${tracks.length} of ${total} track${total === 1 ? '' : 's'}`
        : `${total} track${total === 1 ? '' : 's'}`;
    const album = detail.album;
    elements.albumDetailCount.textContent = [trackCount, album.release_type, album.year, album.country]
        .filter(Boolean).join(' · ');
    if (tracks.length === 0) {
        elements.albumDetailTracks.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
    const selectedIds = new Set(state.library.selectedTrackIds);
    elements.albumDetailTracks.innerHTML = tracks.map((track, index) => {
        const isSelected = selectedIds.has(track.id);
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + escapeHtml(track.id) + '" data-album-context="' + escapeHtml(albumId) + '">' +
            detailTrackRowHtml({
                index: index + 1,
                title: escapeHtml(track.title || 'Unknown'),
                sub: escapeHtml((track.artist || '').trim()),
                thumb: trackThumbHtml(track),
                favoriteButton: detailFavoriteButtonHtml(track.id, !!track.favorite),
                selectionButton: librarySelectionButtonHtml(track.id, isSelected),
                duration: track.duration ? formatTime(track.duration) : '',
            }) +
        '</div>';
    }).join('');
    // Whole-row and round play-button clicks both start the track (matching
    // the Tidal detail rows); the selection Plus and favorite button stop
    // propagation themselves.
    elements.albumDetailTracks.querySelectorAll('.track-item').forEach(row => {
        const play = () => playTrackInAlbum(row.dataset.trackId, row.dataset.albumContext);
        row.querySelector('.track-play').addEventListener('click', (event) => {
            event.stopPropagation();
            play();
        });
        row.addEventListener('click', (event) => {
            if (event.target && event.target.closest('.track-add, .track-fav, .track-row-favorite')) return;
            play();
        });
    });
    elements.albumDetailTracks.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });
    bindTrackFavoriteRowButtons(elements.albumDetailTracks);
}

function libraryFavoriteButtonHtml(trackId, favorite) {
    const heart = favoriteHeartSvg();
    return '<button class="track-row-favorite' + (favorite ? ' active' : '') + '" data-track-favorite="' + escapeHtml(trackId) + '" type="button"' +
        ' aria-pressed="' + (favorite ? 'true' : 'false') + '"' +
        ' aria-label="' + (favorite ? 'Remove track from favorites' : 'Add track to favorites') + '"' +
        ' title="' + (favorite ? 'Remove from favorites' : 'Add to favorites') + '">' + heart + '</button>';
}

function detailFavoriteButtonHtml(trackId, favorite) {
    const heart = favoriteHeartSvg();
    return '<button class="track-fav' + (favorite ? ' active' : '') + '" data-track-favorite="' + escapeHtml(trackId) + '" type="button"' +
        ' aria-pressed="' + (favorite ? 'true' : 'false') + '"' +
        ' aria-label="' + (favorite ? 'Remove track from favorites' : 'Add track to favorites') + '"' +
        ' title="' + (favorite ? 'Remove from favorites' : 'Add to favorites') + '">' + heart + '</button>';
}

function librarySelectionButtonHtml(trackId, isSelected) {
    const mark = isSelected ? '✓' : '+';
    return '<button class="track-add' + (isSelected ? ' is-active' : '') + '" data-track-add="' + escapeHtml(trackId) + '" type="button"' +
        ' aria-pressed="' + (isSelected ? 'true' : 'false') + '"' +
        ' aria-label="' + (isSelected ? 'Remove track from selection' : 'Add track to selection') + '"' +
        ' title="' + (isSelected ? 'Remove from selection' : 'Add to selection') + '">' + mark + '</button>';
}

// Shared detail track-row body for the library list view, album / playlist
// detail, and (via the init api) TIDAL detail rows.  One row language:
// optional index, round play button, stacked title / sub, optional album
// context, selection Plus, favorite, duration. Rows with an index wrap the
// number and the play button in one leading element so narrow phones can
// share a single slot; rows without an index keep a lone play button.
function detailTrackRowHtml({ index, title, sub, album, favoriteButton, selectionButton, duration, thumb }) {
    const playButton = '<button type="button" class="track-play" title="Play">▶</button>';
    const lead = index != null
        ? '<span class="track-numplay"><span class="track-index">' + index + '</span>' + playButton + '</span>'
        : playButton;
    return (
        lead +
        (thumb || '') +
        '<div class="track-info">' +
            '<div class="track-title">' + title + '</div>' +
            (sub ? '<div class="track-sub">' + sub + '</div>' : '') +
        '</div>' +
        (album ? '<div class="track-album">' + album + '</div>' : '') +
        (selectionButton || '') +
        favoriteButton +
        (duration ? '<span class="track-duration">' + duration + '</span>' : '')
    );
}

function updateAlbumFavoriteButton(album) {
    if (!elements.albumFavoriteToggle) return;
    if (album?.smart) {
        elements.albumFavoriteToggle.classList.add('hidden');
        elements.albumFavoriteToggle.disabled = true;
        return;
    }
    elements.albumFavoriteToggle.classList.remove('hidden');
    elements.albumFavoriteToggle.disabled = false;
    const favorite = !!album?.favorite;
    elements.albumFavoriteToggle.innerHTML = favoriteHeartSvg();
    elements.albumFavoriteToggle.classList.toggle('active', favorite);
    elements.albumFavoriteToggle.setAttribute('aria-pressed', favorite ? 'true' : 'false');
    elements.albumFavoriteToggle.setAttribute('aria-label', favorite ? 'Remove album from favorites' : 'Add album to favorites');
    elements.albumFavoriteToggle.title = favorite ? 'Remove from favorites' : 'Add to favorites';
}

async function toggleCurrentAlbumFavorite() {
    const detail = state.library.albumDetail;
    const album = detail?.album;
    if (!album || !elements.albumFavoriteToggle) return;
    const nextFavorite = !album.favorite;
    elements.albumFavoriteToggle.disabled = true;
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(album.id)}/favorite`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorite: nextFavorite }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to update favorite');
        album.favorite = !!data.favorite;
        const stored = (state.library.albums || []).find(item => item.id === album.id);
        if (stored) stored.favorite = album.favorite;
        updateAlbumFavoriteButton(album);
        showToast(album.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
    } catch (e) {
        showToast(e.message || 'Failed to update favorite', 'error');
    } finally {
        elements.albumFavoriteToggle.disabled = false;
    }
}

async function toggleAlbumCardFavorite(albumId) {
    const stored = (state.library.albums || []).find(item => item.id === albumId);
    if (!stored) return;
    const nextFavorite = !stored.favorite;
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(albumId)}/favorite`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorite: nextFavorite }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to update favorite');
        stored.favorite = !!data.favorite;
        // Also update the detail-page album if it's the same one.
        const detailAlbum = state.library.albumDetail?.album;
        if (detailAlbum && detailAlbum.id === albumId) {
            detailAlbum.favorite = stored.favorite;
            updateAlbumFavoriteButton(detailAlbum);
        }
        // Update the grid buttons for this album id.
        document.querySelectorAll('.album-card-fav[data-fav-id="' + CSS.escape(albumId) + '"]').forEach((btn) => {
            const f = stored.favorite;
            btn.classList.toggle('is-active', f);
            btn.innerHTML = favoriteHeartSvg();
            btn.setAttribute('aria-label', f ? 'Remove from favorites' : 'Add to favorites');
            btn.title = f ? 'Remove from favorites' : 'Add to favorites';
        });
        // The Favorites view must drop an unfavorited album immediately;
        // re-render the grid so the filter stays authoritative.
        if (state.library.showFavoriteAlbums && !state.library.albumDetail) {
            renderAlbums();
        }
        showToast(stored.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
    } catch (e) {
        showToast(e.message || 'Failed to update favorite', 'error');
    }
}

function albumFactsHtml(album) {
    const label = album.label ? `Label: ${album.label}` : '';
    const genres = (album.genres || []).slice(0, 3).filter(Boolean);
    const genreLine = genres.length ? `Genre: ${genres.join(' / ')}` : '';
    return detailFactsHtml([[label, genreLine]]);
}

// Shared metadata rows: optional fields wrap as units, without empty rows or
// dangling separators. Streaming receives the same builder via the init api.
function detailFactsHtml(lines) {
    const rows = (lines || []).map(line => {
        const fields = (Array.isArray(line) ? line : [line]).filter(Boolean);
        if (!fields.length) return '';
        return `<div class="detail-fact-row">${fields.map(field => `<span>${escapeHtml(field)}</span>`).join('')}</div>`;
    }).join('');
    if (!rows) return '';
    return `<div class="album-detail-facts">${rows}</div>`;
}

function albumAboutHtml(album) {
    const albumDescription = (album.album_description || '').trim();
    const artistDescription = (album.artist_description || '').trim();
    const description = albumDescription || artistDescription;
    if (!description) return '';
    const label = albumDescription ? 'About this album' : 'About this artist';
    return detailAboutHtml(label, description);
}

// Short enrichment text is directly readable, with an accessible section label
// instead of an extra heading row or an expansion-driven layout change.
function detailAboutHtml(label, description) {
    if (!description || !description.trim()) return '';
    return `
        <section class="album-detail-about detail-description" aria-label="${escapeHtml(label)}">
            <p>${escapeHtml(description)}</p>
        </section>
    `;
}

function closeAlbumDetail() {
    state.library.albumDetail = null;
    // Re-render the grid (instead of just unhiding it) so newly saved
    // playlists appear as tiles as soon as the user leaves the album.
    renderAlbums();
}

async function playTrackInAlbum(trackId, albumId) {
    const album = state.library.albumDetail;
    if (!album) return;
    const albumTrackIds = (album.tracks || []).map(t => t.id);
    // Make sure album tracks are known to the library state
    const knownIds = new Set(state.library.tracks.map(t => t.id));
    for (const t of (album.tracks || [])) {
        if (!knownIds.has(t.id)) {
            state.library.tracks.push(t);
            knownIds.add(t.id);
        }
    }
    await playLocal(trackId, albumTrackIds);
}

// ── Playlist detail ────────────────────────────────────────────

function resolvePlaylistTracks(playlist) {
    const ids = getTrackIdsInLibraryOrder(playlist?.track_ids || []);
    const byId = new Map((state.library.tracks || []).map(t => [t.id, t]));
    return ids.map(id => byId.get(id)).filter(Boolean);
}

function openPlaylistDetail(playlistId) {
    const playlist = (state.playlists || []).find(p => p.id === playlistId);
    if (!playlist) return;
    const tracks = resolvePlaylistTracks(playlist);
    state.library.playlistDetail = { playlist, tracks };
    state.library.albumDetail = null;
    updateLibraryViewModeToggle();

    if (elements.playlistDetailCover) {
        elements.playlistDetailCover.innerHTML = playlistCoverHtml(playlist);
    }
    if (elements.playlistDetailName) elements.playlistDetailName.textContent = playlist.name;
    setPlaylistDetailBackdrop(playlist);
    renderPlaylistDetailInfo(playlist, tracks);
    renderPlaylistDetailTracks();

    elements.albumsGrid.classList.add('hidden');
    elements.albumDetail.classList.add('hidden');
    if (elements.playlistDetail) elements.playlistDetail.classList.remove('hidden');
    updatePlaylistSaveRowVisibility();
    scrollLibraryDetailToTop();
}

function renderPlaylistDetailTracks() {
    const detail = state.library.playlistDetail;
    if (!detail || !elements.playlistDetailTracks) return;
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    const tracks = query
        ? (detail.tracks || []).filter(track => trackMatchesLibraryQuery(track, query))
        : (detail.tracks || []);
    const total = (detail.tracks || []).length;
    if (elements.playlistDetailCount) {
        elements.playlistDetailCount.textContent = query
            ? `${tracks.length} of ${total} track${total === 1 ? '' : 's'}`
            : `${total} track${total === 1 ? '' : 's'}`;
    }
    if (tracks.length === 0) {
        elements.playlistDetailTracks.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
    const selectedIds = new Set(state.library.selectedTrackIds);
    elements.playlistDetailTracks.innerHTML = tracks.map((track, index) => {
        const isSelected = selectedIds.has(track.id);
        const sub = escapeHtml([(track.artist || '').trim(), (track.album || '').trim()].filter(Boolean).join(' · '));
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + escapeHtml(track.id) + '">' +
            detailTrackRowHtml({
                index: index + 1,
                title: escapeHtml(track.title || 'Unknown'),
                sub,
                thumb: trackThumbHtml(track),
                favoriteButton: detailFavoriteButtonHtml(track.id, !!track.favorite),
                selectionButton: librarySelectionButtonHtml(track.id, isSelected),
                duration: track.duration ? formatTime(track.duration) : '',
            }) +
        '</div>';
    }).join('');
    elements.playlistDetailTracks.querySelectorAll('.track-item').forEach(row => {
        const play = () => playTrackInPlaylist(row.dataset.trackId);
        row.querySelector('.track-play').addEventListener('click', (event) => {
            event.stopPropagation();
            play();
        });
        row.addEventListener('click', (event) => {
            if (event.target && event.target.closest('.track-add, .track-fav, .track-row-favorite')) return;
            play();
        });
    });
    elements.playlistDetailTracks.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });
    bindTrackFavoriteRowButtons(elements.playlistDetailTracks);
}

function closePlaylistDetail() {
    state.library.playlistDetail = null;
    renderAlbums();
}

async function playTrackInPlaylist(trackId) {
    const detail = state.library.playlistDetail;
    if (!detail) return;
    const trackIds = (detail.tracks || []).map(t => t.id);
    // Make sure playlist tracks are known to the library state
    const knownIds = new Set(state.library.tracks.map(t => t.id));
    for (const t of (detail.tracks || [])) {
        if (!knownIds.has(t.id)) {
            state.library.tracks.push(t);
            knownIds.add(t.id);
        }
    }
    await playLocal(trackId, trackIds);
}

function artworkPlaceholderUrl() {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.artworkPlaceholderUrl();
}

function albumArtFallbackSvg() {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    if (mod && typeof mod.albumArtFallbackSvg === 'function') return mod.albumArtFallbackSvg();
    return artworkPlaceholderUrl();
}

async function playLibraryFolder(folder) {
    const tracks = getTracksInFolder(folder);
    if (tracks.length === 0) {
        showToast('Folder has no playable tracks', 'error');
        return;
    }
    await playLocal(tracks[0].id, tracks.map(track => track.id));
}

async function deleteLibraryFolder(folder) {
    const tracks = getTracksInFolder(folder);
    if (tracks.length === 0) {
        showToast('Folder is already empty', 'error');
        return;
    }
    const folderName = folder.split('/').filter(Boolean).pop() || folder;
    if (!confirm(`Delete "${folderName}"? This will remove ${tracks.length} track${tracks.length === 1 ? '' : 's'} from the library.`)) {
        return;
    }
    try {
        const resp = await fetch('/api/library/folders/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ folder }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Delete failed');
        }
        const parentFolder = folder.split('/').filter(Boolean).slice(0, -1).join('/');
        state.library.currentFolder = parentFolder;
        state.library.selectedTrackIds = state.library.selectedTrackIds.filter(id => !tracks.some(track => track.id === id));
        state.library.albumsLoaded = false;
        state.library.albumDetail = null;
        showToast(`Deleted ${tracks.length} track${tracks.length === 1 ? '' : 's'}`, 'success');
        await fetchTracks();
    } catch (e) {
        showToast(`Failed to delete folder: ${e.message}`, 'error');
    }
}
function toggleLibraryFolderSelection(folder) {
    const folderTrackIds = getTracksInFolder(folder).map(track => track.id);
    if (folderTrackIds.length === 0) {
        showToast('Folder has no tracks to select', 'error');
        return;
    }
    const selectedIds = new Set(state.library.selectedTrackIds);
    const allSelected = folderTrackIds.every(id => selectedIds.has(id));
    folderTrackIds.forEach(id => {
        if (allSelected) {
            selectedIds.delete(id);
        } else {
            selectedIds.add(id);
        }
    });
    state.library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    showToast(allSelected ? 'Folder selection cleared' : `Selected ${folderTrackIds.length} folder tracks`, 'info');
}
function toggleTrackSelected(trackId) {
    const selectedIds = new Set(state.library.selectedTrackIds);
    if (selectedIds.has(trackId)) {
        selectedIds.delete(trackId);
    } else {
        selectedIds.add(trackId);
    }
    state.library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}
function clearTrackSelection() {
    state.library.selectedTrackIds = [];
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}
// Conscious end of the playlist-build selection: clears the cross-album
// selectedTrackIds (+/check marks), the name field, and the save row.
// Used by the Cancel button; Save calls clearTrackSelection on success.
function cancelPlaylistSelection() {
    clearTrackSelection();
    if (elements.playlistName) elements.playlistName.value = '';
    updatePlaylistSaveRowVisibility();
}
function selectAllVisibleTracks() {
    const selectedIds = new Set(state.library.selectedTrackIds);
    getFilteredTracks().forEach(track => selectedIds.add(track.id));
    state.library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}
function clearVisibleTrackSelection() {
    const visibleIds = new Set(getFilteredTracks().map(track => track.id));
    state.library.selectedTrackIds = state.library.selectedTrackIds.filter(id => !visibleIds.has(id));
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}
function toggleVisibleTrackSelection() {
    const filteredTracks = getFilteredTracks();
    const selectedIds = new Set(state.library.selectedTrackIds);
    const visibleIds = filteredTracks.map(track => track.id);
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));
    if (allVisibleSelected) {
        clearVisibleTrackSelection();
    } else {
        selectAllVisibleTracks();
    }
}
function setLibrarySearchQuery(value) {
    state.library.searchQuery = value || '';
    updateLibrarySearchControls();
    if (state.library.viewMode === 'albums' && state.library.albumDetail) {
        renderAlbumDetailTracks();
    } else if (state.library.viewMode === 'albums' && state.library.playlistDetail) {
        renderPlaylistDetailTracks();
    } else if (state.library.viewMode === 'albums') {
        renderAlbums();
    } else {
        renderTracks();
    }
}
function updateLibrarySearchControls() {
    if (elements.librarySearchClear) {
        elements.librarySearchClear.disabled = !(state.library.searchQuery || '').trim();
    }
}
function updateLibrarySearchPlaceholder() {
    if (!elements.librarySearchInput) return;
    const compact = window.matchMedia && window.matchMedia("(max-width: 600px)").matches;
    const isAlbums = state.library.viewMode === "albums";
    const fullText = isAlbums
        ? (elements.librarySearchInput.dataset.placeholderAlbumsFull || "Search album, artist, genre, year…")
        : (elements.librarySearchInput.dataset.placeholderFull || "Search folder, artist, track…");
    const compactText = isAlbums
        ? (elements.librarySearchInput.dataset.placeholderAlbumsCompact || "Search albums…")
        : (elements.librarySearchInput.dataset.placeholderCompact || "Search…");
    elements.librarySearchInput.placeholder = compact ? compactText : fullText;
    elements.librarySearchInput.setAttribute('aria-label', fullText);
}
function clearLibrarySearch() {
    if (!elements.librarySearchInput && !state.library.searchQuery) return;
    if (elements.librarySearchInput) {
        elements.librarySearchInput.value = '';
        elements.librarySearchInput.focus();
    }
    setLibrarySearchQuery('');
}
function syncRenderedTrackSelection() {
    const selectedIds = new Set(state.library.selectedTrackIds);
    [elements.tracksList, elements.albumDetailTracks, elements.playlistDetailTracks].forEach(container => {
        if (!container) return;
        container.querySelectorAll('.track-item').forEach(item => {
            item.classList.toggle('selected', selectedIds.has(item.dataset.trackId));
        });
        container.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
            const active = selectedIds.has(btn.dataset.trackAdd);
            btn.classList.toggle('is-active', active);
            btn.textContent = active ? '✓' : '+';
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.setAttribute('aria-label', active ? 'Remove track from selection' : 'Add track to selection');
            btn.title = active ? 'Remove from selection' : 'Add to selection';
        });
    });
}
// The save row is a single shared node. Its home is below the detail cards
// (right before #library-info), but while an album detail is open it docks
// inside the detail between header and tracks — like the TIDAL save row —
// instead of sitting misplaced under the track list. Placement is
// idempotent, so typing in the name field never moves the focused input.
function dockPlaylistSaveRow() {
    const row = elements.playlistSaveRow;
    if (!row || !elements.albumDetail || !elements.albumDetailTracks || !elements.libraryInfo) return;
    if (!elements.albumDetail.classList.contains('hidden')) {
        if (row.parentElement !== elements.albumDetail || row.nextSibling !== elements.albumDetailTracks) {
            elements.albumDetail.insertBefore(row, elements.albumDetailTracks);
        }
        return;
    }
    const home = elements.libraryInfo;
    if (row.parentElement !== home.parentElement || row.nextSibling !== home) {
        home.parentElement.insertBefore(row, home);
    }
}
function updatePlaylistSaveRowVisibility() {
    if (!elements.playlistSaveRow) return;
    const count = state.library.selectedTrackIds.length;
    // The playlist-build selection is independent of the view mode: as soon
    // as at least one track is consciously added via the + action (never via
    // playback), the save-playlist row is reachable. Playback never touches
    // selectedTrackIds, so this trigger stays exclusive to the + selection.
    // The row also stays open while a playlist detail is edited so Delete
    // remains reachable without a selection.
    const hasPlaylistSelection = count >= 1;
    const isEditingPlaylist = !!state.library.playlistDetail;
    const showRow = hasPlaylistSelection || isEditingPlaylist;
    elements.playlistSaveRow.classList.toggle('hidden', !showRow);
    if (elements.playlistSaveControls) {
        elements.playlistSaveControls.classList.toggle('hidden', !showRow);
    }
    // Delete belongs to the edit flow only: hidden while merely creating.
    if (elements.deletePlaylistBtn) {
        elements.deletePlaylistBtn.classList.toggle('hidden', !isEditingPlaylist);
    }
    dockPlaylistSaveRow();
}
function updateLibrarySelectionUI() {
    const allTracks = state.library.tracks || [];
    const filteredTracks = getFilteredTracks();
    const filteredPlaylists = getFilteredPlaylists();
    const selectedIds = new Set(state.library.selectedTrackIds);
    const visibleIds = filteredTracks.map(track => track.id);
    const selectedVisibleCount = visibleIds.filter(id => selectedIds.has(id)).length;
    const totalSelectedCount = selectedIds.size;
    const hasSearch = !!(state.library.searchQuery || '').trim();
    const isTracksMode = state.library.viewMode === 'tracks';

    // Select all: visible in tracks and folders mode, hidden in albums
    if (elements.selectAllTracksBtn) {
        const isAlbumsMode = state.library.viewMode === 'albums';
        const allVisibleSelected = filteredTracks.length > 0 && selectedVisibleCount === filteredTracks.length;
        elements.selectAllTracksBtn.classList.toggle('hidden', isAlbumsMode);
        elements.selectAllTracksBtn.disabled = isAlbumsMode || filteredTracks.length === 0;
        if (allVisibleSelected) {
            elements.selectAllTracksBtn.textContent = hasSearch ? 'Clear visible' : 'Clear selection';
        } else {
            elements.selectAllTracksBtn.textContent = hasSearch ? 'Select visible' : 'Select all';
        }
    }

    // Download: visible in all modes when tracks selected
    if (elements.downloadSelectedTracksBtn) {
        elements.downloadSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
        elements.downloadSelectedTracksBtn.disabled = totalSelectedCount === 0 || state.library.selectionDownloadPending;
        elements.downloadSelectedTracksBtn.classList.toggle('is-busy', state.library.selectionDownloadPending);
    }

    // Delete: visible in all modes when tracks selected
    if (elements.deleteSelectedTracksBtn) {
        elements.deleteSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
    }

    if (elements.libraryInfo) {
        const playlistText = filteredPlaylists.length > 0
            ? `, ${filteredPlaylists.length} playlist${filteredPlaylists.length === 1 ? '' : 's'}`
            : '';
        const baseText = hasSearch
            ? `${filteredTracks.length} of ${allTracks.length} tracks${playlistText}`
            : `${allTracks.length} tracks`;
        if (totalSelectedCount === 0) {
            elements.libraryInfo.textContent = baseText;
        } else if (hasSearch && totalSelectedCount !== selectedVisibleCount) {
            elements.libraryInfo.textContent = `${baseText}, ${selectedVisibleCount} visible selected (${totalSelectedCount} total)`;
        } else {
            elements.libraryInfo.textContent = `${baseText}, ${totalSelectedCount} selected`;
        }
    }
    updatePlaylistSaveRowVisibility();
}
async function refreshLibrary() {
    if (state.library.scanning) return;
    state.library.scanning = true;
    state.library.scanStatus = { scanning: true, tracks_found: 0, files_seen: 0 };
    state.library.albumsLoaded = false;
    state.library.albums = [];
    renderTracks();
    try {
        const resp = await fetch('/api/library/refresh', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok || data.status === 'error') throw new Error(data.message || 'Refresh failed');
        state.library.scanStatus = data;
        setTimeout(fetchLibraryStatus, LIBRARY_SCAN_POLL_INTERVAL_MS);
    } catch (e) {
        showToast('Failed to refresh library', 'error');
        state.library.scanning = false;
        renderTracks();
    }
}
function uploadTrackFile() {
    const file = elements.uploadTrackFile.files[0];
    if (!file) {
        showToast('Please choose an audio file, playlist, or ZIP', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('file', file);
    const filename = file.name;
    state.upload = { filename, status_text: `Uploading ${filename}… 0%`, progress_percent: 0, status: 'uploading' };
    updateDownloadUI();
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/library/upload', true);
    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const pct = (e.loaded / e.total * 100).toFixed(1);
            state.upload.progress_percent = parseFloat(pct);
            state.upload.status_text = `Uploading ${filename}… ${pct}%`;
            updateDownloadUI();
        }
    });
    xhr.addEventListener('load', () => {
        if (xhr.status === 200) {
            const data = JSON.parse(xhr.responseText);
            const successMessage = data.message || (data.kind === 'zip'
                ? `Imported ${data.imported_track_count || 0} track${(data.imported_track_count || 0) === 1 ? '' : 's'} from ${data.filename}`
                : `Uploaded ${data.filename}`);
            resetUploadAreaSelection('upload-track-file');
            state.upload = { filename: data.filename, status_text: successMessage, progress_percent: 100, status: 'complete' };
            updateDownloadUI();
            showToast(successMessage, 'success');
            refreshLibrary();
            fetchPlaylists();
            setTimeout(() => {
                state.upload = null;
                updateDownloadUI();
            }, 2000);
        } else {
            let msg = 'Upload failed';
            try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
            resetUploadAreaSelection('upload-track-file');
            state.upload = { filename, status_text: msg, progress_percent: 0, status: 'error' };
            updateDownloadUI();
            showToast(msg, 'error');
        }
    });
    xhr.addEventListener('error', () => {
        resetUploadAreaSelection('upload-track-file');
        state.upload = { filename, status_text: 'Upload failed', progress_percent: 0, status: 'error' };
        updateDownloadUI();
        showToast('Upload failed', 'error');
    });
    xhr.send(formData);
}
async function savePlaylist() {
    const trackIds = getSelectedPlayableTrackIds();
    const name = (elements.playlistName?.value || '').trim();
    if (!name) {
        showToast('Please enter a playlist name', 'error');
        return;
    }
    if (trackIds.length < 1) {
        showToast('Select at least 1 track', 'error');
        return;
    }        elements.savePlaylistBtn.disabled = true;
    try {
        const resp = await fetch('/api/playlists', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save playlist');
        // Success: finalize the action. Clear the name field, the selection
        // (selectedTrackIds + rendered +/check marks), and the save row.
        if (elements.playlistName) elements.playlistName.value = '';
        clearTrackSelection();
        await fetchPlaylists();
        showToast(`Saved: ${data.playlist?.name || name}`, 'success');
    } catch (e) {
        showToast(e.message || 'Failed to save playlist', 'error');
    } finally {
        elements.savePlaylistBtn.disabled = false;
        updatePlaylistSaveRowVisibility();
    }
}
async function loadPlaylistById(playlistId, options = {}) {
    const { autoplay = false } = options;
    const playlist = state.playlists.find(item => item.id === playlistId);
    if (!playlist) {
        showToast('Playlist not found', 'error');
        return;
    }
    const validTrackIds = getTrackIdsInLibraryOrder(playlist.track_ids);
    if (validTrackIds.length === 0) {
        showToast(`Playlist "${playlist.name}" has no playable tracks`, 'error');
        return;
    }
    const missingCount = playlist.track_ids.length - validTrackIds.length;
    if (autoplay) {
        if (missingCount > 0) {
            showToast(`Starting ${validTrackIds.length}/${playlist.track_ids.length} tracks from ${playlist.name}`, 'info');
        }
        await playLocal(validTrackIds[0], validTrackIds);
        return;
    }
    showToast(missingCount > 0
        ? `Loaded ${validTrackIds.length}/${playlist.track_ids.length} tracks from ${playlist.name}`
        : `Loaded: ${playlist.name}`, 'info');
}
async function downloadPlaylistById(playlistId) {
    const playlist = state.playlists.find(item => item.id === playlistId);
    if (!playlist) return;
    try {
        const resp = await fetch(`/api/playlists/${encodeURIComponent(playlistId)}/export`);
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Playlist export failed');
        }
        const blob = await resp.blob();
        const filename = getDownloadFilenameFromResponse(resp, `${playlist.name || 'playlist'}.m3u8`);
        triggerBlobDownload(blob, filename);
        showToast(`Downloading ${filename}`, 'success');
    } catch (e) {
        showToast(e.message || 'Playlist export failed', 'error');
    }
}
async function deletePlaylistById(playlistId) {
    const playlist = state.playlists.find(item => item.id === playlistId);
    if (!playlist) return;
    try {
        const resp = await fetch(`/api/playlists/${encodeURIComponent(playlistId)}`, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Delete failed');
        await Promise.all([fetchPlaylists(), fetchTracks()]);
        // renderTracks is called by both fetchPlaylists() and fetchTracks()
        showToast(`Deleted: ${playlist.name}`, 'success');
    } catch (e) {
        showToast(e.message || 'Failed to delete playlist', 'error');
    }
}
function getDownloadFilenameFromResponse(resp, fallbackName = 'download') {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.getDownloadFilenameFromResponse(resp, fallbackName);
}
function triggerBlobDownload(blob, filename) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.triggerBlobDownload(blob, filename);
}
async function downloadSelectedTracks() {
    const trackIds = getSelectedDownloadTrackIds();
    if (trackIds.length === 0) {
        showToast('Please select tracks first', 'error');
        return;
    }
    state.library.selectionDownloadPending = true;
    updateLibrarySelectionUI();
    try {
        const resp = await fetch('/api/tracks/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: trackIds }),
        });
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Download failed');
        }
        const blob = await resp.blob();
        const filename = getDownloadFilenameFromResponse(resp, trackIds.length === 1 ? 'track' : 'fxroute-library-selection.zip');
        triggerBlobDownload(blob, filename);
        showToast(trackIds.length === 1 ? `Downloading ${filename}` : `Downloading ${trackIds.length} tracks`, 'success');
    } catch (e) {
        showToast(e.message || 'Download failed', 'error');
    } finally {
        state.library.selectionDownloadPending = false;
        updateLibrarySelectionUI();
    }
}
async function deleteSelectedTracks() {
    const trackIds = [...state.library.selectedTrackIds];
    if (trackIds.length === 0) {
        showToast('Please select tracks first', 'error');
        return;
    }
    const label = trackIds.length === 1 ? 'this track' : `${trackIds.length} tracks`;
    if (!confirm(`Delete ${label} from the library?`)) return;
    elements.deleteSelectedTracksBtn.disabled = true;
    try {
        const resp = await fetch('/api/tracks/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Delete failed');
        const deletedCount = (data.deleted || []).length;
        state.library.selectedTrackIds = [];
        await fetchTracks();
        showToast(`Deleted ${deletedCount} track${deletedCount === 1 ? '' : 's'}`, 'success');
        if ((data.errors || []).length > 0) {
            showToast(`Some tracks could not be deleted`, 'error');
        }
    } catch (e) {
        showToast(e.message || 'Delete failed', 'error');
    } finally {
        elements.deleteSelectedTracksBtn.disabled = false;
        updateLibrarySelectionUI();
    }
}
// Playback actions
async function playRadio(stationId) {
    pendingFooterSingleTrackStart = null;
    const station = state.stations.find(s => s.id === stationId);
    if (!station) {
        showToast('Station not found', 'error');
        return;
    }
    // Cancel any in-flight action — new play takes priority
    if (playbackActionInFlight) {
        pendingPlaybackRequestId++;
        playbackActionInFlight = false;
    }
    const requestId = ++pendingPlaybackRequestId;
    playbackActionInFlight = true;
    armLocalFooterHold();
    armFooterContentFreeze();
    const optimisticRadioTrack = {
        id: `radio_${station.id}`,
        title: station.title,
        artist: station.artist || 'SomaFM',
        source: 'radio',
        url: station.stream_url,
    };
    rememberLastRadioTrack(optimisticRadioTrack);
    state.playback.current_track = optimisticRadioTrack;
    pendingOptimisticTrack = { requestId, track: optimisticRadioTrack };
    state.playback.live_title = null;
    state.playback.radio_metadata = null;
    state.playback.playing = true;
    state.playback.paused = false;
    _spotifyTakeoverUntil = 0;
    if (window.__spotifyLastData && window.__spotifyLastData.status === 'Playing') {
        window.__spotifyLastData = { ...window.__spotifyLastData, status: 'Paused' };
    }
    window.__footerSource = 'local';
    _spotifyPollGeneration++;
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: 'radio', track_id: station.id, url: station.stream_url }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Play command failed'));
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        clearPendingOptimisticTrack(requestId);
        applyNativePlayResponse(data);
        void fetchMetadata();
        triggerSamplerateBurstPolling();
        const playedTrack = data?.playback?.current_track || {
            id: `radio_${station.id}`,
            title: station.title,
            artist: station.artist || 'Radio',
            source: 'radio',
            artwork_available: !!(station.image || station.image_url || station.custom_image_url),
            artwork_url: station.image || station.image_url || station.custom_image_url || '',
            artwork_source: (station.image || station.image_url || station.custom_image_url) ? 'radio' : 'none',
        };
        maybeShowNativeTrackCue(playedTrack, 'Now playing');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        clearPendingOptimisticTrack(requestId);
        state.playback.playing = false;
        state.playback.paused = false;
        updatePlaybackUI();
        showToast('Failed to start playback', 'error');
    }
}
async function playLocal(trackId, queueTrackIds = null) {
    const track = state.library.tracks.find(t => t.id === trackId);
    if (!track) {
        showToast('Track not found', 'error');
        return;
    }
    // Cancel any in-flight action — new play takes priority
    if (playbackActionInFlight) {
        pendingPlaybackRequestId++;
        playbackActionInFlight = false;
    }
    const shouldUseQueue = Array.isArray(queueTrackIds) && queueTrackIds.length > 1 && queueTrackIds.includes(trackId);
    const requestId = ++pendingPlaybackRequestId;
    pendingFooterSingleTrackStart = shouldUseQueue ? null : {
        requestId,
        trackId: track.id,
        expiresAt: Date.now() + FOOTER_SINGLE_TRACK_START_LOCK_MS,
    };
    playbackActionInFlight = true;
    armLocalFooterHold();
    armFooterContentFreeze();
    libraryModeSyncArmed = true;
    state.playback.current_track = track;
    pendingOptimisticTrack = { requestId, track };
    state.playback.live_title = null;
    state.playback.playing = true;
    state.playback.paused = false;
    state.playback.queue = shouldUseQueue
        ? {
            active: true,
            index: Math.max(0, queueTrackIds.indexOf(track.id)),
            count: queueTrackIds.length,
            mode: state.playback.queue?.mode || 'app_replace',
            tracks: queueTrackIds
                .map(id => state.library.tracks.find(item => item.id === id))
                .filter(Boolean),
            loop: !!state.library.loop,
            shuffle: !!state.library.shuffle,
        }
        : buildOptimisticSingleTrackQueue(track);
    _spotifyTakeoverUntil = 0;
    if (window.__spotifyLastData && window.__spotifyLastData.status === 'Playing') {
        window.__spotifyLastData = { ...window.__spotifyLastData, status: 'Paused' };
    }
    window.__footerSource = 'local';
    _spotifyPollGeneration++;
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: 'local',
                track_id: track.id,
                queue_track_ids: shouldUseQueue ? queueTrackIds : undefined,
                shuffle: !!state.library.shuffle,
                loop: !!state.library.loop,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Play command failed'));
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        clearPendingOptimisticTrack(requestId);
        applyNativePlayResponse(data);
        if (data.playback) {
            // Library state must react to the merged playback context, so it
            // runs after the shared commit; the helper itself is provider-
            // neutral and does not know library specifics.
            syncLibraryStateFromPlaybackContext(true);
        }
        triggerSamplerateBurstPolling();
        const playedTrack = data?.playback?.current_track || track;
        const queueCount = (((data || {}).playback || {}).queue || {}).count || 0;
        maybeShowNativeTrackCue(playedTrack, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        clearPendingOptimisticTrack(requestId);
        clearPendingFooterSingleTrackStart(requestId);
        libraryModeSyncArmed = false;
        state.playback.playing = false;
        state.playback.paused = false;
        updatePlaybackUI();
        showToast('Failed to start playback', 'error');
    }
}
// Download
function setupDownloadActions() {
    if (elements.downloadUrlDropArea) {
        setupDownloadUrlDropArea();
    }
    if (elements.downloadUrl) {
        elements.downloadUrl.addEventListener('input', handleDownloadUrlInput);
        elements.downloadUrl.addEventListener('paste', () => {
            requestAnimationFrame(() => maybeStartDownloadFromInput('Pasted URL'));
        });
        elements.downloadUrl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                maybeStartDownloadFromInput('Entered URL');
            }
        });
    }
    if (elements.cancelDownloadBtn) {
        elements.cancelDownloadBtn.addEventListener('click', cancelDownload);
    }
}

function setDownloadUrlValue(url, sourceLabel = '') {
    const cleaned = (url || '').trim();
    if (!cleaned || !elements.downloadUrl) return;
    elements.downloadUrl.value = cleaned;
    if (elements.downloadUrlHint) {
        elements.downloadUrlHint.textContent = sourceLabel ? `${sourceLabel}: ${cleaned}` : cleaned;
    }
}

function handleDownloadUrlInput() {
    const value = (elements.downloadUrl?.value || '').trim();
    if (!elements.downloadUrlHint) return;
    elements.downloadUrlHint.textContent = value
        ? `URL: ${value}`
        : 'YouTube or direct media link.';
}

async function maybeStartDownloadFromInput(sourceLabel = '') {
    const value = (elements.downloadUrl?.value || '').trim();
    const match = value.match(/https?:\/\/\S+/i);
    if (!match) {
        showToast('No valid URL found', 'error');
        return;
    }
    setDownloadUrlValue(match[0], sourceLabel || 'URL');
    await startDownload(match[0]);
}

function extractDroppedUrl(dataTransfer) {
    if (!dataTransfer) return '';
    const uriList = dataTransfer.getData('text/uri-list') || '';
    const plain = dataTransfer.getData('text/plain') || '';
    const raw = uriList || plain;
    const match = raw.match(/https?:\/\/\S+/i);
    if (!match) return '';
    return match[0].trim().replace(/[),.;:"'!\]]+$/, '');
}

function setupDownloadUrlDropArea() {
    const area = elements.downloadUrlDropArea;
    if (!area) return;
    const activate = () => elements.downloadUrl?.focus();
    area.addEventListener('click', activate);
    area.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            activate();
        }
    });
    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('drag-over');
    });
    area.addEventListener('dragleave', () => area.classList.remove('drag-over'));
    area.addEventListener('drop', async (e) => {
        e.preventDefault();
        area.classList.remove('drag-over');
        const url = extractDroppedUrl(e.dataTransfer);
        if (!url) {
            showToast('No URL found in dropped content', 'error');
            return;
        }
        setDownloadUrlValue(url, 'Dropped URL');
        await startDownload(url);
    });
}

// Upload area: drag-over, filename display, auto-trigger
function resetUploadAreaSelection(fileInputId) {
    const input = document.getElementById(fileInputId);
    if (!input) return;
    const area = input.closest('.upload-area');
    const filenameEl = area?.querySelector('.upload-area-filename');
    const defaultFilenameText = filenameEl?.dataset.defaultText || filenameEl?.textContent || '';
    input.value = '';
    if (filenameEl) filenameEl.textContent = defaultFilenameText;
}

function setupUploadArea(areaId, fileInputId, onFile) {
    const area = document.getElementById(areaId);
    const input = document.getElementById(fileInputId);
    if (!area || !input) return;
    const filenameEl = area.querySelector('.upload-area-filename');
    const defaultFilenameText = filenameEl?.textContent || '';
    if (filenameEl && !filenameEl.dataset.defaultText) filenameEl.dataset.defaultText = defaultFilenameText;

    const describeFileSelection = (files) => {
        const count = files?.length || 0;
        if (!count) return defaultFilenameText;
        const firstName = files[0]?.name || 'file';
        return count > 1 ? `${firstName} (+${count - 1} more)` : firstName;
    };

    area.addEventListener('dragover', (e) => {
        e.preventDefault();
        area.classList.add('drag-over');
    });
    area.addEventListener('dragleave', () => area.classList.remove('drag-over'));
    area.addEventListener('drop', (e) => {
        e.preventDefault();
        area.classList.remove('drag-over');
        const files = Array.from(e.dataTransfer?.files || []);
        const file = files[0] || null;
        if (!file) return;
        const dt = new DataTransfer();
        dt.items.add(file);
        input.files = dt.files;
        if (filenameEl) filenameEl.textContent = describeFileSelection(files);
        if (files.length > 1) showToast(`Using first file only: ${file.name}`, 'warning');
        onFile(file);
    });
    input.addEventListener('change', () => {
        const files = Array.from(input.files || []);
        const file = files[0] || null;
        if (filenameEl) filenameEl.textContent = describeFileSelection(files);
        if (file) onFile(file);
    });
}


/* Collapsed-card preview: compact read-only band list from the draft. */


async function startDownload(urlOverride = null) {
    const url = (urlOverride || elements.downloadUrl.value || '').trim();
    if (!url) {
        showToast('Please enter a URL', 'error');
        return;
    }
    if (state.download && ['starting', 'downloading'].includes(state.download.status)) {
        showToast('Download already in progress', 'error');
        return;
    }
    try {
        const resp = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(data.detail || 'Download failed');
        }
        state.download = {
            url,
            status: 'starting',
            progress_percent: 0,
            filename: data.filename || null,
            error: null,
            status_text: 'Preparing download…',
        };
        lastDownloadStatus = 'starting';
        updateDownloadUI();
        startDownloadStatusPolling();
        elements.downloadUrl.value = '';
        if (elements.downloadUrlHint) {
            elements.downloadUrlHint.textContent = 'YouTube or direct media link.';
        }
        showToast('Download started', 'info');
    } catch (e) {
        showToast(e.message, 'error');
    }
}
async function cancelDownload() {
    try {
        const resp = await fetch('/api/download/cancel', { method: 'POST' });
        if (!resp.ok) throw new Error('Cancel failed');
    } catch (e) {
        showToast('Failed to cancel download', 'error');
    }
}
async function fetchDownloadStatus() {
    if (isPageHidden()) return;
    try {
        const resp = await fetch('/api/download/status');
        if (!resp.ok) throw new Error('Failed to fetch download status');
        const data = await resp.json();
        if (data.status === 'idle') {
            if (state.download && ['starting', 'downloading', 'complete', 'error', 'cancelled'].includes(state.download.status)) {
                stopDownloadStatusPolling();
            }
            if (!state.download || ['starting', 'downloading'].includes(state.download.status)) {
                state.download = null;
                updateDownloadUI();
            }
            lastDownloadStatus = 'idle';
            return;
        }
        state.download = data;
        updateDownloadUI();
        handleDownloadStatusTransition(data);
        if (['starting', 'downloading'].includes(data.status)) {
            startDownloadStatusPolling();
        } else {
            stopDownloadStatusPolling();
        }
    } catch (e) {
        console.debug('Download status unavailable', e);
    }
}
function startDownloadStatusPolling() {
    if (downloadStatusPollTimer !== null) return;
    downloadStatusPollTimer = setInterval(fetchDownloadStatus, DOWNLOAD_STATUS_POLL_INTERVAL_MS);
}
function stopDownloadStatusPolling() {
    if (downloadStatusPollTimer === null) return;
    clearInterval(downloadStatusPollTimer);
    downloadStatusPollTimer = null;
}
function handleDownloadStatusTransition(dl) {
    const previous = lastDownloadStatus;
    lastDownloadStatus = dl.status;
    if (dl.status === 'complete' && previous !== 'complete') {
        showToast(`Download complete: ${dl.filename || 'file saved'}`, 'success');
        refreshLibrary();
    } else if (dl.status === 'error' && previous !== 'error') {
        showToast(`Download error: ${dl.error || 'Unknown error'}`, 'error');
    } else if (dl.status === 'cancelled' && previous !== 'cancelled') {
        showToast('Download cancelled', 'info');
    }
}
function updateDownloadUI() {
    const dl = state.upload || state.download;
    if (!elements.downloadStatus || !elements.cancelDownloadBtn) return;
    if (!dl) {
        elements.downloadStatus.innerHTML = '';
        elements.downloadStatus.classList.add('hidden');
        elements.cancelDownloadBtn.classList.add('hidden');
        return;
    }
    let html = '';
    if (dl.status === 'uploading' || dl.status === 'starting' || dl.status === 'downloading') {
        const isUpload = dl.status === 'uploading';
        const progress = Number(dl.progress_percent || 0).toFixed(1);
        const label = isUpload ? 'Uploading' : 'Downloading';
        html = `
            <div class="download-progress">
                <div><strong>${escapeHtml(dl.filename || label)}</strong></div>
                <div style="color: var(--text-secondary); margin-bottom: 0.35rem;">${escapeHtml(dl.status_text || (isUpload ? `${label}…` : 'Preparing download…'))}</div>
                ${dl.progress_percent >= 0 ? `
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${progress}%"></div>
                </div>
                <div style="text-align: center; color: var(--text-secondary);">${progress}%</div>` : ''}
            </div>
        `;
        if (!isUpload) {
            elements.cancelDownloadBtn.classList.remove('hidden');
        }
    } else if (dl.status === 'complete') {
        html = `<div style="color: var(--success);">${escapeHtml(dl.status_text || (state.upload ? 'Upload complete' : 'Download complete'))}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
    } else if (dl.status === 'error') {
        html = `<div style="color: var(--danger);"><strong>${state.upload ? 'Upload failed' : 'Download failed'}</strong><br>${escapeHtml(dl.error || dl.status_text || 'Unknown error')}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
    } else if (dl.status === 'cancelled') {
        html = `<div style="color: var(--text-secondary);">${state.upload ? 'Upload cancelled' : 'Download cancelled'}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
    }
    elements.downloadStatus.innerHTML = html;
    elements.downloadStatus.classList.toggle('hidden', !html.trim());
}


function getVisibleMeasurementEntries() {
    const currentId = state.measurement.currentMeasurement?.id;
    return (state.measurement.measurements || []).filter(measurement => measurement.id !== currentId && state.measurement.visibilityById?.[measurement.id]);
}

function getCurrentMeasurementEntry() {
    return state.measurement.currentMeasurement ? MeasurementUI.normalizeMeasurementEntry(state.measurement.currentMeasurement, 0) : null;
}

function getCurrentMeasurementEntries() {
    const autoSubMeasurements = Array.isArray(state.measurement.autoSubMeasurements)
        ? state.measurement.autoSubMeasurements.map((measurement, index) => MeasurementUI.normalizeMeasurementEntry(measurement, index))
        : [];
    const repeatMeasurements = Array.isArray(state.measurement.pendingRepeatMeasurements)
        ? state.measurement.pendingRepeatMeasurements.map((measurement, index) => MeasurementUI.normalizeMeasurementEntry(measurement, index))
        : [];
    if (autoSubMeasurements.length) return autoSubMeasurements;
    return repeatMeasurements.length ? repeatMeasurements : [getCurrentMeasurementEntry()].filter(Boolean);
}

function measurementReviewVisible(measurementId) {
    return !!state.measurement.reviewVisibilityById?.[measurementId];
}

function getMeasurementDisplayTraces(measurement = {}) {
    const traces = Array.isArray(measurement.traces) ? measurement.traces : [];
    const reviewTraces = Array.isArray(measurement.review_traces) ? measurement.review_traces : [];
    if (reviewTraces.length && measurementReviewVisible(measurement.id)) return reviewTraces;
    return traces;
}

function getAutoSubDisplayReferenceEntries() {
    const entriesById = new Map();
    [...(state.measurement.measurements || []), ...getCurrentMeasurementEntries()]
        .filter(measurement => window.FXRouteAutoSubTarget?.isAutoSubMeasurement(measurement))
        .forEach((measurement, index) => {
            entriesById.set(String(measurement.id || `current-${index}`), measurement);
        });
    return [...entriesById.values()];
}


function getVisibleMeasurementColorById() {
    const colorById = {};
    let colorIndex = 0;
    getVisibleMeasurementEntries().forEach((measurement) => {
        if (!getMeasurementDisplayTraces(measurement).length) return;
        colorById[measurement.id] = measurementComparePalette[colorIndex % measurementComparePalette.length] || '#60a5fa';
        colorIndex += 1;
    });
    return colorById;
}


function getMeasurementActiveEditor() {
    const editor = String(state.measurement?.activeEditor || 'none');
    return ['none', 'peq', 'houseCurve'].includes(editor) ? editor : 'none';
}

function getMeasurementRestorableTargetCurve() {
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    const targetCurve = String(conv.targetCurve || '');
    return window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverCurveOptions().some((curve) => curve.key === targetCurve)
        ? targetCurve
        : 'neutral';
}

function setMeasurementActiveEditor(editor = 'none') {
    const nextEditor = ['none', 'peq', 'houseCurve'].includes(editor) ? editor : 'none';
    state.measurement.activeEditor = nextEditor;
    const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
    custom.open = nextEditor === 'houseCurve';
    custom.displayTarget = nextEditor === 'houseCurve' ? 'editing-custom-house-curve' : 'actual';
    if (nextEditor !== 'peq') {
        const peq = window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState();
        peq.dragFilterId = null;
    }
    window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState().dragPointId = null;
    if (state.measurement.convolverAssistant && typeof state.measurement.convolverAssistant === 'object') {
        state.measurement.convolverAssistant.dragMode = null;
    }
    return nextEditor;
}

function setMeasurementAssistMode(mode) {
    const nextMode = mode === 'convolver' ? 'convolver' : 'peq';
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    // The custom editor is an overlay on the actual target selection. Never
    // let its sentinel or a deleted house curve become the active target when
    // returning to PEQ or Convolver.
    conv.targetCurve = getMeasurementRestorableTargetCurve();
    state.measurement.assistMode = nextMode;
    setMeasurementActiveEditor(nextMode === 'peq' ? 'peq' : 'none');
    if (nextMode === 'peq') window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState().enabled = true;
    renderMeasurementPanel();
    MeasurementGraph.scheduleMeasurementGraphRender();
}


function getAutoSubTargetCurveSnapshot() {
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    const key = String(conv.targetCurve || '');
    const curve = window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverCurveOptions().find((option) => option.key === key);
    if (!curve) return null;
    return {
        key,
        label: String(curve.label || curve.shortLabel || key),
        provenance: key.startsWith('house:') ? 'uploaded' : 'built_in',
        points: (curve.points || []).map((point) => [Number(point[0]), Number(point[1])]),
    };
}


function getMeasurementHouseCurvePreviewPoints() {
    return window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState().points
        .map((point) => [Number(point.freqHz), Number(point.gainDb)])
        .filter(([frequency, gain]) => Number.isFinite(frequency) && frequency > 0 && Number.isFinite(gain))
        .sort((left, right) => left[0] - right[0]);
}

function getMeasurementTargetCurvePreview() {
    if (getMeasurementActiveEditor() === 'houseCurve') {
        return { label: 'Editing Custom House Curve…', shortLabel: 'Editing Custom House Curve…', points: getMeasurementHouseCurvePreviewPoints() };
    }
    return window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverCurve(window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState().targetCurve);
}


function resetMeasurementGraph() {
    if (getMeasurementActiveEditor() === 'houseCurve') {
        window.FXRouteMeasurementCalibration.resetCustomHouseCurveDraft();
        renderMeasurementPanel();
        MeasurementGraph.scheduleMeasurementGraphRender();
        return;
    }
    setMeasurementActiveEditor('none');
    const peq = window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState();
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    state.measurement.currentMeasurement = null;
    state.measurement.pendingRepeatMeasurements = [];
    state.measurement.autoSubMeasurements = [];
    state.measurement.currentMeasurementSaved = false;
    state.measurement.currentMeasurementName = '';
    peq.enabled = false;
    peq.filters = [];
    peq.activeFilterId = null;
    peq.dragFilterId = null;
    Object.assign(conv, window.FXRouteMeasurementConvolverEditor.getDefaultMeasurementConvolverState());
    renderMeasurementPanel();
    MeasurementGraph.scheduleMeasurementGraphRender();
}


function waitForNextAnimationFrame() {
    return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}


function measurementCommitSourceId() {
    /* The stored measurement a generated PEQ/FIR is derived from.  The commit
     * gate rejects a preset whose source measurement area or the processing it
     * ran through has moved since the sweep; an unsaved measurement sends no
     * id and keeps the pre-gate behaviour. */
    const current = state.measurement?.currentMeasurement;
    return String(current?.id || '');
}


function getMeasurementGraphRenderContext() {
    const canvas = elements.measurementGraph;
    if (!canvas) return null;
    const displaySize = MeasurementUI.getMeasurementGraphDisplaySize(canvas);
    if (!displaySize.width || !displaySize.height) return null;
    const displayWidth = displaySize.width;
    const displayHeight = displaySize.height;
    const bounds = MeasurementUI.getMeasurementGraphBounds(displayWidth, displayHeight);
    const range = MeasurementDsp.getMeasurementGraphRange(MeasurementGraph.getGraphMeasurementEntries());
    return { canvas, displayWidth, displayHeight, bounds, range };
}

function getMeasurementGraphView() {
    return state.measurement?.measurementView === 'ir' ? 'ir' : 'freq';
}


function buildMeasurementIrGraphEntry(measurement = {}, { current = false, graphColor = '' } = {}) {
    const displayTraces = getMeasurementDisplayTraces(measurement)
        .filter(trace => String(trace.kind || 'measured') !== 'target' && String(trace.kind || '').indexOf('filter') === -1);
    if (!displayTraces.length) return null;
    const points = MeasurementUI.getMeasurementIrPreviewPoints(measurement);
    if (!points.length) return null;
    return {
        ...measurement,
        traces: [{
            kind: 'impulse-response-preview',
            label: displayTraces[0]?.label || measurement.name || 'Impulse response',
            color: displayTraces[0]?.color || graphColor,
            role: displayTraces[0]?.role || 'trusted',
            points,
        }],
        current,
        graphColor: graphColor || (current ? measurementCurrentColor : ''),
    };
}


function formatMeasurementIrCompactRange(values = [], { digits = 0, suffix = '' } = {}) {
    const finiteValues = values.map(Number).filter(Number.isFinite);
    if (!finiteValues.length) return 'n/a';
    const minValue = Math.min(...finiteValues);
    const maxValue = Math.max(...finiteValues);
    const formatValue = value => Number(value).toFixed(digits);
    if (Math.abs(maxValue - minValue) < (digits ? 0.05 : 0.5)) return `${formatValue(minValue)}${suffix}`;
    return `${formatValue(minValue)}–${formatValue(maxValue)}${suffix}`;
}


function renderMeasurementIrDiagnostics(graphEntries = [], frequencyView = true) {
    if (!elements.measurementIrDiagnostics) return;
    const diagnostics = MeasurementUI.buildMeasurementIrDiagnostics(graphEntries, frequencyView);
    const tooltip = MeasurementUI.buildMeasurementIrDiagnosticsTooltip(diagnostics);
    elements.measurementIrDiagnostics.classList.add('hidden');
    elements.measurementIrDiagnostics.textContent = '';
    elements.measurementIrDiagnostics.title = tooltip;
}

function getMeasurementGraphPointerPosition(event) {
    const context = getMeasurementGraphRenderContext();
    if (!context) return null;
    const rect = context.canvas.getBoundingClientRect();
    return {
        ...context,
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
    };
}


function getMeasurementFrequencyHoverTooltip(event) {
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return '';
    const { x, y, bounds, range } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return '';
    const frequencyHz = MeasurementDsp.measurementXToFrequency(x, bounds);
    const graphEntries = MeasurementGraph.getGraphMeasurementEntries();
    const candidates = [];
    graphEntries.forEach((entry) => {
        (entry.traces || []).forEach((trace) => {
            const levelDb = MeasurementUI.getMeasurementTraceDisplayedDbAtFrequency(trace.points || [], frequencyHz);
            if (!Number.isFinite(levelDb)) return;
            const traceY = Math.max(bounds.top, Math.min(bounds.top + bounds.height, MeasurementDsp.measurementDbToY(levelDb, bounds, range)));
            candidates.push({ levelDb, distancePx: Math.abs(traceY - y) });
        });
    });
    candidates.sort((a, b) => a.distancePx - b.distancePx);
    const candidate = candidates[0] || null;
    if (!candidate) return '';
    return `${MeasurementUI.formatMeasurementHoverFrequency(frequencyHz)} · ${MeasurementUI.formatMeasurementHoverDb(candidate.levelDb)}`;
}


function getCustomHouseCurvePointSlot(pointId) {
    const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
    const index = custom.points.findIndex((point) => point.id === pointId);
    if (index < 0) return -1;
    const slot = Number(custom.points[index]?.slot);
    return Number.isInteger(slot) && slot >= 0 && slot < 8 ? slot : index;
}

function getCustomHouseCurvePointColor(point, slot = getCustomHouseCurvePointSlot(point?.id)) {
    const palette = [
        ...(Array.isArray(measurementPeqPalette) ? measurementPeqPalette : []),
        '#34d399', '#fb7185', '#22d3ee', '#facc15',
    ];
    return palette[Math.max(0, slot) % palette.length] || '#60a5fa';
}

function getCustomHouseCurveHandlePosition(point, bounds, range) {
    return {
        x: MeasurementDsp.measurementFrequencyToX(point.freqHz || 20, bounds),
        y: Math.max(bounds.top, Math.min(bounds.top + bounds.height, MeasurementDsp.measurementDbToY(point.gainDb || 0, bounds, range))),
    };
}

function getCustomHouseCurveHandleHitRadius(pointerType = '') {
    return pointerType === 'touch'
        ? MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX
        : MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX;
}

function findCustomHouseCurveHandleAtPosition(x, y, bounds, range, pointerType = '') {
    if (getMeasurementActiveEditor() !== 'houseCurve') return null;
    const hitRadius = getCustomHouseCurveHandleHitRadius(pointerType);
    const points = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState().points;
    for (let index = points.length - 1; index >= 0; index -= 1) {
        const point = points[index];
        const handle = getCustomHouseCurveHandlePosition(point, bounds, range);
        if (Math.hypot(handle.x - x, handle.y - y) <= hitRadius) return point;
    }
    return null;
}


function getMeasurementConvolverRangeHandleAtPosition(x, y, bounds) {
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    const startX = MeasurementDsp.measurementFrequencyToX(conv.rangeStartHz, bounds);
    const endX = MeasurementDsp.measurementFrequencyToX(conv.rangeEndHz, bounds);
    if (y < bounds.top || y > bounds.top + bounds.height) return null;
    if (Math.abs(x - startX) <= 12) return 'start';
    if (Math.abs(x - endX) <= 12) return 'end';
    if (x > startX && x < endX) return 'move';
    return null;
}

function drawMeasurementTargetCurve(ctx, bounds, range) {
    const curve = getMeasurementTargetCurvePreview();
    const points = curve.points || measurementConvolverCurves.neutral.points;
    if (getMeasurementActiveEditor() === 'houseCurve' && !points.length) return;
    const frequencies = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000];
    ctx.save();
    ctx.strokeStyle = '#6ee7b7';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    frequencies.forEach((frequency, index) => {
        const x = MeasurementDsp.measurementFrequencyToX(frequency, bounds);
        const levelDb = MeasurementDsp.getMeasurementConvolverCurveDbFromPoints(points, frequency);
        const y = Math.max(bounds.top, Math.min(bounds.top + bounds.height, MeasurementDsp.measurementDbToY(levelDb, bounds, range)));
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#a7f3d0';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillText(`${curve.shortLabel || curve.label} target`, bounds.left + 8, bounds.top + bounds.height - 8);
    ctx.restore();
}

function drawMeasurementConvolverRangeOverlay(ctx, bounds) {
    if (getMeasurementActiveEditor() === 'houseCurve' || (state.measurement?.assistMode || 'peq') !== 'convolver') return;
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    const startX = MeasurementDsp.measurementFrequencyToX(conv.rangeStartHz, bounds);
    const endX = MeasurementDsp.measurementFrequencyToX(conv.rangeEndHz, bounds);
    ctx.save();
    ctx.fillStyle = 'rgba(96, 165, 250, 0.16)';
    ctx.fillRect(startX, bounds.top, Math.max(1, endX - startX), bounds.height);
    ctx.strokeStyle = 'rgba(147, 197, 253, 0.85)';
    ctx.lineWidth = 1.6;
    ctx.setLineDash([5, 4]);
    [startX, endX].forEach((rangeX) => {
        ctx.beginPath();
        ctx.moveTo(rangeX, bounds.top);
        ctx.lineTo(rangeX, bounds.top + bounds.height);
        ctx.stroke();
    });
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(191, 219, 254, 0.95)';
    ctx.font = '12px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(`${Math.round(conv.rangeStartHz)}–${Math.round(conv.rangeEndHz)} Hz`, (startX + endX) / 2, bounds.top + 8);
    ctx.restore();
}

function drawCustomHouseCurveHandles(ctx, bounds, range) {
    if (getMeasurementActiveEditor() !== 'houseCurve') return;
    const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
    if (!custom.points.length) return;
    ctx.save();
    custom.points.forEach((point) => {
        const handle = getCustomHouseCurveHandlePosition(point, bounds, range);
        const active = point.id === custom.activePointId;
        const color = getCustomHouseCurvePointColor(point);
        ctx.fillStyle = color;
        ctx.strokeStyle = active ? '#f8fafc' : 'rgba(15,23,42,0.9)';
        ctx.lineWidth = active ? 2.4 : 1.5;
        ctx.beginPath();
        ctx.arc(handle.x, handle.y, active ? 7 : 5.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
    });
    ctx.restore();
}


function getMeasurementIrHoverTooltip(event) {
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return '';
    const { x, y, bounds } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return '';
    const targetTimeMs = MeasurementUI.measurementXToIrTime(x, bounds);
    const graphEntries = MeasurementGraph.getGraphMeasurementEntries();
    const candidates = graphEntries.map((entry) => {
        const trace = (entry.traces || [])[0] || {};
        const nearest = MeasurementUI.getNearestMeasurementIrPoint(trace.points || [], targetTimeMs);
        if (!nearest) return null;
        const pointX = MeasurementUI.measurementIrTimeToX(nearest.timeMs, bounds);
        const pointY = MeasurementUI.measurementIrAmplitudeToY(nearest.amplitude, bounds);
        return {
            nearest,
            distancePx: Math.hypot(pointX - x, pointY - y),
        };
    }).filter(Boolean).sort((a, b) => a.distancePx - b.distancePx);
    const candidate = candidates[0] || null;
    if (!candidate) return '';
    const { nearest } = candidate;
    return `${MeasurementUI.formatMeasurementIrMs(nearest.timeMs)} · amp ${MeasurementUI.formatMeasurementIrAmplitude(nearest.amplitude)}`;
}

function drawMeasurementIrGraph(ctx, bounds, graphEntries) {
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    [-1, -0.5, 0, 0.5, 1].forEach((amplitude) => {
        const y = MeasurementUI.measurementIrAmplitudeToY(amplitude, bounds);
        ctx.beginPath();
        ctx.moveTo(bounds.left, y);
        ctx.lineTo(bounds.left + bounds.width, y);
        ctx.stroke();
        ctx.fillStyle = amplitude === 0 ? '#d1fae5' : 'rgba(236,236,240,0.72)';
        ctx.font = '12px sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(amplitude === 0 ? '0' : amplitude.toFixed(1), bounds.left - 10, y);
    });

    [-2, 0, 5, 10, 15, 20, 25, 30].forEach((timeMs) => {
        const x = MeasurementUI.measurementIrTimeToX(timeMs, bounds);
        ctx.strokeStyle = timeMs === 0 ? 'rgba(209,250,229,0.26)' : 'rgba(255,255,255,0.08)';
        ctx.beginPath();
        ctx.moveTo(x, bounds.top);
        ctx.lineTo(x, bounds.top + bounds.height);
        ctx.stroke();
        ctx.fillStyle = 'rgba(236,236,240,0.72)';
        ctx.font = '12px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(`${timeMs} ms`, x, bounds.top + bounds.height + 10);
    });

    graphEntries.forEach(entry => {
        (entry.traces || []).forEach(trace => {
            if (!trace.points.length) return;
            const isReviewTrace = trace.role === 'raw-review';
            ctx.strokeStyle = entry.graphColor || trace.color || '#6ee7b7';
            ctx.lineWidth = entry.current ? 2.8 : (isReviewTrace ? 1.8 : 2.1);
            ctx.setLineDash(entry.current ? [] : (isReviewTrace ? [6, 5] : [10, 6]));
            ctx.beginPath();
            trace.points.forEach(([timeMs, amplitude], pointIndex) => {
                const x = MeasurementUI.measurementIrTimeToX(timeMs, bounds);
                const y = MeasurementUI.measurementIrAmplitudeToY(amplitude, bounds);
                if (pointIndex === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.setLineDash([]);
        });
    });
}

function handleMeasurementGraphPointerDown(event) {
    if (getMeasurementGraphView() === 'ir') return;
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return;
    const { x, y, bounds, range } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return;
    const pointerType = String(event.pointerType || '');
    if (getMeasurementActiveEditor() === 'houseCurve') {
        const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
        const hitPoint = findCustomHouseCurveHandleAtPosition(x, y, bounds, range, pointerType);
        const point = hitPoint || window.FXRouteMeasurementCalibration.addCustomHouseCurvePointAtPosition({ x, y, bounds, range });
        if (!point) return;
        if (pointerType === 'touch') event.preventDefault();
        custom.activePointId = point.id;
        custom.dragPointId = point.id;
        measurementGraphPointerId = event.pointerId;
        elements.measurementGraph?.setPointerCapture?.(event.pointerId);
        renderMeasurementPanel();
        MeasurementGraph.scheduleMeasurementGraphRender();
        return;
    }
    if (getMeasurementActiveEditor() === 'houseCurve') return;
    if ((state.measurement?.assistMode || 'peq') === 'convolver') {
        const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
        const dragMode = getMeasurementConvolverRangeHandleAtPosition(x, y, bounds);
        if (!dragMode) return;
        if (pointerType === 'touch') event.preventDefault();
        conv.dragMode = dragMode;
        conv.dragAnchorHz = MeasurementDsp.measurementXToFrequency(x, bounds);
        conv.dragStartHz = conv.rangeStartHz;
        conv.dragEndHz = conv.rangeEndHz;
        measurementGraphPointerId = event.pointerId;
        elements.measurementGraph?.setPointerCapture?.(event.pointerId);
        renderMeasurementPanel();
        return;
    }
    window.FXRouteMeasurementPeqEditor.handleMeasurementPeqPointerDown(event, pointer, pointerType);
}

function handleMeasurementGraphPointerMove(event) {
    if (getMeasurementGraphView() === 'ir') {
        if (elements.measurementGraph) {
            elements.measurementGraph.title = getMeasurementIrHoverTooltip(event);
        }
        return;
    }
    if (elements.measurementGraph) {
        elements.measurementGraph.title = getMeasurementFrequencyHoverTooltip(event);
    }
    const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
    if (custom.dragPointId && measurementGraphPointerId === event.pointerId && getMeasurementActiveEditor() === 'houseCurve') {
        if (event.pointerType === 'touch') event.preventDefault();
        const pointer = getMeasurementGraphPointerPosition(event);
        if (!pointer) return;
        const point = custom.points.find((item) => item.id === custom.dragPointId);
        if (!point) return;
        const visibleFrequency = MeasurementDsp.measurementXToFrequency(pointer.x, pointer.bounds);
        const visibleGain = Math.min(pointer.range.maxDb, Math.max(pointer.range.minDb, MeasurementDsp.measurementYToDb(pointer.y, pointer.bounds, pointer.range)));
        window.FXRouteMeasurementCalibration.updateCustomHouseCurvePoint(point.id, { freqHz: visibleFrequency, gainDb: visibleGain });
        MeasurementGraph.scheduleMeasurementGraphRender();
        renderMeasurementPanel();
        return;
    }
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    if (conv.dragMode && measurementGraphPointerId === event.pointerId) {
        if (event.pointerType === 'touch') event.preventDefault();
        const pointer = getMeasurementGraphPointerPosition(event);
        if (!pointer) return;
        const currentHz = MeasurementDsp.measurementXToFrequency(pointer.x, pointer.bounds);
        if (conv.dragMode === 'start') conv.rangeStartHz = Math.min(Math.round(window.FXRouteMeasurementConvolverEditor.clampMeasurementConvolverFrequency(currentHz)), conv.rangeEndHz - 1);
        if (conv.dragMode === 'end') conv.rangeEndHz = Math.max(Math.round(window.FXRouteMeasurementConvolverEditor.clampMeasurementConvolverFrequency(currentHz)), conv.rangeStartHz + 1);
        if (conv.dragMode === 'move') {
            const ratio = Math.log10(currentHz / Math.max(1, conv.dragAnchorHz || currentHz));
            const start = window.FXRouteMeasurementConvolverEditor.clampMeasurementConvolverFrequency((conv.dragStartHz || conv.rangeStartHz) * (10 ** ratio));
            const end = window.FXRouteMeasurementConvolverEditor.clampMeasurementConvolverFrequency((conv.dragEndHz || conv.rangeEndHz) * (10 ** ratio));
            const widthRatio = (conv.dragEndHz || conv.rangeEndHz) / Math.max(1, conv.dragStartHz || conv.rangeStartHz);
            if (start <= 20) {
                conv.rangeStartHz = 20;
                conv.rangeEndHz = Math.min(20000, Math.round(20 * widthRatio));
            } else if (end >= 20000) {
                conv.rangeEndHz = 20000;
                conv.rangeStartHz = Math.max(20, Math.round(20000 / widthRatio));
            } else {
                conv.rangeStartHz = Math.round(start);
                conv.rangeEndHz = Math.round(end);
            }
        }
        window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
        MeasurementGraph.scheduleMeasurementGraphRender();
        renderMeasurementPanel();
        return;
    }
    window.FXRouteMeasurementPeqEditor.handleMeasurementPeqPointerMove(event);
}

function handleMeasurementGraphPointerLeave() {
    if (elements.measurementGraph) elements.measurementGraph.title = '';
}

function handleMeasurementGraphPointerUp(event) {
    if (getMeasurementGraphView() === 'ir') return;
    const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId && event.pointerType === 'touch') {
        event.preventDefault();
    }
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId) {
        elements.measurementGraph?.releasePointerCapture?.(event.pointerId);
        measurementGraphPointerId = null;
    }
    window.FXRouteMeasurementPeqEditor.clearMeasurementPeqPointerDrag();
    custom.dragPointId = null;
    conv.dragMode = null;
    delete conv.dragAnchorHz;
    delete conv.dragStartHz;
    delete conv.dragEndHz;
}


function measurementModeReady() {
    return !!state.measurement.hostCaptureAvailable && !!state.measurement.selectedInputId;
}

function describeMeasurementScope(scopeNote = '') {
    if (scopeNote) return 'Host-local sweep ready. Full graph view is available after capture.';
    return 'Host-local sweep ready. Calibration file is optional.';
}

function measurementModeNoteText() {
    return 'Host-local capture on this system.';
}


async function fetchMeasurements() {
    const settingsRevision = window.FXRouteMeasurementSetup.getMeasurementSettingsRevision();
    state.measurement.loading = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch('/api/measurements');
        if (!resp.ok) throw new Error('Failed to fetch measurements');
        const data = await resp.json();
        const measurements = Array.isArray(data.measurements) ? data.measurements.map((measurement, index) => MeasurementUI.normalizeMeasurementEntry(measurement, index)) : [];
        state.measurement.measurements = measurements;
        state.measurement.visibilityById = MeasurementUI.normalizeMeasurementVisibility(measurements, state.measurement.visibilityById || {});
        state.measurement.reviewVisibilityById = MeasurementUI.normalizeMeasurementReviewVisibility(measurements, state.measurement.reviewVisibilityById || {});
        state.measurement.storage = data.storage || null;
        window.FXRouteMeasurementCalibration.applyMeasurementFileCatalog(data);
        if (settingsRevision === window.FXRouteMeasurementSetup.getMeasurementSettingsRevision()) {
            window.FXRouteMeasurementSetup.applyMeasurementSetupSettings(data.measurement_settings || {});
        }
        window.FXRouteMeasurementCalibration.validateMeasurementCalibrationSelection();
        if (!state.measurement.startInFlight && !state.measurement.saveInFlight && !state.measurement.activeJobId && !state.measurement.currentMeasurement) {
            state.measurement.statusText = describeMeasurementScope(data.scope_note);
        }
    } catch (error) {
        console.error('fetchMeasurements failed', error);
        state.measurement.statusText = error.message || 'Failed to load measurements';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.loading = false;
        renderMeasurementPanel();
    }
}

function measurementInputAvailabilityMessage() {
    const measurementState = state.measurement || {};
    if (measurementState.startInFlight || measurementState.activeJobId) return '';
    if (measurementState.selectedInputUnavailable) {
        return 'The selected measurement microphone is currently unavailable. Reconnect it or deliberately select another input.';
    }
    if (!measurementState.hostCaptureAvailable) {
        return (measurementState.inputs || []).length
            ? 'No available capture source is ready right now.'
            : 'No PipeWire capture sources are currently visible on this host.';
    }
    return '';
}

function measurementSetupStatusText() {
    const measurementState = state.measurement || {};
    return measurementInputAvailabilityMessage() || measurementState.statusText || describeMeasurementScope();
}


function isMeasurementPanelOpen() {
    return !!elements.measurementPanel && !elements.measurementPanel.classList.contains('hidden');
}

function sendMeasurementWindowHeartbeat(open, keepalive = false) {
    const options = {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ open: !!open }),
    };
    if (keepalive) options.keepalive = true;
    return fetch('/api/power/measurement-heartbeat', options).catch((error) => {
        if (!keepalive) console.warn('measurement heartbeat failed', error);
    });
}

function startMeasurementWindowHeartbeat() {
    void sendMeasurementWindowHeartbeat(true);
    if (measurementWindowHeartbeatTimer) return;
    measurementWindowHeartbeatTimer = setInterval(() => {
        if (!isMeasurementPanelOpen()) {
            stopMeasurementWindowHeartbeat();
            return;
        }
        void sendMeasurementWindowHeartbeat(true);
    }, MEASUREMENT_WINDOW_HEARTBEAT_INTERVAL_MS);
}

function stopMeasurementWindowHeartbeat(keepalive = false) {
    if (measurementWindowHeartbeatTimer) {
        clearInterval(measurementWindowHeartbeatTimer);
        measurementWindowHeartbeatTimer = null;
    }
    void sendMeasurementWindowHeartbeat(false, keepalive);
}

const MEASUREMENT_AUTO_SUB_STATUS_DEFAULT_TEXT = 'Scans sub delay around crossover, picks best alignment.';

function resetMeasurementTransientStatus() {
    // A cancelled/completed/failed AutoSub (or sweep) leaves its statusText
    // and the directly written AutoSub inline status behind; no render pass
    // ever restores them, so reopening the panel would show stale
    // cancelled/completed/error/progress/result state until a page refresh.
    // Unsaved measurement data (autoSubMeasurements, currentMeasurement) is
    // deliberately kept: it is saveable content, not transient status.
    const measurementState = state.measurement || {};
    if (measurementState.autoSubInFlight || measurementState.speakerAlignInFlight || measurementState.startInFlight
        || measurementState.activeJobId || measurementState.autoSubJobId || measurementState.speakerAlignJobId
        || measurementState.activeMeasurementKind) return;
    measurementState.statusText = '';
    measurementState.autoSubResult = null;
    measurementState.speakerAlignResult = null;
    measurementState.speakerAlignResults = null;
    if (elements.measurementAutoSubStatus) {
        elements.measurementAutoSubStatus.textContent = MEASUREMENT_AUTO_SUB_STATUS_DEFAULT_TEXT;
    }
    if (elements.measurementSpeakerAlignStatus) {
        elements.measurementSpeakerAlignStatus.textContent = '';
    }
    if (elements.measurementSpeakerAlignResults) {
        elements.measurementSpeakerAlignResults.innerHTML = '';
    }
}

function toggleMeasurementPanel(forceOpen = null) {
    if (!elements.measurementPanel) return;
    state.measurement.modeNote = measurementModeNoteText();
    const shouldOpen = forceOpen === null ? elements.measurementPanel.classList.contains('hidden') : !!forceOpen;
    state.measurement.open = shouldOpen;
    elements.measurementPanel.classList.toggle('hidden', !shouldOpen);
    if (elements.effectsMeasureOpenBtn) {
        elements.effectsMeasureOpenBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    }
    if (!shouldOpen) setMeasurementSweepMenuOpen(false);
    if (shouldOpen) {
        startMeasurementWindowHeartbeat();
        measurementInputScanOnFocusDone = false;
        resetMeasurementTransientStatus();
        renderMeasurementPanel();
        void window.FXRouteMeasurementSetup.fetchMeasurementInputs();
        void ensureMeasurementAreaCatalog();
        MeasurementGraph.scheduleMeasurementGraphRender();
        window.FXRouteModal?.open(elements.measurementPanel, {
            initialFocus: elements.measurementCloseBtn,
            onEscape: () => {
                if (elements.measurementSweepMenu && !elements.measurementSweepMenu.classList.contains('hidden')) {
                    setMeasurementSweepMenuOpen(false);
                    return;
                }
                if (state.measurement.setupOpen) {
                    setMeasurementSetupOpen(false);
                    return;
                }
                toggleMeasurementPanel(false);
            },
        });
    } else {
        stopMeasurementWindowHeartbeat();
        if (typeof MeasurementFlows !== 'undefined' && typeof MeasurementFlows.clearSpeakerAlignResult === 'function') {
            MeasurementFlows.clearSpeakerAlignResult();
        } else {
            const measurementState = state.measurement || {};
            if (!measurementState.speakerAlignInFlight && !measurementState.speakerAlignJobId
                && measurementState.activeMeasurementKind !== 'speaker_align') {
                measurementState.speakerAlignResult = null;
                measurementState.speakerAlignResults = null;
                if (elements.measurementSpeakerAlignStatus) elements.measurementSpeakerAlignStatus.textContent = '';
                if (elements.measurementSpeakerAlignResults) elements.measurementSpeakerAlignResults.innerHTML = '';
            }
        }
        window.FXRouteModal?.close(elements.measurementPanel);
    }
}


function drawMeasurementPeqOverlay(ctx, bounds, range) {
    if (getMeasurementActiveEditor() !== 'peq') return;
    const peq = window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState();
    if (!peq.enabled || !peq.filters.length) return;
    const sampleFrequencies = Array.from({ length: 220 }, (_, index) => 20 * (10 ** ((Math.log10(20000 / 20) * index) / 219)));
    const activeFilterId = peq.activeFilterId;

    peq.filters.forEach((filter) => {
        if (filter.type === 'gain') {
            const y = MeasurementDsp.measurementDbToY(filter.gainDb || 0, bounds, range);
            ctx.strokeStyle = `${filter.color}88`;
            ctx.lineWidth = filter.id === activeFilterId ? 1.7 : 1.1;
            ctx.setLineDash([4, 4]);
            ctx.beginPath();
            ctx.moveTo(bounds.left, y);
            ctx.lineTo(bounds.left + bounds.width, y);
            ctx.stroke();
            ctx.setLineDash([]);
            return;
        }
        ctx.strokeStyle = `${filter.color}${filter.id === activeFilterId ? 'dd' : '88'}`;
        ctx.lineWidth = filter.id === activeFilterId ? 2 : 1.2;
        ctx.beginPath();
        sampleFrequencies.forEach((frequencyHz, index) => {
            const level = MeasurementDsp.getMeasurementPeqFilterMagnitude(filter, frequencyHz);
            const x = MeasurementDsp.measurementFrequencyToX(frequencyHz, bounds);
            const y = MeasurementDsp.measurementDbToY(level, bounds, range);
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
    });

    ctx.strokeStyle = '#f8fafc';
    ctx.lineWidth = 2.1;
    ctx.beginPath();
    sampleFrequencies.forEach((frequencyHz, index) => {
        const summed = peq.filters.reduce((sum, filter) => sum + MeasurementDsp.getMeasurementPeqFilterMagnitude(filter, frequencyHz), 0);
        const x = MeasurementDsp.measurementFrequencyToX(frequencyHz, bounds);
        const y = MeasurementDsp.measurementDbToY(summed, bounds, range);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    peq.filters.forEach((filter) => {
        const handle = window.FXRouteMeasurementPeqEditor.getMeasurementPeqHandlePosition(filter, bounds, range);
        ctx.fillStyle = filter.color;
        ctx.strokeStyle = filter.id === activeFilterId ? '#f8fafc' : 'rgba(15,23,42,0.9)';
        ctx.lineWidth = filter.id === activeFilterId ? 2.4 : 1.5;
        ctx.beginPath();
        ctx.arc(handle.x, handle.y, filter.id === activeFilterId ? 7 : 5.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
    });
}


function sleep(ms) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.sleep(ms);
}


function renderMeasurementPanel() {
    if (!elements.measurementSummary || !elements.measurementList) return;
    const measurementState = state.measurement || {};
    window.FXRouteMeasurementSetup.normalizeMeasurementInputChannelSelections();
    measurementState.modeNote = measurementModeNoteText();
    const current = getCurrentMeasurementEntry();
    const measurements = window.FXRouteMeasurementSavedUI.getSavedListMeasurements();
    const graphEntries = MeasurementGraph.getGraphMeasurementEntries();
    const assistMode = measurementState.assistMode === 'convolver' ? 'convolver' : 'peq';
    const activeEditor = getMeasurementActiveEditor();
    const graphView = getMeasurementGraphView();
    const frequencyView = graphView === 'freq';
    const peq = window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState();
    const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
    const activePeqFilter = window.FXRouteMeasurementPeqEditor.getMeasurementPeqActiveFilter();

    const ctx = { measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter };
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelSetupSection(ctx);
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelInputsSection(ctx);
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelCalibrationSection(ctx);
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelHouseCurveSection(ctx);
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelActionsSection(ctx);
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelViewSection(ctx);
    window.FXRouteMeasurementPanelUI.renderMeasurementPanelStatusSection(ctx);
    window.FXRouteMeasurementEditorsUI.renderMeasurementPanelEditorsSection(ctx);
    window.FXRouteMeasurementEditorsUI.renderMeasurementPanelConvolverSection(ctx);
    window.FXRouteMeasurementSavedUI.renderMeasurementPanelSavedListSection(ctx);
    MeasurementFlows.syncAutoSubButton();
    MeasurementFlows.syncSpeakerAlignButton();
    MeasurementGraph.scheduleMeasurementGraphRender();
}

function measurementBankSumsBothInputs() {
    /* A mono bank is fed by both inputs (one output role), so per-side takes
     * would stage one half of a correction the engine applies to both inputs. */
    const area = measurementAreaFromCatalog();
    return area?.channel_mode === 'mono';
}

function setMeasurementSetupOpen(open) {
    state.measurement.setupOpen = open;
    setMeasurementSweepMenuOpen(false);
    renderMeasurementPanel();
    const focusTarget = open ? elements.measurementSetupBackBtn : elements.measurementSetupToggleBtn;
    focusTarget?.focus();
    if (open) elements.measurementPanel.querySelector('.measurement-dialog').scrollTop = 0;
}

function setupMeasurementActions() {
    if (!elements.measurementPanel || !elements.effectsMeasureOpenBtn || !elements.measurementCloseBtn) return;
    window.FXRouteMeasurementEditorsUI.bindMeasurementEditorDelegation();
    window.FXRouteMeasurementSavedUI.bindMeasurementSavedListDelegation();
    elements.effectsMeasureOpenBtn.addEventListener('click', () => toggleMeasurementPanel(true));
    elements.measurementCloseBtn.addEventListener('click', () => toggleMeasurementPanel(false));
    const backdrop = elements.measurementPanel.querySelector('.manage-overlay-backdrop');
    if (backdrop) backdrop.addEventListener('click', () => toggleMeasurementPanel(false));
    window.addEventListener('pagehide', () => {
        if (isMeasurementPanelOpen()) stopMeasurementWindowHeartbeat(true);
    });
    if (elements.measurementSetupToggleBtn) {
        elements.measurementSetupToggleBtn.addEventListener('click', () => {
            setMeasurementSetupOpen(true);
        });
    }
    elements.measurementSetupBackBtn?.addEventListener('click', () => setMeasurementSetupOpen(false));
    if (elements.measurementSweepToggleBtn) {
        elements.measurementSweepToggleBtn.addEventListener('click', () => {
            if (state.measurement.startInFlight || window.FXRouteMeasurementJob.hasActiveMeasurementJob()) {
                void window.FXRouteMeasurementJob.requestMeasurementCancellation();
                return;
            }
            const shouldOpen = elements.measurementSweepMenu?.classList.contains('hidden');
            setMeasurementSweepMenuOpen(!!shouldOpen);
        });
    }
    if (elements.measurementSweepMenu) {
        elements.measurementSweepMenu.addEventListener('click', (event) => {
            // A side chip only narrows the single sweep: keep the menu open so
            // the user can still read the area note and press Run. Only the
            // action buttons (Start, Repeat, Advanced) close it.
            if (event.target.closest('#measurement-sweep-start, #measurement-repeat-start, #measurement-hybrid-open')) setMeasurementSweepMenuOpen(false);
        });
        document.addEventListener('click', (event) => {
            if (!elements.measurementSweepMenu.classList.contains('hidden')
                    && !event.target.closest('.measurement-workflow-menu')) {
                setMeasurementSweepMenuOpen(false);
            }
        });
    }
    const smoothingChipsRow = document.getElementById('measurement-smoothing-chips');
    if (smoothingChipsRow) window.FXRouteMeasurementPanelUI.bindChipArrowKeyNavigation(smoothingChipsRow, '[data-measurement-smoothing]');
    const measurementViewToggle = document.querySelector('.measurement-view-toggle');
    if (measurementViewToggle) window.FXRouteMeasurementPanelUI.bindChipArrowKeyNavigation(measurementViewToggle, '[data-measurement-view]');
    if (elements.measurementInputSelect) {
        const scanMeasurementInputsOnceForSelect = () => {
            if (measurementInputScanOnFocusDone) return;
            measurementInputScanOnFocusDone = true;
            void window.FXRouteMeasurementSetup.fetchMeasurementInputs();
        };
        elements.measurementInputSelect.addEventListener('pointerdown', scanMeasurementInputsOnceForSelect);
        elements.measurementInputSelect.addEventListener('focus', scanMeasurementInputsOnceForSelect);
        elements.measurementInputSelect.addEventListener('change', (event) => {
            window.FXRouteMeasurementSetup.applyMeasurementInputSelection(event.target.value || '');
        });
    }
    if (elements.measurementInputRefreshBtn) {
        elements.measurementInputRefreshBtn.addEventListener('click', () => {
            void window.FXRouteMeasurementSetup.fetchMeasurementInputs();
        });
    }
    const saveMeasurementReferenceSelections = () => {
        void window.FXRouteMeasurementSetup.saveMeasurementSetupSettings({
            selectedMicInputChannel: state.measurement.selectedMicInputChannel,
            selectedReferenceInputChannel: state.measurement.selectedReferenceInputChannel || '',
            selectedReferenceInputChannelLeft: state.measurement.selectedReferenceInputChannelLeft || '',
            selectedReferenceInputChannelRight: state.measurement.selectedReferenceInputChannelRight || '',
        });
    };
    if (elements.measurementMicInputChannelSelect) {
        elements.measurementMicInputChannelSelect.addEventListener('change', (event) => {
            state.measurement.selectedMicInputChannel = event.target.value || '1';
            window.FXRouteMeasurementSetup.normalizeMeasurementInputChannelSelections();
            saveMeasurementReferenceSelections();
            renderMeasurementPanel();
        });
    }
    const bindReferenceInputChannelSelect = (select, stateKey) => {
        if (!select) return;
        select.addEventListener('change', (event) => {
            state.measurement[stateKey] = event.target.value || '';
            window.FXRouteMeasurementSetup.normalizeMeasurementInputChannelSelections();
            renderMeasurementPanel();
            saveMeasurementReferenceSelections();
        });
    };
    bindReferenceInputChannelSelect(elements.measurementReferenceInputChannelSelect, 'selectedReferenceInputChannel');
    bindReferenceInputChannelSelect(elements.measurementReferenceInputChannelLeftSelect, 'selectedReferenceInputChannelLeft');
    bindReferenceInputChannelSelect(elements.measurementReferenceInputChannelRightSelect, 'selectedReferenceInputChannelRight');
    if (elements.measurementSweepStartBtn) {
        elements.measurementSweepStartBtn.addEventListener('click', () => {
            if (state.measurement.startInFlight || window.FXRouteMeasurementJob.hasActiveMeasurementJob()) return;
            setMeasurementSweepMenuOpen(false);
            void window.FXRouteMeasurementCapture.startMeasurement();
        });
    }
    document.querySelectorAll('#measurement-sweep-side-row [data-sweep-side]').forEach((button) => {
        button.addEventListener('click', () => {
            if (state.measurement.startInFlight || window.FXRouteMeasurementJob.hasActiveMeasurementJob()) return;
            const side = button.getAttribute('data-sweep-side') || 'stereo';
            const area = measurementAreaFromCatalog();
            if (!Array.isArray(area?.sides) || !area.sides.includes(side)) return;
            state.measurement.sweepSide = side;
            syncSweepSideRow(area);
        });
    });
    document.querySelectorAll('[data-measurement-smoothing]').forEach((button) => {
        button.addEventListener('click', () => {
            if (getMeasurementGraphView() === 'ir') return;
            state.measurement.displaySmoothing = button.getAttribute('data-measurement-smoothing') || '1/6-oct';
            renderMeasurementPanel();
            MeasurementGraph.scheduleMeasurementGraphRender();
        });
    });
    document.querySelectorAll('[data-measurement-view]').forEach((button) => {
        button.addEventListener('click', () => {
            state.measurement.measurementView = button.getAttribute('data-measurement-view') || 'freq';
            window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState().dragFilterId = null;
            window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState().dragPointId = null;
            window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState().dragMode = null;
            measurementGraphPointerId = null;
            renderMeasurementPanel();
            MeasurementGraph.scheduleMeasurementGraphRender();
        });
    });
    if (elements.measurementCalibrationSelect) {
        elements.measurementCalibrationSelect.addEventListener('change', (event) => {
            state.measurement.selectedCalibrationRef = event.target.value || '';
            if (elements.measurementCalibrationFile) elements.measurementCalibrationFile.value = '';
            state.measurement.calibrationFilename = '';
            renderMeasurementPanel();
            void window.FXRouteMeasurementCalibration.setActiveMeasurementCalibration(state.measurement.selectedCalibrationRef);
        });
    }
    if (elements.measurementCalibrationFile) {
        elements.measurementCalibrationFile.addEventListener('change', () => {
            const file = elements.measurementCalibrationFile.files?.[0];
            if (file) {
                void window.FXRouteMeasurementCalibration.uploadMeasurementCalibration(file);
            } else {
                state.measurement.calibrationFilename = '';
                renderMeasurementPanel();
            }
        });
    }
    if (elements.measurementCalibrationDeleteBtn) {
        elements.measurementCalibrationDeleteBtn.addEventListener('click', () => { void window.FXRouteMeasurementCalibration.deleteSelectedMeasurementCalibration(); });
    }
    if (elements.measurementCalibrationExportBtn) {
        elements.measurementCalibrationExportBtn.addEventListener('click', () => { void window.FXRouteMeasurementCalibration.downloadSelectedMeasurementCalibration(); });
    }
    if (elements.measurementHouseCurveSelect) {
        elements.measurementHouseCurveSelect.addEventListener('change', (event) => {
            const houseCurveId = event.target.value || '';
            if (elements.measurementHouseCurveFile) elements.measurementHouseCurveFile.value = '';
            state.measurement.houseCurveFilename = '';
            setMeasurementActiveEditor('none');
            window.FXRouteMeasurementConvolverEditor.updateMeasurementConvolverField('targetCurve', houseCurveId ? `house:${houseCurveId}` : 'neutral');
            renderMeasurementPanel();
            MeasurementGraph.scheduleMeasurementGraphRender();
        });
    }
    if (elements.measurementHouseCurveFile) {
        elements.measurementHouseCurveFile.addEventListener('change', () => {
            const file = elements.measurementHouseCurveFile.files?.[0];
            if (file) {
                void window.FXRouteMeasurementCalibration.uploadMeasurementHouseCurve(file);
            } else {
                state.measurement.houseCurveFilename = '';
                renderMeasurementPanel();
            }
        });
    }
    if (elements.measurementHouseCurveDeleteBtn) {
        elements.measurementHouseCurveDeleteBtn.addEventListener('click', () => { void window.FXRouteMeasurementCalibration.deleteSelectedMeasurementHouseCurve(); });
    }
    if (elements.measurementHouseCurveExportBtn) {
        elements.measurementHouseCurveExportBtn.addEventListener('click', () => { void window.FXRouteMeasurementCalibration.downloadSelectedMeasurementHouseCurve(); });
    }
    if (elements.measurementNameInput) {
        elements.measurementNameInput.addEventListener('input', (event) => {
            state.measurement.currentMeasurementName = event.target.value || '';
        });
    }
    if (elements.measurementRepeatStartBtn) {
        elements.measurementRepeatStartBtn.addEventListener('click', () => {
            setMeasurementSweepMenuOpen(false);
            if (window.FXRouteMeasurementJob.getActiveMeasurementKind() === 'lr_repeat') {
                void window.FXRouteMeasurementJob.cancelMeasurement();
                return;
            }
            void window.FXRouteMeasurementCapture.startLrRepeat();
        });
    }
    if (elements.measurementAutoSubStartBtn) {
        elements.measurementAutoSubStartBtn.addEventListener('click', () => {
            const measurementState = state.measurement || {};
            if (measurementState.autoSubInFlight) {
                void MeasurementFlows.cancelAutoSubOptimize();
                return;
            }
            void MeasurementFlows.startAutoSubOptimize();
        });
    }
    elements.measurementSpeakerAlignLeftBtn?.addEventListener('click', () => { void MeasurementFlows.startSpeakerAlign('left'); });
    elements.measurementSpeakerAlignRightBtn?.addEventListener('click', () => { void MeasurementFlows.startSpeakerAlign('right'); });
    elements.measurementSpeakerAlignCancelBtn?.addEventListener('click', () => { void MeasurementFlows.cancelSpeakerAlign(); });
    elements.measurementSpeakerAlignSaveBtn?.addEventListener('click', () => { void MeasurementFlows.saveSpeakerAlignRun(); });
    elements.measurementSpeakerAlignOpenBtn?.addEventListener('click', () => { void MeasurementFlows.openSpeakerAlignRun(); });
    if (elements.measurementSaveBtn) {
        elements.measurementSaveBtn.addEventListener('click', () => { void window.FXRouteMeasurementSavedActions.saveCurrentMeasurement(); });
    }
    if (elements.measurementClearBtn) {
        elements.measurementClearBtn.addEventListener('click', () => resetMeasurementGraph());
    }
    if (elements.measurementAssistMode) {
        elements.measurementAssistMode.addEventListener('change', (event) => setMeasurementAssistMode(event.target.value));
        let assistModeFocusPending = false;
        const activateSelectedMethod = () => {
            if (getMeasurementActiveEditor() !== 'houseCurve') return;
            const selectedMode = elements.measurementAssistMode.value === 'convolver' ? 'convolver' : 'peq';
            setMeasurementAssistMode(selectedMode);
        };
        // `change` is not emitted when the user re-selects the already active
        // method. Ignore the opening click that focuses the native select;
        // a click after the popup selection (including same-value PEQ) then
        // performs the complete activation. A changed value is still handled
        // by `change`, without prematurely treating Convolver as PEQ.
        elements.measurementAssistMode.addEventListener('focus', () => { assistModeFocusPending = true; });
        elements.measurementAssistMode.addEventListener('blur', () => { assistModeFocusPending = false; });
        elements.measurementAssistMode.addEventListener('click', () => {
            if (assistModeFocusPending) {
                assistModeFocusPending = false;
                return;
            }
            window.setTimeout(activateSelectedMethod, 0);
        });
    }
    if (elements.measurementTargetCurve) {
        elements.measurementTargetCurve.addEventListener('change', (event) => window.FXRouteMeasurementCalibration.handleMeasurementTargetCurveSelection(event.target.value));
    }
    if (elements.measurementCustomHouseCurveName) {
        elements.measurementCustomHouseCurveName.addEventListener('input', (event) => {
            const custom = window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState();
            custom.name = event.target.value || '';
            custom.nameTouched = true;
            if (elements.measurementCustomHouseCurveCreateBtn) {
                elements.measurementCustomHouseCurveCreateBtn.disabled = custom.points.length < 2 || !custom.name.trim() || custom.saving;
            }
        });
    }
    if (elements.measurementCustomHouseCurveCreateBtn) {
        elements.measurementCustomHouseCurveCreateBtn.addEventListener('click', () => { void window.FXRouteMeasurementCalibration.createCustomHouseCurve(); });
    }
    [elements.measurementConvolverTarget, elements.measurementConvolverRangeStart, elements.measurementConvolverRangeEnd, elements.measurementConvolverMaxBoost, elements.measurementConvolverMaxCut, elements.measurementConvolverDipGuard, elements.measurementConvolverSampleRate, elements.measurementConvolverPhaseMode, elements.measurementConvolverIrLength].forEach((input) => {
        if (!input) return;
        const commit = () => {
            setMeasurementActiveEditor('none');
            window.FXRouteMeasurementConvolverEditor.updateMeasurementConvolverField(input.dataset.measurementConvolverField, input.value);
            renderMeasurementPanel();
            MeasurementGraph.scheduleMeasurementGraphRender();
        };
        input.addEventListener('change', commit);
        if (input instanceof HTMLInputElement) input.addEventListener('input', commit);
    });
    if (elements.measurementConvolverPresetName) {
        elements.measurementConvolverPresetName.addEventListener('input', (event) => {
            const conv = window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState();
            conv.draft.presetName = event.target.value || '';
            conv.draft.nameTouched = true;
            if (elements.measurementConvolverCreateBtn) {
                const hasDraft = !!conv.draft.left || !!conv.draft.right;
                elements.measurementConvolverCreateBtn.disabled = !hasDraft || !!window.FXRouteMeasurementConvolverEditor.getMeasurementConvolverDraftPhaseMismatch(conv) || !conv.draft.presetName.trim() || convolverCreateInFlight;
            }
        });
    }
    if (elements.measurementConvolverTakeLeftBtn) {
        elements.measurementConvolverTakeLeftBtn.addEventListener('click', () => window.FXRouteMeasurementConvolverEditor.takeMeasurementConvolverToDraft('left'));
    }
    if (elements.measurementConvolverTakeRightBtn) {
        elements.measurementConvolverTakeRightBtn.addEventListener('click', () => window.FXRouteMeasurementConvolverEditor.takeMeasurementConvolverToDraft('right'));
    }
    if (elements.measurementConvolverTakeBothBtn) {
        elements.measurementConvolverTakeBothBtn.addEventListener('click', () => window.FXRouteMeasurementConvolverEditor.takeMeasurementConvolverToDraft('both'));
    }
    if (elements.measurementConvolverCreateBtn) {
        elements.measurementConvolverCreateBtn.addEventListener('click', () => { void window.FXRouteMeasurementConvolverEditor.createMeasurementConvolverPresetFromDraft(); });
    }
    if (elements.measurementPeqPresetName) {
        elements.measurementPeqPresetName.addEventListener('input', (event) => {
            const peq = window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState();
            peq.draft.presetName = event.target.value || '';
            peq.draft.nameTouched = true;
            if (elements.measurementPeqCreateBtn) {
                const hasDraft = !!peq.draft.leftBands?.length || !!peq.draft.rightBands?.length;
                elements.measurementPeqCreateBtn.disabled = !hasDraft || !peq.draft.presetName.trim() || peqCreateInFlight;
            }
        });
    }
    if (elements.measurementPeqTakeLeftBtn) {
        elements.measurementPeqTakeLeftBtn.addEventListener('click', () => window.FXRouteMeasurementPeqEditor.takeMeasurementPeqToPreset('left'));
    }
    if (elements.measurementPeqTakeRightBtn) {
        elements.measurementPeqTakeRightBtn.addEventListener('click', () => window.FXRouteMeasurementPeqEditor.takeMeasurementPeqToPreset('right'));
    }
    if (elements.measurementPeqTakeBothBtn) {
        elements.measurementPeqTakeBothBtn.addEventListener('click', () => window.FXRouteMeasurementPeqEditor.takeMeasurementPeqToPreset('both'));
    }
    if (elements.measurementPeqCreateBtn) {
        elements.measurementPeqCreateBtn.addEventListener('click', () => { void window.FXRouteMeasurementPeqEditor.createMeasurementPeqPresetFromDraft(); });
    }
    if (elements.measurementGraph) {
        elements.measurementGraph.addEventListener('pointerdown', handleMeasurementGraphPointerDown);
        elements.measurementGraph.addEventListener('pointermove', handleMeasurementGraphPointerMove);
        elements.measurementGraph.addEventListener('pointerup', handleMeasurementGraphPointerUp);
        elements.measurementGraph.addEventListener('pointercancel', handleMeasurementGraphPointerUp);
        elements.measurementGraph.addEventListener('pointerleave', handleMeasurementGraphPointerLeave);
        elements.measurementGraph.addEventListener('wheel', (...args) => window.FXRouteMeasurementPeqEditor.handleMeasurementPeqGraphWheel(...args), { passive: false });
    }
    const measurementGraphWrap = elements.measurementGraph?.closest('.measurement-graph-wrap');
    if (measurementGraphWrap && typeof ResizeObserver === 'function') {
        measurementGraphResizeObserver = new ResizeObserver(() => MeasurementGraph.scheduleMeasurementGraphRenderForResize());
        measurementGraphResizeObserver.observe(measurementGraphWrap);
    }
    window.addEventListener('resize', () => MeasurementGraph.scheduleMeasurementGraphRenderForResize());
    window.addEventListener('orientationchange', () => MeasurementGraph.scheduleMeasurementGraphRenderForResize());
    document.addEventListener('fullscreenchange', () => MeasurementGraph.scheduleMeasurementGraphRenderForResize());
    window.visualViewport?.addEventListener('resize', () => MeasurementGraph.scheduleMeasurementGraphRenderForResize());
    renderMeasurementPanel();
}


// Track which inputs are currently being edited by the user
const _activeEditing = new Set();


function getSubwooferPreviewSettingsFromState() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.getSubwooferPreviewSettingsFromState();
}

function primeSubwooferPreview() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.primeSubwooferPreview();
}

function requestSubwooferPreviewRedrawFromState() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.requestSubwooferPreviewRedrawFromState();
}

function scheduleSubwooferPreviewDraw(subwoofer) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.scheduleSubwooferPreviewDraw(subwoofer);
}

function formatSubwooferDelayMs(value) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.formatSubwooferDelayMs(value);
}


function subwooferSelectedSide() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.subwooferSelectedSide();
}

function setSubwooferSelectedSide(side) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.setSubwooferSelectedSide(side);
}

function repaintCrossoverGraph() {
    ensureOutputSystemBoxes();
    const canvas = elements.effectsCrossoverGraph;
    if (!canvas || !state.crossover.response) return;
    drawCrossoverResponse(canvas, state.crossover.response.ways, state.crossover.activeWay);
}

function renderSubwooferPanel() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.renderSubwooferPanel();
}

function subCrossoverSlopes(family) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.subCrossoverSlopes(family);
}

// Paint one crossover shape into its Frequency/Type/Slope controls. The Main
// highpass switch is a single global flag, so every visible copy shows it.
function applySubCrossoverShape(frequencyEl, familyEl, slopeEl, shape) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.applySubCrossoverShape(frequencyEl, familyEl, slopeEl, shape);
}

function renderSubwooferCrossover(layout) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.renderSubwooferCrossover(layout);
}

// The Main highpass is one global flag; its per-side copies stay in sync.
function applySubMainHighpass(enabled) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.applySubMainHighpass(enabled);
}

function subMainHighpassEnabled() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.subMainHighpassEnabled();
}

function clearSubwooferActiveEditing() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.clearSubwooferActiveEditing();
}

function getSubwooferPreviewLayout(width, height) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.getSubwooferPreviewLayout(width, height);
}

function drawSubwooferPreview(subwoofer) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.drawSubwooferPreview(subwoofer);
}

function drawCrossoverResponse(canvas, ways, activeRole) {
    // Canvas painter lives in static/crossover_view.js
    // (window.FXRouteCrossoverView). Thin wrapper keeps existing call sites
    // (renderCrossoverTile, repaintCrossoverGraph) unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteCrossoverView) || (typeof globalThis !== 'undefined' && globalThis.FXRouteCrossoverView) || null;
    return mod.drawCrossoverResponse(canvas, ways, activeRole);
}

// Tile status is error-only by design (no Applying/Saved hints): failures
// stay visible, success stays silent.
function setSubwooferFeedback(message, cls = '') {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.setSubwooferFeedback(message, cls);
}


function updateSubwooferDraftFromControls() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.updateSubwooferDraftFromControls();
}

function beginSubwooferSave(pending) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.beginSubwooferSave(pending);
}

// The link checkbox is the one subwoofer control without a blur event, so its
// render guard ends with the save that carried the toggle.
function releaseSubwooferLinkGuard() {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.releaseSubwooferLinkGuard();
}

function createPendingSubwooferSave(mode, settings, signature) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.createPendingSubwooferSave(mode, settings, signature);
}

function cancelPendingSubwooferSave(reason = 'Subwoofer settings save superseded') {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.cancelPendingSubwooferSave(reason);
}

function saveSubwooferDebounced(delayMs) {
    // Subwoofer tile UI lives in static/subwoofer_ui.js
    // (window.FXRouteSubwooferUI). Thin wrapper keeps existing
    // call sites unchanged.
    const mod = (typeof window !== 'undefined' && window.FXRouteSubwooferUI) || (typeof globalThis !== 'undefined' && globalThis.FXRouteSubwooferUI) || null;
    return mod.saveSubwooferDebounced(delayMs);
}


function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    elements.toastContainer.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('remove');
        toast.addEventListener('animationend', () => toast.remove(), { once: true });
    }, 4000);
}
function trackCoverUrl(track) {
    if (!track || track.source !== 'local' || !track.id) return '';
    if (track.cover_available === false) return '';
    if (track.cover_url) return track.cover_url;
    return `/api/tracks/cover/${encodeURIComponent(track.id)}`;
}
function trackCoverInfoUrl(track) {
    if (!track || track.source !== 'local' || !track.id) return '';
    if (track.cover_info_url) return track.cover_info_url;
    return `/api/tracks/cover-info/${encodeURIComponent(track.id)}`;
}
function trackCoverKnownAvailable(track) {
    return track && track.source === 'local' && track.cover_available === true;
}
function playbackArtworkUrl(item) {
    if (!item) return '';
    if (item.artwork_available === false) return '';
    const explicitUrl = item.artwork_url || item.artUrl || item.image || '';
    if (explicitUrl) return explicitUrl;
    return trackCoverUrl(item);
}
function playbackArtworkKnownAvailable(item) {
    if (!item) return false;
    if (item.artwork_available === true) return true;
    if (item.artwork_available === false) return false;
    return trackCoverKnownAvailable(item) || !!(item.artwork_url || item.artUrl || item.image);
}
function streamingArtworkItem(data, source = 'spotify') {
    const artworkUrl = data?.artwork_url || data?.artUrl || '';
    return {
        source: source,
        artwork_available: !!artworkUrl,
        artwork_url: artworkUrl || '',
        artwork_source: artworkUrl ? source : 'none',
    };
}
function updatePlaybackCover(track) {
    if (!elements.playbackCover) return;
    const coverUrl = playbackArtworkKnownAvailable(track) ? playbackArtworkUrl(track) : '';
    if (!coverUrl) {
        elements.playbackCover.removeAttribute('src');
        elements.playbackCover.classList.add('hidden');
        elements.playbackCover.classList.remove('is-ready');
        elements.playbackBar?.classList.remove('has-cover');
        return;
    }
    elements.playbackCover.onerror = function() {
        const fallbackUrl = track?.artwork_fallback_url || '';
        if (fallbackUrl && this.getAttribute('src') !== fallbackUrl) {
            this.src = fallbackUrl;
            return;
        }
        this.onerror = null;
        this.removeAttribute('src');
        this.classList.add('hidden');
        this.classList.remove('is-ready');
        elements.playbackBar?.classList.remove('has-cover');
    };
    elements.playbackCover.onload = function() {
        this.classList.remove('hidden');
        this.classList.add('is-ready');
        elements.playbackBar?.classList.add('has-cover');
    };
    if (elements.playbackCover.getAttribute('src') !== coverUrl) {
        elements.playbackCover.classList.remove('is-ready');
        elements.playbackCover.src = coverUrl;
    } else {
        elements.playbackCover.classList.remove('hidden');
        elements.playbackBar?.classList.add('has-cover');
    }
}
function scheduleNowPlayingCueRemoval(cue, delayMs = 4200) {
    if (nowPlayingCueTimer) clearTimeout(nowPlayingCueTimer);
    nowPlayingCueTimer = setTimeout(() => {
        cue.classList.add('remove');
        cue.addEventListener('animationend', () => cue.remove(), { once: true });
        nowPlayingCueTimer = null;
    }, delayMs);
}
async function revealNowPlayingCoverWhenReady(cue, img, coverUrl, coverInfoUrl = '') {
    if (!coverUrl) return;
    const controller = new AbortController();
    nowPlayingCueCoverAbort = controller;
    const timeout = setTimeout(() => controller.abort(), 2500);
    let objectUrl = '';
    const isExternalCover = (() => {
        try {
            return new URL(coverUrl, window.location.href).origin !== window.location.origin;
        } catch (e) {
            return false;
        }
    })();
    try {
        if (coverInfoUrl) {
            const infoResp = await fetch(coverInfoUrl, { signal: controller.signal, cache: 'no-store' });
            if (!infoResp.ok) return;
            const info = await infoResp.json();
            if (!info.available) return;
        }
        if (isExternalCover) {
            // External covers load directly via <img>; fetch() would hit CORS.
            img.src = coverUrl;
            const abortWait = new Promise((_, reject) => {
                controller.signal.addEventListener('abort', () => {
                    reject(controller.signal.reason || new DOMException('Aborted', 'AbortError'));
                }, { once: true });
            });
            const onAbort = () => {
                // Cancel the pending image request; late load/decode must not win.
                if (img.getAttribute('src') === coverUrl) img.removeAttribute('src');
            };
            controller.signal.addEventListener('abort', onAbort, { once: true });
            try {
                if (img.decode) {
                    await Promise.race([img.decode(), abortWait]);
                } else {
                    await Promise.race([
                        new Promise((resolve, reject) => {
                            img.addEventListener('load', resolve, { once: true });
                            img.addEventListener('error', reject, { once: true });
                        }),
                        abortWait,
                    ]);
                }
            } finally {
                controller.signal.removeEventListener('abort', onAbort);
            }
        } else {
            const resp = await fetch(coverUrl, { signal: controller.signal, cache: 'force-cache' });
            if (!resp.ok) return;
            const blob = await resp.blob();
            objectUrl = URL.createObjectURL(blob);
            img.src = objectUrl;
            if (img.decode) await img.decode();
        }
        if (!document.body.contains(cue) || nowPlayingCueCoverAbort !== controller) return;
        img.classList.add('is-ready');
        cue.classList.add('has-cover');
        scheduleNowPlayingCueRemoval(cue, 3600);
    } catch (e) {
        // Slow/missing covers should not degrade the now-playing cue.
    } finally {
        clearTimeout(timeout);
        if (nowPlayingCueCoverAbort === controller) nowPlayingCueCoverAbort = null;
        if (objectUrl) {
            setTimeout(() => URL.revokeObjectURL(objectUrl), 8000);
        }
    }
}
function showNowPlayingCue(track, message = 'Now playing') {
    if (!track) return;
    if (nowPlayingCueTimer) {
        clearTimeout(nowPlayingCueTimer);
        nowPlayingCueTimer = null;
    }
    if (nowPlayingCueCoverAbort) {
        nowPlayingCueCoverAbort.abort();
        nowPlayingCueCoverAbort = null;
    }
    elements.toastContainer.querySelectorAll('.now-playing-cue').forEach(item => item.remove());
    const cue = document.createElement('div');
    cue.className = 'toast info now-playing-cue';
    const coverUrl = playbackArtworkUrl(track);
    cue.innerHTML = `
        <img class="now-playing-cover" alt="">
        <div class="now-playing-text">
            <div class="now-playing-label">${escapeHtml(message)}</div>
            <div class="now-playing-title">${escapeHtml(track.title || 'Unknown track')}</div>
            <div class="now-playing-meta">${escapeHtml([track.artist, track.album].filter(Boolean).join(' · '))}</div>
        </div>
    `;
    elements.toastContainer.appendChild(cue);
    scheduleNowPlayingCueRemoval(cue, 4200);
    const img = cue.querySelector('.now-playing-cover');
    revealNowPlayingCoverWhenReady(cue, img, coverUrl, playbackArtworkKnownAvailable(track) ? '' : trackCoverInfoUrl(track));
}
// Shared queue-started cue for external streaming providers (Spotify/Qobuz).
// Uses the same showNowPlayingCue path as Local Library/Radio: identical
// content, presentation and duration, only the metadata source differs.
function streamingCueTrack(data, source) {
    if (!data || typeof data !== 'object') return null;
    const artworkUrl = data.artwork_url || data.artUrl || '';
    return {
        title: data.title || '',
        artist: data.artist || '',
        album: data.album || '',
        source,
        artwork_available: !!artworkUrl,
        artwork_url: artworkUrl,
        artwork_source: artworkUrl ? source : 'none',
    };
}
function streamingCueTrackId(data) {
    if (!data || typeof data !== 'object') return '';
    return String(data.trackId || data.trackid || data.id || '');
}
function streamingCueKey(data) {
    if (!data || typeof data !== 'object') return '';
    return [
        streamingCueTrackId(data),
        data.title || '',
        data.artist || '',
        data.album || '',
    ].join('|');
}
function showStreamingQueueStarted(source, data) {
    const track = streamingCueTrack(data, source);
    if (!track || (!track.title && !track.artist && !track.album)) return;
    const queueCount = Number(data?.queue_len || 0);
    showNowPlayingCue(track, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing');
}
// Single cue decision for every provider playback start, whatever observed it
// (FXRoute transport response, provider tab transport, status poll, WS
// broadcast or an out-of-band external start). New track/queue -> exactly one
// cue; resume of the session track -> silent; first snapshot after page load
// or provider switch -> silent. Poll/broadcast refreshes re-observe the same
// track and stay silent, so there is no time-based suppression that could
// swallow a rapid same-track restart.
//
// `known` is the session track of the provider: the track its playback session
// is already on, seeded from the first snapshot seen for that provider and
// advanced by every cue. It is deliberately NOT "the previous snapshot": a
// provider Next while paused publishes the new track while still Paused and
// only the later Playing edge is the real start, so the intermediate Paused
// snapshot must never become the track whose start is treated as a resume.
// (Comparing against it is what keeps Paused-old -> Paused-new -> Playing-new
// cueing, while a plain Paused -> Playing of the same track stays silent.)
function lastPlayingQueueKey(source) {
    const known = window.__lastPlayingQueueKey;
    return known && typeof known === 'object' ? known[source] || '' : '';
}
function recordPlayingQueueKey(source, next) {
    if (!next || typeof next !== 'object') return '';
    const key = `${source}|${streamingCueKey(next)}`;
    if (!window.__lastPlayingQueueKey || typeof window.__lastPlayingQueueKey !== 'object') {
        window.__lastPlayingQueueKey = {};
    }
    window.__lastPlayingQueueKey[source] = key;
    return key;
}
function maybeShowStreamingQueueCue(source, prev, next) {
    if (!next || typeof next !== 'object') return false;
    const previous = prev && typeof prev === 'object' && prev.status ? prev : null;
    if (!lastPlayingQueueKey(source) || !previous) {
        // No session track for this provider yet (page load, provider switch)
        // or no usable previous snapshot: adopt the current track silently.
        // Adopting also from a Paused/Stopped snapshot is what makes the later
        // start of a track selected while paused a real start.
        recordPlayingQueueKey(source, next);
        return false;
    }
    if (next.status !== 'Playing') return false;    // Paused/Stopped never cues
    const key = `${source}|${streamingCueKey(next)}`;
    // Same session track: a Paused/Playing edge is a resume or a poll refresh
    // (silent); only a start after Stopped is a start again.
    if (key === lastPlayingQueueKey(source) && previous.status !== 'Stopped') return false;
    showStreamingQueueStarted(source, next);
    recordPlayingQueueKey(source, next);
    return true;
}
// ---------------------------------------------------------------------------
// Native player (Library/Radio/TIDAL) track-change cue
// ---------------------------------------------------------------------------
// The native player plays exactly one track at a time whatever the source, so a
// single session key covers all three. A radio station counts as the track:
// live metadata changes inside the stream are not track changes.
function nativeTrackCueKey(track) {
    if (!track || typeof track !== 'object') return '';
    const source = track.source || '';
    // Identity first: the explicit play response and the WebSocket frame must
    // resolve to the same key or the same start would cue twice.
    const id = track.id || track.url || '';
    if (id) return `${source}|${id}`;
    return [source, track.title || '', track.artist || ''].join('|');
}
// One cue per real track change of the native player, whatever observed it: the
// response of an explicit play (Library/Radio/TIDAL) or an authoritative
// WebSocket playback frame (queue auto-advance, next/previous). The session
// track is remembered, so a resume of a paused track, a repeated frame or a
// burst of position updates never cues again. Mirrors
// maybeShowStreamingQueueCue so every source behaves identically.
function maybeShowNativeTrackCue(track, message = 'Now playing', previousPlayback = null) {
    const key = nativeTrackCueKey(track);
    if (!key) return false;
    // A restart after the track ended is a start again even though the track is
    // unchanged (same rule the streaming providers use).
    const restartAfterStop = !!(
        previousPlayback
        && (previousPlayback.ended === true || previousPlayback.stopped === true)
    );
    if (key === window.__lastNativeTrackKey && !restartAfterStop) return false;
    window.__lastNativeTrackKey = key;
    showNowPlayingCue(track, message);
    return true;
}
// Adopt the session track without cueing: used when the page (re)connects while
// a track already plays, so attaching to a running player stays silent.
function seedNativeTrackCueKey(track) {
    window.__lastNativeTrackKey = nativeTrackCueKey(track);
}
// A WebSocket playback frame is the authoritative track change signal for the
// native player. Spotify/Qobuz keep their own per-provider decision.
function maybeCueNativePlaybackTrack(data, previousPlayback) {
    const track = data?.current_track;
    if (!track || typeof track !== 'object') return false;
    if (!['local', 'radio', 'tidal'].includes(track.source)) return false;
    if (!data?.playing || data?.ended) return false;
    return maybeShowNativeTrackCue(track, 'Now playing', previousPlayback);
}
// Library actions
function setupLibraryActions() {
    elements.refreshLibraryBtn.addEventListener('click', refreshLibrary);
    if (elements.libraryViewTracksBtn) {
        elements.libraryViewTracksBtn.addEventListener('click', () => setLibraryViewMode('tracks'));
    }
    if (elements.libraryViewFoldersBtn) {
        elements.libraryViewFoldersBtn.addEventListener('click', () => setLibraryViewMode('folders'));
    }
    if (elements.libraryViewFavoritesBtn) {
        elements.libraryViewFavoritesBtn.addEventListener('click', () => setLibraryViewMode('favorites'));
    }
    if (elements.libraryViewAlbumsBtn) {
        elements.libraryViewAlbumsBtn.addEventListener('click', () => setLibraryViewMode('albums'));
    }
    if (elements.libraryViewModeGridBtn) {
        elements.libraryViewModeGridBtn.addEventListener('click', () => setAlbumLayout('grid'));
    }
    if (elements.libraryViewModeListBtn) {
        elements.libraryViewModeListBtn.addEventListener('click', () => setAlbumLayout('list'));
    }
    if (elements.albumDetailBack) {
        elements.albumDetailBack.addEventListener('click', () => closeAlbumDetail());
    }
    if (elements.playlistDetailBack) {
        elements.playlistDetailBack.addEventListener('click', () => closePlaylistDetail());
    }
    if (elements.deletePlaylistBtn) {
        elements.deletePlaylistBtn.addEventListener('click', async () => {
            const detail = state.library.playlistDetail;
            if (!detail || !detail.playlist) return;
            if (!confirm(`Delete playlist "${detail.playlist.name}"?`)) return;
            await deletePlaylistById(detail.playlist.id);
            // deletePlaylistById reloads playlists; close the (now gone) detail.
            if (!state.playlists.some(p => p.id === (detail.playlist && detail.playlist.id))) {
                closePlaylistDetail();
            }
        });
    }
    elements.toggleImportBtn.addEventListener('click', () => {
        const shouldOpen = elements.libraryImportPanel.classList.contains('hidden');
        if (!shouldOpen) {
            closeLibraryImportPanel();
            return;
        }
        const searchWrap = elements.librarySearchInput ? elements.librarySearchInput.closest('.library-search-wrap') : null;
        const selectionToolbar = elements.selectAllTracksBtn ? elements.selectAllTracksBtn.closest('.library-selection-toolbar') : null;
        elements.libraryImportPanel.classList.remove('hidden');
        if (searchWrap) {
            searchWrap.classList.add('hidden');
        }
        if (selectionToolbar) {
            selectionToolbar.classList.add('hidden');
        }
        if (elements.playlistSaveRow) {
            elements.playlistSaveRow.classList.add('hidden');
        }
        clearLibraryImportFeedbackIfIdle();
        resetUploadAreaSelection('upload-track-file');
        elements.toggleImportBtn.textContent = 'Close Import';
        elements.toggleImportBtn.setAttribute('aria-expanded', 'true');
    });
    if (elements.librarySearchInput) {
        updateLibrarySearchPlaceholder();
        elements.librarySearchInput.addEventListener('input', (event) => setLibrarySearchQuery(event.target.value));
        elements.librarySearchInput.addEventListener('search', (event) => setLibrarySearchQuery(event.target.value));
    }
    if (elements.librarySearchClear) {
        elements.librarySearchClear.addEventListener('click', clearLibrarySearch);
    }
    if (window.matchMedia) {
        const searchPlaceholderQuery = window.matchMedia('(max-width: 600px)');
        if (searchPlaceholderQuery.addEventListener) {
            searchPlaceholderQuery.addEventListener('change', updateLibrarySearchPlaceholder);
        } else if (searchPlaceholderQuery.addListener) {
            searchPlaceholderQuery.addListener(updateLibrarySearchPlaceholder);
        }
    }
    if (elements.selectAllTracksBtn) {
        elements.selectAllTracksBtn.addEventListener('click', toggleVisibleTrackSelection);
    }
    if (elements.albumFavoriteToggle) {
        elements.albumFavoriteToggle.addEventListener('click', toggleCurrentAlbumFavorite);
    }
    if (elements.downloadSelectedTracksBtn) {
        elements.downloadSelectedTracksBtn.addEventListener('click', downloadSelectedTracks);
    }
    if (elements.savePlaylistBtn) {
        elements.savePlaylistBtn.addEventListener('click', savePlaylist);
    }
    if (elements.cancelPlaylistSelectionBtn) {
        elements.cancelPlaylistSelectionBtn.addEventListener('click', cancelPlaylistSelection);
    }
    setupUploadArea('upload-track-area', 'upload-track-file', (file) => {
        uploadTrackFile();
    });
    elements.deleteSelectedTracksBtn.addEventListener('click', deleteSelectedTracks);
}
// Seek
function initSeek() {
    if (!elements.seekSlider) return;
    elements.seekSlider.addEventListener('input', seekChange);
    elements.seekSlider.addEventListener('mousedown', seekStart);
    elements.seekSlider.addEventListener('touchstart', seekStart, { passive: true });
    elements.seekSlider.addEventListener('mouseup', seekEnd);
    elements.seekSlider.addEventListener('touchend', seekEnd);
}
function seekStart() {
    if (elements.playbackBar?.classList.contains('progress-readonly')) return;
    seekDragging = true;
    if (isStreamingFooterSource(window.__footerSource)) window.__streamingSeeking = true;
}
function seekEnd() {
    if (elements.playbackBar?.classList.contains('progress-readonly')) return;
    seekDragging = false;
    if (isStreamingFooterSource(window.__footerSource)) {
        window.__streamingSeeking = false;
        const streamingData = streamingFooterData();
        if (streamingData && streamingData.duration) {
            const posSec = (parseInt(elements.seekSlider.value, 10) / 1000) * streamingData.duration;
            if (window.__footerSource === 'qobuz') qobuzSeek(posSec);
            else spotifySeek(posSec);
        }
        return;
    }
    if (seekPendingPos !== null && state.playback.duration > 0) {
        doSeek(seekPendingPos);
        seekPendingPos = null;
    }
}
function seekChange() {
    if (elements.playbackBar?.classList.contains('progress-readonly')) return;
    const pos = parseInt(elements.seekSlider.value, 10) || 0;
    setRangeProgress(elements.seekSlider, pos / 1000);
    if (isStreamingFooterSource(window.__footerSource)) {
        const streamingData = streamingFooterData();
        const duration = streamingData?.duration || 0;
        const current = (pos / 1000) * duration;
        if (elements.seekCurrent) elements.seekCurrent.textContent = formatTime(current);
        return;
    }
    const duration = state.playback.duration || 0;
    const current = (pos / 1000) * duration;
    if (elements.seekCurrent) elements.seekCurrent.textContent = formatTime(current);
    seekPendingPos = current;
}
async function doSeek(seconds) {
    try {
        const resp = await fetch('/api/playback/seek', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ position: seconds }),
        });
        if (!resp.ok) console.debug('Seek result:', await resp.json().catch(() => '??'));
    } catch (e) { /* silent for seek */ }
}
function updateSeekUI() {
    if (!elements.seekSlider || !elements.seekCurrent || !elements.seekDuration) return;
    const currentTrack = state.playback.current_track;
    const isRadio = currentTrack?.source === 'radio';
    const radioMetadata = isRadio ? state.playback.radio_metadata : null;
    const radioDuration = Number(radioMetadata?.duration_seconds);
    const radioProgress = radioMetadata?.progress_seconds === null || radioMetadata?.progress_seconds === undefined
        ? Number.NaN
        : Number(radioMetadata.progress_seconds);
    const radioStartedAt = radioMetadata?.started_at === null || radioMetadata?.started_at === undefined
        ? Number.NaN
        : Number(radioMetadata.started_at);
    const radioTimed = !!(radioMetadata && !radioMetadata.stale && radioDuration > 0
        && (Number.isFinite(radioProgress) || Number.isFinite(radioStartedAt)));
    const duration = radioTimed ? radioDuration : (isRadio ? 0 : Number(state.playback.duration || 0));
    let position = radioTimed && Number.isFinite(radioProgress)
        ? radioProgress
        : (isRadio ? 0 : Number(state.playback.position || 0));
    if (radioTimed && Number.isFinite(radioStartedAt) && state.playback.playing && !state.playback.paused) {
        position = Math.min(duration, Math.max(0, Date.now() / 1000 - radioStartedAt));
    }
    const hasProgress = !!currentTrack && Number.isFinite(duration) && duration > 0;
    setFooterProgressState(hasProgress, radioTimed);
    elements.seekSlider.disabled = hasProgress && radioTimed;
    elements.seekSlider.setAttribute('aria-disabled', hasProgress && radioTimed ? 'true' : 'false');
    if (!hasProgress) {
        elements.seekCurrent.textContent = '0:00';
        elements.seekDuration.textContent = '0:00';
        elements.seekSlider.value = 0;
        setRangeProgress(elements.seekSlider, 0);
        return;
    }
    elements.seekDuration.textContent = formatTime(duration);
    if (!seekDragging) {
        elements.seekCurrent.textContent = formatTime(position);
        if (duration > 0) {
            elements.seekSlider.value = Math.round((position / duration) * 1000);
        } else {
            elements.seekSlider.value = 0;
        }
        setRangeProgress(elements.seekSlider, Number(elements.seekSlider.value || 0) / 1000);
    }
}
// Utilities
// Shared JSON fetch helpers live in static/api.js (window.FXRouteApi).
async function apiFetchJson(url, options = {}) {
    const mod = (typeof window !== 'undefined' && window.FXRouteApi) || (typeof globalThis !== 'undefined' && globalThis.FXRouteApi) || null;
    return mod.apiFetchJson(url, options);
}

function apiPostJson(url, body) {
    const mod = (typeof window !== 'undefined' && window.FXRouteApi) || (typeof globalThis !== 'undefined' && globalThis.FXRouteApi) || null;
    return mod.apiPostJson(url, body);
}

function escapeHtml(text) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.escapeHtml(text);
}

// =========================================================================
// Spotify tab (playerctl / MPRIS)
// =========================================================================

// =========================================================================
// Source-agnostic player control model
// =========================================================================
// State shape: { source, capabilities, status, artist, title, album,
//                artUrl, shuffle, loop, position, duration }
// UI reads capabilities to show/hide controls per source.
// Future sources (library) can adopt the same model without UI redesign.

// =========================================================================
// Spotify source (playerctl / MPRIS)
// =========================================================================
// The Spotify tab renders through the shared streaming shell
// (streaming.js .streaming-shell) and its visibility is owned by streaming.js
// too, so no legacy Spotify tab DOM lives here.
let _spotifyInstalledKnown = null;

let _spotifyPollTimer = null;
let _spotifyCommandInFlight = false;
let _spotifySeekCommitTimer = null;
let _spotifyLastRenderedTrackKey = '';
let _spotifyLastPositionUpdateAt = 0;
let _spotifyTakeoverUntil = 0;
let _localFooterHoldUntil = 0;
let _footerContentFreezeUntil = 0;
let _footerContentFreezeTimer = null;

// ---------------------------------------------------------------------------
// Format seconds → m:ss
// ---------------------------------------------------------------------------
function formatTime(sec) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.formatTime(sec);
}

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------
async function fetchSpotifyStatus() {
    try {
        const resp = await fetch('/api/spotify/status');
        if (!resp.ok) throw new Error('request failed');
        return await resp.json();
    } catch {
        return { available: false, installed: false, source: 'spotify', capabilities: {}, status: 'Stopped', artist: '', title: '', album: '', trackId: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
    }
}

// Footer-owned Qobuz snapshot, mirroring fetchSpotifyStatus: the shared
// footer needs an HTTP resync path because WS broadcasts can be missed
// while a client is backgrounded, and the Qobuz tab poll only feeds the tab.
async function fetchQobuzStatus() {
    try {
        const resp = await fetch('/api/streaming/qobuz/status');
        if (!resp.ok) throw new Error('request failed');
        return await resp.json();
    } catch {
        return { available: false, installed: false, source: 'qobuz', capabilities: {}, status: 'Stopped', artist: '', title: '', album: '' };
    }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function spotifyTrackKey(data) {
    return [
        data?.trackId || data?.trackid || '',
        data?.title || '',
        data?.artist || '',
        data?.album || '',
        data?.artUrl || '',
        Math.round(Number(data?.duration || 0) * 1000),
    ].join('|');
}

function mergeSpotifyState(data) {
    const incoming = data && typeof data === 'object' ? data : {};
    const previous = window.__spotifyLastData && typeof window.__spotifyLastData === 'object'
        ? window.__spotifyLastData
        : {};
    if (!Object.keys(previous).length) return { ...incoming };

    // playerctl can briefly return an incomplete record while MPRIS is
    // updating. Keep the last complete Spotify record during that window;
    // an explicit new track id starts a fresh metadata record.
    const previousTrackId = previous.trackId || previous.trackid || '';
    const incomingTrackId = incoming.trackId || incoming.trackid || '';
    const sameTrack = !incomingTrackId || !previousTrackId || incomingTrackId === previousTrackId;
    if (incoming.available === false && previous.available === true && previous.status !== 'Stopped') {
        return { ...previous };
    }
    if (incoming.status === 'Stopped' && !incoming.title && !incoming.artist && !incoming.album && !incoming.artUrl) {
        return {
            ...incoming,
            title: '',
            artist: '',
            album: '',
            artUrl: '',
            artwork_url: '',
            artwork_available: false,
            artwork_source: 'none',
        };
    }
    if (!sameTrack) return { ...incoming };

    const merged = { ...previous, ...incoming };
    // Backend sets spotifyd_standby only while idle; a playing/paused read
    // omits the key, so a previous standby flag must not linger into playback.
    if (incoming.spotifyd_standby !== true) delete merged.spotifyd_standby;
    const stableFields = [
        'artist', 'title', 'album', 'trackId', 'trackid', 'artUrl',
        'artwork_url', 'artwork_available', 'artwork_source', 'duration',
        'stream_info',
    ];
    for (const field of stableFields) {
        if (incoming[field] === undefined || incoming[field] === null || incoming[field] === '') {
            if (previous[field] !== undefined) merged[field] = previous[field];
        }
    }
    return merged;
}

function syncSpotifySourceOwnership(data) {
    if (!data || !data.available) return;
    window.__spotifyLastData = data;
    reconcileFooterSource();
}

function shouldAdoptSpotifyUpdate(data) {
    if (!data || !data.available) return false;
    const isPlaying = data.status === 'Playing';
    if (isPlaying) return true;
    reconcileFooterSource();
    return window.__footerSource === 'spotify';
}

// Provider tabs are owned by streaming.js, which derives their visibility from
// the discovery payload (installed + enabled). Spotify status arrives far more
// often than discovery, so this path must not write the tab DOM: a write here
// re-shows the tab of a provider the user disabled. Only an installed <->
// uninstalled transition is forwarded, so it lands without a discovery poll.
function syncSpotifyTabAvailability(installed) {
    const available = installed === true;
    if (_spotifyInstalledKnown === available) return;
    _spotifyInstalledKnown = available;
    void window.FXRouteStreaming?.refreshEnabledFlags?.();
}

function handleIncomingQobuzState(data, options = {}) {
    // Thin footer path for Qobuz, mirroring the footer half of
    // handleIncomingSpotifyState. The Qobuz tab itself keeps rendering via
    // streaming.js.
    if (!data) return;
    const { renderFooter = true } = options;
    const previousQobuzData = window.__qobuzLastData && typeof window.__qobuzLastData === 'object'
        ? { ...window.__qobuzLastData }
        : null;
    // Volume-domain guard: GET /api/streaming/qobuz/status returns the raw
    // qbzd engine snapshot (unity-pinned 100%, no source_volume), while WS
    // broadcasts, init and Qobuz actions carry normalized UI state with
    // volume in the master domain plus source_volume for the raw value.
    // A raw engine volume must never slam the shared master slider.
    const normalized = { ...data };
    if (!('source_volume' in normalized)) delete normalized.volume;
    window.__qobuzLastData = normalized;
    reconcileFooterSource();
    if (renderFooter && window.__footerSource === 'qobuz') {
        updateFooterForStreamingOwner(normalized);
    }
    maybeShowStreamingQueueCue('qobuz', previousQobuzData, normalized);
}

function handleIncomingSpotifyState(data, options = {}) {
    if (!data) return;
    const { renderTab = true, renderFooter = true } = options;
    const previousData = window.__spotifyLastData || {};
    const mergedData = mergeSpotifyState(data);
    syncSpotifyTabAvailability(mergedData.installed === true);
    if (mergedData.installed !== true) {
        stopSpotifyPoll();
    }
    const previousTrackKey = spotifyTrackKey(previousData);
    const nextTrackKey = spotifyTrackKey(mergedData);
    const trackChanged = previousTrackKey !== nextTrackKey;

    footerDebug('incoming-spotify-state', {
        payload: {
            title: mergedData?.title || null,
            artist: mergedData?.artist || null,
            status: mergedData?.status || null,
            available: !!mergedData?.available,
        },
        renderTab,
        renderFooter,
        previousTrackKey,
        nextTrackKey,
        trackChanged,
    });

    window.__spotifyLastData = mergedData;
    if (shouldAdoptSpotifyUpdate(mergedData)) {
        syncSpotifySourceOwnership(mergedData);
    }
    reconcileFooterSource();

    if (trackChanged) {
        _spotifyLastRenderedTrackKey = nextTrackKey;
        _spotifyLastPositionUpdateAt = Date.now();
    }

    if (renderFooter && window.__footerSource === 'spotify') {
        updateFooterForStreamingOwner(mergedData);
    }
    if (renderTab) {
        const spotifyTab = document.getElementById('tab-spotify');
        if (spotifyTab && spotifyTab.classList.contains('active')) {
            renderSpotifyTab(mergedData);
        }
    }        maybeShowStreamingQueueCue('spotify', previousData, mergedData);
}

function renderSpotify(data) {
    // The Spotify tab now renders through the shared capability-driven
    // streaming card (window.FXRouteStreaming). Footer ownership and the
    // playerctl transport stay here; only the tab-internal now-playing card
    // moved to the shared component so Spotify/Qobuz/TIDAL render identically.
    if (window.FXRouteStreaming && typeof window.FXRouteStreaming.renderProvider === 'function') {
        window.FXRouteStreaming.renderProvider('spotify', data);
    }
    updateGlobalControlsForSource();
}

function updateGlobalControlsForSource() {
    if (window.__footerSource !== 'spotify') return;
    const data = window.__spotifyLastData;
    if (data) updateFooterForStreamingOwner(data);
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------
function armSpotifyTakeover(ms = 4000) {
    _spotifyTakeoverUntil = Date.now() + ms;
    window.__footerSource = 'spotify';
}

function armLocalFooterHold(ms = 1200) {
    _localFooterHoldUntil = Date.now() + ms;
}

async function forceSpotifyRefreshBurst() {
    const delays = [250, 700, 1400];
    for (const delay of delays) {
        setTimeout(async () => {
            try {
                const fresh = await fetchSpotifyStatus();
                handleIncomingSpotifyState(fresh, { renderTab: true, renderFooter: true });
                reconcileFooterSource();
                if (window.__footerSource === 'spotify') startSpotifyPoll();
            } catch {}
        }, delay);
    }
}

async function qobuzCommand(action) {
    const prev = window.__qobuzLastData && typeof window.__qobuzLastData === 'object'
        ? { ...window.__qobuzLastData }
        : null;
    try {
        const data = await apiPostJson(`/api/streaming/qobuz/${action}`);
        window.__qobuzLastData = data;
        reconcileFooterSource();
        updateFooterForStreamingOwner(data);
        if (action === 'play' || action === 'toggle') {
            maybeShowStreamingQueueCue('qobuz', prev, data);
        }
        return data;
    } catch (e) {
        showToast('Qobuz transport failed', 'error');
        return null;
    }
}

async function qobuzSeek(positionSec) {
    try {
        const data = await apiPostJson('/api/streaming/qobuz/seek', { position: positionSec });
        if (data) {
            window.__qobuzLastData = data;
            if (window.__footerSource === 'qobuz') updateFooterForStreamingOwner(data);
        }
    } catch (e) {
        console.debug('Qobuz seek failed', e);
    }
}

async function spotifyCommand(action) {
    if (_spotifyCommandInFlight) return;
    const interactiveTakeover = ['play', 'toggle', 'next', 'previous'].includes(action);
    if (interactiveTakeover) {
        armSpotifyTakeover();
    }
    const gen = _spotifyPollGeneration;
    _spotifyCommandInFlight = true;
    try {
        const data = await apiPostJson(`/api/spotify/${action}`);
        if (gen !== _spotifyPollGeneration) return;
        handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
        if ((data || {}).status === 'Playing') {
            syncSpotifySourceOwnership(data);
            startSpotifyPoll();
        }
        if (interactiveTakeover) {
            forceSpotifyRefreshBurst();
        }
    } catch (e) {
        console.debug('Spotify transport failed, refreshing state', e);
        const fresh = await fetchSpotifyStatus();
        if (gen !== _spotifyPollGeneration) return;
        handleIncomingSpotifyState(fresh, { renderTab: true, renderFooter: true });
        if ((fresh || {}).status === 'Playing') {
            syncSpotifySourceOwnership(fresh);
            startSpotifyPoll();
        }
        if (interactiveTakeover) {
            forceSpotifyRefreshBurst();
        }
    } finally {
        _spotifyCommandInFlight = false;
    }
}

async function spotifySeek(positionSec) {
    if (_spotifySeekCommitTimer) {
        clearTimeout(_spotifySeekCommitTimer);
        _spotifySeekCommitTimer = null;
    }
    try {
        const data = await apiPostJson('/api/spotify/seek', { position: positionSec });
        if (data) {
            handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
        }
    } catch (e) {
        console.debug('Spotify seek failed', e);
    }
    _spotifySeekCommitTimer = setTimeout(async () => {
        const fresh = await fetchSpotifyStatus();
        handleIncomingSpotifyState(fresh, { renderTab: true, renderFooter: true });
    }, 700);
}

function qobuzIsInstalled(data = window.__qobuzLastData) {
    return data?.installed === true;
}

function shouldPollQobuz() {
    // Mirror shouldPollSpotify, plus the authoritative backend commit: when
    // the backend names qobuz, the footer must resync even if its own source
    // still points elsewhere (stale after a missed broadcast).
    return qobuzIsInstalled() && (window.__visibleTab === 'qobuz' || window.__footerSource === 'qobuz' || getBackendFooterOwner() === 'qobuz');
}

let _qobuzPollTimer = null;
let _qobuzPollGeneration = 0;
let _qobuzPollTimerGeneration = null;

function stopQobuzPoll() {
    if (_qobuzPollTimer) {
        clearInterval(_qobuzPollTimer);
        _qobuzPollTimer = null;
    }
    _qobuzPollTimerGeneration = null;
}

function startQobuzPoll() {
    if (!shouldPollQobuz()) return;
    if (_qobuzPollTimer) {
        if (_qobuzPollTimerGeneration !== _qobuzPollGeneration) stopQobuzPoll();
        else return;
    }
    const gen = ++_qobuzPollGeneration;
    _qobuzPollTimerGeneration = gen;
    _qobuzPollTimer = setInterval(async () => {
        if (document.hidden) return;
        if (!shouldPollQobuz()) {
            _qobuzPollGeneration++;
            stopQobuzPoll();
            return;
        }
        if (gen !== _qobuzPollGeneration) return;
        const data = await fetchQobuzStatus();
        if (gen !== _qobuzPollGeneration) return;
        handleIncomingQobuzState(data, { renderFooter: true });
    }, 2000);
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
function stopSpotifyPoll() {
    if (_spotifyPollTimer) {
        clearInterval(_spotifyPollTimer);
        _spotifyPollTimer = null;
    }
    _spotifyPollTimerGeneration = null;
}

// Guard against stale in-flight poll responses — bump generation when source changes
let _spotifyPollGeneration = 0;
// Generation the running poll timer was started with. Another path may bump
// _spotifyPollGeneration without stopping the timer, which would leave the
// callback no-op'ing forever; startSpotifyPoll() uses this to replace it.
let _spotifyPollTimerGeneration = null;

function startSpotifyPoll() {
    if (!shouldPollSpotify()) return;
    if (_spotifyPollTimer) {
        // The timer is running but its generation was invalidated elsewhere —
        // restart it instead of leaving a poller that can never update state.
        if (_spotifyPollTimerGeneration !== _spotifyPollGeneration) stopSpotifyPoll();
        else return;
    }
    const gen = ++_spotifyPollGeneration;
    _spotifyPollTimerGeneration = gen;
    _spotifyPollTimer = setInterval(async () => {
        if (document.hidden) return;
        if (!shouldPollSpotify()) {
            _spotifyPollGeneration++;
            stopSpotifyPoll();
            return;
        }
        if (gen !== _spotifyPollGeneration) return;
        const data = await fetchSpotifyStatus();
        if (gen !== _spotifyPollGeneration) return;
        handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
    }, 1000);
}

// Footer update for an external streaming owner (Spotify or Qobuz) — single
// source of truth for the shared normalized streaming state shape.
function updateFooterForStreamingOwner(data) {
    if (!isStreamingFooterSource(window.__footerSource)) return;
    if (footerContentFreezeActive()) return;
    const hasMedia = !!(data?.available && (data.title || data.artist || data.album || data.status !== 'Stopped'));
    renderTrackFavoriteButton(null);
    updatePlaybackCover(hasMedia ? streamingArtworkItem(data, window.__footerSource) : null);
    elements.playbackBar?.classList.toggle('has-media', hasMedia);
    elements.playbackBar?.classList.toggle('is-playing', hasMedia && data.status === 'Playing');
    elements.playbackBar?.classList.toggle('is-paused', hasMedia && data.status === 'Paused');
    if (typeof data.volume === 'number') {
        applyRemoteVolume(data.volume);
        if (!volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
            renderVolumeControlsFromActualVolume(state.playback.volume);
        }
    }
    if (!hasMedia) {
        renderFooterModeButtons();
        setFooterProgressState(false);
        if (elements.btnPlayPause) {
            elements.btnPlayPause.disabled = true;
            elements.btnPlayPause.textContent = '▶';
        }
        if (elements.btnPrevious) elements.btnPrevious.classList.add('hidden');
        if (elements.btnNext) elements.btnNext.classList.add('hidden');
        if (elements.btnClearQueue) elements.btnClearQueue.classList.add('hidden');
        if (elements.queueStatus) elements.queueStatus.classList.add('hidden');
        if (elements.samplerateStatus) elements.samplerateStatus.classList.add('hidden');
        renderPeakWarningBadge(false);
        const titleEl = document.getElementById('track-title');
        const artistEl = document.getElementById('track-artist');
        const scTitle = document.getElementById('sc-title');
        const scArtist = document.getElementById('sc-artist');
        const scAlbum = document.getElementById('sc-album');
        if (titleEl) {
            titleEl.textContent = 'Not playing';
            titleEl.classList.add('placeholder');
            titleEl.style.display = '';
        }
        if (artistEl) {
            artistEl.textContent = '';
            artistEl.style.display = '';
        }
        if (scTitle) scTitle.textContent = '';
        if (scArtist) scArtist.textContent = '';
        if (scAlbum) {
            scAlbum.textContent = '';
            scAlbum.style.display = 'none';
        }
        return;
    }
    if (elements.btnPlayPause) {
        elements.btnPlayPause.disabled = false;
        elements.btnPlayPause.textContent = data.status === 'Playing' ? '⏸' : '▶';
    }
    if (elements.btnPrevious) { elements.btnPrevious.classList.remove('hidden'); elements.btnPrevious.disabled = false; }
    if (elements.btnNext) { elements.btnNext.classList.remove('hidden'); elements.btnNext.disabled = false; }
    if (elements.btnClearQueue) { elements.btnClearQueue.classList.add('hidden'); }
    if (elements.queueStatus) { elements.queueStatus.classList.add('hidden'); }
    const titleEl = document.getElementById('track-title');
    const artistEl = document.getElementById('track-artist');
    if (titleEl) {
        titleEl.textContent = '';
        titleEl.classList.add('placeholder');
        titleEl.style.display = 'none';
    }
    if (artistEl) {
        artistEl.textContent = '';
        artistEl.style.display = 'none';
    }
    const scTitle = document.getElementById('sc-title');
    const scArtist = document.getElementById('sc-artist');
    const scAlbum = document.getElementById('sc-album');
    if (scTitle) scTitle.textContent = data.title || '';
    if (scArtist) scArtist.textContent = data.artist || '';
    if (scAlbum) {
        scAlbum.textContent = data.album || '';
        scAlbum.style.display = data.album ? '' : 'none';
    }
    if (elements.seekSlider && elements.seekCurrent && elements.seekDuration) {
        const pos = Number(data.position || 0);
        const dur = Number(data.duration || 0);
        const hasProgress = Number.isFinite(dur) && dur > 0;
        setFooterProgressState(hasProgress, false);
        elements.seekSlider.disabled = false;
        elements.seekSlider.setAttribute('aria-disabled', 'false');
        if (!hasProgress) {
            elements.seekCurrent.textContent = '0:00';
            elements.seekDuration.textContent = '0:00';
            elements.seekSlider.value = 0;
            setRangeProgress(elements.seekSlider, 0);
        } else if (!window.__streamingSeeking) {
            elements.seekCurrent.textContent = formatTime(pos);
            elements.seekDuration.textContent = formatTime(dur);
            elements.seekSlider.value = Math.round((pos / dur) * 1000);
            setRangeProgress(elements.seekSlider, Number(elements.seekSlider.value || 0) / 1000);
        }
    }
    if (elements.samplerateStatus) {
        // Shared library/radio meta-tag renderer: Qobuz contributes its real
        // stream facts, Spotify only the resolved rate (no invented format).
        // No UI-side caching: the backend keeps the track's stream facts
        // complete across transient gaps, so the payload is authoritative.
        footerDebug('streaming-footer-meta', {
            owner: window.__footerSource,
            source: data?.source || null,
            trackId: data?.trackId || null,
            audio_format: data?.audio_format ?? null,
            bit_depth: data?.bit_depth ?? null,
            bitrate: data?.bitrate ?? data?.bitrate_kbps ?? null,
            sample_rate: data?.sample_rate ?? null,
        });
        const samplerateLine = formatStreamingMetaLine(data);
        elements.samplerateStatus.textContent = samplerateLine;
        elements.samplerateStatus.classList.toggle('hidden', !samplerateLine);
    }
    renderPeakWarningBadge(data.status === 'Playing');
    renderFooterModeButtons();
}

// Spotify tab internal UI (cover, controls inside the tab)
function renderSpotifyTab(data) {
    renderSpotify(data);
}

async function initSpotify() {
    const data = await fetchSpotifyStatus();
    handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
    if (shouldPollSpotify()) {
        startSpotifyPoll();
    } else {
        stopSpotifyPoll();
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSpotify);
} else {
    initSpotify();
}
