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
const LibraryUI = window.FXRouteLibraryUI || {};
const PlaybackCore = window.FXRoutePlaybackCore || {};
const PlaybackUI = window.FXRoutePlaybackUI || {};
const StreamingRuntime = window.FXRouteStreamingRuntime || {};
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
    setMeasurementGraphView,
    escapeHtml,
    sleep,
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
    renderLibraryView: () => LibraryUI.renderLibraryView(),
    fetchLibraryStatus: () => LibraryUI.fetchLibraryStatus(),
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
    setupUploadArea: (areaId, fileInputId, onFile) => LibraryUI.setupUploadArea(areaId, fileInputId, onFile),
    getActiveEditing: () => _activeEditing,
    isPeqCreateInFlight: () => peqCreateInFlight,
    setPeqCreateInFlight: (active) => { peqCreateInFlight = active; },
});

// Library module (static/library_ui.js): fetch/filter/render, detail views,
// selection, search, upload/download and playlist/library CRUD incl. the row
// favorites. Footer favorite, playback/transport callbacks and the generic
// ui-helper shims stay in app.js behind explicit callbacks; the favorite and
// library-mode request flags remain shared between both sides.
window.FXRouteLibraryUI?.init({
    getState: () => state,
    getElements: () => elements,
    showToast,
    escapeHtml: (...args) => escapeHtml(...args),
    formatTime: (...args) => formatTime(...args),
    formatTransitionErrorDetail: (...args) => formatTransitionErrorDetail(...args),
    getDownloadFilenameFromResponse: (...args) => getDownloadFilenameFromResponse(...args),
    triggerBlobDownload: (...args) => triggerBlobDownload(...args),
    isPageHidden: () => PlaybackCore.isPageHidden(),
    mergePlaybackState: (...args) => PlaybackCore.mergePlaybackState(...args),
    playLocal: (...args) => PlaybackCore.playLocal(...args),
    renderFooterModeButtons: () => PlaybackUI.renderFooterModeButtons(),
    syncLibraryStateFromPlaybackContext: (...args) => PlaybackCore.syncLibraryStateFromPlaybackContext(...args),
    updatePlaybackUI: (...args) => PlaybackUI.updatePlaybackUI(...args),
    favoriteHeartSvg: () => favoriteHeartSvg(),
    artworkPlaceholderUrl: () => artworkPlaceholderUrl(),
    albumArtFallbackSvg: () => albumArtFallbackSvg(),
    isLibraryModeRequestInFlight: () => libraryModeRequestInFlight,
    setLibraryModeRequestInFlight: (active) => { libraryModeRequestInFlight = active; },
    isTidalFavoriteRequestInFlight: () => tidalFavoriteRequestInFlight,
});
// Playback core module (static/playback_core.js): native transport, footer
// ownership/reconcile (single owner of window.__footerSource decisions),
// volume, polling/metadata and queue commit paths. Rendering and gestures
// live in static/playback_ui.js; the Spotify/Qobuz provider runtime stays in
// app.js until the streaming-runtime extraction and reaches the core through
// explicit callbacks.
window.FXRoutePlaybackCore?.init({
    getState: () => state,
    getElements: () => elements,
    fetchFn: (...args) => fetch(...args),
    showToast,
    formatTransitionErrorDetail,
    setRangeProgress: (...args) => setRangeProgress(...args),
    nonAppSourceModeActive: (...args) => nonAppSourceModeActive(...args),
    updateLiveBanner: (...args) => updateLiveBanner(...args),
    renderSettingsPanel: () => renderSettingsPanel(),
    fetchAudioOutputOverview: (...args) => fetchAudioOutputOverview(...args),
    fetchAudioSourceOverview: (...args) => fetchAudioSourceOverview(...args),
    fetchMeasurements: (...args) => fetchMeasurements(...args),
    fetchProviderAdmin: (...args) => fetchProviderAdmin(...args),
    requestSubwooferPreviewRedrawFromState: (...args) => requestSubwooferPreviewRedrawFromState(...args),
    spotifyCommand: (...args) => StreamingRuntime.spotifyCommand(...args),
    qobuzCommand: (...args) => StreamingRuntime.qobuzCommand(...args),
    spotifySeek: (...args) => StreamingRuntime.spotifySeek(...args),
    qobuzSeek: (...args) => StreamingRuntime.qobuzSeek(...args),
    startSpotifyPoll: (...args) => StreamingRuntime.startSpotifyPoll(...args),
    stopSpotifyPoll: (...args) => StreamingRuntime.stopSpotifyPoll(...args),
    startQobuzPoll: (...args) => StreamingRuntime.startQobuzPoll(...args),
    stopQobuzPoll: (...args) => StreamingRuntime.stopQobuzPoll(...args),
    fetchSpotifyStatus: (...args) => StreamingRuntime.fetchSpotifyStatus(...args),
    fetchQobuzStatus: (...args) => StreamingRuntime.fetchQobuzStatus(...args),
    handleIncomingSpotifyState: (...args) => StreamingRuntime.handleIncomingSpotifyState(...args),
    handleIncomingQobuzState: (...args) => StreamingRuntime.handleIncomingQobuzState(...args),
    shouldPollQobuz: (...args) => StreamingRuntime.shouldPollQobuz(...args),
    bumpSpotifyPollGeneration: () => StreamingRuntime.bumpSpotifyPollGeneration(),
    bumpQobuzPollGeneration: () => StreamingRuntime.bumpQobuzPollGeneration(),
    claimWsSyncGeneration: () => ++wsReconnectSyncGeneration,
    isWsSyncGenerationCurrent: (gen) => gen === wsReconnectSyncGeneration,
    renderLibraryModeButtons: (...args) => LibraryUI.renderLibraryModeButtons(...args),
    fetchTracks: (...args) => LibraryUI.fetchTracks(...args),
    fetchPlaylists: (...args) => LibraryUI.fetchPlaylists(...args),
    fetchDownloadStatus: (...args) => LibraryUI.fetchDownloadStatus(...args),
    fetchEffects: (...args) => EffectsUI.fetchEffects(...args),
    fetchStations: (...args) => radioModule.fetchStations(...args),
    updatePlaybackUI: (...args) => PlaybackUI.updatePlaybackUI(...args),
    updateSeekUI: (...args) => PlaybackUI.updateSeekUI(...args),
    showVolumeDisplayTemporarily: (...args) => PlaybackUI.showVolumeDisplayTemporarily(...args),
    maybeShowNativeTrackCue: (...args) => PlaybackUI.maybeShowNativeTrackCue(...args),
    seedNativeTrackCueKey: (...args) => PlaybackUI.seedNativeTrackCueKey(...args),
    coverQueuePlayTarget: (...args) => PlaybackUI.coverQueuePlayTarget(...args),
    renderSamplerateUI: (...args) => PlaybackUI.renderSamplerateUI(...args),
});
// Playback UI module (static/playback_ui.js): footer layout/render, meter,
// queue UI, seek UI, cover detail, artwork, track cues, footer favorites and
// the streaming-owner footer renderer. Ownership and transport resolve
// through the core module behind explicit callbacks; the provider runtime
// stays in app.js.
window.FXRoutePlaybackUI?.init({
    getState: () => state,
    getElements: () => elements,
    fetchFn: (...args) => fetch(...args),
    showToast,
    escapeHtml: (...args) => escapeHtml(...args),
    formatTime: (...args) => formatTime(...args),
    formatRateKhz: (...args) => formatRateKhz(...args),
    setRangeProgress: (...args) => setRangeProgress(...args),
    formatTransitionErrorDetail: (...args) => formatTransitionErrorDetail(...args),
    nonAppSourceModeActive: (...args) => nonAppSourceModeActive(...args),
    isFooterSignalActive: (...args) => isFooterSignalActive(...args),
    renderSourceModeFooter: (...args) => renderSourceModeFooter(...args),
    buildSourceSwitcherEntries: (...args) => buildSourceSwitcherEntries(...args),
    activateSourceSwitcherEntry: (...args) => activateSourceSwitcherEntry(...args),
    stepSourceSwitcher: (...args) => stepSourceSwitcher(...args),
    spotifyCommand: (...args) => StreamingRuntime.spotifyCommand(...args),
    qobuzCommand: (...args) => StreamingRuntime.qobuzCommand(...args),
    spotifySeek: (...args) => StreamingRuntime.spotifySeek(...args),
    qobuzSeek: (...args) => StreamingRuntime.qobuzSeek(...args),
    shouldPollQobuz: (...args) => StreamingRuntime.shouldPollQobuz(...args),
    startQobuzPoll: (...args) => StreamingRuntime.startQobuzPoll(...args),
    stopQobuzPoll: (...args) => StreamingRuntime.stopQobuzPoll(...args),
    startSpotifyPoll: (...args) => StreamingRuntime.startSpotifyPoll(...args),
    stopSpotifyPoll: (...args) => StreamingRuntime.stopSpotifyPoll(...args),
    bumpSpotifyPollGeneration: () => StreamingRuntime.bumpSpotifyPollGeneration(),
    bumpQobuzPollGeneration: () => StreamingRuntime.bumpQobuzPollGeneration(),
    isLibraryModeRequestInFlight: () => libraryModeRequestInFlight,
    isTidalFavoriteRequestInFlight: () => tidalFavoriteRequestInFlight,
    setTidalFavoriteRequestInFlight: (active) => { tidalFavoriteRequestInFlight = active; },
    isSpotifyTransportInFlight: () => StreamingRuntime.isSpotifyTransportInFlight(),
    renderLibraryModeButtons: (...args) => LibraryUI.renderLibraryModeButtons(...args),
    toggleLibraryShuffle: (...args) => LibraryUI.toggleLibraryShuffle(...args),
    toggleLibraryLoop: (...args) => LibraryUI.toggleLibraryLoop(...args),
    renderTrackFavoriteButton: (...args) => LibraryUI.renderTrackFavoriteButton(...args),
    toggleTrackFavoriteById: (...args) => LibraryUI.toggleTrackFavoriteById(...args),
    isStreamingFooterSource: (...args) => PlaybackCore.isStreamingFooterSource(...args),
    streamingFooterData: (...args) => PlaybackCore.streamingFooterData(...args),
    getBackendFooterOwner: (...args) => PlaybackCore.getBackendFooterOwner(...args),
    getEffectivePlaybackControlSource: (...args) => PlaybackCore.getEffectivePlaybackControlSource(...args),
    reconcileFooterSource: (...args) => PlaybackCore.reconcileFooterSource(...args),
    footerDebug: (...args) => PlaybackCore.footerDebug(...args),
    footerContentFreezeActive: (...args) => PlaybackCore.footerContentFreezeActive(...args),
    footerSingleTrackStartLockActive: (...args) => PlaybackCore.footerSingleTrackStartLockActive(...args),
    shouldPollSpotify: (...args) => PlaybackCore.shouldPollSpotify(...args),
    applyRemoteVolume: (...args) => PlaybackCore.applyRemoteVolume(...args),
    renderVolumeControlsFromActualVolume: (...args) => PlaybackCore.renderVolumeControlsFromActualVolume(...args),
    actualVolumeToSliderValue: (...args) => PlaybackCore.actualVolumeToSliderValue(...args),
    sliderVolumeToActualVolume: (...args) => PlaybackCore.sliderVolumeToActualVolume(...args),
    queueVolumeSend: (...args) => PlaybackCore.queueVolumeSend(...args),
    doSeek: (...args) => PlaybackCore.doSeek(...args),
    playCoverQueueIndex: (...args) => PlaybackCore.playCoverQueueIndex(...args),
    getLastRadioTrack: (...args) => PlaybackCore.getLastRadioTrack(...args),
    startPlaybackPositionPoll: (...args) => PlaybackCore.startPlaybackPositionPoll(...args),
    stopPlaybackPositionPoll: (...args) => PlaybackCore.stopPlaybackPositionPoll(...args),
    globalTogglePlayback: (...args) => PlaybackCore.globalTogglePlayback(...args),
    globalPrevious: (...args) => PlaybackCore.globalPrevious(...args),
    globalNext: (...args) => PlaybackCore.globalNext(...args),
    clearQueue: (...args) => PlaybackCore.clearQueue(...args),
    handleVolumeChange: (...args) => PlaybackCore.handleVolumeChange(...args),
    isPlaybackActionInFlight: () => PlaybackCore.isPlaybackActionInFlight(),
    getPendingOptimisticTrack: () => PlaybackCore.getPendingOptimisticTrack(),
    setLibraryModeSyncArmed: (active) => PlaybackCore.setLibraryModeSyncArmed(active),
    isVolumeGestureActive: () => PlaybackCore.isVolumeGestureActive(),
    setVolumeGestureActive: (active) => PlaybackCore.setVolumeGestureActive(active),
    isVolumeRequestInFlight: () => PlaybackCore.isVolumeRequestInFlight(),
    getPendingVolume: () => PlaybackCore.getPendingVolume(),
});
// Streaming runtime module (static/streaming_runtime.js): Spotify/Qobuz
// status fetch, state merge, incoming-state processing, transport commands,
// polling with poll generations and the provider-side footer/ownership sync.
// Ownership stays in playback_core.js, rendering in playback_ui.js; the
// provider tabs/browse UI in streaming.js is wired below through the same
// command entry points.
window.FXRouteStreamingRuntime?.init({
    fetchFn: (...args) => fetch(...args),
    apiPostJson: (...args) => apiPostJson(...args),
    showToast,
    reconcileFooterSource: (...args) => PlaybackCore.reconcileFooterSource(...args),
    footerDebug: (...args) => PlaybackCore.footerDebug(...args),
    getBackendFooterOwner: (...args) => PlaybackCore.getBackendFooterOwner(...args),
    shouldPollSpotify: (...args) => PlaybackCore.shouldPollSpotify(...args),
    armSpotifyTakeover: (...args) => PlaybackCore.armSpotifyTakeover(...args),
    updateFooterForStreamingOwner: (...args) => PlaybackUI.updateFooterForStreamingOwner(...args),
    maybeShowStreamingQueueCue: (...args) => PlaybackUI.maybeShowStreamingQueueCue(...args),
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
        albumLayout: LibraryUI.readStoredViewMode('library-albums'),
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
let tidalFavoriteRequestInFlight = false;
let peqCreateInFlight = false;
let convolverCreateInFlight = false;
let libraryModeRequestInFlight = false;
let settingsStatusPollTimer = null;
let settingsOutputScanOnFocusDone = false;
let measurementInputScanOnFocusDone = false;
let measurementGraphResizeObserver = null;
let measurementGraphPointerId = null;
let measurementWindowHeartbeatTimer = null;
// Seek - globals
const MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX = 14;
const MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX = 24;
const MEASUREMENT_PEQ_TOUCH_CREATE_COOLDOWN_MS = 350;
const SPOTIFY_POLL_INTERVAL_MS = 1000;
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
    try { PlaybackUI.setupPlaybackControls(); } catch(e) { console.error('setupPlaybackControls crashed:', e); }
    try { PlaybackUI.initPlaybackFooterLayout(); } catch(e) { console.error('initPlaybackFooterLayout crashed:', e); }
    try { setupSettingsActions(); } catch(e) { console.error('setupSettingsActions crashed:', e); }
    try { PlaybackUI.initSeek(); } catch(e) { console.error('initSeek crashed:', e); }
    try {
        radioModule = window.FXRouteRadio.init({
            getStations: () => state.stations,
            setStations: stations => { state.stations = stations; },
            playStation: stationId => PlaybackCore.playRadio(stationId),
            showToast,
            escapeHtml,
            favoriteHeartSvg,
            highlightActiveTrack: (...args) => PlaybackUI.highlightActiveTrack(...args),
            extractDroppedUrl: LibraryUI.extractDroppedUrl,
        });
    } catch(e) { console.error('radio module initialization crashed:', e); }
    try {
        window.FXRouteStreaming.init({
            showToast,
            maybeShowNativeTrackCue: (...args) => PlaybackUI.maybeShowNativeTrackCue(...args),
            maybeShowStreamingQueueCue: (...args) => PlaybackUI.maybeShowStreamingQueueCue(...args),
            escapeHtml,
            favoriteHeartSvg,
            formatTime,
            artworkPlaceholderUrl,
            formatTransitionErrorDetail,
            trackRowHtml: LibraryUI.detailTrackRowHtml,
            factsHtml: LibraryUI.detailFactsHtml,
            aboutHtml: LibraryUI.detailAboutHtml,
            spotifyCommand: (...args) => StreamingRuntime.spotifyCommand(...args),
            spotifySeek: (...args) => StreamingRuntime.spotifySeek(...args),
            openQobuzLogin: () => void beginQobuzLogin(),
            openTidalLogin: () => void beginTidalLogin(),
        });
    } catch(e) { console.error('streaming module initialization crashed:', e); }
    try { LibraryUI.setupLibraryActions(); } catch(e) { console.error('setupLibraryActions crashed:', e); }
    try { LibraryUI.setupDownloadActions(); } catch(e) { console.error('setupDownloadActions crashed:', e); }
    try { EffectsUI.setupEffectsActions(); } catch(e) { console.error('EffectsUI.setupEffectsActions crashed:', e); }
    try { setupMeasurementActions(); } catch(e) { console.error('setupMeasurementActions crashed:', e); }
    try { MeasurementFlows.setupHybridMeasurementWizard(); } catch(e) { console.error('setupHybridMeasurementWizard crashed:', e); }
    try { primeSubwooferPreview(); } catch(e) { console.error('primeSubwooferPreview crashed:', e); }
    try { window.addEventListener('load', requestSubwooferPreviewRedrawFromState, { once: true }); } catch(e) { console.error('subwoofer preview load hook crashed:', e); }
    try { PlaybackCore.fetchInitialData(); } catch(e) { console.error('fetchInitialData crashed:', e); }
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
        PlaybackCore.stopMetadataPolling();
        PlaybackCore.startPeakStatusPolling();
        void PlaybackCore.resyncPlaybackAfterReconnect();
    };
    socket.onclose = (event) => {
        if (ws === socket) ws = null;
        console.log('WebSocket disconnected', { code: event.code, reason: event.reason, wasClean: event.wasClean, serial });
        if (serial !== wsConnectSerial) return;
        state.wsConnected = false;
        scheduleOfflineIndicator();
        PlaybackCore.stopPeakStatusPolling();
        PlaybackCore.startMetadataPolling();
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
function handleWebSocketMessage(msg) {
    const { type, data } = msg;
    switch (type) {
        case 'init':
            // Initial state
            if (data.player) {
                PlaybackCore.mergePlaybackState(data.player.state);
                // Page load: whatever already plays is the session track, not a
                // track change, so attaching never cues.
                PlaybackUI.seedNativeTrackCueKey(data.player.state.current_track);
                PlaybackCore.syncFooterOwnershipFromPlayback(data.player.state);
                PlaybackCore.syncLibraryStateFromPlaybackContext(true);
                PlaybackUI.updatePlaybackUI();
            }
            if (data.library) {
                state.library.tracks = [];
                LibraryUI.renderLibraryView();
            }
            if (data.stations) {
                radioModule.setStations(data.stations);
            }
            if (data.catalog) {
                radioModule.setCatalogStations(data.catalog);
            }
            if (data.spotify) {
                StreamingRuntime.handleIncomingSpotifyState(data.spotify, { renderTab: true, renderFooter: true });
            }
            if (data.qobuz) {
                StreamingRuntime.handleIncomingQobuzState(data.qobuz, { renderFooter: true });
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
            PlaybackCore.footerDebug('ws-playback', {
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
            PlaybackCore.mergePlaybackState(data);
            PlaybackUI.maybeCueNativePlaybackTrack(data, previousNativePlayback);
            const clearFooterSingleTrackLockAfterSync = PlaybackCore.footerSingleTrackStartLockSatisfied(state.playback);
            PlaybackCore.syncFooterOwnershipFromPlayback(data);
            if (clearFooterSingleTrackLockAfterSync) {
                PlaybackCore.clearPendingFooterSingleTrackStart();
            }
            PlaybackCore.syncLibraryStateFromPlaybackContext();
            // Reset action guard so this client doesn't block its own UI from server state.
            PlaybackCore.setPlaybackActionInFlight(false);
            PlaybackUI.updatePlaybackUI();
            window.FXRouteStreaming?.notifyPlayback(data);
            if (data?.current_track?.source === 'local' && nextSamplerateSignature !== previousSamplerateSignature) {
                PlaybackCore.triggerSamplerateBurstPolling();
            }
            lastSampleratePlaybackSignature = nextSamplerateSignature;
            break;
        }
        case 'spotify':
            PlaybackCore.footerDebug('ws-spotify', {
                payload: {
                    title: data?.title || null,
                    artist: data?.artist || null,
                    status: data?.status || null,
                    available: !!data?.available,
                },
            });
            StreamingRuntime.handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
            if (data && data.available && (data.status === 'Playing' || data.status === 'Paused' || data.title)) {
                PlaybackCore.reconcileFooterSource();
                if (window.__footerSource === 'spotify') {
                    StreamingRuntime.startSpotifyPoll();
                }
            }
            break;
        case 'qobuz':
            StreamingRuntime.handleIncomingQobuzState(data, { renderFooter: true });
            if (data && data.available && (data.status === 'Playing' || data.status === 'Paused' || data.title)) {
                PlaybackCore.reconcileFooterSource();
                if (window.__footerSource === 'qobuz') {
                    StreamingRuntime.startQobuzPoll();
                }
            }
            break;
        case 'playback_peak_warning':
            state.playback.output_peak_warning = data || state.playback.output_peak_warning;
            PlaybackUI.renderPeakWarningBadge();
            break;
        case 'download':
            state.download = data;
            LibraryUI.updateDownloadUI();
            LibraryUI.handleDownloadStatusTransition(data);
            if (['starting', 'downloading'].includes(data.status)) {
                LibraryUI.startDownloadStatusPolling();
            } else {
                LibraryUI.stopDownloadStatusPolling();
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
            LibraryUI.refreshLibrary();
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
    if (PlaybackCore.isStreamingFooterSource(window.__footerSource)) {
        return PlaybackCore.streamingFooterData()?.status === 'Playing';
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
    // PlaybackCore.reconcileFooterSource() already forces 'local' in these modes; this
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
    PlaybackUI.setFooterProgressState(false);
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
        PlaybackUI.renderSamplerateUI();
        // A rate change can switch the channel inventory server-side; pull
        // the fresh output overview so channel count, tier note and routing
        // matrix update without reopening settings.
        await fetchAudioOutputOverview();
        PlaybackCore.triggerSamplerateBurstPolling();
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


function switchTab(tabId) {
    LibraryUI.closeLibraryImportPanel();
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
    PlaybackUI.highlightActiveTrack();
    if (tabId === 'spotify') {
        const d = window.__spotifyLastData;
        if (d) StreamingRuntime.renderSpotify(d);
        StreamingRuntime.startSpotifyPoll();
        void StreamingRuntime.fetchSpotifyStatus().then(data => {
            StreamingRuntime.handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
        }).catch(() => {});
    } else if (window.__footerSource !== 'spotify') {
        StreamingRuntime.stopSpotifyPoll();
    }
    if (tabId === 'qobuz') {
        StreamingRuntime.startQobuzPoll();
        void StreamingRuntime.fetchQobuzStatus().then(data => {
            StreamingRuntime.handleIncomingQobuzState(data, { renderFooter: true });
        }).catch(() => {});
    } else if (window.__footerSource !== 'qobuz' && window.__visibleTab !== 'qobuz' && PlaybackCore.getBackendFooterOwner() !== 'qobuz') {
        StreamingRuntime.stopQobuzPoll();
    }
}


function setRangeProgress(input, fraction) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.setRangeProgress(input, fraction);
}


// Metadata polling for radio ICY tags
let lastSampleratePlaybackSignature = null;

// Favorite hearts render as one inline SVG instead of the Unicode hearts
// (U+2665 / U+2661). In some browser/OS combinations those fall back to a
// colour-emoji font, which paints an active heart red no matter what the
// button's CSS colour says. The SVG is painted from currentColor, so the
// existing muted / accent button states stay the single source of truth.
function favoriteHeartSvg() {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.favoriteHeartSvg();
}


// Last valid VU level: a single missing/invalid sample (stale poll,
// dropped WS frame, monitor rearm gap) must not blank the meter. The cache
// holds only the slow VU level, never the fast peak flags.


window.__fxDebugFooter = localStorage.getItem('fx-debug-footer') === '1';


// Signature of the last rendered queue list. Rebuilding the <ol> on every
// status poll would replace the row under the cursor and drop its :hover
// state (visible flicker) and keyboard focus, so only rebuild on change.


    LibraryUI.updateLibrarySearchPlaceholder();
// ── Albums ──────────────────────────────────────────────────────


// ── Playlist detail ────────────────────────────────────────────


function artworkPlaceholderUrl() {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.artworkPlaceholderUrl();
}

function albumArtFallbackSvg() {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    if (mod && typeof mod.albumArtFallbackSvg === 'function') return mod.albumArtFallbackSvg();
    return artworkPlaceholderUrl();
}


function getDownloadFilenameFromResponse(resp, fallbackName = 'download') {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.getDownloadFilenameFromResponse(resp, fallbackName);
}
function triggerBlobDownload(blob, filename) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.triggerBlobDownload(blob, filename);
}


/* Collapsed-card preview: compact read-only band list from the draft. */


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

function setMeasurementGraphView(view) {
    state.measurement.measurementView = view === 'ir' ? 'ir' : 'freq';
    window.FXRouteMeasurementPeqEditor.ensureMeasurementPeqState().dragFilterId = null;
    window.FXRouteMeasurementCalibration.ensureCustomHouseCurveState().dragPointId = null;
    window.FXRouteMeasurementConvolverEditor.ensureMeasurementConvolverState().dragMode = null;
    measurementGraphPointerId = null;
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
            setMeasurementGraphView(button.getAttribute('data-measurement-view') || 'freq');
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


// ---------------------------------------------------------------------------
// Format seconds → m:ss
// ---------------------------------------------------------------------------
function formatTime(sec) {
    const mod = (typeof window !== 'undefined' && window.FXRouteUiHelpers) || (typeof globalThis !== 'undefined' && globalThis.FXRouteUiHelpers) || null;
    return mod.formatTime(sec);
}


// Guard against stale in-flight poll responses — bump generation when source changes
// Generation the running poll timer was started with. Another path may bump
// _spotifyPollGeneration without stopping the timer, which would leave the
// callback no-op'ing forever; StreamingRuntime.startSpotifyPoll() uses this to replace it.


if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => StreamingRuntime.initSpotify());
} else {
    StreamingRuntime.initSpotify();
}
