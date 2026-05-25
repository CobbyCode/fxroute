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
const MeasurementDsp = window.FXRouteMeasurementDsp || {};
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
        scanStatus: null,
        viewMode: 'tracks',
        currentFolder: '',
        selectedTrackIds: [],
        searchQuery: '',
        shuffle: false,
        loop: false,
        selectionDownloadPending: false,
        albums: [],
        albumsLoaded: false,
        showFavoriteAlbums: false,
        albumDetail: null,
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
        inputs: [],
        selectedInputId: '',
        selectedChannel: 'left',
        displaySmoothing: '1/6-oct',
        hostCaptureAvailable: false,
        modeNote: '',
        calibrationFilename: '',
        calibrationOptions: [],
        selectedCalibrationRef: '',
        calibrationUpdating: false,
        calibrationDeleting: false,
        houseCurveFilename: '',
        houseCurveOptions: [],
        houseCurveUpdating: false,
        houseCurveDeleting: false,
        visibilityById: {},
        reviewVisibilityById: {},
        savedGroupOpen: false,
        setupOpen: false,
        storage: null,
        captureAvailable: false,
        activeJobId: '',
        statusText: 'Sweep ready. Calibration file is optional.',
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
            sampleRate: '48000',
            quality: 'linear_8192',
            phaseMode: 'minimum',
            irLength: '8192',
            dragMode: null,
            draft: { left: null, right: null, presetName: '', nameTouched: false },
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
    },
    wsConnected: false,
};
// WebSocket
let ws = null;
let reconnectAttempts = 0;
let reconnectTimer = null;
let wsConnectSerial = 0;
let wsReconnectSyncGeneration = 0;
let playbackActionInFlight = false;
let pendingPlaybackRequestId = 0;
let nowPlayingCueTimer = null;
let nowPlayingCueCoverAbort = null;
let pendingFooterSingleTrackStart = null;
let pauseActionRequestId = 0;
const FOOTER_SINGLE_TRACK_START_LOCK_MS = 5000;
let volumeTimer = null;
let volumeRequestInFlight = false;
let pendingVolume = null;
let volumeGestureActive = false;
let effectsImportInFlight = false;
let peqCreateInFlight = false;
let convolverCreateInFlight = false;
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
let settingsOutputScanOnFocusDone = false;
let measurementInputScanOnFocusDone = false;
let measurementResizeScheduled = false;
let measurementGraphPointerId = null;
let measurementPeqTakeFeedbackTimer = null;
let measurementPeqLastTouchCreateAt = 0;
// Seek - globals
let seekDragging = false;
let seekPendingPos = null;
let playbackPositionPollTimer = null;
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
const LIBRARY_SCAN_POLL_INTERVAL_MS = 1200;
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
    settingsHardwareSummary: document.getElementById('settings-hardware-summary'),
    settingsHardwareDetail: document.getElementById('settings-hardware-detail'),
    settingsHardwareRcaBtn: document.getElementById('settings-hardware-rca'),
    settingsHardwareXlrBtn: document.getElementById('settings-hardware-xlr'),
    settingsHardwarePressBtn: document.getElementById('settings-hardware-press'),
    settingsHardwareAutoOnBtn: document.getElementById('settings-hardware-auto-on'),
    settingsHardwareAutoOffBtn: document.getElementById('settings-hardware-auto-off'),
    settingsCertificateLink: document.getElementById('settings-certificate-link'),
    tabs: document.querySelectorAll('.tab-btn'),
    tabPanels: document.querySelectorAll('.tab-panel'),
    stationsGrid: document.getElementById('stations-grid'),
    stationSearchInput: document.getElementById('station-search'),
    stationSearchClear: document.getElementById('station-search-clear'),
    stationExportAllBtn: document.getElementById('station-export-all'),
    stationsEmptySearch: document.getElementById('stations-empty-search'),
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
    libraryViewTracksBtn: document.getElementById('library-view-tracks'),
    libraryViewFoldersBtn: document.getElementById('library-view-folders'),
    libraryViewAlbumsBtn: document.getElementById('library-view-albums'),
    libraryFolderPath: document.getElementById('library-folder-path'),
    librarySearchInput: document.getElementById('library-search'),
    librarySearchClear: document.getElementById('library-search-clear'),
    albumsGrid: document.getElementById('albums-grid'),
    albumDetail: document.getElementById('album-detail'),
    albumDetailBack: document.getElementById('album-detail-back'),
    albumDetailCover: document.getElementById('album-detail-cover'),
    albumDetailName: document.getElementById('album-detail-name'),
    albumDetailArtist: document.getElementById('album-detail-artist'),
    albumDetailCount: document.getElementById('album-detail-count'),
    albumFavoriteToggle: document.getElementById('album-favorite-toggle'),
    albumDetailTracks: document.getElementById('album-detail-tracks'),
    albumDiscover: document.getElementById('album-discover'),
    playSelectedTracksBtn: document.getElementById('play-selected-tracks'),
    albumFavoritesToggleBtn: document.getElementById('album-favorites-toggle'),
    selectAllTracksBtn: document.getElementById('select-all-tracks'),
    playlistName: document.getElementById('playlist-name'),
    savePlaylistBtn: document.getElementById('save-playlist'),
    playlistSaveRow: document.getElementById('playlist-save-row'),
    playlistSaveControls: document.querySelector('.playlist-save-controls'),
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
    measurementModeNote: document.getElementById('measurement-mode-note'),
    measurementInputGroup: document.getElementById('measurement-input-group'),
    measurementInputSelect: document.getElementById('measurement-input-select'),
    measurementInputRefreshBtn: document.getElementById('measurement-input-refresh'),
    measurementChannelSelect: document.getElementById('measurement-channel-select'),
    measurementCalibrationSelect: document.getElementById('measurement-calibration-select'),
    measurementCalibrationFile: document.getElementById('measurement-calibration-file'),
    measurementCalibrationDeleteBtn: document.getElementById('measurement-calibration-delete'),
    measurementCalibrationUploadName: document.getElementById('measurement-calibration-upload-name'),
    measurementCalibrationName: document.getElementById('measurement-calibration-name'),
    measurementHouseCurveSelect: document.getElementById('measurement-house-curve-select'),
    measurementHouseCurveFile: document.getElementById('measurement-house-curve-file'),
    measurementHouseCurveDeleteBtn: document.getElementById('measurement-house-curve-delete'),
    measurementHouseCurveUploadName: document.getElementById('measurement-house-curve-upload-name'),
    measurementHouseCurveName: document.getElementById('measurement-house-curve-name'),
    measurementNameInput: document.getElementById('measurement-name'),
    measurementStartBtn: document.getElementById('measurement-start'),
    measurementSaveBtn: document.getElementById('measurement-save'),
    measurementClearBtn: document.getElementById('measurement-clear'),
    measurementAssistMode: document.getElementById('measurement-assist-mode'),
    measurementTargetCurve: document.getElementById('measurement-target-curve'),
    measurementSetupStatus: document.getElementById('measurement-setup-status'),
    measurementSummary: document.getElementById('measurement-summary'),
    measurementGraphControls: document.getElementById('measurement-graph-controls'),
    measurementGraph: document.getElementById('measurement-graph'),
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
    measurementConvolverQuality: document.getElementById('measurement-convolver-quality'),
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
    elements.effectsToggleImportBtn.textContent = shouldOpen ? 'Close import' : 'Import';
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
        void resyncPlaybackAfterReconnect();
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
async function resyncPlaybackAfterReconnect() {
    const generation = ++wsReconnectSyncGeneration;
    try {
        const [playback, spotify] = await Promise.all([
            fetch('/api/status')
                .then(resp => resp.ok ? resp.json() : null)
                .catch(() => null),
            fetchSpotifyStatus(),
        ]);
        if (generation !== wsReconnectSyncGeneration) return;

        if (playback) {
            mergePlaybackState(playback);
            syncFooterOwnershipFromPlayback(playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        if (spotify) {
            handleIncomingSpotifyState(spotify, { renderTab: true, renderFooter: true });
        }
        reconcileFooterSource();
        updatePlaybackUI();
        if (shouldPollSpotify()) {
            startSpotifyPoll();
        } else {
            stopSpotifyPoll();
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
    elements.settingsHardwareRcaBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/input/rca', 'RCA selected'));
    elements.settingsHardwareXlrBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/input/xlr', 'XLR selected'));
    elements.settingsHardwarePressBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/input/press', 'Input button pressed'));
    elements.settingsHardwareAutoOnBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/auto/on', 'Auto mode enabled'));
    elements.settingsHardwareAutoOffBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/auto/off', 'Auto mode disabled'));
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
        void Promise.all([fetchAudioSourceOverview(), fetchHardwareStatus()]);
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
        void Promise.all([fetchAudioOutputOverview(), fetchAudioSourceOverview(), fetchHardwareStatus()]);
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
    return host ? `http://${host}/api/certificate/local-root` : '/api/certificate/local-root';
}

function isSelectFocused(selectEl) {
    return !!selectEl && document.activeElement === selectEl;
}

function formatHardwareBool(value, onLabel = 'on', offLabel = 'off') {
    if (value === true) return onLabel;
    if (value === false) return offLabel;
    return 'unknown';
}

function renderHardwareController() {
    const hardware = state.settings?.hardware || {};
    const connected = !!hardware.connected;
    const status = hardware.status || {};
    const input = hardware.input || status.INPUT || 'unknown';
    if (elements.settingsHardwareSummary) {
        elements.settingsHardwareSummary.textContent = connected
            ? `Connected${hardware.device ? `: ${hardware.device}` : ''}`
            : 'Controller not detected.';
    }
    if (elements.settingsHardwareDetail) {
        if (connected) {
            const trigger = formatHardwareBool(hardware.trigger ?? status.TRIGGER, 'trigger active', 'trigger off');
            const power = formatHardwareBool(hardware.power ?? status.POWER, 'power on', 'power off');
            const auto = formatHardwareBool(hardware.auto ?? status.AUTO, 'auto on', 'auto off');
            elements.settingsHardwareDetail.textContent = `Input: ${input} · ${trigger} · ${power} · ${auto}`;
        } else {
            const note = Array.isArray(hardware.notes) && hardware.notes.length ? hardware.notes[0] : 'USB controller is optional.';
            elements.settingsHardwareDetail.textContent = note;
        }
    }
    [
        elements.settingsHardwareRcaBtn,
        elements.settingsHardwareXlrBtn,
        elements.settingsHardwarePressBtn,
        elements.settingsHardwareAutoOnBtn,
        elements.settingsHardwareAutoOffBtn,
    ].forEach((button) => {
        if (button) button.disabled = !connected || !!hardware.pending;
    });
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
    renderHardwareController();
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

function normalizeHardwareStatus(data = {}) {
    return {
        available: data.available !== false,
        connected: !!data.connected,
        device: data.device || null,
        status: data.status || {},
        raw: data.raw || null,
        input: data.input || data.status?.INPUT || null,
        power: data.power ?? data.status?.POWER ?? null,
        trigger: data.trigger ?? data.status?.TRIGGER ?? null,
        auto: data.auto ?? data.status?.AUTO ?? null,
        notes: Array.isArray(data.notes) ? data.notes : [],
        pending: false,
    };
}

async function fetchHardwareStatus() {
    try {
        const resp = await fetch('/api/hardware/status');
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to fetch hardware status');
        state.settings.hardware = normalizeHardwareStatus(data);
    } catch (e) {
        state.settings.hardware = {
            available: false,
            connected: false,
            device: null,
            status: {},
            raw: null,
            input: null,
            power: null,
            trigger: null,
            auto: null,
            notes: [e.message || 'Failed to fetch hardware status'],
            pending: false,
        };
    }
    renderSettingsPanel();
}

async function runHardwareCommand(endpoint, successMessage) {
    state.settings.hardware.pending = true;
    renderSettingsPanel();
    try {
        const resp = await fetch(endpoint, { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Hardware command failed');
        state.settings.hardware = normalizeHardwareStatus(data);
        if (state.settings.hardware.connected) {
            showToast(successMessage, 'success');
        } else {
            const note = state.settings.hardware.notes?.[0];
            showToast(note || 'Hardware controller not connected', 'warning');
        }
    } catch (e) {
        state.settings.hardware.pending = false;
        showToast(e.message || 'Hardware command failed', 'error');
        void fetchHardwareStatus();
    }
    renderSettingsPanel();
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
    if (elements.toggleImportBtn) elements.toggleImportBtn.textContent = 'Import';
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
    const incomingSeq = typeof data._seq === 'number' ? data._seq : null;
    const currentSeq = typeof state.playback?._seq === 'number' ? state.playback._seq : null;
    if (incomingSeq !== null && currentSeq !== null && incomingSeq < currentSeq) {
        footerDebug('ignore-stale-playback-state', { incomingSeq, currentSeq });
        return;
    }
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
            if (state.library.viewMode !== 'albums') renderLibraryView();
            renderLibraryModeButtons();
        }
        return;
    }

    state.library.selectedTrackIds = [...context.selectedTrackIds];
    state.library.shuffle = context.shuffle;
    state.library.loop = context.loop;
    if (state.library.viewMode !== 'albums') renderLibraryView();
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
        stopPlaybackPositionPoll();
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
    // Start/stop position polling for local playback
    if (playing && window.__footerSource !== 'spotify') {
        startPlaybackPositionPoll();
    } else {
        stopPlaybackPositionPoll();
    }
}
function startPlaybackPositionPoll() {
    if (window.__footerSource === 'spotify') return;
    if (playbackPositionPollTimer !== null) return;
    playbackPositionPollTimer = setInterval(async () => {
        try {
            if (window.__footerSource === 'spotify') {
                stopPlaybackPositionPoll();
                return;
            }
            const resp = await fetch('/api/status');
            if (!resp.ok) return;
            const data = await resp.json();
            if (window.__footerSource === 'spotify' || getBackendFooterOwner(data) === 'spotify') {
                if (getBackendFooterOwner(data) === 'spotify') {
                    setFooterSource('spotify', 'local-poll-backend-owner-spotify');
                }
                stopPlaybackPositionPoll();
                return;
            }
            mergePlaybackState(data);
            if (window.__footerSource === 'spotify') {
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
        renderLibraryView();
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
    if (elements.stationSearchInput) {
        elements.stationSearchInput.addEventListener('input', () => {
            if (elements.stationSearchClear) {
                elements.stationSearchClear.disabled = !elements.stationSearchInput.value;
            }
            renderStations();
        });
    }
    if (elements.stationSearchClear) {
        elements.stationSearchClear.addEventListener('click', () => {
            elements.stationSearchInput.value = '';
            elements.stationSearchClear.disabled = true;
            renderStations();
            elements.stationSearchInput.focus();
        });
    }
    if (elements.stationExportAllBtn) {
        elements.stationExportAllBtn.addEventListener('click', exportAllStations);
    }
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
function exportAllStations() {
    if (!state.stations.length) {
        showToast('No stations to export', 'warning');
        return;
    }
    const exportData = state.stations.map(st => ({
        name: st.title || st.name || '',
        url: st.stream_url || '',
        logo: st.image_url || st.custom_image_url || '',
        genre: st.artist || '',
    }));
    const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'fxroute-radio-stations.json';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
async function importStationFile(data) {
    const items = data.filter(item => item && (item.url || item.stream_url)).map(item => ({
        name: (item.name || item.title || '').trim(),
        url: (item.url || item.stream_url || '').trim(),
        logo: (item.logo || item.image_url || item.custom_image_url || '').trim(),
        genre: (item.genre || item.artist || '').trim(),
    }));
    if (!items.length) {
        showToast('No valid stations found in file', 'error');
        return;
    }
    showToast('Importing ' + items.length + ' station' + (items.length > 1 ? 's' : '') + '…', 'info');
    try {
        const resp = await fetch('/api/stations/import', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(items),
        });
        if (!resp.ok) throw new Error('Import failed');
        const result = await resp.json();
        const ok = (result.results || []).filter(r => r.status === 'ok').length;
        const err = (result.results || []).filter(r => r.status === 'error').length;
        const skipped = (result.results || []).filter(r => r.status === 'skipped').length;
        if (ok > 0) {
            showToast('Imported ' + ok + ' station' + (ok > 1 ? 's' : '') + (err > 0 ? ', ' + err + ' failed' : '') + (skipped > 0 ? ', ' + skipped + ' skipped' : ''), err > 0 ? 'warning' : 'success');
            await fetchStations();
        } else {
            showToast('No stations imported' + (err > 0 ? ', ' + err + ' failed' : ''), 'error');
        }
    } catch (e) {
        showToast('Import failed: ' + (e.message || 'unknown error'), 'error');
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
    const knownLocalArtSlugs = new Set(['groovesalad', 'suburbsofgoa', 'thetrip', 'poptron', 'dubstep', 'live', 'gsclassic', '7soul']);
    const inputUrl = (station?.input_url || station?.url || '').trim();
    const match = inputUrl.match(/somafm\.com\/([^/?#]+)/i);
    if (match && match[1]) {
        const slug = match[1].replace(/(256|130)?\.pls$/i, '').trim().toLowerCase();
        if (knownLocalArtSlugs.has(slug)) return slug;
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

function getStationSearchQuery() {
    return (elements.stationSearchInput?.value || '').trim().toLowerCase();
}

function stationMatchesSearch(station, query) {
    if (!query) return true;
    const fields = [
        station.title || '',
        station.stream_url || '',
        station.input_url || '',
        station.image_url || '',
        station.custom_image_url || '',
    ];
    return fields.some(f => f.toLowerCase().includes(query));
}

function renderStations() {
    const loadingEl = document.querySelector('#tab-radio .loading');
    if (state.stations.length === 0) {
        if (loadingEl) loadingEl.textContent = 'No stations yet. Open Manage to add one.';
        elements.stationsGrid.innerHTML = '';
        if (elements.stationsEmptySearch) elements.stationsEmptySearch.classList.add('hidden');
        renderStationDeleteOptions();
        return;
    }
    if (loadingEl) loadingEl.style.display = 'none';
    const query = getStationSearchQuery();
    const filtered = state.stations.filter(s => stationMatchesSearch(s, query));
    if (filtered.length === 0 && query) {
        elements.stationsGrid.innerHTML = '';
        if (elements.stationsEmptySearch) elements.stationsEmptySearch.classList.remove('hidden');
        return;
    }
    if (elements.stationsEmptySearch) elements.stationsEmptySearch.classList.add('hidden');
    elements.stationsGrid.innerHTML = filtered.map(station => {
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
        const file = e.dataTransfer?.files?.[0];
        if (file && file.type === 'application/json') {
            try {
                const text = await file.text();
                const data = JSON.parse(text);
                if (!Array.isArray(data)) {
                    showToast('Invalid format: expected a JSON array', 'error');
                    return;
                }
                await importStationFile(data);
            } catch (err) {
                showToast('Failed to read station file: ' + (err.message || 'unknown error'), 'error');
            }
            return;
        }
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
async function fetchLibraryStatus() {
    try {
        const resp = await fetch('/api/library/status');
        if (!resp.ok) throw new Error('Failed to fetch library status');
        const status = await resp.json();
        const wasScanning = !!state.library.scanning;
        state.library.scanStatus = status;
        state.library.scanning = !!status.scanning;
        renderTracks();
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
        renderTracks();
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
        renderTracks();
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
        } else {
            renderAlbums();
        }
    } else {
        renderTracks();
    }
}

function renderLibraryViewButtons() {
    const mode = state.library.viewMode;
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
    if (elements.libraryViewAlbumsBtn) {
        const active = mode === 'albums';
        elements.libraryViewAlbumsBtn.classList.toggle('active', active);
        elements.libraryViewAlbumsBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    updateAlbumFavoritesFilterButton();
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
    elements.libraryFolderPath.innerHTML = html;
    elements.libraryFolderPath.classList.remove('hidden');
    elements.libraryFolderPath.querySelectorAll('button[data-folder]').forEach(btn => {
        btn.addEventListener('click', () => setLibraryFolder(btn.dataset.folder || ''));
    });
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
    renderLibraryFolderPath();
    // Hide album views when in tracks/folders mode
    if (elements.albumsGrid) elements.albumsGrid.classList.add('hidden');
    if (elements.albumDetail) elements.albumDetail.classList.add('hidden');
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
    const loadingEl = document.querySelector('#tab-library .loading');
    const scanText = formatLibraryScanStatus();
    const hasSearch = !!(state.library.searchQuery || '').trim();

    if (allTracks.length === 0 && (!hasSearch || filteredPlaylists.length === 0)) {
        if (loadingEl) {
            loadingEl.textContent = scanText || 'No tracks yet. Import a file or URL to get started.';
            loadingEl.style.display = '';
        }
        elements.tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }
    if (loadingEl) {
        loadingEl.textContent = scanText || '';
        loadingEl.style.display = scanText ? '' : 'none';
        loadingEl.classList.toggle('scan-status', !!scanText);
    }

    const folderMode = state.library.viewMode === 'folders';
    const childFolders = folderMode ? getFolderChildren() : [];
    if (filteredTracks.length === 0 && childFolders.length === 0 && filteredPlaylists.length === 0) {
        if (loadingEl) {
            loadingEl.textContent = hasSearch ? 'No matching tracks or playlists. Try a broader search.' : 'No tracks in this folder.';
            loadingEl.style.display = '';
        }
        elements.tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }

    let html = '';

    if (!folderMode && filteredPlaylists.length > 0) {
        html += filteredPlaylists.map(playlist => {
            const classes = ['track-item', 'playlist-item'];
            return `<div class="${classes.join(' ')}" data-playlist-id="${escapeHtml(playlist.id)}">
                <button class="track-play-button" data-playlist-id="${escapeHtml(playlist.id)}" type="button">
                    <span class="track-item-icon">📋</span>
                    <div class="track-title">${escapeHtml(playlist.name)}</div>
                    <div class="track-artist">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
                </button>
                <button class="playlist-download-btn" data-playlist-download="${escapeHtml(playlist.id)}" type="button" title="Export playlist as M3U8">⬇</button>
                <button class="playlist-delete-btn" data-playlist-delete="${escapeHtml(playlist.id)}" type="button" title="Delete playlist">🗑</button>
            </div>`;
        }).join('');
    }

    if (folderMode) {
        html += childFolders.map(folder => `<div class="track-item folder-item" data-folder="${escapeHtml(folder.path)}">
            <button class="track-play-button" data-folder="${escapeHtml(folder.path)}" type="button" title="Open folder">
                <span class="track-item-icon">📁</span>
                <div class="track-title">${escapeHtml(folder.name)}</div>
                <div class="folder-count">${folder.count} track${folder.count === 1 ? '' : 's'}</div>
            </button>
            <div class="folder-actions" aria-label="Folder actions">
                <button class="folder-action-btn" data-folder-play="${escapeHtml(folder.path)}" type="button" title="Play folder" aria-label="Play ${escapeHtml(folder.name)}">▶</button>
                <button class="folder-action-btn folder-action-btn--delete" data-folder-delete="${escapeHtml(folder.path)}" type="button" title="Delete folder" aria-label="Delete ${escapeHtml(folder.name)}">🗑</button>
            </div>
        </div>`).join('');
    }

    html += filteredTracks.map(track => {
        const isSelected = selectedIds.has(track.id);
        const artist = (track.artist || '').trim();
        const album = (track.album || '').trim();
        const rel = getTrackRelativePath(track);
        const metadataLine = [artist, album].filter(Boolean).join(' · ');
        const subline = metadataLine || (folderMode ? getTrackFilename(track) : getTrackFolder(track));
        return `
            <div class="track-item ${isSelected ? 'selected' : ''}" data-track-id="${escapeHtml(track.id)}">
                <label class="track-select">
                    <input type="checkbox" class="track-checkbox" data-track-id="${escapeHtml(track.id)}" ${isSelected ? 'checked' : ''}>
                    <span class="track-select-box"></span>
                </label>
                <button class="track-play-button" data-track-id="${escapeHtml(track.id)}" type="button" title="${escapeHtml(rel)}">
                    <span class="track-item-icon">♫</span>
                    <div class="track-title">${escapeHtml(track.title)}</div>
                    ${subline ? `<div class="track-artist">${escapeHtml(subline)}</div>` : ''}
                </button>
            </div>
        `;
    }).join('');

    elements.tracksList.innerHTML = html;

    elements.tracksList.querySelectorAll('.track-play-button[data-folder]').forEach(item => {
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
    state.library.viewMode = mode === 'folders' ? 'folders' : mode === 'albums' ? 'albums' : 'tracks';
    if (state.library.viewMode === 'tracks') state.library.currentFolder = '';
    if (state.library.viewMode === 'albums') {
        state.library.albumDetail = null;
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

async function fetchAlbums() {
    if (state.library.albumsLoaded && state.library.albums.length > 0) return;
    try {
        const res = await fetch('/api/albums');
        if (!res.ok) return;
        state.library.albums = await res.json();
        state.library.albumsLoaded = true;
        state.library.albumsCacheToken = Date.now();
        if (state.library.viewMode === 'albums') renderAlbums();
    } catch (e) {
        console.warn('Failed to fetch albums', e);
    }
}

function renderAlbums() {
    renderLibraryViewButtons();
    updateLibrarySelectionUI();
    const loadingEl = document.querySelector('#tab-library .loading');
    const query = (state.library.searchQuery || '').trim().toLowerCase();

    // Hide tracks list, show albums grid
    elements.tracksList.classList.add('hidden');
    elements.albumDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    if (elements.libraryFolderPath) elements.libraryFolderPath.classList.add('hidden');

    if (!state.library.albumsLoaded) {
        // Albums not yet loaded — show loading state and trigger fetch
        if (loadingEl) {
            loadingEl.textContent = 'Loading albums…';
            loadingEl.style.display = '';
        }
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

    if (albums.length === 0 && !showSmartFavorites) {
        if (loadingEl) {
            loadingEl.textContent = state.library.showFavoriteAlbums
                ? 'No favorite albums.'
                : query ? 'No matching albums.' : 'No albums found. Import music with album tags.';
            loadingEl.style.display = '';
        }
        elements.albumsGrid.innerHTML = '';
        elements.albumsGrid.classList.remove('hidden');
        return;
    }
    if (loadingEl) loadingEl.style.display = 'none';

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
    const manualHtml = albums.length > 0 ? albums.map(album => {
        const coverUrl = `/api/albums/${album.id}/cover?v=${state.library.albumsCacheToken || ''}`;
        const fallbackSvg = albumArtFallbackSvg(album.name || album.artist || 'Album');
        return `
        <div class="album-card" data-album-id="${escapeHtml(album.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">
                <img class="album-art" src="${escapeHtml(coverUrl)}"
                     alt="${escapeHtml(album.name)}"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${fallbackSvg}'" />
            </div>
            <div class="album-name">${escapeHtml(album.name)}</div>
            <div class="album-artist">${escapeHtml(album.artist)}</div>
        </div>`;
    }).join('') : '';
    elements.albumsGrid.innerHTML = smartHtml + manualHtml;
    elements.albumsGrid.classList.remove('hidden');

    elements.albumsGrid.querySelectorAll('.album-card[data-smart-favorite="top40"]').forEach(card => {
        card.addEventListener('click', () => openSmartTopTracks());
    });
    elements.albumsGrid.querySelectorAll('.album-card').forEach(card => {
        if (card.dataset.smartFavorite) return;
        card.addEventListener('click', () => openAlbumDetail(card.dataset.albumId));
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

async function openAlbumDetail(albumId) {
    const album = (state.library.albums || []).find(a => a.id === albumId);
    if (!album) return;

    try {
        const res = await fetch(`/api/albums/${albumId}/tracks`);
        if (!res.ok) return;
        const tracks = await res.json();
        state.library.albumDetail = { album, tracks };

        // Update detail header
        const coverUrl = `/api/albums/${albumId}/cover?v=${state.library.albumsCacheToken || ''}`;
        const fallbackSvg = albumArtFallbackSvg(album.name || album.artist || 'Album');
        elements.albumDetailCover.src = coverUrl;
        elements.albumDetailCover.onerror = function() { this.onerror = null; this.src = fallbackSvg; };
        elements.albumDetailName.textContent = album.name;
        elements.albumDetailArtist.textContent = album.artist;
        elements.albumDetailCount.textContent = `${tracks.length} track${tracks.length === 1 ? '' : 's'}`;
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
        elements.albumDetail.classList.remove('hidden');
        updatePlaylistSaveRowVisibility();
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
        const knownIds = new Set(state.library.tracks.map(t => t.id));
        for (const track of state.library.albumDetail.tracks) {
            if (track?.id && !knownIds.has(track.id)) {
                state.library.tracks.push(track);
                knownIds.add(track.id);
            }
        }
        elements.albumDetailCover.src = `${album.coverUrl}?v=${state.library.albumsCacheToken || ''}`;
        elements.albumDetailCover.onerror = function() { this.onerror = null; this.src = albumArtFallbackSvg(album.name); };
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
        elements.albumDetail.classList.remove('hidden');
        updatePlaylistSaveRowVisibility();
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
    elements.albumDetailCount.textContent = query
        ? `${tracks.length} of ${total} track${total === 1 ? '' : 's'}`
        : `${total} track${total === 1 ? '' : 's'}`;
    if (tracks.length === 0) {
        elements.albumDetailTracks.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
    elements.albumDetailTracks.innerHTML = tracks.map(track => {
        const title = escapeHtml(track.title || 'Unknown');
        const duration = track.duration ? formatTime(track.duration) : '';
        const meta = [track.artist, track.album].filter(Boolean).join(' · ');
        return `
        <div class="track-item" data-track-id="${escapeHtml(track.id)}">
            <button class="track-play-button" data-track-id="${escapeHtml(track.id)}" data-album-context="${escapeHtml(albumId)}" type="button">
                <span class="track-item-icon">▶</span>
                <div class="track-title">${title}</div>
                ${meta ? `<div class="track-artist">${escapeHtml(meta)}</div>` : ''}
            </button>
            ${duration ? `<span class="track-duration">${duration}</span>` : ''}
        </div>`;
    }).join('');
    elements.albumDetailTracks.querySelectorAll('.track-play-button').forEach(btn => {
        btn.addEventListener('click', () => {
            const trackId = btn.dataset.trackId;
            const albumContext = btn.dataset.albumContext;
            playTrackInAlbum(trackId, albumContext);
        });
    });
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
    elements.albumFavoriteToggle.textContent = favorite ? '★' : '☆';
    elements.albumFavoriteToggle.classList.toggle('active', favorite);
    elements.albumFavoriteToggle.setAttribute('aria-pressed', favorite ? 'true' : 'false');
    elements.albumFavoriteToggle.setAttribute('aria-label', favorite ? 'Remove album from favorites' : 'Add album to favorites');
    elements.albumFavoriteToggle.title = favorite ? 'Remove from favorites' : 'Add to favorites';
}

function updateAlbumFavoritesFilterButton() {
    if (!elements.albumFavoritesToggleBtn) return;
    const isAlbumsMode = state.library.viewMode === 'albums';
    const active = !!state.library.showFavoriteAlbums;
    elements.albumFavoritesToggleBtn.classList.toggle('hidden', !isAlbumsMode);
    elements.albumFavoritesToggleBtn.classList.toggle('active', active);
    elements.albumFavoritesToggleBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    elements.albumFavoritesToggleBtn.textContent = active ? 'All albums' : 'Favorites';
}

function toggleAlbumFavoritesFilter() {
    state.library.showFavoriteAlbums = !state.library.showFavoriteAlbums;
    state.library.albumDetail = null;
    updateAlbumFavoritesFilterButton();
    renderAlbums();
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
        updateAlbumFavoritesFilterButton();
        showToast(album.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
    } catch (e) {
        showToast(e.message || 'Failed to update favorite', 'error');
    } finally {
        elements.albumFavoriteToggle.disabled = false;
    }
}

function albumFactsHtml(album) {
    const headline = [album.release_type, album.year, album.country].filter(Boolean).join(' · ');
    const label = album.label ? `Label: ${album.label}` : '';
    const genres = (album.genres || []).slice(0, 3).filter(Boolean);
    const genreLine = genres.length ? `Genre: ${genres.join(' / ')}` : '';
    const lines = [headline, label, genreLine].filter(Boolean);
    if (!lines.length) return '';
    return `<div class="album-detail-facts">${lines.map(line => `<div>${escapeHtml(line)}</div>`).join('')}</div>`;
}

function albumAboutHtml(album) {
    const albumDescription = (album.album_description || '').trim();
    const artistDescription = (album.artist_description || '').trim();
    const description = albumDescription || artistDescription;
    if (!description) return '';
    const label = albumDescription ? 'About this album' : 'About this artist';
    return `
        <details class="album-detail-about">
            <summary>${escapeHtml(label)}</summary>
            <p>${escapeHtml(description)}</p>
        </details>
    `;
}

function closeAlbumDetail() {
    state.library.albumDetail = null;
    elements.albumDetail.classList.add('hidden');
    elements.albumsGrid.classList.remove('hidden');
    if (elements.albumDiscover) {
        elements.albumDiscover.classList.add('hidden');
        elements.albumDiscover.innerHTML = '';
    }
    updatePlaylistSaveRowVisibility();
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
    // Replace selection with album tracks – old selection is cleared
    state.library.selectedTrackIds = albumTrackIds;
    updateLibrarySelectionUI();
    await playLocal(trackId);
}

function albumArtFallbackSvg(text) {
    const initials = (text || 'ALBUM').split(/\s+/).map(w => w[0]).join('').substring(0, 2).toUpperCase();
    const colors = ['#6366f1', '#8b5cf6', '#a78bfa', '#c084fc', '#7c3aed', '#4f46e5'];
    const color = colors[(text || '').length % colors.length];
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
        <rect width="200" height="200" rx="16" fill="${color}"/>
        <text x="100" y="108" text-anchor="middle" fill="white" font-size="64" font-weight="bold" font-family="system-ui,sans-serif">${escapeHtml(initials)}</text>
    </svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

async function playLibraryFolder(folder) {
    const tracks = getTracksInFolder(folder);
    if (tracks.length === 0) {
        showToast('Folder has no playable tracks', 'error');
        return;
    }
    state.library.selectedTrackIds = tracks.map(track => track.id);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    scheduleActiveLocalQueueSync();
    await playLocal(tracks[0].id);
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
    scheduleActiveLocalQueueSync();
    showToast(allSelected ? 'Folder selection cleared' : `Selected ${folderTrackIds.length} folder tracks`, 'info');
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
    updateLibrarySearchControls();
    if (state.library.viewMode === 'albums' && state.library.albumDetail) {
        renderAlbumDetailTracks();
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
    const isAlbumsMode = state.library.viewMode === 'albums';
    const hasPlaylistSelection = count >= 2 && !isAlbumsMode;
    const hasAlbumDetail = state.library.viewMode === 'albums' && !!state.library.albumDetail;
    elements.playlistSaveRow.classList.toggle('hidden', !hasPlaylistSelection && !hasAlbumDetail);
    if (elements.playlistSaveControls) {
        elements.playlistSaveControls.classList.toggle('hidden', !hasPlaylistSelection);
    }
    if (elements.albumDetailBack) {
        elements.albumDetailBack.classList.toggle('hidden', !hasAlbumDetail);
    }
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
    updateAlbumFavoritesFilterButton();

    // Download: visible in all modes when tracks selected
    if (elements.downloadSelectedTracksBtn) {
        elements.downloadSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
        elements.downloadSelectedTracksBtn.disabled = totalSelectedCount === 0 || state.library.selectionDownloadPending;
    }

    // Delete: visible in all modes when tracks selected
    if (elements.deleteSelectedTracksBtn) {
        elements.deleteSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
    }

    // Play: visible when tracks selected
    if (elements.playSelectedTracksBtn) {
        elements.playSelectedTracksBtn.disabled = totalSelectedCount === 0;
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
    updateLibrarySearchControls();
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
        showNowPlayingCue(track, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing');
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

function clearEffectsPeqStatusOnCollapse() {
    if (!elements.effectsPeqDisclosure || elements.effectsPeqDisclosure.open) return;
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
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
        elements.effectsPeqDisclosure.addEventListener('toggle', () => {
            updateEffectsPeqDisclosureLabel();
            clearEffectsPeqStatusOnCollapse();
        });
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
    const traces = Array.isArray(measurement.traces) ? measurement.traces : [];
    const reviewTraces = Array.isArray(measurement.review_traces) ? measurement.review_traces : [];
    if (reviewTraces.length && measurementReviewVisible(measurement.id)) return reviewTraces;
    return traces;
}

function measurementSmoothingHalfWindowOctaves(mode = '1/6-oct') {
    return MeasurementDsp.measurementSmoothingHalfWindowOctaves(mode);
}

function smoothMeasurementTracePoints(points = [], mode = '1/6-oct') {
    return MeasurementDsp.smoothMeasurementTracePoints(points, mode);
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
const MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS = 6.0;
const measurementConvolverCurves = {
    neutral: { label: 'Neutral', shortLabel: 'Neutral', points: [[20, 0], [20000, 0]] },
    bass_shelf: { label: 'Bass Shelf', shortLabel: 'Bass', points: [[20, 4], [30, 4], [50, 3], [80, 2], [120, 1], [200, 0], [1000, 0], [20000, 0]] },
    harman: { label: 'Harman-style', shortLabel: 'Harman', points: [[20, 5], [30, 4.5], [50, 4], [80, 3], [120, 2], [200, 1], [500, 0.5], [1000, 0], [2000, -1], [5000, -2.5], [10000, -4], [20000, -5]] },
    bk: { label: 'Bruel & Kjaer-style', shortLabel: 'BK', points: [[20, 2], [50, 2], [100, 1.5], [200, 1], [500, 0.5], [1000, 0], [2000, -0.5], [5000, -1.5], [10000, -2.5], [20000, -3.5]] },
};
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
        draft: { leftBands: [], rightBands: [], presetName: '', nameTouched: false },
    };
}

function ensureMeasurementPeqState() {
    if (!state.measurement) state.measurement = {};
    if (!state.measurement.peqAssistant || typeof state.measurement.peqAssistant !== 'object') {
        state.measurement.peqAssistant = getDefaultMeasurementPeqState();
    }
    const peq = state.measurement.peqAssistant;
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

function clampMeasurementConvolverFrequency(value, fallback = 20) {
    return MeasurementDsp.clampMeasurementConvolverFrequency(value, fallback);
}

function getDefaultMeasurementConvolverState() {
    return {
        targetCurve: 'neutral',
        rangeStartHz: 20,
        rangeEndHz: 250,
        maxBoostDb: 6,
        maxCutDb: -9,
        dipGuard: 'off',
        safetyMarginDb: 1,
        autoGainEnabled: true,
        sampleRate: '48000',
        quality: 'linear_8192',
        phaseMode: 'minimum',
        irLength: '8192',
        dragMode: null,
        draft: { left: null, right: null, presetName: '', nameTouched: false },
    };
}

function ensureMeasurementConvolverState() {
    if (!state.measurement) state.measurement = {};
    const defaults = getDefaultMeasurementConvolverState();
    if (!state.measurement.convolverAssistant || typeof state.measurement.convolverAssistant !== 'object') {
        state.measurement.convolverAssistant = { ...defaults };
    }
    const conv = state.measurement.convolverAssistant;
    Object.entries(defaults).forEach(([key, value]) => {
        if (conv[key] === undefined || conv[key] === null || conv[key] === '') conv[key] = value;
    });
    conv.targetCurve = getMeasurementConvolverCurveOptions().some((curve) => curve.key === conv.targetCurve) ? conv.targetCurve : defaults.targetCurve;
    conv.rangeStartHz = Math.round(clampMeasurementConvolverFrequency(conv.rangeStartHz, defaults.rangeStartHz));
    conv.rangeEndHz = Math.round(clampMeasurementConvolverFrequency(conv.rangeEndHz, defaults.rangeEndHz));
    if (conv.rangeEndHz <= conv.rangeStartHz) conv.rangeEndHz = Math.min(20000, conv.rangeStartHz + 1);
    conv.maxBoostDb = [0, 3, 6, 9].includes(Number(conv.maxBoostDb)) ? Number(conv.maxBoostDb) : defaults.maxBoostDb;
    conv.maxCutDb = [-3, -6, -9, -12, -18, -24].includes(Number(conv.maxCutDb)) ? Number(conv.maxCutDb) : defaults.maxCutDb;
    conv.dipGuard = ['off', 'gentle', 'adaptive'].includes(String(conv.dipGuard)) ? String(conv.dipGuard) : defaults.dipGuard;
    conv.safetyMarginDb = Math.max(0, Number(conv.safetyMarginDb) || defaults.safetyMarginDb);
    conv.sampleRate = ['44100', '48000', '88200', '96000', '176400', '192000'].includes(String(conv.sampleRate)) ? String(conv.sampleRate) : defaults.sampleRate;
    const qualityAliases = { auto: 'linear_4096', normal: 'linear_4096', high: 'linear_8192' };
    const incomingQuality = qualityAliases[String(conv.quality)] || String(conv.quality || defaults.quality);
    const hasValidPhaseMode = ['linear', 'minimum', 'minimum_aligned'].includes(String(conv.phaseMode));
    const hasValidIrLength = measurementConvolverTapOptions.includes(Number(conv.irLength));
    if ((!hasValidPhaseMode || !hasValidIrLength) && getMeasurementConvolverTypeKeys().includes(incomingQuality)) {
        conv.phaseMode = getMeasurementConvolverPhaseModeForType(incomingQuality);
        conv.irLength = String(getMeasurementConvolverFirLengthForType(incomingQuality));
    }
    conv.phaseMode = ['linear', 'minimum', 'minimum_aligned'].includes(String(conv.phaseMode)) ? String(conv.phaseMode) : defaults.phaseMode;
    conv.irLength = measurementConvolverTapOptions.includes(Number(conv.irLength)) ? String(conv.irLength) : defaults.irLength;
    conv.quality = `${conv.phaseMode}_${conv.irLength}`;
    if (!conv.draft || typeof conv.draft !== 'object') conv.draft = { left: null, right: null, presetName: '', nameTouched: false };
    if (!conv.draft.left || typeof conv.draft.left !== 'object') conv.draft.left = null;
    if (!conv.draft.right || typeof conv.draft.right !== 'object') conv.draft.right = null;
    if (typeof conv.draft.presetName !== 'string') conv.draft.presetName = '';
    conv.draft.nameTouched = !!conv.draft.nameTouched;
    return conv;
}

function setMeasurementAssistMode(mode) {
    state.measurement.assistMode = mode === 'convolver' ? 'convolver' : 'peq';
    ensureMeasurementConvolverState();
    renderMeasurementPanel();
    scheduleMeasurementGraphRender();
}

function getMeasurementConvolverCurveOptions() {
    const customCurves = (state.measurement?.houseCurveOptions || [])
        .filter((curve) => Array.isArray(curve.points) && curve.points.length >= 2)
        .map((curve) => ({ key: `house:${curve.id}`, label: curve.filename || 'House curve', shortLabel: curve.filename || 'House', points: curve.points }));
    return [
        ...Object.entries(measurementConvolverCurves).map(([key, curve]) => ({ key, ...curve })),
        ...customCurves,
    ];
}

function getMeasurementConvolverCurve(curveKey) {
    return getMeasurementConvolverCurveOptions().find((curve) => curve.key === curveKey) || measurementConvolverCurves.neutral;
}

function getMeasurementConvolverCurveDb(curveKey, frequencyHz) {
    const curve = getMeasurementConvolverCurve(curveKey);
    const points = curve.points || measurementConvolverCurves.neutral.points;
    return MeasurementDsp.getMeasurementConvolverCurveDbFromPoints(points, frequencyHz);
}

function updateMeasurementConvolverField(field, value) {
    const conv = ensureMeasurementConvolverState();
    if (field === 'targetCurve') conv.targetCurve = getMeasurementConvolverCurveOptions().some((curve) => curve.key === value) ? value : conv.targetCurve;
    if (field === 'rangeStartHz') conv.rangeStartHz = Math.min(Math.round(clampMeasurementConvolverFrequency(value, conv.rangeStartHz)), conv.rangeEndHz - 1);
    if (field === 'rangeEndHz') conv.rangeEndHz = Math.max(Math.round(clampMeasurementConvolverFrequency(value, conv.rangeEndHz)), conv.rangeStartHz + 1);
    if (field === 'maxBoostDb') conv.maxBoostDb = [0, 3, 6, 9].includes(Number(value)) ? Number(value) : conv.maxBoostDb;
    if (field === 'maxCutDb') conv.maxCutDb = [-3, -6, -9, -12, -18, -24].includes(Number(value)) ? Number(value) : conv.maxCutDb;
    if (field === 'dipGuard') conv.dipGuard = ['off', 'gentle', 'adaptive'].includes(String(value)) ? String(value) : conv.dipGuard;
    if (field === 'sampleRate') conv.sampleRate = String(value || '48000');
    if (field === 'phaseMode') conv.phaseMode = ['linear', 'minimum', 'minimum_aligned'].includes(String(value)) ? String(value) : conv.phaseMode;
    if (field === 'irLength') conv.irLength = measurementConvolverTapOptions.includes(Number(value)) ? String(value) : conv.irLength;
    if (field === 'quality') {
        const quality = String(value || 'linear_4096');
        if (getMeasurementConvolverTypeKeys().includes(quality)) {
            conv.phaseMode = getMeasurementConvolverPhaseModeForType(quality);
            conv.irLength = String(getMeasurementConvolverFirLengthForType(quality));
        }
    }
    ensureMeasurementConvolverState();
    renderMeasurementPanel();
    scheduleMeasurementGraphRender();
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
    return MeasurementDsp.clampMeasurementPeqFrequency(value);
}

function clampMeasurementPeqGain(value) {
    return MeasurementDsp.clampMeasurementPeqGain(value);
}

function clampMeasurementPeqQ(value) {
    return MeasurementDsp.clampMeasurementPeqQ(value);
}

function measurementXToFrequency(x, bounds) {
    return MeasurementDsp.measurementXToFrequency(x, bounds);
}

function measurementYToDb(y, bounds, range) {
    return MeasurementDsp.measurementYToDb(y, bounds, range);
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
    const conv = ensureMeasurementConvolverState();
    state.measurement.currentMeasurement = null;
    state.measurement.currentMeasurementSaved = false;
    state.measurement.currentMeasurementName = '';
    peq.enabled = false;
    peq.filters = [];
    peq.activeFilterId = null;
    peq.dragFilterId = null;
    Object.assign(conv, getDefaultMeasurementConvolverState());
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

function getMeasurementPeqNameSuffix(date = new Date()) {
    const pad = (value) => String(value).padStart(2, '0');
    return `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
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
        showToast('Add at least one measurement PEQ filter first', 'warning');
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
    state.easyeffects = state.easyeffects || {};
    state.easyeffects.assistStack = state.easyeffects.assistStack || [];
    state.easyeffects.assistStack.push({ type: 'peq', mode, createdAt: new Date().toISOString(), bands: mappedBands.map((band) => ({ ...band })) });
    renderMeasurementPanel();
    const successMessage = mode === 'left'
        ? 'Measurement PEQ staged Left bands'
        : (mode === 'right' ? 'Measurement PEQ staged Right bands' : 'Measurement PEQ staged Left and Right bands');
    showMeasurementPeqTakeFeedback(mode === 'left' ? 'Left staged' : (mode === 'right' ? 'Right staged' : 'Left + Right staged'));
    showToast(successMessage, 'success');
}

async function createMeasurementPeqPresetFromDraft() {
    if (peqCreateInFlight) {
        showToast('PEQ preset creation already in progress', 'warning');
        return;
    }
    const peq = ensureMeasurementPeqState();
    const leftBands = (peq.draft?.leftBands || []).map((band) => ({ ...band }));
    const rightBands = (peq.draft?.rightBands || []).map((band) => ({ ...band }));
    if (!leftBands.length && !rightBands.length) {
        showToast('Take L, R or Both into the PEQ draft first', 'warning');
        return;
    }
    const validationError = validatePeqBands('Left', leftBands) || validatePeqBands('Right', rightBands);
    if (validationError) {
        showMeasurementPeqTakeFeedback(validationError);
        showToast(validationError, 'error');
        return;
    }
    const presetName = String(peq.draft?.presetName || '').trim() || getMeasurementPeqPresetName(getMeasurementPeqDraftMode(peq) || 'both', { unique: true });
    peq.draft.presetName = presetName;
    const eqMode = normalizePeqEqMode(state.easyeffects?.peqDraft?.eqMode || elements.effectsPeqModeSelect?.value || 'IIR');
    peqCreateInFlight = true;
    if (elements.measurementPeqCreateBtn) elements.measurementPeqCreateBtn.disabled = true;
    showMeasurementPeqTakeFeedback(`Creating ${presetName}…`);
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
        peq.draft.leftBands = [];
        peq.draft.rightBands = [];
        peq.draft.presetName = '';
        peq.draft.nameTouched = false;
        showMeasurementPeqTakeFeedback(`${presetName} created`);
        showToast(`Created PEQ preset: ${data.preset?.name || presetName}`, 'success');
    } catch (e) {
        showMeasurementPeqTakeFeedback('PEQ preset creation failed');
        showToast(e.message || 'PEQ preset creation failed', 'error');
    } finally {
        peqCreateInFlight = false;
        renderMeasurementPanel();
    }
}

function getMeasurementConvolverSelectedSourceEntries() {
    return [getCurrentMeasurementEntry(), ...getVisibleMeasurementEntries()].filter(Boolean);
}

function getMeasurementConvolverSourceEntries() {
    return getMeasurementConvolverSelectedSourceEntries();
}

function getMeasurementConvolverMeasurementForSide(side = 'left') {
    const desired = side === 'right' ? 'right' : 'left';
    const candidates = getMeasurementConvolverSourceEntries();
    const hasTrace = (measurement) => getMeasurementDisplayTraces(measurement || {}).length > 0;
    return candidates.find((measurement) => String(measurement.channel || 'left').toLowerCase() === desired && hasTrace(measurement))
        || candidates.find((measurement) => String(measurement.channel || '').toLowerCase() === 'stereo' && hasTrace(measurement))
        || null;
}

function getMeasurementConvolverTracePoints(side = 'left') {
    const measurement = getMeasurementConvolverMeasurementForSide(side);
    const trace = getMeasurementDisplayTraces(measurement || {}).find((item) => (item.points || []).length) || null;
    return trace ? smoothMeasurementTracePoints(trace.points || [], '1/6-oct') : [];
}

function getMeasurementConvolverAdaptiveDipGuardStrength(frequencyHz) {
    return MeasurementDsp.getMeasurementConvolverAdaptiveDipGuardStrength(frequencyHz);
}

function applyMeasurementConvolverDipGuard(requestedCorrections, index, mode = 'off') {
    return MeasurementDsp.applyMeasurementConvolverDipGuard(requestedCorrections, index, mode);
}

function analyzeMeasurementConvolverSide(side = 'left') {
    const conv = ensureMeasurementConvolverState();
    const points = getMeasurementConvolverTracePoints(side).filter(([frequency]) => frequency >= conv.rangeStartHz && frequency <= conv.rangeEndHz);
    if (!points.length) return null;
    const curve = getMeasurementConvolverCurve(conv.targetCurve);
    return {
        side,
        points: points.length,
        ...MeasurementDsp.analyzeMeasurementConvolverCorrections(points, curve.points || measurementConvolverCurves.neutral.points, conv),
    };
}

function getMeasurementConvolverSelectedSourceCount() {
    return getMeasurementConvolverSelectedSourceEntries().filter((measurement) => getMeasurementDisplayTraces(measurement || {}).length > 0).length;
}

function getMeasurementConvolverMultiSourceWarning() {
    return 'Multiple measurement curves are selected. Convolver uses measurement data as source; hide unrelated saved runs.';
}

function buildMeasurementConvolverWarnings(analyses = []) {
    const conv = ensureMeasurementConvolverState();
    const warnings = [];
    if (analyses.some((analysis) => analysis && analysis.lowBassBoost)) warnings.push('Deep bass boost can demand much more amplifier power and speaker excursion.');
    if (conv.phaseMode === 'minimum_aligned') {
        const leftTiming = conv.draft?.left?.timing || getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('left'));
        const rightTiming = conv.draft?.right?.timing || getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('right'));
        const safetyMessage = getMeasurementConvolverTimingSafetyMessage(getMeasurementConvolverTimingDelta(leftTiming, rightTiming));
        if (safetyMessage) warnings.push('Filter not created because timing offset exceeds safety limit.');
    }
    return warnings;
}

function formatMeasurementConvolverGain(value) {
    const numeric = Number(value) || 0;
    return `${numeric > 0 ? '+' : ''}${Number.isInteger(numeric) ? numeric.toFixed(0) : numeric.toFixed(1)}dB`;
}

function getMeasurementConvolverNameSuffix(date = new Date()) {
    const pad = (value) => String(value).padStart(2, '0');
    return `${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

function getMeasurementConvolverItemName(mode = 'both', autoGainDb = 0, options = {}) {
    const conv = ensureMeasurementConvolverState();
    const curve = getMeasurementConvolverCurve(conv.targetCurve);
    const prefix = mode === 'both' ? 'Conv LR' : (mode === 'right' ? 'Conv R' : 'Conv L');
    const phaseTag = getMeasurementConvolverPhaseTag(conv.phaseMode);
    const base = `${prefix} ${phaseTag} ${curve.shortLabel || curve.label} ${Math.round(conv.rangeStartHz)}-${Math.round(conv.rangeEndHz)}Hz ${formatMeasurementConvolverGain(autoGainDb)}`;
    return options.unique ? `${base} ${getMeasurementConvolverNameSuffix()}` : base;
}

function showMeasurementConvolverFeedback(message) {
    if (!elements.measurementConvolverFeedback) return;
    elements.measurementConvolverFeedback.textContent = message;
    elements.measurementConvolverFeedback.classList.add('is-visible');
    setTimeout(() => elements.measurementConvolverFeedback?.classList.remove('is-visible'), 2600);
}

function getMeasurementConvolverSampleRate() {
    const conv = ensureMeasurementConvolverState();
    const selected = Number(conv.sampleRate);
    return Number.isFinite(selected) && selected > 0 ? selected : 48000;
}

const measurementConvolverTapOptions = [2048, 4096, 8192, 16384, 32768];
const measurementConvolverTypeOptions = ['linear', 'minimum', 'minimum_aligned'].flatMap((phaseMode) => measurementConvolverTapOptions.map((taps) => ({
    key: `${phaseMode}_${taps}`,
    phaseMode,
    taps,
    label: `${phaseMode === 'minimum' ? 'Min.' : phaseMode === 'minimum_aligned' ? 'Min.align' : 'Linear'} phase ${taps}`,
})));

function getMeasurementConvolverTypeOption(type = 'linear_4096') {
    return measurementConvolverTypeOptions.find((option) => option.key === type) || measurementConvolverTypeOptions[0];
}

function getMeasurementConvolverTypeKeys() {
    return measurementConvolverTypeOptions.map((option) => option.key);
}

function getMeasurementConvolverPhaseModeForType(type = 'linear_4096') {
    return getMeasurementConvolverTypeOption(type).phaseMode;
}

function getMeasurementConvolverPhaseLabel(phaseMode = 'linear') {
    if (phaseMode === 'minimum') return 'Minimum phase FIR';
    if (phaseMode === 'minimum_aligned') return 'Minimum phase aligned FIR';
    return 'Linear FIR';
}

function getMeasurementConvolverPhaseTag(phaseMode = 'linear') {
    if (phaseMode === 'minimum') return 'Min';
    if (phaseMode === 'minimum_aligned') return 'Min.align';
    return 'Lin';
}

function getMeasurementConvolverFirLengthForType(type = 'linear_4096') {
    return getMeasurementConvolverTypeOption(type).taps;
}

function getMeasurementConvolverFirLength() {
    const conv = ensureMeasurementConvolverState();
    return getMeasurementConvolverFirLengthForType(conv.quality);
}

function getMeasurementConvolverTypeLabel(type = 'linear_4096') {
    return getMeasurementConvolverTypeOption(type).label;
}

function interpolateMeasurementConvolverCorrection(analysis, frequencyHz, autoGainDb) {
    return MeasurementDsp.interpolateMeasurementConvolverCorrection(analysis, frequencyHz, autoGainDb);
}

function buildMeasurementConvolverMagnitudeBins(analysis, sampleRate, length, autoGainDb) {
    return MeasurementDsp.buildMeasurementConvolverMagnitudeBins(analysis, sampleRate, length, autoGainDb);
}

function buildMeasurementConvolverLinearImpulseFromMagnitudes(magnitudes, length) {
    return MeasurementDsp.buildMeasurementConvolverLinearImpulseFromMagnitudes(magnitudes, length);
}

function fftMeasurementConvolverComplex(real, imag, inverse = false) {
    return MeasurementDsp.fftMeasurementConvolverComplex(real, imag, inverse);
}

function buildMeasurementConvolverMinimumSpectrum(magnitudes, length) {
    return MeasurementDsp.buildMeasurementConvolverMinimumSpectrum(magnitudes, length);
}

function buildMeasurementConvolverImpulseFromSpectrum(real, imag) {
    return MeasurementDsp.buildMeasurementConvolverImpulseFromSpectrum(real, imag);
}

function buildMeasurementConvolverImpulse(analysis, sampleRate, length, autoGainDb, phaseMode = 'linear') {
    return MeasurementDsp.buildMeasurementConvolverImpulse(analysis, sampleRate, length, autoGainDb, phaseMode);
}

function getMeasurementConvolverTimingMs(timing = {}) {
    const arrivalMs = Number(timing?.arrivalMs);
    return Number.isFinite(arrivalMs) ? arrivalMs : null;
}

function getMeasurementConvolverTimingDelta(leftTiming, rightTiming) {
    if (!leftTiming?.available || !rightTiming?.available) return null;
    const leftMs = getMeasurementConvolverTimingMs(leftTiming);
    const rightMs = getMeasurementConvolverTimingMs(rightTiming);
    if (leftMs === null || rightMs === null) return null;
    const deltaMs = rightMs - leftMs;
    if (!Number.isFinite(deltaMs)) return null;
    const result = {
        deltaMs,
        absMs: Math.abs(deltaMs),
        laterSide: deltaMs >= 0 ? 'R' : 'L',
        correctionSide: deltaMs >= 0 ? 'L' : 'R',
        leftTiming,
        rightTiming,
    };
    console.info('[measurement-convolver-timing]', {
        left: {
            channel: leftTiming.channel || 'left',
            source: leftTiming.source || '',
            measurementId: leftTiming.measurementId || '',
            peakSample: leftTiming.peakSample ?? null,
            directSample: leftTiming.directSample ?? null,
            referencePeakSample: leftTiming.referencePeakSample ?? null,
            referenceAnchorSample: leftTiming.referenceAnchorSample ?? null,
            arrivalSamples: leftTiming.arrivalSamples ?? null,
            selectedScore: leftTiming.selectedScore ?? null,
            selectedSupportScore: leftTiming.selectedSupportScore ?? null,
            confidence: leftTiming.confidence ?? null,
            selectionRule: leftTiming.selectionRule || '',
            firstThresholdSample: leftTiming.firstThresholdSample ?? null,
            firstThresholdOffsetFromPeakSamples: leftTiming.firstThresholdOffsetFromPeakSamples ?? null,
            candidateCount: leftTiming.candidateCount ?? null,
            topCandidates: leftTiming.topCandidates || [],
            chronologicalCandidates: leftTiming.chronologicalCandidates || [],
            sampleRate: leftTiming.sampleRate ?? null,
        },
        right: {
            channel: rightTiming.channel || 'right',
            source: rightTiming.source || '',
            measurementId: rightTiming.measurementId || '',
            peakSample: rightTiming.peakSample ?? null,
            directSample: rightTiming.directSample ?? null,
            referencePeakSample: rightTiming.referencePeakSample ?? null,
            referenceAnchorSample: rightTiming.referenceAnchorSample ?? null,
            arrivalSamples: rightTiming.arrivalSamples ?? null,
            selectedScore: rightTiming.selectedScore ?? null,
            selectedSupportScore: rightTiming.selectedSupportScore ?? null,
            confidence: rightTiming.confidence ?? null,
            selectionRule: rightTiming.selectionRule || '',
            firstThresholdSample: rightTiming.firstThresholdSample ?? null,
            firstThresholdOffsetFromPeakSamples: rightTiming.firstThresholdOffsetFromPeakSamples ?? null,
            candidateCount: rightTiming.candidateCount ?? null,
            topCandidates: rightTiming.topCandidates || [],
            chronologicalCandidates: rightTiming.chronologicalCandidates || [],
            sampleRate: rightTiming.sampleRate ?? null,
        },
        deltaMs,
        deltaSamples: Number.isFinite(Number(leftTiming.sampleRate)) ? Math.round(deltaMs / 1000 * Number(leftTiming.sampleRate)) : null,
        sampleRate: leftTiming.sampleRate || rightTiming.sampleRate || null,
    });
    return result;
}

function formatMeasurementConvolverTimingRelation(timingDelta) {
    if (!timingDelta) return 'Timing unavailable';
    const earlierSide = timingDelta.laterSide === 'R' ? 'L' : 'R';
    return `${timingDelta.laterSide} arrives ${timingDelta.absMs.toFixed(2)} ms later than ${earlierSide}`;
}

function getMeasurementConvolverTimingSafetyMessage(timingDelta, limitMs = MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
    if (!timingDelta || !(timingDelta.absMs > limitMs)) return '';
    return `Timing offset too large: ${timingDelta.absMs.toFixed(2)} ms. Filter was not created. Move the microphone closer to the center position or raise the safety limit for test measurements.`;
}

function alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, leftTiming, rightTiming, maxAlignMs = MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
    const timingDelta = getMeasurementConvolverTimingDelta(leftTiming, rightTiming);
    if (!timingDelta) return [leftImpulse, rightImpulse];
    if (timingDelta.absMs > maxAlignMs) {
        throw new Error(getMeasurementConvolverTimingSafetyMessage(timingDelta, maxAlignMs) || 'Timing offset exceeds safety limit.');
    }
    const deltaMs = timingDelta.deltaMs;
    if (Math.abs(deltaMs) < 0.005) return [leftImpulse, rightImpulse];
    const cappedMs = Math.max(-maxAlignMs, Math.min(maxAlignMs, deltaMs));
    const alignedSamples = Math.round(Math.abs(cappedMs) / 1000 * sampleRate);
    if (alignedSamples < 1) return [leftImpulse, rightImpulse];
    const silence = new Float64Array(alignedSamples);
    if (cappedMs > 0) {
        const newLeft = new Float64Array(alignedSamples + leftImpulse.length);
        newLeft.set(silence, 0);
        newLeft.set(leftImpulse, alignedSamples);
        return [newLeft, rightImpulse];
    } else {
        const newRight = new Float64Array(alignedSamples + rightImpulse.length);
        newRight.set(silence, 0);
        newRight.set(rightImpulse, alignedSamples);
        return [leftImpulse, newRight];
    }
}

function getMeasurementAnalysisSampleRate(measurement = {}) {
    const candidates = [
        measurement?.analysis?.sample_rate,
        measurement?.analysis?.sampleRate,
        measurement?.summary?.sample_rate,
        measurement?.summary?.sampleRate,
        measurement?.review_summary?.sample_rate,
        measurement?.review_summary?.sampleRate,
        measurement?.sample_rate,
        measurement?.sampleRate,
    ];
    const sampleRate = candidates.map(Number).find((value) => Number.isFinite(value) && value > 0);
    return sampleRate || null;
}

function getMeasurementDirectArrivalTiming(measurement = {}) {
    const impulse = measurement?.analysis?.impulse_response || null;
    const channel = String(measurement?.channel || '').toLowerCase();
    if (channel === 'stereo') {
        return { available: false, reason: 'merged-measurement' };
    }
    const sampleRate = getMeasurementAnalysisSampleRate(measurement);
    if (!impulse || !sampleRate) {
        return { available: false, reason: 'missing-direct-arrival-timing' };
    }
    const arrivalMs = Number(impulse?.arrival_ms);
    const arrivalSamples = Number(impulse?.arrival_samples);
    const directSample = Number(impulse?.direct_arrival_index);
    const referencePeakSample = Number(impulse?.reference_peak_index);
    const referenceAnchorSample = Number(measurement?.analysis?.alignment_samples);
    if (
        !Number.isFinite(arrivalMs)
        || !Number.isFinite(arrivalSamples)
        || !Number.isFinite(directSample)
        || !Number.isFinite(referencePeakSample)
    ) {
        return { available: false, reason: 'missing-direct-arrival-timing' };
    }
    const peakSample = Number(impulse?.peak_index);
    const mapDirectCandidate = (candidate) => ({
            sample: Number.isFinite(Number(candidate?.sample)) ? Number(candidate.sample) : null,
            offsetFromPeakSamples: Number.isFinite(Number(candidate?.offset_from_peak_samples)) ? Number(candidate.offset_from_peak_samples) : null,
            offsetFromPeakMs: Number.isFinite(Number(candidate?.offset_from_peak_ms)) ? Number(candidate.offset_from_peak_ms) : null,
            score: Number.isFinite(Number(candidate?.score)) ? Number(candidate.score) : null,
            peakScore: Number.isFinite(Number(candidate?.peak_score)) ? Number(candidate.peak_score) : null,
            relativeDb: Number.isFinite(Number(candidate?.relative_db)) ? Number(candidate.relative_db) : null,
            localEnergyRelative: Number.isFinite(Number(candidate?.local_energy_relative)) ? Number(candidate.local_energy_relative) : null,
            prominenceRelative: Number.isFinite(Number(candidate?.prominence_relative)) ? Number(candidate.prominence_relative) : null,
            prominenceRatio: Number.isFinite(Number(candidate?.prominence_ratio)) ? Number(candidate.prominence_ratio) : null,
            supportScore: Number.isFinite(Number(candidate?.support_score)) ? Number(candidate.support_score) : null,
            distanceFromFirstThresholdSamples: Number.isFinite(Number(candidate?.distance_from_first_threshold_samples)) ? Number(candidate.distance_from_first_threshold_samples) : null,
            weakThresholdEdge: !!candidate?.weak_threshold_edge,
            strongerImpulseRegion: !!candidate?.stronger_impulse_region,
        });
    const scoreSortedSource = Array.isArray(impulse?.direct_candidates_by_score)
        ? impulse.direct_candidates_by_score
        : impulse?.direct_candidates;
    const topCandidates = Array.isArray(scoreSortedSource)
        ? scoreSortedSource.slice(0, 8).map(mapDirectCandidate)
        : [];
    const chronologicalCandidates = Array.isArray(impulse?.direct_candidates_chronological)
        ? impulse.direct_candidates_chronological.slice(0, 12).map(mapDirectCandidate)
        : [];
    return {
        available: true,
        arrivalMs,
        arrivalSamples,
        peakSample: Number.isFinite(peakSample) ? peakSample : null,
        directSample,
        referencePeakSample,
        referenceAnchorSample: Number.isFinite(referenceAnchorSample) ? referenceAnchorSample : null,
        selectedScore: Number.isFinite(Number(impulse?.direct_selected_score)) ? Number(impulse.direct_selected_score) : null,
        selectedSupportScore: Number.isFinite(Number(impulse?.direct_selected_support_score)) ? Number(impulse.direct_selected_support_score) : null,
        confidence: Number.isFinite(Number(impulse?.direct_confidence)) ? Number(impulse.direct_confidence) : null,
        selectionRule: impulse?.direct_selection_rule || '',
        firstThresholdSample: Number.isFinite(Number(impulse?.direct_first_threshold_index)) ? Number(impulse.direct_first_threshold_index) : null,
        firstThresholdOffsetFromPeakSamples: Number.isFinite(Number(impulse?.direct_first_threshold_offset_from_peak_samples)) ? Number(impulse.direct_first_threshold_offset_from_peak_samples) : null,
        candidateCount: Number.isFinite(Number(impulse?.direct_candidate_count)) ? Number(impulse.direct_candidate_count) : null,
        topCandidates,
        chronologicalCandidates,
        sampleRate,
        channel,
        measurementId: measurement?.id || '',
        measurementName: measurement?.name || '',
        source: impulse?.timing_source || 'direct_arrival_minus_reference_peak',
    };
}

function writeMeasurementConvolverWav(channels, sampleRate) {
    return MeasurementDsp.writeMeasurementConvolverWav(channels, sampleRate);
}

function appendMeasurementConvolverExtras(formData) {
    const extras = collectEffectsExtras();
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
}

async function createMeasurementConvolverPreset(mode, analyses, sharedAutoGainDb, itemName, options = {}) {
    const sampleRate = Number(options.sampleRate) || getMeasurementConvolverSampleRate();
    const length = Number(options.irLength) || getMeasurementConvolverFirLength();
    const phaseMode = ['linear', 'minimum', 'minimum_aligned'].includes(options.phaseMode) ? options.phaseMode : ensureMeasurementConvolverState().phaseMode;
    const filenameBase = itemName.replace(/[^a-z0-9._-]+/gi, '-').replace(/^-+|-+$/g, '') || 'measurement-convolver';
    const bySide = Object.fromEntries(analyses.map((analysis) => [analysis.side, analysis]));
    if (mode === 'both') {
        const applyPhaseMode = phaseMode === 'minimum_aligned' ? 'minimum' : phaseMode;
        const leftImpulse = buildMeasurementConvolverImpulse(bySide.left, sampleRate, length, sharedAutoGainDb, applyPhaseMode);
        const rightImpulse = buildMeasurementConvolverImpulse(bySide.right, sampleRate, length, sharedAutoGainDb, applyPhaseMode);
        const [finalLeft, finalRight] = phaseMode === 'minimum_aligned'
            ? alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, options.leftTiming || null, options.rightTiming || null, Number(options.timingSafetyLimitMs) || MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS)
            : [leftImpulse, rightImpulse];
        const leftBlob = writeMeasurementConvolverWav([finalLeft], sampleRate);
        const rightBlob = writeMeasurementConvolverWav([finalRight], sampleRate);
        const formData = new FormData();
        formData.append('preset_name', itemName);
        appendMeasurementConvolverExtras(formData);
        formData.append('left_file', leftBlob, `${filenameBase}-L.wav`);
        formData.append('right_file', rightBlob, `${filenameBase}-R.wav`);
        const resp = await fetch('/api/easyeffects/presets/import-filter-dual', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Convolver preset creation failed');
        return data;
    }
    const side = mode === 'right' ? 'right' : 'left';
    const applyPhaseMode = phaseMode === 'minimum_aligned' ? 'minimum' : phaseMode;
    const impulse = buildMeasurementConvolverImpulse(bySide[side], sampleRate, length, sharedAutoGainDb, applyPhaseMode);
    const blob = writeMeasurementConvolverWav([impulse], sampleRate);
    const formData = new FormData();
    formData.append('preset_name', itemName);
    appendMeasurementConvolverExtras(formData);
    formData.append('file', blob, `${filenameBase}-${side === 'right' ? 'R' : 'L'}.wav`);
    const resp = await fetch('/api/easyeffects/presets/create-with-ir', { method: 'POST', body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || 'Convolver preset creation failed');
    return data;
}

function takeMeasurementConvolverToDraft(mode = 'both') {
    if (getMeasurementConvolverSelectedSourceCount() > 1) {
        const warning = getMeasurementConvolverMultiSourceWarning();
        showMeasurementConvolverFeedback(warning);
        showToast(warning, 'warning');
        return;
    }
    const sides = mode === 'left' ? ['left'] : (mode === 'right' ? ['right'] : ['left', 'right']);
    const analyses = sides.map((side) => analyzeMeasurementConvolverSide(side));
    if (analyses.some((analysis) => !analysis)) {
        showToast('Run or show a measurement with points in the selected correction range first', 'warning');
        return;
    }
    const conv = ensureMeasurementConvolverState();
    if (conv.phaseMode === 'minimum_aligned' && sides.length === 2) {
        const sourceTimings = sides.map((side) => getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide(side)));
        const leftTimingOk = sourceTimings[0]?.available === true;
        const rightTimingOk = sourceTimings[1]?.available === true;
        if (!leftTimingOk || !rightTimingOk) {
            showMeasurementConvolverFeedback('Timing align needs single L/R measurements.');
            showToast('Timing align needs single L/R measurements.', 'warning');
            return;
        }
        const timingDelta = getMeasurementConvolverTimingDelta(sourceTimings[0], sourceTimings[1]);
        const safetyMessage = getMeasurementConvolverTimingSafetyMessage(timingDelta);
        if (safetyMessage) {
            showMeasurementConvolverFeedback('Filter not created because timing offset exceeds safety limit.');
            showToast(safetyMessage, 'warning');
            return;
        }
    }
    sides.forEach((side, index) => {
        const analysis = analyses[index];
        const sourceMeasurement = getMeasurementConvolverMeasurementForSide(side);
        const timing = getMeasurementDirectArrivalTiming(sourceMeasurement);
        conv.draft[side] = {
            side,
            createdAt: new Date().toISOString(),
            analysis,
            timing,
            metadata: {
                targetCurve: conv.targetCurve,
                rangeStartHz: conv.rangeStartHz,
                rangeEndHz: conv.rangeEndHz,
                maxBoostDb: conv.maxBoostDb,
                maxCutDb: conv.maxCutDb,
                dipGuard: conv.dipGuard,
                safetyMarginDb: conv.safetyMarginDb,
                autoGainDb: analysis.autoGainDb,
                sampleRate: getMeasurementConvolverSampleRate(),
                quality: conv.quality,
                phaseMode: conv.phaseMode,
                irLength: getMeasurementConvolverFirLength(),
                sourceMeasurementId: sourceMeasurement?.id || '',
                sourceMeasurementName: sourceMeasurement?.name || '',
                sourceMeasurementCreatedAt: sourceMeasurement?.created_at || '',
                sourceChannel: sourceMeasurement?.channel || side,
            },
        };
    });
    const sharedAutoGainDb = Math.min(...analyses.map((analysis) => analysis.autoGainDb));
    const effectiveMode = conv.draft.left && conv.draft.right ? 'both' : mode;
    if (!conv.draft.nameTouched) conv.draft.presetName = getMeasurementConvolverItemName(effectiveMode, sharedAutoGainDb, { unique: true });
    renderMeasurementPanel();
    const label = mode === 'both' ? 'Left + Right staged' : (mode === 'right' ? 'Right staged' : 'Left staged');
    showMeasurementConvolverFeedback(label);
    showToast(`Convolver draft updated: ${label}`, 'success');
}

async function createMeasurementConvolverPresetFromDraft() {
    if (convolverCreateInFlight) {
        showToast('Convolver preset creation already in progress', 'warning');
        return;
    }
    const conv = ensureMeasurementConvolverState();
    const leftDraft = conv.draft?.left || null;
    const rightDraft = conv.draft?.right || null;
    const mode = leftDraft && rightDraft ? 'both' : (rightDraft ? 'right' : (leftDraft ? 'left' : null));
    if (!mode) {
        showToast('Take L, R or Both into the convolver draft first', 'warning');
        return;
    }
    const drafts = mode === 'both' ? [leftDraft, rightDraft] : [mode === 'right' ? rightDraft : leftDraft];
    if (mode === 'both') {
        const [leftMeta, rightMeta] = drafts.map((draft) => draft?.metadata || {});
        const sameGeneration = ['sampleRate', 'quality', 'phaseMode', 'irLength'].every((key) => String(leftMeta[key] || '') === String(rightMeta[key] || ''));
        if (!sameGeneration) {
            showMeasurementConvolverFeedback('Retake L/R with matching Convolver type and sample rate');
            showToast('Left and Right convolver drafts use different FIR settings. Retake Both for a matched comparison preset.', 'warning');
            return;
        }
        const draftPhaseMode = leftMeta.phaseMode || rightMeta.phaseMode || conv.phaseMode;
        if (draftPhaseMode === 'minimum_aligned') {
            const leftTimingOk = leftDraft?.timing?.available === true;
            const rightTimingOk = rightDraft?.timing?.available === true;
            if (!leftTimingOk || !rightTimingOk) {
                showMeasurementConvolverFeedback('Timing align needs single L/R measurements.');
                showToast('Timing align needs single L/R measurements.', 'warning');
                return;
            }
            const timingDelta = getMeasurementConvolverTimingDelta(leftDraft?.timing, rightDraft?.timing);
            const safetyMessage = getMeasurementConvolverTimingSafetyMessage(timingDelta);
            if (safetyMessage) {
                showMeasurementConvolverFeedback('Filter not created because timing offset exceeds safety limit.');
                showToast(safetyMessage, 'warning');
                return;
            }
        }
    }
    const analyses = drafts.map((draft) => draft.analysis);
    const sharedAutoGainDb = Math.min(...analyses.map((analysis) => analysis.autoGainDb));
    const itemName = String(conv.draft?.presetName || '').trim() || getMeasurementConvolverItemName(mode, sharedAutoGainDb, { unique: true });
    conv.draft.presetName = itemName;
    convolverCreateInFlight = true;
    if (elements.measurementConvolverCreateBtn) elements.measurementConvolverCreateBtn.disabled = true;
    showMeasurementConvolverFeedback(`Creating ${itemName}…`);
    try {
        const draftMetadata = drafts[0]?.metadata || {};
        const generationOptions = {
            sampleRate: draftMetadata.sampleRate || getMeasurementConvolverSampleRate(),
            quality: draftMetadata.quality || conv.quality,
            phaseMode: draftMetadata.phaseMode || conv.phaseMode,
            irLength: draftMetadata.irLength || getMeasurementConvolverFirLength(),
            leftTiming: leftDraft?.timing || null,
            rightTiming: rightDraft?.timing || null,
            timingSafetyLimitMs: MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS,
        };
        const created = await createMeasurementConvolverPreset(mode, analyses, sharedAutoGainDb, itemName, generationOptions);
        const item = {
            type: 'convolver',
            mode,
            name: itemName,
            createdAt: new Date().toISOString(),
            preset: created.preset || null,
            ir: created.ir || null,
            metadata: {
                targetCurve: draftMetadata.targetCurve || conv.targetCurve,
                rangeStartHz: draftMetadata.rangeStartHz || conv.rangeStartHz,
                rangeEndHz: draftMetadata.rangeEndHz || conv.rangeEndHz,
                maxBoostDb: draftMetadata.maxBoostDb ?? conv.maxBoostDb,
                maxCutDb: draftMetadata.maxCutDb ?? conv.maxCutDb,
                dipGuard: draftMetadata.dipGuard ?? conv.dipGuard,
                safetyMarginDb: draftMetadata.safetyMarginDb ?? conv.safetyMarginDb,
                autoGainDb: sharedAutoGainDb,
                sampleRate: generationOptions.sampleRate,
                quality: generationOptions.quality,
                phaseMode: generationOptions.phaseMode,
                irLength: generationOptions.irLength,
                generatedIr: true,
            },
            analyses: analyses.map((analysis) => ({ side: analysis.side, points: analysis.points, maxPositive: analysis.maxPositive, minCorrection: analysis.minCorrection, autoGainDb: analysis.autoGainDb, dipGuardReductionMaxDb: analysis.dipGuardReductionMaxDb })),
        };
        state.easyeffects = state.easyeffects || {};
        state.easyeffects.assistStack = state.easyeffects.assistStack || [];
        state.easyeffects.assistStack.push(item);
        conv.draft.left = null;
        conv.draft.right = null;
        conv.draft.presetName = '';
        conv.draft.nameTouched = false;
        await fetchEffects();
        showMeasurementConvolverFeedback(`${itemName} created`);
        showToast(`Created convolver preset: ${created.preset?.name || itemName}`, 'success');
    } catch (e) {
        showMeasurementConvolverFeedback('Convolver preset creation failed');
        showToast(e.message || 'Convolver preset creation failed', 'error');
    } finally {
        convolverCreateInFlight = false;
        renderMeasurementPanel();
    }
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

function getMeasurementConvolverRangeHandleAtPosition(x, y, bounds) {
    const conv = ensureMeasurementConvolverState();
    const startX = measurementFrequencyToX(conv.rangeStartHz, bounds);
    const endX = measurementFrequencyToX(conv.rangeEndHz, bounds);
    if (y < bounds.top || y > bounds.top + bounds.height) return null;
    if (Math.abs(x - startX) <= 12) return 'start';
    if (Math.abs(x - endX) <= 12) return 'end';
    if (x > startX && x < endX) return 'move';
    return null;
}

function drawMeasurementTargetCurve(ctx, bounds, range) {
    const conv = ensureMeasurementConvolverState();
    const curve = getMeasurementConvolverCurve(conv.targetCurve);
    const frequencies = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000];
    ctx.save();
    ctx.strokeStyle = '#6ee7b7';
    ctx.lineWidth = 1.4;
    ctx.setLineDash([6, 5]);
    ctx.beginPath();
    frequencies.forEach((frequency, index) => {
        const x = measurementFrequencyToX(frequency, bounds);
        const y = Math.max(bounds.top, Math.min(bounds.top + bounds.height, measurementDbToY(getMeasurementConvolverCurveDb(conv.targetCurve, frequency), bounds, range)));
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
    if ((state.measurement?.assistMode || 'peq') !== 'convolver') return;
    const conv = ensureMeasurementConvolverState();
    const startX = measurementFrequencyToX(conv.rangeStartHz, bounds);
    const endX = measurementFrequencyToX(conv.rangeEndHz, bounds);
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

function handleMeasurementGraphPointerDown(event) {
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return;
    const { x, y, bounds, range } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return;
    const pointerType = String(event.pointerType || '');
    if ((state.measurement?.assistMode || 'peq') === 'convolver') {
        const conv = ensureMeasurementConvolverState();
        const dragMode = getMeasurementConvolverRangeHandleAtPosition(x, y, bounds);
        if (!dragMode) return;
        if (pointerType === 'touch') event.preventDefault();
        conv.dragMode = dragMode;
        conv.dragAnchorHz = measurementXToFrequency(x, bounds);
        conv.dragStartHz = conv.rangeStartHz;
        conv.dragEndHz = conv.rangeEndHz;
        measurementGraphPointerId = event.pointerId;
        elements.measurementGraph?.setPointerCapture?.(event.pointerId);
        renderMeasurementPanel();
        return;
    }
    const peq = ensureMeasurementPeqState();
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
    const conv = ensureMeasurementConvolverState();
    if (conv.dragMode && measurementGraphPointerId === event.pointerId) {
        if (event.pointerType === 'touch') event.preventDefault();
        const pointer = getMeasurementGraphPointerPosition(event);
        if (!pointer) return;
        const currentHz = measurementXToFrequency(pointer.x, pointer.bounds);
        if (conv.dragMode === 'start') conv.rangeStartHz = Math.min(Math.round(clampMeasurementConvolverFrequency(currentHz)), conv.rangeEndHz - 1);
        if (conv.dragMode === 'end') conv.rangeEndHz = Math.max(Math.round(clampMeasurementConvolverFrequency(currentHz)), conv.rangeStartHz + 1);
        if (conv.dragMode === 'move') {
            const ratio = Math.log10(currentHz / Math.max(1, conv.dragAnchorHz || currentHz));
            const start = clampMeasurementConvolverFrequency((conv.dragStartHz || conv.rangeStartHz) * (10 ** ratio));
            const end = clampMeasurementConvolverFrequency((conv.dragEndHz || conv.rangeEndHz) * (10 ** ratio));
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
        ensureMeasurementConvolverState();
        scheduleMeasurementGraphRender();
        renderMeasurementPanel();
        return;
    }
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
    const conv = ensureMeasurementConvolverState();
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId && event.pointerType === 'touch') {
        event.preventDefault();
    }
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId) {
        elements.measurementGraph?.releasePointerCapture?.(event.pointerId);
        measurementGraphPointerId = null;
    }
    peq.dragFilterId = null;
    conv.dragMode = null;
    delete conv.dragAnchorHz;
    delete conv.dragStartHz;
    delete conv.dragEndHz;
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

function measurementHasCalibrationSelected() {
    return !!(state.measurement.calibrationFilename || state.measurement.selectedCalibrationRef);
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

function applyMeasurementHouseCurveState(data) {
    if (!data || typeof data !== 'object') return;
    state.measurement.houseCurveOptions = Array.isArray(data.house_curves) ? data.house_curves : state.measurement.houseCurveOptions;
    state.measurement.houseCurveFilename = '';
    if (elements.measurementHouseCurveFile) elements.measurementHouseCurveFile.value = '';
}

async function uploadMeasurementHouseCurve(file) {
    if (!file) return;
    state.measurement.houseCurveUpdating = true;
    state.measurement.houseCurveFilename = file.name || 'house-curve.txt';
    renderMeasurementPanel();
    const formData = new FormData();
    formData.append('house_curve_file', file);
    try {
        const resp = await fetch('/api/measurements/house-curves', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to upload house curve file');
        applyMeasurementHouseCurveState(data);
        const uploadedId = data.uploaded_house_curve_id ? `house:${data.uploaded_house_curve_id}` : '';
        if (uploadedId) updateMeasurementConvolverField('targetCurve', uploadedId);
        showToast('House curve uploaded and selected', 'success');
    } catch (error) {
        console.error('uploadMeasurementHouseCurve failed', error);
        state.measurement.statusText = error.message || 'Failed to upload house curve file';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.houseCurveUpdating = false;
        renderMeasurementPanel();
    }
}

async function deleteSelectedMeasurementHouseCurve() {
    const conv = ensureMeasurementConvolverState();
    const selectedKey = String(conv.targetCurve || '');
    const houseCurveId = selectedKey.startsWith('house:') ? selectedKey.slice(6) : '';
    const selected = (state.measurement.houseCurveOptions || []).find(option => option.id === houseCurveId);
    if (!houseCurveId || !selected) {
        showToast('No house curve file selected to delete', 'warning');
        return;
    }
    if (!window.confirm(`Delete house curve file "${selected.filename || houseCurveId}"?`)) return;
    state.measurement.houseCurveDeleting = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch(`/api/measurements/house-curves/${encodeURIComponent(houseCurveId)}`, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to delete house curve file');
        applyMeasurementHouseCurveState(data);
        if (conv.targetCurve === selectedKey) conv.targetCurve = 'neutral';
        showToast('House curve file deleted', 'success');
    } catch (error) {
        console.error('deleteSelectedMeasurementHouseCurve failed', error);
        state.measurement.statusText = error.message || 'Failed to delete house curve file';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.houseCurveDeleting = false;
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
        state.measurement.houseCurveOptions = Array.isArray(data.house_curves) ? data.house_curves : [];
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
            if (!state.measurement.hostCaptureAvailable) {
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
        state.measurement.statusText = error.message || 'Failed to load capture inputs';
    } finally {
        state.measurement.inputsLoading = false;
        renderMeasurementPanel();
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
    if (shouldOpen) {
        measurementInputScanOnFocusDone = false;
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
    return MeasurementDsp.getSortedNumericValues(values);
}

function getValueQuantile(sortedValues = [], quantile = 0.5) {
    return MeasurementDsp.getValueQuantile(sortedValues, quantile);
}

function getMeasurementGraphRange(entries = []) {
    return MeasurementDsp.getMeasurementGraphRange(entries);
}

function measurementFrequencyToX(frequency, bounds) {
    return MeasurementDsp.measurementFrequencyToX(frequency, bounds);
}

function measurementDbToY(level, bounds, range) {
    return MeasurementDsp.measurementDbToY(level, bounds, range);
}

function getMeasurementPeqFilterMagnitude(filter = {}, frequencyHz = 1000, sampleRate = 48000) {
    return MeasurementDsp.getMeasurementPeqFilterMagnitude(filter, frequencyHz, sampleRate);
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

    drawMeasurementTargetCurve(ctx, bounds, range);

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

    drawMeasurementConvolverRangeOverlay(ctx, bounds);
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

function logSaveCurrentMeasurementDebug(stage, details = {}) {
    const measurementState = state.measurement || {};
    const current = measurementState.currentMeasurement || null;
    const visibleIds = Object.entries(measurementState.visibilityById || {})
        .filter(([, visible]) => !!visible)
        .map(([id]) => id);
    console.info('[measurement-save-current]', stage, {
        currentId: current?.id || '',
        currentName: current?.name || '',
        currentTraceCount: Array.isArray(current?.traces) ? current.traces.length : 0,
        savedCount: Array.isArray(measurementState.measurements) ? measurementState.measurements.length : 0,
        visibleIds,
        saveInFlight: !!measurementState.saveInFlight,
        startInFlight: !!measurementState.startInFlight,
        activeJobId: measurementState.activeJobId || '',
        ...details,
    });
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
    if (!measurementModeReady()) {
        state.measurement.statusText = 'No usable host capture source is available for a real measurement on this host.';
        renderMeasurementPanel();
        showToast(state.measurement.statusText, 'error');
        return;
    }

    state.measurement.startInFlight = true;
    state.measurement.activeJobId = '';
    state.measurement.currentMeasurementSaved = false;
    renderMeasurementPanel();

    try {
        await startHostMeasurement();
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

    const debugStart = window.performance?.now?.() || Date.now();
    const debugElapsedMs = () => Math.round((window.performance?.now?.() || Date.now()) - debugStart);
    const payload = JSON.parse(JSON.stringify(current));
    payload.name = (state.measurement.currentMeasurementName || current.name || '').trim() || current.name || 'Measurement';

    state.measurement.saveInFlight = true;
    state.measurement.statusText = 'Saving current measurement…';
    logSaveCurrentMeasurementDebug('start', {
        payloadId: payload.id || '',
        payloadName: payload.name || '',
        payloadTraceCount: Array.isArray(payload.traces) ? payload.traces.length : 0,
        elapsedMs: debugElapsedMs(),
    });
    logSaveCurrentMeasurementDebug('before initial renderMeasurementPanel', { elapsedMs: debugElapsedMs() });
    renderMeasurementPanel();
    logSaveCurrentMeasurementDebug('after initial renderMeasurementPanel', { elapsedMs: debugElapsedMs() });
    try {
        logSaveCurrentMeasurementDebug('before POST /api/measurements/save', {
            payloadId: payload.id || '',
            payloadName: payload.name || '',
            elapsedMs: debugElapsedMs(),
        });
        const resp = await fetch('/api/measurements/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        logSaveCurrentMeasurementDebug('after POST response received', {
            status: resp.status,
            ok: resp.ok,
            elapsedMs: debugElapsedMs(),
        });
        const data = await resp.json().catch(() => ({}));
        logSaveCurrentMeasurementDebug('after response JSON parsed', {
            status: resp.status,
            ok: resp.ok,
            responseStatus: data.status || '',
            responseDetail: data.detail || '',
            returnedId: data.measurement?.id || '',
            returnedName: data.measurement?.name || '',
            elapsedMs: debugElapsedMs(),
        });
        if (!resp.ok) throw new Error(data.detail || 'Failed to save measurement');
        const saved = normalizeMeasurementEntry(data.measurement || payload, 0);
        logSaveCurrentMeasurementDebug('saved measurement normalized', {
            savedId: saved.id,
            savedName: saved.name,
            savedTraceCount: saved.traces.length,
            elapsedMs: debugElapsedMs(),
        });
        state.measurement.visibilityById[saved.id] = true;
        state.measurement.reviewVisibilityById[saved.id] = false;
        logSaveCurrentMeasurementDebug('before clearing currentMeasurement', {
            savedId: saved.id,
            savedName: saved.name,
            elapsedMs: debugElapsedMs(),
        });
        state.measurement.currentMeasurement = null;
        state.measurement.currentMeasurementSaved = false;
        state.measurement.currentMeasurementName = '';
        state.measurement.statusText = 'Measurement saved.';
        logSaveCurrentMeasurementDebug('after clearing currentMeasurement', {
            savedId: saved.id,
            savedName: saved.name,
            elapsedMs: debugElapsedMs(),
        });
        logSaveCurrentMeasurementDebug('before fetchMeasurements', { elapsedMs: debugElapsedMs() });
        await fetchMeasurements();
        logSaveCurrentMeasurementDebug('after fetchMeasurements', { elapsedMs: debugElapsedMs() });
        showToast('Measurement saved', 'success');
    } catch (error) {
        console.error('saveCurrentMeasurement failed', {
            message: error?.message || String(error),
            name: error?.name || '',
            stack: error?.stack || '',
            elapsedMs: debugElapsedMs(),
            error,
        });
        logSaveCurrentMeasurementDebug('catch error details', {
            errorMessage: error?.message || String(error),
            errorName: error?.name || '',
            elapsedMs: debugElapsedMs(),
        });
        state.measurement.statusText = error.message || 'Failed to save measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        logSaveCurrentMeasurementDebug('finally before renderMeasurementPanel', { elapsedMs: debugElapsedMs() });
        state.measurement.saveInFlight = false;
        renderMeasurementPanel();
        logSaveCurrentMeasurementDebug('finally after renderMeasurementPanel', { elapsedMs: debugElapsedMs() });
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

async function mergeSelectedMeasurements() {
    if (state.measurement.saveInFlight || state.measurement.startInFlight) return;
    const measurements = getVisibleMeasurementEntries();
    if (measurements.length < 2) {
        showToast('Select at least two saved measurements to merge', 'warning');
        return;
    }
    const defaultName = `Merged ${measurements.length} measurements`;
    const requestedName = window.prompt('Name for merged measurement file:', defaultName);
    if (requestedName === null) return;
    const name = requestedName.trim() || defaultName;

    state.measurement.saveInFlight = true;
    state.measurement.statusText = `Merging ${measurements.length} saved measurements…`;
    renderMeasurementPanel();
    try {
        const resp = await fetch('/api/measurements/merge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                measurementIds: measurements.map(measurement => measurement.id),
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to merge selected measurements');
        const merged = normalizeMeasurementEntry(data.measurement || {}, 0);
        if (merged.id) {
            state.measurement.visibilityById[merged.id] = true;
            state.measurement.reviewVisibilityById[merged.id] = false;
        }
        state.measurement.statusText = 'Merged measurement saved.';
        await fetchMeasurements();
        if (merged.id) state.measurement.visibilityById[merged.id] = true;
        showToast(`Created merged measurement: ${merged.name || name}`, 'success');
    } catch (error) {
        console.error('mergeSelectedMeasurements failed', error);
        state.measurement.statusText = error.message || 'Failed to merge selected measurements';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.saveInFlight = false;
        renderMeasurementPanel();
    }
}

function renderMeasurementPanel() {
    if (!elements.measurementSummary || !elements.measurementList) return;
    const measurementState = state.measurement || {};
    measurementState.modeNote = measurementModeNoteText();
    const current = getCurrentMeasurementEntry();
    const measurements = (measurementState.measurements || []).filter(measurement => measurement.id !== current?.id);
    const graphEntries = getGraphMeasurementEntries();
    const assistMode = measurementState.assistMode === 'convolver' ? 'convolver' : 'peq';
    const peq = ensureMeasurementPeqState();
    const conv = ensureMeasurementConvolverState();
    const activePeqFilter = getMeasurementPeqActiveFilter();

    if (elements.measurementSetupCard) {
        elements.measurementSetupCard.classList.toggle('hidden', !measurementState.setupOpen);
    }
    if (elements.measurementSetupToggleBtn) {
        elements.measurementSetupToggleBtn.textContent = measurementState.setupOpen ? 'Close setup' : 'Setup';
        elements.measurementSetupToggleBtn.disabled = measurementState.startInFlight;
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
    if (elements.measurementInputGroup) {
        elements.measurementInputGroup.classList.remove('hidden');
    }
    if (elements.measurementInputSelect && !isSelectFocused(elements.measurementInputSelect)) {
        const inputs = measurementState.inputs && measurementState.inputs.length
            ? measurementState.inputs
            : [{ id: '', label: measurementState.inputsLoading ? 'Loading…' : 'No host capture inputs available' }];
        elements.measurementInputSelect.innerHTML = inputs.map(input => `<option value="${escapeHtml(input.id)}" ${input.id === measurementState.selectedInputId ? 'selected' : ''}>${escapeHtml(input.label)}</option>`).join('');
        elements.measurementInputSelect.disabled = measurementState.inputsLoading || !measurementState.hostCaptureAvailable;
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
    if (elements.measurementHouseCurveSelect) {
        const options = [{ id: '', filename: 'Built-in target curves only' }, ...(measurementState.houseCurveOptions || [])];
        const selectedHouseCurveId = String(conv.targetCurve || '').startsWith('house:') ? String(conv.targetCurve).slice(6) : '';
        elements.measurementHouseCurveSelect.innerHTML = options.map(option => `<option value="${escapeHtml(option.id || '')}" ${(option.id || '') === selectedHouseCurveId ? 'selected' : ''}>${escapeHtml(option.filename || 'House curve')}</option>`).join('');
        elements.measurementHouseCurveSelect.disabled = measurementState.houseCurveUpdating || measurementState.houseCurveDeleting;
    }
    if (elements.measurementHouseCurveDeleteBtn) {
        const canDeleteHouseCurve = String(conv.targetCurve || '').startsWith('house:') && !measurementState.houseCurveUpdating && !measurementState.houseCurveDeleting;
        elements.measurementHouseCurveDeleteBtn.disabled = !canDeleteHouseCurve;
        elements.measurementHouseCurveDeleteBtn.textContent = measurementState.houseCurveDeleting ? 'Deleting…' : 'Delete';
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
    if (elements.measurementNameInput) {
        elements.measurementNameInput.value = measurementState.currentMeasurementName || '';
        elements.measurementNameInput.disabled = !current || measurementState.startInFlight || measurementState.saveInFlight;
        elements.measurementNameInput.placeholder = current ? 'Name for save' : 'Available after capture';
    }
    if (elements.measurementStartBtn) {
        const activeJobRunning = !!measurementState.activeJobId;
        elements.measurementStartBtn.disabled = measurementState.calibrationUpdating || measurementState.calibrationDeleting
            ? true
            : (!activeJobRunning && (measurementState.inputsLoading || !measurementState.hostCaptureAvailable));
        elements.measurementStartBtn.textContent = activeJobRunning
            ? 'Cancel measurement'
            : (measurementState.startInFlight ? 'Starting…' : 'Start host-local sweep');
    }
    if (elements.measurementSaveBtn) {
        elements.measurementSaveBtn.disabled = !current || measurementState.saveInFlight || measurementState.startInFlight || measurementState.currentMeasurementSaved;
        elements.measurementSaveBtn.textContent = measurementState.saveInFlight ? 'Working…' : (measurementState.currentMeasurementSaved ? 'Saved' : 'Save current');
    }
    if (elements.measurementAssistMode) elements.measurementAssistMode.value = assistMode;
    if (elements.measurementTargetCurve) {
        elements.measurementTargetCurve.innerHTML = getMeasurementConvolverCurveOptions()
            .map((curve) => `<option value="${escapeHtml(curve.key)}" ${conv.targetCurve === curve.key ? 'selected' : ''}>${escapeHtml(curve.label || curve.shortLabel || curve.key)}</option>`)
            .join('');
        elements.measurementTargetCurve.value = conv.targetCurve;
        elements.measurementTargetCurve.classList.remove('hidden');
    }
    if (elements.measurementClearBtn) {
        const defaultConv = getDefaultMeasurementConvolverState();
        const hasConvolverResettableState = assistMode === 'convolver' && (
            conv.targetCurve !== defaultConv.targetCurve
            || Math.round(conv.rangeStartHz) !== defaultConv.rangeStartHz
            || Math.round(conv.rangeEndHz) !== defaultConv.rangeEndHz
            || Number(conv.maxBoostDb) !== defaultConv.maxBoostDb
            || Number(conv.maxCutDb) !== defaultConv.maxCutDb
            || String(conv.dipGuard) !== defaultConv.dipGuard
            || String(conv.sampleRate) !== defaultConv.sampleRate
            || String(conv.quality) !== defaultConv.quality
        );
        const hasResettableGraphState = !!current || !!peq.filters.length || hasConvolverResettableState;
        elements.measurementClearBtn.disabled = !hasResettableGraphState || measurementState.startInFlight || !!measurementState.activeJobId;
    }
    if (elements.measurementSetupStatus) {
        elements.measurementSetupStatus.textContent = measurementState.statusText || describeMeasurementScope();
    }
    if (elements.measurementSummary) {
        if (assistMode === 'convolver') {
            elements.measurementSummary.textContent = `${Math.round(conv.rangeStartHz)}–${Math.round(conv.rangeEndHz)} Hz`;
        } else {
            elements.measurementSummary.textContent = peq.filters.length ? `${peq.filters.length}/4 assistant filters` : '';
        }
    }
    if (elements.measurementEmpty) {
        elements.measurementEmpty.classList.toggle('hidden', graphEntries.length > 0);
    }
    if (elements.measurementGraphControls) {
        elements.measurementGraphControls.textContent = current
            ? (assistMode === 'convolver' ? 'Drag the blue range block or its edges to set the FIR correction range.' : 'Tap/click near 0 dB to add a filter, drag handles for freq/gain.')
            : 'Run a sweep to see the graph.';
    }
    if (elements.measurementPeqPanel) {
        elements.measurementPeqPanel.classList.toggle('hidden', assistMode !== 'peq' || (!peq.enabled && !peq.filters.length));
    }
    if (elements.measurementConvolverPanel) {
        elements.measurementConvolverPanel.classList.toggle('hidden', assistMode !== 'convolver');
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
    const peqDraftLeftCount = peq.draft?.leftBands?.length || 0;
    const peqDraftRightCount = peq.draft?.rightBands?.length || 0;
    if (elements.measurementPeqDraftSummary) {
        const draftMode = peqDraftLeftCount && peqDraftRightCount ? 'LR draft ready' : (peqDraftRightCount ? 'R draft ready' : (peqDraftLeftCount ? 'L draft ready' : 'no draft staged'));
        elements.measurementPeqDraftSummary.innerHTML = `<div>Draft — ${escapeHtml(draftMode)} · L: ${peqDraftLeftCount} bands · R: ${peqDraftRightCount} bands</div>`;
    }
    if (elements.measurementPeqPresetName) {
        const hasDraft = !!peqDraftLeftCount || !!peqDraftRightCount;
        if (document.activeElement !== elements.measurementPeqPresetName) {
            elements.measurementPeqPresetName.value = peq.draft?.presetName || '';
        }
        elements.measurementPeqPresetName.disabled = !hasDraft;
        elements.measurementPeqPresetName.placeholder = hasDraft ? 'Preset name' : 'Take L/R/Both to generate a name';
    }
    if (elements.measurementPeqTakeLeftBtn) elements.measurementPeqTakeLeftBtn.disabled = !peq.filters.length;
    if (elements.measurementPeqTakeRightBtn) elements.measurementPeqTakeRightBtn.disabled = !peq.filters.length;
    if (elements.measurementPeqTakeBothBtn) elements.measurementPeqTakeBothBtn.disabled = !peq.filters.length;
    if (elements.measurementPeqCreateBtn) elements.measurementPeqCreateBtn.disabled = (!peqDraftLeftCount && !peqDraftRightCount) || !String(peq.draft?.presetName || '').trim() || peqCreateInFlight;

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

    if (elements.measurementConvolverTarget) {
        const optionsHtml = getMeasurementConvolverCurveOptions().map((curve) => `<option value="${escapeHtml(curve.key)}" ${conv.targetCurve === curve.key ? 'selected' : ''}>${escapeHtml(curve.label || curve.shortLabel || curve.key)}</option>`).join('');
        if (elements.measurementConvolverTarget.innerHTML !== optionsHtml) elements.measurementConvolverTarget.innerHTML = optionsHtml;
        elements.measurementConvolverTarget.value = conv.targetCurve;
    }
    if (elements.measurementConvolverRangeStart && document.activeElement !== elements.measurementConvolverRangeStart) elements.measurementConvolverRangeStart.value = String(Math.round(conv.rangeStartHz));
    if (elements.measurementConvolverRangeEnd && document.activeElement !== elements.measurementConvolverRangeEnd) elements.measurementConvolverRangeEnd.value = String(Math.round(conv.rangeEndHz));
    if (elements.measurementConvolverMaxBoost) elements.measurementConvolverMaxBoost.value = String(conv.maxBoostDb);
    if (elements.measurementConvolverMaxCut) elements.measurementConvolverMaxCut.value = String(conv.maxCutDb);
    if (elements.measurementConvolverDipGuard) elements.measurementConvolverDipGuard.value = conv.dipGuard;
    if (elements.measurementConvolverSampleRate) elements.measurementConvolverSampleRate.value = conv.sampleRate;
    if (elements.measurementConvolverPhaseMode) elements.measurementConvolverPhaseMode.value = conv.phaseMode;
    if (elements.measurementConvolverIrLength) elements.measurementConvolverIrLength.value = String(conv.irLength);
    if (elements.measurementConvolverQuality) {
        const optionsHtml = measurementConvolverTypeOptions.map((option) => `<option value="${escapeHtml(option.key)}" ${conv.quality === option.key ? 'selected' : ''}>${escapeHtml(option.label)}</option>`).join('');
        if (elements.measurementConvolverQuality.innerHTML !== optionsHtml) elements.measurementConvolverQuality.innerHTML = optionsHtml;
        elements.measurementConvolverQuality.value = conv.quality;
    }
    const convAnalyses = ['left', 'right'].map((side) => analyzeMeasurementConvolverSide(side));
    const left = convAnalyses[0];
    const right = convAnalyses[1];
    const leftDraft = conv.draft?.left || null;
    const rightDraft = conv.draft?.right || null;
    if (elements.measurementConvolverSummary) {
        const curve = getMeasurementConvolverCurve(conv.targetCurve);
        const hasCreatedConvolver = (state.easyeffects?.assistStack || []).some((item) => item.type === 'convolver');
        let draftStatus;
        const draftDetails = [];
        const currentTimingDelta = left && right
            ? getMeasurementConvolverTimingDelta(
                getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('left')),
                getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('right')),
            )
            : null;
        const summaryTimingDelta = leftDraft && rightDraft
            ? getMeasurementConvolverTimingDelta(leftDraft.timing, rightDraft.timing)
            : currentTimingDelta;
        if (leftDraft && rightDraft) {
            const timingDelta = summaryTimingDelta;
            if (timingDelta) {
                draftStatus = `Draft ready · ${formatMeasurementConvolverTimingRelation(timingDelta)}`;
            } else {
                draftStatus = 'Draft ready · Timing unavailable';
            }
        } else if (leftDraft || rightDraft) {
            draftStatus = 'Draft ready · Timing unavailable';
        } else {
            draftStatus = hasCreatedConvolver ? 'Convolver preset created' : 'No draft staged';
            if (currentTimingDelta && conv.phaseMode === 'minimum_aligned') {
                draftStatus += ` · ${formatMeasurementConvolverTimingRelation(currentTimingDelta)}`;
            }
        }
        if (summaryTimingDelta && (leftDraft && rightDraft || (left && right && conv.phaseMode === 'minimum_aligned'))) {
            if (summaryTimingDelta.absMs > MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
                draftDetails.push('Filter not created because timing offset exceeds safety limit.');
            }
        }
        elements.measurementConvolverSummary.innerHTML = `
            <div><strong>${escapeHtml(curve.label)}</strong> · ${escapeHtml(getMeasurementConvolverTypeLabel(conv.quality))} · Max Boost +${conv.maxBoostDb} dB · Max Cut ${conv.maxCutDb} dB · Dip Guard ${escapeHtml(conv.dipGuard)}</div>
            <div>Range data — L: ${left ? `${left.points} pts, gain ${formatMeasurementConvolverGain(left.autoGainDb)}` : 'none'} · R: ${right ? `${right.points} pts, gain ${formatMeasurementConvolverGain(right.autoGainDb)}` : 'none'}</div>
            <div>${escapeHtml(draftStatus)}</div>
            ${draftDetails.map((detail) => `<div>${escapeHtml(detail)}</div>`).join('')}
        `;
    }
    if (elements.measurementConvolverPresetName) {
        const hasDraft = !!leftDraft || !!rightDraft;
        if (document.activeElement !== elements.measurementConvolverPresetName) {
            elements.measurementConvolverPresetName.value = conv.draft?.presetName || '';
        }
        elements.measurementConvolverPresetName.disabled = !hasDraft;
        elements.measurementConvolverPresetName.placeholder = hasDraft ? 'Preset name' : 'Take L/R/Both to generate a name';
    }
    if (elements.measurementConvolverWarnings) {
        const warnings = buildMeasurementConvolverWarnings(convAnalyses);
        elements.measurementConvolverWarnings.innerHTML = warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join('');
        elements.measurementConvolverWarnings.classList.toggle('hidden', !warnings.length);
    }
    if (elements.measurementConvolverTakeLeftBtn) elements.measurementConvolverTakeLeftBtn.disabled = !left;
    if (elements.measurementConvolverTakeRightBtn) elements.measurementConvolverTakeRightBtn.disabled = !right;
    if (elements.measurementConvolverTakeBothBtn) elements.measurementConvolverTakeBothBtn.disabled = !left || !right;
    if (elements.measurementConvolverCreateBtn) elements.measurementConvolverCreateBtn.disabled = (!leftDraft && !rightDraft) || !String(conv.draft?.presetName || '').trim() || convolverCreateInFlight;

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
                        <span class="measurement-list-title"><a href="${escapeHtml(measurementFileUrl(measurement.id))}" title="${escapeHtml(measurement.name)}">${escapeHtml(getCompactDisplayName(measurement.name, 24))}</a></span>
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
                            <button type="button" class="btn-danger measurement-saved-delete-action ${selectedSavedCount ? '' : 'is-inert'}" data-measurement-delete-selected ${selectedSavedCount ? '' : 'disabled'} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''} aria-hidden="${selectedSavedCount ? 'false' : 'true'}" aria-label="Delete selected measurements"><span class="label-full">Delete selected</span><span class="label-compact" aria-hidden="true">🗑</span></button>
                            <button type="button" class="btn-secondary measurement-saved-merge-action ${selectedSavedCount >= 2 ? '' : 'is-inert'}" data-measurement-merge-selected ${selectedSavedCount >= 2 ? '' : 'disabled'} ${measurementState.saveInFlight || measurementState.startInFlight ? 'disabled' : ''} aria-hidden="${selectedSavedCount >= 2 ? 'false' : 'true'}" aria-label="Merge selected measurements"><span class="label-full">Merge selected</span><span class="label-compact" aria-hidden="true">⇄</span></button>
                        </div>
                        <button type="button" class="btn-secondary measurement-saved-close-action" data-measurement-close-saved aria-label="Close saved measurements"><span class="label-full">Close</span><span class="label-compact">×</span></button>
                    </div>
                    ${savedItemsHtml}
                </div>
            </details>
        `
        : '';

    elements.measurementList.innerHTML = savedHtml;
    elements.measurementList.querySelectorAll('.measurement-saved-group').forEach((details) => {
        details.addEventListener('toggle', () => {
            state.measurement.savedGroupOpen = !!details.open;
            const summary = details.querySelector('summary');
            if (summary) {
                summary.textContent = `${state.measurement.savedGroupOpen ? 'Close saved' : 'Open saved'} (${measurements.length})`;
            }
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
    elements.measurementList.querySelectorAll('[data-measurement-merge-selected]').forEach((button) => {
        button.addEventListener('click', () => {
            mergeSelectedMeasurements();
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
    if (elements.measurementInputSelect) {
        const scanMeasurementInputsOnceForSelect = () => {
            if (measurementInputScanOnFocusDone) return;
            measurementInputScanOnFocusDone = true;
            void fetchMeasurementInputs();
        };
        elements.measurementInputSelect.addEventListener('pointerdown', scanMeasurementInputsOnceForSelect);
        elements.measurementInputSelect.addEventListener('focus', scanMeasurementInputsOnceForSelect);
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
    if (elements.measurementHouseCurveSelect) {
        elements.measurementHouseCurveSelect.addEventListener('change', (event) => {
            const houseCurveId = event.target.value || '';
            if (elements.measurementHouseCurveFile) elements.measurementHouseCurveFile.value = '';
            state.measurement.houseCurveFilename = '';
            updateMeasurementConvolverField('targetCurve', houseCurveId ? `house:${houseCurveId}` : 'neutral');
            renderMeasurementPanel();
            scheduleMeasurementGraphRender();
        });
    }
    if (elements.measurementHouseCurveFile) {
        elements.measurementHouseCurveFile.addEventListener('change', () => {
            const file = elements.measurementHouseCurveFile.files?.[0];
            if (file) {
                void uploadMeasurementHouseCurve(file);
            } else {
                state.measurement.houseCurveFilename = '';
                renderMeasurementPanel();
            }
        });
    }
    if (elements.measurementHouseCurveDeleteBtn) {
        elements.measurementHouseCurveDeleteBtn.addEventListener('click', () => { void deleteSelectedMeasurementHouseCurve(); });
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
    if (elements.measurementAssistMode) {
        elements.measurementAssistMode.addEventListener('change', (event) => setMeasurementAssistMode(event.target.value));
    }
    if (elements.measurementTargetCurve) {
        elements.measurementTargetCurve.addEventListener('change', (event) => updateMeasurementConvolverField('targetCurve', event.target.value));
    }
    [elements.measurementConvolverTarget, elements.measurementConvolverRangeStart, elements.measurementConvolverRangeEnd, elements.measurementConvolverMaxBoost, elements.measurementConvolverMaxCut, elements.measurementConvolverDipGuard, elements.measurementConvolverSampleRate, elements.measurementConvolverPhaseMode, elements.measurementConvolverIrLength, elements.measurementConvolverQuality].forEach((input) => {
        if (!input) return;
        const commit = () => updateMeasurementConvolverField(input.dataset.measurementConvolverField, input.value);
        input.addEventListener('change', commit);
        if (input instanceof HTMLInputElement) input.addEventListener('input', commit);
    });
    if (elements.measurementConvolverPresetName) {
        elements.measurementConvolverPresetName.addEventListener('input', (event) => {
            const conv = ensureMeasurementConvolverState();
            conv.draft.presetName = event.target.value || '';
            conv.draft.nameTouched = true;
            if (elements.measurementConvolverCreateBtn) {
                const hasDraft = !!conv.draft.left || !!conv.draft.right;
                elements.measurementConvolverCreateBtn.disabled = !hasDraft || !conv.draft.presetName.trim() || convolverCreateInFlight;
            }
        });
    }
    if (elements.measurementConvolverTakeLeftBtn) {
        elements.measurementConvolverTakeLeftBtn.addEventListener('click', () => takeMeasurementConvolverToDraft('left'));
    }
    if (elements.measurementConvolverTakeRightBtn) {
        elements.measurementConvolverTakeRightBtn.addEventListener('click', () => takeMeasurementConvolverToDraft('right'));
    }
    if (elements.measurementConvolverTakeBothBtn) {
        elements.measurementConvolverTakeBothBtn.addEventListener('click', () => takeMeasurementConvolverToDraft('both'));
    }
    if (elements.measurementConvolverCreateBtn) {
        elements.measurementConvolverCreateBtn.addEventListener('click', () => { void createMeasurementConvolverPresetFromDraft(); });
    }
    if (elements.measurementPeqPresetName) {
        elements.measurementPeqPresetName.addEventListener('input', (event) => {
            const peq = ensureMeasurementPeqState();
            peq.draft.presetName = event.target.value || '';
            peq.draft.nameTouched = true;
            if (elements.measurementPeqCreateBtn) {
                const hasDraft = !!peq.draft.leftBands?.length || !!peq.draft.rightBands?.length;
                elements.measurementPeqCreateBtn.disabled = !hasDraft || !peq.draft.presetName.trim() || peqCreateInFlight;
            }
        });
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
    if (elements.measurementPeqCreateBtn) {
        elements.measurementPeqCreateBtn.addEventListener('click', () => { void createMeasurementPeqPresetFromDraft(); });
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
            assistStack: Array.isArray(state.easyeffects?.assistStack) ? state.easyeffects.assistStack : [],
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
function removePeqBand(side, index) {
    if (!state.easyeffects?.peqDraft) return;
    const key = side === 'right' ? 'rightBands' : 'leftBands';
    const bands = state.easyeffects.peqDraft[key] || [];
    if (index < 0 || index >= bands.length) return;
    bands.splice(index, 1);
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
function getPeqLinkedSpecialType(leftBand = {}, rightBand = {}) {
    if (isPeqGainBand(leftBand) && isPeqGainBand(rightBand)) return 'gain';
    if (isPeqDelayBand(leftBand) && isPeqDelayBand(rightBand)) return 'delay';
    return '';
}
function getPeqBandPair(side, index) {
    if (!state.easyeffects?.peqDraft) return { sourceBand: null, otherBand: null };
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
    if (!state.easyeffects?.peqDraft) return;
    const leftBands = state.easyeffects.peqDraft.leftBands || [];
    const rightBands = state.easyeffects.peqDraft.rightBands || [];
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
    if (!state.easyeffects?.peqDraft) return;
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
        const otherBands = side === 'right' ? (state.easyeffects?.peqDraft?.leftBands || []) : (state.easyeffects?.peqDraft?.rightBands || []);
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
                    <input id="${fieldIdPrefix}-q" name="${fieldIdPrefix}-q" type="number" class="url-input" min="0.1" max="20" step="0.1" data-peq-side="${side}" data-peq-index="${index}" data-peq-field="q" value="${band.q}">
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
    if (!leftBands.length && !rightBands.length) {
        peqCreateInFlight = false;
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Please add at least one Left or Right band.</div>';
        showToast('Please add at least one Left or Right band', 'error');
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

function getCompactDisplayName(name = '', maxChars = 24) {
    const cleanName = String(name || '').trim();
    if (!cleanName || cleanName.length <= maxChars) return cleanName;
    return `${cleanName.slice(0, Math.max(1, maxChars)).trimEnd()}…`;
}

function renderPresetDownloadLink(presetName = '') {
    const cleanName = String(presetName || '').trim();
    if (!cleanName) return '—';
    const displayName = getCompactDisplayName(cleanName, 24);
    return `<a href="${escapeHtml(presetFileUrl(cleanName))}" title="${escapeHtml(cleanName)}">${escapeHtml(displayName)}</a>`;
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
    if (lowerName.endsWith('.zip')) return 'preset-bundle';
    return null;
}

function updateEffectsImportUi() {
    const file = elements.effectsImportFile?.files?.[0] || null;
    const detectedType = detectEffectsImportType(file);
    if (elements.effectsImportFile) {
        elements.effectsImportFile.accept = '.irs,.wav,.json,.zip,audio/wav,application/json,application/zip';
    }
    if (elements.effectsImportFilename) {
        if (!file) {
            elements.effectsImportFilename.textContent = 'Stereo convolver .irs/.wav, Preset .json, or Bundle .zip';
        } else if (detectedType === 'convolver' || detectedType === 'preset-json' || detectedType === 'preset-bundle') {
            elements.effectsImportFilename.textContent = file.name;
        } else {
            elements.effectsImportFilename.textContent = `Unsupported file: ${file.name}`;
        }
    }
    const importArea = document.getElementById('effects-import-area');
    if (importArea) {
        importArea.classList.toggle('is-ready', detectedType === 'convolver' || detectedType === 'preset-json' || detectedType === 'preset-bundle');
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
    if (detectedType === 'preset-bundle') {
        return importEffectsPresetBundle();
    }
    elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">Unsupported import file type. Use .irs, .wav, preset .json, or bundle .zip.</div>';
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
async function importEffectsPresetBundle() {
    if (effectsImportInFlight) {
        showToast('Import already in progress', 'warning');
        return;
    }
    effectsImportInFlight = true;
    const file = elements.effectsImportFile?.files?.[0] || null;
    if (!file) {
        effectsImportInFlight = false;
        showToast('Please select a preset bundle first', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('load_after_create', 'false');
    formData.append('file', file);
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Importing bundle: <strong>${escapeHtml(file.name)}</strong>…</div>`;
    const importArea = document.getElementById('effects-import-area');
    if (importArea) importArea.classList.add('is-busy');
    try {
        const resp = await fetch('/api/easyeffects/presets/import-bundle', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Preset bundle import failed');
        await fetchEffects();
        if (elements.effectsImportFile) elements.effectsImportFile.value = '';
        updateEffectsImportUi();
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
        const irCount = Array.isArray(data.irs) ? data.irs.length : 0;
        showToast(`Imported preset bundle: ${data.preset?.name || file.name}${irCount ? ` (${irCount} IR)` : ''}`, 'success');
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'Preset bundle import failed', 'error');
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
function trackCoverUrl(track) {
    if (!track || track.source !== 'local' || !track.id) return '';
    return `/api/tracks/cover/${encodeURIComponent(track.id)}`;
}
function trackCoverInfoUrl(track) {
    if (!track || track.source !== 'local' || !track.id) return '';
    return `/api/tracks/cover-info/${encodeURIComponent(track.id)}`;
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
    try {
        if (coverInfoUrl) {
            const infoResp = await fetch(coverInfoUrl, { signal: controller.signal, cache: 'force-cache' });
            if (!infoResp.ok) return;
            const info = await infoResp.json();
            if (!info.available) return;
        }
        const resp = await fetch(coverUrl, { signal: controller.signal, cache: 'force-cache' });
        if (!resp.ok) return;
        const blob = await resp.blob();
        objectUrl = URL.createObjectURL(blob);
        img.src = objectUrl;
        if (img.decode) await img.decode();
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
    cue.className = 'toast info now-playing-cue no-cover';
    const coverUrl = trackCoverUrl(track);
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
    revealNowPlayingCoverWhenReady(cue, img, coverUrl, trackCoverInfoUrl(track));
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
    if (elements.libraryViewAlbumsBtn) {
        elements.libraryViewAlbumsBtn.addEventListener('click', () => setLibraryViewMode('albums'));
    }
    if (elements.albumDetailBack) {
        elements.albumDetailBack.addEventListener('click', () => closeAlbumDetail());
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
        elements.toggleImportBtn.textContent = '− Close';
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
    if (elements.playSelectedTracksBtn) {
        // Legacy button removed from markup, keep null-safe no-op path only.
    }
    if (elements.selectAllTracksBtn) {
        elements.selectAllTracksBtn.addEventListener('click', toggleVisibleTrackSelection);
    }
    if (elements.albumFavoritesToggleBtn) {
        elements.albumFavoritesToggleBtn.addEventListener('click', toggleAlbumFavoritesFilter);
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
        return { available: false, installed: false, source: 'spotify', capabilities: {}, status: 'Stopped', artist: '', title: '', album: '', trackId: '', artUrl: '', shuffle: false, loop: 'none', position: 0, duration: 0 };
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

function setSpotifyUiVisibility(installed) {
    const available = installed === true;
    const visible = available && !nonAppSourceModeActive();
    const tabPanel = document.getElementById('tab-spotify');
    if (spotifyElements.tabBtn) {
        spotifyElements.tabBtn.hidden = !available;
        spotifyElements.tabBtn.style.display = available ? '' : 'none';
        spotifyElements.tabBtn.classList.toggle('hidden', !visible);
    }
    if (tabPanel) {
        tabPanel.hidden = !available;
        tabPanel.classList.toggle('hidden', !visible);
    }
    if (!visible && window.__visibleTab === 'spotify') {
        switchTab('radio');
    }
}

function handleIncomingSpotifyState(data, options = {}) {
    if (!data) return;
    const { renderTab = true, renderFooter = true } = options;
    setSpotifyUiVisibility(data.installed === true);
    if (data.installed !== true) {
        stopSpotifyPoll();
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
    setSpotifyUiVisibility(data.installed === true);
    if (data.installed !== true) {
        updateGlobalControlsForSource();
        return;
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
    const seekRow = document.querySelector('.seek-row');
    if (seekRow) seekRow.style.display = '';
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
