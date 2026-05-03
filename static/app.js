// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute - Frontend JavaScript
 * Vanilla JS, no dependencies
 */
const CONFIG = {
    wsUrl: null, // will be set dynamically
    reconnectInterval: 3000,
    maxReconnectAttempts: 10,
};
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
        output_peak_warning: {
            available: false,
            detected: false,
            hold_ms: 0,
            threshold: 1.0,
            vu_db: null,
            target: null,
            last_over_at: null,
            last_error: null,
        },
    },
    library: {
        tracks: [],
        scanning: false,
        selectedTrackIds: [],
        searchQuery: '',
        shuffle: false,
        loop: false,
        selectionDownloadPending: false,
    },
    playlists: [],
    stations: [],
    download: null,
    easyeffects: {
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
        currentMeasurementSaved: false,
        currentMeasurementName: '',
        browserInputs: [],
        browserInputsLoading: false,
        browserPermissionGranted: false,
        selectedBrowserInputId: '',
        inputs: [],
        selectedInputId: '',
        selectedChannel: 'left',
        displaySmoothing: '1/6-oct',
        captureMode: 'host-local',
        hostCaptureAvailable: false,
        browserSupported: false,
        browserInputLabel: '',
        modeNote: '',
        calibrationFilename: '',
        calibrationOptions: [],
        selectedCalibrationRef: '',
        calibrationUpdating: false,
        calibrationDeleting: false,
        browserMeasurementPrimed: false,
        visibilityById: {},
        reviewVisibilityById: {},
        savedGroupOpen: false,
        setupOpen: false,
        storage: null,
        captureAvailable: false,
        activeJobId: '',
        statusText: 'Sweep ready. Calibration file is optional.',
        peqAssistant: {
            enabled: false,
            filters: [],
            activeFilterId: null,
            dragFilterId: null,
        },
    },
    samplerate: {
        available: false,
        active_rate: null,
        mode: null,
        force_rate: null,
    },
    settings: {
        audioOutputs: {
            available: false,
            default_output: null,
            selected_output: null,
            current_output: null,
            outputs: [],
            notes: [],
            pendingSelectionKey: null,
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
    },
    wsConnected: false,
};
// WebSocket
let ws = null;
let reconnectAttempts = 0;
let reconnectTimer = null;
let wsConnectSerial = 0;
let playbackActionInFlight = false;
let pendingPlaybackRequestId = 0;
let pendingFooterSingleTrackStart = null;
let pauseActionRequestId = 0;
const FOOTER_SINGLE_TRACK_START_LOCK_MS = 5000;
let volumeTimer = null;
let volumeRequestInFlight = false;
let pendingVolume = null;
let volumeGestureActive = false;
let effectsImportInFlight = false;
let peqCreateInFlight = false;
let optimisticVolume = null;
let lastConfirmedVolume = state.playback.volume;
let volumeSyncGraceUntil = 0;
let downloadStatusPollTimer = null;
let lastDownloadStatus = null;
let spotifyVolumeTimer = null;
let spotifyVolumeRequestInFlight = false;
let pendingSpotifyVolume = null;
let libraryModeSyncArmed = false;
let lastLibraryPlaybackContextSignature = null;
let libraryModeRequestInFlight = false;
let effectsCompareLoadInFlight = false;
let librarySelectionSyncTimer = null;
let librarySelectionSyncRequestId = 0;
let settingsStatusPollTimer = null;
let measurementResizeScheduled = false;
let measurementGraphPointerId = null;
let measurementPeqTakeFeedbackTimer = null;
let measurementPeqLastTouchCreateAt = 0;
// Seek - globals
let seekDragging = false;
let seekPendingPos = null;
const VOLUME_SEND_DEBOUNCE_MS = 120;
const VOLUME_SYNC_GRACE_MS = 700;
const VOLUME_CURVE_GAMMA = 0.6;
const MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX = 14;
const MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX = 24;
const MEASUREMENT_PEQ_TOUCH_CREATE_COOLDOWN_MS = 350;
const SPOTIFY_POLL_INTERVAL_MS = 1000;
const SAMPLERATE_POLL_INTERVAL_MS = 5000;
const SAMPLERATE_BURST_POLL_DELAYS_MS = [0, 120, 280, 520, 900, 1400, 2200, 3200];
const LIBRARY_SELECTION_SYNC_DEBOUNCE_MS = 180;
const DOWNLOAD_STATUS_POLL_INTERVAL_MS = 1500;
const PEAK_STATUS_POLL_INTERVAL_MS = 1200;
const EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS = 800;
const EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS = 2000;
// DOM elements
const elements = {
    offlineIndicator: document.getElementById('offline-indicator'),
    settingsOpenBtn: document.getElementById('open-settings'),
    settingsPanel: document.getElementById('settings-panel'),
    settingsCloseBtn: document.getElementById('close-settings'),
    settingsOutputSummary: document.getElementById('settings-output-summary'),
    settingsOutputSelect: document.getElementById('settings-output-select'),
    settingsSourceSelect: document.getElementById('settings-source-select'),
    settingsSourceModeHint: document.getElementById('settings-source-mode-hint'),
    settingsBluetoothStatus: document.getElementById('settings-bluetooth-status'),
    settingsCertificateLink: document.getElementById('settings-certificate-link'),
    tabs: document.querySelectorAll('.tab-btn'),
    tabPanels: document.querySelectorAll('.tab-panel'),
    stationsGrid: document.getElementById('stations-grid'),
    toggleStationManageBtn: document.getElementById('toggle-station-manage'),
    closeStationManageBtn: document.getElementById('close-station-manage'),
    radioManagePanel: document.getElementById('radio-manage-panel'),
    stationNameGroup: document.getElementById('station-name-group'),
    stationName: document.getElementById('station-name'),
    stationImageGroup: document.getElementById('station-image-group'),
    stationImageUrl: document.getElementById('station-image-url'),
    stationUrlDropArea: document.getElementById('station-url-drop-area'),
    stationUrlHint: document.getElementById('station-url-hint'),
    stationUrl: document.getElementById('station-url'),
    stationSaveRow: document.getElementById('station-save-row'),
    stationSaveBtn: document.getElementById('station-save'),
    stationDeleteSelect: document.getElementById('station-delete-select'),
    stationExistingFields: document.getElementById('station-existing-fields'),
    stationExistingUrl: document.getElementById('station-existing-url'),
    stationExistingImageUrl: document.getElementById('station-existing-image-url'),
    stationUpdateBtn: document.getElementById('station-update'),
    stationDeleteBtn: document.getElementById('station-delete'),
    stationFormStatus: document.getElementById('station-form-status'),
    toggleImportBtn: document.getElementById('toggle-import'),
    libraryShuffleBtn: document.getElementById('library-shuffle'),
    libraryLoopBtn: document.getElementById('library-loop'),
    libraryImportPanel: document.getElementById('library-import-panel'),
    refreshLibraryBtn: document.getElementById('refresh-library'),
    librarySearchInput: document.getElementById('library-search'),
    playSelectedTracksBtn: document.getElementById('play-selected-tracks'),
    selectAllTracksBtn: document.getElementById('select-all-tracks'),
    playlistName: document.getElementById('playlist-name'),
    savePlaylistBtn: document.getElementById('save-playlist'),
    playlistSaveRow: document.getElementById('playlist-save-row'),
    libraryInfo: document.getElementById('library-info'),
    downloadSelectedTracksBtn: document.getElementById('download-selected-tracks'),
    deleteSelectedTracksBtn: document.getElementById('delete-selected-tracks'),
    tracksList: document.getElementById('tracks-list'),
    downloadUrlDropArea: document.getElementById('download-url-drop-area'),
    downloadUrlHint: document.getElementById('download-url-detail'),
    downloadUrl: document.getElementById('download-url'),
    downloadBtn: document.getElementById('download-btn'),
    cancelDownloadBtn: document.getElementById('cancel-download'),
    uploadTrackFile: document.getElementById('upload-track-file'),
    uploadTrackBtn: document.getElementById('upload-track-btn'),
    downloadStatus: document.getElementById('download-status'),
    refreshEffectsBtn: document.getElementById('refresh-effects'),
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
    measurementSetupToggleBtn: document.getElementById('measurement-setup-toggle'),
    measurementModeSelect: document.getElementById('measurement-mode-select'),
    measurementModeNote: document.getElementById('measurement-mode-note'),
    measurementBrowserHelp: document.getElementById('measurement-browser-help'),
    measurementBrowserInputGroup: document.getElementById('measurement-browser-input-group'),
    measurementBrowserInputSelect: document.getElementById('measurement-browser-input-select'),
    measurementBrowserInputRefreshBtn: document.getElementById('measurement-browser-input-refresh'),
    measurementBrowserInputNote: document.getElementById('measurement-browser-input-note'),
    measurementInputGroup: document.getElementById('measurement-input-group'),
    measurementInputSelect: document.getElementById('measurement-input-select'),
    measurementInputRefreshBtn: document.getElementById('measurement-input-refresh'),
    measurementChannelSelect: document.getElementById('measurement-channel-select'),
    measurementCalibrationSelect: document.getElementById('measurement-calibration-select'),
    measurementCalibrationFile: document.getElementById('measurement-calibration-file'),
    measurementCalibrationDeleteBtn: document.getElementById('measurement-calibration-delete'),
    measurementCalibrationUploadName: document.getElementById('measurement-calibration-upload-name'),
    measurementCalibrationName: document.getElementById('measurement-calibration-name'),
    measurementNameInput: document.getElementById('measurement-name'),
    measurementStartBtn: document.getElementById('measurement-start'),
    measurementSaveBtn: document.getElementById('measurement-save'),
    measurementClearBtn: document.getElementById('measurement-clear'),
    measurementSetupStatus: document.getElementById('measurement-setup-status'),
    measurementSummary: document.getElementById('measurement-summary'),
    measurementGraphControls: document.getElementById('measurement-graph-controls'),
    measurementGraph: document.getElementById('measurement-graph'),
    measurementEmpty: document.getElementById('measurement-empty'),
    measurementPeqPanel: document.getElementById('measurement-peq-panel'),
    measurementPeqChips: document.getElementById('measurement-peq-chips'),
    measurementPeqEditor: document.getElementById('measurement-peq-editor'),
    measurementPeqTakeLeftBtn: document.getElementById('measurement-peq-take-left'),
    measurementPeqTakeRightBtn: document.getElementById('measurement-peq-take-right'),
    measurementPeqTakeBothBtn: document.getElementById('measurement-peq-take-both'),
    measurementPeqTakeFeedback: document.getElementById('measurement-peq-take-feedback'),
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
    effectsDelayEnabled: document.getElementById('effects-delay-enabled'),
    effectsDelayInputsWrap: document.getElementById('effects-delay-inputs-wrap'),
    effectsDelayLeftMs: document.getElementById('effects-delay-left-ms'),
    effectsDelayRightMs: document.getElementById('effects-delay-right-ms'),
    effectsBassEnabled: document.getElementById('effects-bass-enabled'),
    effectsBassAmount: document.getElementById('effects-bass-amount'),
    effectsBassControlsWrap: document.getElementById('effects-bass-controls-wrap'),
    effectsToneEffectEnabled: document.getElementById('effects-tone-effect-enabled'),
    effectsToneEffectWrap: document.getElementById('effects-tone-effect-wrap'),
    effectsToneEffectMode: document.getElementById('effects-tone-effect-mode'),
    effectsExtrasFeedback: document.getElementById('effects-extras-feedback'),
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
    effectsPeqPresetName: document.getElementById('effects-peq-preset-name'),
    effectsPeqModeSelect: document.getElementById('effects-peq-mode-select'),
    effectsPeqLoadAfterCreate: document.getElementById('effects-peq-load-after-create'),
    effectsPeqAddBandBtn: document.getElementById('effects-peq-add-band'),
    effectsPeqLeftBands: document.getElementById('effects-peq-left-bands'),
    effectsPeqRightBands: document.getElementById('effects-peq-right-bands'),
    effectsPeqCreatePresetBtn: document.getElementById('effects-peq-create-preset'),
    effectsStatus: document.getElementById('effects-status'),
    playbackBar: document.getElementById('playback-bar'),
    trackTitle: document.getElementById('track-title'),
    trackArtist: document.getElementById('track-artist'),
    playbackEq: document.getElementById('playback-eq'),
    connDot: document.getElementById('connection-dot'),
    connText: document.getElementById('connection-text'),
    btnPrevious: document.getElementById('btn-previous'),
    btnPlayPause: document.getElementById('btn-play-pause'),
    btnNext: document.getElementById('btn-next'),
    btnClearQueue: document.getElementById('btn-clear-queue'),
    queueStatus: document.getElementById('queue-status'),
    samplerateStatus: document.getElementById('samplerate-status'),
    outputLevelBadge: document.getElementById('output-level-badge'),
    peakWarningBadge: document.getElementById('peak-warning-badge'),
    seekSlider: document.getElementById('seek-slider'),
    seekCurrent: document.getElementById('seek-current'),
    seekDuration: document.getElementById('seek-duration'),
    volumeSlider: document.getElementById('volume-slider'),
    volumeDisplay: document.getElementById('volume-display'),
    toastContainer: document.getElementById('toast-container'),
};
// Initialization
document.addEventListener('DOMContentLoaded', () => {
    try { setupWebSocket(); } catch(e) { console.error('setupWebSocket crashed:', e); }
    try { setupTabNavigation(); } catch(e) { console.error('setupTabNavigation crashed:', e); }
    try { setupPlaybackControls(); } catch(e) { console.error('setupPlaybackControls crashed:', e); }
    try { setupSettingsActions(); } catch(e) { console.error('setupSettingsActions crashed:', e); }
    try { initSeek(); } catch(e) { console.error('initSeek crashed:', e); }
    try { setupStationActions(); } catch(e) { console.error('setupStationActions crashed:', e); }
    try { setupLibraryActions(); } catch(e) { console.error('setupLibraryActions crashed:', e); }
    try { setupDownloadActions(); } catch(e) { console.error('setupDownloadActions crashed:', e); }
    try { setupEffectsActions(); } catch(e) { console.error('setupEffectsActions crashed:', e); }
    try { setupMeasurementActions(); } catch(e) { console.error('setupMeasurementActions crashed:', e); }
    try { fetchInitialData(); } catch(e) { console.error('fetchInitialData crashed:', e); }
});
// WebSocket
function normalizeEffectsCompareSelection(compare = {}) {
    const presetA = typeof compare.presetA === 'string' ? compare.presetA : '';
    let presetB = typeof compare.presetB === 'string' ? compare.presetB : '';
    const activeSide = compare.activeSide === 'A' || compare.activeSide === 'B' ? compare.activeSide : null;
    if (presetA && presetB && presetA === presetB) {
        presetB = '';
    }
    return { presetA, presetB, activeSide };
}

function resolveEffectsCompareState(compare, presets = [], activePreset = '') {
    const server = normalizeEffectsCompareSelection(compare || {});
    const presetSet = new Set((presets || []).filter(Boolean));
    const presetA = presetSet.has(server.presetA) ? server.presetA : (activePreset && presetSet.has(activePreset) ? activePreset : '');
    const presetB = presetSet.has(server.presetB) ? server.presetB : '';
    const activeSide = server.activeSide === 'A' || server.activeSide === 'B' ? server.activeSide : null;
    return normalizeEffectsCompareSelection({ presetA, presetB, activeSide });
}

async function saveEffectsCompareState(compare) {
    try {
        await fetch('/api/easyeffects/compare', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(compare),
        });
    } catch (e) {
        console.warn('Failed to persist effects compare state', e);
    }
}

function getDefaultEffectsCombineDraft() {
    return {
        preset1: '',
        preset2: '',
        preset3: '',
        presetName: '',
    };
}

function normalizeEffectsCombineDraft(draft = {}, presets = []) {
    const presetSet = new Set((presets || []).filter(Boolean));
    const chosen = [];
    const pickUnique = (value) => {
        const preset = presetSet.has(value) ? value : '';
        if (!preset || chosen.includes(preset)) return '';
        chosen.push(preset);
        return preset;
    };
    return {
        preset1: pickUnique(draft.preset1),
        preset2: pickUnique(draft.preset2),
        preset3: pickUnique(draft.preset3),
        presetName: typeof draft.presetName === 'string' ? draft.presetName : '',
    };
}

function setEffectsImportPanelOpen(shouldOpen) {
    if (!elements.effectsImportPanel || !elements.effectsToggleImportBtn) return;
    elements.effectsImportPanel.classList.toggle('hidden', !shouldOpen);
    elements.effectsToggleImportBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    elements.effectsToggleImportBtn.textContent = shouldOpen ? 'Close import' : 'Import…';
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
        updateConnectionBadge(true);
        elements.offlineIndicator.classList.add('hidden');
        stopMetadataPolling();
        startPeakStatusPolling();
    };
    socket.onclose = (event) => {
        if (ws === socket) ws = null;
        console.log('WebSocket disconnected', { code: event.code, reason: event.reason, wasClean: event.wasClean, serial });
        if (serial !== wsConnectSerial) return;
        state.wsConnected = false;
        updateConnectionBadge(false);
        elements.offlineIndicator.classList.remove('hidden');
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
    reconnectAttempts++;
    console.log(`Reconnecting in ${CONFIG.reconnectInterval}ms (attempt ${reconnectAttempts})`);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWebSocket();
    }, CONFIG.reconnectInterval);
}
function handleWebSocketMessage(msg) {
    const { type, data } = msg;
    switch (type) {
        case 'init':
            // Initial state
            if (data.player) {
                mergePlaybackState(data.player.state);
                syncFooterOwnershipFromPlayback(data.player.state);
                syncLibraryStateFromPlaybackContext(true);
                updatePlaybackUI();
            }
            if (data.library) {
                state.library.tracks = [];
                renderTracks();
            }
            if (data.stations) {
                state.stations = data.stations;
                renderStations();
                renderStationDeleteOptions();
            }
            if (data.spotify) {
                handleIncomingSpotifyState(data.spotify, { renderTab: true, renderFooter: true });
            }
            if (data.player && data.player.state && data.player.state.easyeffects) {
                state.easyeffects = data.player.state.easyeffects;
                if (state.easyeffects?.global_extras) {
                    applyEffectsExtras({
                        limiterEnabled: !!state.easyeffects.global_extras?.limiter?.enabled,
                        headroomEnabled: !!state.easyeffects.global_extras?.headroom?.enabled,
                        headroomGainDb: Number(state.easyeffects.global_extras?.headroom?.params?.gainDb ?? -3),
                        autogainEnabled: !!state.easyeffects.global_extras?.autogain?.enabled,
                        autogainTargetDb: Number(state.easyeffects.global_extras?.autogain?.params?.targetDb ?? -12),
                        delayEnabled: !!state.easyeffects.global_extras?.delay?.enabled,
                        delayLeftMs: Number(state.easyeffects.global_extras?.delay?.params?.leftMs || 0),
                        delayRightMs: Number(state.easyeffects.global_extras?.delay?.params?.rightMs || 0),
                        bassEnabled: !!state.easyeffects.global_extras?.bass_enhancer?.enabled,
                        bassAmount: Number(state.easyeffects.global_extras?.bass_enhancer?.params?.amount || 0),
                        toneEffectEnabled: !!state.easyeffects.global_extras?.tone_effect?.enabled,
                        toneEffectMode: String(state.easyeffects.global_extras?.tone_effect?.mode || 'crystalizer'),
                    });
                }
                renderEffects();
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
            mergePlaybackState(data);
            const clearFooterSingleTrackLockAfterSync = footerSingleTrackStartLockSatisfied(state.playback);
            syncFooterOwnershipFromPlayback(data);
            if (clearFooterSingleTrackLockAfterSync) {
                clearPendingFooterSingleTrackStart();
            }
            syncLibraryStateFromPlaybackContext();
            // Reset action guard so this client doesn't block its own UI from server state.
            playbackActionInFlight = false;
            updatePlaybackUI();
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
        case 'easyeffects':
            const prev = state.easyeffects?.compare;
            const presetNames = (data.presets || []).map(p => p.name);
            state.easyeffects = {
                ...data,
                combineDraft: state.easyeffects?.combineDraft || getDefaultEffectsCombineDraft(),
                peqDraft: state.easyeffects?.peqDraft || { presetName: '', eqMode: 'IIR', loadAfterCreate: false, leftBands: [defaultPeqBand()], rightBands: [defaultPeqBand()] },
                compare: resolveEffectsCompareState(data.compare || prev, presetNames, data.active_preset || ''),
            };
            state.easyeffects.combineDraft = normalizeEffectsCombineDraft(state.easyeffects.combineDraft, presetNames);
            renderEffects();
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
    });
}

function setupSettingsActions() {
    if (!elements.settingsOpenBtn || !elements.settingsPanel || !elements.settingsCloseBtn) return;
    elements.settingsOpenBtn.addEventListener('click', () => toggleSettingsPanel(true));
    elements.settingsCloseBtn.addEventListener('click', () => toggleSettingsPanel(false));
    if (elements.settingsOutputSelect) {
        elements.settingsOutputSelect.addEventListener('change', (event) => {
            const outputKey = event.target.value || '';
            if (outputKey) void saveAudioOutputSelection(outputKey);
        });
    }
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
    const backdrop = elements.settingsPanel.querySelector('.manage-overlay-backdrop');
    if (backdrop) backdrop.addEventListener('click', () => toggleSettingsPanel(false));
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !elements.settingsPanel.classList.contains('hidden')) {
            toggleSettingsPanel(false);
        }
    });
    renderSettingsPanel();
}

function stopSettingsStatusPolling() {
    if (settingsStatusPollTimer) {
        clearInterval(settingsStatusPollTimer);
        settingsStatusPollTimer = null;
    }
}

function startSettingsStatusPolling() {
    stopSettingsStatusPolling();
    settingsStatusPollTimer = setInterval(() => {
        if (!elements.settingsPanel || elements.settingsPanel.classList.contains('hidden')) {
            stopSettingsStatusPolling();
            return;
        }
        void fetchAudioSourceOverview();
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
        renderSettingsPanel();
        void Promise.all([fetchAudioOutputOverview(), fetchAudioSourceOverview()]);
        startSettingsStatusPolling();
        elements.settingsCloseBtn?.focus();
    } else {
        stopSettingsStatusPolling();
    }
}

function formatRateKhz(rate) {
    const numericRate = Number(rate);
    if (!Number.isFinite(numericRate) || numericRate <= 0) return '';
    return `${(numericRate / 1000).toFixed(1).replace(/\.0$/, '')} kHz`;
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
    const host = String(window.location.host || window.location.hostname || '').trim();
    return host ? `http://${host}/api/browser-mic/certificate` : '/api/browser-mic/certificate';
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

    if (elements.settingsOutputSelect) {
        const options = selectableOutputs.map((output) => {
            const label = output.label || output.name || 'Unknown output';
            return `<option value="${escapeHtml(output.key || '')}">${escapeHtml(label)}</option>`;
        });
        elements.settingsOutputSelect.innerHTML = options.join('') || '<option value="">No outputs available</option>';
        if (effectiveSelectedKey) elements.settingsOutputSelect.value = effectiveSelectedKey;
        elements.settingsOutputSelect.disabled = !overview.available || !!pendingSelectionKey || !selectableOutputs.length;
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
            ...sourceInputs.map((input) => `<option value="external-input::${escapeHtml(input.key || '')}">External input — ${escapeHtml(input.label || input.name || 'Unknown input')}</option>`),
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
    applySourceModeUiState();
}

function externalInputModeActive() {
    return state.settings?.sourceMode?.mode === 'external-input';
}

function nonAppSourceModeActive() {
    return ['external-input', 'bluetooth-input'].includes(state.settings?.sourceMode?.mode);
}

function applySourceModeUiState() {
    const nonAppSourceActive = nonAppSourceModeActive();
    ['radio', 'spotify', 'library'].forEach((tabId) => {
        const tabButton = document.querySelector(`.tab-btn[data-tab="${tabId}"]`);
        const tabPanel = document.getElementById(`tab-${tabId}`);
        if (tabButton) tabButton.classList.toggle('hidden', nonAppSourceActive);
        if (tabPanel) tabPanel.classList.toggle('hidden', nonAppSourceActive);
    });
    if (elements.playbackBar) {
        elements.playbackBar.classList.toggle('hidden', nonAppSourceActive);
    }
    if (nonAppSourceActive && ['radio', 'spotify', 'library'].includes(window.__visibleTab)) {
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
            available: !!data.available,
            default_output: data.default_output || null,
            selected_output: data.selected_output || null,
            current_output: data.current_output || null,
            outputs: Array.isArray(data.outputs) ? data.outputs : [],
            notes: Array.isArray(data.notes) ? data.notes : [],
            pendingSelectionKey: null,
        };
        renderSettingsPanel();
        showToast('Audio output updated', 'success');
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
        state.settings.audioOutputs = {
            available: !!data.available,
            default_output: data.default_output || null,
            selected_output: data.selected_output || null,
            current_output: data.current_output || null,
            outputs: Array.isArray(data.outputs) ? data.outputs : [],
            notes: Array.isArray(data.notes) ? data.notes : [],
            pendingSelectionKey: null,
        };
        renderSettingsPanel();
    } catch (e) {
        state.settings.audioOutputs = {
            available: false,
            default_output: null,
            selected_output: null,
            current_output: null,
            outputs: [],
            notes: [e.message || 'Failed to fetch audio outputs'],
            pendingSelectionKey: null,
        };
        renderSettingsPanel();
    }
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
// __footerSource  = which source the footer displays ('local' or 'spotify')
// Transport/volume controls route based on the effective playback owner, not the visible tab.
// Footer renders based on __footerSource. Tab switch does NOT change footer or playback.

window.__visibleTab = 'radio';
window.__footerSource = 'local';
window.__spotifySeeking = false;

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
    if (!elements.libraryImportPanel || elements.libraryImportPanel.classList.contains('hidden')) return;
    const searchWrap = elements.librarySearchInput ? elements.librarySearchInput.closest('.library-search-wrap') : null;
    const selectionToolbar = elements.selectAllTracksBtn ? elements.selectAllTracksBtn.closest('.library-selection-toolbar') : null;
    clearLibraryImportFeedbackIfIdle();
    resetUploadAreaSelection('upload-track-file');
    elements.libraryImportPanel.classList.add('hidden');
    if (searchWrap) searchWrap.classList.remove('hidden');
    if (selectionToolbar) selectionToolbar.classList.remove('hidden');
    if (elements.playlistSaveRow) updatePlaylistSaveRowVisibility();
    if (elements.toggleImportBtn) elements.toggleImportBtn.textContent = '＋ Import';
}

function switchTab(tabId) {
    closeLibraryImportPanel();
    elements.tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tabId));
    elements.tabPanels.forEach(p => p.classList.toggle('active', p.id === `tab-${tabId}`));
    window.__visibleTab = tabId;
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
}

function getBackendFooterOwner(playback = state.playback, spotify = window.__spotifyLastData) {
    const owner = playback?.footer_owner || spotify?.footer_owner || null;
    return owner === 'spotify' || owner === 'local' ? owner : null;
}

function getEffectivePlaybackControlSource() {
    const backendOwner = getBackendFooterOwner();
    if (backendOwner) return backendOwner;
    if (spotifyPlayingOwnsFooter()) return 'spotify';
    if (localPlaybackHasFooterContext(state.playback) || localEndedPlaybackHasFooterContext(state.playback)) return 'local';
    if (spotifyPausedHasFooterContext()) return 'spotify';
    return window.__footerSource === 'spotify' ? 'spotify' : 'local';
}

function globalTogglePlayback() {
    if (getEffectivePlaybackControlSource() === 'spotify') {
        spotifyCommand('toggle');
    } else {
        togglePlayback();
    }
}

function globalPrevious() {
    if (getEffectivePlaybackControlSource() === 'spotify') {
        spotifyCommand('previous');
    } else {
        previousInQueue();
    }
}

function globalNext() {
    if (getEffectivePlaybackControlSource() === 'spotify') {
        spotifyCommand('next');
    } else {
        nextInQueue();
    }
}

function globalSeekChange() {
    if (window.__footerSource === 'spotify') {
        const spotifyData = window.__spotifyLastData;
        if (spotifyData && spotifyData.duration) window.__spotifySeeking = true;
    }
}

function globalSeekEnd() {
    if (window.__footerSource === 'spotify') {
        window.__spotifySeeking = false;
        const spotifyData = window.__spotifyLastData;
        if (spotifyData && spotifyData.duration) {
            const posSec = (parseFloat(elements.seekSlider.value) / 1000) * spotifyData.duration;
            spotifySeek(posSec);
        }
    }
}

function setupPlaybackControls() {
    if (!elements.btnPlayPause || !elements.volumeSlider) {
        console.error('Playback controls are missing in the DOM');
        return;
    }
    if (elements.btnPrevious) elements.btnPrevious.addEventListener('click', globalPrevious);
    elements.btnPlayPause.addEventListener('click', globalTogglePlayback);
    if (elements.btnNext) elements.btnNext.addEventListener('click', globalNext);
    if (elements.btnClearQueue) elements.btnClearQueue.addEventListener('click', clearQueue);
    if (elements.libraryShuffleBtn) elements.libraryShuffleBtn.addEventListener('click', toggleLibraryShuffle);
    if (elements.libraryLoopBtn) elements.libraryLoopBtn.addEventListener('click', toggleLibraryLoop);
    elements.volumeSlider.addEventListener('input', handleVolumeChange);
    elements.volumeSlider.addEventListener('change', (e) => {
        const sliderValue = parseInt(e.target.value, 10);
        const actualVolume = sliderVolumeToActualVolume(sliderValue);
        volumeGestureActive = false;
        if (getEffectivePlaybackControlSource() === 'spotify') {
            queueSpotifyVolumeSend(actualVolume, true);
            return;
        }
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
    const wanted = new Set((trackIds || []).filter(Boolean));
    if (wanted.size === 0) return [];
    const ordered = (state.library.tracks || [])
        .map(track => track?.id)
        .filter(id => id && wanted.has(id));
    return [...new Set(ordered)];
}
function getSelectedPlayableTrackIds() {
    return getTrackIdsInLibraryOrder(state.library.selectedTrackIds || []);
}
function getSelectedDownloadTrackIds() {
    return getTrackIdsInLibraryOrder(state.library.selectedTrackIds || []);
}
function renderLibraryModeButtons() {
    const localActive = !!(state.playback.current_track && state.playback.current_track.source === 'local');
    const queue = state.playback.queue || {};
    const shuffleAvailable = localActive && Number(queue.count || 0) > 1;
    const loopAvailable = localActive;
    if (elements.libraryShuffleBtn) {
        elements.libraryShuffleBtn.classList.toggle('active', !!state.library.shuffle);
        elements.libraryShuffleBtn.setAttribute('aria-pressed', state.library.shuffle ? 'true' : 'false');
        elements.libraryShuffleBtn.disabled = libraryModeRequestInFlight || !shuffleAvailable;
        elements.libraryShuffleBtn.title = shuffleAvailable ? 'Shuffle queue' : 'Shuffle requires an active local queue';
    }
    if (elements.libraryLoopBtn) {
        elements.libraryLoopBtn.classList.toggle('active', !!state.library.loop);
        elements.libraryLoopBtn.setAttribute('aria-pressed', state.library.loop ? 'true' : 'false');
        elements.libraryLoopBtn.disabled = libraryModeRequestInFlight || !loopAvailable;
        elements.libraryLoopBtn.title = loopAvailable ? 'Loop queue or track' : 'Loop requires active local playback';
    }
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
        if (!resp.ok) throw new Error(data.detail || 'Shuffle update failed');
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
        if (!resp.ok) throw new Error(data.detail || 'Loop update failed');
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
    const canTogglePause = !!state.playback.current_track && !!state.playback.current_file && !previousEnded;
    if (!canTogglePause) {
        if (!state.playback.current_track) return;
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
            throw new Error(data.detail || 'Playback toggle failed');
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
    } catch (e) {
        if (requestId !== pauseActionRequestId) return;
        state.playback.playing = previousPlaying;
        state.playback.paused = previousPaused;
        state.playback.ended = previousEnded;
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

function renderVolumeControlsFromActualVolume(actualVolume) {
    const sliderValue = actualVolumeToSliderValue(actualVolume);
    elements.volumeSlider.value = sliderValue;
    elements.volumeDisplay.textContent = `${sliderValue}%`;
}

function setLocalVolume(sliderValue) {
    const clampedSliderValue = clampVolumeValue(sliderValue);
    const actualVolume = sliderVolumeToActualVolume(clampedSliderValue);
    state.playback.volume = actualVolume;
    elements.volumeSlider.value = clampedSliderValue;
    elements.volumeDisplay.textContent = `${clampedSliderValue}%`;
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
    const nextPlayback = { ...data };
    const remoteVolume = typeof nextPlayback.volume === 'number' ? nextPlayback.volume : null;
    if (remoteVolume !== null) {
        delete nextPlayback.volume;
    }
    state.playback = { ...state.playback, ...nextPlayback };
    if (remoteVolume !== null) {
        applyRemoteVolume(remoteVolume);
    }
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
    if (!(track && (track.source === 'local' || track.source === 'radio'))) return false;
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
function getLibraryPlaybackContext(playback = state.playback) {
    const currentTrack = playback?.current_track;
    const queue = playback?.queue || {};
    if (!currentTrack || currentTrack.source !== 'local') {
        return null;
    }
    const queueTrackIds = Array.isArray(queue.tracks)
        ? queue.tracks.map(track => track?.id).filter(Boolean)
        : [];
    const selectedTrackIds = queueTrackIds.length > 1
        ? queueTrackIds
        : (currentTrack.id ? [currentTrack.id] : []);
    return {
        selectedTrackIds,
        shuffle: !!queue.shuffle,
        loop: !!queue.loop,
    };
}
function syncLibraryStateFromPlaybackContext(force = false) {
    const context = getLibraryPlaybackContext();
    const signature = JSON.stringify(context || { selectedTrackIds: [], shuffle: false, loop: false });
    const changed = signature !== lastLibraryPlaybackContextSignature;
    lastLibraryPlaybackContextSignature = signature;
    if (!force && !changed) return;
    if (playbackActionInFlight) return;

    if (!context) {
        if (state.library.selectedTrackIds.length || state.library.shuffle || state.library.loop) {
            state.library.selectedTrackIds = [];
            state.library.shuffle = false;
            state.library.loop = false;
            renderTracks();
            renderLibraryModeButtons();
        }
        return;
    }

    state.library.selectedTrackIds = [...context.selectedTrackIds];
    state.library.shuffle = context.shuffle;
    state.library.loop = context.loop;
    renderTracks();
    renderLibraryModeButtons();
}
function getActiveLocalTrackId() {
    const currentTrack = state.playback?.current_track;
    return currentTrack && currentTrack.source === 'local' ? currentTrack.id : null;
}
function buildLibrarySelectionPlaybackContext() {
    const activeTrackId = getActiveLocalTrackId();
    const selectedTrackIds = getSelectedPlayableTrackIds();
    if (!activeTrackId || selectedTrackIds.length === 0 || !selectedTrackIds.includes(activeTrackId)) {
        return null;
    }
    return {
        selectedTrackIds,
        shuffle: !!state.library.shuffle,
        loop: !!state.library.loop,
    };
}
function scheduleActiveLocalQueueSync() {
    if (librarySelectionSyncTimer) {
        clearTimeout(librarySelectionSyncTimer);
        librarySelectionSyncTimer = null;
    }
    const targetContext = buildLibrarySelectionPlaybackContext();
    if (!targetContext || playbackActionInFlight) {
        return;
    }
    const currentContext = getLibraryPlaybackContext();
    if (JSON.stringify(targetContext) === JSON.stringify(currentContext || { selectedTrackIds: [], shuffle: false, loop: false })) {
        return;
    }
    librarySelectionSyncTimer = setTimeout(() => {
        librarySelectionSyncTimer = null;
        void syncActiveLocalQueueFromSelection();
    }, LIBRARY_SELECTION_SYNC_DEBOUNCE_MS);
}
async function syncActiveLocalQueueFromSelection() {
    const targetContext = buildLibrarySelectionPlaybackContext();
    if (!targetContext || playbackActionInFlight || libraryModeRequestInFlight) {
        return;
    }
    const currentContext = getLibraryPlaybackContext();
    if (JSON.stringify(targetContext) === JSON.stringify(currentContext || { selectedTrackIds: [], shuffle: false, loop: false })) {
        return;
    }
    const requestId = ++librarySelectionSyncRequestId;
    try {
        const resp = await fetch('/api/playback/selection', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                queue_track_ids: targetContext.selectedTrackIds,
                shuffle: targetContext.shuffle,
                loop: targetContext.loop,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (requestId !== librarySelectionSyncRequestId) return;
        if (!resp.ok) throw new Error(data.detail || 'Queue update failed');
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        updatePlaybackUI();
    } catch (e) {
        if (requestId !== librarySelectionSyncRequestId) return;
        console.warn('Failed to sync active local queue from selection', e);
    }
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

function queueSpotifyVolumeSend(volume, immediate = false) {
    pendingSpotifyVolume = volume;
    clearTimeout(spotifyVolumeTimer);
    if (immediate) {
        void sendSpotifyVolume();
        return;
    }
    spotifyVolumeTimer = setTimeout(() => {
        void sendSpotifyVolume();
    }, VOLUME_SEND_DEBOUNCE_MS);
}

async function sendSpotifyVolume() {
    if (spotifyVolumeRequestInFlight || pendingSpotifyVolume === null) return;
    spotifyVolumeRequestInFlight = true;
    while (pendingSpotifyVolume !== null) {
        const nextVolume = pendingSpotifyVolume;
        pendingSpotifyVolume = null;
        try {
            const resp = await fetch('/api/spotify/volume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ volume: nextVolume }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Spotify volume change failed');
            if (data) handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
            lastConfirmedVolume = typeof data.volume === 'number' ? data.volume : nextVolume;
            state.playback.volume = lastConfirmedVolume;
            volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
            if (!volumeGestureActive && pendingSpotifyVolume === null) {
                optimisticVolume = null;
            }
        } catch (e) {
            pendingSpotifyVolume = null;
            volumeGestureActive = false;
            optimisticVolume = null;
            showToast(e.message || 'Failed to set Spotify volume', 'error');
            break;
        }
    }
    spotifyVolumeRequestInFlight = false;
    updatePlaybackUI();
}

async function handleVolumeChange(e) {
    const sliderValue = parseInt(e.target.value, 10);
    const actualVolume = sliderVolumeToActualVolume(sliderValue);
    volumeGestureActive = true;
    optimisticVolume = actualVolume;
    volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
    setLocalVolume(sliderValue);
    if (getEffectivePlaybackControlSource() === 'spotify') {
        queueSpotifyVolumeSend(actualVolume);
        return;
    }
    queueVolumeSend(actualVolume);
}
// Metadata polling for radio ICY tags
let metadataPollTimer = null;
let sampleratePollTimer = null;
let samplerateBurstPollTimers = [];
let lastSampleratePlaybackSignature = null;
let peakStatusPollTimer = null;
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
    if (!state.playback.playing && !state.playback.paused) return;
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
            mergePlaybackState({ current_track: data.current_track, playing: data.playing, paused: data.paused, live_title: data.live_title });
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
    const samplerate = state.samplerate || {};
    if (!samplerate.available || !samplerate.active_rate) {
        elements.samplerateStatus.textContent = 'Auto';
        elements.samplerateStatus.classList.add('hidden');
        return;
    }
    const khz = (samplerate.active_rate / 1000).toFixed(1).replace(/\.0$/, '');
    const modePrefix = samplerate.mode === 'auto' ? 'Auto · ' : '';
    elements.samplerateStatus.textContent = `${modePrefix}${khz} kHz`;
    elements.samplerateStatus.classList.remove('hidden');
}
function formatOutputLevelBadgeDb(level) {
    const rounded = Math.round(Number(level));
    if (!Number.isFinite(rounded)) return '';
    const sign = rounded < 0 ? '-' : '';
    const abs = Math.abs(rounded);
    const digits = abs < 10 ? `0${abs}` : String(abs);
    return `${sign}${digits} dB`;
}

function renderPeakWarningBadge() {
    const warning = state.playback.output_peak_warning || {};
    const showPeak = !!warning.detected;
    const title = warning.target?.description || warning.target?.source_name || 'EasyEffects output monitor';
    const vuDb = Number.isFinite(Number(warning.vu_db)) ? Number(warning.vu_db) : null;

    if (elements.peakWarningBadge) {
        elements.peakWarningBadge.classList.toggle('hidden', !showPeak);
        elements.peakWarningBadge.title = showPeak ? `Post-EasyEffects output peak detected on ${title}` : '';
    }

    if (elements.outputLevelBadge) {
        const showVu = !!warning.available && vuDb !== null;
        elements.outputLevelBadge.classList.toggle('hidden', !showVu);
        elements.outputLevelBadge.textContent = showVu ? formatOutputLevelBadgeDb(vuDb) : '';
        elements.outputLevelBadge.title = showVu ? `Post-EasyEffects output level (slow VU) on ${title}` : '';
    }

    if (elements.playbackEq) {
        elements.playbackEq.classList.toggle('peak-alert', showPeak);
        elements.playbackEq.title = showPeak ? `Post-EasyEffects output peak detected on ${title}` : '';
        if (showPeak) {
            elements.playbackEq.innerHTML = '<span class="peak-alert-label">PEAK</span>';
        } else if (!elements.playbackEq.querySelector('.bar')) {
            elements.playbackEq.innerHTML = '<span class="bar"></span><span class="bar"></span><span class="bar"></span><span class="bar"></span>';
        }
    }
}
function renderQueueUI() {
    const queue = state.playback.queue || {};
    const footerSingleTrackOverride = footerSingleTrackStartLockActive();
    const hasQueue = footerSingleTrackOverride ? false : queue.count > 1;
    const queueIndex = footerSingleTrackOverride ? -1 : (typeof queue.index === 'number' ? queue.index : -1);
    const currentTrack = state.playback.current_track;
    const hasLocalTrack = currentTrack && currentTrack.source === 'local';
    state.library.shuffle = hasLocalTrack ? !!queue.shuffle : false;
    state.library.loop = hasLocalTrack ? !!queue.loop : false;
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

    if (elements.btnPrevious && window.__footerSource !== 'spotify') {
        elements.btnPrevious.classList.toggle('hidden', !hasQueue);
        elements.btnPrevious.disabled = playbackActionInFlight || !hasQueue || queueIndex <= 0;
    }
    if (elements.btnNext && window.__footerSource !== 'spotify') {
        elements.btnNext.classList.toggle('hidden', !hasQueue);
        elements.btnNext.disabled = playbackActionInFlight || !hasQueue || queueIndex < 0 || queueIndex >= queue.count - 1;
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
    footerDebug('footer-source', { reason, prevSource, nextSource, ...details });
}

function localPlaybackHasFooterContext(playback = state.playback) {
    const track = playback?.current_track;
    if (!(track && (track.source === 'radio' || track.source === 'local'))) return false;
    if (spotifyPlayingOwnsFooter()) return false;
    if (playback?.paused && window.__footerSource === 'spotify' && spotifyPausedHasFooterContext()) return false;
    return !!(playback?.playing || playback?.paused);
}

function localEndedPlaybackHasFooterContext(playback = state.playback) {
    const track = playback?.current_track;
    if (!(track && track.source === 'local')) return false;
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

function localFooterHoldHasContext(playback = state.playback) {
    const track = playback?.current_track;
    if (!(track && (track.source === 'radio' || track.source === 'local'))) return false;
    return Date.now() < _localFooterHoldUntil;
}

function reconcileFooterSource() {
    const backendOwner = getBackendFooterOwner();
    if (backendOwner === 'local') {
        setFooterSource('local', 'backend-footer-owner-local');
        return;
    }
    if (backendOwner === 'spotify') {
        setFooterSource('spotify', 'backend-footer-owner-spotify');
        return;
    }
    if (spotifyPlayingOwnsFooter()) {
        setFooterSource('spotify', 'spotify-playing');
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

function shouldPollSpotify() {
    return window.__visibleTab === 'spotify' || window.__footerSource === 'spotify';
}

function syncFooterOwnershipFromPlayback(playback = state.playback) {
    footerDebug('sync-from-playback', {
        playback: {
            source: playback?.current_track?.source || null,
            title: playback?.current_track?.title || null,
            liveTitle: playback?.live_title || null,
            playing: !!playback?.playing,
            paused: !!playback?.paused,
            footerOwner: playback?.footer_owner || null,
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
        setFooterSource('spotify', 'sync-playback-backend-owner-spotify');
        return;
    }
    if (spotifyPlayingOwnsFooter()) {
        setFooterSource('spotify', 'sync-playback-spotify-still-playing');
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
    // When Spotify owns the footer, local UI must NOT touch footer elements at all.
    // Refresh from Spotify truth and return — the Spotify poll owns the footer exclusively.
    if (window.__footerSource === 'spotify') {
        const spData = window.__spotifyLastData;
        if (!freezeActive && spData) updateFooterForSpotify(spData);
        highlightActiveTrack();
        return;
    }
    // Set body dataset for CSS radio/song rules
    const isRadio = current_track && current_track.source === 'radio';
    if (!freezeActive) {
        document.body.classList.remove('source-local', 'source-radio');
        document.body.classList.add(isRadio ? 'source-radio' : 'source-local');
        // Hide seek-row on radio, show on local
        const seekRow = document.querySelector('.seek-row');
        if (seekRow) seekRow.style.display = isRadio ? 'none' : '';
        // Track info
        if (current_track) {
            elements.trackTitle.textContent = isRadio && live_title ? live_title : current_track.title;
            elements.trackTitle.classList.remove('placeholder');
            elements.trackTitle.style.display = 'none';
            elements.trackTitle.classList.add('placeholder');
            const scArtist = document.getElementById('sc-artist');
            const scTitle = document.getElementById('sc-title');
            if (scArtist) scArtist.textContent = isRadio && live_title ? current_track.title : (current_track.artist || '');
            if (scTitle) scTitle.textContent = isRadio && live_title ? live_title : current_track.title;
            elements.trackArtist.textContent = isRadio && live_title ? current_track.title : (current_track.artist || '');
            elements.trackArtist.style.display = 'none';
        } else {
            elements.trackTitle.textContent = 'Not playing';
            elements.trackTitle.classList.add('placeholder');
            elements.trackArtist.textContent = '';
            if (elements.trackTitle) elements.trackTitle.style.display = '';
            if (elements.trackArtist) elements.trackArtist.style.display = '';
        }
    }
    document.body.classList.remove('is-playing', 'is-paused');
    if (playing) {
        document.body.classList.add('is-playing');
    } else if (paused) {
        document.body.classList.add('is-paused');
    }
    // EQ bar & bar glow
    if (elements.playbackEq) {
        const showPeak = !!state.playback.output_peak_warning?.detected;
        elements.playbackEq.style.display = (playing || showPeak) ? 'inline-flex' : 'none';
    }
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
    // Highlight active
    highlightActiveTrack();
}
function updatePlayPauseButton(playbackState) {
    elements.btnPlayPause.textContent = playbackState === 'playing' ? '⏸' : '▶';
    elements.btnPlayPause.disabled = playbackActionInFlight || (!state.playback.current_track && playbackState === 'stopped');
}
function highlightActiveTrack() {
    if (window.__footerSource === 'spotify') {
        document.querySelectorAll('.station-card.active, .track-item.active').forEach(item => item.classList.remove('active'));
        return;
    }
    // Radio stations
    document.querySelectorAll('.station-card').forEach(card => {
        const stationId = card.dataset.stationId;
        const activeStationId = state.playback.current_track && state.playback.current_track.source === 'radio'
            ? state.playback.current_track.id.replace(/^radio_/, '')
            : null;
        if (activeStationId && activeStationId === stationId) {
            card.classList.add('active');
        } else {
            card.classList.remove('active');
        }
    });
    // Library tracks
    document.querySelectorAll('.track-item').forEach(item => {
        const trackId = item.dataset.trackId;
        if (state.playback.current_track && state.playback.current_track.id === trackId) {
            item.classList.add('active');
        } else {
            item.classList.remove('active');
        }
    });
}
// Library
async function fetchInitialData() {
    startSampleratePolling();
    await Promise.all([fetchStations(), fetchTracks(), fetchEffects(), fetchMeasurements(), fetchPlaybackStatus(), fetchSamplerateStatus(), fetchDownloadStatus(), fetchAudioOutputOverview(), fetchAudioSourceOverview()]);
    await fetchPlaylists();
}
async function fetchPlaybackStatus() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error('Failed to fetch playback status');
        const data = await resp.json();
        mergePlaybackState(data);
        syncFooterOwnershipFromPlayback(data);
        syncLibraryStateFromPlaybackContext(true);
        updatePlaybackUI();
    } catch (e) {
        console.debug('Playback status unavailable on load', e);
    }
}
async function fetchSamplerateStatus() {
    try {
        const resp = await fetch('/api/audio/samplerate');
        if (!resp.ok) throw new Error('Failed to fetch samplerate status');
        const data = await resp.json();
        state.samplerate = { ...state.samplerate, ...data };
        renderSamplerateUI();
    } catch (e) {
        console.debug('Samplerate status unavailable', e);
        state.samplerate = { ...state.samplerate, available: false, active_rate: null };
        renderSamplerateUI();
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
        if (!resp.ok) throw new Error(data.detail || 'Previous failed');
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
        if (!resp.ok) throw new Error(data.detail || 'Next failed');
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
        state.library.selectedTrackIds = [];
        state.library.shuffle = false;
        state.library.loop = false;
        lastLibraryPlaybackContextSignature = JSON.stringify({ selectedTrackIds: [], shuffle: false, loop: false });
        renderTracks();
        renderLibraryModeButtons();
        showToast('Queue cleared', 'info');
    } catch (e) {
        showToast(e.message || 'Failed to clear queue', 'error');
    } finally {
        playbackActionInFlight = false;
        updatePlaybackUI();
    }
}
function clearStationFormStatus() {
    if (elements.stationFormStatus) {
        elements.stationFormStatus.textContent = '';
    }
}

function selectedManagedStation() {
    const stationId = elements.stationDeleteSelect?.value || '';
    if (!stationId) return null;
    return state.stations.find(item => item.id === stationId) || null;
}

function populateManagedStationFields() {
    const station = selectedManagedStation();
    const hasStation = !!station;
    if (elements.stationExistingFields) {
        elements.stationExistingFields.classList.toggle('hidden', !hasStation);
    }
    if (elements.stationExistingUrl) {
        elements.stationExistingUrl.value = hasStation ? (station.input_url || station.stream_url || '') : '';
    }
    if (elements.stationExistingImageUrl) {
        elements.stationExistingImageUrl.value = hasStation ? (station.custom_image_url || '') : '';
    }
}

function resetManagedStationForm() {
    if (elements.stationDeleteSelect) {
        elements.stationDeleteSelect.value = '';
    }
    populateManagedStationFields();
    updateStationActionButtons();
}

function setupStationActions() {
    elements.stationSaveBtn.addEventListener('click', () => saveStation());
    if (elements.stationUpdateBtn) {
        elements.stationUpdateBtn.addEventListener('click', saveManagedStationChanges);
    }
    elements.stationDeleteBtn.addEventListener('click', deleteSelectedStation);
    elements.toggleStationManageBtn.addEventListener('click', () => toggleStationManagePanel(true));
    elements.closeStationManageBtn.addEventListener('click', () => toggleStationManagePanel(false));
    elements.radioManagePanel.querySelector('.manage-overlay-backdrop').addEventListener('click', () => toggleStationManagePanel(false));
    if (elements.stationUrl) {
        elements.stationUrl.addEventListener('input', () => {
            clearStationFormStatus();
            updateStationNameRequirement();
        });
        elements.stationUrl.addEventListener('paste', () => {
            requestAnimationFrame(() => handleStationUrlReady('Pasted station URL'));
        });
        elements.stationUrl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                handleStationUrlReady('Entered station URL');
            }
        });
    }
    if (elements.stationName) {
        elements.stationName.addEventListener('input', () => {
            clearStationFormStatus();
            updateStationActionButtons();
        });
        elements.stationName.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !elements.stationSaveBtn?.disabled) {
                e.preventDefault();
                saveStation();
            }
        });
    }
    if (elements.stationImageUrl) {
        elements.stationImageUrl.addEventListener('input', clearStationFormStatus);
    }
    if (elements.stationDeleteSelect) {
        elements.stationDeleteSelect.addEventListener('change', () => {
            populateManagedStationFields();
            updateStationActionButtons();
        });
    }
    if (elements.stationExistingUrl) {
        elements.stationExistingUrl.addEventListener('input', updateStationActionButtons);
    }
    if (elements.stationExistingImageUrl) {
        elements.stationExistingImageUrl.addEventListener('input', clearStationFormStatus);
    }
    if (elements.stationUrlDropArea) setupStationUrlDropArea();
    updateStationNameRequirement();
}
function toggleStationManagePanel(forceOpen = null) {
    const shouldOpen = forceOpen === null
        ? elements.radioManagePanel.classList.contains('hidden')
        : !!forceOpen;
    elements.radioManagePanel.classList.toggle('hidden', !shouldOpen);
    resetStationForm();
    resetManagedStationForm();
    if (shouldOpen) {
        elements.closeStationManageBtn?.focus();
    }
}
async function fetchStations() {
    try {
        const resp = await fetch('/api/stations');
        if (!resp.ok) throw new Error('Failed to fetch stations');
        state.stations = await resp.json();
        renderStations();
        renderStationDeleteOptions();
    } catch (e) {
        showToast('Failed to load stations', 'error');
    }
}
function stationArtFallbackSvg(station) {
    const title = station.title || station.name || 'Radio';
    const genre = station.artist || 'Radio';
    const seed = `${station.id || ''}-${title}`;
    let hash = 0;
    for (let i = 0; i < seed.length; i++) hash = ((hash << 5) - hash) + seed.charCodeAt(i);
    const palettes = [
        ['#6ee7b7', '#065f46', '#d1fae5'],
        ['#93c5fd', '#1e3a8a', '#dbeafe'],
        ['#c4b5fd', '#4c1d95', '#ede9fe'],
        ['#f9a8d4', '#9d174d', '#fce7f3'],
        ['#fcd34d', '#92400e', '#fef3c7'],
        ['#67e8f9', '#155e75', '#cffafe']
    ];
    const [bg, fg, accent] = palettes[Math.abs(hash) % palettes.length];
    const words = title.split(/\s+/).filter(Boolean);
    const initials = (words[0]?.[0] || '') + (words[1]?.[0] || words[0]?.[1] || '');
    const label = (initials || 'R').toUpperCase();
    const chip = escapeHtml((genre || 'Radio').slice(0, 16));
    const svg = `
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
            <defs>
                <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" stop-color="${bg}"/>
                    <stop offset="100%" stop-color="${fg}"/>
                </linearGradient>
                <radialGradient id="glow" cx="30%" cy="22%" r="75%">
                    <stop offset="0%" stop-color="rgba(255,255,255,0.28)"/>
                    <stop offset="100%" stop-color="rgba(255,255,255,0)"/>
                </radialGradient>
            </defs>
            <rect width="128" height="128" rx="24" fill="url(#g)"/>
            <rect x="1.5" y="1.5" width="125" height="125" rx="22.5" fill="none" stroke="rgba(255,255,255,0.10)"/>
            <rect width="128" height="128" rx="24" fill="url(#glow)"/>
            <circle cx="96" cy="28" r="9" fill="rgba(255,255,255,0.16)"/>
            <circle cx="96" cy="28" r="3.5" fill="${accent}" fill-opacity="0.9"/>
            <g fill="none" stroke="rgba(255,255,255,0.32)" stroke-width="3" stroke-linecap="round">
                <path d="M28 97c9-9 25-9 34 0"/>
                <path d="M22 90c13-13 34-13 47 0"/>
                <path d="M16 83c17-18 43-18 59 0"/>
            </g>
            <text x="64" y="66" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="42" font-weight="800" letter-spacing="1" fill="white">${label}</text>
            <rect x="30" y="88" width="68" height="18" rx="9" fill="rgba(12,16,24,0.22)" stroke="rgba(255,255,255,0.14)"/>
            <text x="64" y="100.5" text-anchor="middle" font-family="Inter, Arial, sans-serif" font-size="10.5" font-weight="700" fill="${accent}">${chip}</text>
        </svg>`;
    return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
}

function inferSomaStationSlug(station) {
    const inputUrl = (station?.input_url || station?.url || '').trim();
    const match = inputUrl.match(/somafm\.com\/([^/?#]+)/i);
    if (match && match[1]) {
        return match[1].replace(/(256|130)?\.pls$/i, '').trim().toLowerCase();
    }
    const title = (station?.title || station?.name || '').trim().toLowerCase();
    const knownSlugs = {
        'groove salad': 'groovesalad',
        'suburbs of goa': 'suburbsofgoa',
        'the trip': 'thetrip',
        'poptron': 'poptron',
        'dub step beyond': 'dubstep',
        'dubstep beyond': 'dubstep',
        'somafm live': 'live',
        'groove salad classic': 'gsclassic',
        'seven inch soul': '7soul',
    };
    return knownSlugs[title] || '';
}

function stationArtCandidates(station) {
    const seen = new Set();
    const candidates = [];
    const push = (value) => {
        const cleaned = (value || '').trim();
        if (!cleaned || seen.has(cleaned)) return;
        seen.add(cleaned);
        candidates.push(cleaned);
    };

    push(station.custom_image_url);
    push(station.image_url);
    push(station.logo_url);
    push(station.logo);
    push(station.image);

    const somaSlug = inferSomaStationSlug(station);
    if (somaSlug) {
        push(`/static/station-art/${somaSlug}.png`);
        push(`/static/station-art/${somaSlug}.jpg`);
        push(`/static/station-art/${somaSlug}.jpeg`);
        push(`/static/station-art/${somaSlug}.webp`);
    }

    push(stationArtFallbackSvg(station));
    return candidates;
}

function stationArtUrl(station) {
    return stationArtCandidates(station)[0] || stationArtFallbackSvg(station);
}

function renderStations() {
    const loadingEl = document.querySelector('#tab-radio .loading');
    if (state.stations.length === 0) {
        if (loadingEl) loadingEl.textContent = 'No stations yet. Open Manage to add one.';
        elements.stationsGrid.innerHTML = '';
        renderStationDeleteOptions();
        return;
    }
    if (loadingEl) loadingEl.style.display = 'none';
    elements.stationsGrid.innerHTML = state.stations.map(station => {
        const artCandidates = stationArtCandidates(station);
        const artSrc = artCandidates[0] || stationArtFallbackSvg(station);
        const isFallbackArt = artSrc.startsWith('data:image/svg+xml');
        const wrapClass = isFallbackArt ? 'station-art-wrap station-art-wrap--fallback' : 'station-art-wrap station-art-wrap--real';
        const imgClass = isFallbackArt ? 'station-art station-art--fallback' : 'station-art station-art--real';
        return `
        <div class="station-card" data-station-id="${escapeHtml(station.id)}" role="button" tabindex="0">
            <div class="${wrapClass}">
                <img class="${imgClass}" src="${escapeHtml(artSrc)}" data-art-candidates="${escapeHtml(JSON.stringify(artCandidates))}" data-art-index="0" alt="${escapeHtml(station.title)}" loading="lazy" />
            </div>
            <div class="station-name">${escapeHtml(station.title)}</div>
        </div>`;
    }).join('');
    elements.stationsGrid.querySelectorAll('.station-card').forEach(card => {
        card.addEventListener('click', () => playRadio(card.dataset.stationId));
    });
    elements.stationsGrid.querySelectorAll('.station-art').forEach(img => {
        img.addEventListener('error', () => {
            let candidates = [];
            try {
                candidates = JSON.parse(img.dataset.artCandidates || '[]');
            } catch {}
            const currentIndex = Number(img.dataset.artIndex || 0);
            const nextIndex = Number.isFinite(currentIndex) ? currentIndex + 1 : 1;
            const nextSrc = candidates[nextIndex];
            if (nextSrc) {
                img.dataset.artIndex = String(nextIndex);
                img.src = nextSrc;
            }
        });
    });
    highlightActiveTrack();
}
function updateStationActionButtons() {
    const value = (elements.stationUrl?.value || '').trim();
    const name = (elements.stationName?.value || '').trim();
    const isSoma = isSomaFmUrl(value);
    if (elements.stationSaveBtn) {
        elements.stationSaveBtn.disabled = !value || (!isSoma && !name);
    }
    const hasManagedStation = !!selectedManagedStation();
    const managedUrl = (elements.stationExistingUrl?.value || '').trim();
    if (elements.stationUpdateBtn) {
        elements.stationUpdateBtn.disabled = !hasManagedStation || !managedUrl;
    }
    if (elements.stationDeleteBtn) {
        elements.stationDeleteBtn.disabled = !hasManagedStation;
    }
}

function renderStationDeleteOptions() {
    if (!elements.stationDeleteSelect) return;
    if (state.stations.length === 0) {
        elements.stationDeleteSelect.innerHTML = '<option value="">No stations saved yet</option>';
        resetManagedStationForm();
        return;
    }
    elements.stationDeleteSelect.innerHTML = ['<option value="">Select a station…</option>']
        .concat(state.stations.map(station => `<option value="${escapeHtml(station.id)}">${escapeHtml(station.title)}</option>`))
        .join('');
    resetManagedStationForm();
}
function isSomaFmUrl(value) {
    return /https?:\/\/(?:[^/]*\.)?somafm\.com\//i.test((value || '').trim()) || /https?:\/\/[^\s]*somafm\.com\//i.test((value || '').trim());
}

function updateStationNameRequirement() {
    const value = (elements.stationUrl?.value || '').trim();
    const hasUrl = !!value;
    const isSoma = isSomaFmUrl(value);
    const needsManualName = hasUrl && !isSoma;
    if (!needsManualName && elements.stationImageUrl) {
        elements.stationImageUrl.value = '';
    }
    if (elements.stationNameGroup) {
        elements.stationNameGroup.classList.toggle('hidden', !needsManualName);
    }
    if (elements.stationSaveRow) {
        elements.stationSaveRow.classList.toggle('hidden', !needsManualName);
    }
    if (elements.stationImageGroup) {
        elements.stationImageGroup.classList.toggle('hidden', !needsManualName);
    }
    if (elements.stationUrlHint) {
        elements.stationUrlHint.textContent = !hasUrl
            ? 'SomaFM adds directly.'
            : isSoma
                ? 'SomaFM detected. It will be added directly.'
                : 'Other stream detected. Enter a name below, cover URL optional.';
    }
    updateStationActionButtons();
}

function setStationUrlValue(url, sourceLabel = '') {
    const cleaned = (url || '').trim();
    if (!cleaned || !elements.stationUrl) return;
    elements.stationUrl.value = cleaned;
    clearStationFormStatus();
    if (elements.stationUrlHint) {
        elements.stationUrlHint.textContent = sourceLabel ? `${sourceLabel}: ${cleaned}` : cleaned;
    }
    updateStationNameRequirement();
    elements.stationUrl.focus();
}

async function handleStationUrlReady(sourceLabel = '') {
    const value = (elements.stationUrl?.value || '').trim();
    const match = value.match(/https?:\/\/\S+/i);
    if (!match) {
        return;
    }
    setStationUrlValue(match[0], sourceLabel || 'Station URL');
    if (isSomaFmUrl(match[0])) {
        await saveStation(match[0]);
        return;
    }
    if (elements.stationNameGroup) elements.stationNameGroup.classList.remove('hidden');
    if (elements.stationImageGroup) elements.stationImageGroup.classList.remove('hidden');
    if (elements.stationSaveRow) elements.stationSaveRow.classList.remove('hidden');
    elements.stationName?.focus();
}

function setupStationUrlDropArea() {
    const area = elements.stationUrlDropArea;
    if (!area) return;
    const activate = () => elements.stationUrl?.focus();
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
        setStationUrlValue(url, 'Dropped station URL');
        await handleStationUrlReady('Dropped station URL');
    });
}

function resetStationForm() {
    elements.stationName.value = '';
    if (elements.stationImageUrl) elements.stationImageUrl.value = '';
    elements.stationUrl.value = '';
    clearStationFormStatus();
    updateStationNameRequirement();
    if (elements.stationDeleteSelect) {
        updateStationActionButtons();
    }
}
async function saveStation(urlOverride = null) {
    const name = elements.stationName.value.trim();
    const streamUrl = (urlOverride || elements.stationUrl.value || '').trim();
    const customImageUrl = (elements.stationImageUrl?.value || '').trim();
    const soma = isSomaFmUrl(streamUrl);
    if (!streamUrl) {
        showToast('Please enter a station URL', 'error');
        return;
    }
    if (!name && !soma) {
        showToast('Please enter a station name for non-SomaFM streams', 'error');
        return;
    }
    elements.stationSaveBtn.disabled = true;
    elements.stationFormStatus.textContent = soma ? 'Adding SomaFM station…' : 'Adding station…';
    try {
        const resp = await fetch('/api/stations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name || '', stream_url: streamUrl, custom_image_url: customImageUrl || '' }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save station');
        await fetchStations();
        resetStationForm();
        showToast(`Added station: ${data.station?.title || name || 'Station'}`, 'success');
    } catch (e) {
        elements.stationFormStatus.textContent = e.message || 'Failed to save station';
        showToast(e.message || 'Failed to save station', 'error');
    } finally {
        elements.stationSaveBtn.disabled = false;
    }
}
async function saveManagedStationChanges() {
    const station = selectedManagedStation();
    const streamUrl = (elements.stationExistingUrl?.value || '').trim();
    const customImageUrl = (elements.stationExistingImageUrl?.value || '').trim();
    if (!station) {
        showToast('Please select a station to edit', 'error');
        return;
    }
    if (!streamUrl) {
        showToast('Please enter a station URL', 'error');
        return;
    }
    const nextName = isSomaFmUrl(streamUrl) ? '' : (station.title || '');
    if (elements.stationUpdateBtn) elements.stationUpdateBtn.disabled = true;
    if (elements.stationDeleteBtn) elements.stationDeleteBtn.disabled = true;
    clearStationFormStatus();
    try {
        const resp = await fetch(`/api/stations/${encodeURIComponent(station.id)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: nextName, stream_url: streamUrl, custom_image_url: customImageUrl || '' }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to update station');
        await fetchStations();
        showToast(`Updated station: ${data.station?.title || station.title}`, 'success');
    } catch (e) {
        showToast(e.message || 'Failed to update station', 'error');
    } finally {
        updateStationActionButtons();
    }
}

async function deleteSelectedStation() {
    const stationId = elements.stationDeleteSelect.value;
    const station = state.stations.find(item => item.id === stationId);
    if (!stationId || !station) {
        showToast('Please select a station to delete', 'error');
        return;
    }
    if (!confirm(`Delete station "${station.title}"?`)) return;
    try {
        const resp = await fetch(`/api/stations/${encodeURIComponent(stationId)}`, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to delete station');
        await fetchStations();
        showToast(`Deleted station: ${station.title}`, 'success');
    } catch (e) {
        showToast(e.message || 'Failed to delete station', 'error');
    }
}
async function fetchTracks() {
    try {
        const resp = await fetch('/api/tracks');
        if (!resp.ok) throw new Error('Failed to fetch tracks');
        state.library.tracks = await resp.json();
        state.library.scanning = false;
        renderTracks();
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
        renderTracks();
    } catch (e) {
        console.debug('Failed to fetch playlists', e);
    }
}
function getFilteredTracks() {
    const tracks = state.library.tracks || [];
    const query = (state.library.searchQuery || '').trim().toLowerCase();
    if (!query) return tracks;
    return tracks.filter(track => {
        const haystack = [track.title, track.artist, track.path, track.url, track.id]
            .filter(Boolean)
            .join(' ')
            .toLowerCase();
        return haystack.includes(query);
    });
}
function renderTracks() {
    const allTracks = state.library.tracks || [];
    const filteredTracks = getFilteredTracks();
    const validSelectedIds = allTracks.length > 0
        ? state.library.selectedTrackIds.filter(id => allTracks.some(track => track.id === id))
        : state.library.selectedTrackIds;
    const selectedIds = new Set(validSelectedIds);
    state.library.selectedTrackIds = Array.from(selectedIds);
    const loadingEl = document.querySelector('#tab-library .loading');

    if (allTracks.length === 0) {
        if (loadingEl) { loadingEl.textContent = 'No tracks yet. Import a file or URL to get started.'; loadingEl.style.display = ''; }
        elements.tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }
    if (filteredTracks.length === 0) {
        if (loadingEl) { loadingEl.textContent = 'No matching tracks. Try a broader search.'; loadingEl.style.display = ''; }
        elements.tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }
    if (loadingEl) loadingEl.style.display = 'none';

    const hasSearch = !!(state.library.searchQuery || '').trim();
    let html = '';

    // Playlist items at top (only when no search active)
    if (!hasSearch && state.playlists.length > 0) {
        html += state.playlists.map(playlist => {
            const classes = ['track-item', 'playlist-item'];
            return `<div class="${classes.join(' ')}" data-playlist-id="${escapeHtml(playlist.id)}">
                <button class="track-play-button" data-playlist-id="${escapeHtml(playlist.id)}" type="button">
                    <span class="track-item-icon">📋</span>
                    <div class="track-title">${escapeHtml(playlist.name)}</div>
                    <div class="track-artist">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
                </button>
                <button class="playlist-delete-btn" data-playlist-delete="${escapeHtml(playlist.id)}" type="button" title="Delete playlist">🗑</button>
            </div>`;
        }).join('');
    }

    // Track items
    html += filteredTracks.map(track => {
        const isSelected = selectedIds.has(track.id);
        const artist = (track.artist || '').trim();
        return `
            <div class="track-item ${isSelected ? 'selected' : ''}" data-track-id="${escapeHtml(track.id)}">
                <label class="track-select">
                    <input type="checkbox" class="track-checkbox" data-track-id="${escapeHtml(track.id)}" ${isSelected ? 'checked' : ''}>
                    <span class="track-select-box"></span>
                </label>
                <button class="track-play-button" data-track-id="${escapeHtml(track.id)}" type="button">
                    <span class="track-item-icon">♫</span>
                    <div class="track-title">${escapeHtml(track.title)}</div>
                    ${artist ? `<div class="track-artist">${escapeHtml(artist)}</div>` : ''}
                </button>
            </div>
        `;
    }).join('');

    elements.tracksList.innerHTML = html;

    elements.tracksList.querySelectorAll('.track-play-button[data-track-id]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            playLocal(item.dataset.trackId);
        });
    });

    elements.tracksList.querySelectorAll('.track-play-button[data-playlist-id]').forEach(item => {
        item.addEventListener('click', async (e) => {
            e.stopPropagation();
            await loadPlaylistById(item.dataset.playlistId, { autoplay: true });
        });
    });

    elements.tracksList.querySelectorAll('.track-checkbox').forEach(input => {
        input.addEventListener('change', () => toggleTrackSelection(input.dataset.trackId, input.checked));
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
function toggleTrackSelection(trackId, selected) {
    const selectedIds = new Set(state.library.selectedTrackIds);
    if (selected) {
        selectedIds.add(trackId);
    } else {
        selectedIds.delete(trackId);
    }
    state.library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    scheduleActiveLocalQueueSync();
}
function clearTrackSelection() {
    state.library.selectedTrackIds = [];
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    scheduleActiveLocalQueueSync();
}
function selectAllVisibleTracks() {
    const selectedIds = new Set(state.library.selectedTrackIds);
    getFilteredTracks().forEach(track => selectedIds.add(track.id));
    state.library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    scheduleActiveLocalQueueSync();
}
function clearVisibleTrackSelection() {
    const visibleIds = new Set(getFilteredTracks().map(track => track.id));
    state.library.selectedTrackIds = state.library.selectedTrackIds.filter(id => !visibleIds.has(id));
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    scheduleActiveLocalQueueSync();
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
    renderTracks();
}
function syncRenderedTrackSelection() {
    const selectedIds = new Set(state.library.selectedTrackIds);
    elements.tracksList.querySelectorAll('.track-item').forEach(item => {
        item.classList.toggle('selected', selectedIds.has(item.dataset.trackId));
    });
    elements.tracksList.querySelectorAll('.track-checkbox').forEach(input => {
        input.checked = selectedIds.has(input.dataset.trackId);
    });
}
function updatePlaylistSaveRowVisibility() {
    if (!elements.playlistSaveRow) return;
    const count = state.library.selectedTrackIds.length;
    elements.playlistSaveRow.classList.toggle('hidden', count < 2);
}
function updateLibrarySelectionUI() {
    const allTracks = state.library.tracks || [];
    const filteredTracks = getFilteredTracks();
    const selectedIds = new Set(state.library.selectedTrackIds);
    const visibleIds = filteredTracks.map(track => track.id);
    const selectedVisibleCount = visibleIds.filter(id => selectedIds.has(id)).length;
    const totalSelectedCount = selectedIds.size;
    const hasSearch = !!(state.library.searchQuery || '').trim();

    if (elements.downloadSelectedTracksBtn) {
        elements.downloadSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
        elements.downloadSelectedTracksBtn.disabled = totalSelectedCount === 0 || state.library.selectionDownloadPending;
    }
    if (elements.deleteSelectedTracksBtn) {
        elements.deleteSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
    }
    if (elements.playSelectedTracksBtn) {
        elements.playSelectedTracksBtn.disabled = totalSelectedCount === 0;
    }
    if (elements.selectAllTracksBtn) {
        const allVisibleSelected = filteredTracks.length > 0 && selectedVisibleCount === filteredTracks.length;
        elements.selectAllTracksBtn.disabled = filteredTracks.length === 0;
        if (allVisibleSelected) {
            elements.selectAllTracksBtn.textContent = hasSearch ? 'Clear visible' : 'Clear selection';
        } else {
            elements.selectAllTracksBtn.textContent = hasSearch ? 'Select visible' : 'Select all';
        }
    }
    if (elements.libraryInfo) {
        const baseText = hasSearch
            ? `${filteredTracks.length} of ${allTracks.length} tracks`
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
    elements.tracksList.innerHTML = '<div class="loading">Refreshing library…</div>';
    try {
        const resp = await fetch('/api/library/refresh', { method: 'POST' });
        const data = await resp.json();
        if (data.status === 'scanning') {
            setTimeout(fetchTracks, 2000);
        } else {
            await fetchTracks();
        }
    } catch (e) {
        showToast('Failed to refresh library', 'error');
        state.library.scanning = false;
    }
}
function uploadTrackFile() {
    const file = elements.uploadTrackFile.files[0];
    if (!file) {
        showToast('Please choose an audio file or ZIP', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('file', file);
    if (elements.uploadTrackBtn) elements.uploadTrackBtn.disabled = true;
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
        if (elements.uploadTrackBtn) elements.uploadTrackBtn.disabled = false;
    });
    xhr.addEventListener('error', () => {
        resetUploadAreaSelection('upload-track-file');
        state.upload = { filename, status_text: 'Upload failed', progress_percent: 0, status: 'error' };
        updateDownloadUI();
        showToast('Upload failed', 'error');
        if (elements.uploadTrackBtn) elements.uploadTrackBtn.disabled = false;
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
    if (trackIds.length < 2) {
        showToast('Select at least 2 tracks', 'error');
        return;
    }
    elements.savePlaylistBtn.disabled = true;
    try {
        const resp = await fetch('/api/playlists', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save playlist');
        if (elements.playlistName) elements.playlistName.value = '';
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
    state.library.selectedTrackIds = validTrackIds;
    state.library.searchQuery = '';
    if (elements.librarySearchInput) elements.librarySearchInput.value = '';
    renderTracks();
    const missingCount = playlist.track_ids.length - validTrackIds.length;
    if (autoplay) {
        if (missingCount > 0) {
            showToast(`Starting ${validTrackIds.length}/${playlist.track_ids.length} tracks from ${playlist.name}`, 'info');
        }
        await playLocal(validTrackIds[0]);
        return;
    }
    showToast(missingCount > 0
        ? `Loaded ${validTrackIds.length}/${playlist.track_ids.length} tracks from ${playlist.name}`
        : `Loaded: ${playlist.name}`, 'info');
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
    const header = resp.headers.get('Content-Disposition') || '';
    const utf8Match = header.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match && utf8Match[1]) {
        try {
            return decodeURIComponent(utf8Match[1]);
        } catch (_) {
            return utf8Match[1];
        }
    }
    const plainMatch = header.match(/filename="?([^";]+)"?/i);
    if (plainMatch && plainMatch[1]) return plainMatch[1];
    return fallbackName;
}
function triggerBlobDownload(blob, filename) {
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename || 'download';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
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
    state.playback.current_track = {
        id: `radio_${station.id}`,
        title: station.title,
        artist: station.artist || 'SomaFM',
        source: 'radio',
        url: station.stream_url,
    };
    state.playback.live_title = null;
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
        if (!resp.ok) throw new Error(data.detail || 'Play command failed');
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        if (data.playback) {
            mergePlaybackState(data.playback);
        }
        updatePlaybackUI();
        triggerSamplerateBurstPolling();
        showToast(`Now playing: ${station.title}`, 'info');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        state.playback.playing = false;
        state.playback.paused = false;
        updatePlaybackUI();
        showToast('Failed to start playback', 'error');
    }
}
async function playLocal(trackId) {
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
    const selectedTrackIds = getSelectedPlayableTrackIds();
    const shouldUseSelectionQueue = selectedTrackIds.length > 1 && selectedTrackIds.includes(trackId);
    const requestId = ++pendingPlaybackRequestId;
    pendingFooterSingleTrackStart = shouldUseSelectionQueue ? null : {
        requestId,
        trackId: track.id,
        expiresAt: Date.now() + FOOTER_SINGLE_TRACK_START_LOCK_MS,
    };
    playbackActionInFlight = true;
    armLocalFooterHold();
    armFooterContentFreeze();
    libraryModeSyncArmed = true;
    state.playback.current_track = track;
    state.playback.live_title = null;
    state.playback.playing = true;
    state.playback.paused = false;
    state.playback.queue = shouldUseSelectionQueue
        ? {
            active: true,
            index: Math.max(0, selectedTrackIds.indexOf(track.id)),
            count: selectedTrackIds.length,
            mode: state.playback.queue?.mode || 'app_replace',
            tracks: selectedTrackIds
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
                queue_track_ids: shouldUseSelectionQueue ? selectedTrackIds : undefined,
                shuffle: !!state.library.shuffle,
                loop: !!state.library.loop,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Play command failed');
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        updatePlaybackUI();
        triggerSamplerateBurstPolling();
        const queueCount = (((data || {}).playback || {}).queue || {}).count || 0;
        showToast(queueCount > 1 ? `Queue started: ${track.title} (${queueCount} tracks)` : `Now playing: ${track.title}`, 'info');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
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
    return match ? match[0].trim() : '';
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

async function readTextFile(file) {
    return await file.text();
}

function getDualFilterFileKind(file) {
    const name = (file?.name || '').toLowerCase();
    if (name.endsWith('.txt')) return 'rew-text';
    if (name.endsWith('.irs') || name.endsWith('.wav')) return 'convolver';
    return null;
}

async function populateDualFilterTextareaFromFile(side, file) {
    if (!file) return;
    const kind = getDualFilterFileKind(file);
    const target = side === 'left' ? elements.effectsRewLeftText : elements.effectsRewRightText;
    if (!target) return;
    if (kind !== 'rew-text') {
        target.value = '';
        return;
    }
    try {
        target.value = await readTextFile(file);
    } catch (e) {
        showToast(`Failed to read ${side} filter text file`, 'error');
    }
}

async function createDualFilterPreset() {
    const presetName = elements.effectsRewDualPresetName?.value?.trim() || '';
    const leftText = elements.effectsRewLeftText?.value?.trim() || '';
    const rightText = elements.effectsRewRightText?.value?.trim() || '';
    const leftFile = elements.effectsRewLeftFile?.files?.[0] || null;
    const rightFile = elements.effectsRewRightFile?.files?.[0] || null;
    const leftFileKind = getDualFilterFileKind(leftFile);
    const rightFileKind = getDualFilterFileKind(rightFile);
    const usingDualFiles = !!leftFile && !!rightFile;
    const usingDualConvolverFiles = usingDualFiles && leftFileKind === 'convolver' && rightFileKind === 'convolver';

    if (!presetName) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please enter a preset name.</div>';
        showToast('Please enter a preset name', 'error');
        elements.effectsRewDualPresetName?.focus();
        return;
    }
    if (usingDualFiles && leftFileKind !== rightFileKind) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Use the same file type on Left and Right.</div>';
        showToast('Use the same file type on Left and Right', 'error');
        return;
    }
    if (!!leftFile !== !!rightFile) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Provide both Left and Right files.</div>';
        showToast('Provide both Left and Right files', 'error');
        return;
    }
    if (!usingDualConvolverFiles && !leftText) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please provide Left filter text or file.</div>';
        showToast('Please provide Left filter text or file', 'error');
        elements.effectsRewLeftText?.focus();
        return;
    }
    if (!usingDualConvolverFiles && !rightText) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please provide Right filter text or file.</div>';
        showToast('Please provide Right filter text or file', 'error');
        elements.effectsRewRightText?.focus();
        return;
    }

    if (elements.effectsRewDualCreatePresetBtn) elements.effectsRewDualCreatePresetBtn.disabled = true;
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Creating dual filter preset: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    try {
        const extras = collectEffectsExtras();
        const formData = new FormData();
        formData.append('preset_name', presetName);
        formData.append('left_text', leftText);
        formData.append('right_text', rightText);
        formData.append('load_after_create', 'false');
        formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
        formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
        formData.append('headroom_gain_db', String(extras.headroomGainDb));
        formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
        formData.append('autogain_target_db', String(extras.autogainTargetDb));
        formData.append('delay_enabled', extras.delayEnabled ? 'true' : 'false');
        formData.append('delay_left_ms', String(extras.delayLeftMs));
        formData.append('delay_right_ms', String(extras.delayRightMs));
        formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
        formData.append('bass_amount', String(extras.bassAmount));
        formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
        formData.append('tone_effect_mode', extras.toneEffectMode);
        if (leftFile) formData.append('left_file', leftFile);
        if (rightFile) formData.append('right_file', rightFile);

        const resp = await fetch('/api/easyeffects/presets/import-filter-dual', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Dual filter import failed');
        await fetchEffects();
        if (elements.effectsRewLeftText) elements.effectsRewLeftText.value = '';
        if (elements.effectsRewRightText) elements.effectsRewRightText.value = '';
        if (elements.effectsRewLeftFile) elements.effectsRewLeftFile.value = '';
        if (elements.effectsRewRightFile) elements.effectsRewRightFile.value = '';
        const leftFilename = document.getElementById('effects-rew-left-filename');
        const rightFilename = document.getElementById('effects-rew-right-filename');
        if (leftFilename) leftFilename.textContent = '';
        if (rightFilename) rightFilename.textContent = '';
        if (elements.effectsRewDualPresetName) elements.effectsRewDualPresetName.value = '';
        const importedKind = data.import_kind === 'dual-convolver' ? 'Dual convolver' : 'Dual PEQ';
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        showToast(`Created ${importedKind.toLowerCase()} preset: ${data.preset.name}`, 'success');
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message || 'Dual filter import failed')}</div>`;
        showToast(e.message || 'Dual filter import failed', 'error');
    } finally {
        if (elements.effectsRewDualCreatePresetBtn) elements.effectsRewDualCreatePresetBtn.disabled = false;
    }
}

function updateEffectsPeqDisclosureLabel() {
    if (!elements.effectsPeqDisclosureMeta || !elements.effectsPeqDisclosure) return;
    const leftCount = state.easyeffects?.peqDraft?.leftBands?.length || 0;
    const rightCount = state.easyeffects?.peqDraft?.rightBands?.length || 0;
    const actionLabel = elements.effectsPeqDisclosure.open ? 'Collapse' : 'Expand';
    elements.effectsPeqDisclosureMeta.textContent = `L${leftCount} · R${rightCount} · ${actionLabel}`;
}

function setupEffectsActions() {
    if (elements.refreshEffectsBtn) elements.refreshEffectsBtn.addEventListener('click', fetchEffects);
    if (elements.effectsDeleteBtn) elements.effectsDeleteBtn.addEventListener('click', deleteEffectsPreset);
    if (elements.effectsToggleImportBtn) {
        elements.effectsToggleImportBtn.addEventListener('click', () => {
            const shouldOpen = elements.effectsImportPanel?.classList.contains('hidden');
            setEffectsImportPanelOpen(!!shouldOpen);
        });
    }
    if (elements.effectsImportFile) elements.effectsImportFile.addEventListener('change', handleEffectsImportFileChange);
    if (elements.effectsRewDualCreatePresetBtn) elements.effectsRewDualCreatePresetBtn.addEventListener('click', createDualFilterPreset);
    if (elements.effectsCombinePreset1) {
        elements.effectsCombinePreset1.addEventListener('change', (event) => {
            state.easyeffects.combineDraft = state.easyeffects.combineDraft || getDefaultEffectsCombineDraft();
            state.easyeffects.combineDraft.preset1 = event.target.value;
            if (state.easyeffects.combineDraft.preset1 && state.easyeffects.combineDraft.preset1 === state.easyeffects.combineDraft.preset2) {
                state.easyeffects.combineDraft.preset2 = '';
                if (elements.effectsCombinePreset2) elements.effectsCombinePreset2.value = '';
            }
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombinePreset2) {
        elements.effectsCombinePreset2.addEventListener('change', (event) => {
            state.easyeffects.combineDraft = state.easyeffects.combineDraft || getDefaultEffectsCombineDraft();
            state.easyeffects.combineDraft.preset2 = event.target.value;
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombinePreset3) {
        elements.effectsCombinePreset3.addEventListener('change', (event) => {
            state.easyeffects.combineDraft = state.easyeffects.combineDraft || getDefaultEffectsCombineDraft();
            state.easyeffects.combineDraft.preset3 = event.target.value;
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombinePresetName) {
        elements.effectsCombinePresetName.addEventListener('input', (event) => {
            state.easyeffects.combineDraft = state.easyeffects.combineDraft || getDefaultEffectsCombineDraft();
            state.easyeffects.combineDraft.presetName = event.target.value;
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombineSaveBtn) elements.effectsCombineSaveBtn.addEventListener('click', createCombinedEffectsPreset);
    if (elements.effectsPeqPresetName) elements.effectsPeqPresetName.addEventListener('input', (event) => {
        if (!state.easyeffects?.peqDraft) return;
        state.easyeffects.peqDraft.presetName = event.target.value;
    });
    if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.addEventListener('change', (event) => {
        if (!state.easyeffects?.peqDraft) return;
        state.easyeffects.peqDraft.eqMode = normalizePeqEqMode(event.target.value);
        event.target.value = state.easyeffects.peqDraft.eqMode;
    });
    if (elements.effectsPeqAddBandBtn) elements.effectsPeqAddBandBtn.addEventListener('click', addPeqBandPair);
    if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.addEventListener('click', createPeqPreset);
    // Track focus to avoid resetting input values while user is typing
    [
        elements.effectsHeadroomGainDb,
        elements.effectsAutogainTargetDb,
        elements.effectsDelayLeftMs,
        elements.effectsDelayRightMs,
        elements.effectsBassAmount,
        elements.effectsToneEffectMode,
    ].forEach(el => {
        if (!el) return;
        el.addEventListener('focus', () => _activeEditing.add(el));
        el.addEventListener('input', () => saveEffectsExtrasDebounced(EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS));
        el.addEventListener('change', () => saveEffectsExtrasDebounced(EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS));
        el.addEventListener('blur', () => {
            _activeEditing.delete(el);
            saveEffectsExtrasDebounced(0); // commit immediately on blur
        });
    });

    if (elements.effectsLimiterEnabled) elements.effectsLimiterEnabled.addEventListener('change', () => saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS));
    if (elements.effectsHeadroomEnabled) elements.effectsHeadroomEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (elements.effectsAutogainEnabled) elements.effectsAutogainEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (elements.effectsDelayEnabled) elements.effectsDelayEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (elements.effectsBassEnabled) elements.effectsBassEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    if (elements.effectsToneEffectEnabled) elements.effectsToneEffectEnabled.addEventListener('change', () => {
        updateEffectsExtrasUi();
        saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
    });
    loadSavedEffectsExtras();
    setupEffectsCompareActions();
    if (elements.effectsPeqDisclosure) {
        elements.effectsPeqDisclosure.addEventListener('toggle', updateEffectsPeqDisclosureLabel);
        updateEffectsPeqDisclosureLabel();
    }
    setupUploadArea('effects-import-area', 'effects-import-file', (file) => {
        console.log('upload area file selected:', file.name);
        submitEffectsImport();
    });
    setupUploadArea('effects-rew-left-area', 'effects-rew-left-file', (file) => {
        void populateDualFilterTextareaFromFile('left', file);
    });
    setupUploadArea('effects-rew-right-area', 'effects-rew-right-file', (file) => {
        void populateDualFilterTextareaFromFile('right', file);
    });
    updateEffectsImportUi();
    setEffectsImportPanelOpen(false);
    resetPeqDraft();
    renderEffectsCombine();
}
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
    const setDownloadButtonDisabled = (disabled) => {
        if (elements.downloadBtn) elements.downloadBtn.disabled = disabled;
    };
    if (!dl) {
        elements.downloadStatus.innerHTML = '';
        elements.downloadStatus.classList.add('hidden');
        elements.cancelDownloadBtn.classList.add('hidden');
        setDownloadButtonDisabled(false);
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
            setDownloadButtonDisabled(true);
        }
    } else if (dl.status === 'complete') {
        html = `<div style="color: var(--success);">${escapeHtml(dl.status_text || (state.upload ? 'Upload complete' : 'Download complete'))}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
        setDownloadButtonDisabled(false);
    } else if (dl.status === 'error') {
        html = `<div style="color: var(--danger);"><strong>${state.upload ? 'Upload failed' : 'Download failed'}</strong><br>${escapeHtml(dl.error || dl.status_text || 'Unknown error')}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
        setDownloadButtonDisabled(false);
    } else if (dl.status === 'cancelled') {
        html = `<div style="color: var(--text-secondary);">${state.upload ? 'Upload cancelled' : 'Download cancelled'}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
        setDownloadButtonDisabled(false);
    }
    elements.downloadStatus.innerHTML = html;
    elements.downloadStatus.classList.toggle('hidden', !html.trim());
}
function normalizeMeasurementTrace(trace = {}, index = 0) {
    const points = Array.isArray(trace.points)
        ? trace.points.filter(point => Array.isArray(point) && point.length === 2 && Number.isFinite(Number(point[0])) && Number.isFinite(Number(point[1]))).map(point => [Number(point[0]), Number(point[1])])
        : [];
    return {
        kind: String(trace.kind || 'measured'),
        label: String(trace.label || `Trace ${index + 1}`),
        color: String(trace.color || ['#6ee7b7', '#a78bfa', '#f59e0b', '#60a5fa'][index % 4]),
        role: String(trace.role || ''),
        points,
    };
}

function normalizeMeasurementEntry(measurement = {}, index = 0) {
    const traces = Array.isArray(measurement.traces) ? measurement.traces.map((trace, traceIndex) => normalizeMeasurementTrace(trace, traceIndex)).filter(trace => trace.points.length) : [];
    const reviewTraces = Array.isArray(measurement.review_traces) ? measurement.review_traces.map((trace, traceIndex) => normalizeMeasurementTrace(trace, traceIndex)).filter(trace => trace.points.length) : [];
    return {
        id: String(measurement.id || `measurement-${index + 1}`),
        name: String(measurement.name || `Measurement ${index + 1}`),
        created_at: String(measurement.created_at || ''),
        channel: String(measurement.channel || 'left'),
        input_device: measurement.input_device || {},
        calibration: measurement.calibration || {},
        summary: measurement.summary || {},
        review_summary: measurement.review_summary || {},
        analysis: measurement.analysis || {},
        storage_path: measurement.storage_path || '',
        traces,
        review_traces: reviewTraces,
    };
}

function normalizeMeasurementVisibility(measurements = [], previous = {}) {
    const next = {};
    measurements.forEach((measurement) => {
        if (typeof previous?.[measurement.id] === 'boolean') {
            next[measurement.id] = previous[measurement.id];
        } else {
            next[measurement.id] = false;
        }
    });
    return next;
}

function formatMeasurementDate(value) {
    if (!value) return 'Unknown date';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function normalizeMeasurementReviewVisibility(measurements = [], previous = {}) {
    const next = {};
    measurements.forEach((measurement) => {
        next[measurement.id] = typeof previous?.[measurement.id] === 'boolean' ? previous[measurement.id] : false;
    });
    return next;
}

function getVisibleMeasurementEntries() {
    const currentId = state.measurement.currentMeasurement?.id;
    return (state.measurement.measurements || []).filter(measurement => measurement.id !== currentId && state.measurement.visibilityById?.[measurement.id]);
}

function getCurrentMeasurementEntry() {
    return state.measurement.currentMeasurement ? normalizeMeasurementEntry(state.measurement.currentMeasurement, 0) : null;
}

function measurementReviewVisible(measurementId) {
    return !!state.measurement.reviewVisibilityById?.[measurementId];
}

function getMeasurementDisplayTraces(measurement = {}) {
    const reviewTraces = Array.isArray(measurement.review_traces) ? measurement.review_traces : [];
    if (reviewTraces.length) return reviewTraces;
    return Array.isArray(measurement.traces) ? measurement.traces : [];
}

function measurementSmoothingHalfWindowOctaves(mode = '1/6-oct') {
    switch (String(mode || '1/6-oct')) {
        case 'raw': return 0;
        case '1/1-oct': return 0.5;
        case '1/3-oct': return 1 / 6;
        case '1/6-oct':
        default:
            return 1 / 12;
    }
}

function smoothMeasurementTracePoints(points = [], mode = '1/6-oct') {
    if (!Array.isArray(points) || points.length < 5 || mode === 'raw') return points;
    const halfWindow = measurementSmoothingHalfWindowOctaves(mode);
    if (!(halfWindow > 0)) return points;
    return points.map(([frequency, level], index) => {
        const logFrequency = Math.log2(Math.max(1e-9, frequency));
        let weightedLevel = 0;
        let totalWeight = 0;
        for (let cursor = 0; cursor < points.length; cursor += 1) {
            const [neighborFrequency, neighborLevel] = points[cursor];
            const logDelta = Math.abs(Math.log2(Math.max(1e-9, neighborFrequency)) - logFrequency);
            if (logDelta > halfWindow * 2.2) continue;
            const distance = logDelta / halfWindow;
            const weight = Math.exp(-(distance * distance) * 1.7);
            weightedLevel += neighborLevel * weight;
            totalWeight += weight;
        }
        return [frequency, totalWeight > 0 ? weightedLevel / totalWeight : level];
    });
}

function trackFileUrl(trackId = '') {
    return `/api/tracks/file/${encodeURIComponent(String(trackId || ''))}`;
}

function measurementFileUrl(measurementId = '') {
    return `/api/measurements/${encodeURIComponent(String(measurementId || ''))}/file`;
}

function presetFileUrl(presetName = '') {
    return `/api/easyeffects/presets/${encodeURIComponent(String(presetName || ''))}/file`;
}

const measurementComparePalette = ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa', '#f87171', '#facc15'];
const measurementCurrentColor = '#22c55e';
const measurementPeqPalette = ['#60a5fa', '#f59e0b', '#f472b6', '#a78bfa'];
const measurementPeqTypes = ['bell', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass', 'notch', 'gain'];
const measurementPeqTypeLabels = {
    bell: 'Bell',
    low_shelf: 'Low shelf',
    high_shelf: 'High shelf',
    low_pass: 'Low pass',
    high_pass: 'High pass',
    notch: 'Notch',
    gain: 'Gain',
};

function getSavedMeasurementColor(index = 0) {
    return measurementComparePalette[index % measurementComparePalette.length] || '#60a5fa';
}

function getDefaultMeasurementPeqFilter(index = 0) {
    return {
        id: `measurement-peq-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        type: 'bell',
        freqHz: 1000,
        gainDb: 0,
        q: 1,
        color: measurementPeqPalette[index % measurementPeqPalette.length] || '#60a5fa',
    };
}

function getDefaultMeasurementPeqState() {
    return {
        enabled: false,
        filters: [],
        activeFilterId: null,
        dragFilterId: null,
    };
}

function ensureMeasurementPeqState() {
    if (!state.measurement) state.measurement = {};
    if (!state.measurement.peqAssistant || typeof state.measurement.peqAssistant !== 'object') {
        state.measurement.peqAssistant = getDefaultMeasurementPeqState();
    }
    if (!Array.isArray(state.measurement.peqAssistant.filters)) state.measurement.peqAssistant.filters = [];
    if (typeof state.measurement.peqAssistant.enabled !== 'boolean') state.measurement.peqAssistant.enabled = state.measurement.peqAssistant.filters.length > 0;
    return state.measurement.peqAssistant;
}

function getMeasurementPeqFilters() {
    return ensureMeasurementPeqState().filters;
}

function getMeasurementPeqActiveFilter() {
    const peq = ensureMeasurementPeqState();
    return peq.filters.find((filter) => filter.id === peq.activeFilterId) || null;
}

function focusMeasurementPeqPanelContext() {
    if (!elements.measurementPeqPanel || elements.measurementPeqPanel.classList.contains('hidden')) return;
    elements.measurementPeqPanel.focus({ preventScroll: true });
}

function isEditableMeasurementPeqTarget(target) {
    if (!(target instanceof Element)) return false;
    return !!target.closest('input, select, textarea, [contenteditable="true"]');
}

function handleMeasurementPeqNumberInputArrowKey(event) {
    const input = event.currentTarget;
    if (!(input instanceof HTMLInputElement) || input.type !== 'number') return;
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return;
    event.preventDefault();
    event.stopPropagation();
    if (event.key === 'ArrowUp') {
        input.stepUp();
    } else {
        input.stepDown();
    }
    input.dispatchEvent(new Event('input', { bubbles: true }));
}

function syncMeasurementPeqQInput(value) {
    const input = elements.measurementPeqEditor?.querySelector('#measurement-peq-q');
    if (input) input.value = Number(value).toFixed(2);
}

function stepActiveMeasurementPeqQ(direction = 1, step = 0.1) {
    const activeFilter = getMeasurementPeqActiveFilter();
    if (!activeFilter) return null;
    const nextValue = stepMeasurementPeqQ(activeFilter.id, direction, step);
    if (nextValue === null) return null;
    syncMeasurementPeqQInput(nextValue);
    scheduleMeasurementGraphRender();
    return nextValue;
}

function handleMeasurementPeqGraphWheel(event) {
    if (!elements.measurementPanel || elements.measurementPanel.classList.contains('hidden')) return;
    if (!elements.measurementPeqPanel || elements.measurementPeqPanel.classList.contains('hidden')) return;
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
    return Math.min(20000, Math.max(20, Number(value) || 20));
}

function clampMeasurementPeqGain(value) {
    return Math.min(24, Math.max(-24, Number(value) || 0));
}

function clampMeasurementPeqQ(value) {
    return Math.min(20, Math.max(0.1, Number(value) || 1));
}

function measurementXToFrequency(x, bounds) {
    const minLog = Math.log10(20);
    const maxLog = Math.log10(20000);
    const ratio = Math.min(1, Math.max(0, (x - bounds.left) / Math.max(1, bounds.width)));
    return 10 ** (minLog + ((maxLog - minLog) * ratio));
}

function measurementYToDb(y, bounds, range) {
    const ratio = Math.min(1, Math.max(0, (y - bounds.top) / Math.max(1, bounds.height)));
    return range.maxDb - (ratio * (range.maxDb - range.minDb));
}

function addMeasurementPeqFilter(defaults = {}) {
    const peq = ensureMeasurementPeqState();
    if (peq.filters.length >= 4) {
        showToast('Measurement assistant supports up to 4 filters', 'warning');
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
        freqHz: measurementXToFrequency(x, bounds),
        gainDb: measurementYToDb(y, bounds, range),
    });
}

function updateMeasurementPeqFilter(filterId, updates = {}) {
    const filter = getMeasurementPeqFilters().find((item) => item.id === filterId);
    if (!filter) return;
    if (updates.type) filter.type = measurementPeqTypes.includes(updates.type) ? updates.type : filter.type;
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

function resetMeasurementGraph() {
    const peq = ensureMeasurementPeqState();
    state.measurement.currentMeasurement = null;
    state.measurement.currentMeasurementSaved = false;
    state.measurement.currentMeasurementName = '';
    peq.enabled = false;
    peq.filters = [];
    peq.activeFilterId = null;
    peq.dragFilterId = null;
    renderMeasurementPanel();
    scheduleMeasurementGraphRender();
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
    if (!elements.measurementPeqTakeFeedback) return;
    elements.measurementPeqTakeFeedback.textContent = message;
    elements.measurementPeqTakeFeedback.classList.add('is-visible');
    if (measurementPeqTakeFeedbackTimer) clearTimeout(measurementPeqTakeFeedbackTimer);
    measurementPeqTakeFeedbackTimer = setTimeout(() => {
        elements.measurementPeqTakeFeedback?.classList.remove('is-visible');
        measurementPeqTakeFeedbackTimer = null;
    }, 2200);
}

function takeMeasurementPeqToPreset(mode = 'both') {
    const peq = ensureMeasurementPeqState();
    if (!peq.filters.length) {
        showToast('Add at least one measurement PEQ filter first', 'warning');
        return;
    }
    if (!state.easyeffects) state.easyeffects = {};
    state.easyeffects.peqDraft = state.easyeffects.peqDraft || getDefaultPeqDraft();
    const mappedBands = peq.filters.map((filter) => measurementPeqFilterToBand(filter));
    if (mode === 'left') {
        state.easyeffects.peqDraft.leftBands = mappedBands.map((band) => ({ ...band }));
    } else if (mode === 'right') {
        state.easyeffects.peqDraft.rightBands = mappedBands.map((band) => ({ ...band }));
    } else {
        state.easyeffects.peqDraft.leftBands = mappedBands.map((band) => ({ ...band }));
        state.easyeffects.peqDraft.rightBands = mappedBands.map((band) => ({ ...band }));
    }
    if (elements.effectsPeqDisclosure) elements.effectsPeqDisclosure.open = true;
    renderPeqBands();
    const successMessage = mode === 'left'
        ? 'Measurement PEQ replaced Left builder bands'
        : (mode === 'right' ? 'Measurement PEQ replaced Right builder bands' : 'Measurement PEQ replaced Left and Right builder bands');
    showMeasurementPeqTakeFeedback(mode === 'left' ? 'Left bands updated' : (mode === 'right' ? 'Right bands updated' : 'Left + Right bands updated'));
    showToast(successMessage, 'success');
}

function getMeasurementGraphBounds(displayWidth, displayHeight) {
    return {
        left: 62,
        top: 22,
        width: Math.max(120, displayWidth - 84),
        height: Math.max(120, displayHeight - 58),
    };
}

function getMeasurementGraphRenderContext() {
    const canvas = elements.measurementGraph;
    if (!canvas) return null;
    const displayWidth = Math.max(320, Math.round(canvas.clientWidth || canvas.width || 960));
    const displayHeight = Math.max(260, Math.round(canvas.clientHeight || canvas.height || 420));
    const bounds = getMeasurementGraphBounds(displayWidth, displayHeight);
    const range = getMeasurementGraphRange(getGraphMeasurementEntries());
    return { canvas, displayWidth, displayHeight, bounds, range };
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

function getMeasurementPeqHandlePosition(filter, bounds, range) {
    return {
        x: measurementFrequencyToX(filter.freqHz || 1000, bounds),
        y: measurementDbToY(filter.gainDb || 0, bounds, range),
    };
}

function getMeasurementPeqHandleHitRadius(pointerType = '') {
    return pointerType === 'touch'
        ? MEASUREMENT_PEQ_TOUCH_HANDLE_HIT_RADIUS_PX
        : MEASUREMENT_PEQ_HANDLE_HIT_RADIUS_PX;
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
    const zeroY = measurementDbToY(0, bounds, range);
    return Math.abs(y - zeroY) <= 40;
}

function measurementPeqTouchCreateCoolingDown(pointerType = '') {
    return pointerType === 'touch' && (Date.now() - measurementPeqLastTouchCreateAt) < MEASUREMENT_PEQ_TOUCH_CREATE_COOLDOWN_MS;
}

function markMeasurementPeqTouchCreate(pointerType = '') {
    if (pointerType === 'touch') measurementPeqLastTouchCreateAt = Date.now();
}

function handleMeasurementGraphPointerDown(event) {
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return;
    const { x, y, bounds, range } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return;
    const peq = ensureMeasurementPeqState();
    const pointerType = String(event.pointerType || '');
    const hitFilter = findMeasurementPeqFilterHandleAtPosition(x, y, bounds, range, pointerType);
    if (hitFilter) {
        if (pointerType === 'touch') event.preventDefault();
        peq.enabled = true;
        peq.activeFilterId = hitFilter.id;
        peq.dragFilterId = hitFilter.id;
        measurementGraphPointerId = event.pointerId;
        elements.measurementGraph?.setPointerCapture?.(event.pointerId);
        renderMeasurementPanel();
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
    measurementGraphPointerId = event.pointerId;
    elements.measurementGraph?.setPointerCapture?.(event.pointerId);
    renderMeasurementPanel();
    focusMeasurementPeqPanelContext();
}

function handleMeasurementGraphPointerMove(event) {
    const peq = ensureMeasurementPeqState();
    if (!peq.dragFilterId || measurementGraphPointerId !== event.pointerId) return;
    if (event.pointerType === 'touch') event.preventDefault();
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return;
    const { x, y, bounds, range } = pointer;
    updateMeasurementPeqFilter(peq.dragFilterId, {
        freqHz: measurementXToFrequency(x, bounds),
        gainDb: measurementYToDb(y, bounds, range),
    });
    scheduleMeasurementGraphRender();
    renderMeasurementPanel();
}

function handleMeasurementGraphPointerUp(event) {
    const peq = ensureMeasurementPeqState();
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId && event.pointerType === 'touch') {
        event.preventDefault();
    }
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId) {
        elements.measurementGraph?.releasePointerCapture?.(event.pointerId);
        measurementGraphPointerId = null;
    }
    peq.dragFilterId = null;
}

function buildMeasurementGraphEntry(measurement = {}, { current = false, compareIndex = 0 } = {}) {
    const traces = getMeasurementDisplayTraces(measurement);
    if (!traces.length) return null;
    const smoothing = state.measurement.displaySmoothing || '1/6-oct';
    return {
        ...measurement,
        traces: traces.map((trace) => ({
            ...trace,
            points: smoothMeasurementTracePoints(trace.points || [], smoothing),
        })),
        current,
        graphColor: current ? measurementCurrentColor : getSavedMeasurementColor(compareIndex),
    };
}

function getGraphMeasurementEntries() {
    const entries = [];
    const current = getCurrentMeasurementEntry();
    const currentEntry = current ? buildMeasurementGraphEntry(current, { current: true }) : null;
    if (currentEntry) entries.push(currentEntry);
    getVisibleMeasurementEntries().forEach((measurement, index) => {
        const entry = buildMeasurementGraphEntry(measurement, { current: false, compareIndex: index });
        if (entry) entries.push(entry);
    });
    return entries;
}

function preferredMeasurementHttpsHost() {
    const host = String(window.location.hostname || '').trim();
    if (!host || host === 'fxroute.local') return '192.168.178.104';
    return host;
}

function browserMeasurementSupportIssue() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!window.isSecureContext) {
        return `Browser mic needs HTTPS: https://${preferredMeasurementHttpsHost()}/`;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
        return 'This browser does not expose microphone capture via getUserMedia.';
    }
    if (!AudioContextClass) {
        return 'This browser does not support Web Audio for the measurement recorder.';
    }
    return '';
}

function browserMeasurementSupported() {
    return !browserMeasurementSupportIssue();
}

function measurementModeIsBrowser() {
    return false;
}

function measurementModeReady() {
    if (measurementModeIsBrowser()) return browserMeasurementSupported();
    return !!state.measurement.hostCaptureAvailable && !!state.measurement.selectedInputId;
}

function describeMeasurementScope(scopeNote = '') {
    if (measurementModeIsBrowser()) {
        return browserMeasurementSupported()
            ? 'Browser mic ready. FXRoute will play the sweep on the active output and this browser will upload the capture.'
            : browserMeasurementSupportIssue();
    }
    if (scopeNote) return 'Host-local sweep ready. Full graph view is available after capture.';
    return 'Host-local sweep ready. Calibration file is optional.';
}

function measurementModeNoteText() {
    if (measurementModeIsBrowser()) {
        return browserMeasurementSupported()
            ? 'Browser mic over HTTPS.'
            : browserMeasurementSupportIssue();
    }
    return 'Host-local capture on this system.';
}

function browserMeasurementHelpHtml() {
    const host = preferredMeasurementHttpsHost();
    const certUrl = `http://${host}/api/browser-mic/certificate`;
    const httpsUrl = `https://${host}/`;
    if (!measurementModeIsBrowser()) return '';
    if (browserMeasurementSupported()) {
        return `
            <strong>Browser measurement path</strong>
            <div class="measurement-inline-note">FXRoute plays the sweep, this browser records the mic, then uploads it for analysis.</div>
            <div class="measurement-inline-note">If this browser showed a certificate warning earlier, install this host's cert here: <a href="${escapeHtml(certUrl)}" target="_blank" rel="noopener noreferrer">download cert</a>.</div>
        `;
    }
    return `
        <strong>Browser mic setup</strong>
        <ol>
            <li>Download the certificate: <a href="${escapeHtml(certUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(certUrl)}</a></li>
            <li>Trust that certificate on the notebook/client.</li>
            <li>Open FXRoute here: <a href="${escapeHtml(httpsUrl)}" target="_blank" rel="noopener noreferrer">${escapeHtml(httpsUrl)}</a></li>
        </ol>
        <div class="measurement-inline-note">Browser microphone capture only works over trusted HTTPS.</div>
    `;
}

function getUsableBrowserInputDevices(devices = []) {
    const audioInputs = devices.filter(device => device.kind === 'audioinput');
    const realInputs = audioInputs.filter(device => !['default', 'communications'].includes(device.deviceId));
    const source = realInputs.length ? realInputs : audioInputs;
    return source.map((device, index) => ({
        id: String(device.deviceId || `browser-input-${index + 1}`),
        label: String(device.label || `Browser microphone ${index + 1}`),
        groupId: String(device.groupId || ''),
    }));
}

function looksLikeMeasurementMicLabel(label = '') {
    return /(umik|mini\s*dsp|measurement|usb)/i.test(String(label || ''));
}

function formatBrowserInputLabelShort(label = '') {
    const text = String(label || '').trim();
    if (!text) return '';
    const parenMatch = text.match(/^Microphone\s*\((.+)\)$/i);
    if (parenMatch?.[1]) return parenMatch[1].trim();
    return text.replace(/^Microphone\s*/i, '').trim() || text;
}

function measurementHasCalibrationSelected() {
    return !!(state.measurement.calibrationFilename || state.measurement.selectedCalibrationRef);
}

function buildBrowserMeasurementAudioConstraints(requestedInputId = '') {
    return {
        ...(requestedInputId ? { deviceId: { exact: requestedInputId } } : {}),
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: { ideal: 1 },
    };
}

function browserCalibrationWarningText() {
    if (!measurementModeIsBrowser() || !measurementHasCalibrationSelected()) return '';
    const selected = (state.measurement.browserInputs || []).find(input => input.id === state.measurement.selectedBrowserInputId);
    const label = selected?.label || state.measurement.browserInputLabel || '';
    if (!label) {
        return 'A calibration file is selected. Make sure the browser microphone is really the same mic this calibration belongs to.';
    }
    if (looksLikeMeasurementMicLabel(label)) return '';
    return `Calibration file is selected, but the browser microphone currently looks like “${label}”. If that is the notebook/onboard mic instead of the UMIK, the result will be misleading.`;
}

async function fetchBrowserInputs(requestPermission = false) {
    if (!measurementModeIsBrowser() || !browserMeasurementSupported()) return;
    if (!navigator.mediaDevices?.enumerateDevices) {
        state.measurement.browserInputs = [];
        state.measurement.selectedBrowserInputId = '';
        renderMeasurementPanel();
        return;
    }
    state.measurement.browserInputsLoading = true;
    renderMeasurementPanel();
    let tempStream = null;
    try {
        const existingDevices = await navigator.mediaDevices.enumerateDevices().catch(() => []);
        const existingInputs = getUsableBrowserInputDevices(existingDevices);
        const requestedInputId = existingInputs.some(input => input.id === state.measurement.selectedBrowserInputId)
            ? state.measurement.selectedBrowserInputId
            : (existingInputs.find(input => looksLikeMeasurementMicLabel(input.label))?.id || '');
        if (requestPermission) {
            tempStream = await navigator.mediaDevices.getUserMedia({
                audio: buildBrowserMeasurementAudioConstraints(requestedInputId),
            });
            state.measurement.browserPermissionGranted = true;
        }
        const devices = await navigator.mediaDevices.enumerateDevices();
        const inputs = getUsableBrowserInputDevices(devices);
        state.measurement.browserInputs = inputs;
        const preferred = inputs.find(input => looksLikeMeasurementMicLabel(input.label));
        state.measurement.selectedBrowserInputId = inputs.some(input => input.id === state.measurement.selectedBrowserInputId)
            ? state.measurement.selectedBrowserInputId
            : (preferred?.id || inputs[0]?.id || '');
        if (!state.measurement.startInFlight && !state.measurement.activeJobId) {
            state.measurement.statusText = describeMeasurementScope();
        }
    } catch (error) {
        console.error('fetchBrowserInputs failed', error);
        if (requestPermission) {
            state.measurement.statusText = error?.message || 'Browser microphone permission was not granted.';
        }
    } finally {
        tempStream?.getTracks?.().forEach(track => track.stop());
        state.measurement.browserInputsLoading = false;
        renderMeasurementPanel();
    }
}

function applyMeasurementCalibrationState(data) {
    if (!data || typeof data !== 'object') return;
    state.measurement.calibrationOptions = Array.isArray(data.calibrations) ? data.calibrations : state.measurement.calibrationOptions;
    state.measurement.selectedCalibrationRef = String(data.active_calibration_file_id || '');
    if (state.measurement.selectedCalibrationRef && !state.measurement.calibrationOptions.some(item => item.id === state.measurement.selectedCalibrationRef)) {
        state.measurement.selectedCalibrationRef = '';
    }
    state.measurement.calibrationFilename = '';
    if (elements.measurementCalibrationFile) elements.measurementCalibrationFile.value = '';
}

async function setActiveMeasurementCalibration(calibrationFileId) {
    state.measurement.calibrationUpdating = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch('/api/measurements/calibrations/active', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ calibration_file_id: calibrationFileId || '' }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save calibration selection');
        applyMeasurementCalibrationState(data);
        showToast(calibrationFileId ? 'Calibration file selected' : 'Calibration disabled', 'success');
    } catch (error) {
        console.error('setActiveMeasurementCalibration failed', error);
        state.measurement.statusText = error.message || 'Failed to save calibration selection';
        showToast(state.measurement.statusText, 'error');
        await fetchMeasurements();
    } finally {
        state.measurement.calibrationUpdating = false;
        renderMeasurementPanel();
    }
}

async function uploadMeasurementCalibration(file) {
    if (!file) return;
    state.measurement.calibrationUpdating = true;
    state.measurement.calibrationFilename = file.name || 'calibration.txt';
    renderMeasurementPanel();
    const formData = new FormData();
    formData.append('calibration_file', file);
    try {
        const resp = await fetch('/api/measurements/calibrations', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to upload calibration file');
        applyMeasurementCalibrationState(data);
        showToast('Calibration file uploaded and selected', 'success');
    } catch (error) {
        console.error('uploadMeasurementCalibration failed', error);
        state.measurement.statusText = error.message || 'Failed to upload calibration file';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.calibrationUpdating = false;
        renderMeasurementPanel();
    }
}

async function deleteSelectedMeasurementCalibration() {
    const calibrationId = state.measurement.selectedCalibrationRef || '';
    const selected = (state.measurement.calibrationOptions || []).find(option => option.id === calibrationId);
    if (!calibrationId || !selected) {
        showToast('No calibration file selected to delete', 'warning');
        return;
    }
    if (state.measurement.startInFlight || state.measurement.activeJobId) {
        showToast('Cannot delete calibration during an active measurement', 'warning');
        return;
    }
    if (!window.confirm(`Delete calibration file "${selected.filename || calibrationId}"?`)) return;
    state.measurement.calibrationDeleting = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch(`/api/measurements/calibrations/${encodeURIComponent(calibrationId)}`, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to delete calibration file');
        applyMeasurementCalibrationState(data);
        showToast('Calibration file deleted', 'success');
    } catch (error) {
        console.error('deleteSelectedMeasurementCalibration failed', error);
        state.measurement.statusText = error.message || 'Failed to delete calibration file';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.calibrationDeleting = false;
        renderMeasurementPanel();
    }
}

async function fetchMeasurements() {
    state.measurement.loading = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch('/api/measurements');
        if (!resp.ok) throw new Error('Failed to fetch measurements');
        const data = await resp.json();
        const measurements = Array.isArray(data.measurements) ? data.measurements.map((measurement, index) => normalizeMeasurementEntry(measurement, index)) : [];
        state.measurement.measurements = measurements;
        state.measurement.visibilityById = normalizeMeasurementVisibility(measurements, state.measurement.visibilityById || {});
        state.measurement.reviewVisibilityById = normalizeMeasurementReviewVisibility(measurements, state.measurement.reviewVisibilityById || {});
        state.measurement.storage = data.storage || null;
        state.measurement.calibrationOptions = Array.isArray(data.calibrations) ? data.calibrations : [];
        state.measurement.selectedCalibrationRef = String(data.active_calibration_file_id || '');
        if (state.measurement.selectedCalibrationRef && !state.measurement.calibrationOptions.some(item => item.id === state.measurement.selectedCalibrationRef)) {
            state.measurement.selectedCalibrationRef = '';
        }
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

async function fetchMeasurementInputs() {
    state.measurement.inputsLoading = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch('/api/measurements/inputs');
        if (!resp.ok) throw new Error('Failed to fetch measurement inputs');
        const data = await resp.json();
        const inputs = Array.isArray(data.inputs) && data.inputs.length
            ? data.inputs.map((input, index) => ({
                id: String(input.id || `input-${index + 1}`),
                label: String(input.label || input.id || `Input ${index + 1}`),
                note: String(input.note || ''),
            }))
            : [];
        state.measurement.inputs = inputs;
        state.measurement.selectedInputId = inputs.some(input => input.id === state.measurement.selectedInputId)
            ? state.measurement.selectedInputId
            : (inputs[0]?.id || '');
        state.measurement.hostCaptureAvailable = !!data.capture_available && !!inputs.length;
        state.measurement.captureAvailable = state.measurement.hostCaptureAvailable;
        state.measurement.modeNote = measurementModeNoteText();
        if (!state.measurement.startInFlight && !state.measurement.activeJobId) {
            if (!state.measurement.hostCaptureAvailable && !measurementModeIsBrowser()) {
                state.measurement.statusText = inputs.length
                    ? 'No available capture source is ready right now.'
                    : 'No PipeWire capture sources are currently visible on this host.';
            } else {
                state.measurement.statusText = describeMeasurementScope(data.scope_note);
            }
        }
    } catch (error) {
        console.error('fetchMeasurementInputs failed', error);
        state.measurement.inputs = [];
        state.measurement.selectedInputId = '';
        state.measurement.hostCaptureAvailable = false;
        state.measurement.captureAvailable = false;
        state.measurement.modeNote = measurementModeNoteText();
        state.measurement.statusText = measurementModeIsBrowser() ? describeMeasurementScope() : (error.message || 'Failed to load capture inputs');
    } finally {
        state.measurement.inputsLoading = false;
        renderMeasurementPanel();
    }
}

function toggleMeasurementPanel(forceOpen = null) {
    if (!elements.measurementPanel) return;
    state.measurement.browserSupported = browserMeasurementSupported();
    state.measurement.modeNote = measurementModeNoteText();
    const shouldOpen = forceOpen === null ? elements.measurementPanel.classList.contains('hidden') : !!forceOpen;
    state.measurement.open = shouldOpen;
    elements.measurementPanel.classList.toggle('hidden', !shouldOpen);
    if (elements.effectsMeasureOpenBtn) {
        elements.effectsMeasureOpenBtn.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    }
    if (shouldOpen) {
        renderMeasurementPanel();
        void fetchMeasurementInputs();
        scheduleMeasurementGraphRender();
        elements.measurementCloseBtn?.focus();
    }
}

function scheduleMeasurementGraphRender() {
    if (measurementResizeScheduled) return;
    measurementResizeScheduled = true;
    window.requestAnimationFrame(() => {
        measurementResizeScheduled = false;
        drawMeasurementGraph();
    });
}

function getSortedNumericValues(values = []) {
    return values.filter(value => Number.isFinite(value)).sort((a, b) => a - b);
}

function getValueQuantile(sortedValues = [], quantile = 0.5) {
    if (!sortedValues.length) return 0;
    if (sortedValues.length === 1) return sortedValues[0];
    const clamped = Math.min(1, Math.max(0, quantile));
    const position = (sortedValues.length - 1) * clamped;
    const lowerIndex = Math.floor(position);
    const upperIndex = Math.ceil(position);
    if (lowerIndex === upperIndex) return sortedValues[lowerIndex];
    const weight = position - lowerIndex;
    return sortedValues[lowerIndex] + ((sortedValues[upperIndex] - sortedValues[lowerIndex]) * weight);
}

function getMeasurementGraphRange(entries = []) {
    const focusValues = [];
    entries.forEach(entry => {
        (entry.traces || []).forEach(trace => {
            (trace.points || []).forEach(([, level]) => {
                focusValues.push(level);
            });
        });
    });
    const values = getSortedNumericValues(focusValues);
    if (!values.length) {
        return { minDb: -18, maxDb: 18 };
    }
    const useRobustWindow = values.length >= 24;
    const rawMinDb = useRobustWindow ? Math.min(getValueQuantile(values, 0.02), 0) : Math.min(...values, 0);
    const rawMaxDb = useRobustWindow ? Math.max(getValueQuantile(values, 0.98), 0) : Math.max(...values, 0);
    const peakAbs = Math.max(Math.abs(rawMinDb), Math.abs(rawMaxDb), 9);
    const paddedPeakAbs = Math.ceil((peakAbs + 3) / 3) * 3;
    return {
        minDb: -Math.min(24, paddedPeakAbs),
        maxDb: Math.min(24, paddedPeakAbs),
    };
}

function measurementFrequencyToX(frequency, bounds) {
    const minLog = Math.log10(20);
    const maxLog = Math.log10(20000);
    const valueLog = Math.log10(Math.min(20000, Math.max(20, frequency)));
    return bounds.left + ((valueLog - minLog) / (maxLog - minLog)) * bounds.width;
}

function measurementDbToY(level, bounds, range) {
    const ratio = (range.maxDb - level) / Math.max(1, range.maxDb - range.minDb);
    return bounds.top + ratio * bounds.height;
}

function getMeasurementPeqFilterMagnitude(filter = {}, frequencyHz = 1000, sampleRate = 48000) {
    const type = String(filter.type || 'bell');
    if (type === 'gain') return clampMeasurementPeqGain(filter.gainDb || 0);

    const freq = clampMeasurementPeqFrequency(filter.freqHz || 1000);
    const q = clampMeasurementPeqQ(filter.q || 1);
    const gainDb = clampMeasurementPeqGain(filter.gainDb || 0);
    const A = 10 ** (gainDb / 40);
    const w0 = (2 * Math.PI * freq) / sampleRate;
    const cos = Math.cos(w0);
    const sin = Math.sin(w0);
    const alpha = sin / (2 * q);
    let b0 = 1; let b1 = 0; let b2 = 0; let a0 = 1; let a1 = 0; let a2 = 0;

    if (type === 'bell') {
        b0 = 1 + (alpha * A);
        b1 = -2 * cos;
        b2 = 1 - (alpha * A);
        a0 = 1 + (alpha / A);
        a1 = -2 * cos;
        a2 = 1 - (alpha / A);
    } else if (type === 'notch') {
        b0 = 1;
        b1 = -2 * cos;
        b2 = 1;
        a0 = 1 + alpha;
        a1 = -2 * cos;
        a2 = 1 - alpha;
    } else if (type === 'low_shelf' || type === 'high_shelf') {
        const shelfAlpha = sin / 2 * Math.sqrt(Math.max(0, (A + (1 / A)) * ((1 / q) - 1) + 2));
        const beta = 2 * Math.sqrt(A) * shelfAlpha;
        if (type === 'low_shelf') {
            b0 = A * ((A + 1) - ((A - 1) * cos) + beta);
            b1 = 2 * A * ((A - 1) - ((A + 1) * cos));
            b2 = A * ((A + 1) - ((A - 1) * cos) - beta);
            a0 = (A + 1) + ((A - 1) * cos) + beta;
            a1 = -2 * ((A - 1) + ((A + 1) * cos));
            a2 = (A + 1) + ((A - 1) * cos) - beta;
        } else {
            b0 = A * ((A + 1) + ((A - 1) * cos) + beta);
            b1 = -2 * A * ((A - 1) + ((A + 1) * cos));
            b2 = A * ((A + 1) + ((A - 1) * cos) - beta);
            a0 = (A + 1) - ((A - 1) * cos) + beta;
            a1 = 2 * ((A - 1) - ((A + 1) * cos));
            a2 = (A + 1) - ((A - 1) * cos) - beta;
        }
    } else if (type === 'low_pass' || type === 'high_pass') {
        if (type === 'low_pass') {
            b0 = (1 - cos) / 2;
            b1 = 1 - cos;
            b2 = (1 - cos) / 2;
        } else {
            b0 = (1 + cos) / 2;
            b1 = -(1 + cos);
            b2 = (1 + cos) / 2;
        }
        a0 = 1 + alpha;
        a1 = -2 * cos;
        a2 = 1 - alpha;
    }

    const omega = (2 * Math.PI * clampMeasurementPeqFrequency(frequencyHz)) / sampleRate;
    const z1r = Math.cos(-omega);
    const z1i = Math.sin(-omega);
    const z2r = Math.cos(-2 * omega);
    const z2i = Math.sin(-2 * omega);
    const nr = b0 + (b1 * z1r) + (b2 * z2r);
    const ni = (b1 * z1i) + (b2 * z2i);
    const dr = a0 + (a1 * z1r) + (a2 * z2r);
    const di = (a1 * z1i) + (a2 * z2i);
    const numerator = Math.hypot(nr, ni);
    const denominator = Math.max(1e-9, Math.hypot(dr, di));
    return 20 * Math.log10(Math.max(1e-9, numerator / denominator));
}

function drawMeasurementPeqOverlay(ctx, bounds, range) {
    const peq = ensureMeasurementPeqState();
    if (!peq.enabled || !peq.filters.length) return;
    const sampleFrequencies = Array.from({ length: 220 }, (_, index) => 20 * (10 ** ((Math.log10(20000 / 20) * index) / 219)));
    const activeFilterId = peq.activeFilterId;

    peq.filters.forEach((filter) => {
        if (filter.type === 'gain') {
            const y = measurementDbToY(filter.gainDb || 0, bounds, range);
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
            const level = getMeasurementPeqFilterMagnitude(filter, frequencyHz);
            const x = measurementFrequencyToX(frequencyHz, bounds);
            const y = measurementDbToY(level, bounds, range);
            if (index === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();
    });

    ctx.strokeStyle = '#f8fafc';
    ctx.lineWidth = 2.1;
    ctx.beginPath();
    sampleFrequencies.forEach((frequencyHz, index) => {
        const summed = peq.filters.reduce((sum, filter) => sum + getMeasurementPeqFilterMagnitude(filter, frequencyHz), 0);
        const x = measurementFrequencyToX(frequencyHz, bounds);
        const y = measurementDbToY(summed, bounds, range);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    });
    ctx.stroke();

    peq.filters.forEach((filter) => {
        const handle = getMeasurementPeqHandlePosition(filter, bounds, range);
        ctx.fillStyle = filter.color;
        ctx.strokeStyle = filter.id === activeFilterId ? '#f8fafc' : 'rgba(15,23,42,0.9)';
        ctx.lineWidth = filter.id === activeFilterId ? 2.4 : 1.5;
        ctx.beginPath();
        ctx.arc(handle.x, handle.y, filter.id === activeFilterId ? 7 : 5.5, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();
    });
}

function drawMeasurementGraph() {
    const canvas = elements.measurementGraph;
    if (!canvas || elements.measurementPanel?.classList.contains('hidden')) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const displayWidth = Math.max(320, Math.round(canvas.clientWidth || canvas.width || 960));
    const displayHeight = Math.max(260, Math.round(canvas.clientHeight || canvas.height || 420));
    const dpr = window.devicePixelRatio || 1;
    const targetWidth = Math.round(displayWidth * dpr);
    const targetHeight = Math.round(displayHeight * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, displayWidth, displayHeight);

    ctx.fillStyle = '#161619';
    ctx.fillRect(0, 0, displayWidth, displayHeight);

    const graphEntries = getGraphMeasurementEntries();
    const range = getMeasurementGraphRange(graphEntries);
    const bounds = getMeasurementGraphBounds(displayWidth, displayHeight);

    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    const dbStep = 6;
    for (let db = range.minDb; db <= range.maxDb; db += dbStep) {
        const y = measurementDbToY(db, bounds, range);
        ctx.beginPath();
        ctx.moveTo(bounds.left, y);
        ctx.lineTo(bounds.left + bounds.width, y);
        ctx.stroke();
        ctx.fillStyle = db === 0 ? '#d1fae5' : 'rgba(236,236,240,0.72)';
        ctx.font = '12px sans-serif';
        ctx.textAlign = 'right';
        ctx.textBaseline = 'middle';
        ctx.fillText(`${db} dB`, bounds.left - 10, y);
    }

    [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000].forEach(frequency => {
        const x = measurementFrequencyToX(frequency, bounds);
        ctx.strokeStyle = frequency === 1000 ? 'rgba(255,255,255,0.12)' : 'rgba(255,255,255,0.08)';
        ctx.beginPath();
        ctx.moveTo(x, bounds.top);
        ctx.lineTo(x, bounds.top + bounds.height);
        ctx.stroke();
        ctx.fillStyle = 'rgba(236,236,240,0.72)';
        ctx.font = '12px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(frequency >= 1000 ? `${frequency / 1000}k` : `${frequency}`, x, bounds.top + bounds.height + 10);
    });

    const zeroY = measurementDbToY(0, bounds, range);
    ctx.strokeStyle = '#6ee7b7';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    ctx.moveTo(bounds.left, zeroY);
    ctx.lineTo(bounds.left + bounds.width, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    graphEntries.forEach(entry => {
        (entry.traces || []).forEach(trace => {
            if (!trace.points.length) return;
            const isReviewTrace = trace.role === 'raw-review';
            ctx.strokeStyle = entry.graphColor || '#6ee7b7';
            ctx.lineWidth = entry.current ? 2.8 : (isReviewTrace ? 1.8 : 2.1);
            ctx.setLineDash(entry.current ? [] : (isReviewTrace ? [6, 5] : [10, 6]));
            ctx.beginPath();
            trace.points.forEach(([frequency, level], pointIndex) => {
                const x = measurementFrequencyToX(frequency, bounds);
                const y = Math.max(bounds.top, Math.min(bounds.top + bounds.height, measurementDbToY(level, bounds, range)));
                if (pointIndex === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.setLineDash([]);
        });
    });

    drawMeasurementPeqOverlay(ctx, bounds, range);

    ctx.strokeStyle = 'rgba(255,255,255,0.16)';
    ctx.lineWidth = 1;
    ctx.strokeRect(bounds.left, bounds.top, bounds.width, bounds.height);
}

function summarizeMeasurementBand(summary = {}, fallbackLabel = 'No points') {
    return summary.point_count ? `${summary.point_count} pts · ${summary.min_hz || 20}–${summary.max_hz || 20000} Hz` : fallbackLabel;
}

function formatMeasurementUpperLimit(maxHz) {
    const numericMaxHz = Number(maxHz);
    if (!Number.isFinite(numericMaxHz) || numericMaxHz <= 0) return '';
    if (numericMaxHz >= 1000) return `${(numericMaxHz / 1000).toFixed(numericMaxHz >= 10000 ? 1 : 2).replace(/\.0$/, '')}k`;
    return `${Math.round(numericMaxHz)} Hz`;
}

function summarizeMeasurementEntry(measurement = {}) {
    const displaySummary = (measurement.review_summary || {}).point_count ? (measurement.review_summary || {}) : (measurement.summary || {});
    return summarizeMeasurementBand(displaySummary, 'No graph data');
}

function formatMeasurementQualityReason(item = {}) {
    const code = String(item?.code || '').trim();
    const message = String(item?.message || '').trim();
    const lookup = {
        'soft-start-alignment': 'soft start',
        'soft-end-alignment': 'soft end',
        'clock-drift-high': 'clock drift',
        'capture-level-low': 'level low',
        'capture-level-high': 'level high',
        'volume-low': 'volume low',
        'volume-high': 'volume high',
    };
    if (lookup[code]) return lookup[code];
    if (/volume\s+low/i.test(message)) return 'volume low';
    if (/volume\s+high/i.test(message)) return 'volume high';
    if (/level\s+low/i.test(message)) return 'level low';
    if (/level\s+high/i.test(message)) return 'level high';
    if (/clock|drift/i.test(message)) return 'clock drift';
    if (/start/i.test(message)) return 'soft start';
    if (/end/i.test(message)) return 'soft end';
    return 'qc warn';
}

function getMeasurementQualitySummary(measurement = {}) {
    const items = Array.isArray(measurement.analysis?.quality_checks?.items) ? measurement.analysis.quality_checks.items : [];
    const warnings = items.filter(item => item?.level === 'warning');
    if (!warnings.length) return 'QC pass';
    return formatMeasurementQualityReason(warnings[0]);
}

function getMeasurementQualityTitle(measurement = {}) {
    const items = Array.isArray(measurement.analysis?.quality_checks?.items) ? measurement.analysis.quality_checks.items : [];
    return items.map(item => item?.message).filter(Boolean).join(' · ');
}

function sleep(ms) {
    return new Promise(resolve => window.setTimeout(resolve, ms));
}

function getMeasurementAudioContextClass() {
    return window.AudioContext || window.webkitAudioContext || null;
}

function createMeasurementWavBlob(frames, sampleRate, channelCount) {
    const totalFrames = frames.reduce((sum, frame) => sum + (frame[0]?.length || 0), 0);
    const bytesPerSample = 2;
    const blockAlign = channelCount * bytesPerSample;
    const buffer = new ArrayBuffer(44 + totalFrames * blockAlign);
    const view = new DataView(buffer);
    const writeString = (offset, value) => {
        for (let i = 0; i < value.length; i += 1) view.setUint8(offset + i, value.charCodeAt(i));
    };
    writeString(0, 'RIFF');
    view.setUint32(4, 36 + totalFrames * blockAlign, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, channelCount, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * blockAlign, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bytesPerSample * 8, true);
    writeString(36, 'data');
    view.setUint32(40, totalFrames * blockAlign, true);
    let offset = 44;
    frames.forEach((frame) => {
        const frameLength = frame[0]?.length || 0;
        for (let i = 0; i < frameLength; i += 1) {
            for (let channel = 0; channel < channelCount; channel += 1) {
                const source = frame[Math.min(channel, frame.length - 1)] || frame[0];
                const sample = Math.max(-1, Math.min(1, Number(source?.[i] || 0)));
                view.setInt16(offset, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
                offset += 2;
            }
        }
    });
    return new Blob([buffer], { type: 'audio/wav' });
}

function buildMeasurementRecorderWorkletSource() {
    return `
        class FxrouteMeasurementRecorder extends AudioWorkletProcessor {
            process(inputs) {
                const input = inputs[0] || [];
                const copied = input.map((channel) => new Float32Array(channel));
                let peak = 0;
                for (const channel of copied) {
                    for (let index = 0; index < channel.length; index += 1) {
                        const value = Math.abs(channel[index] || 0);
                        if (value > peak) peak = value;
                    }
                }
                this.port.postMessage({ channels: copied, peak }, copied.map((channel) => channel.buffer));
                return true;
            }
        }
        registerProcessor('fxroute-measurement-recorder', FxrouteMeasurementRecorder);
    `;
}

function collectBrowserCaptureMeta(track, recorder) {
    const trackSettings = track?.getSettings?.() || {};
    const trackConstraints = track?.getConstraints?.() || {};
    const trackCapabilities = track?.getCapabilities?.() || {};
    return {
        inputLabel: track?.label || 'Browser microphone',
        requestedInputId: state.measurement.selectedBrowserInputId || '',
        secureContext: !!window.isSecureContext,
        trackSettings: {
            deviceId: trackSettings.deviceId || '',
            channelCount: Number(trackSettings.channelCount || 0) || null,
            sampleRate: Number(trackSettings.sampleRate || 0) || null,
            echoCancellation: typeof trackSettings.echoCancellation === 'boolean' ? trackSettings.echoCancellation : null,
            noiseSuppression: typeof trackSettings.noiseSuppression === 'boolean' ? trackSettings.noiseSuppression : null,
            autoGainControl: typeof trackSettings.autoGainControl === 'boolean' ? trackSettings.autoGainControl : null,
            latency: Number.isFinite(Number(trackSettings.latency)) ? Number(trackSettings.latency) : null,
            sampleSize: Number(trackSettings.sampleSize || 0) || null,
        },
        trackConstraints,
        trackCapabilities,
        recorder: {
            processingModel: recorder?.processingModel || '',
            sampleRate: Number(recorder?.sampleRate || 0) || null,
            channelCount: Number(recorder?.channelCount || 0) || null,
            baseLatency: Number.isFinite(Number(recorder?.baseLatency)) ? Number(recorder.baseLatency) : null,
            outputLatency: Number.isFinite(Number(recorder?.outputLatency)) ? Number(recorder.outputLatency) : null,
            contextState: recorder?.contextState || '',
            inputChannelCount: Number(recorder?.inputChannelCount || 0) || null,
        },
        browser: {
            userAgent: navigator.userAgent || '',
            platform: navigator.userAgentData?.platform || navigator.platform || '',
            language: navigator.language || '',
            visibilityState: document.visibilityState || '',
        },
    };
}

function getBrowserCaptureBlockingIssue(captureMeta = {}) {
    const settings = captureMeta.trackSettings || {};
    if (captureMeta.requestedInputId && settings.deviceId && captureMeta.requestedInputId !== settings.deviceId) {
        return 'Browser measurement refused to start because the browser opened a different microphone than the one selected in FXRoute.';
    }
    if (settings.echoCancellation === true) return 'Browser measurement refused to start because echo cancellation is still enabled on the mic path.';
    if (settings.noiseSuppression === true) return 'Browser measurement refused to start because noise suppression is still enabled on the mic path.';
    if (settings.autoGainControl === true) return 'Browser measurement refused to start because automatic gain control is still enabled on the mic path.';
    if (settings.sampleRate && settings.sampleRate !== 48000) return `Browser measurement refused to start because the browser mic is actually running at ${settings.sampleRate} Hz instead of 48 kHz.`;
    return '';
}

function getBrowserCaptureCaution(captureMeta = {}) {
    const recorder = captureMeta.recorder || {};
    if (recorder.processingModel && recorder.processingModel !== 'audio-worklet') {
        return 'Browser recorder fell back to ScriptProcessor, so timing may still be less stable than we want.';
    }
    return '';
}

function analyzeMeasurementRecorderFrames(frames = [], inputChannels = 1) {
    const channelCount = Math.max(1, Number(inputChannels || 1) || 1);
    const perChannelPeak = Array.from({ length: channelCount }, () => 0);
    const perChannelEnergy = Array.from({ length: channelCount }, () => 0);
    const perChannelSamples = Array.from({ length: channelCount }, () => 0);
    let overallPeak = 0;
    let overallEnergy = 0;
    let overallSamples = 0;

    for (const frameSet of frames) {
        if (!Array.isArray(frameSet)) continue;
        for (let channelIndex = 0; channelIndex < channelCount; channelIndex += 1) {
            const channel = frameSet[channelIndex];
            if (!(channel instanceof Float32Array) && !Array.isArray(channel)) continue;
            const length = Number(channel.length || 0);
            for (let sampleIndex = 0; sampleIndex < length; sampleIndex += 1) {
                const sample = Number(channel[sampleIndex] || 0);
                const abs = Math.abs(sample);
                if (abs > perChannelPeak[channelIndex]) perChannelPeak[channelIndex] = abs;
                if (abs > overallPeak) overallPeak = abs;
                perChannelEnergy[channelIndex] += sample * sample;
                perChannelSamples[channelIndex] += 1;
                overallEnergy += sample * sample;
                overallSamples += 1;
            }
        }
    }

    const perChannelRms = perChannelEnergy.map((energy, index) => {
        const samples = perChannelSamples[index] || 0;
        return samples > 0 ? Math.sqrt(energy / samples) : 0;
    });
    const overallRms = overallSamples > 0 ? Math.sqrt(overallEnergy / overallSamples) : 0;

    return {
        peak: overallPeak,
        rms: overallRms,
        framesCaptured: frames.length,
        totalSamples: overallSamples,
        perChannelPeak,
        perChannelRms,
        perChannelSamples,
    };
}

async function createBrowserMeasurementRecorder(stream, preferredChannels = 1) {
    const AudioContextClass = getMeasurementAudioContextClass();
    if (!AudioContextClass) throw new Error('Web Audio is unavailable in this browser');
    const audioContext = new AudioContextClass({ sampleRate: 48000, latencyHint: 'interactive' });
    if (audioContext.state === 'suspended') {
        await audioContext.resume().catch(() => {});
    }
    const source = audioContext.createMediaStreamSource(stream);
    const inputChannels = Math.max(1, Math.min(2, source.channelCount || preferredChannels || 1));
    const silence = audioContext.createGain();
    silence.gain.value = 0;
    const frames = [];
    let peak = 0;
    let stopImpl = null;
    let processingModel = '';

    if (audioContext.audioWorklet?.addModule && typeof AudioWorkletNode !== 'undefined') {
        const moduleUrl = URL.createObjectURL(new Blob([buildMeasurementRecorderWorkletSource()], { type: 'application/javascript' }));
        try {
            await audioContext.audioWorklet.addModule(moduleUrl);
            const node = new AudioWorkletNode(audioContext, 'fxroute-measurement-recorder', {
                numberOfInputs: 1,
                numberOfOutputs: 1,
                channelCount: inputChannels,
                channelCountMode: 'explicit',
                channelInterpretation: 'speakers',
            });
            node.port.onmessage = (event) => {
                const channels = Array.isArray(event.data?.channels) ? event.data.channels.map((channel) => new Float32Array(channel)) : [];
                if (channels.length) frames.push(channels);
                peak = Math.max(peak, Number(event.data?.peak || 0));
            };
            source.connect(node);
            node.connect(silence);
            silence.connect(audioContext.destination);
            processingModel = 'audio-worklet';
            stopImpl = async () => {
                source.disconnect();
                node.disconnect();
                silence.disconnect();
                node.port.onmessage = null;
                URL.revokeObjectURL(moduleUrl);
                const sampleRate = audioContext.sampleRate || 48000;
                await audioContext.close().catch(() => {});
                const analysis = analyzeMeasurementRecorderFrames(frames, inputChannels);
                return {
                    blob: createMeasurementWavBlob(frames, sampleRate, inputChannels),
                    stats: {
                        peak: Math.max(peak, Number(analysis.peak || 0)),
                        rms: Number(analysis.rms || 0),
                        framesCaptured: Number(analysis.framesCaptured || 0),
                        totalSamples: Number(analysis.totalSamples || 0),
                        perChannelPeak: Array.isArray(analysis.perChannelPeak) ? analysis.perChannelPeak : [],
                        perChannelRms: Array.isArray(analysis.perChannelRms) ? analysis.perChannelRms : [],
                        perChannelSamples: Array.isArray(analysis.perChannelSamples) ? analysis.perChannelSamples : [],
                        processingModel: 'audio-worklet',
                        sampleRate,
                        channelCount: inputChannels,
                    },
                };
            };
        } catch (error) {
            console.warn('AudioWorklet recorder setup failed, falling back to ScriptProcessor', error);
            URL.revokeObjectURL(moduleUrl);
        }
    }

    if (!stopImpl) {
        const processor = audioContext.createScriptProcessor(4096, inputChannels, inputChannels);
        processor.onaudioprocess = (event) => {
            const channelFrames = [];
            for (let channel = 0; channel < inputChannels; channel += 1) {
                const data = new Float32Array(event.inputBuffer.getChannelData(Math.min(channel, event.inputBuffer.numberOfChannels - 1)));
                channelFrames.push(data);
                for (let index = 0; index < data.length; index += 1) {
                    peak = Math.max(peak, Math.abs(data[index] || 0));
                }
            }
            frames.push(channelFrames);
        };
        source.connect(processor);
        processor.connect(silence);
        silence.connect(audioContext.destination);
        processingModel = 'script-processor';
        stopImpl = async () => {
            processor.disconnect();
            silence.disconnect();
            source.disconnect();
            processor.onaudioprocess = null;
            const sampleRate = audioContext.sampleRate || 48000;
            await audioContext.close().catch(() => {});
            const analysis = analyzeMeasurementRecorderFrames(frames, inputChannels);
            return {
                blob: createMeasurementWavBlob(frames, sampleRate, inputChannels),
                stats: {
                    peak: Math.max(peak, Number(analysis.peak || 0)),
                    rms: Number(analysis.rms || 0),
                    framesCaptured: Number(analysis.framesCaptured || 0),
                    totalSamples: Number(analysis.totalSamples || 0),
                    perChannelPeak: Array.isArray(analysis.perChannelPeak) ? analysis.perChannelPeak : [],
                    perChannelRms: Array.isArray(analysis.perChannelRms) ? analysis.perChannelRms : [],
                    perChannelSamples: Array.isArray(analysis.perChannelSamples) ? analysis.perChannelSamples : [],
                    processingModel: 'script-processor',
                    sampleRate,
                    channelCount: inputChannels,
                },
            };
        };
    }

    return {
        sampleRate: audioContext.sampleRate || 48000,
        channelCount: inputChannels,
        processingModel,
        baseLatency: Number.isFinite(Number(audioContext.baseLatency)) ? Number(audioContext.baseLatency) : null,
        outputLatency: Number.isFinite(Number(audioContext.outputLatency)) ? Number(audioContext.outputLatency) : null,
        contextState: audioContext.state || '',
        inputChannelCount: inputChannels,
        async stop() {
            const result = await stopImpl();
            this.processingModel = result.stats.processingModel;
            this.sampleRate = result.stats.sampleRate;
            this.channelCount = result.stats.channelCount;
            this.contextState = 'closed';
            return result;
        },
    };
}

async function primeBrowserMeasurementRecorder(stream, preferredChannels = 1) {
    const primer = await createBrowserMeasurementRecorder(stream, preferredChannels);
    await sleep(700);
    await primer.stop();
    state.measurement.browserMeasurementPrimed = true;
}

function getBrowserMeasurementAnalysis(job = {}) {
    return job?.result?.analysis || job?.result?.measurement?.analysis || {};
}

function browserMeasurementDriftPpm(job = {}) {
    return Number(getBrowserMeasurementAnalysis(job)?.clock?.drift_ppm || 0) || 0;
}

function browserMeasurementNormalizedByDb(job = {}) {
    return Number(getBrowserMeasurementAnalysis(job)?.normalized_by_db || 0) || 0;
}

function browserMeasurementClockCompensated(job = {}) {
    return !!getBrowserMeasurementAnalysis(job)?.clock?.compensated;
}

function browserMeasurementQualityCodes(job = {}) {
    const items = getBrowserMeasurementAnalysis(job)?.quality_checks?.items || [];
    return items.map(item => String(item?.code || '').trim()).filter(Boolean);
}

function browserMeasurementHasClockDriftWarning(job = {}) {
    return browserMeasurementQualityCodes(job).includes('clock-drift-high');
}

function browserMeasurementQualityErrorCodes(job = {}) {
    const items = getBrowserMeasurementAnalysis(job)?.quality_checks?.items || [];
    return items
        .filter(item => String(item?.level || '').trim() === 'error')
        .map(item => String(item?.code || '').trim())
        .filter(Boolean);
}

function browserMeasurementRetryMessage(job = {}) {
    return String(job?.message || job?.result?.message || '').toLowerCase();
}

function browserMeasurementLowLevelHint(job = {}) {
    const analysis = getBrowserMeasurementAnalysis(job);
    const items = analysis?.quality_checks?.items || [];
    const lowLevelItem = items.find(item => String(item?.code || '').trim() === 'capture-level-low');
    if (lowLevelItem?.message) return String(lowLevelItem.message).trim();
    const audit = analysis?.capture_audit || {};
    const peakDbfs = Number(audit?.peak_dbfs);
    const rmsDbfs = Number(audit?.rms_dbfs);
    if (!Number.isFinite(peakDbfs) && !Number.isFinite(rmsDbfs)) return '';
    const pieces = [];
    if (Number.isFinite(peakDbfs)) pieces.push(`peak ${peakDbfs.toFixed(2)} dBFS`);
    if (Number.isFinite(rmsDbfs)) pieces.push(`rms ${rmsDbfs.toFixed(2)} dBFS`);
    if (!pieces.length) return '';
    const errorCodes = browserMeasurementQualityErrorCodes(job);
    const syncTimingErrors = ['weak-start-alignment', 'weak-end-alignment', 'insufficient-sync-bursts', 'sync-cluster-a-insufficient', 'sync-cluster-b-insufficient', 'sync-order-invalid', 'sync-fit-residual-high', 'sync-burst-residual-high', 'corrected-sweep-weak'];
    if (errorCodes.some(code => syncTimingErrors.includes(code))) {
        return `Browser capture level did not look obviously too low (${pieces.join(', ')}); this run failed on sync/timing confidence instead.`;
    }
    return `Latest browser capture stats: ${pieces.join(', ')}.`;
}

function browserMeasurementShouldRetry(job = {}) {
    const driftPpm = Math.abs(browserMeasurementDriftPpm(job));
    const normalizedByDb = browserMeasurementNormalizedByDb(job);
    const compensated = browserMeasurementClockCompensated(job);
    const qualityCodes = browserMeasurementQualityCodes(job);
    const qualityErrorCodes = browserMeasurementQualityErrorCodes(job);
    const retryMessage = browserMeasurementRetryMessage(job);
    if (retryMessage.includes('should be retried automatically')) return true;
    const retryableSyncCodes = ['weak-start-alignment', 'weak-end-alignment', 'insufficient-sync-bursts', 'sync-cluster-a-insufficient', 'sync-cluster-b-insufficient', 'sync-order-invalid', 'sync-fit-residual-high', 'sync-burst-residual-high', 'corrected-sweep-weak', 'browser-clock-drift-excessive'];
    if (qualityErrorCodes.length && qualityErrorCodes.every(code => retryableSyncCodes.includes(code))) return true;
    if (browserMeasurementHasClockDriftWarning(job)) return true;
    if (driftPpm > 5000) return true;
    if (compensated && driftPpm > 1500) return true;
    if (compensated && normalizedByDb < -42) return true;
    if (normalizedByDb < -46 && (qualityCodes.includes('soft-start-alignment') || qualityCodes.includes('soft-end-alignment'))) return true;
    return false;
}

async function runBrowserMeasurementAttempt({
    stream,
    track,
    browserInputLabel,
    preferredRecorderChannels,
    browserCaptureCaution,
    attemptIndex = 0,
    maxAttempts = 3,
}) {
    const recorder = await createBrowserMeasurementRecorder(stream, preferredRecorderChannels);
    const browserCaptureMeta = collectBrowserCaptureMeta(track, recorder);
    const blockingIssue = getBrowserCaptureBlockingIssue(browserCaptureMeta);
    if (blockingIssue) throw new Error(blockingIssue);

    const formData = new FormData();
    formData.append('channel', state.measurement.selectedChannel || 'left');
    const calibrationFile = elements.measurementCalibrationFile?.files?.[0];
    if (calibrationFile) {
        formData.append('calibration_file', calibrationFile);
    } else if (state.measurement.selectedCalibrationRef) {
        formData.append('calibration_ref', state.measurement.selectedCalibrationRef);
    }

    state.measurement.statusText = attemptIndex > 0
        ? `Browser capture timing was unstable. Retrying automatically (${attemptIndex + 1}/${maxAttempts})…`
        : (browserCaptureCaution || 'Browser microphone armed. Preparing FXRoute sweep…');
    renderMeasurementPanel();
    const startResp = await fetch('/api/measurements/browser/start', { method: 'POST', body: formData });
    const startData = await startResp.json().catch(() => ({}));
    if (!startResp.ok) throw new Error(startData.detail || 'Failed to start browser measurement');

    const job = startData.job || {};
    state.measurement.activeJobId = String(job.id || '');
    state.measurement.statusText = String(job.message || 'Recording browser microphone…');
    renderMeasurementPanel();

    const captureInfo = job.browser_capture || {};
    const recordDurationMs = Number(captureInfo.record_duration_ms || 11000);
    await sleep(recordDurationMs);
    state.measurement.statusText = 'Uploading browser capture…';
    renderMeasurementPanel();

    const { blob: captureBlob, stats: captureStats } = await recorder.stop();
    browserCaptureMeta.recorder = {
        ...(browserCaptureMeta.recorder || {}),
        processingModel: captureStats?.processingModel || browserCaptureMeta.recorder?.processingModel || '',
        sampleRate: Number(captureStats?.sampleRate || browserCaptureMeta.recorder?.sampleRate || 0) || null,
        channelCount: Number(captureStats?.channelCount || browserCaptureMeta.recorder?.channelCount || 0) || null,
        peak: Number.isFinite(Number(captureStats?.peak)) ? Number(captureStats.peak) : null,
        rms: Number.isFinite(Number(captureStats?.rms)) ? Number(captureStats.rms) : null,
        framesCaptured: Number.isFinite(Number(captureStats?.framesCaptured)) ? Number(captureStats.framesCaptured) : null,
        totalSamples: Number.isFinite(Number(captureStats?.totalSamples)) ? Number(captureStats.totalSamples) : null,
        perChannelPeak: Array.isArray(captureStats?.perChannelPeak) ? captureStats.perChannelPeak.map(value => Number(value || 0)) : [],
        perChannelRms: Array.isArray(captureStats?.perChannelRms) ? captureStats.perChannelRms.map(value => Number(value || 0)) : [],
        perChannelSamples: Array.isArray(captureStats?.perChannelSamples) ? captureStats.perChannelSamples.map(value => Number(value || 0)) : [],
    };
    console.info('FXRoute browser measurement recorder stats', browserCaptureMeta.recorder);
    const completeForm = new FormData();
    completeForm.append('job_id', state.measurement.activeJobId);
    completeForm.append('browser_input_label', browserInputLabel);
    completeForm.append('browser_capture_meta', JSON.stringify(browserCaptureMeta));
    completeForm.append('capture_file', captureBlob, 'browser-measurement.wav');
    const completeResp = await fetch('/api/measurements/browser/complete', { method: 'POST', body: completeForm });
    const completeData = await completeResp.json().catch(() => ({}));
    if (!completeResp.ok) throw new Error(completeData.detail || 'Failed to upload browser capture');
    return completeData.job || {};
}

async function startBrowserMeasurement() {
    if (!browserMeasurementSupported()) {
        throw new Error('Browser microphone capture is not supported in this browser');
    }

    const calibrationWarning = browserCalibrationWarningText();
    if (calibrationWarning && !window.confirm(`${calibrationWarning}\n\nContinue anyway?`)) {
        state.measurement.statusText = 'Browser measurement cancelled so you can pick the correct microphone.';
        renderMeasurementPanel();
        return;
    }

    state.measurement.statusText = 'Requesting browser microphone permission…';
    renderMeasurementPanel();

    const stream = await navigator.mediaDevices.getUserMedia({
        audio: buildBrowserMeasurementAudioConstraints(state.measurement.selectedBrowserInputId),
    });
    const track = stream.getAudioTracks?.()[0] || null;
    const browserInputLabel = track?.label || 'Browser microphone';
    const browserTrackSettings = track?.getSettings?.() || {};
    const preferredRecorderChannels = Math.max(1, Math.min(2, Number(browserTrackSettings.channelCount || 1) || 1));
    state.measurement.browserInputLabel = browserInputLabel;
    state.measurement.browserPermissionGranted = true;
    if (!state.measurement.browserMeasurementPrimed) {
        state.measurement.statusText = measurementHasCalibrationSelected()
            ? 'Priming browser capture path before calibrated sweep…'
            : 'Priming browser capture path before sweep…';
        renderMeasurementPanel();
        await primeBrowserMeasurementRecorder(stream, preferredRecorderChannels);
    }
    const browserCaptureCaution = getBrowserCaptureCaution(collectBrowserCaptureMeta(track, {
        sampleRate: 48000,
        channelCount: preferredRecorderChannels,
        processingModel: '',
    }));
    await fetchBrowserInputs(false);

    try {
        let completedJob = null;
        const maxAttempts = 3;
        for (let attemptIndex = 0; attemptIndex < maxAttempts; attemptIndex += 1) {
            completedJob = await runBrowserMeasurementAttempt({
                stream,
                track,
                browserInputLabel,
                preferredRecorderChannels,
                browserCaptureCaution,
                attemptIndex,
                maxAttempts,
            });
            if (!browserMeasurementShouldRetry(completedJob)) {
                break;
            }
            if (attemptIndex === maxAttempts - 1) {
                const lowLevelHint = browserMeasurementLowLevelHint(completedJob || {});
                throw new Error(lowLevelHint
                    ? `Browser measurement stayed unstable across all retry attempts. Discarded this capture; please run it once more. ${lowLevelHint}`
                    : 'Browser measurement stayed unstable across all retry attempts. Discarded this capture; please run it once more.');
            }
            state.measurement.activeJobId = '';
            state.measurement.statusText = 'Browser capture sync scaffold was unstable; discarding this run and retrying automatically…';
            renderMeasurementPanel();
            await sleep(400);
        }

        state.measurement.activeJobId = '';
        state.measurement.currentMeasurement = normalizeMeasurementEntry(completedJob?.result?.measurement || {}, 0);
        state.measurement.currentMeasurementName = state.measurement.currentMeasurement.name || '';
        state.measurement.currentMeasurementSaved = false;
        state.measurement.reviewVisibilityById[state.measurement.currentMeasurement.id] = !!state.measurement.currentMeasurement.review_traces?.length;
        state.measurement.statusText = String(completedJob?.message || 'Browser microphone measurement finished.');
        renderMeasurementPanel();
        showToast(browserMeasurementShouldRetry(completedJob || {}) ? 'Measurement finished, but timing still looked unstable' : 'Measurement finished', browserMeasurementShouldRetry(completedJob || {}) ? 'warning' : 'success');
    } finally {
        stream.getTracks().forEach((streamTrack) => streamTrack.stop());
    }
}

async function startHostMeasurement() {
    if (!state.measurement.hostCaptureAvailable || !state.measurement.selectedInputId) {
        state.measurement.statusText = 'No usable host capture source is available for a real measurement on this host.';
        renderMeasurementPanel();
        showToast(state.measurement.statusText, 'error');
        return;
    }

    const formData = new FormData();
    formData.append('input_id', state.measurement.selectedInputId);
    formData.append('channel', state.measurement.selectedChannel || 'left');
    const calibrationFile = elements.measurementCalibrationFile?.files?.[0];
    if (calibrationFile) {
        formData.append('calibration_file', calibrationFile);
    } else if (state.measurement.selectedCalibrationRef) {
        formData.append('calibration_ref', state.measurement.selectedCalibrationRef);
    }

    state.measurement.activeJobId = '';
    state.measurement.currentMeasurementSaved = false;
    state.measurement.statusText = 'Starting host-local sweep…';
    renderMeasurementPanel();

    const resp = await fetch('/api/measurements/start', { method: 'POST', body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || 'Failed to start measurement');
    const job = data.job || {};
    state.measurement.activeJobId = String(job.id || '');
    state.measurement.statusText = String(job.message || 'Preparing sweep…');
    renderMeasurementPanel();
    await pollMeasurementJob(state.measurement.activeJobId);
}

async function startMeasurement() {
    if (state.measurement.startInFlight || state.measurement.activeJobId) return;
    state.measurement.browserSupported = browserMeasurementSupported();
    if (!measurementModeReady()) {
        state.measurement.statusText = measurementModeIsBrowser()
            ? 'Browser microphone capture is unavailable in this browser.'
            : 'No usable host capture source is available for a real measurement on this host.';
        renderMeasurementPanel();
        showToast(state.measurement.statusText, 'error');
        return;
    }

    state.measurement.startInFlight = true;
    state.measurement.activeJobId = '';
    state.measurement.currentMeasurementSaved = false;
    renderMeasurementPanel();

    try {
        if (measurementModeIsBrowser()) await startBrowserMeasurement();
        else await startHostMeasurement();
    } catch (error) {
        console.error('startMeasurement failed', error);
        state.measurement.statusText = error.message || 'Failed to start measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.startInFlight = false;
        renderMeasurementPanel();
    }
}

async function cancelMeasurement() {
    const jobId = String(state.measurement.activeJobId || '');
    if (!jobId) return;
    state.measurement.statusText = 'Cancelling measurement…';
    renderMeasurementPanel();
    try {
        const resp = await fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to cancel measurement');
        state.measurement.statusText = String(data.job?.message || 'Measurement cancelled.');
    } catch (error) {
        console.error('cancelMeasurement failed', error);
        state.measurement.statusText = error.message || 'Failed to cancel measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        renderMeasurementPanel();
    }
}

async function pollMeasurementJob(jobId) {
    if (!jobId) return;
    for (let attempt = 0; attempt < 120; attempt += 1) {
        const resp = await fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to fetch measurement job');
        const job = data.job || {};
        state.measurement.activeJobId = String(job.id || jobId);
        state.measurement.statusText = String(job.message || state.measurement.statusText || 'Measurement running…');
        if (job.status === 'completed' && job.result?.measurement) {
            state.measurement.currentMeasurement = normalizeMeasurementEntry(job.result.measurement, 0);
            state.measurement.currentMeasurementName = state.measurement.currentMeasurement.name || '';
            state.measurement.currentMeasurementSaved = false;
            state.measurement.reviewVisibilityById[state.measurement.currentMeasurement.id] = !!state.measurement.currentMeasurement.review_traces?.length;
            state.measurement.statusText = String(job.message || 'Measurement finished.');
            state.measurement.activeJobId = '';
            renderMeasurementPanel();
            showToast('Measurement finished', 'success');
            return;
        }
        if (job.status === 'failed') {
            state.measurement.activeJobId = '';
            throw new Error(job.error?.detail || job.message || 'Measurement failed');
        }
        if (job.status === 'cancelled') {
            state.measurement.activeJobId = '';
            state.measurement.statusText = String(job.message || 'Measurement cancelled.');
            renderMeasurementPanel();
            showToast('Measurement cancelled', 'success');
            return;
        }
        renderMeasurementPanel();
        await sleep(800);
    }
    throw new Error('Measurement job timed out while waiting for completion');
}

async function saveCurrentMeasurement() {
    const current = state.measurement.currentMeasurement;
    if (!current || state.measurement.saveInFlight || state.measurement.currentMeasurementSaved) return;

    const payload = JSON.parse(JSON.stringify(current));
    payload.name = (state.measurement.currentMeasurementName || current.name || '').trim() || current.name || 'Measurement';

    state.measurement.saveInFlight = true;
    state.measurement.statusText = 'Saving current measurement…';
    renderMeasurementPanel();
    try {
        const resp = await fetch('/api/measurements/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save measurement');
        state.measurement.currentMeasurementSaved = true;
        state.measurement.currentMeasurement = normalizeMeasurementEntry(data.measurement || payload, 0);
        state.measurement.currentMeasurementName = state.measurement.currentMeasurement.name || payload.name;
        state.measurement.reviewVisibilityById[state.measurement.currentMeasurement.id] = measurementReviewVisible(state.measurement.currentMeasurement.id);
        state.measurement.statusText = 'Measurement saved.';
        await fetchMeasurements();
        showToast('Measurement saved', 'success');
    } catch (error) {
        console.error('saveCurrentMeasurement failed', error);
        state.measurement.statusText = error.message || 'Failed to save measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.saveInFlight = false;
        renderMeasurementPanel();
    }
}

async function deleteMeasurement(measurementId, measurementName = 'Measurement') {
    if (!measurementId || state.measurement.saveInFlight || state.measurement.startInFlight) return;
    if (!window.confirm(`Delete saved measurement \"${measurementName}\"?`)) return;
    state.measurement.saveInFlight = true;
    state.measurement.statusText = 'Deleting saved measurement…';
    renderMeasurementPanel();
    try {
        const resp = await fetch(`/api/measurements/${encodeURIComponent(measurementId)}`, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to delete measurement');
        delete state.measurement.visibilityById[measurementId];
        delete state.measurement.reviewVisibilityById[measurementId];
        state.measurement.statusText = 'Saved runs updated.';
        await fetchMeasurements();
    } catch (error) {
        console.error('deleteMeasurement failed', error);
        state.measurement.statusText = error.message || 'Failed to delete measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.saveInFlight = false;
        renderMeasurementPanel();
    }
}

async function deleteSelectedMeasurements() {
    if (state.measurement.saveInFlight || state.measurement.startInFlight) return;
    const measurements = getVisibleMeasurementEntries();
    if (!measurements.length) {
        showToast('No saved measurements selected', 'warning');
        return;
    }
    const label = measurements.length === 1 ? `saved measurement \"${measurements[0].name}\"` : `${measurements.length} saved measurements`;
    if (!window.confirm(`Delete ${label}?`)) return;
    state.measurement.saveInFlight = true;
    state.measurement.statusText = `Deleting ${measurements.length === 1 ? 'saved measurement' : 'saved measurements'}…`;
    renderMeasurementPanel();
    let deletedCount = 0;
    try {
        for (const measurement of measurements) {
            const resp = await fetch(`/api/measurements/${encodeURIComponent(measurement.id)}`, { method: 'DELETE' });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || `Failed to delete ${measurement.name}`);
            delete state.measurement.visibilityById[measurement.id];
            delete state.measurement.reviewVisibilityById[measurement.id];
            deletedCount += 1;
        }
        state.measurement.statusText = 'Saved runs updated.';
        await fetchMeasurements();
    } catch (error) {
        console.error('deleteSelectedMeasurements failed', error);
        state.measurement.statusText = error.message || 'Failed to delete selected measurements';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.saveInFlight = false;
        renderMeasurementPanel();
    }
}

function renderMeasurementPanel() {
    if (!elements.measurementSummary || !elements.measurementList) return;
    const measurementState = state.measurement || {};
    measurementState.browserSupported = browserMeasurementSupported();
    measurementState.modeNote = measurementModeNoteText();
    const usingBrowser = measurementModeIsBrowser();
    const current = getCurrentMeasurementEntry();
    const measurements = (measurementState.measurements || []).filter(measurement => measurement.id !== current?.id);
    const graphEntries = getGraphMeasurementEntries();
    const peq = ensureMeasurementPeqState();
    const activePeqFilter = getMeasurementPeqActiveFilter();

    if (elements.measurementSetupCard) {
        elements.measurementSetupCard.classList.toggle('hidden', !measurementState.setupOpen);
    }
    if (elements.measurementSetupToggleBtn) {
        elements.measurementSetupToggleBtn.textContent = measurementState.setupOpen ? 'Close setup' : 'Setup';
        elements.measurementSetupToggleBtn.disabled = measurementState.startInFlight;
    }
    if (elements.measurementModeSelect) {
        elements.measurementModeSelect.value = measurementState.captureMode || 'host-local';
        elements.measurementModeSelect.disabled = measurementState.startInFlight;
    }
    if (elements.measurementModeNote) {
        elements.measurementModeNote.textContent = measurementState.modeNote || '';
    }
    document.querySelectorAll('[data-measurement-channel]').forEach((button) => {
        const active = (button.getAttribute('data-measurement-channel') || '') === (measurementState.selectedChannel || 'left');
        button.classList.toggle('is-active', active);
        button.disabled = measurementState.startInFlight;
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-measurement-smoothing]').forEach((button) => {
        const active = (button.getAttribute('data-measurement-smoothing') || '') === (measurementState.displaySmoothing || '1/6-oct');
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    if (elements.measurementBrowserHelp) {
        const helpHtml = browserMeasurementHelpHtml();
        elements.measurementBrowserHelp.classList.toggle('hidden', !helpHtml);
        elements.measurementBrowserHelp.innerHTML = helpHtml;
    }
    if (elements.measurementBrowserInputGroup) {
        elements.measurementBrowserInputGroup.classList.toggle('hidden', !usingBrowser);
    }
    if (elements.measurementBrowserInputSelect) {
        const browserInputs = measurementState.browserInputs && measurementState.browserInputs.length
            ? measurementState.browserInputs
            : [{ id: '', label: measurementState.browserInputsLoading ? 'Detecting browser microphones…' : 'Default browser microphone' }];
        elements.measurementBrowserInputSelect.innerHTML = browserInputs.map(input => `<option value="${escapeHtml(input.id)}" ${input.id === measurementState.selectedBrowserInputId ? 'selected' : ''}>${escapeHtml(input.label)}</option>`).join('');
        elements.measurementBrowserInputSelect.disabled = !usingBrowser || measurementState.startInFlight || measurementState.browserInputsLoading;
    }
    if (elements.measurementBrowserInputRefreshBtn) {
        elements.measurementBrowserInputRefreshBtn.disabled = !usingBrowser || measurementState.startInFlight || measurementState.browserInputsLoading || !measurementState.browserSupported;
        elements.measurementBrowserInputRefreshBtn.textContent = measurementState.browserInputsLoading ? 'Detecting…' : 'Detect / refresh browser microphones';
    }
    if (elements.measurementBrowserInputNote) {
        const calibrationWarning = browserCalibrationWarningText();
        const selected = (measurementState.browserInputs || []).find(input => input.id === measurementState.selectedBrowserInputId);
        const selectedLabel = selected?.label || measurementState.browserInputLabel || '';
        elements.measurementBrowserInputNote.textContent = calibrationWarning
            || (selectedLabel
                ? `Selected: ${formatBrowserInputLabelShort(selectedLabel)}`
                : 'Pick the actual measurement mic here. Browser defaults are often the notebook onboard microphone.');
    }
    if (elements.measurementInputGroup) {
        elements.measurementInputGroup.classList.toggle('hidden', usingBrowser);
    }
    if (elements.measurementInputSelect) {
        const inputs = measurementState.inputs && measurementState.inputs.length
            ? measurementState.inputs
            : [{ id: '', label: measurementState.inputsLoading ? 'Loading…' : 'No host capture inputs available' }];
        elements.measurementInputSelect.innerHTML = inputs.map(input => `<option value="${escapeHtml(input.id)}" ${input.id === measurementState.selectedInputId ? 'selected' : ''}>${escapeHtml(input.label)}</option>`).join('');
        elements.measurementInputSelect.disabled = usingBrowser || measurementState.inputsLoading || !measurementState.hostCaptureAvailable;
    }
    if (elements.measurementInputRefreshBtn) {
        elements.measurementInputRefreshBtn.disabled = measurementState.startInFlight || measurementState.inputsLoading;
        elements.measurementInputRefreshBtn.textContent = measurementState.inputsLoading ? 'Detecting…' : 'Detect / refresh host microphones';
    }
    if (elements.measurementChannelSelect) {
        elements.measurementChannelSelect.value = measurementState.selectedChannel || 'left';
        elements.measurementChannelSelect.disabled = measurementState.startInFlight;
    }
    if (elements.measurementCalibrationSelect) {
        const options = [{ id: '', filename: 'No calibration file' }, ...(measurementState.calibrationOptions || [])];
        elements.measurementCalibrationSelect.innerHTML = options.map(option => `<option value="${escapeHtml(option.id || '')}" ${(option.id || '') === (measurementState.selectedCalibrationRef || '') ? 'selected' : ''}>${escapeHtml(option.filename || 'Calibration')}</option>`).join('');
        elements.measurementCalibrationSelect.disabled = measurementState.startInFlight || measurementState.calibrationUpdating || measurementState.calibrationDeleting;
    }
    if (elements.measurementCalibrationDeleteBtn) {
        const canDeleteCalibration = !!measurementState.selectedCalibrationRef && !measurementState.startInFlight && !measurementState.activeJobId && !measurementState.calibrationUpdating && !measurementState.calibrationDeleting;
        elements.measurementCalibrationDeleteBtn.disabled = !canDeleteCalibration;
        elements.measurementCalibrationDeleteBtn.textContent = measurementState.calibrationDeleting ? 'Deleting…' : 'Delete';
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
    if (elements.measurementNameInput) {
        elements.measurementNameInput.value = measurementState.currentMeasurementName || '';
        elements.measurementNameInput.disabled = !current || measurementState.startInFlight || measurementState.saveInFlight;
        elements.measurementNameInput.placeholder = current ? 'Name for save' : 'Available after capture';
    }
    if (elements.measurementStartBtn) {
        const activeJobRunning = !!measurementState.activeJobId;
        elements.measurementStartBtn.disabled = measurementState.calibrationUpdating || measurementState.calibrationDeleting
            ? true
            : (usingBrowser
                ? (!activeJobRunning && !measurementState.browserSupported)
                : (!activeJobRunning && (measurementState.inputsLoading || !measurementState.hostCaptureAvailable)));
        elements.measurementStartBtn.textContent = activeJobRunning
            ? 'Cancel measurement'
            : (measurementState.startInFlight
                ? 'Starting…'
                : (usingBrowser
                    ? (measurementState.browserSupported ? 'Start browser sweep' : 'Browser mic needs HTTPS')
                    : 'Start host-local sweep'));
    }
    if (elements.measurementSaveBtn) {
        elements.measurementSaveBtn.disabled = !current || measurementState.saveInFlight || measurementState.startInFlight || measurementState.currentMeasurementSaved;
        elements.measurementSaveBtn.textContent = measurementState.saveInFlight ? 'Working…' : (measurementState.currentMeasurementSaved ? 'Saved' : 'Save current');
    }
    if (elements.measurementClearBtn) {
        const hasResettableGraphState = !!current || !!peq.filters.length;
        elements.measurementClearBtn.disabled = !hasResettableGraphState || measurementState.startInFlight || !!measurementState.activeJobId;
    }
    if (elements.measurementSetupStatus) {
        elements.measurementSetupStatus.textContent = measurementState.statusText || describeMeasurementScope();
    }
    if (elements.measurementSummary) {
        elements.measurementSummary.textContent = peq.filters.length ? `${peq.filters.length}/4 assistant filters` : '';
    }
    if (elements.measurementEmpty) {
        elements.measurementEmpty.classList.toggle('hidden', graphEntries.length > 0);
    }
    if (elements.measurementGraphControls) {
        elements.measurementGraphControls.textContent = current
            ? 'Tap/click near 0 dB to add a filter, drag handles for freq/gain.'
            : 'Run a sweep to see the graph.';
    }
    if (elements.measurementPeqPanel) {
        elements.measurementPeqPanel.classList.toggle('hidden', !peq.enabled && !peq.filters.length);
    }
    if (elements.measurementPeqChips) {
        elements.measurementPeqChips.innerHTML = Array.from({ length: 4 }, (_, index) => {
            const filter = peq.filters[index] || null;
            const active = filter && filter.id === peq.activeFilterId;
            const classes = `measurement-peq-chip${active ? ' is-active' : ''}${filter ? '' : ' is-empty'}`;
            const style = filter ? `style="border-color:${escapeHtml(filter.color)}66; background:${escapeHtml(filter.color)}22;${active ? ` color:${escapeHtml(filter.color)}; background:${escapeHtml(filter.color)}33;` : ''}"` : '';
            return `<button type="button" class="${classes}" data-measurement-peq-slot="${index}" data-measurement-peq-chip="${filter ? escapeHtml(filter.id) : ''}" ${style}>F${index + 1}</button>`;
        }).join('');
    }
    if (elements.measurementPeqEditor) {
        if (!activePeqFilter) {
            elements.measurementPeqEditor.innerHTML = '<div class="measurement-peq-editor-empty">Use F1-F4 or the graph near the fixed 0 dB line to add up to 4 temporary filters.</div>';
        } else {
            const hideFreqQ = activePeqFilter.type === 'gain';
            elements.measurementPeqEditor.innerHTML = `
                <div class="measurement-peq-editor-grid">
                    <div class="field-group">
                        <label for="measurement-peq-type">Type</label>
                        <select id="measurement-peq-type" class="url-input" data-measurement-peq-field="type">
                            ${measurementPeqTypes.map((type) => `<option value="${type}" ${activePeqFilter.type === type ? 'selected' : ''}>${measurementPeqTypeLabels[type] || type}</option>`).join('')}
                        </select>
                    </div>
                    ${hideFreqQ ? '' : `
                    <div class="field-group measurement-peq-direct-input-field">
                        <label for="measurement-peq-freq">Frequency (Hz)</label>
                        <div class="measurement-peq-stepper">
                            <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-frequency-step="-1" aria-label="Decrease frequency">−</button>
                            <input id="measurement-peq-freq" class="url-input measurement-peq-number-input" type="number" min="20" max="20000" step="1" inputmode="numeric" value="${Math.round(activePeqFilter.freqHz || 1000)}" data-measurement-peq-field="freqHz">
                            <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-frequency-step="1" aria-label="Increase frequency">+</button>
                        </div>
                    </div>`}
                    <div class="field-group measurement-peq-direct-input-field">
                        <label for="measurement-peq-gain">Gain (dB)</label>
                        <div class="measurement-peq-stepper">
                            <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-gain-step="-1" aria-label="Decrease gain">−</button>
                            <input id="measurement-peq-gain" class="url-input measurement-peq-number-input" type="number" min="-24" max="24" step="0.1" inputmode="decimal" value="${Number(activePeqFilter.gainDb || 0).toFixed(1)}" data-measurement-peq-field="gainDb">
                            <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-gain-step="1" aria-label="Increase gain">+</button>
                        </div>
                    </div>
                    ${hideFreqQ ? '' : `
                    <div class="field-group measurement-peq-direct-input-field">
                        <label for="measurement-peq-q">Q</label>
                        <div class="measurement-peq-stepper">
                            <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-q-step="-1" aria-label="Decrease Q">−</button>
                            <input id="measurement-peq-q" class="url-input measurement-peq-number-input" type="number" min="0.1" max="20" step="0.1" inputmode="decimal" aria-keyshortcuts="ArrowUp ArrowDown" value="${Number(activePeqFilter.q || 1).toFixed(2)}" data-measurement-peq-field="q">
                            <button type="button" class="btn-secondary measurement-peq-step-btn" data-measurement-peq-q-step="1" aria-label="Increase Q">+</button>
                        </div>
                    </div>`}
                    <div class="measurement-peq-editor-actions">
                        <button type="button" class="btn-danger" data-measurement-peq-delete="${escapeHtml(activePeqFilter.id)}">Delete</button>
                    </div>
                </div>
            `;
        }
    }
    if (elements.measurementPeqTakeLeftBtn) elements.measurementPeqTakeLeftBtn.disabled = !peq.filters.length;
    if (elements.measurementPeqTakeRightBtn) elements.measurementPeqTakeRightBtn.disabled = !peq.filters.length;
    if (elements.measurementPeqTakeBothBtn) elements.measurementPeqTakeBothBtn.disabled = !peq.filters.length;

    elements.measurementPeqChips?.querySelectorAll('[data-measurement-peq-slot]').forEach((button) => {
        button.addEventListener('click', () => {
            const filterId = button.dataset.measurementPeqChip;
            if (filterId) {
                selectMeasurementPeqFilter(filterId);
            } else {
                const created = addMeasurementPeqFilter();
                if (!created) return;
            }
            renderMeasurementPanel();
            scheduleMeasurementGraphRender();
            focusMeasurementPeqPanelContext();
        });
    });
    elements.measurementPeqEditor?.querySelectorAll('[data-measurement-peq-field]').forEach((input) => {
        const commit = (reRender) => {
            const activeFilter = getMeasurementPeqActiveFilter();
            if (!activeFilter) return;
            const field = input.dataset.measurementPeqField;
            const value = field === 'type' ? input.value : Number(input.value);
            updateMeasurementPeqFilter(activeFilter.id, { [field]: value });
            if (reRender) renderMeasurementPanel();
            scheduleMeasurementGraphRender();
        };
        if (input instanceof HTMLInputElement && input.type === 'number') {
            input.addEventListener('keydown', handleMeasurementPeqNumberInputArrowKey);
        }
        input.addEventListener('input', () => commit(input.dataset.measurementPeqField === 'type'));
        input.addEventListener('change', () => commit(true));
    });
    elements.measurementPeqEditor?.querySelectorAll('[data-measurement-peq-frequency-step]').forEach((button) => {
        button.addEventListener('click', () => {
            const activeFilter = getMeasurementPeqActiveFilter();
            if (!activeFilter) return;
            const input = elements.measurementPeqEditor?.querySelector('#measurement-peq-freq');
            const step = Number(input?.step) || 1;
            const direction = Number(button.dataset.measurementPeqFrequencyStep || '0');
            const nextValue = stepMeasurementPeqFrequency(activeFilter.id, direction, step);
            if (nextValue === null) return;
            if (input) input.value = String(nextValue);
            scheduleMeasurementGraphRender();
            focusMeasurementPeqPanelContext();
        });
    });
    elements.measurementPeqEditor?.querySelectorAll('[data-measurement-peq-gain-step]').forEach((button) => {
        button.addEventListener('click', () => {
            const activeFilter = getMeasurementPeqActiveFilter();
            if (!activeFilter) return;
            const input = elements.measurementPeqEditor?.querySelector('#measurement-peq-gain');
            const step = Number(input?.step) || 0.1;
            const direction = Number(button.dataset.measurementPeqGainStep || '0');
            const nextValue = stepMeasurementPeqGain(activeFilter.id, direction, step);
            if (nextValue === null) return;
            if (input) input.value = nextValue.toFixed(1);
            scheduleMeasurementGraphRender();
            focusMeasurementPeqPanelContext();
        });
    });
    elements.measurementPeqEditor?.querySelectorAll('[data-measurement-peq-q-step]').forEach((button) => {
        button.addEventListener('click', () => {
            const activeFilter = getMeasurementPeqActiveFilter();
            if (!activeFilter) return;
            const input = elements.measurementPeqEditor?.querySelector('#measurement-peq-q');
            const step = Number(input?.step) || 0.1;
            const direction = Number(button.dataset.measurementPeqQStep || '0');
            const nextValue = stepMeasurementPeqQ(activeFilter.id, direction, step);
            if (nextValue === null) return;
            if (input) input.value = nextValue.toFixed(2);
            scheduleMeasurementGraphRender();
            focusMeasurementPeqPanelContext();
        });
    });
    elements.measurementPeqEditor?.querySelectorAll('[data-measurement-peq-delete]').forEach((button) => {
        button.addEventListener('click', () => {
            deleteMeasurementPeqFilter(button.dataset.measurementPeqDelete);
            renderMeasurementPanel();
            scheduleMeasurementGraphRender();
        });
    });

    const currentHtml = current ? (() => {
        const pointsLabel = summarizeMeasurementEntry(current);
        const displayTraces = getMeasurementDisplayTraces(current);
        const traceColor = measurementCurrentColor;
        const badge = measurementState.currentMeasurementSaved
            ? '<span class="measurement-badge measurement-badge-success">Saved</span>'
            : '<span class="measurement-badge">Current</span>';
        const calibrationLabel = current.calibration?.filename
            ? `${current.calibration.filename}${current.calibration?.applied ? ' · applied' : ''}`
            : 'No calibration';
        const qualitySummary = getMeasurementQualitySummary(current);
        const qualityTitle = getMeasurementQualityTitle(current);
        const currentTitle = (measurementState.currentMeasurementSaved || current.storage_path)
            ? `<a href="${escapeHtml(measurementFileUrl(current.id))}">${escapeHtml(current.name || 'Current sweep')}</a>`
            : escapeHtml(current.name || 'Current sweep');
        return `
            <div class="measurement-list-item measurement-list-item-current">
                <div class="measurement-list-row">
                    <span class="measurement-toggle">
                        <span class="measurement-swatch" style="background:${escapeHtml(traceColor)}"></span>
                        <span class="measurement-list-title">${currentTitle}</span>
                        ${badge}
                    </span>
                    <span class="measurement-list-meta">${escapeHtml(formatMeasurementDate(current.created_at))}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta">${escapeHtml(current.input_device?.label || 'Capture input')} · ${escapeHtml(String(current.channel || 'left'))}</span>
                    <span class="measurement-list-points">${escapeHtml(pointsLabel)}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta" title="${escapeHtml(qualityTitle)}">${escapeHtml(calibrationLabel)} · ${escapeHtml(qualitySummary)}</span>
                    <span class="measurement-list-meta">Visible trace: full graph data</span>
                </div>
            </div>
        `;
    })() : '<div class="measurement-list-item"><div class="measurement-list-row"><span class="measurement-list-meta">No current sweep yet.</span></div></div>';

    const selectedSavedCount = measurements.filter(measurement => measurementState.visibilityById?.[measurement.id]).length;
    const allSavedSelected = measurements.length > 0 && selectedSavedCount === measurements.length;

    const savedItemsHtml = measurements.map((measurement, index) => {
        const pointsLabel = summarizeMeasurementEntry(measurement);
        const traceColor = getSavedMeasurementColor(index);
        const isSelected = !!measurementState.visibilityById?.[measurement.id];
        const qualitySummary = getMeasurementQualitySummary(measurement);
        const qualityTitle = getMeasurementQualityTitle(measurement);
        return `
            <div class="measurement-list-item" style="${isSelected ? `border-color:${traceColor}; box-shadow: inset 0 0 0 1px ${traceColor}33; background: linear-gradient(180deg, rgba(255,255,255,0.03), ${traceColor}12);` : ''}">
                <div class="measurement-list-row">
                    <span class="measurement-toggle">
                        <input type="checkbox" data-measurement-toggle="${escapeHtml(measurement.id)}" ${isSelected ? 'checked' : ''}>
                        <span class="measurement-swatch" style="background:${escapeHtml(traceColor)}"></span>
                        <span class="measurement-list-title"><a href="${escapeHtml(measurementFileUrl(measurement.id))}">${escapeHtml(measurement.name)}</a></span>
                    </span>
                    <span class="measurement-list-meta">${escapeHtml(formatMeasurementDate(measurement.created_at))}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta">${escapeHtml(measurement.input_device?.label || 'Capture input')} · ${escapeHtml(String(measurement.channel || 'left'))}</span>
                    <span class="measurement-list-points">${escapeHtml(pointsLabel)}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta" title="${escapeHtml(qualityTitle)}">${escapeHtml(qualitySummary)} · ${isSelected ? 'visible, dashed compare trace' : 'hidden compare trace'}</span>
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
                        <div class="measurement-saved-toolbar-selection">
                            <label class="measurement-list-meta measurement-select-all-toggle"><input type="checkbox" data-measurement-select-all ${allSavedSelected ? 'checked' : ''} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''}>Select all</label>
                            <button type="button" class="btn-danger measurement-saved-delete-action ${selectedSavedCount ? '' : 'is-inert'}" data-measurement-delete-selected ${selectedSavedCount ? '' : 'disabled'} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''} aria-hidden="${selectedSavedCount ? 'false' : 'true'}">Delete selected</button>
                        </div>
                        <button type="button" class="btn-secondary" data-measurement-close-saved>Close</button>
                    </div>
                    ${savedItemsHtml}
                </div>
            </details>
        `
        : '';

    elements.measurementList.innerHTML = `${currentHtml}${savedHtml}`;
    elements.measurementList.querySelectorAll('.measurement-saved-group').forEach((details) => {
        details.addEventListener('toggle', () => {
            state.measurement.savedGroupOpen = !!details.open;
        });
    });
    elements.measurementList.querySelectorAll('[data-measurement-toggle]').forEach((input) => {
        input.addEventListener('change', () => {
            state.measurement.visibilityById[input.dataset.measurementToggle] = !!input.checked;
            state.measurement.savedGroupOpen = true;
            renderMeasurementPanel();
        });
    });
    elements.measurementList.querySelectorAll('[data-measurement-select-all]').forEach((input) => {
        input.addEventListener('change', () => {
            measurements.forEach((measurement) => {
                state.measurement.visibilityById[measurement.id] = !!input.checked;
            });
            state.measurement.savedGroupOpen = true;
            renderMeasurementPanel();
        });
    });
    elements.measurementList.querySelectorAll('[data-measurement-delete-selected]').forEach((button) => {
        button.addEventListener('click', () => {
            deleteSelectedMeasurements();
        });
    });
    elements.measurementList.querySelectorAll('[data-measurement-close-saved]').forEach((button) => {
        button.addEventListener('click', () => {
            state.measurement.savedGroupOpen = false;
            renderMeasurementPanel();
        });
    });
    scheduleMeasurementGraphRender();
}

function setupMeasurementActions() {
    if (!elements.measurementPanel || !elements.effectsMeasureOpenBtn || !elements.measurementCloseBtn) return;
    elements.effectsMeasureOpenBtn.addEventListener('click', () => toggleMeasurementPanel(true));
    elements.measurementCloseBtn.addEventListener('click', () => toggleMeasurementPanel(false));
    const backdrop = elements.measurementPanel.querySelector('.manage-overlay-backdrop');
    if (backdrop) backdrop.addEventListener('click', () => toggleMeasurementPanel(false));
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !elements.measurementPanel.classList.contains('hidden')) {
            toggleMeasurementPanel(false);
        }
    });
    if (elements.measurementSetupToggleBtn) {
        elements.measurementSetupToggleBtn.addEventListener('click', () => {
            state.measurement.setupOpen = !state.measurement.setupOpen;
            renderMeasurementPanel();
        });
    }
    if (elements.measurementModeSelect) {
        elements.measurementModeSelect.addEventListener('change', (event) => {
            state.measurement.captureMode = event.target.value || 'host-local';
            state.measurement.modeNote = measurementModeNoteText();
            if (!state.measurement.startInFlight) {
                state.measurement.statusText = describeMeasurementScope();
            }
            renderMeasurementPanel();
        });
    }
    if (elements.measurementBrowserInputSelect) {
        elements.measurementBrowserInputSelect.addEventListener('change', (event) => {
            state.measurement.selectedBrowserInputId = event.target.value || '';
            renderMeasurementPanel();
        });
    }
    if (elements.measurementBrowserInputRefreshBtn) {
        elements.measurementBrowserInputRefreshBtn.addEventListener('click', () => {
            void fetchBrowserInputs(true);
        });
    }
    if (elements.measurementInputSelect) {
        elements.measurementInputSelect.addEventListener('change', (event) => {
            state.measurement.selectedInputId = event.target.value || '';
        });
    }
    if (elements.measurementInputRefreshBtn) {
        elements.measurementInputRefreshBtn.addEventListener('click', () => {
            void fetchMeasurementInputs();
        });
    }
    if (elements.measurementChannelSelect) {
        elements.measurementChannelSelect.addEventListener('change', (event) => {
            state.measurement.selectedChannel = event.target.value || 'left';
            renderMeasurementPanel();
        });
    }
    document.querySelectorAll('[data-measurement-channel]').forEach((button) => {
        button.addEventListener('click', () => {
            state.measurement.selectedChannel = button.getAttribute('data-measurement-channel') || 'left';
            if (elements.measurementChannelSelect) elements.measurementChannelSelect.value = state.measurement.selectedChannel;
            renderMeasurementPanel();
        });
    });
    document.querySelectorAll('[data-measurement-smoothing]').forEach((button) => {
        button.addEventListener('click', () => {
            state.measurement.displaySmoothing = button.getAttribute('data-measurement-smoothing') || '1/6-oct';
            renderMeasurementPanel();
            scheduleMeasurementGraphRender();
        });
    });
    if (elements.measurementCalibrationSelect) {
        elements.measurementCalibrationSelect.addEventListener('change', (event) => {
            state.measurement.selectedCalibrationRef = event.target.value || '';
            if (elements.measurementCalibrationFile) elements.measurementCalibrationFile.value = '';
            state.measurement.calibrationFilename = '';
            renderMeasurementPanel();
            void setActiveMeasurementCalibration(state.measurement.selectedCalibrationRef);
        });
    }
    if (elements.measurementCalibrationFile) {
        elements.measurementCalibrationFile.addEventListener('change', () => {
            const file = elements.measurementCalibrationFile.files?.[0];
            if (file) {
                void uploadMeasurementCalibration(file);
            } else {
                state.measurement.calibrationFilename = '';
                renderMeasurementPanel();
            }
        });
    }
    if (elements.measurementCalibrationDeleteBtn) {
        elements.measurementCalibrationDeleteBtn.addEventListener('click', () => { void deleteSelectedMeasurementCalibration(); });
    }
    if (elements.measurementNameInput) {
        elements.measurementNameInput.addEventListener('input', (event) => {
            state.measurement.currentMeasurementName = event.target.value || '';
        });
    }
    if (elements.measurementStartBtn) {
        elements.measurementStartBtn.addEventListener('click', () => {
            if (state.measurement.activeJobId) {
                void cancelMeasurement();
                return;
            }
            void startMeasurement();
        });
    }
    if (elements.measurementSaveBtn) {
        elements.measurementSaveBtn.addEventListener('click', () => { void saveCurrentMeasurement(); });
    }
    if (elements.measurementClearBtn) {
        elements.measurementClearBtn.addEventListener('click', () => resetMeasurementGraph());
    }
    if (elements.measurementPeqTakeLeftBtn) {
        elements.measurementPeqTakeLeftBtn.addEventListener('click', () => takeMeasurementPeqToPreset('left'));
    }
    if (elements.measurementPeqTakeRightBtn) {
        elements.measurementPeqTakeRightBtn.addEventListener('click', () => takeMeasurementPeqToPreset('right'));
    }
    if (elements.measurementPeqTakeBothBtn) {
        elements.measurementPeqTakeBothBtn.addEventListener('click', () => takeMeasurementPeqToPreset('both'));
    }
    if (elements.measurementGraph) {
        elements.measurementGraph.addEventListener('pointerdown', handleMeasurementGraphPointerDown);
        elements.measurementGraph.addEventListener('pointermove', handleMeasurementGraphPointerMove);
        elements.measurementGraph.addEventListener('pointerup', handleMeasurementGraphPointerUp);
        elements.measurementGraph.addEventListener('pointercancel', handleMeasurementGraphPointerUp);
        elements.measurementGraph.addEventListener('wheel', handleMeasurementPeqGraphWheel, { passive: false });
    }
    window.addEventListener('resize', () => {
        if (!elements.measurementPanel.classList.contains('hidden')) {
            scheduleMeasurementGraphRender();
        }
    });
    renderMeasurementPanel();
}

async function fetchEffects() {
    try {
        const resp = await fetch('/api/easyeffects/presets');
        if (!resp.ok) throw new Error('Failed to fetch EasyEffects presets');
        const data = await resp.json();
        const prev = state.easyeffects?.compare;
        const presetNames = (data.presets || []).map(p => p.name);
        state.easyeffects = {
            ...data,
            combineDraft: state.easyeffects?.combineDraft || getDefaultEffectsCombineDraft(),
            peqDraft: state.easyeffects?.peqDraft || {
                presetName: '',
                eqMode: 'IIR',
                loadAfterCreate: false,
                leftBands: [defaultPeqBand()],
                rightBands: [defaultPeqBand()],
            },
            compare: resolveEffectsCompareState(data.compare || prev, presetNames, data.active_preset || ''),
        };
        state.easyeffects.combineDraft = normalizeEffectsCombineDraft(state.easyeffects.combineDraft, presetNames);
        if (!Array.isArray(state.easyeffects.peqDraft?.leftBands) || !state.easyeffects.peqDraft.leftBands.length) state.easyeffects.peqDraft.leftBands = [defaultPeqBand()];
        if (!Array.isArray(state.easyeffects.peqDraft?.rightBands) || !state.easyeffects.peqDraft.rightBands.length) state.easyeffects.peqDraft.rightBands = [defaultPeqBand()];
        state.easyeffects.peqDraft.eqMode = normalizePeqEqMode(state.easyeffects.peqDraft?.eqMode);
        if (data.global_extras) {
            applyEffectsExtras({
                limiterEnabled: !!data.global_extras?.limiter?.enabled,
                headroomEnabled: !!data.global_extras?.headroom?.enabled,
                headroomGainDb: Number(data.global_extras?.headroom?.params?.gainDb ?? -3),
                autogainEnabled: !!data.global_extras?.autogain?.enabled,
                autogainTargetDb: Number(data.global_extras?.autogain?.params?.targetDb ?? -12),
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
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">EasyEffects presets are unavailable</div>';
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
    state.easyeffects.peqDraft = getDefaultPeqDraft();
    if (elements.effectsPeqPresetName) elements.effectsPeqPresetName.value = '';
    if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.value = 'IIR';
    if (elements.effectsPeqLoadAfterCreate) elements.effectsPeqLoadAfterCreate.checked = false;
    renderPeqBands();
}
function addPeqBandPair() {
    if (!state.easyeffects?.peqDraft) {
        state.easyeffects.peqDraft = getDefaultPeqDraft();
    }
    const leftBands = state.easyeffects.peqDraft.leftBands || (state.easyeffects.peqDraft.leftBands = []);
    const rightBands = state.easyeffects.peqDraft.rightBands || (state.easyeffects.peqDraft.rightBands = []);
    if (leftBands.length >= 20 || rightBands.length >= 20) {
        showToast('Maximum 20 Left and Right PEQ bands supported', 'error');
        return;
    }
    leftBands.push(defaultPeqBand());
    rightBands.push(defaultPeqBand());
    renderPeqBands();
}
function removePeqBandPair(index) {
    if (!state.easyeffects?.peqDraft) {
        showToast('At least one Left and one Right PEQ band is required', 'error');
        return;
    }
    const leftBands = state.easyeffects.peqDraft.leftBands || [];
    const rightBands = state.easyeffects.peqDraft.rightBands || [];
    if (leftBands.length <= 1 || rightBands.length <= 1 || index <= 0) {
        showToast('Band 1 stays as the required base band', 'error');
        return;
    }
    leftBands.splice(index, 1);
    rightBands.splice(index, 1);
    renderPeqBands();
}
function ensurePeqBandExists(side, index) {
    if (!state.easyeffects?.peqDraft) return null;
    const key = side === 'right' ? 'rightBands' : 'leftBands';
    state.easyeffects.peqDraft[key] = state.easyeffects.peqDraft[key] || [];
    while (state.easyeffects.peqDraft[key].length <= index) {
        state.easyeffects.peqDraft[key].push(defaultPeqBand());
    }
    return state.easyeffects.peqDraft[key][index] || null;
}
function getOtherPeqSide(side) {
    return side === 'right' ? 'left' : 'right';
}
function syncLinkedPeqSpecialBand(side, index) {
    if (!state.easyeffects?.peqDraft) return;
    const sourceBand = ensurePeqBandExists(side, index);
    const otherBand = ensurePeqBandExists(getOtherPeqSide(side), index);
    if (!sourceBand || !otherBand) return;
    otherBand.filterType = sourceBand.filterType;
    if (isPeqGainBand(sourceBand)) {
        otherBand.gainDb = sourceBand.gainDb;
    }
}
function normalizeLinkedPeqSpecialBands() {
    if (!state.easyeffects?.peqDraft) return;
    const leftBands = state.easyeffects.peqDraft.leftBands || [];
    const rightBands = state.easyeffects.peqDraft.rightBands || [];
    const count = Math.max(leftBands.length, rightBands.length);
    for (let index = 0; index < count; index += 1) {
        const leftBand = leftBands[index] || null;
        const rightBand = rightBands[index] || null;
        if (isPeqGainBand(leftBand) || isPeqDelayBand(leftBand)) {
            syncLinkedPeqSpecialBand('left', index);
        } else if (isPeqGainBand(rightBand) || isPeqDelayBand(rightBand)) {
            syncLinkedPeqSpecialBand('right', index);
        }
    }
}
function updatePeqBand(side, index, field, value) {
    if (!state.easyeffects?.peqDraft) return;
    const band = ensurePeqBandExists(side, index);
    if (!band) return;
    band[field] = value;

    const otherBand = ensurePeqBandExists(getOtherPeqSide(side), index);
    const specialLinked = isPeqGainBand(band) || isPeqGainBand(otherBand) || isPeqDelayBand(band) || isPeqDelayBand(otherBand);
    if (field === 'filterType') {
        syncLinkedPeqSpecialBand(side, index);
    } else if (specialLinked && (field === 'gainDb' || field === 'delayMs')) {
        syncLinkedPeqSpecialBand(side, index);
    }
}
function syncLinkedPeqSpecialBandValueInDom(side, index, field) {
    const sourceBand = ensurePeqBandExists(side, index);
    const otherSide = getOtherPeqSide(side);
    const otherInput = document.querySelector(`[data-peq-side="${otherSide}"][data-peq-index="${index}"][data-peq-field="${field}"]`);
    if (!sourceBand || !otherInput) return;
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
        const showRemove = side === 'left' && index > 0;
        return `
        <div class="effects-peq-band" data-peq-side="${side}" data-peq-band="${index}">
            <div class="effects-peq-band-header">
                <div>
                    <div class="effects-peq-band-title">Band ${index + 1}</div>
                    ${(isGain || isDelay) ? '<div class="effects-peq-band-subtitle">L/R linked</div>' : ''}
                </div>
                ${showRemove ? `<button type="button" class="btn-danger btn-inline" data-peq-remove="${index}">Remove</button>` : '<span class="effects-peq-remove-spacer"></span>'}
            </div>
            <div class="effects-peq-band-fields${isGain ? ' effects-peq-band-fields-gain' : ''}">
                <div class="field-group">
                    <label>Type</label>
                    <select class="url-input" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="filterType">
                        ${['bell', 'notch', 'gain', 'delay', 'low_shelf', 'high_shelf', 'low_pass', 'high_pass'].map(type => `<option value="${type}" ${band.filterType === type ? 'selected' : ''}>${filterTypeLabels[type]}</option>`).join('')}
                    </select>
                </div>
                ${isDelay ? `
                <div class="field-group">
                    <label>Delay (ms)</label>
                    <input type="number" class="url-input" min="0" max="500" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="delayMs" value="${Number.isFinite(Number(band.delayMs)) ? band.delayMs : 0}">
                </div>` : `
                <div class="field-group">
                    <label>Gain (dB)</label>
                    <input type="number" class="url-input" min="-24" max="24" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="gainDb" value="${band.gainDb}">
                </div>
                ${isGain ? '' : `
                <div class="field-group">
                    <label>Freq (Hz)</label>
                    <input type="number" class="url-input" min="20" max="20000" step="1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="frequencyHz" value="${band.frequencyHz}">
                </div>
                <div class="field-group">
                    <label>Q</label>
                    <input type="number" class="url-input" min="0.1" max="20" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="q" value="${band.q}">
                </div>`}`}
            </div>
        </div>
    `;
    }).join('');
    container.querySelectorAll('[data-peq-remove]').forEach(button => {
        button.addEventListener('click', () => {
            removePeqBandPair(Number(button.dataset.peqRemove));
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
            if (field === 'delayMs' && isPeqDelayBand(currentBand) && !live) {
                renderPeqBands();
            }
        };
        input.addEventListener('change', () => handleFieldUpdate(false));
        if (input.dataset.peqField === 'gainDb' || input.dataset.peqField === 'delayMs') {
            input.addEventListener('input', () => handleFieldUpdate(true));
        }
    });
}
function renderPeqBands() {
    if (!state.easyeffects?.peqDraft) {
        state.easyeffects = state.easyeffects || {};
        state.easyeffects.peqDraft = getDefaultPeqDraft();
    }
    normalizeLinkedPeqSpecialBands();
    const draft = state.easyeffects.peqDraft;
    draft.eqMode = normalizePeqEqMode(draft.eqMode);
    if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.value = draft.eqMode;
    renderPeqBandColumn(elements.effectsPeqLeftBands, 'left', draft.leftBands || []);
    renderPeqBandColumn(elements.effectsPeqRightBands, 'right', draft.rightBands || []);
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
    const container = side === 'right' ? elements.effectsPeqRightBands : elements.effectsPeqLeftBands;
    if (!container) return [];
    const draftBands = side === 'right' ? (state.easyeffects?.peqDraft?.rightBands || []) : (state.easyeffects?.peqDraft?.leftBands || []);
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
        if (!isGain && (!Number.isFinite(band.q) || band.q < 0.1 || band.q > 20)) {
            return `${side} band ${index + 1}: Q must be between 0.1 and 20`;
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
    if (peqCreateInFlight) {
        showToast('PEQ preset creation already in progress', 'warning');
        return;
    }
    peqCreateInFlight = true;
    if (!state.easyeffects?.peqDraft) {
        state.easyeffects = state.easyeffects || {};
        state.easyeffects.peqDraft = getDefaultPeqDraft();
    }
    const presetName = elements.effectsPeqPresetName?.value?.trim() || '';
    if (!presetName) {
        peqCreateInFlight = false;
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please enter a PEQ preset name.</div>';
        if (elements.effectsPeqPresetName) elements.effectsPeqPresetName.focus();
        showToast('Please enter a PEQ preset name', 'error');
        return;
    }
    const leftBands = collectPeqBandsFromDom('left');
    const rightBands = collectPeqBandsFromDom('right');
    if (!leftBands.length || !rightBands.length) {
        peqCreateInFlight = false;
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please add at least one Left and one Right band.</div>';
        showToast('Please add at least one Left and one Right band', 'error');
        return;
    }
    const validationError = validatePeqBands('Left', leftBands) || validatePeqBands('Right', rightBands);
    if (validationError) {
        peqCreateInFlight = false;
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(validationError)}</div>`;
        showToast(validationError, 'error');
        return;
    }
    const leftGainTotal = getPeqGainTotal(leftBands);
    const rightGainTotal = getPeqGainTotal(rightBands);
    const dualGainMismatch = Math.abs(leftGainTotal) > 1e-9 && Math.abs(rightGainTotal) > 1e-9 && Math.abs(leftGainTotal - rightGainTotal) > 1e-9;
    if (dualGainMismatch) {
        peqCreateInFlight = false;
        const gainError = 'Gain currently works as shared stereo trim; use the same Gain on both sides or only one shared Gain value.';
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(gainError)}</div>`;
        showToast(gainError, 'error');
        return;
    }
    const eqMode = normalizePeqEqMode(elements.effectsPeqModeSelect?.value || state.easyeffects.peqDraft?.eqMode);
    state.easyeffects.peqDraft.leftBands = leftBands;
    state.easyeffects.peqDraft.rightBands = rightBands;
    state.easyeffects.peqDraft.eqMode = eqMode;
    if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.disabled = true;
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Creating PEQ preset: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/easyeffects/presets/create-peq', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                presetName,
                loadAfterCreate: false,
                ...collectEffectsExtras(),
                peq: {
                    enabled: true,
                    params: {
                        channelMode: 'dual',
                        eqMode,
                        leftBands,
                        rightBands,
                    },
                },
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'PEQ preset creation failed');
        await fetchEffects();
        if (elements.effectsPeqDisclosure) elements.effectsPeqDisclosure.open = false;
        updateEffectsPeqDisclosureLabel();
        resetPeqDraft();
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        showToast(`Created PEQ preset: ${data.preset.name}`, 'success');
    } catch (e) {
        elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">PEQ preset creation failed: ${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'PEQ preset creation failed', 'error');
    } finally {
        if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.disabled = false;
        peqCreateInFlight = false;
    }
}
async function importRewPeqPreset() {
    const file = elements.effectsImportFile?.files?.[0];
    const presetName = file ? file.name.replace(/\.[^.]+$/, '') : '';
    if (!file) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please choose a REW text file.</div>';
        showToast('Please choose a REW text file', 'error');
        return;
    }
    if (!file) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please choose a REW text file.</div>';
        showToast('Please choose a REW text file', 'error');
        return;
    }
    const extras = collectEffectsExtras();
    const formData = new FormData();
    formData.append('preset_name', presetName);
    formData.append('load_after_create', 'false');
    formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
    formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
    formData.append('headroom_gain_db', String(extras.headroomGainDb));
    formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
    formData.append('autogain_target_db', String(extras.autogainTargetDb));
    formData.append('delay_enabled', extras.delayEnabled ? 'true' : 'false');
    formData.append('delay_left_ms', String(extras.delayLeftMs));
    formData.append('delay_right_ms', String(extras.delayRightMs));
    formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
    formData.append('bass_amount', String(extras.bassAmount));
    formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
    formData.append('tone_effect_mode', extras.toneEffectMode);
    formData.append('file', file);
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Importing REW PEQ: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/easyeffects/presets/import-rew-peq', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'REW PEQ import failed');
        await fetchEffects();
        elements.effectsImportFile.value = '';
        updateEffectsImportUi();
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        showToast(`Imported REW PEQ: ${data.preset.name}`, 'success');
    } catch (e) {
        elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">REW PEQ import failed: ${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'REW PEQ import failed', 'error');
    } finally {
        // no-op: submit button removed
    }
}
function renderEffects() {
    const fx = state.easyeffects;
    const presets = fx.presets || [];
    const presetNames = presets.map(p => p.name);
    elements.effectsInfo.textContent = fx.available
        ? `${fx.preset_count} presets`
        : 'EasyEffects is not available';
    if (fx.combineDraft) {
        fx.combineDraft = normalizeEffectsCombineDraft(fx.combineDraft, presetNames);
    }
    if (!fx.available) {
        elements.effectsDeleteBtn.disabled = true;
        if (elements.effectsToggleImportBtn) elements.effectsToggleImportBtn.disabled = true;
        if (elements.effectsRewDualCreatePresetBtn) elements.effectsRewDualCreatePresetBtn.disabled = true;
        if (elements.effectsCombineSaveBtn) elements.effectsCombineSaveBtn.disabled = true;
        if (elements.effectsPeqAddBandBtn) elements.effectsPeqAddBandBtn.disabled = true;
        if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.disabled = true;
        if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.disabled = true;
        elements.effectsStatus.innerHTML = '';
        return;
    }
    if (presets.length === 0) {
        elements.effectsDeleteBtn.disabled = true;
    } else {
        elements.effectsDeleteBtn.disabled = fx.active_preset === 'Direct' || fx.active_preset === 'Neutral';
    }
    if (elements.effectsToggleImportBtn) elements.effectsToggleImportBtn.disabled = false;
    if (elements.effectsRewDualCreatePresetBtn) elements.effectsRewDualCreatePresetBtn.disabled = false;
    if (elements.effectsPeqAddBandBtn) elements.effectsPeqAddBandBtn.disabled = false;
    if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.disabled = false;
    if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.disabled = false;
    renderPeqBands();
    renderEffectsPresetStatus();
    renderEffectsCompare();
    renderEffectsCombine();
}
function renderEffectsPresetStatus() {
    // The preset status is now shown inside the compare row via effectsCompareActive.
    // This function is kept for backwards compatibility but is a no-op.
}
function getEmptyEffectsCompareState() {
    return { presetA: '', presetB: '', activeSide: null };
}

function getEffectsCompareState() {
    const fx = state.easyeffects || {};
    const compare = normalizeEffectsCompareSelection(fx.compare || getEmptyEffectsCompareState());
    const activePreset = fx.active_preset || '';
    const effectiveActiveSide = getEffectiveEffectsCompareSide(compare, activePreset);
    return {
        compare,
        activePreset,
        effectiveActiveSide,
        presetA: compare.presetA || activePreset || '',
        presetB: compare.presetB || '',
    };
}

function getEffectiveEffectsCompareSide(compare, activePreset) {
    if (activePreset && compare?.presetA === activePreset) return 'A';
    if (activePreset && compare?.presetB === activePreset) return 'B';
    return compare?.activeSide || null;
}

function setEffectsCompareLoadBusy(isBusy) {
    effectsCompareLoadInFlight = !!isBusy;
    if (elements.effectsCompareA) elements.effectsCompareA.disabled = effectsCompareLoadInFlight;
    if (elements.effectsCompareB) elements.effectsCompareB.disabled = effectsCompareLoadInFlight;
    if (elements.effectsCompareToggle) elements.effectsCompareToggle.disabled = effectsCompareLoadInFlight;
}

function getEffectsChainLabelForPreset(presetName, presetMap = new Map()) {
    if (!presetName) return 'Chain: —';
    const preset = presetMap.get(presetName);
    const sourcePresets = Array.isArray(preset?.source_presets)
        ? preset.source_presets.map(name => String(name || '').trim()).filter(Boolean)
        : [];
    if (sourcePresets.length >= 2) {
        return `Chain: ${sourcePresets.join(' → ')}`;
    }
    return 'Chain: Single preset';
}

function renderPresetDownloadLink(presetName = '') {
    const cleanName = String(presetName || '').trim();
    if (!cleanName) return '—';
    return `<a href="${escapeHtml(presetFileUrl(cleanName))}">${escapeHtml(cleanName)}</a>`;
}

function renderEffectsCompare() {
    const fx = state.easyeffects;
    const presetEntries = fx.presets || [];
    const presets = presetEntries.map(p => p.name);
    const presetMap = new Map(presetEntries.map(preset => [preset.name, preset]));
    const { compare, activePreset, effectiveActiveSide, presetA, presetB } = getEffectsCompareState();

    if (!elements.effectsCompareRow) return;
    if (presets.length === 0) {
        elements.effectsCompareRow.style.display = 'none';
        return;
    }
    elements.effectsCompareRow.style.display = '';

    elements.effectsCompareA.innerHTML = presets.map(n =>
        `<option value="${escapeHtml(n)}" ${n === presetA ? 'selected' : ''}>${escapeHtml(n)}</option>`
    ).join('');

    elements.effectsCompareB.innerHTML = [`<option value="" ${!presetB ? 'selected' : ''}>Select preset…</option>`].concat(
        presets.map(n => `<option value="${escapeHtml(n)}" ${n === presetB ? 'selected' : ''}>${escapeHtml(n)}</option>`)
    ).join('');

    let activeLabel = 'Listening: —';
    let chainPresetName = '';
    if (effectiveActiveSide === 'A' && compare.presetA) {
        activeLabel = `Listening: A · ${compare.presetA}`;
        chainPresetName = compare.presetA;
    } else if (effectiveActiveSide === 'B' && compare.presetB) {
        activeLabel = `Listening: B · ${compare.presetB}`;
        chainPresetName = compare.presetB;
    } else if (activePreset) {
        activeLabel = `Listening: ${activePreset}`;
        chainPresetName = activePreset;
    }
    if (elements.effectsCompareActive) {
        if (effectiveActiveSide === 'A' && compare.presetA) {
            elements.effectsCompareActive.innerHTML = `Listening: A · ${renderPresetDownloadLink(compare.presetA)}`;
        } else if (effectiveActiveSide === 'B' && compare.presetB) {
            elements.effectsCompareActive.innerHTML = `Listening: B · ${renderPresetDownloadLink(compare.presetB)}`;
        } else if (activePreset) {
            elements.effectsCompareActive.innerHTML = `Listening: ${renderPresetDownloadLink(activePreset)}`;
        } else {
            elements.effectsCompareActive.textContent = activeLabel;
        }
    }
    if (elements.effectsCompareChain) {
        const chainLabel = getEffectsChainLabelForPreset(chainPresetName, presetMap);
        elements.effectsCompareChain.textContent = chainLabel;
    }
    const badge = document.getElementById('effects-compare-active-badge');
    if (badge) {
        badge.classList.toggle('is-side-a', effectiveActiveSide === 'A');
        badge.classList.toggle('is-side-b', effectiveActiveSide === 'B');
    }
    document.querySelectorAll('.effects-compare-slot').forEach((slotEl) => {
        const slot = slotEl.dataset.compareSlot;
        const slotPreset = slot === 'A' ? presetA : presetB;
        slotEl.classList.toggle('is-active', effectiveActiveSide === slot && !!slotPreset);
        slotEl.classList.toggle('is-armed', effectiveActiveSide !== slot && !!slotPreset);
    });
    setEffectsCompareLoadBusy(effectsCompareLoadInFlight);
}

function getEffectsCombineValidationState() {
    const preset1 = elements.effectsCombinePreset1?.value || '';
    const preset2 = elements.effectsCombinePreset2?.value || '';
    const preset3 = elements.effectsCombinePreset3?.value || '';
    const presetName = elements.effectsCombinePresetName?.value?.trim() || '';
    const selectedPresets = [preset1, preset2, preset3].filter(Boolean);
    const isDuplicateSelection = new Set(selectedPresets).size !== selectedPresets.length;
    return {
        preset1,
        preset2,
        preset3,
        presetName,
        selectedPresets,
        isValid: selectedPresets.length >= 2 && !!presetName && !isDuplicateSelection,
        isDuplicateSelection,
    };
}

function renderEffectsCombine() {
    const fx = state.easyeffects || {};
    const presets = (fx.presets || []).map(p => p.name);
    const draft = fx.combineDraft || getDefaultEffectsCombineDraft();
    if (!elements.effectsCombinePreset1 || !elements.effectsCombinePreset2 || !elements.effectsCombinePreset3 || !elements.effectsCombinePresetName) return;

    const normalized = normalizeEffectsCombineDraft(draft, presets);
    fx.combineDraft = normalized;

    elements.effectsCombinePreset1.innerHTML = [`<option value="" ${!normalized.preset1 ? 'selected' : ''}>Select preset…</option>`].concat(
        presets.map(n => `<option value="${escapeHtml(n)}" ${n === normalized.preset1 ? 'selected' : ''}>${escapeHtml(n)}</option>`)
    ).join('');
    elements.effectsCombinePreset2.innerHTML = [`<option value="" ${!normalized.preset2 ? 'selected' : ''}>Select preset…</option>`].concat(
        presets.map(n => `<option value="${escapeHtml(n)}" ${n === normalized.preset2 ? 'selected' : ''}>${escapeHtml(n)}</option>`)
    ).join('');
    elements.effectsCombinePreset3.innerHTML = [`<option value="" ${!normalized.preset3 ? 'selected' : ''}>Optional…</option>`].concat(
        presets.map(n => `<option value="${escapeHtml(n)}" ${n === normalized.preset3 ? 'selected' : ''}>${escapeHtml(n)}</option>`)
    ).join('');
    if (document.activeElement !== elements.effectsCombinePresetName) {
        elements.effectsCombinePresetName.value = normalized.presetName || '';
    }

    const validation = getEffectsCombineValidationState();
    if (elements.effectsCombineSaveBtn) {
        elements.effectsCombineSaveBtn.disabled = !fx.available || !validation.isValid;
    }
}

async function createCombinedEffectsPreset() {
    const validation = getEffectsCombineValidationState();
    if (validation.isDuplicateSelection) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Each selected preset must be different.</div>';
        showToast('Each selected preset must be different', 'error');
        return;
    }
    if (validation.selectedPresets.length < 2) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Choose at least two presets to combine.</div>';
        showToast('Choose at least two presets to combine', 'error');
        return;
    }
    if (!validation.presetName) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please enter a new preset name.</div>';
        showToast('Please enter a new preset name', 'error');
        elements.effectsCombinePresetName?.focus();
        return;
    }

    if (elements.effectsCombineSaveBtn) elements.effectsCombineSaveBtn.disabled = true;
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Saving combined preset: <strong>${escapeHtml(validation.presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/easyeffects/presets/combine', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                presetName: validation.presetName,
                presetNames: validation.selectedPresets,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Combined preset save failed');
        state.easyeffects.combineDraft = getDefaultEffectsCombineDraft();
        await fetchEffects();
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        showToast(`Created combined preset: ${data.preset.name}`, 'success');
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message || 'Combined preset save failed')}</div>`;
        showToast(e.message || 'Combined preset save failed', 'error');
    } finally {
        renderEffectsCombine();
    }
}

async function loadEffectsComparePreset(target, newSide, targetA, targetB) {
    if (effectsCompareLoadInFlight) return;
    setEffectsCompareLoadBusy(true);
    try {
        const resp = await fetch('/api/easyeffects/presets/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_name: target }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Failed to load preset');
        state.easyeffects.active_preset = target;
        state.easyeffects.compare = {
            presetA: targetA,
            presetB: targetB,
            activeSide: newSide,
        };
        await saveEffectsCompareState(state.easyeffects.compare);
        renderEffects();
    } finally {
        setEffectsCompareLoadBusy(false);
    }
}

async function handleEffectsCompareSelectionChange(slot) {
    if (effectsCompareLoadInFlight) return;
    state.easyeffects.compare = normalizeEffectsCompareSelection(state.easyeffects.compare || getEmptyEffectsCompareState());

    const previousCompare = {
        presetA: state.easyeffects.compare.presetA || '',
        presetB: state.easyeffects.compare.presetB || '',
        activeSide: state.easyeffects.compare.activeSide || null,
    };
    const activePreset = state.easyeffects?.active_preset || '';
    const effectiveActiveSide = getEffectiveEffectsCompareSide(previousCompare, activePreset);

    let targetA = elements.effectsCompareA?.value || '';
    let targetB = elements.effectsCompareB?.value || '';
    if (targetA && targetB && targetA === targetB) {
        if (slot === 'B') {
            targetB = '';
            if (elements.effectsCompareB) elements.effectsCompareB.value = '';
            showToast('A and B must use different presets', 'warning');
        } else {
            targetB = '';
        }
    }
    state.easyeffects.compare = normalizeEffectsCompareSelection({
        presetA: targetA,
        presetB: targetB,
        activeSide: state.easyeffects.compare.activeSide || null,
    });
    await saveEffectsCompareState(state.easyeffects.compare);

    const selectedValue = slot === 'A' ? targetA : targetB;
    const shouldAutoload = !!selectedValue && selectedValue !== activePreset && (!effectiveActiveSide || effectiveActiveSide === slot);

    if (shouldAutoload) {
        await loadEffectsComparePreset(selectedValue, slot, state.easyeffects.compare.presetA, state.easyeffects.compare.presetB);
    } else {
        renderEffectsCompare();
    }
}

function getEffectsCompareToggleTarget({ effectiveActiveSide, activePreset, presetA, presetB }) {
    if (effectiveActiveSide === 'A' && presetB) return { target: presetB, side: 'B' };
    if (effectiveActiveSide === 'B' && presetA) return { target: presetA, side: 'A' };
    if (presetB && presetA === activePreset) return { target: presetB, side: 'B' };
    if (presetA) return { target: presetA, side: 'A' };
    if (presetB) return { target: presetB, side: 'B' };
    return { target: null, side: null };
}

async function toggleComparePreset() {
    if (effectsCompareLoadInFlight) return;
    try {
        const compareState = getEffectsCompareState();
        if (!compareState.presetA && !compareState.presetB) {
            showToast('Select a preset in A or B first', 'warning');
            return;
        }

        const { target, side } = getEffectsCompareToggleTarget(compareState);
        if (!target || !side) {
            showToast('Select a preset in A or B first', 'warning');
            return;
        }

        await loadEffectsComparePreset(target, side, compareState.presetA, compareState.presetB);
    } catch (e) {
        console.error('toggleComparePreset error:', e);
        showToast(e.message || 'Failed to toggle preset', 'error');
    }
}

function setupEffectsCompareActions() {
    if (elements.effectsCompareToggle) {
        elements.effectsCompareToggle.addEventListener('click', toggleComparePreset);
    }
    if (elements.effectsCompareA) {
        elements.effectsCompareA.addEventListener('change', async () => {
            try {
                await handleEffectsCompareSelectionChange('A');
            } catch (e) {
                console.error('compare preset A change error:', e);
                showToast(e.message || 'Failed to load preset', 'error');
            }
        });
    }
    if (elements.effectsCompareB) {
        elements.effectsCompareB.addEventListener('change', async () => {
            try {
                await handleEffectsCompareSelectionChange('B');
            } catch (e) {
                console.error('compare preset B change error:', e);
                showToast(e.message || 'Failed to load preset', 'error');
            }
        });
    }
}

async function switchEffectsPreset() {
    // Preset switching now goes exclusively through toggleComparePreset
    // (or the A/B dropdowns) — this function is kept only as a harmless stub
    // for older callers / delete-from-active flow remnants.
    return null;
}

// Track which inputs are currently being edited by the user
const _activeEditing = new Set();
const EFFECTS_HEADROOM_ALLOWED_GAIN_DB = new Set([-2, -3, -4, -5, -6]);
const EFFECTS_AUTOGAIN_ALLOWED_TARGET_DB = new Set([-9, -12, -15, -18]);
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

function normalizeEffectsToneEffectMode(value, fallback = 'crystalizer') {
    const normalized = String(value || fallback).trim().toLowerCase();
    return EFFECTS_TONE_EFFECT_MODES.has(normalized) ? normalized : fallback;
}

function applyEffectsExtras(extras = {}) {
    if (elements.effectsLimiterEnabled) elements.effectsLimiterEnabled.checked = !!extras.limiterEnabled;
    if (elements.effectsHeadroomEnabled) elements.effectsHeadroomEnabled.checked = !!extras.headroomEnabled;
    if (elements.effectsHeadroomGainDb && !_activeEditing.has(elements.effectsHeadroomGainDb)) {
        elements.effectsHeadroomGainDb.value = String(normalizeEffectsHeadroomGainDb(extras.headroomGainDb, -3));
    }
    if (elements.effectsAutogainEnabled) elements.effectsAutogainEnabled.checked = !!extras.autogainEnabled;
    if (elements.effectsAutogainTargetDb && !_activeEditing.has(elements.effectsAutogainTargetDb)) {
        elements.effectsAutogainTargetDb.value = String(normalizeEffectsAutogainTargetDb(extras.autogainTargetDb, -12));
    }
    if (elements.effectsDelayEnabled) elements.effectsDelayEnabled.checked = !!extras.delayEnabled;
    if (elements.effectsDelayLeftMs && !_activeEditing.has(elements.effectsDelayLeftMs)) elements.effectsDelayLeftMs.value = String(Number(extras.delayLeftMs || 0));
    if (elements.effectsDelayRightMs && !_activeEditing.has(elements.effectsDelayRightMs)) elements.effectsDelayRightMs.value = String(Number(extras.delayRightMs || 0));
    if (elements.effectsBassEnabled) elements.effectsBassEnabled.checked = !!extras.bassEnabled;
    // Only update amount when bass is enabled — otherwise keep field value (user may re-enable)
    if (elements.effectsBassAmount && !!extras.bassEnabled && !_activeEditing.has(elements.effectsBassAmount)) {
        elements.effectsBassAmount.value = String(Number(extras.bassAmount || 0));
    }
    if (elements.effectsToneEffectEnabled) elements.effectsToneEffectEnabled.checked = !!extras.toneEffectEnabled;
    if (elements.effectsToneEffectMode && !_activeEditing.has(elements.effectsToneEffectMode)) {
        elements.effectsToneEffectMode.value = normalizeEffectsToneEffectMode(extras.toneEffectMode, 'crystalizer');
    }
    updateEffectsExtrasUi();
}

function updateEffectsExtrasUi() {
    if (elements.effectsHeadroomGainWrap) {
        elements.effectsHeadroomGainWrap.classList.toggle('hidden', !elements.effectsHeadroomEnabled?.checked);
    }
    if (elements.effectsAutogainTargetWrap) {
        elements.effectsAutogainTargetWrap.classList.toggle('hidden', !elements.effectsAutogainEnabled?.checked);
    }
    if (elements.effectsDelayInputsWrap) {
        elements.effectsDelayInputsWrap.classList.toggle('hidden', !elements.effectsDelayEnabled?.checked);
    }
    if (elements.effectsBassControlsWrap) {
        elements.effectsBassControlsWrap.classList.toggle('hidden', !elements.effectsBassEnabled?.checked);
    }
    if (elements.effectsToneEffectWrap) {
        elements.effectsToneEffectWrap.classList.toggle('hidden', !elements.effectsToneEffectEnabled?.checked);
    }
}

function loadSavedEffectsExtras() {
    const fx = state.easyeffects;
    if (!fx?.global_extras) return;
    applyEffectsExtras({
        limiterEnabled: !!fx.global_extras?.limiter?.enabled,
        headroomEnabled: !!fx.global_extras?.headroom?.enabled,
        headroomGainDb: Number(fx.global_extras?.headroom?.params?.gainDb ?? -3),
        autogainEnabled: !!fx.global_extras?.autogain?.enabled,
        autogainTargetDb: Number(fx.global_extras?.autogain?.params?.targetDb ?? -12),
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
    parts.push(extras.autogainEnabled ? `Autogain ON (${extras.autogainTargetDb} dB)` : 'Autogain OFF');
    parts.push(extras.toneEffectEnabled ? `Tone ON (${normalizeEffectsToneEffectMode(extras.toneEffectMode, 'crystalizer')})` : 'Tone OFF');
    return parts.join(' • ');
}

function showEffectsExtrasFeedback(message, isSuccess = true) {
    const feedbackEl = document.getElementById('effects-extras-feedback');
    if (feedbackEl) {
        feedbackEl.textContent = message;
        feedbackEl.className = isSuccess ? 'effects-feedback success' : 'effects-feedback error';
        feedbackEl.style.display = 'block';
        window.clearTimeout(showEffectsExtrasFeedback._timer);
        showEffectsExtrasFeedback._timer = window.setTimeout(() => {
            feedbackEl.style.display = 'none';
        }, 3200);
    }
}

let _extrasDebounceTimer = null;
let effectsExtrasSaveInFlight = false;
let effectsExtrasPendingResave = false;
function saveEffectsExtrasDebounced(delayMs = EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS) {
    window.clearTimeout(_extrasDebounceTimer);
    if (delayMs <= 0) {
        _doSaveEffectsExtras('saving');
        return;
    }
    _extrasDebounceTimer = window.setTimeout(() => {
        _doSaveEffectsExtras('saving');
    }, delayMs);
}

function collectEffectsExtras() {
    return {
        limiterEnabled: elements.effectsLimiterEnabled?.checked || false,
        headroomEnabled: elements.effectsHeadroomEnabled?.checked || false,
        headroomGainDb: normalizeEffectsHeadroomGainDb(elements.effectsHeadroomGainDb?.value, -3),
        autogainEnabled: elements.effectsAutogainEnabled?.checked || false,
        autogainTargetDb: normalizeEffectsAutogainTargetDb(elements.effectsAutogainTargetDb?.value, -12),
        delayEnabled: elements.effectsDelayEnabled?.checked || false,
        delayLeftMs: parseFloat(elements.effectsDelayLeftMs?.value || '0'),
        delayRightMs: parseFloat(elements.effectsDelayRightMs?.value || '0'),
        bassEnabled: elements.effectsBassEnabled?.checked || false,
        bassAmount: parseFloat(elements.effectsBassAmount?.value || '0'),
        toneEffectEnabled: elements.effectsToneEffectEnabled?.checked || false,
        toneEffectMode: normalizeEffectsToneEffectMode(elements.effectsToneEffectMode?.value, 'crystalizer'),
    };
}

async function _doSaveEffectsExtras(phase) {
    if (effectsCompareLoadInFlight || effectsExtrasSaveInFlight) {
        effectsExtrasPendingResave = true;
        return;
    }
    effectsExtrasSaveInFlight = true;
    effectsExtrasPendingResave = false;
    if (phase === 'saving') {
        setEffectsExtrasFeedback('Saving…', '');
    }
    const extras = collectEffectsExtras();
    try {
        const resp = await fetch('/api/easyeffects/extras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(extras),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save output extras');
        state.easyeffects = state.easyeffects || {};
        state.easyeffects.global_extras = data.extras || {
            limiter: { enabled: !!extras.limiterEnabled, params: { thresholdDb: -1.0, attackMs: 5.0, releaseMs: 50.0, lookaheadMs: 5.0, stereoLinkPercent: 100.0 } },
            headroom: { enabled: !!extras.headroomEnabled, params: { gainDb: extras.headroomGainDb } },
            autogain: { enabled: !!extras.autogainEnabled, params: { targetDb: extras.autogainTargetDb } },
            delay: { enabled: !!extras.delayEnabled, params: { leftMs: extras.delayLeftMs, rightMs: extras.delayRightMs } },
            bass_enhancer: { enabled: !!extras.bassEnabled, params: { amount: extras.bassAmount, harmonics: 8.5, scope: 100.0, blend: 0.0 } },
            tone_effect: { enabled: !!extras.toneEffectEnabled, mode: extras.toneEffectMode },
        };
        setEffectsExtrasFeedback('Saved', 'success');
        renderEffects();
    } catch (error) {
        setEffectsExtrasFeedback('Failed', 'error');
        showToast(error.message || 'Failed to save output extras', 'error');
    } finally {
        effectsExtrasSaveInFlight = false;
        if (effectsExtrasPendingResave) {
            effectsExtrasPendingResave = false;
            saveEffectsExtrasDebounced(EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS);
        }
    }
}

function setEffectsExtrasFeedback(message, cls) {
    if (!elements.effectsExtrasFeedback) return;
    elements.effectsExtrasFeedback.textContent = message;
    elements.effectsExtrasFeedback.className = 'effects-extras-feedback' + (cls ? ' ' + cls : '');
}

function detectEffectsImportType(file) {
    if (!file || !file.name) return null;
    const lowerName = file.name.toLowerCase();
    if (lowerName.endsWith('.irs') || lowerName.endsWith('.wav')) return 'convolver';
    if (lowerName.endsWith('.json')) return 'preset-json';
    return null;
}

function updateEffectsImportUi() {
    const file = elements.effectsImportFile?.files?.[0] || null;
    const detectedType = detectEffectsImportType(file);
    if (elements.effectsImportFile) {
        elements.effectsImportFile.accept = '.irs,.wav,.json,audio/wav,application/json';
    }
    if (elements.effectsImportFilename) {
        if (!file) {
            elements.effectsImportFilename.textContent = 'Stereo convolver .irs/.wav or Preset .json';
        } else if (detectedType === 'convolver' || detectedType === 'preset-json') {
            elements.effectsImportFilename.textContent = file.name;
        } else {
            elements.effectsImportFilename.textContent = `Unsupported file: ${file.name}`;
        }
    }
    const importArea = document.getElementById('effects-import-area');
    if (importArea) {
        importArea.classList.toggle('is-ready', detectedType === 'convolver' || detectedType === 'preset-json');
    }
}

function handleEffectsImportFileChange() {
    updateEffectsImportUi();
    const file = elements.effectsImportFile?.files?.[0] || null;
    if (detectEffectsImportType(file)) {
        void submitEffectsImport();
    }
}

async function submitEffectsImport() {
    const file = elements.effectsImportFile?.files?.[0];
    const detectedType = detectEffectsImportType(file);
    if (!file) {
        elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please choose an import file first.</div>';
        showToast('Please choose an import file first', 'error');
        return;
    }
    if (detectedType === 'convolver') {
        return createConvolverPreset();
    }
    if (detectedType === 'preset-json') {
        return importEffectsPresetJson();
    }
    elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Unsupported import file type. Use .irs, .wav, or preset .json.</div>';
    showToast('Unsupported import file type', 'error');
}

async function importEffectsPresetJson() {
    if (effectsImportInFlight) {
        showToast('Import already in progress', 'warning');
        return;
    }
    effectsImportInFlight = true;
    const file = elements.effectsImportFile?.files?.[0] || null;
    if (!file) {
        effectsImportInFlight = false;
        showToast('Please select a preset JSON file first', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('load_after_create', 'false');
    formData.append('file', file);
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Importing preset: <strong>${escapeHtml(file.name)}</strong>…</div>`;
    const importArea = document.getElementById('effects-import-area');
    if (importArea) importArea.classList.add('is-busy');
    try {
        const resp = await fetch('/api/easyeffects/presets/import-json', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Preset JSON import failed');
        await fetchEffects();
        if (elements.effectsImportFile) elements.effectsImportFile.value = '';
        updateEffectsImportUi();
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        showToast(`Imported preset: ${data.preset?.name || file.name}`, 'success');
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'Preset JSON import failed', 'error');
    } finally {
        if (importArea) importArea.classList.remove('is-busy');
        effectsImportInFlight = false;
    }
}
async function createConvolverPreset() {
    if (effectsImportInFlight) {
        showToast('Import already in progress', 'warning');
        return;
    }
    effectsImportInFlight = true;
    const file = elements.effectsImportFile.files[0];
    const presetName = file ? file.name.replace(/\.[^.]+$/, '') : '';
    if (!file) {
        effectsImportInFlight = false;
        showToast('Please select a stereo IR file first', 'error');
        return;
    }
    const extras = collectEffectsExtras();
    const formData = new FormData();
    formData.append('preset_name', presetName);
    formData.append('load_after_create', 'false');
    formData.append('limiter_enabled', extras.limiterEnabled ? 'true' : 'false');
    formData.append('headroom_enabled', extras.headroomEnabled ? 'true' : 'false');
    formData.append('headroom_gain_db', String(extras.headroomGainDb));
    formData.append('autogain_enabled', extras.autogainEnabled ? 'true' : 'false');
    formData.append('autogain_target_db', String(extras.autogainTargetDb));
    formData.append('delay_enabled', extras.delayEnabled ? 'true' : 'false');
    formData.append('delay_left_ms', String(extras.delayLeftMs));
    formData.append('delay_right_ms', String(extras.delayRightMs));
    formData.append('bass_enabled', extras.bassEnabled ? 'true' : 'false');
    formData.append('bass_amount', String(extras.bassAmount));
    formData.append('tone_effect_enabled', extras.toneEffectEnabled ? 'true' : 'false');
    formData.append('tone_effect_mode', extras.toneEffectMode);
    formData.append('file', file);
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Importing: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    const importArea = document.getElementById('effects-import-area');
    if (importArea) importArea.classList.add('is-busy');
    try {
        const resp = await fetch('/api/easyeffects/presets/create-with-ir', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Preset creation failed');
        await fetchEffects();
        elements.effectsImportFile.value = '';
        updateEffectsImportUi();
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        showToast(`Imported preset: ${data.preset.name}`, 'success');
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'Preset creation failed', 'error');
    } finally {
        if (importArea) importArea.classList.remove('is-busy');
        effectsImportInFlight = false;
    }
}
async function deleteEffectsPreset() {
    const presetName = state.easyeffects.active_preset;
    if (!presetName) {
        showToast('No active preset to delete', 'warning');
        return;
    }
    if (presetName === 'Direct' || presetName === 'Neutral') {
        showToast(`Preset "${presetName}" is built-in and cannot be deleted`, 'error');
        return;
    }
    if (!confirm(`Delete preset "${presetName}"?`)) return;
    elements.effectsDeleteBtn.disabled = true;
    try {
        const resp = await fetch('/api/easyeffects/presets/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_name: presetName }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Preset delete failed');
        await fetchEffects();
        showToast(`Deleted preset: ${presetName}`, 'success');
    } catch (e) {
        showToast(e.message || 'Preset delete failed', 'error');
    } finally {
        elements.effectsDeleteBtn.disabled = false;
    }
}
function updateOfflineIndicator() {
    updateConnectionBadge(state.wsConnected);
    if (state.wsConnected) {
        elements.offlineIndicator.classList.add('hidden');
    } else {
        elements.offlineIndicator.classList.remove('hidden');
    }
}
function updateConnectionBadge(online) {
    if (!elements.connDot || !elements.connText) return;
    if (online) {
        elements.connDot.className = 'connection-dot online';
        elements.connText.textContent = 'Online';
    } else {
        elements.connDot.className = 'connection-dot offline';
        elements.connText.textContent = 'Offline';
    }
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
// Library actions
function setupLibraryActions() {
    elements.refreshLibraryBtn.addEventListener('click', refreshLibrary);
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
        elements.toggleImportBtn.textContent = '− Close';
    });
    if (elements.librarySearchInput) {
        elements.librarySearchInput.addEventListener('input', (event) => setLibrarySearchQuery(event.target.value));
        elements.librarySearchInput.addEventListener('search', (event) => setLibrarySearchQuery(event.target.value));
    }
    if (elements.playSelectedTracksBtn) {
        // Legacy button removed from markup, keep null-safe no-op path only.
    }
    if (elements.selectAllTracksBtn) {
        elements.selectAllTracksBtn.addEventListener('click', toggleVisibleTrackSelection);
    }
    if (elements.downloadSelectedTracksBtn) {
        elements.downloadSelectedTracksBtn.addEventListener('click', downloadSelectedTracks);
    }
    if (elements.savePlaylistBtn) {
        elements.savePlaylistBtn.addEventListener('click', savePlaylist);
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
    seekDragging = true;
    if (window.__footerSource === 'spotify') window.__spotifySeeking = true;
}
function seekEnd() {
    seekDragging = false;
    if (window.__footerSource === 'spotify') {
        window.__spotifySeeking = false;
        const spotifyData = window.__spotifyLastData;
        if (spotifyData && spotifyData.duration) {
            const posSec = (parseInt(elements.seekSlider.value, 10) / 1000) * spotifyData.duration;
            spotifySeek(posSec);
        }
        return;
    }
    if (seekPendingPos !== null && state.playback.duration > 0) {
        doSeek(seekPendingPos);
        seekPendingPos = null;
    }
}
function seekChange() {
    const pos = parseInt(elements.seekSlider.value, 10) || 0;
    if (window.__footerSource === 'spotify') {
        const spotifyData = window.__spotifyLastData;
        const duration = spotifyData?.duration || 0;
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
function formatTime(s) {
    if (!s || isNaN(s) || s < 0) return '0:00';
    const m = Math.floor(s / 60);
    const sec = Math.floor(s % 60);
    return m + ':' + (sec < 10 ? '0' : '') + sec;
}
function updateSeekUI() {
    if (!elements.seekSlider || !elements.seekCurrent || !elements.seekDuration) return;
    const duration = state.playback.duration || 0;
    const position = state.playback.position || 0;
    elements.seekDuration.textContent = formatTime(duration);
    if (!seekDragging) {
        elements.seekCurrent.textContent = formatTime(position);
        if (duration > 0) {
            elements.seekSlider.value = Math.round((position / duration) * 1000);
        } else {
            elements.seekSlider.value = 0;
        }
    }
}
// Utilities
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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
const spotifyElements = {
    unavailable: document.getElementById('spotify-unavailable'),
    unavailableMsg: document.getElementById('spotify-unavailable-msg'),
    player: document.getElementById('spotify-player'),
    cover: document.getElementById('spotify-cover'),
    title: document.getElementById('spotify-title'),
    artist: document.getElementById('spotify-artist'),
    album: document.getElementById('spotify-album'),
    toggle: document.getElementById('spotify-toggle'),
    prev: document.getElementById('spotify-prev'),
    next: document.getElementById('spotify-next'),
    statusLine: document.getElementById('spotify-status'),
    secondaryControls: document.getElementById('spotify-secondary-controls'),
    shuffle: document.getElementById('spotify-shuffle'),
    loop: document.getElementById('spotify-loop'),
    loopIcon: document.getElementById('spotify-loop-icon'),
    loopLabel: document.getElementById('spotify-loop-label'),
    progress: document.getElementById('spotify-progress'),
    timeCurrent: document.getElementById('spotify-time-current'),
    timeTotal: document.getElementById('spotify-time-total'),
    tabBtn: document.querySelector('[data-tab="spotify"]'),
};

let _spotifyPollTimer = null;
let _spotifySeeking = false;   // true while user drags slider
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
    if (!sec || !isFinite(sec) || sec < 0) return '0:00';
    const m = Math.floor(sec / 60);
    const s = Math.floor(sec % 60);
    return `${m}:${s < 10 ? '0' : ''}${s}`;
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
        return { available: false, installed: false, source: 'spotify', capabilities: {}, status: 'Stopped', artist: '', title: '', album: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
    }
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------
function spotifyTrackKey(data) {
    return [data?.title || '', data?.artist || '', data?.album || '', Math.round(Number(data?.duration || 0))].join('|');
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

function handleIncomingSpotifyState(data, options = {}) {
    if (!data) return;
    const { renderTab = true, renderFooter = true } = options;
    if (spotifyElements.tabBtn) {
        spotifyElements.tabBtn.style.display = data.installed === false ? 'none' : '';
    }
    const previousTrackKey = spotifyTrackKey(window.__spotifyLastData || {});
    const nextTrackKey = spotifyTrackKey(data);
    const trackChanged = previousTrackKey !== nextTrackKey;

    footerDebug('incoming-spotify-state', {
        payload: {
            title: data?.title || null,
            artist: data?.artist || null,
            status: data?.status || null,
            available: !!data?.available,
        },
        renderTab,
        renderFooter,
        previousTrackKey,
        nextTrackKey,
        trackChanged,
    });

    window.__spotifyLastData = data;
    if (shouldAdoptSpotifyUpdate(data)) {
        syncSpotifySourceOwnership(data);
    }
    reconcileFooterSource();

    if (trackChanged) {
        _spotifyLastRenderedTrackKey = nextTrackKey;
        _spotifyLastPositionUpdateAt = Date.now();
    }

    if (renderFooter && window.__footerSource === 'spotify') {
        updateFooterForSpotify(data);
    }
    if (renderTab) {
        const spotifyTab = document.getElementById('tab-spotify');
        if (spotifyTab && spotifyTab.classList.contains('active')) {
            renderSpotifyTab(data);
        }
    }
}

function renderSpotify(data) {
    const el = spotifyElements;
    if (!el.unavailable || !el.player) return;

    const caps = data.capabilities || {};

    // ---- Tab visibility: hide tab entirely if Spotify not installed ----
    if (el.tabBtn) {
        el.tabBtn.style.display = data.installed === false ? 'none' : '';
    }

    // ---- Available check (playerctl missing) ----
    if (!data.available) {
        el.unavailable.style.display = '';
        el.player.style.display = 'none';
        el.unavailableMsg.textContent = 'playerctl is not installed. Install it to control Spotify.';
        updateGlobalControlsForSource();
        return;
    }

    // ---- Spotify installed but not running ----
    if (data.status === 'Stopped' && !data.title) {
        el.unavailable.style.display = '';
        el.player.style.display = 'none';
        el.unavailableMsg.textContent = 'Spotify is not running.';
        updateGlobalControlsForSource();
        return;
    }

    // ---- Normal state ----
    el.unavailable.style.display = 'none';
    el.player.style.display = '';

    // Cover art — only update src if changed
    if (data.artUrl) {
        if (el.cover.src !== data.artUrl) el.cover.src = data.artUrl;
        el.cover.style.display = '';
    } else {
        el.cover.style.display = 'none';
    }

    // Track info
    if (el.title.textContent !== (data.title || '—')) el.title.textContent = data.title || '—';
    if (el.artist.textContent !== (data.artist || '—')) el.artist.textContent = data.artist || '—';
    if (el.album.textContent !== (data.album || '—')) el.album.textContent = data.album || '—';

    // Play/Pause
    const playing = data.status === 'Playing';
    const icon = playing ? '⏸' : '▶';
    if (el.toggle.textContent !== icon) {
        el.toggle.textContent = icon;
        el.toggle.title = playing ? 'Pause' : 'Play';
    }

    // Shuffle (capability-gated)
    if (el.shuffle) {
        el.shuffle.style.display = caps.shuffle ? '' : 'none';
        el.shuffle.classList.toggle('active', !!data.shuffle);
        el.shuffle.title = data.shuffle ? 'Shuffle on' : 'Shuffle off';
        el.shuffle.setAttribute('aria-pressed', data.shuffle ? 'true' : 'false');
    }

    // Loop (capability-gated)
    if (el.loop) {
        el.loop.style.display = caps.loop ? '' : 'none';
        const loopVal = data.loop || 'none';
        const loopActive = loopVal !== 'none';
        el.loop.classList.toggle('active', loopActive);
        el.loop.dataset.mode = loopVal;
        el.loop.setAttribute('aria-pressed', loopActive ? 'true' : 'false');
        if (loopVal === 'track') {
            if (el.loopIcon) el.loopIcon.textContent = '🔂';
            if (el.loopLabel) el.loopLabel.textContent = 'Loop';
            el.loop.title = 'Loop: track';
        } else if (loopVal === 'playlist') {
            if (el.loopIcon) el.loopIcon.textContent = '🔁';
            if (el.loopLabel) el.loopLabel.textContent = 'Loop';
            el.loop.title = 'Loop: playlist';
        } else {
            if (el.loopIcon) el.loopIcon.textContent = '🔁';
            if (el.loopLabel) el.loopLabel.textContent = 'Loop';
            el.loop.title = 'Loop: off';
        }
    }

    // Secondary controls row
    if (el.secondaryControls) {
        el.secondaryControls.style.display = (caps.shuffle || caps.loop) ? '' : 'none';
    }

    // Progress / seek (capability-gated, don't update while seeking)
    if (caps.progress && el.progress && !_spotifySeeking) {
        const pos = Number(data.position || 0);
        const dur = Number(data.duration || 0);
        const pct = dur > 0 ? Math.max(0, Math.min(100, (pos / dur) * 100)) : 0;
        el.progress.value = pct;
        el.progress.max = 100;
        if (el.timeCurrent) el.timeCurrent.textContent = formatTime(pos);
        if (el.timeTotal) el.timeTotal.textContent = formatTime(dur);
        _spotifyLastPositionUpdateAt = Date.now();
    }
    if (el.progress) {
        el.progress.style.display = caps.seek ? '' : 'none';
    }
    if (el.timeCurrent) el.timeCurrent.style.display = caps.progress ? '' : 'none';
    if (el.timeTotal) el.timeTotal.style.display = caps.progress ? '' : 'none';

    // Status line
    const statusBits = [];
    if (data.status) statusBits.push(data.status.toLowerCase());
    if (caps.shuffle) statusBits.push(data.shuffle ? 'shuffle on' : 'shuffle off');
    if (caps.loop) statusBits.push(`loop ${data.loop || 'none'}`);
    const statusText = statusBits.join(' · ');
    if (el.statusLine.textContent !== statusText) el.statusLine.textContent = statusText;
    // Update global footer controls when spotify data changes
    updateGlobalControlsForSource();
}

function updateGlobalControlsForSource() {
    if (window.__footerSource !== 'spotify') return;
    const data = window.__spotifyLastData;
    if (data) updateFooterForSpotify(data);
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

async function spotifyCommand(action) {
    if (_spotifyCommandInFlight) return;
    const interactiveTakeover = ['play', 'toggle', 'next', 'previous'].includes(action);
    if (interactiveTakeover) {
        armSpotifyTakeover();
    }
    const gen = _spotifyPollGeneration;
    _spotifyCommandInFlight = true;
    try {
        const resp = await fetch(`/api/spotify/${action}`, { method: 'POST' });
        const data = await resp.json().catch(() => null);
        if (gen !== _spotifyPollGeneration) return;
        if (data) {
            handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
        } else {
            const fresh = await fetchSpotifyStatus();
            if (gen !== _spotifyPollGeneration) return;
            handleIncomingSpotifyState(fresh, { renderTab: true, renderFooter: true });
        }
        if ((data || {}).status === 'Playing') {
            syncSpotifySourceOwnership(data);
            startSpotifyPoll();
        }
        if (interactiveTakeover) {
            forceSpotifyRefreshBurst();
        }
    } catch {
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
        const resp = await fetch('/api/spotify/seek', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ position: positionSec }),
        });
        const data = await resp.json().catch(() => null);
        if (data) {
            handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
        }
    } catch { /* ignore */ }
    _spotifySeekCommitTimer = setTimeout(async () => {
        const fresh = await fetchSpotifyStatus();
        handleIncomingSpotifyState(fresh, { renderTab: true, renderFooter: true });
    }, 700);
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
function setupSpotifyActions() {
    const el = spotifyElements;

    // Transport
    el.toggle?.addEventListener('click', () => spotifyCommand('toggle'));
    el.prev?.addEventListener('click', () => spotifyCommand('previous'));
    el.next?.addEventListener('click', () => spotifyCommand('next'));

    // Shuffle / Loop
    el.shuffle?.addEventListener('click', () => spotifyCommand('shuffle'));
    el.loop?.addEventListener('click', () => spotifyCommand('loop'));

    // Seek slider
    if (el.progress) {
        el.progress.addEventListener('mousedown', () => { _spotifySeeking = true; });
        el.progress.addEventListener('touchstart', () => { _spotifySeeking = true; }, { passive: true });

        const commitSeek = () => {
            _spotifySeeking = false;
            const data = window.__spotifyLastData;
            if (!data || !data.duration) return;
            const posSec = (parseFloat(el.progress.value) / 100) * data.duration;
            spotifySeek(posSec);
        };

        el.progress.addEventListener('mouseup', commitSeek);
        el.progress.addEventListener('touchend', commitSeek);
        el.progress.addEventListener('change', commitSeek);
    }
}

function stopSpotifyPoll() {
    if (_spotifyPollTimer) {
        clearInterval(_spotifyPollTimer);
        _spotifyPollTimer = null;
    }
}

// Guard against stale in-flight poll responses — bump generation when source changes
let _spotifyPollGeneration = 0;

function startSpotifyPoll() {
    if (!shouldPollSpotify()) return;
    if (_spotifyPollTimer) return;
    const gen = ++_spotifyPollGeneration;
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

// Footer update for Spotify source — single source of truth
function updateFooterForSpotify(data) {
    if (window.__footerSource !== 'spotify') return;
    if (footerContentFreezeActive()) return;
    if (typeof data.volume === 'number' && !volumeGestureActive && !spotifyVolumeRequestInFlight && pendingSpotifyVolume === null) {
        state.playback.volume = data.volume;
        renderVolumeControlsFromActualVolume(data.volume);
    }
    if (!data.available || (data.status === 'Stopped' && !data.title)) return;
    document.body.classList.remove('source-local', 'source-radio');
    document.body.classList.add('source-local');
    if (elements.btnPlayPause) {
        elements.btnPlayPause.disabled = false;
        elements.btnPlayPause.textContent = data.status === 'Playing' ? '⏸' : '▶';
    }
    if (elements.playbackEq) {
        elements.playbackEq.style.display = data.status === 'Playing' ? 'inline-flex' : 'none';
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
    if (scTitle) scTitle.textContent = data.title || '';
    if (scArtist) scArtist.textContent = data.artist || '';
    if (!window.__spotifySeeking && elements.seekSlider && elements.seekCurrent && elements.seekDuration) {
        const pos = Number(data.position || 0);
        const dur = Number(data.duration || 0);
        elements.seekCurrent.textContent = formatTime(pos);
        elements.seekDuration.textContent = formatTime(dur);
        if (dur > 0) elements.seekSlider.value = Math.round((pos / dur) * 1000);
        else elements.seekSlider.value = 0;
    }
    renderPeakWarningBadge();
}

// Spotify tab internal UI (cover, controls inside the tab)
function renderSpotifyTab(data) {
    renderSpotify(data);
}

async function initSpotify() {
    setupSpotifyActions();
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
