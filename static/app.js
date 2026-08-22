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
    buildMeasurementIrGraphEntry,
    drawMeasurementIrGraph,
    drawMeasurementTargetCurve,
    drawMeasurementConvolverRangeOverlay,
    drawMeasurementPeqOverlay,
    drawCustomHouseCurveHandles,
});
const MeasurementFlows = window.FXRouteMeasurementFlows || {};
// Flow module (auto-sub + hybrid wizard): backend calls via injected api,
// state/dom getters plus ui callbacks injected as hoisted references.
window.FXRouteMeasurementFlows?.init({
    api: {
        startAutoSubOptimize: (formData) => fetch('/api/measurements/auto-sub-optimize/start', { method: 'POST', body: formData }),
        cancelAutoSubJob: (jobId) => fetch(`/api/measurements/auto-sub-optimize/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
        pollAutoSubJob: (jobId) => fetch(`/api/measurements/auto-sub-optimize/jobs/${encodeURIComponent(jobId)}`),
        startMeasurement: (formData) => fetch('/api/measurements/start', { method: 'POST', body: formData }),
        cancelMeasurementJob: (jobId) => fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
        pollMeasurementJob: (jobId) => fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}`),
    },
    getState: () => state,
    getElements: () => elements,
    showToast,
    renderMeasurementPanel,
    renderMeasurementPanelDefensively,
    isSubwooferModeName,
    isSubwoofer22Mode,
    getActiveMeasurementKind,
    hasActiveMeasurementJob,
    measurementModeReady,
    normalizeMeasurementInputChannelSelections,
    getAutoSubTargetCurveSnapshot,
    flushSubwooferSettingsBeforeMeasurement,
    postRuntimeDebugSnapshot,
    formatTransitionErrorDetail,
    fetchAudioOutputOverview,
    getMeasurementReferenceWarning,
    normalizeOutputModeName,
    getMeasurementJobStatus,
    normalizeMeasurementEntry,
    getMeasurementJobResultMeasurement,
    setMeasurementAssistMode,
    escapeHtml,
    sleep,
});
const MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS = MeasurementUI.MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS;
const MEASUREMENT_JOB_CANCELLED_STATES = MeasurementUI.MEASUREMENT_JOB_CANCELLED_STATES;
const MEASUREMENT_JOB_FAILED_STATES = MeasurementUI.MEASUREMENT_JOB_FAILED_STATES;
const MEASUREMENT_JOB_SUCCESS_STATES = MeasurementUI.MEASUREMENT_JOB_SUCCESS_STATES;
const measurementComparePalette = MeasurementUI.measurementComparePalette;
const measurementCurrentColor = MeasurementUI.measurementCurrentColor;
const measurementPeqPalette = MeasurementUI.measurementPeqPalette;
const measurementPeqTypes = MeasurementUI.measurementPeqTypes;
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
        showFavoriteAlbums: false,
        albumDetail: null,
        playlistDetail: null,
    },
    playlists: [],
    stations: [],
    download: null,
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
        selectedChannel: 'left',
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
        activeMeasurementKind: '',
        autoSubJobId: '',
        autoSubProgress: null,
        autoSubInFlight: false,
        autoSubResult: null,
        autoSubMeasurements: [],
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
let effectsImportInFlight = false;
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
let effectsCompareLoadInFlight = false;
let settingsStatusPollTimer = null;
let settingsOutputScanOnFocusDone = false;
let measurementInputScanOnFocusDone = false;
let measurementSettingsRevision = 0;
let measurementGraphResizeObserver = null;
let playbackFooterResizeObserver = null;
let playbackFooterSpaceFrame = null;
let measurementGraphPointerId = null;
let measurementPeqTakeFeedbackTimer = null;
let measurementPeqLastTouchCreateAt = 0;
let measurementWindowHeartbeatTimer = null;
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
const LIBRARY_SCAN_POLL_INTERVAL_MS = 1200;
const DOWNLOAD_STATUS_POLL_INTERVAL_MS = 1500;
const PEAK_STATUS_POLL_INTERVAL_MS = 1200;
const EFFECTS_EXTRAS_TOGGLE_DEBOUNCE_MS = 800;
const EFFECTS_EXTRAS_VALUE_DEBOUNCE_MS = 2000;
const MEASUREMENT_WINDOW_HEARTBEAT_INTERVAL_MS = 10000;
// DOM elements
const elements = {
    offlineIndicator: document.getElementById('offline-indicator'),
    settingsOpenBtn: document.getElementById('open-settings'),
    settingsPanel: document.getElementById('settings-panel'),
    settingsCloseBtn: document.getElementById('close-settings'),
    settingsOutputSummary: document.getElementById('settings-output-summary'),
    settingsOutputSelect: document.getElementById('settings-output-select'),
    settingsOutputModeSelect: document.getElementById('settings-output-mode-select'),
    settingsOutputModeHint: document.getElementById('settings-output-mode-hint'),
    settingsSamplerateSelect: document.getElementById('settings-samplerate-select'),
    settingsSamplerateHint: document.getElementById('settings-samplerate-hint'),
    settingsSourceSelect: document.getElementById('settings-source-select'),
    settingsSourceModeHint: document.getElementById('settings-source-mode-hint'),
    settingsBluetoothStatus: document.getElementById('settings-bluetooth-status'),
    settingsMusicLibrarySelect: document.getElementById('settings-music-library-select'),
    settingsHardwareSummary: document.getElementById('settings-hardware-summary'),
    settingsHardwareDetail: document.getElementById('settings-hardware-detail'),
    settingsHardwareRcaBtn: document.getElementById('settings-hardware-rca'),
    settingsHardwareXlrBtn: document.getElementById('settings-hardware-xlr'),
    settingsHardwarePressBtn: document.getElementById('settings-hardware-press'),
    settingsHardwareAutoOnBtn: document.getElementById('settings-hardware-auto-on'),
    settingsHardwareAutoOffBtn: document.getElementById('settings-hardware-auto-off'),
    settingsCertificateLink: document.getElementById('settings-certificate-link'),
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
    playlistDetail: document.getElementById('playlist-detail'),
    playlistDetailBack: document.getElementById('playlist-detail-back'),
    playlistDetailCover: document.getElementById('playlist-detail-cover'),
    playlistDetailName: document.getElementById('playlist-detail-name'),
    playlistDetailCount: document.getElementById('playlist-detail-count'),
    playlistDetailTracks: document.getElementById('playlist-detail-tracks'),
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
    measurementSetupToggleBtn: document.getElementById('measurement-setup-toggle'),
    measurementModeNote: document.getElementById('measurement-mode-note'),
    measurementInputGroup: document.getElementById('measurement-input-group'),
    measurementInputSelect: document.getElementById('measurement-input-select'),
    measurementInputRefreshBtn: document.getElementById('measurement-input-refresh'),
    measurementMicInputChannelSelect: document.getElementById('measurement-mic-input-channel-select'),
    measurementReferenceInputChannelSelect: document.getElementById('measurement-reference-input-channel-select'),
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
    measurementStartBtn: document.getElementById('measurement-start'),
    measurementRepeatStartBtn: document.getElementById('measurement-repeat-start'),
    measurementAutoSubStartBtn: document.getElementById('measurement-auto-sub-start'),
    measurementAutoSubGroup: document.getElementById('measurement-auto-sub-group'),
    measurementAutoSubStatus: document.getElementById('measurement-auto-sub-status'),
    measurementHybridOpenBtn: document.getElementById('measurement-hybrid-open'),
    measurementHybridPanel: document.getElementById('measurement-hybrid-panel'),
    measurementHybridCloseBtn: document.getElementById('measurement-hybrid-close'),
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
    effectsSubwooferFrequencyNumber: document.getElementById('effects-subwoofer-frequency-number'),
    effectsSubwooferMainHighpass: document.getElementById('effects-subwoofer-main-highpass'),
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

function createFxrouteModalManager() {
    const stack = [];
    const managedInertElements = new Set();

    function focusElement(element) {
        if (!element || typeof element.focus !== 'function') return false;
        try {
            element.focus({ preventScroll: true });
        } catch (error) {
            element.focus();
        }
        return document.activeElement === element;
    }

    function isVisible(element) {
        const closedDetails = element.closest('details:not([open])');
        if (closedDetails && element.tagName !== 'SUMMARY') return false;
        return !element.hidden
            && element.getAttribute('aria-hidden') !== 'true'
            && (element.offsetParent !== null || element === document.activeElement);
    }

    function getFocusableElements(root) {
        if (!root) return [];
        const selector = [
            'a[href]',
            'area[href]',
            'button:not([disabled])',
            'input:not([disabled]):not([type="hidden"])',
            'select:not([disabled])',
            'textarea:not([disabled])',
            'summary',
            '[tabindex]:not([tabindex="-1"])',
        ].join(',');
        return Array.from(root.querySelectorAll(selector)).filter(isVisible);
    }

    function clearManagedInert() {
        managedInertElements.forEach(element => {
            element.inert = false;
        });
        managedInertElements.clear();
    }

    function containsProtectedRoot(element, protectedRoots) {
        return protectedRoots.some(root => element === root || element.contains(root));
    }

    function applyBackgroundInert(entry) {
        clearManagedInert();
        if (!entry) return;

        const protectedRoots = [entry.root, ...(entry.siblingRoots || [])]
            .filter(Boolean);
        protectedRoots.forEach(root => {
            let node = root;
            while (node && node.parentElement) {
                const parent = node.parentElement;
                Array.from(parent.children).forEach(sibling => {
                    if (sibling === node || containsProtectedRoot(sibling, protectedRoots)) return;
                    if (!(sibling instanceof HTMLElement) || sibling.inert) return;
                    sibling.inert = true;
                    managedInertElements.add(sibling);
                });
                node = parent;
            }
        });
    }

    function focusInitial(entry) {
        const focusables = getFocusableElements(entry.root);
        if (entry.initialFocus && isVisible(entry.initialFocus) && focusElement(entry.initialFocus)) return;
        if (focusElement(focusables[0])) return;
        focusElement(entry.dialog || entry.root);
    }

    function open(root, options = {}) {
        if (!root) return;
        const existingIndex = stack.findIndex(entry => entry.root === root);
        if (existingIndex >= 0) {
            const existing = stack.splice(existingIndex, 1)[0];
            stack.push(existing);
            applyBackgroundInert(existing);
            focusInitial(existing);
            return;
        }

        const dialog = options.dialog
            || (root.matches?.('[role="dialog"]') ? root : root.querySelector?.('[role="dialog"]'))
            || root;
        const opener = options.opener
            || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
        const entry = {
            root,
            dialog,
            opener,
            initialFocus: options.initialFocus || null,
            siblingRoots: options.siblingRoots || [],
            onEscape: options.onEscape,
        };
        stack.push(entry);
        applyBackgroundInert(entry);
        focusInitial(entry);
    }

    function close(root) {
        const index = stack.findIndex(entry => entry.root === root);
        if (index < 0) return;
        const wasTop = index === stack.length - 1;
        const [entry] = stack.splice(index, 1);
        applyBackgroundInert(stack[stack.length - 1]);
        if (!wasTop) return;

        const replacement = stack[stack.length - 1];
        if (replacement) {
            if (replacement.root.contains(entry.opener)) {
                focusElement(entry.opener);
            } else {
                focusInitial(replacement);
            }
            return;
        }
        focusElement(entry.opener);
    }

    function isOpen(root) {
        return stack.some(entry => entry.root === root);
    }

    document.addEventListener('keydown', event => {
        const entry = stack[stack.length - 1];
        if (!entry) return;

        if (event.key === 'Escape' && typeof entry.onEscape === 'function') {
            event.preventDefault();
            event.stopImmediatePropagation();
            void entry.onEscape(event);
            return;
        }
        if (event.key !== 'Tab') return;

        const focusables = getFocusableElements(entry.root);
        if (!focusables.length) {
            event.preventDefault();
            focusElement(entry.dialog || entry.root);
            return;
        }

        const active = document.activeElement;
        if (!entry.root.contains(active)) {
            event.preventDefault();
            focusElement(event.shiftKey ? focusables[focusables.length - 1] : focusables[0]);
            return;
        }

        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && active === first) {
            event.preventDefault();
            focusElement(last);
        } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            focusElement(first);
        }
    }, true);

    return { open, close, isOpen };
}

window.FXRouteModal = createFxrouteModalManager();

/* Shared compact content-state renderer (Library, Radio, TIDAL browse).
   One vocabulary for Loading / Empty / Error text slots so content areas
   never fall back to ad-hoc bare text. Exposed globally because radio.js
   and streaming.js load before app.js but only call it at runtime, after
   this module has defined it. */
function setContentState(el, state, message) {
    if (!el) return;
    if (state === 'none' || state === 'hidden') {
        el.classList.add('hidden');
        el.textContent = '';
        return;
    }
    el.classList.remove('hidden');
    el.hidden = false;
    el.style.display = '';
    el.className = 'content-state content-state--' + state;
    el.textContent = message || '';
}
window.FXRouteContentState = {
    set: setContentState,
    loading(el, msg) { setContentState(el, 'loading', msg); },
    empty(el, msg) { setContentState(el, 'empty', msg); },
    error(el, msg) { setContentState(el, 'error', msg); },
    hide(el) { setContentState(el, 'none', ''); },
};

// Initialization
document.addEventListener('DOMContentLoaded', () => {
    try { updatePowerButtonConnectionState(); } catch(e) { console.error('updatePowerButtonConnectionState crashed:', e); }
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
            highlightActiveTrack,
            extractDroppedUrl,
        });
    } catch(e) { console.error('radio module initialization crashed:', e); }
    try {
        window.FXRouteStreaming.init({
            showToast,
            escapeHtml,
            formatTime,
            trackRowHtml: detailTrackRowHtml,
            factsHtml: detailFactsHtml,
            aboutHtml: detailAboutHtml,
            spotifyCommand,
            spotifySeek,
        });
    } catch(e) { console.error('streaming module initialization crashed:', e); }
    try { setupLibraryActions(); } catch(e) { console.error('setupLibraryActions crashed:', e); }
    try { setupDownloadActions(); } catch(e) { console.error('setupDownloadActions crashed:', e); }
    try { setupEffectsActions(); } catch(e) { console.error('setupEffectsActions crashed:', e); }
    try { setupMeasurementActions(); } catch(e) { console.error('setupMeasurementActions crashed:', e); }
    try { setupHybridMeasurementWizard(); } catch(e) { console.error('setupHybridMeasurementWizard crashed:', e); }
    try { primeSubwooferPreview(); } catch(e) { console.error('primeSubwooferPreview crashed:', e); }
    try { window.addEventListener('load', requestSubwooferPreviewRedrawFromState, { once: true }); } catch(e) { console.error('subwoofer preview load hook crashed:', e); }
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
        await apiPostJson('/api/dsp/compare', compare);
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
        if (offlineIndicatorTimer) {
            clearTimeout(offlineIndicatorTimer);
            offlineIndicatorTimer = null;
        }
        updatePowerButtonConnectionState();
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
        updatePowerButtonConnectionState();
        elements.offlineIndicator.classList.remove('hidden');
    }, CONFIG.offlineIndicatorDelay);
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
                renderLibraryView();
            }
            if (data.stations) {
                radioModule.setStations(data.stations);
            }
            if (data.spotify) {
                handleIncomingSpotifyState(data.spotify, { renderTab: true, renderFooter: true });
            }
            if (data.player && data.player.state && data.player.state.dsp) {
                state.dsp = data.player.state.dsp;
                if (state.dsp?.global_extras) {
                    applyEffectsExtras({
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
            window.__qobuzLastData = data || null;
            if (data && data.available && (data.status === 'Playing' || data.status === 'Paused' || data.title)) {
                reconcileFooterSource();
                if (window.__footerSource === 'qobuz') {
                    updateFooterForStreamingOwner(data);
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
                combineDraft: state.dsp?.combineDraft || getDefaultEffectsCombineDraft(),
                peqDraft: state.dsp?.peqDraft || { presetName: '', eqMode: 'IIR', loadAfterCreate: false, leftBands: [defaultPeqBand()], rightBands: [defaultPeqBand()] },
                compare: resolveEffectsCompareState(data.compare || prev, presetNames, data.active_preset || ''),
            };
            state.dsp.combineDraft = normalizeEffectsCombineDraft(state.dsp.combineDraft, presetNames);
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
            if (_audioOutputModeSwitchInProgress) {
                event.target.value = state.settings.audioOutputs.output_mode?.mode || 'stereo';
                return;
            }
            void switchAudioOutputMode(event.target.value || 'stereo');
        });
    }
    if (elements.settingsSamplerateSelect) {
        elements.settingsSamplerateSelect.addEventListener('change', (event) => {
            void saveSampleRatePolicy(event.target.value || 'auto');
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
    if (elements.settingsMusicLibrarySelect) {
        elements.settingsMusicLibrarySelect.addEventListener('change', (event) => {
            const libraryId = event.target.value;
            if (libraryId === 'manual') {
                const url = prompt('SMB share URL', 'smb://server/share');
                if (url) void addManualMusicLibrary(url);
                else renderSettingsPanel();
            } else if (libraryId) {
                void selectMusicLibrary(libraryId);
            }
        });
    }
    elements.settingsHardwareRcaBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/input/rca', 'RCA selected'));
    elements.settingsHardwareXlrBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/input/xlr', 'XLR selected'));
    elements.settingsHardwarePressBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/input/press', 'Input button pressed'));
    elements.settingsHardwareAutoOnBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/auto/on', 'Auto mode enabled'));
    elements.settingsHardwareAutoOffBtn?.addEventListener('click', () => runHardwareCommand('/api/hardware/auto/off', 'Auto mode disabled'));
    elements.settingsUpdateCheckBtn?.addEventListener('click', () => checkFxrouteUpdate());
    elements.settingsUpdateRunBtn?.addEventListener('click', () => runFxrouteUpdate());
    elements.settingsRestoreRunBtn?.addEventListener('click', () => restoreFxrouteToPublic());
    elements.settingsUpdateDetailsToggle?.addEventListener('click', () => {
        const maintenance = state.settings.maintenance;
        const isOpen = !!maintenance.detailsExpanded
            || (!!maintenance.pending && maintenance.operation === 'update')
            || !!maintenance.restartPending
            || (!!maintenance.hasError && !maintenance.userCollapsedDetails);
        maintenance.detailsExpanded = !isOpen;
        maintenance.userCollapsedDetails = isOpen;
        renderMaintenancePanel();
    });
    const backdrop = elements.settingsPanel.querySelector('.manage-overlay-backdrop');
    if (backdrop) backdrop.addEventListener('click', () => toggleSettingsPanel(false));
    renderSettingsPanel();
}

function normalizeSubwooferSettings(input = {}) {
    const frequency = Math.max(40, Math.min(200, Math.round(Number(input.crossover_frequency_hz ?? input.crossoverFrequencyHz ?? 80) || 80)));
    const level = Math.max(-24, Math.min(12, Number(input.sub_level_db ?? input.subLevelDb ?? 0) || 0));
    const alignment = Math.max(-40, Math.min(40, Number(input.sub_alignment_ms ?? input.subAlignmentMs ?? 0) || 0));
    const polarity = String(input.sub_polarity ?? input.subPolarity ?? 'normal').toLowerCase() === 'invert' ? 'invert' : 'normal';
    const roundedAlignment = Math.round(alignment * 100) / 100;
    return {
        crossover_frequency_hz: frequency,
        slope: 'LR24',
        main_highpass_enabled: input.main_highpass_enabled ?? input.mainHighpassEnabled ?? true ? true : false,
        sub_level_db: Math.round(level * 10) / 10,
        sub_alignment_ms: roundedAlignment,
        sub_polarity: polarity,
    };
}

function normalizeSingleSubwooferSettings(input = {}) {
    const level = Math.max(-24, Math.min(12, Number(input.level_db ?? input.levelDb ?? 0) || 0));
    const alignment = Math.max(-40, Math.min(40, Number(input.alignment_ms ?? input.alignmentMs ?? 0) || 0));
    const polarity = String(input.polarity ?? 'normal').toLowerCase() === 'invert' ? 'invert' : 'normal';
    return {
        level_db: Math.round(level * 10) / 10,
        alignment_ms: Math.round(alignment * 100) / 100,
        polarity,
    };
}

function subwoofer21ToSub22Sub(subwoofer = {}) {
    const normalized = normalizeSubwooferSettings(subwoofer || {});
    return normalizeSingleSubwooferSettings({
        level_db: normalized.sub_level_db,
        alignment_ms: normalized.sub_alignment_ms,
        polarity: normalized.sub_polarity,
    });
}

function getSubwooferGlobalSettings(outputMode = {}, fallback = {}) {
    return normalizeSubwooferSettings({
        ...(fallback || {}),
        ...(outputMode.subwoofer || {}),
        crossover_frequency_hz: outputMode.crossover_frequency_hz ?? outputMode.subwoofer?.crossover_frequency_hz ?? fallback?.crossover_frequency_hz,
        main_highpass_enabled: outputMode.main_highpass_enabled ?? outputMode.subwoofer?.main_highpass_enabled ?? fallback?.main_highpass_enabled,
        sub_level_db: outputMode.subwoofers?.sub1?.level_db ?? outputMode.subwoofer?.sub_level_db ?? fallback?.sub_level_db,
        sub_alignment_ms: outputMode.subwoofers?.sub1?.alignment_ms ?? outputMode.subwoofer?.sub_alignment_ms ?? fallback?.sub_alignment_ms,
        sub_polarity: outputMode.subwoofers?.sub1?.polarity ?? outputMode.subwoofer?.sub_polarity ?? fallback?.sub_polarity,
    });
}

function normalizeSubwoofersSettings(subwoofers = {}, fallbackSubwoofer = {}) {
    const fallbackSub = subwoofer21ToSub22Sub(fallbackSubwoofer);
    return {
        sub1: normalizeSingleSubwooferSettings(subwoofers?.sub1 || fallbackSub),
        sub2: normalizeSingleSubwooferSettings(subwoofers?.sub2 || {}),
    };
}

function collectSubwooferSettings() {
    return normalizeSubwooferSettings({
        crossover_frequency_hz: elements.effectsSubwooferFrequencyNumber?.value || 80,
        main_highpass_enabled: (elements.effectsSubwooferMainHighpass?.value || 'on') !== 'off',
        sub_level_db: elements.effectsSubwooferLevel?.value || 0,
        sub_alignment_ms: elements.effectsSubwooferDelay?.value || 0,
        sub_polarity: elements.effectsSubwooferPolarity?.value || 'normal',
    });
}

function collectSubwoofer22Settings() {
    const subwoofer = collectSubwooferSettings();
    return {
        subwoofer,
        subwoofers: {
            sub1: subwoofer21ToSub22Sub(subwoofer),
            sub2: normalizeSingleSubwooferSettings({
                level_db: elements.effectsSubwooferSub2Level?.value || 0,
                alignment_ms: elements.effectsSubwooferSub2Delay?.value || 0,
                polarity: elements.effectsSubwooferSub2Polarity?.value || 'normal',
            }),
        },
    };
}

function isSubwoofer22Mode(mode) {
    return ['subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
}

function isSubwooferModeName(mode) {
    return ['subwoofer-2.1', 'subwoofer-2.2', 'subwoofer-2.2-stereo'].includes(mode);
}

function normalizeOutputModeName(mode) {
    return isSubwooferModeName(mode) ? mode : 'stereo';
}

function formatSampleRateKhz(rate) {
    const numeric = Number(rate);
    if (!Number.isFinite(numeric) || numeric <= 0) return 'Auto';
    return `${(numeric / 1000).toFixed(1).replace(/\.0$/, '')} kHz`;
}

function formatTransitionErrorDetail(detail, fallback = 'Request failed') {
    if (typeof detail === 'string' && detail.trim()) return detail;
    if (detail && typeof detail === 'object') {
        const message = typeof detail.message === 'string' ? detail.message.trim() : '';
        if (message) {
            const stage = typeof detail.stage === 'string' ? detail.stage.trim() : '';
            if (stage && !message.toLowerCase().includes(stage.toLowerCase())) {
                return `${message} (stage: ${stage})`;
            }
            return message;
        }
    }
    return fallback;
}

function formatRadioStreamLine(streamInfo, effectiveOutputRate = null) {
    if (!streamInfo || typeof streamInfo !== 'object') streamInfo = {};
    const parts = [];
    const codec = streamInfo.codec ? String(streamInfo.codec) : '';
    // Lossless codecs: the decoded bitrate is content-dependent and the
    // `Lossless` profile label is redundant, so neither is shown for them —
    // the meaningful facts are bit depth and sample rate (FLAC · 24 bit ·
    // 44.1 kHz). Lossy codecs keep the profile/bitrate line (AAC · 320 kbps
    // · 44.1 kHz).
    const lossless = !!codec && ['FLAC', 'ALAC', 'APE', 'WAVPACK', 'TTA', 'PCM'].includes(codec.toUpperCase());
    if (codec) parts.push(codec);
    if (!lossless) {
        if (streamInfo.profile) {
            parts.push(String(streamInfo.profile));
        } else if (Number.isFinite(Number(streamInfo.bitrate_kbps)) && Number(streamInfo.bitrate_kbps) > 0) {
            parts.push(`${Math.round(Number(streamInfo.bitrate_kbps))} kbps`);
        }
    }
    if (Number.isFinite(Number(streamInfo.bit_depth)) && Number(streamInfo.bit_depth) > 0) {
        parts.push(`${Math.round(Number(streamInfo.bit_depth))} bit`);
    }
    const displayedRate = Number(effectiveOutputRate) || Number(streamInfo.samplerate_hz);
    if (Number.isFinite(displayedRate) && displayedRate > 0) {
        parts.push(`${(displayedRate / 1000).toFixed(1).replace(/\.0$/, '')} kHz`);
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

function buildAudioOutputModeRequest(mode, settings = null, options = {}) {
    const nextMode = normalizeOutputModeName(mode);
    if (options.modeOnly) {
        return { mode: nextMode };
    }
    const outputMode = state.settings.audioOutputs.output_mode || {};
    const fallbackSubwoofer = getSubwooferGlobalSettings(outputMode, outputMode.subwoofer || {});
    if (isSubwoofer22Mode(nextMode)) {
        const source = settings && typeof settings === 'object' ? settings : {};
        const switchingTo22 = !isSubwoofer22Mode(outputMode.mode) && !settings;
        const sourceHasSettings = Object.keys(source).length > 0;
        const subwooferSource = source.subwoofer || (sourceHasSettings ? source : getSubwooferGlobalSettings(outputMode, fallbackSubwoofer));
        const subwoofer = normalizeSubwooferSettings(subwooferSource);
        const subwoofers = normalizeSubwoofersSettings(source.subwoofers || (switchingTo22 ? {} : outputMode.subwoofers) || {}, subwoofer);
        return {
            mode: nextMode,
            subwoofer,
            subwoofers,
        };
    }
    return {
        mode: nextMode,
        subwoofer: normalizeSubwooferSettings(settings?.subwoofer || settings || fallbackSubwoofer),
    };
}

let _audioOutputModeRequestId = 0;
let _audioOutputModeMutationGeneration = 0;
let _audioOutputModeSwitchInProgress = false;
function getAudioOutputModeSignature(mode, settings = null, options = {}) {
    return JSON.stringify(buildAudioOutputModeRequest(mode, settings, options));
}

async function saveAudioOutputMode(mode, settings = null, options = {}) {
    const modeOnly = !!options.modeOnly;
    const suppressApply = !!options.suppressApply;
    const propagateError = !!options.propagateError;
    const requestBody = buildAudioOutputModeRequest(mode, settings, options);
    const nextMode = requestBody.mode;
    const previousMode = state.settings.audioOutputs.output_mode?.mode || 'stereo';
    const requestSignature = JSON.stringify(requestBody);
    const requestId = ++_audioOutputModeRequestId;
    const mutationGeneration = ++_audioOutputModeMutationGeneration;
    if (!modeOnly && !suppressApply) {
        state.settings.audioOutputs.output_mode = {
            ...(state.settings.audioOutputs.output_mode || {}),
            mode: nextMode,
            ...('subwoofer' in requestBody ? { subwoofer: requestBody.subwoofer } : {}),
            ...('subwoofers' in requestBody ? { subwoofers: requestBody.subwoofers } : {}),
        };
        renderSettingsPanel();
    }
    try {
        const resp = await fetch('/api/audio/output-mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to save output mode'));
        if (
            requestId !== _audioOutputModeRequestId
            || mutationGeneration !== _audioOutputModeMutationGeneration
            || (!modeOnly && requestSignature !== getAudioOutputModeSignature(
                state.settings.audioOutputs.output_mode?.mode,
                state.settings.audioOutputs.output_mode,
            ))
        ) {
            return null;
        }
        if (suppressApply) return data;
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
        setSubwooferFeedback('Saved', 'success');
        void postRuntimeDebugSnapshot(`ui-output-mode-saved-${nextMode}`, { requestedMode: nextMode });
        syncAutoSubButton();
        return data;
    } catch (error) {
        if (requestId !== _audioOutputModeRequestId || mutationGeneration !== _audioOutputModeMutationGeneration) return null;
        if (!modeOnly && requestSignature !== getAudioOutputModeSignature(
            state.settings.audioOutputs.output_mode?.mode,
            state.settings.audioOutputs.output_mode,
        )) {
            return null;
        }
        _subwooferLastRequestedSignature = '';
        if (!suppressApply) {
            state.settings.audioOutputs.output_mode = {
                ...(state.settings.audioOutputs.output_mode || {}),
                mode: previousMode,
            };
        }
        renderSettingsPanel();
        setSubwooferFeedback('Failed', 'error');
        if (propagateError) throw error;
        showToast(error.message || 'Failed to save output mode', 'error');
        return null;
    }
}

async function switchAudioOutputMode(mode) {
    const nextMode = normalizeOutputModeName(mode);
    const currentMode = state.settings.audioOutputs.output_mode?.mode || 'stereo';
    if (_audioOutputModeSwitchInProgress) {
        if (elements.settingsOutputModeSelect) elements.settingsOutputModeSelect.value = currentMode;
        return;
    }
    if (nextMode === currentMode) return;
    _audioOutputModeSwitchInProgress = true;
    cancelPendingSubwooferSave();
    _subwooferLastRequestedSignature = '';

    // Lock UI immediately — disable select and show "Switching…" label
    if (elements.settingsOutputModeSelect) {
        elements.settingsOutputModeSelect.disabled = true;
    }
    if (elements.settingsOutputModeHint) {
        elements.settingsOutputModeHint.textContent = 'Switching…';
        elements.settingsOutputModeHint.classList.add('switching');
    }

    try {
        if (isSubwooferModeName(currentMode)) {
            const currentSettings = isSubwoofer22Mode(currentMode)
                ? collectSubwoofer22Settings()
                : collectSubwooferSettings();
            await saveAudioOutputMode(currentMode, currentSettings, { suppressApply: true });
        }
        clearSubwooferActiveEditing();
        setSubwooferFeedback('Applying…');
        await saveAudioOutputMode(nextMode, null, { modeOnly: true });
    } finally {
        _audioOutputModeSwitchInProgress = false;
        if (elements.settingsOutputModeSelect) {
            elements.settingsOutputModeSelect.disabled = false;
        }
        if (elements.settingsOutputModeHint) {
            elements.settingsOutputModeHint.textContent = '';
            elements.settingsOutputModeHint.classList.remove('switching');
        }
        renderSettingsPanel();
    }
}

/**
 * Await only a real pending/running debounced subwoofer save before a
 * measurement. A committed mode must not be posted again just because the
 * measurement panel was opened.
 */
async function flushSubwooferSettingsBeforeMeasurement() {
    const outputMode = state.settings.audioOutputs?.output_mode || {};
    if (!isSubwooferModeName(outputMode.mode)) return;

    let pendingPromise = null;
    if (_subwooferPendingSave) {
        pendingPromise = _subwooferPendingSave.start();
    }
    if (pendingPromise) {
        await pendingPromise;
    } else if (_subwooferSavePromise) {
        await _subwooferSavePromise;
    }
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
        void Promise.all([fetchAudioOutputOverview(), fetchAudioSourceOverview(), fetchHardwareStatus(), fetchMusicLibraries(), checkFxrouteUpdate({ silent: true })]);
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

function updateLogFromResult(data = {}) {
    const parts = [];
    if (data.stdout) parts.push(String(data.stdout).trim());
    if (data.stderr) parts.push(String(data.stderr).trim());
    return parts.filter(Boolean).join('\n\n');
}

function parseUpdateSummary(logText = '') {
    const currentMatch = logText.match(/Current:\s*([^\n]+)/);
    const remoteMatch = logText.match(/Remote:\s*([^\n]+)/);
    const current = currentMatch ? currentMatch[1].trim() : '';
    const remote = remoteMatch ? remoteMatch[1].trim() : '';
    if (current && remote) return `Current ${current} · Latest ${remote}`;
    return current ? `Current ${current}` : 'Version status checked.';
}

function parseUpdateVersion(value = '') {
    return String(value || '').replace(/\s*\([^)]*\).*$/, '').trim();
}

function parseUpdateInfo(logText = '') {
    const currentMatch = logText.match(/Current:\s*([^\n]+)/);
    const remoteMatch = logText.match(/Remote:\s*([^\n]+)/);
    const completedMatch = logText.match(/Update completed:\s*([^\n]+)/);
    const completedVersion = parseUpdateVersion(completedMatch ? completedMatch[1] : '');
    const currentVersion = completedVersion || parseUpdateVersion(currentMatch ? currentMatch[1] : '');
    const latestVersion = parseUpdateVersion(remoteMatch ? remoteMatch[1] : '');
    const updateAvailable = logText.includes('Update available.')
        ? true
        : logText.includes('FXRoute is already up to date.')
            || logText.includes('Update completed:')
            ? false
            : null;
    return { currentVersion, latestVersion, updateAvailable };
}

function maintenanceStatusText(maintenance = {}) {
    if (maintenance.dirtyBlock) return 'Local source changes. Update disabled';
    if (maintenance.hasError) return 'Update check failed';
    if (maintenance.restartPending) return 'Restarting FXRoute';
    if (maintenance.pending) return maintenance.updateAvailable === true ? 'Updating FXRoute' : 'Checking for updates';
    if (maintenance.updateAvailable === true) return 'Update available';
    if (maintenance.updateAvailable === false) return 'FXRoute is up to date';
    return maintenance.latestSummary || 'Version status not checked yet.';
}

function renderMaintenancePanel() {
    const maintenance = state.settings?.maintenance || {};
    const currentVersion = maintenance.currentVersion || maintenance.installedVersion || '';
    const latestVersion = maintenance.latestVersion || '';
    const showLatest = !!latestVersion && latestVersion !== currentVersion;
    const showDetails = !!maintenance.detailsExpanded
        || (!!maintenance.pending && maintenance.operation === 'update')
        || !!maintenance.restartPending
        || (!!maintenance.hasError && !maintenance.userCollapsedDetails)
        || !!maintenance.dirtyBlock;
    if (elements.settingsMaintenanceStatus) {
        elements.settingsMaintenanceStatus.textContent = maintenanceStatusText(maintenance);
    }
    if (elements.settingsMaintenanceCurrent) {
        elements.settingsMaintenanceCurrent.textContent = currentVersion || 'Unknown';
    }
    if (elements.settingsMaintenanceLatestRow) {
        elements.settingsMaintenanceLatestRow.classList.toggle('hidden', !showLatest);
    }
    if (elements.settingsMaintenanceLatest) {
        elements.settingsMaintenanceLatest.textContent = latestVersion || 'Unknown';
    }
    if (elements.settingsMaintenanceDetail) {
        elements.settingsMaintenanceDetail.textContent = maintenance.detail || '';
    }
    if (elements.settingsUpdateLog) {
        elements.settingsUpdateLog.textContent = maintenance.log || 'No update log available yet.';
    }
    if (elements.settingsUpdateDetailsToggle) {
        elements.settingsUpdateDetailsToggle.textContent = showDetails ? 'Hide update details' : 'Show update details';
        elements.settingsUpdateDetailsToggle.setAttribute('aria-expanded', showDetails ? 'true' : 'false');
    }
    if (elements.settingsUpdateDetails) {
        elements.settingsUpdateDetails.classList.toggle('hidden', !showDetails);
    }
    if (elements.settingsUpdateCheckBtn) {
        elements.settingsUpdateCheckBtn.disabled = !!maintenance.pending || !!maintenance.restartPending;
    }
    if (elements.settingsUpdateRunBtn) {
        const updateDisabled = !!maintenance.pending || !!maintenance.restartPending || maintenance.updateAvailable !== true;
        elements.settingsUpdateRunBtn.disabled = updateDisabled;
        elements.settingsUpdateRunBtn.classList.toggle('btn-primary', maintenance.updateAvailable === true && !updateDisabled);
        elements.settingsUpdateRunBtn.classList.toggle('btn-secondary', maintenance.updateAvailable !== true || updateDisabled);
    }
    if (elements.settingsRestoreRunBtn) {
        const restoreDisabled = !!maintenance.pending || !!maintenance.restartPending;
        elements.settingsRestoreRunBtn.disabled = restoreDisabled;
    }
    if (elements.settingsRestoreSection) {
        elements.settingsRestoreSection.classList.toggle('hidden', !maintenance.dirtyBlock);
    }
}

async function checkFxrouteUpdate(options = {}) {
    const silent = !!options.silent;
    if (!silent) {
        state.settings.maintenance.pending = true;
        state.settings.maintenance.operation = 'check';
        state.settings.maintenance.detail = 'Checking GitHub for updates…';
        state.settings.maintenance.hasError = false;
        state.settings.maintenance.userCollapsedDetails = false;
        renderSettingsPanel();
    }
    let data = {};
    try {
        const resp = await fetch('/api/system/update');
        data = await resp.json().catch(() => ({}));
        const logText = updateLogFromResult(data);
        if (!resp.ok || data.ok === false) throw new Error(data.detail || data.stderr || 'Update check failed');
        const updateInfo = parseUpdateInfo(logText);
        state.settings.maintenance = {
            ...state.settings.maintenance,
            installedVersion: data.installed_version || '',
            latestSummary: parseUpdateSummary(logText),
            detail: updateInfo.updateAvailable ? 'Update available.' : 'FXRoute is up to date.',
            ...updateInfo,
            log: logText,
            pending: false,
            restartPending: false,
            operation: '',
            userCollapsedDetails: false,
            hasError: false,
        };
    } catch (error) {
        const errorMsg = error.message || 'Update check failed';
        const isDirtyBlock = errorMsg.includes('Local changes detected');
        state.settings.maintenance = {
            ...state.settings.maintenance,
            installedVersion: data.installed_version || state.settings.maintenance.installedVersion || '',
            latestSummary: isDirtyBlock
                ? 'Local source changes detected. Update disabled to protect this checkout.'
                : 'Update check failed.',
            detail: errorMsg,
            log: errorMsg,
            pending: false,
            restartPending: false,
            operation: '',
            userCollapsedDetails: false,
            hasError: true,
            dirtyBlock: isDirtyBlock,
        };
    }
    renderSettingsPanel();
}

async function waitForFxrouteRestart() {
    const deadline = Date.now() + 30000;
    await new Promise(resolve => setTimeout(resolve, 1200));
    while (Date.now() < deadline) {
        try {
            const resp = await fetch('/api/status', { cache: 'no-store' });
            if (resp.ok) {
                state.settings.maintenance.restartPending = false;
                state.settings.maintenance.operation = '';
                state.settings.maintenance.detail = 'Reload/restart completed. Refresh the page if the interface still shows old assets.';
                renderSettingsPanel();
                showToast('FXRoute restart completed', 'success');
                return;
            }
        } catch (e) {
            // The service is expected to disappear briefly during restart.
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
    }
    state.settings.maintenance.restartPending = false;
    state.settings.maintenance.operation = '';
    state.settings.maintenance.detail = 'Update finished, but restart confirmation timed out. Check fxroute-status on the host.';
    renderSettingsPanel();
}

async function runFxrouteUpdate() {
    state.settings.maintenance.pending = true;
    state.settings.maintenance.operation = 'update';
    state.settings.maintenance.detail = 'Updating FXRoute…';
    state.settings.maintenance.hasError = false;
    state.settings.maintenance.userCollapsedDetails = false;
    renderSettingsPanel();
    let data = {};
    try {
        const resp = await fetch('/api/system/update', { method: 'POST' });
        data = await resp.json().catch(() => ({}));
        const logText = updateLogFromResult(data);
        if (!resp.ok || data.ok === false) throw new Error(data.detail || data.stderr || 'Update failed');
        const updateInfo = parseUpdateInfo(logText);
        state.settings.maintenance = {
            ...state.settings.maintenance,
            installedVersion: data.installed_version || state.settings.maintenance.installedVersion,
            latestSummary: 'Update completed.',
            detail: data.restart_scheduled ? 'Restarting FXRoute service…' : 'Update completed.',
            currentVersion: updateInfo.currentVersion || data.installed_version || state.settings.maintenance.currentVersion,
            latestVersion: updateInfo.latestVersion || state.settings.maintenance.latestVersion,
            updateAvailable: false,
            log: logText,
            pending: false,
            restartPending: !!data.restart_scheduled,
            operation: data.restart_scheduled ? 'update' : '',
            userCollapsedDetails: false,
            hasError: false,
            dirtyBlock: false,
        };
        renderSettingsPanel();
        if (data.restart_scheduled) {
            void waitForFxrouteRestart();
        } else {
            showToast('FXRoute update completed', 'success');
        }
    } catch (error) {
        const errorMsg = error.message || 'Update failed';
        const isDirtyBlock = errorMsg.includes('Local changes detected');
        state.settings.maintenance.pending = false;
        state.settings.maintenance.restartPending = false;
        state.settings.maintenance.operation = '';
        state.settings.maintenance.installedVersion = data.installed_version || state.settings.maintenance.installedVersion || '';
        state.settings.maintenance.latestSummary = isDirtyBlock
            ? 'Local source changes detected. Update disabled to protect this checkout.'
            : 'Update failed.';
        state.settings.maintenance.detail = errorMsg;
        state.settings.maintenance.log = errorMsg;
        state.settings.maintenance.hasError = true;
        state.settings.maintenance.userCollapsedDetails = false;
        state.settings.maintenance.dirtyBlock = isDirtyBlock;
        renderSettingsPanel();
        showToast(errorMsg, 'error');
    }
}

async function restoreFxrouteToPublic() {
    const confirmMsg = [
        'This will reset the FXRoute checkout to the current public release on GitHub. ',
        'Local source changes will be saved as a patch file in backups/. ',
        'User data, music, config, and runtime cache are not affected. ',
        'The service will restart after restore.\n\nContinue?'
    ].join('');
    if (!confirm(confirmMsg)) return;

    state.settings.maintenance.pending = true;
    state.settings.maintenance.operation = 'restore';
    state.settings.maintenance.detail = 'Restoring to public release…';
    state.settings.maintenance.hasError = false;
    state.settings.maintenance.userCollapsedDetails = false;
    renderSettingsPanel();
    let data = {};
    try {
        const resp = await fetch('/api/system/restore', { method: 'POST' });
        data = await resp.json().catch(() => ({}));
        const logText = updateLogFromResult(data);
        if (!resp.ok || data.ok === false) throw new Error(data.detail || data.stderr || 'Restore failed');
        state.settings.maintenance = {
            ...state.settings.maintenance,
            installedVersion: data.installed_version || state.settings.maintenance.installedVersion,
            latestSummary: 'Restore completed.',
            detail: 'Restarting FXRoute service…',
            currentVersion: data.installed_version || state.settings.maintenance.currentVersion,
            latestVersion: '',
            updateAvailable: null,
            log: logText,
            pending: false,
            restartPending: true,
            operation: 'restore',
            userCollapsedDetails: false,
            hasError: false,
            dirtyBlock: false,
        };
        renderSettingsPanel();
        void waitForFxrouteRestart();
    } catch (error) {
        const errorMsg = error.message || 'Restore failed';
        state.settings.maintenance.pending = false;
        state.settings.maintenance.restartPending = false;
        state.settings.maintenance.operation = '';
        state.settings.maintenance.installedVersion = data.installed_version || state.settings.maintenance.installedVersion || '';
        state.settings.maintenance.latestSummary = 'Restore failed.';
        state.settings.maintenance.detail = errorMsg;
        state.settings.maintenance.log = errorMsg;
        state.settings.maintenance.hasError = true;
        state.settings.maintenance.userCollapsedDetails = false;
        state.settings.maintenance.dirtyBlock = false;
        renderSettingsPanel();
        showToast(errorMsg, 'error');
    }
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
    const outputMode = overview.output_mode || {};
    const mode = outputMode.mode || 'stereo';

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

    if (elements.settingsOutputModeSelect && (_audioOutputModeSwitchInProgress || !isSelectFocused(elements.settingsOutputModeSelect))) {
        const optionsHtml = [
            '<option value="stereo">Stereo</option>',
            '<option value="subwoofer-2.1">2.1 Subwoofer</option>',
            '<option value="subwoofer-2.2">2.2 Subwoofer</option>',
            '<option value="subwoofer-2.2-stereo">2.2 Stereo Bass</option>',
        ].join('');
        if (elements.settingsOutputModeSelect.innerHTML !== optionsHtml) {
            elements.settingsOutputModeSelect.innerHTML = optionsHtml;
        }
        elements.settingsOutputModeSelect.value = mode;
        elements.settingsOutputModeSelect.disabled = !overview.available || _audioOutputModeSwitchInProgress;
    }
    if (elements.settingsOutputModeHint) {
        const channels = outputMode.effective_output_channels;
        if (mode === 'subwoofer-2.1') {
            elements.settingsOutputModeHint.textContent = outputMode.available
                ? `2.1 fixed routing active: ${outputMode.routing?.status || 'Out 1/2 Main · Out 3/4 Sub'}.`
                : '2.1 requires a selected output device with at least 4 channels.';
        } else if (mode === 'subwoofer-2.2') {
            elements.settingsOutputModeHint.textContent = outputMode.available
                ? `2.2 fixed routing active: Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2.`
                : '2.2 requires a selected output device with at least 4 channels.';
        } else if (mode === 'subwoofer-2.2-stereo') {
            elements.settingsOutputModeHint.textContent = outputMode.available
                ? `2.2 Stereo Bass fixed routing active: Out 1/2 Main · Out 3 Left Sub · Out 4 Right Sub.`
                : '2.2 Stereo Bass requires a selected output device with at least 4 channels.';
        } else {
            elements.settingsOutputModeHint.textContent = channels
                ? `Stereo mode active. Selected output reports ${channels} channels.`
                : 'Stereo output mode active.';
        }
    }

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
        elements.settingsSamplerateHint.textContent = sampleRatePolicy.mode === 'fixed'
            ? `Playback graph and hardware output are fixed at ${formatSampleRateKhz(sampleRatePolicy.rate)}.`
            : 'Follows the effective playback sample rate.';
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
        const libraries = Array.isArray(musicLibrary.libraries) ? musicLibrary.libraries : [];
        elements.settingsMusicLibrarySelect.innerHTML = libraries
            .map((library) => `<option value="${escapeHtml(library.id || '')}">${escapeHtml(library.label || '')}</option>`)
            .join('') || '<option value="local">Local</option>';
        elements.settingsMusicLibrarySelect.value = musicLibrary.active_id || 'local';
        elements.settingsMusicLibrarySelect.disabled = !!musicLibrary.pending;
    }
    renderHardwareController();
    renderMaintenancePanel();
    renderSubwooferPanel();
    applySourceModeUiState();
}

async function fetchMusicLibraries() {
    try {
        const resp = await fetch('/api/music-libraries');
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to discover music libraries');
        state.settings.musicLibrary = { ...data, pending: false };
        renderSettingsPanel();
    } catch (error) {
        console.debug('Failed to discover music libraries', error);
    }
}

async function selectMusicLibrary(libraryId) {
    const previousId = state.settings.musicLibrary.active_id;
    state.settings.musicLibrary.pending = true;
    renderSettingsPanel();
    try {
        const resp = await fetch('/api/music-libraries/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: libraryId }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to switch music library');
        state.settings.musicLibrary = { ...data, pending: false };
        state.library.tracks = [];
        state.library.selectedTrackIds = [];
        state.library.currentFolder = '';
        state.library.albums = [];
        state.library.albumsLoaded = false;
        state.library.albumDetail = null;
        state.library.scanning = true;
        renderSettingsPanel();
        renderLibraryView();
        await fetchLibraryStatus();
        showToast('Music library switched', 'success');
    } catch (error) {
        state.settings.musicLibrary.active_id = previousId;
        state.settings.musicLibrary.pending = false;
        renderSettingsPanel();
        showToast(error.message || 'Failed to switch music library', 'error');
    }
}

async function addManualMusicLibrary(url) {
    try {
        const resp = await fetch('/api/music-libraries/manual', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to add network share');
        state.settings.musicLibrary = { ...data, pending: false };
        renderSettingsPanel();
        await selectMusicLibrary(data.entry.id);
    } catch (error) {
        showToast(error.message || 'Failed to add network share', 'error');
    }
}

function externalInputModeActive() {
    return state.settings?.sourceMode?.mode === 'external-input';
}

function nonAppSourceModeActive() {
    return ['external-input', 'bluetooth-input'].includes(state.settings?.sourceMode?.mode);
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
        elements.playbackBar.classList.toggle('hidden', nonAppSourceActive);
    }
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
    const mutationGeneration = _audioOutputModeMutationGeneration;
    try {
        const resp = await fetch('/api/audio/outputs');
        if (!resp.ok) throw new Error('Failed to fetch audio outputs');
        const data = await resp.json();
        if (mutationGeneration !== _audioOutputModeMutationGeneration || _audioOutputModeSwitchInProgress) return;
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
        syncAutoSubButton();
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
        syncAutoSubButton();
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
        renderSettingsPanel();
        triggerSamplerateBurstPolling();
        showToast('Sample-rate policy updated', 'success');
    } catch (error) {
        state.samplerate.pending = false;
        renderSettingsPanel();
        showToast(error.message || 'Failed to save sample-rate policy', 'error');
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
    elements.playbackBar?.classList.toggle('has-progress', showProgress);
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
    const showShuffle = nativeQueueActive && Number(queue.count || 0) > 1;
    const showLoop = nativeQueueActive;
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
    if (!input) return;
    const percent = Math.max(0, Math.min(100, Number(fraction) * 100));
    input.style.setProperty('--range-progress', `${percent}%`);
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
            mergePlaybackState({ current_track: data.current_track, playing: data.playing, paused: data.paused, live_title: data.live_title, radio_metadata: data.radio_metadata, stream_info: data.stream_info });
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
    // is the Qobuz footer flicker between "FLAC · 16 bit · 44.1 kHz" and
    // "44.1 kHz". No state is cached here: this path simply does not own the
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
        button.textContent = '♡';
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
        button.textContent = favorite ? '♥' : '♡';
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
    button.textContent = favorite ? '♥' : '♡';
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
        button.textContent = favorite ? '♥' : '♡';
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

function renderStereoMeter(warning, active) {
    if (!elements.playbackMeter) return;
    const fresh = !!warning?.available && warning?.vu_fresh === true && !!active;
    elements.playbackMeter.classList.toggle('is-active', fresh);
    elements.playbackMeter.classList.toggle('is-peak', !!(warning?.detected_l || warning?.detected_r || warning?.detected));
    renderMeterChannel(elements.meterLeft, fresh ? warning?.vu_db_l : null, fresh && !!warning?.detected_l);
    renderMeterChannel(elements.meterRight, fresh ? warning?.vu_db_r : null, fresh && !!warning?.detected_r);
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
    const vuDb = Number.isFinite(Number(warning.vu_db)) ? Number(warning.vu_db) : null;
    const playbackActive = activeOverride === null
        ? (isStreamingFooterSource(window.__footerSource)
            ? streamingFooterData()?.status === 'Playing'
            : !!state.playback.playing && !state.playback.paused)
        : !!activeOverride;
    const showVu = !!warning.available && warning.vu_fresh === true
        && playbackActive && vuDb !== null;

    if (elements.outputLevelBadge) {
        elements.outputLevelBadge.classList.toggle('hidden', !showVu);
        elements.outputLevelBadge.style.visibility = '';
        elements.outputLevelBadge.textContent = showVu ? formatOutputLevelBadgeDb(vuDb) : '';
        elements.outputLevelBadge.title = showVu ? `Post-DSP output level (slow VU) on ${title}` : '';
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
    if (backendOwner === 'qobuz') {
        setFooterSource('qobuz', 'backend-footer-owner-qobuz');
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
    // Source classes remain available to unrelated page features. The footer
    // itself is laid out exclusively from the data-backed visibility classes.
    const isRadio = current_track && current_track.source === 'radio';
    if (!freezeActive) {
        document.body.classList.remove('source-local', 'source-radio');
        document.body.classList.add(isRadio ? 'source-radio' : 'source-local');
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
        document.querySelectorAll('.station-card.active, .track-item.active').forEach(item => item.classList.remove('active'));
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
    // Library tracks
    document.querySelectorAll('.track-item').forEach(item => {
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
    await Promise.all([radioModule.fetchStations(), fetchTracks(), fetchEffects(), fetchMeasurements(), fetchPlaybackStatus(), fetchSamplerateStatus(), fetchDownloadStatus(), fetchAudioOutputOverview(), fetchAudioSourceOverview()]);
    requestSubwooferPreviewRedrawFromState();
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

    if (allTracks.length === 0 && (!hasSearch || filteredPlaylists.length === 0)) {
        window.FXRouteContentState.set(loadingEl, scanText ? 'loading' : 'empty',
            scanText || 'No tracks yet. Import a file or URL to get started.');
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
                <button class="playlist-delete-btn" data-playlist-delete="${escapeHtml(playlist.id)}" type="button" title="Delete playlist">🗑</button>
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
        const thumbHtml = trackThumbHtml(track);
        return `
            <div class="track-item ${isSelected ? 'selected' : ''}" data-track-id="${escapeHtml(track.id)}">
                <button class="track-play" data-track-id="${escapeHtml(track.id)}" type="button" title="${escapeHtml(rel)}" aria-label="Play ${escapeHtml(track.title)}">▶</button>
                ${thumbHtml}
                <div class="track-info">
                    <div class="track-title">${escapeHtml(track.title)}</div>
                    ${subline ? `<div class="track-artist track-sub">${escapeHtml(subline)}</div>` : ''}
                </div>
                <button class="track-add ${isSelected ? 'is-active' : ''}"
                        data-track-add="${escapeHtml(track.id)}"
                        type="button"
                        aria-pressed="${isSelected ? 'true' : 'false'}"
                        aria-label="${isSelected ? 'Remove track from selection' : 'Add track to selection'}"
                        title="${isSelected ? 'Remove from selection' : 'Add to selection'}">${isSelected ? '✓' : '+'}</button>
                <button class="track-row-favorite ${track.favorite ? 'active' : ''}"
                        data-track-favorite="${escapeHtml(track.id)}"
                        type="button"
                        aria-pressed="${track.favorite ? 'true' : 'false'}"
                        aria-label="${track.favorite ? 'Remove track from favorites' : 'Add track to favorites'}"
                        title="${track.favorite ? 'Remove from favorites' : 'Add to favorites'}">${track.favorite ? '♥' : '♡'}</button>
            </div>
        `;
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

    elements.tracksList.querySelectorAll('.track-play[data-track-id]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            playLocal(item.dataset.trackId);
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
    state.library.viewMode = mode === 'folders' ? 'folders' : mode === 'albums' ? 'albums' : 'tracks';
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

let albumsFetchInFlight = false;
async function fetchAlbums() {
    if (state.library.albumsLoaded && state.library.albums.length > 0) return;
    if (albumsFetchInFlight) return;
    albumsFetchInFlight = true;
    try {
        const res = await fetch('/api/albums');
        if (!res.ok) return;
        state.library.albums = await res.json();
        state.library.albumsLoaded = true;
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
        // Albums not yet loaded — show loading state and trigger fetch
        window.FXRouteContentState.set(loadingEl, 'loading', 'Loading albums…');
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
        window.FXRouteContentState.set(loadingEl, 'empty',
            state.library.showFavoriteAlbums
                ? 'No favorite albums.'
                : query ? 'No matching albums.' : 'No albums found. Import music with album tags.');
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
    const playlistHtml = playlists.map(playlist => `
        <div class="album-card playlist-card" data-playlist-id="${escapeHtml(playlist.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">${playlistCoverHtml(playlist)}</div>
            <div class="album-name">${escapeHtml(playlist.name)}</div>
            <div class="album-artist">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
        </div>`).join('');
    const manualHtml = albums.length > 0 ? albums.map(album => {
        const coverUrl = albumCoverUrl(album);
        const fallbackSvg = albumArtFallbackSvg(album.name || album.artist || 'Album');
        const imageSrc = coverUrl || fallbackSvg;
        return `
        <div class="album-card" data-album-id="${escapeHtml(album.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">
                <img class="album-art" src="${escapeHtml(imageSrc)}"
                     alt="${escapeHtml(album.name)}"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${fallbackSvg}'" />
            </div>
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

// Square cover thumbnail for the left of a track row. Resolves the track's
// album cover (same lookup the playlist collage uses) and falls back to the
// neutral :empty placeholder when there is no artwork.
function trackThumbHtml(track) {
    const album = findAlbumForTrack(track);
    const coverUrl = album ? albumCoverUrl(album) : '';
    if (!coverUrl) return '<div class="track-thumb" aria-hidden="true"></div>';
    const fallback = albumArtFallbackSvg(album?.name || album?.artist || track?.title || 'Album');
    return '<div class="track-thumb" aria-hidden="true"><img src="' + escapeHtml(coverUrl) +
        '" alt="" loading="lazy" onerror="this.onerror=null;this.remove()" /></div>';
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
            src="${escapeHtml(coverUrl || '/static/favicon.svg')}"
            alt="${escapeHtml(album.name || '')}"
            loading="lazy"
            onerror="this.onerror=null;this.classList.add('is-fallback');this.src='/static/favicon.svg';" />`;
    }).join('');
    return `<div class="playlist-collage playlist-collage--${count}">${cells}</div>`;
}

function playlistFallbackMarkSvg() {
    // Existing FXRoute brand glyph (green FX + waveform from favicon.svg),
    // used as a clearly visible fallback when a playlist has no artwork.
    return `<svg class="playlist-collage-fallback-mark" viewBox="0 0 512 512" aria-hidden="true">
        <path d="M108 352V160h168v54H170v30h96v52h-96v56z" fill="currentColor"/>
        <path d="M312 158h70l-58 92 78 104h-74l-41-58-42 58h-74l80-106-58-90h71l25 42z" fill="currentColor"/>
        <path d="M82 386c46 0 46-32 92-32s46 32 92 32 46-32 92-32 46 32 92 32" fill="none" stroke="currentColor" stroke-opacity="0.55" stroke-width="14" stroke-linecap="round"/>
    </svg>`;
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

        // Update detail header
        const coverUrl = albumCoverUrl(album);
        setAlbumCoverImage(elements.albumDetailCover, coverUrl, album.name || album.artist || 'Album');
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
        if (elements.playlistDetail) elements.playlistDetail.classList.add('hidden');
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
        state.library.playlistDetail = null;
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
    const selectedIds = new Set(state.library.selectedTrackIds);
    elements.albumDetailTracks.innerHTML = tracks.map((track, index) => {
        const favorite = !!track.favorite;
        const favoriteButton =
            '<button class="track-fav' + (favorite ? ' active' : '') + '" data-track-favorite="' + escapeHtml(track.id) + '" type="button"' +
            ' aria-pressed="' + (favorite ? 'true' : 'false') + '"' +
            ' aria-label="' + (favorite ? 'Remove track from favorites' : 'Add track to favorites') + '"' +
            ' title="' + (favorite ? 'Remove from favorites' : 'Add to favorites') + '">' + (favorite ? '♥' : '♡') + '</button>';
        const isSelected = selectedIds.has(track.id);
        const selectionButton =
            '<button class="track-add' + (isSelected ? ' is-active' : '') + '" data-track-add="' + escapeHtml(track.id) + '" type="button"' +
            ' aria-pressed="' + (isSelected ? 'true' : 'false') + '"' +
            ' aria-label="' + (isSelected ? 'Remove track from selection' : 'Add track to selection') + '"' +
            ' title="' + (isSelected ? 'Remove from selection' : 'Add to selection') + '">' + (isSelected ? '✓' : '+') + '</button>';
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + escapeHtml(track.id) + '" data-album-context="' + escapeHtml(albumId) + '">' +
            detailTrackRowHtml({
                index: index + 1,
                title: escapeHtml(track.title || 'Unknown'),
                // In an open album the album name is page context, not row
                // metadata; only the artist is repeated per track.
                sub: escapeHtml((track.artist || '').trim()),
                thumb: trackThumbHtml(track),
                favoriteButton,
                selectionButton,
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

// Shared detail track-row body for the library album detail and the Tidal
// album/playlist details (streaming.js receives it via the init api). One
// row language: index, round play button, stacked title/sub, optional
// selection Plus, favorite, duration.
function detailTrackRowHtml({ index, title, sub, favoriteButton, selectionButton, duration, thumb }) {
    return (
        '<span class="track-index">' + index + '</span>' +
        '<button type="button" class="track-play" title="Play">▶</button>' +
        (thumb || '') +
        '<div class="track-info">' +
            '<div class="track-title">' + title + '</div>' +
            (sub ? '<div class="track-sub">' + sub + '</div>' : '') +
        '</div>' +
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
    elements.albumFavoriteToggle.textContent = favorite ? '♥' : '♡';
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
    state.library.playlistDetail = null;
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
    return detailFactsHtml(lines);
}

// Shared metadata-rows builder used by the library album detail and the TIDAL
// album detail (streaming.js receives it via the init api): one row language
// for every fact, no per-provider copy.
function detailFactsHtml(lines) {
    const rows = (lines || []).filter(Boolean).map(line => `<div>${escapeHtml(line)}</div>`).join('');
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

// Shared collapsible "About" component (library + TIDAL album details). The
// TIDAL artist page intentionally renders about directly visible instead.
function detailAboutHtml(label, description) {
    return `
        <details class="album-detail-about">
            <summary>${escapeHtml(label)}</summary>
            <p>${escapeHtml(description)}</p>
        </details>
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

    if (elements.playlistDetailCover) {
        elements.playlistDetailCover.innerHTML = playlistCoverHtml(playlist);
    }
    if (elements.playlistDetailName) elements.playlistDetailName.textContent = playlist.name;
    renderPlaylistDetailTracks();

    elements.albumsGrid.classList.add('hidden');
    elements.albumDetail.classList.add('hidden');
    if (elements.playlistDetail) elements.playlistDetail.classList.remove('hidden');
    updatePlaylistSaveRowVisibility();
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
        const favorite = !!track.favorite;
        const favoriteButton =
            '<button class="track-fav' + (favorite ? ' active' : '') + '" data-track-favorite="' + escapeHtml(track.id) + '" type="button"' +
            ' aria-pressed="' + (favorite ? 'true' : 'false') + '"' +
            ' aria-label="' + (favorite ? 'Remove track from favorites' : 'Add track to favorites') + '"' +
            ' title="' + (favorite ? 'Remove from favorites' : 'Add to favorites') + '">' + (favorite ? '♥' : '♡') + '</button>';
        const isSelected = selectedIds.has(track.id);
        const selectionButton =
            '<button class="track-add' + (isSelected ? ' is-active' : '') + '" data-track-add="' + escapeHtml(track.id) + '" type="button"' +
            ' aria-pressed="' + (isSelected ? 'true' : 'false') + '"' +
            ' aria-label="' + (isSelected ? 'Remove track from selection' : 'Add track to selection') + '"' +
            ' title="' + (isSelected ? 'Remove from selection' : 'Add to selection') + '">' + (isSelected ? '✓' : '+') + '</button>';
        const sub = escapeHtml([(track.artist || '').trim(), (track.album || '').trim()].filter(Boolean).join(' · '));
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + escapeHtml(track.id) + '">' +
            detailTrackRowHtml({
                index: index + 1,
                title: escapeHtml(track.title || 'Unknown'),
                sub,
                thumb: trackThumbHtml(track),
                favoriteButton,
                selectionButton,
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
function updatePlaylistSaveRowVisibility() {
    if (!elements.playlistSaveRow) return;
    const count = state.library.selectedTrackIds.length;
    // The playlist-build selection is independent of the view mode: whenever
    // at least two tracks are selected (album detail, tracks or folders), the
    // existing save-playlist row stays reachable.
    const hasPlaylistSelection = count >= 2;
    elements.playlistSaveRow.classList.toggle('hidden', !hasPlaylistSelection);
    if (elements.playlistSaveControls) {
        elements.playlistSaveControls.classList.toggle('hidden', !hasPlaylistSelection);
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
        if (data.playback) {
            mergePlaybackState(data.playback);
        }
        updatePlaybackUI();
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
        showNowPlayingCue(playedTrack, 'Now playing');
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
        let playedTrack = track;
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
            playedTrack = data.playback.current_track || track;
        }
        updatePlaybackUI();
        triggerSamplerateBurstPolling();
        const queueCount = (((data || {}).playback || {}).queue || {}).count || 0;
        showNowPlayingCue(playedTrack, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing');
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

        const resp = await fetch('/api/dsp/presets/import-filter-dual', {
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
    const leftCount = state.dsp?.peqDraft?.leftBands?.length || 0;
    const rightCount = state.dsp?.peqDraft?.rightBands?.length || 0;
    const actionLabel = elements.effectsPeqDisclosure.open ? 'Collapse' : 'Expand';
    elements.effectsPeqDisclosureMeta.textContent = `L${leftCount} · R${rightCount} · ${actionLabel}`;
}

function clearEffectsPeqStatusOnCollapse() {
    if (!elements.effectsPeqDisclosure || elements.effectsPeqDisclosure.open) return;
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = '';
}

function setupEffectsActions() {
    if (elements.splCalibrationOpen) elements.splCalibrationOpen.addEventListener('click', openSplCalibration);
    if (elements.splCalibrationClose) elements.splCalibrationClose.addEventListener('click', closeSplCalibration);
    if (elements.splCalibrationPanel?.querySelector('.manage-overlay-backdrop')) {
        elements.splCalibrationPanel.querySelector('.manage-overlay-backdrop').addEventListener('click', closeSplCalibration);
    }
    if (elements.splCalibrationNoise) elements.splCalibrationNoise.addEventListener('click', toggleSplCalibrationNoise);
    if (elements.splCalibrationSave) elements.splCalibrationSave.addEventListener('click', saveSplCalibration);
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
            state.dsp.combineDraft = state.dsp.combineDraft || getDefaultEffectsCombineDraft();
            state.dsp.combineDraft.preset1 = event.target.value;
            if (state.dsp.combineDraft.preset1 && state.dsp.combineDraft.preset1 === state.dsp.combineDraft.preset2) {
                state.dsp.combineDraft.preset2 = '';
                if (elements.effectsCombinePreset2) elements.effectsCombinePreset2.value = '';
            }
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombinePreset2) {
        elements.effectsCombinePreset2.addEventListener('change', (event) => {
            state.dsp.combineDraft = state.dsp.combineDraft || getDefaultEffectsCombineDraft();
            state.dsp.combineDraft.preset2 = event.target.value;
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombinePreset3) {
        elements.effectsCombinePreset3.addEventListener('change', (event) => {
            state.dsp.combineDraft = state.dsp.combineDraft || getDefaultEffectsCombineDraft();
            state.dsp.combineDraft.preset3 = event.target.value;
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombinePresetName) {
        elements.effectsCombinePresetName.addEventListener('input', (event) => {
            state.dsp.combineDraft = state.dsp.combineDraft || getDefaultEffectsCombineDraft();
            state.dsp.combineDraft.presetName = event.target.value;
            renderEffectsCombine();
        });
    }
    if (elements.effectsCombineSaveBtn) elements.effectsCombineSaveBtn.addEventListener('click', createCombinedEffectsPreset);
    if (elements.effectsPeqPresetName) elements.effectsPeqPresetName.addEventListener('input', (event) => {
        if (!state.dsp?.peqDraft) return;
        state.dsp.peqDraft.presetName = event.target.value;
    });
    if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.addEventListener('change', (event) => {
        if (!state.dsp?.peqDraft) return;
        state.dsp.peqDraft.eqMode = normalizePeqEqMode(event.target.value);
        event.target.value = state.dsp.peqDraft.eqMode;
    });
    if (elements.effectsPeqAddBandBtn) elements.effectsPeqAddBandBtn.addEventListener('click', addPeqBandPair);
    if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.addEventListener('click', createPeqPreset);
    [
        elements.effectsSubwooferFrequencyNumber,
        elements.effectsSubwooferLevel,
        elements.effectsSubwooferDelay,
        elements.effectsSubwooferPolarity,
        elements.effectsSubwooferSub2Level,
        elements.effectsSubwooferSub2Delay,
        elements.effectsSubwooferSub2Polarity,
        elements.effectsSubwooferMainHighpass,
    ].forEach(el => {
        if (!el) return;
        el.addEventListener('focus', () => _activeEditing.add(el));
        el.addEventListener('input', () => {
            updateSubwooferDraftFromControls();
        });
        el.addEventListener('change', () => saveSubwooferDebounced(0));
        el.addEventListener('blur', () => {
            _activeEditing.delete(el);
            saveSubwooferDebounced(0);
        });
    });
    if (elements.effectsSubwooferPreview) {
        let draggingCrossover = false;
        const updateFromPointer = (event, commit = false) => {
            const rect = elements.effectsSubwooferPreview.getBoundingClientRect();
            const layout = getSubwooferPreviewLayout(rect.width || 560, rect.height || 132);
            const plotW = Math.max(1, layout.plotW);
            const t = Math.max(0, Math.min(1, (event.clientX - rect.left - layout.pad.left) / plotW));
            const minHz = 20;
            const maxHz = 300;
            const hz = Math.round(Math.pow(10, Math.log10(minHz) + t * (Math.log10(maxHz) - Math.log10(minHz))));
            if (elements.effectsSubwooferFrequencyNumber) elements.effectsSubwooferFrequencyNumber.value = String(hz);
            if (commit) saveSubwooferDebounced(0);
            else updateSubwooferDraftFromControls();
        };
        elements.effectsSubwooferPreview.addEventListener('pointerdown', (event) => {
            draggingCrossover = true;
            if (elements.effectsSubwooferFrequencyNumber) _activeEditing.add(elements.effectsSubwooferFrequencyNumber);
            elements.effectsSubwooferPreview.setPointerCapture?.(event.pointerId);
            updateFromPointer(event, false);
        });
        elements.effectsSubwooferPreview.addEventListener('pointermove', (event) => {
            if (draggingCrossover) updateFromPointer(event, false);
        });
        elements.effectsSubwooferPreview.addEventListener('pointerup', (event) => {
            if (!draggingCrossover) return;
            draggingCrossover = false;
            updateFromPointer(event, true);
            if (elements.effectsSubwooferFrequencyNumber) _activeEditing.delete(elements.effectsSubwooferFrequencyNumber);
        });
        elements.effectsSubwooferPreview.addEventListener('pointercancel', () => {
            draggingCrossover = false;
            if (elements.effectsSubwooferFrequencyNumber) _activeEditing.delete(elements.effectsSubwooferFrequencyNumber);
        });
        if ('ResizeObserver' in window) {
            const resizeObserver = new ResizeObserver(() => requestSubwooferPreviewRedrawFromState());
            resizeObserver.observe(elements.effectsSubwooferPreview);
            elements.effectsSubwooferPreview._fxrouteResizeObserver = resizeObserver;
        } else {
            window.addEventListener('resize', requestSubwooferPreviewRedrawFromState);
        }
    }
    // Track focus to avoid resetting input values while user is typing
    [
        elements.effectsHeadroomGainDb,
        elements.effectsAutogainTargetDb,
        elements.effectsLoudnessStrength,
        elements.effectsLoudnessFftSize,
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
    if (elements.effectsLoudnessEnabled) elements.effectsLoudnessEnabled.addEventListener('change', () => {
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
    setupUploadArea('effects-import-area', 'effects-import-file', () => {
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

let splCalibrationNoiseActive = false;
let splCalibrationAutomaticAvailable = false;
let splCalibrationAutomaticRunning = false;
let splCalibrationOperationGeneration = 0;

function splCalibrationModeLabel(data) {
    return data.automatic?.available
        ? `Automatic SPL measurement: ${data.automatic.microphone_model} detected`
        : 'Manual SPL measurement';
}

function resetSplCalibrationNoiseButton() {
    if (!elements.splCalibrationNoise) return;
    elements.splCalibrationNoise.disabled = false;
    elements.splCalibrationNoise.textContent = splCalibrationNoiseActive ? 'Stop noise' : 'Start noise';
}

async function runSplCalibrationNoiseCountdown(generation) {
    for (const count of [3, 2, 1]) {
        if (generation !== splCalibrationOperationGeneration) return false;
        if (elements.splCalibrationNoise) elements.splCalibrationNoise.textContent = `Starting noise: ${count}`;
        await new Promise((resolve) => setTimeout(resolve, 1000));
    }
    return generation === splCalibrationOperationGeneration;
}

async function stopSplCalibrationOperation(statusText = '') {
    const generation = ++splCalibrationOperationGeneration;
    splCalibrationAutomaticRunning = false;
    if (elements.splCalibrationNoise) elements.splCalibrationNoise.disabled = true;
    try {
        const response = await fetch('/api/measurements/spl-calibration/noise', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: false }),
        });
        if (!response.ok) throw new Error('Failed to stop SPL calibration');
    } finally {
        if (generation !== splCalibrationOperationGeneration) return;
        splCalibrationNoiseActive = false;
        resetSplCalibrationNoiseButton();
        if (statusText && elements.splCalibrationStatus) {
            elements.splCalibrationStatus.textContent = statusText;
        }
    }
}

async function openSplCalibration() {
    elements.splCalibrationPanel?.classList.remove('hidden');
    window.FXRouteModal?.open(elements.splCalibrationPanel, {
        initialFocus: elements.splCalibrationNoise,
        onEscape: () => { void closeSplCalibration(); },
    });
    try {
        const response = await fetch('/api/measurements/spl-calibration');
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Failed to load SPL calibration');
        splCalibrationNoiseActive = !!data.noise_active;
        splCalibrationAutomaticAvailable = !!data.automatic?.available;
        if (elements.splCalibrationNoise) elements.splCalibrationNoise.textContent = splCalibrationNoiseActive ? 'Stop noise' : 'Start noise';
        if (elements.splCalibrationAutoStatus) {
            elements.splCalibrationAutoStatus.textContent = splCalibrationModeLabel(data);
        }
    } catch (error) {
        if (elements.splCalibrationStatus) elements.splCalibrationStatus.textContent = error.message;
    }
}

async function closeSplCalibration() {
    await stopSplCalibrationOperation().catch(() => null);
    elements.splCalibrationPanel?.classList.add('hidden');
    window.FXRouteModal?.close(elements.splCalibrationPanel);
}

async function toggleSplCalibrationNoise() {
    if (splCalibrationAutomaticRunning) {
        await stopSplCalibrationOperation('Automatic SPL measurement cancelled.').catch((error) => {
            if (elements.splCalibrationStatus) elements.splCalibrationStatus.textContent = error.message;
        });
        return;
    }
    const next = !splCalibrationNoiseActive;
    if (!next) {
        await stopSplCalibrationOperation('Noise stopped; previous volume state restored.').catch((error) => {
            if (elements.splCalibrationStatus) elements.splCalibrationStatus.textContent = error.message;
        });
        return;
    }
    const generation = ++splCalibrationOperationGeneration;
    if (elements.splCalibrationNoise) elements.splCalibrationNoise.disabled = true;
    try {
        if (!await runSplCalibrationNoiseCountdown(generation)) return;
        if (splCalibrationAutomaticAvailable) {
            splCalibrationAutomaticRunning = true;
            if (elements.splCalibrationNoise) {
                elements.splCalibrationNoise.disabled = false;
                elements.splCalibrationNoise.textContent = 'Cancel measurement';
            }
            if (elements.splCalibrationStatus) {
                elements.splCalibrationStatus.textContent = 'Automatic SPL measurement in progress…';
            }
            const response = await fetch('/api/measurements/spl-calibration/automatic', { method: 'POST' });
            const data = await response.json();
            if (generation !== splCalibrationOperationGeneration) return;
            if (!response.ok) throw new Error(data.detail || 'Automatic UMIK SPL measurement failed');
            if (elements.splCalibrationMeasured) {
                elements.splCalibrationMeasured.value = Number(data.measured_spl_db).toFixed(1);
            }
            splCalibrationNoiseActive = false;
            if (elements.splCalibrationNoise) elements.splCalibrationNoise.textContent = 'Start noise';
            if (elements.splCalibrationStatus) {
                const adjustment = Number(data.required_adjustment_db);
                elements.splCalibrationStatus.textContent = `${data.microphone_model} measured ${Number(data.measured_spl_db).toFixed(1)} dB SPL · Loudness calibration offset ${adjustment >= 0 ? '+' : ''}${adjustment.toFixed(1)} dB. Save / Apply couples this offset to Loudness only.`;
            }
            return;
        }
        const response = await fetch('/api/measurements/spl-calibration/noise', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: true }),
        });
        const data = await response.json();
        if (generation !== splCalibrationOperationGeneration) return;
        if (!response.ok) throw new Error(data.detail || 'Calibration noise failed');
        splCalibrationNoiseActive = true;
        if (elements.splCalibrationNoise) elements.splCalibrationNoise.textContent = 'Stop noise';
        if (elements.splCalibrationStatus) {
            elements.splCalibrationStatus.textContent = 'Settling… read the C/Slow meter after about 1 second and average for about 3 seconds.';
        }
    } catch (error) {
        if (generation === splCalibrationOperationGeneration && elements.splCalibrationStatus) {
            elements.splCalibrationStatus.textContent = error.message;
        }
    } finally {
        if (generation === splCalibrationOperationGeneration) {
            splCalibrationAutomaticRunning = false;
            resetSplCalibrationNoiseButton();
        }
    }
}

async function saveSplCalibration() {
    const measured = Number(elements.splCalibrationMeasured?.value);
    if (!Number.isFinite(measured)) {
        if (elements.splCalibrationStatus) elements.splCalibrationStatus.textContent = 'Enter the measured C/Slow SPL value.';
        return;
    }
    try {
        const response = await fetch('/api/measurements/spl-calibration/apply', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ measured_spl_db: measured }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || 'Failed to apply SPL calibration');
        splCalibrationNoiseActive = false;
        if (elements.splCalibrationNoise) elements.splCalibrationNoise.textContent = 'Start noise';
        if (elements.splCalibrationStatus) {
            const adjustment = Number(data.required_adjustment_db);
            const sign = adjustment >= 0 ? '+' : '';
            elements.splCalibrationStatus.textContent = data.calibrated
                ? `Measured ${measured.toFixed(1)} dB SPL · Loudness calibration offset ${sign}${adjustment.toFixed(1)} dB · calibrated.`
                : `Measured ${measured.toFixed(1)} dB SPL · Loudness calibration offset ${sign}${adjustment.toFixed(1)} dB. The offset is coupled to Loudness only; playback with Loudness off is unchanged.`;
        }
        await fetchEffects();
    } catch (error) {
        if (elements.splCalibrationStatus) elements.splCalibrationStatus.textContent = error.message;
    }
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
function normalizeMeasurementTrace(trace = {}, index = 0) {
    return MeasurementUI.normalizeMeasurementTrace(trace, index);
}

function normalizeMeasurementEntry(measurement = {}, index = 0) {
    return MeasurementUI.normalizeMeasurementEntry(measurement, index);
}

function normalizeMeasurementVisibility(measurements = [], previous = {}) {
    return MeasurementUI.normalizeMeasurementVisibility(measurements, previous);
}

function formatMeasurementDate(value) {
    return MeasurementUI.formatMeasurementDate(value);
}

function normalizeMeasurementReviewVisibility(measurements = [], previous = {}) {
    return MeasurementUI.normalizeMeasurementReviewVisibility(measurements, previous);
}

function getVisibleMeasurementEntries() {
    const currentId = state.measurement.currentMeasurement?.id;
    return (state.measurement.measurements || []).filter(measurement => measurement.id !== currentId && state.measurement.visibilityById?.[measurement.id]);
}

function getCurrentMeasurementEntry() {
    return state.measurement.currentMeasurement ? normalizeMeasurementEntry(state.measurement.currentMeasurement, 0) : null;
}

function getCurrentMeasurementEntries() {
    const autoSubMeasurements = Array.isArray(state.measurement.autoSubMeasurements)
        ? state.measurement.autoSubMeasurements.map((measurement, index) => normalizeMeasurementEntry(measurement, index))
        : [];
    const repeatMeasurements = Array.isArray(state.measurement.pendingRepeatMeasurements)
        ? state.measurement.pendingRepeatMeasurements.map((measurement, index) => normalizeMeasurementEntry(measurement, index))
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

function measurementSmoothingHalfWindowOctaves(mode = '1/6-oct') {
    return MeasurementDsp.measurementSmoothingHalfWindowOctaves(mode);
}

function smoothMeasurementTracePoints(points = [], mode = '1/6-oct') {
    return MeasurementDsp.smoothMeasurementTracePoints(points, mode);
}

function trackFileUrl(trackId = '') {
    return MeasurementUI.trackFileUrl(trackId);
}

function measurementFileUrl(measurementId = '') {
    return MeasurementUI.measurementFileUrl(measurementId);
}

function presetFileUrl(presetName = '') {
    return MeasurementUI.presetFileUrl(presetName);
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

function getDefaultMeasurementPeqFilter(index = 0) {
    return MeasurementUI.getDefaultMeasurementPeqFilter(index);
}

function getDefaultMeasurementPeqState() {
    return MeasurementUI.getDefaultMeasurementPeqState();
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
    return MeasurementUI.getDefaultMeasurementConvolverState();
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
    const qualityAliases = { auto: 'linear_4096', normal: 'linear_4096', high: 'linear_8192' };
    const incomingQuality = qualityAliases[String(conv.quality)] || String(conv.quality || defaults.quality);
    const hasValidPhaseMode = measurementConvolverPhaseModes.includes(String(conv.phaseMode));
    const hasValidIrLength = measurementConvolverTapOptions.includes(Number(conv.irLength));
    if ((!hasValidPhaseMode || !hasValidIrLength) && getMeasurementConvolverTypeKeys().includes(incomingQuality)) {
        conv.phaseMode = getMeasurementConvolverPhaseModeForType(incomingQuality);
        conv.irLength = String(getMeasurementConvolverFirLengthForType(incomingQuality));
    }
    conv.phaseMode = measurementConvolverPhaseModes.includes(String(conv.phaseMode)) ? String(conv.phaseMode) : defaults.phaseMode;
    conv.irLength = measurementConvolverTapOptions.includes(Number(conv.irLength)) ? String(conv.irLength) : defaults.irLength;
    conv.quality = `${conv.phaseMode}_${conv.irLength}`;
    conv.creatingPreset = !!conv.creatingPreset;
    if (!conv.draft || typeof conv.draft !== 'object') conv.draft = { left: null, right: null, presetName: '', nameTouched: false, notice: '' };
    if (!conv.draft.left || typeof conv.draft.left !== 'object') conv.draft.left = null;
    if (!conv.draft.right || typeof conv.draft.right !== 'object') conv.draft.right = null;
    if (typeof conv.draft.presetName !== 'string') conv.draft.presetName = '';
    conv.draft.nameTouched = !!conv.draft.nameTouched;
    if (typeof conv.draft.notice !== 'string') conv.draft.notice = '';
    return conv;
}

function getMeasurementActiveEditor() {
    const editor = String(state.measurement?.activeEditor || 'none');
    return ['none', 'peq', 'houseCurve'].includes(editor) ? editor : 'none';
}

function getMeasurementRestorableTargetCurve() {
    const conv = ensureMeasurementConvolverState();
    const targetCurve = String(conv.targetCurve || '');
    return getMeasurementConvolverCurveOptions().some((curve) => curve.key === targetCurve)
        ? targetCurve
        : 'neutral';
}

function setMeasurementActiveEditor(editor = 'none') {
    const nextEditor = ['none', 'peq', 'houseCurve'].includes(editor) ? editor : 'none';
    state.measurement.activeEditor = nextEditor;
    const custom = ensureCustomHouseCurveState();
    custom.open = nextEditor === 'houseCurve';
    custom.displayTarget = nextEditor === 'houseCurve' ? 'editing-custom-house-curve' : 'actual';
    if (nextEditor !== 'peq') {
        const peq = ensureMeasurementPeqState();
        peq.dragFilterId = null;
    }
    ensureCustomHouseCurveState().dragPointId = null;
    if (state.measurement.convolverAssistant && typeof state.measurement.convolverAssistant === 'object') {
        state.measurement.convolverAssistant.dragMode = null;
    }
    return nextEditor;
}

function setMeasurementAssistMode(mode) {
    const nextMode = mode === 'convolver' ? 'convolver' : 'peq';
    const conv = ensureMeasurementConvolverState();
    // The custom editor is an overlay on the actual target selection. Never
    // let its sentinel or a deleted house curve become the active target when
    // returning to PEQ or Convolver.
    conv.targetCurve = getMeasurementRestorableTargetCurve();
    state.measurement.assistMode = nextMode;
    setMeasurementActiveEditor(nextMode === 'peq' ? 'peq' : 'none');
    if (nextMode === 'peq') ensureMeasurementPeqState().enabled = true;
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

function getMeasurementSlotChipStyle(color = '', active = false) {
    if (!color) return '';
    return `style="border-color:${escapeHtml(color)}66;background:${escapeHtml(color)}22;${active ? `color:#08110d;background:${escapeHtml(color)};box-shadow:0 0 0 2px ${escapeHtml(color)}66, 0 0 0 4px rgba(248,250,252,0.28);` : ''}"`;
}

function renderMeasurementSlotChip({ label, index, color = '', active = false, occupied = false, attributes = '' }) {
    const classes = `measurement-slot-chip measurement-peq-chip${active ? ' is-active' : ''}${occupied ? '' : ' is-empty'}`;
    return `<button type="button" class="${classes}" ${getMeasurementSlotChipStyle(color, active)} data-measurement-slot-index="${index}" ${attributes}>${escapeHtml(label)}</button>`;
}

function getAutoSubTargetCurveSnapshot() {
    const conv = ensureMeasurementConvolverState();
    const key = String(conv.targetCurve || '');
    const curve = getMeasurementConvolverCurveOptions().find((option) => option.key === key);
    if (!curve) return null;
    return {
        key,
        label: String(curve.label || curve.shortLabel || key),
        provenance: key.startsWith('house:') ? 'uploaded' : 'built_in',
        points: (curve.points || []).map((point) => [Number(point[0]), Number(point[1])]),
    };
}

function getMeasurementConvolverCurveDb(curveKey, frequencyHz) {
    const curve = getMeasurementConvolverCurve(curveKey);
    const points = curve.points || measurementConvolverCurves.neutral.points;
    return MeasurementDsp.getMeasurementConvolverCurveDbFromPoints(points, frequencyHz);
}

function getMeasurementHouseCurvePreviewPoints() {
    return ensureCustomHouseCurveState().points
        .map((point) => [Number(point.freqHz), Number(point.gainDb)])
        .filter(([frequency, gain]) => Number.isFinite(frequency) && frequency > 0 && Number.isFinite(gain))
        .sort((left, right) => left[0] - right[0]);
}

function getMeasurementTargetCurvePreview() {
    if (getMeasurementActiveEditor() === 'houseCurve') {
        return { label: 'Editing Custom House Curve…', shortLabel: 'Editing Custom House Curve…', points: getMeasurementHouseCurvePreviewPoints() };
    }
    return getMeasurementConvolverCurve(ensureMeasurementConvolverState().targetCurve);
}

function getMeasurementConvolverDraftPhaseMode(draft = null) {
    return MeasurementUI.getMeasurementConvolverDraftPhaseMode(draft);
}

function getMeasurementConvolverDrafts(conv = ensureMeasurementConvolverState()) {
    return [conv.draft?.left || null, conv.draft?.right || null].filter(Boolean);
}

function getMeasurementConvolverDraftPhaseMismatch(conv = ensureMeasurementConvolverState()) {
    const currentPhaseMode = String(conv.phaseMode || '');
    const mismatched = getMeasurementConvolverDrafts(conv).find((draft) => {
        const draftPhaseMode = getMeasurementConvolverDraftPhaseMode(draft);
        return draftPhaseMode && draftPhaseMode !== currentPhaseMode;
    });
    return mismatched ? getMeasurementConvolverDraftPhaseMode(mismatched) : '';
}

function clearMeasurementConvolverDraftForPhaseChange(previousPhaseMode = '') {
    const conv = ensureMeasurementConvolverState();
    if (previousPhaseMode === conv.phaseMode) return false;
    if (!conv.draft?.left && !conv.draft?.right) {
        conv.draft.notice = '';
        return false;
    }
    conv.draft.left = null;
    conv.draft.right = null;
    conv.draft.presetName = '';
    conv.draft.nameTouched = false;
    conv.draft.notice = 'Phase type changed. Take L/R again.';
    showMeasurementConvolverFeedback(conv.draft.notice);
    return true;
}

function updateMeasurementConvolverField(field, value) {
    const conv = ensureMeasurementConvolverState();
    const previousPhaseMode = conv.phaseMode;
    if (field === 'targetCurve') conv.targetCurve = getMeasurementConvolverCurveOptions().some((curve) => curve.key === value) ? value : conv.targetCurve;
    if (field === 'rangeStartHz') conv.rangeStartHz = Math.min(Math.round(clampMeasurementConvolverFrequency(value, conv.rangeStartHz)), conv.rangeEndHz - 1);
    if (field === 'rangeEndHz') conv.rangeEndHz = Math.max(Math.round(clampMeasurementConvolverFrequency(value, conv.rangeEndHz)), conv.rangeStartHz + 1);
    if (field === 'maxBoostDb') conv.maxBoostDb = [0, 3, 6, 9].includes(Number(value)) ? Number(value) : conv.maxBoostDb;
    if (field === 'maxCutDb') conv.maxCutDb = [-3, -6, -9, -12, -18, -24].includes(Number(value)) ? Number(value) : conv.maxCutDb;
    if (field === 'dipGuard') conv.dipGuard = ['off', 'gentle', 'adaptive'].includes(String(value)) ? String(value) : conv.dipGuard;
    if (field === 'sampleRate') {
        state.measurement.measurementSampleRate = String(value || '48000');
        void saveMeasurementSetupSettings({ measurementSampleRate: Number(state.measurement.measurementSampleRate) });
    }
    if (field === 'phaseMode') conv.phaseMode = measurementConvolverPhaseModes.includes(String(value)) ? String(value) : conv.phaseMode;
    if (field === 'irLength') conv.irLength = measurementConvolverTapOptions.includes(Number(value)) ? String(value) : conv.irLength;
    if (field === 'quality') {
        const quality = String(value || 'linear_4096');
        if (getMeasurementConvolverTypeKeys().includes(quality)) {
            conv.phaseMode = getMeasurementConvolverPhaseModeForType(quality);
            conv.irLength = String(getMeasurementConvolverFirLengthForType(quality));
        }
    }
    ensureMeasurementConvolverState();
    if (field === 'phaseMode' || field === 'quality') {
        clearMeasurementConvolverDraftForPhaseChange(previousPhaseMode);
    }
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
    if (getMeasurementActiveEditor() === 'houseCurve') return null;
    setMeasurementActiveEditor('peq');
    const peq = ensureMeasurementPeqState();
    if (peq.filters.length >= 12) {
        showToast('Measurement assistant supports up to 12 filters', 'warning');
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
    if (getMeasurementActiveEditor() === 'houseCurve') {
        resetCustomHouseCurveDraft();
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
        return;
    }
    setMeasurementActiveEditor('none');
    const peq = ensureMeasurementPeqState();
    const conv = ensureMeasurementConvolverState();
    state.measurement.currentMeasurement = null;
    state.measurement.pendingRepeatMeasurements = [];
    state.measurement.autoSubMeasurements = [];
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
    return MeasurementUI.getMeasurementPeqNameSuffix(date);
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
    state.dsp = state.dsp || {};
    state.dsp.assistStack = state.dsp.assistStack || [];
    state.dsp.assistStack.push({ type: 'peq', mode, createdAt: new Date().toISOString(), bands: mappedBands.map((band) => ({ ...band })) });
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
    const eqMode = normalizePeqEqMode(state.dsp?.peqDraft?.eqMode || elements.effectsPeqModeSelect?.value || 'IIR');
    peqCreateInFlight = true;
    if (elements.measurementPeqCreateBtn) elements.measurementPeqCreateBtn.disabled = true;
    showMeasurementPeqTakeFeedback(`Creating ${presetName}…`);
    try {
        const resp = await fetch('/api/dsp/presets/create-peq', {
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
    const visibleSavedEntries = getVisibleMeasurementEntries().filter(Boolean);
    return visibleSavedEntries.length ? visibleSavedEntries : getCurrentMeasurementEntries().filter(Boolean);
}

function getMeasurementConvolverSourceEntries() {
    return getMeasurementConvolverSelectedSourceEntries();
}

function getMeasurementConvolverSourceSelectionState() {
    const entries = getMeasurementConvolverSourceEntries()
        .filter((measurement) => getMeasurementDisplayTraces(measurement || {}).length > 0);
    const leftEntries = [];
    const rightEntries = [];
    const otherEntries = [];
    entries.forEach((measurement) => {
        const channel = String(measurement?.channel || 'left').toLowerCase();
        if (channel === 'left') {
            leftEntries.push(measurement);
        } else if (channel === 'right') {
            rightEntries.push(measurement);
        } else {
            otherEntries.push(measurement);
        }
    });
    const take = { left: false, right: false, both: false };
    let mode = '';
    if (entries.length === 1 && leftEntries.length === 1) {
        take.left = true;
        mode = 'left';
    } else if (entries.length === 1 && rightEntries.length === 1) {
        take.right = true;
        mode = 'right';
    } else if (entries.length === 2 && leftEntries.length === 1 && rightEntries.length === 1 && otherEntries.length === 0) {
        take.both = true;
        mode = 'both';
    }
    const warning = entries.length > 1 && !mode
        ? 'Select one measurement or one L/R selection.'
        : '';
    return {
        entries,
        count: entries.length,
        mode,
        take,
        warning,
        left: leftEntries[0] || null,
        right: rightEntries[0] || null,
    };
}

function getMeasurementConvolverMeasurementForSide(side = 'left') {
    const desired = side === 'right' ? 'right' : 'left';
    const selection = getMeasurementConvolverSourceSelectionState();
    if (selection.mode === 'both') return desired === 'right' ? selection.right : selection.left;
    if (selection.mode === desired) return desired === 'right' ? selection.right : selection.left;
    return null;
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
    const measurement = getMeasurementConvolverMeasurementForSide(side);
    const analysisSettings = {
        ...conv,
        correctionConfidence: measurement?.analysis?.hybrid_constraints || [],
    };
    return {
        side,
        points: points.length,
        ...MeasurementDsp.analyzeMeasurementConvolverCorrections(points, curve.points || measurementConvolverCurves.neutral.points, analysisSettings),
    };
}

function getMeasurementConvolverSelectedSourceCount() {
    return getMeasurementConvolverSourceSelectionState().count;
}

function getMeasurementConvolverMultiSourceWarning() {
    return MeasurementUI.getMeasurementConvolverMultiSourceWarning();
}

function buildMeasurementConvolverWarnings(analyses = []) {
    const conv = ensureMeasurementConvolverState();
    const warnings = [];
    const selectionWarning = getMeasurementConvolverSourceSelectionState().warning;
    if (selectionWarning) warnings.push(selectionWarning);
    if (analyses.some((analysis) => analysis && analysis.lowBassBoost)) warnings.push('Deep bass boost can demand much more amplifier power and speaker excursion.');
    if (measurementConvolverAlignedPhaseModes.includes(conv.phaseMode)) {
        const leftTiming = conv.draft?.left?.timing || getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('left'));
        const rightTiming = conv.draft?.right?.timing || getMeasurementDirectArrivalTiming(getMeasurementConvolverMeasurementForSide('right'));
        const safetyMessage = getMeasurementConvolverTimingSafetyMessage(getMeasurementConvolverTimingDelta(leftTiming, rightTiming));
        if (safetyMessage) warnings.push('Filter not created because timing offset exceeds safety limit.');
    }
    return warnings;
}

function formatMeasurementConvolverGain(value) {
    return MeasurementUI.formatMeasurementConvolverGain(value);
}

function getMeasurementConvolverNameSuffix(date = new Date()) {
    return MeasurementUI.getMeasurementConvolverNameSuffix(date);
}

function getMeasurementConvolverItemName(mode = 'both', autoGainDb = 0, options = {}) {
    const conv = ensureMeasurementConvolverState();
    const curve = getMeasurementConvolverCurve(conv.targetCurve);
    const prefix = mode === 'both' ? 'Conv LR' : (mode === 'right' ? 'Conv R' : 'Conv L');
    const phaseMode = measurementConvolverPhaseModes.includes(String(options.phaseMode)) ? String(options.phaseMode) : conv.phaseMode;
    const phaseTag = getMeasurementConvolverPhaseTag(phaseMode);
    const base = `${prefix} ${phaseTag} ${curve.shortLabel || curve.label} ${Math.round(conv.rangeStartHz)}-${Math.round(conv.rangeEndHz)}Hz ${formatMeasurementConvolverGain(autoGainDb)}`;
    return options.unique ? `${base} ${getMeasurementConvolverNameSuffix()}` : base;
}

function getMeasurementConvolverPreviewMode(leftAnalysis, rightAnalysis, leftDraft = null, rightDraft = null) {
    return MeasurementUI.getMeasurementConvolverPreviewMode(leftAnalysis, rightAnalysis, leftDraft, rightDraft);
}

function getMeasurementConvolverPreviewGain(mode = 'both', leftAnalysis = null, rightAnalysis = null, leftDraft = null, rightDraft = null) {
    return MeasurementUI.getMeasurementConvolverPreviewGain(mode, leftAnalysis, rightAnalysis, leftDraft, rightDraft);
}

function showMeasurementConvolverFeedback(message) {
    if (!elements.measurementConvolverFeedback) return;
    elements.measurementConvolverFeedback.textContent = message;
    elements.measurementConvolverFeedback.classList.add('is-visible');
    setTimeout(() => elements.measurementConvolverFeedback?.classList.remove('is-visible'), 2600);
}

function waitForNextAnimationFrame() {
    return new Promise((resolve) => window.requestAnimationFrame(() => resolve()));
}

function getMeasurementConvolverSampleRate() {
    const selected = Number(state.measurement?.measurementSampleRate);
    return Number.isFinite(selected) && selected > 0 ? selected : 48000;
}


function getMeasurementConvolverTypeOption(type = 'linear_4096') {
    return MeasurementUI.getMeasurementConvolverTypeOption(type);
}

function getMeasurementConvolverTypeKeys() {
    return MeasurementUI.getMeasurementConvolverTypeKeys();
}

function getMeasurementConvolverPhaseModeForType(type = 'linear_4096') {
    return MeasurementUI.getMeasurementConvolverPhaseModeForType(type);
}

function getMeasurementConvolverPhaseLabel(phaseMode = 'linear') {
    return MeasurementUI.getMeasurementConvolverPhaseLabel(phaseMode);
}

function getMeasurementConvolverPhaseTag(phaseMode = 'linear') {
    return MeasurementUI.getMeasurementConvolverPhaseTag(phaseMode);
}

function getMeasurementConvolverFirLengthForType(type = 'linear_4096') {
    return MeasurementUI.getMeasurementConvolverFirLengthForType(type);
}

function getMeasurementConvolverFirLength() {
    const conv = ensureMeasurementConvolverState();
    return getMeasurementConvolverFirLengthForType(conv.quality);
}

function getMeasurementConvolverTypeLabel(type = 'linear_4096') {
    return MeasurementUI.getMeasurementConvolverTypeLabel(type);
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
    return MeasurementUI.getMeasurementConvolverTimingMs(timing);
}

function getMeasurementConvolverTimingDelta(leftTiming, rightTiming) {
    return MeasurementUI.getMeasurementConvolverTimingDelta(leftTiming, rightTiming);
}

function getMeasurementConvolverTimingPairDebug(timingDelta) {
    const leftTiming = timingDelta?.leftTiming || {};
    const rightTiming = timingDelta?.rightTiming || {};
    const pendingIds = new Set((state.measurement.pendingRepeatMeasurements || []).map((measurement) => String(measurement?.id || '')));
    const samePendingRepeatResult = !!leftTiming.measurementId
        && !!rightTiming.measurementId
        && pendingIds.has(String(leftTiming.measurementId))
        && pendingIds.has(String(rightTiming.measurementId));
    const leftRepeatSummary = leftTiming.measurementKind === 'lr-repeat-summary';
    const rightRepeatSummary = rightTiming.measurementKind === 'lr-repeat-summary';
    const inferredSameSavedRepeatResult = leftRepeatSummary
        && rightRepeatSummary
        && !!leftTiming.repeatPairKey
        && leftTiming.repeatPairKey === rightTiming.repeatPairKey;
    const sameLrRepeatResult = samePendingRepeatResult || inferredSameSavedRepeatResult;
    return {
        left: {
            measurementId: leftTiming.measurementId || '',
            measurementName: leftTiming.measurementName || '',
            channel: leftTiming.channel || 'left',
            correctedArrivalMs: getMeasurementConvolverTimingMs(leftTiming),
        },
        right: {
            measurementId: rightTiming.measurementId || '',
            measurementName: rightTiming.measurementName || '',
            channel: rightTiming.channel || 'right',
            correctedArrivalMs: getMeasurementConvolverTimingMs(rightTiming),
        },
        calculatedDeltaMs: timingDelta?.deltaMs ?? null,
        displayedAbsDeltaMs: timingDelta?.absMs ?? null,
        sameIntendedLrPair: sameLrRepeatResult ? true : 'unknown',
        sameLrRepeatResult,
        pairEvidence: samePendingRepeatResult
            ? 'current-pending-lr-repeat-result'
            : (inferredSameSavedRepeatResult ? 'saved-lr-repeat-summary-name-and-timestamp' : 'no-explicit-pair-metadata'),
    };
}

function formatMeasurementConvolverTimingRelation(timingDelta) {
    if (!timingDelta) return 'Timing unavailable';
    const earlierSide = timingDelta.laterSide === 'R' ? 'L' : 'R';
    const line = `${timingDelta.laterSide} arrives ${timingDelta.absMs.toFixed(2)} ms later than ${earlierSide}`;
    console.info('[measurement-convolver-visible-lr-delta]', {
        line,
        ...getMeasurementConvolverTimingPairDebug(timingDelta),
    });
    return line;
}

function getMeasurementConvolverTimingSafetyMessage(timingDelta, limitMs = MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
    return MeasurementUI.getMeasurementConvolverTimingSafetyMessage(timingDelta, limitMs);
}

function alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, leftTiming, rightTiming, maxAlignMs = MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
    return MeasurementUI.alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, leftTiming, rightTiming, maxAlignMs);
}

function getMeasurementAnalysisSampleRate(measurement = {}) {
    return MeasurementUI.getMeasurementAnalysisSampleRate(measurement);
}

function getMeasurementDirectArrivalTiming(measurement = {}) {
    return MeasurementUI.getMeasurementDirectArrivalTiming(measurement);
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
    const phaseMode = measurementConvolverPhaseModes.includes(options.phaseMode) ? options.phaseMode : ensureMeasurementConvolverState().phaseMode;
    const filenameBase = itemName.replace(/[^a-z0-9._-]+/gi, '-').replace(/^-+|-+$/g, '') || 'measurement-convolver';
    const bySide = Object.fromEntries(analyses.map((analysis) => [analysis.side, analysis]));
    const hybridTransition = typeof MeasurementDsp.getMeasurementConvolverHybridTransition === 'function'
        ? MeasurementDsp.getMeasurementConvolverHybridTransition()
        : { hybridMinHz: null, hybridLinearHz: null };
    console.debug('[measurement-convolver-fir-generation]', {
        phaseMode,
        hybridMinHz: hybridTransition.hybridMinHz,
        hybridLinearHz: hybridTransition.hybridLinearHz,
        taps: length,
        rangeStart: Number.isFinite(Number(options.rangeStartHz)) ? Number(options.rangeStartHz) : null,
        rangeEnd: Number.isFinite(Number(options.rangeEndHz)) ? Number(options.rangeEndHz) : null,
    });
    if (mode === 'both') {
        const applyPhaseMode = phaseMode === 'minimum_aligned' ? 'minimum' : phaseMode;
        const leftImpulse = buildMeasurementConvolverImpulse(bySide.left, sampleRate, length, sharedAutoGainDb, applyPhaseMode);
        const rightImpulse = buildMeasurementConvolverImpulse(bySide.right, sampleRate, length, sharedAutoGainDb, applyPhaseMode);
        const [finalLeft, finalRight] = measurementConvolverAlignedPhaseModes.includes(phaseMode)
            ? alignStereoImpulsesForMinimumAligned(leftImpulse, rightImpulse, sampleRate, options.leftTiming || null, options.rightTiming || null, Number(options.timingSafetyLimitMs) || MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS)
            : [leftImpulse, rightImpulse];
        const leftBlob = writeMeasurementConvolverWav([finalLeft], sampleRate);
        const rightBlob = writeMeasurementConvolverWav([finalRight], sampleRate);
        const formData = new FormData();
        formData.append('preset_name', itemName);
        appendMeasurementConvolverExtras(formData);
        formData.append('left_file', leftBlob, `${filenameBase}-L.wav`);
        formData.append('right_file', rightBlob, `${filenameBase}-R.wav`);
        const resp = await fetch('/api/dsp/presets/import-filter-dual', { method: 'POST', body: formData });
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
    const resp = await fetch('/api/dsp/presets/create-with-ir', { method: 'POST', body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || 'Convolver preset creation failed');
    return data;
}

function takeMeasurementConvolverToDraft(mode = 'both') {
    const selection = getMeasurementConvolverSourceSelectionState();
    if (!selection.take[mode]) {
        const warning = selection.warning || getMeasurementConvolverMultiSourceWarning();
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
    if (measurementConvolverAlignedPhaseModes.includes(conv.phaseMode) && sides.length === 2) {
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
            phaseMode: conv.phaseMode,
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
    conv.draft.notice = '';
    const sharedAutoGainDb = Math.min(...analyses.map((analysis) => analysis.autoGainDb));
    const effectiveMode = conv.draft.left && conv.draft.right ? 'both' : mode;
    if (!conv.draft.nameTouched) conv.draft.presetName = getMeasurementConvolverItemName(effectiveMode, sharedAutoGainDb, { unique: true, phaseMode: conv.phaseMode });
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
    const phaseMismatch = getMeasurementConvolverDraftPhaseMismatch(conv);
    if (phaseMismatch) {
        conv.draft.notice = 'Draft phase does not match the selected phase type. Take L/R again.';
        showMeasurementConvolverFeedback(conv.draft.notice);
        showToast(conv.draft.notice, 'warning');
        renderMeasurementPanel();
        return;
    }
    if (mode === 'both') {
        const [leftMeta, rightMeta] = drafts.map((draft) => draft?.metadata || {});
        const sameGeneration = ['sampleRate', 'quality', 'phaseMode', 'irLength'].every((key) => String(leftMeta[key] || '') === String(rightMeta[key] || ''));
        if (!sameGeneration) {
            showMeasurementConvolverFeedback('Retake L/R with matching Convolver type and sample rate');
            showToast('Left and Right convolver drafts use different FIR settings. Retake Both for a matched comparison preset.', 'warning');
            return;
        }
        const draftPhaseMode = leftMeta.phaseMode || rightMeta.phaseMode || conv.phaseMode;
        if (measurementConvolverAlignedPhaseModes.includes(draftPhaseMode)) {
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
    conv.creatingPreset = true;
    showMeasurementConvolverFeedback('Creating convolver preset...');
    renderMeasurementPanel();
    await waitForNextAnimationFrame();
    try {
        const draftMetadata = drafts[0]?.metadata || {};
        const generationOptions = {
            sampleRate: draftMetadata.sampleRate || getMeasurementConvolverSampleRate(),
            quality: draftMetadata.quality || conv.quality,
            phaseMode: draftMetadata.phaseMode || conv.phaseMode,
            irLength: draftMetadata.irLength || getMeasurementConvolverFirLength(),
            rangeStartHz: draftMetadata.rangeStartHz || conv.rangeStartHz,
            rangeEndHz: draftMetadata.rangeEndHz || conv.rangeEndHz,
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
        state.dsp = state.dsp || {};
        state.dsp.assistStack = state.dsp.assistStack || [];
        state.dsp.assistStack.push(item);
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
        conv.creatingPreset = false;
        renderMeasurementPanel();
    }
}

function getMeasurementGraphBounds(displayWidth, displayHeight) {
    return MeasurementUI.getMeasurementGraphBounds(displayWidth, displayHeight);
}

function getMeasurementGraphDisplaySize(canvas) {
    return MeasurementUI.getMeasurementGraphDisplaySize(canvas);
}

function getMeasurementGraphRenderContext() {
    const canvas = elements.measurementGraph;
    if (!canvas) return null;
    const displaySize = getMeasurementGraphDisplaySize(canvas);
    if (!displaySize.width || !displaySize.height) return null;
    const displayWidth = displaySize.width;
    const displayHeight = displaySize.height;
    const bounds = getMeasurementGraphBounds(displayWidth, displayHeight);
    const range = getMeasurementGraphRange(getGraphMeasurementEntries());
    return { canvas, displayWidth, displayHeight, bounds, range };
}

function getMeasurementGraphView() {
    return state.measurement?.measurementView === 'ir' ? 'ir' : 'freq';
}

function getMeasurementIrPreviewPoints(measurement = {}) {
    return MeasurementUI.getMeasurementIrPreviewPoints(measurement);
}

function buildMeasurementIrGraphEntry(measurement = {}, { current = false, graphColor = '' } = {}) {
    const displayTraces = getMeasurementDisplayTraces(measurement)
        .filter(trace => String(trace.kind || 'measured') !== 'target' && String(trace.kind || '').indexOf('filter') === -1);
    if (!displayTraces.length) return null;
    const points = getMeasurementIrPreviewPoints(measurement);
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

function getMeasurementIrPeakAbs(points = [], minMs = -0.5, maxMs = 0.5) {
    return MeasurementUI.getMeasurementIrPeakAbs(points, minMs, maxMs);
}

function getMeasurementIrWindowRms(points = [], minMs = 5, maxMs = 30) {
    return MeasurementUI.getMeasurementIrWindowRms(points, minMs, maxMs);
}

function getMeasurementIrStrongestAbs(points = [], minMs = 0.5, maxMs = 10) {
    return MeasurementUI.getMeasurementIrStrongestAbs(points, minMs, maxMs);
}

function formatMeasurementIrDb(valueDb) {
    return MeasurementUI.formatMeasurementIrDb(valueDb);
}

function formatMeasurementIrMs(valueMs) {
    return MeasurementUI.formatMeasurementIrMs(valueMs);
}

function formatMeasurementIrAmplitude(value) {
    return MeasurementUI.formatMeasurementIrAmplitude(value);
}

function getMeasurementIrDiagnostics(entry = {}) {
    return MeasurementUI.getMeasurementIrDiagnostics(entry);
}

function buildMeasurementIrDiagnostics(graphEntries = [], frequencyView = true) {
    return MeasurementUI.buildMeasurementIrDiagnostics(graphEntries, frequencyView);
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

function buildMeasurementIrSummary(diagnostics = []) {
    return MeasurementUI.buildMeasurementIrSummary(diagnostics);
}

function buildMeasurementIrDiagnosticsTooltip(diagnostics = []) {
    return MeasurementUI.buildMeasurementIrDiagnosticsTooltip(diagnostics);
}

function renderMeasurementIrDiagnostics(graphEntries = [], frequencyView = true) {
    if (!elements.measurementIrDiagnostics) return;
    const diagnostics = buildMeasurementIrDiagnostics(graphEntries, frequencyView);
    const tooltip = buildMeasurementIrDiagnosticsTooltip(diagnostics);
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

function formatMeasurementHoverFrequency(frequencyHz) {
    return MeasurementUI.formatMeasurementHoverFrequency(frequencyHz);
}

function formatMeasurementHoverDb(valueDb) {
    return MeasurementUI.formatMeasurementHoverDb(valueDb);
}

function getMeasurementTraceDisplayedDbAtFrequency(points = [], frequencyHz = 1000) {
    return MeasurementUI.getMeasurementTraceDisplayedDbAtFrequency(points, frequencyHz);
}

function getMeasurementFrequencyHoverTooltip(event) {
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return '';
    const { x, y, bounds, range } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return '';
    const frequencyHz = measurementXToFrequency(x, bounds);
    const graphEntries = getGraphMeasurementEntries();
    const candidates = [];
    graphEntries.forEach((entry) => {
        (entry.traces || []).forEach((trace) => {
            const levelDb = getMeasurementTraceDisplayedDbAtFrequency(trace.points || [], frequencyHz);
            if (!Number.isFinite(levelDb)) return;
            const traceY = Math.max(bounds.top, Math.min(bounds.top + bounds.height, measurementDbToY(levelDb, bounds, range)));
            candidates.push({ levelDb, distancePx: Math.abs(traceY - y) });
        });
    });
    candidates.sort((a, b) => a.distancePx - b.distancePx);
    const candidate = candidates[0] || null;
    if (!candidate) return '';
    return `${formatMeasurementHoverFrequency(frequencyHz)} · ${formatMeasurementHoverDb(candidate.levelDb)}`;
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

function getCustomHouseCurvePointSlot(pointId) {
    const custom = ensureCustomHouseCurveState();
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
        x: measurementFrequencyToX(point.freqHz || 20, bounds),
        y: Math.max(bounds.top, Math.min(bounds.top + bounds.height, measurementDbToY(point.gainDb || 0, bounds, range))),
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
    const points = ensureCustomHouseCurveState().points;
    for (let index = points.length - 1; index >= 0; index -= 1) {
        const point = points[index];
        const handle = getCustomHouseCurveHandlePosition(point, bounds, range);
        if (Math.hypot(handle.x - x, handle.y - y) <= hitRadius) return point;
    }
    return null;
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
        const x = measurementFrequencyToX(frequency, bounds);
        const levelDb = MeasurementDsp.getMeasurementConvolverCurveDbFromPoints(points, frequency);
        const y = Math.max(bounds.top, Math.min(bounds.top + bounds.height, measurementDbToY(levelDb, bounds, range)));
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

function drawCustomHouseCurveHandles(ctx, bounds, range) {
    if (getMeasurementActiveEditor() !== 'houseCurve') return;
    const custom = ensureCustomHouseCurveState();
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

function measurementIrTimeToX(timeMs, bounds) {
    return MeasurementUI.measurementIrTimeToX(timeMs, bounds);
}

function measurementXToIrTime(x, bounds) {
    return MeasurementUI.measurementXToIrTime(x, bounds);
}

function measurementIrAmplitudeToY(amplitude, bounds) {
    return MeasurementUI.measurementIrAmplitudeToY(amplitude, bounds);
}

function getNearestMeasurementIrPoint(points = [], targetTimeMs = 0) {
    return MeasurementUI.getNearestMeasurementIrPoint(points, targetTimeMs);
}

function getMeasurementIrHoverTooltip(event) {
    const pointer = getMeasurementGraphPointerPosition(event);
    if (!pointer) return '';
    const { x, y, bounds } = pointer;
    if (x < bounds.left || x > bounds.left + bounds.width || y < bounds.top || y > bounds.top + bounds.height) return '';
    const targetTimeMs = measurementXToIrTime(x, bounds);
    const graphEntries = getGraphMeasurementEntries();
    const candidates = graphEntries.map((entry) => {
        const trace = (entry.traces || [])[0] || {};
        const nearest = getNearestMeasurementIrPoint(trace.points || [], targetTimeMs);
        if (!nearest) return null;
        const pointX = measurementIrTimeToX(nearest.timeMs, bounds);
        const pointY = measurementIrAmplitudeToY(nearest.amplitude, bounds);
        return {
            nearest,
            distancePx: Math.hypot(pointX - x, pointY - y),
        };
    }).filter(Boolean).sort((a, b) => a.distancePx - b.distancePx);
    const candidate = candidates[0] || null;
    if (!candidate) return '';
    const { nearest } = candidate;
    return `${formatMeasurementIrMs(nearest.timeMs)} · amp ${formatMeasurementIrAmplitude(nearest.amplitude)}`;
}

function drawMeasurementIrGraph(ctx, bounds, graphEntries) {
    ctx.strokeStyle = 'rgba(255,255,255,0.08)';
    ctx.lineWidth = 1;
    [-1, -0.5, 0, 0.5, 1].forEach((amplitude) => {
        const y = measurementIrAmplitudeToY(amplitude, bounds);
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
        const x = measurementIrTimeToX(timeMs, bounds);
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
                const x = measurementIrTimeToX(timeMs, bounds);
                const y = measurementIrAmplitudeToY(amplitude, bounds);
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
        const custom = ensureCustomHouseCurveState();
        const hitPoint = findCustomHouseCurveHandleAtPosition(x, y, bounds, range, pointerType);
        const point = hitPoint || addCustomHouseCurvePointAtPosition({ x, y, bounds, range });
        if (!point) return;
        if (pointerType === 'touch') event.preventDefault();
        custom.activePointId = point.id;
        custom.dragPointId = point.id;
        measurementGraphPointerId = event.pointerId;
        elements.measurementGraph?.setPointerCapture?.(event.pointerId);
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
        return;
    }
    if (getMeasurementActiveEditor() === 'houseCurve') return;
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
    if (getMeasurementGraphView() === 'ir') {
        if (elements.measurementGraph) {
            elements.measurementGraph.title = getMeasurementIrHoverTooltip(event);
        }
        return;
    }
    if (elements.measurementGraph) {
        elements.measurementGraph.title = getMeasurementFrequencyHoverTooltip(event);
    }
    const custom = ensureCustomHouseCurveState();
    if (custom.dragPointId && measurementGraphPointerId === event.pointerId && getMeasurementActiveEditor() === 'houseCurve') {
        if (event.pointerType === 'touch') event.preventDefault();
        const pointer = getMeasurementGraphPointerPosition(event);
        if (!pointer) return;
        const point = custom.points.find((item) => item.id === custom.dragPointId);
        if (!point) return;
        const visibleFrequency = measurementXToFrequency(pointer.x, pointer.bounds);
        const visibleGain = Math.min(pointer.range.maxDb, Math.max(pointer.range.minDb, measurementYToDb(pointer.y, pointer.bounds, pointer.range)));
        updateCustomHouseCurvePoint(point.id, { freqHz: visibleFrequency, gainDb: visibleGain });
        scheduleMeasurementGraphRender();
        renderMeasurementPanel();
        return;
    }
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

function handleMeasurementGraphPointerLeave() {
    if (elements.measurementGraph) elements.measurementGraph.title = '';
}

function handleMeasurementGraphPointerUp(event) {
    if (getMeasurementGraphView() === 'ir') return;
    const peq = ensureMeasurementPeqState();
    const custom = ensureCustomHouseCurveState();
    const conv = ensureMeasurementConvolverState();
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId && event.pointerType === 'touch') {
        event.preventDefault();
    }
    if (measurementGraphPointerId !== null && event.pointerId === measurementGraphPointerId) {
        elements.measurementGraph?.releasePointerCapture?.(event.pointerId);
        measurementGraphPointerId = null;
    }
    peq.dragFilterId = null;
    custom.dragPointId = null;
    conv.dragMode = null;
    delete conv.dragAnchorHz;
    delete conv.dragStartHz;
    delete conv.dragEndHz;
}

function buildMeasurementGraphEntry(measurement = {}, { current = false, graphColor = '' } = {}) {
    return MeasurementGraph.buildMeasurementGraphEntry(measurement, { current, graphColor });
}

function getGraphMeasurementEntries() {
    return MeasurementGraph.getGraphMeasurementEntries();
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

async function downloadSelectedMeasurementCalibration() {
    const calibrationId = state.measurement.selectedCalibrationRef || '';
    const selected = (state.measurement.calibrationOptions || []).find(option => option.id === calibrationId);
    if (!calibrationId || !selected) return;
    state.measurement.calibrationExporting = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch(`/api/measurements/calibrations/${encodeURIComponent(calibrationId)}/export`);
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Failed to export calibration file');
        }
        triggerBlobDownload(await resp.blob(), getDownloadFilenameFromResponse(resp, selected.filename || 'calibration.txt'));
    } catch (error) {
        console.error('downloadSelectedMeasurementCalibration failed', error);
        state.measurement.statusText = error.message || 'Failed to export calibration file';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.calibrationExporting = false;
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

function ensureCustomHouseCurveState() {
    const measurement = state.measurement || (state.measurement = {});
    if (!measurement.customHouseCurve || typeof measurement.customHouseCurve !== 'object') {
        measurement.customHouseCurve = { open: false, displayTarget: 'actual', points: [], activePointId: null, dragPointId: null, name: '', nameTouched: false, saving: false };
    }
    const custom = measurement.customHouseCurve;
    custom.displayTarget = custom.displayTarget === 'editing-custom-house-curve' ? custom.displayTarget : 'actual';
    if (!Array.isArray(custom.points)) custom.points = [];
    const usedSlots = new Set();
    custom.points = custom.points.slice(0, 8);
    custom.points.forEach((point) => {
        let slot = Number(point?.slot);
        if (!Number.isInteger(slot) || slot < 0 || slot >= 8 || usedSlots.has(slot)) {
            slot = Array.from({ length: 8 }, (_, candidate) => candidate).find((candidate) => !usedSlots.has(candidate));
        }
        usedSlots.add(slot);
        point.slot = slot;
    });
    if (!custom.points.some((point) => point.id === custom.activePointId)) custom.activePointId = custom.points[0]?.id || null;
    if (!custom.points.some((point) => point.id === custom.dragPointId)) custom.dragPointId = null;
    return custom;
}

function getCustomHouseCurveNameSuggestion() {
    const normalize = (value) => String(value || '').trim().toLowerCase().replace(/[-_.]+/g, ' ').replace(/\s+/g, ' ');
    const used = new Set((state.measurement?.houseCurveOptions || []).map((curve) => normalize(curve.filename)));
    let index = 1;
    while (used.has(normalize('Custom House Curve ' + index))) index += 1;
    return 'Custom House Curve ' + index;
}

function openCustomHouseCurveEditor() {
    const custom = ensureCustomHouseCurveState();
    setMeasurementActiveEditor('houseCurve');
    if (!custom.points.length) addCustomHouseCurvePoint({ slot: 0, freqHz: 20, gainDb: 0 });
    if (!custom.nameTouched || !custom.name.trim()) custom.name = getCustomHouseCurveNameSuggestion();
    renderMeasurementPanel();
    scheduleMeasurementGraphRender();
}

function handleMeasurementTargetCurveSelection(value) {
    if (value === 'create-custom-house-curve' || value === 'editing-custom-house-curve') {
        openCustomHouseCurveEditor();
        return;
    }
    setMeasurementActiveEditor('none');
    updateMeasurementConvolverField('targetCurve', value);
    renderMeasurementPanel();
    scheduleMeasurementGraphRender();
}

function addCustomHouseCurvePoint(defaults = {}) {
    const custom = ensureCustomHouseCurveState();
    const fallbackFrequencies = [20, 40, 80, 160, 320, 1000, 5000, 20000];
    const requestedSlot = Number(defaults.slot);
    const freeSlots = Array.from({ length: 8 }, (_, slot) => slot).filter((slot) => !custom.points.some((point) => Number(point.slot) === slot));
    const slot = Number.isInteger(requestedSlot) && requestedSlot >= 0 && requestedSlot < 8 && freeSlots.includes(requestedSlot)
        ? requestedSlot
        : freeSlots[0];
    if (slot === undefined) return null;
    const point = {
        id: 'house-point-' + Date.now() + '-' + Math.random().toString(16).slice(2),
        slot,
        freqHz: Math.round(Math.min(20000, Math.max(20, Number(defaults.freqHz) || fallbackFrequencies[slot]))),
        gainDb: Number(Math.min(24, Math.max(-24, Number(defaults.gainDb) || 0)).toFixed(1)),
    };
    custom.points.push(point);
    custom.activePointId = point.id;
    return point;
}

function resetCustomHouseCurveDraft() {
    const custom = ensureCustomHouseCurveState();
    custom.points = [];
    custom.activePointId = null;
    custom.dragPointId = null;
    addCustomHouseCurvePoint({ slot: 0, freqHz: 20, gainDb: 0 });
}

function updateCustomHouseCurvePoint(pointId, updates = {}) {
    const point = ensureCustomHouseCurveState().points.find((item) => item.id === pointId);
    if (!point) return;
    if (updates.freqHz !== undefined) point.freqHz = Math.round(Math.min(20000, Math.max(20, Number(updates.freqHz) || 20)));
    if (updates.gainDb !== undefined) point.gainDb = Number(Math.min(24, Math.max(-24, Number(updates.gainDb) || 0)).toFixed(1));
}

function addCustomHouseCurvePointAtPosition({ x, y, bounds, range }) {
    const frequencyHz = measurementXToFrequency(x, bounds);
    const gainDb = Math.min(range.maxDb, Math.max(range.minDb, measurementYToDb(y, bounds, range)));
    return addCustomHouseCurvePoint({ freqHz: frequencyHz, gainDb: gainDb });
}

function deleteCustomHouseCurvePoint(pointId) {
    const custom = ensureCustomHouseCurveState();
    custom.points = custom.points.filter((point) => point.id !== pointId);
    custom.activePointId = custom.points[0]?.id || null;
}

function serializeCustomHouseCurvePoints(points) {
    return points
        .map((point) => [Number(point.freqHz), Number(point.gainDb)])
        .sort((left, right) => left[0] - right[0])
        .map(([frequency, gain]) => String(frequency) + '\t' + gain.toFixed(1))
        .join('\n') + '\n';
}

async function createCustomHouseCurve() {
    const custom = ensureCustomHouseCurveState();
    const name = String(custom.name || '').trim();
    if (!name || custom.points.length < 2 || custom.saving) return;
    const sorted = custom.points.map((point) => [Number(point.freqHz), Number(point.gainDb)]).sort((left, right) => left[0] - right[0]);
    if (sorted.some((point, index) => index && point[0] <= sorted[index - 1][0])) {
        showToast('House curve frequencies must be unique', 'warning');
        return;
    }
    custom.saving = true;
    renderMeasurementPanel();
    const safeFilename = name + '.txt';
    const file = new File([serializeCustomHouseCurvePoints(custom.points)], safeFilename, { type: 'text/plain' });
    const formData = new FormData();
    formData.append('house_curve_file', file);
    try {
        const resp = await fetch('/api/measurements/house-curves', { method: 'POST', body: formData });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to create target curve');
        applyMeasurementHouseCurveState(data);
        const uploadedKey = data.uploaded_house_curve_id ? 'house:' + data.uploaded_house_curve_id : '';
        if (uploadedKey) updateMeasurementConvolverField('targetCurve', uploadedKey);
        setMeasurementActiveEditor('none');
        custom.points = [];
        custom.activePointId = null;
        custom.name = '';
        custom.nameTouched = false;
        showToast('Target curve created and selected', 'success');
    } catch (error) {
        state.measurement.statusText = error.message || 'Failed to create target curve';
        showToast(state.measurement.statusText, 'error');
    } finally {
        custom.saving = false;
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
    }
}

async function downloadSelectedMeasurementHouseCurve() {
    const houseCurveId = elements.measurementHouseCurveSelect ? (elements.measurementHouseCurveSelect.value || '') : '';
    const selected = (state.measurement.houseCurveOptions || []).find(option => option.id === houseCurveId);
    if (!houseCurveId || !selected) return;
    state.measurement.houseCurveExporting = true;
    renderMeasurementPanel();
    try {
        const resp = await fetch(`/api/measurements/house-curves/${encodeURIComponent(houseCurveId)}/export`);
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            throw new Error(data.detail || 'Failed to export house curve file');
        }
        triggerBlobDownload(await resp.blob(), getDownloadFilenameFromResponse(resp, `${selected.filename || 'house-curve'}.txt`));
    } catch (error) {
        console.error('downloadSelectedMeasurementHouseCurve failed', error);
        state.measurement.statusText = error.message || 'Failed to export house curve file';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.houseCurveExporting = false;
        renderMeasurementPanel();
    }
}

async function deleteSelectedMeasurementHouseCurve() {
    const houseCurveId = elements.measurementHouseCurveSelect ? (elements.measurementHouseCurveSelect.value || '') : '';
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
        const conv = ensureMeasurementConvolverState();
        if (conv.targetCurve === `house:${houseCurveId}`) conv.targetCurve = 'neutral';
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

function applyMeasurementSetupSettings(settings = {}, fields = null) {
    if (!settings || typeof settings !== 'object') return;
    const applies = (field) => !fields || fields.has(field);
    if (applies('selectedInputId') && Object.prototype.hasOwnProperty.call(settings, 'selectedInputId')) {
        state.measurement.selectedInputLegacyId = String(settings.selectedInputId || '');
    }
    if (applies('selectedInputKey') && Object.prototype.hasOwnProperty.call(settings, 'selectedInputKey')) {
        state.measurement.selectedInputKey = String(settings.selectedInputKey || '');
    }
    if ((applies('selectedInputId') || applies('selectedInputKey')) && Object.prototype.hasOwnProperty.call(settings, 'selectedInputConfigured')) {
        state.measurement.selectedInputConfigured = !!settings.selectedInputConfigured;
    }
    if (applies('selectedMicInputChannel') && Object.prototype.hasOwnProperty.call(settings, 'selectedMicInputChannel')) {
        state.measurement.selectedMicInputChannel = String(settings.selectedMicInputChannel || '1');
    }
    if (applies('selectedReferenceInputChannel') && Object.prototype.hasOwnProperty.call(settings, 'selectedReferenceInputChannel')) {
        state.measurement.selectedReferenceInputChannel = String(settings.selectedReferenceInputChannel || '');
    }
    if (applies('measurementSampleRate') && Object.prototype.hasOwnProperty.call(settings, 'measurementSampleRate')) {
        state.measurement.measurementSampleRate = String(settings.measurementSampleRate || '48000');
    }
    normalizeMeasurementInputChannelSelections();
}

async function saveMeasurementSetupSettings(patch = {}) {
    const revision = ++measurementSettingsRevision;
    try {
        const resp = await fetch('/api/measurements/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save measurement settings');
        if (revision === measurementSettingsRevision) {
            applyMeasurementSetupSettings(data.measurement_settings || {}, new Set(Object.keys(patch)));
        }
        renderMeasurementPanel();
    } catch (error) {
        console.error('saveMeasurementSetupSettings failed', error);
    }
}

async function fetchMeasurements() {
    const settingsRevision = measurementSettingsRevision;
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
        if (settingsRevision === measurementSettingsRevision) {
            applyMeasurementSetupSettings(data.measurement_settings || {});
        }
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

function applyMeasurementInputSelection(inputId) {
    state.measurement.selectedInputId = String(inputId || '');
    const selectedInput = getSelectedMeasurementInput();
    state.measurement.selectedInputKey = selectedInput?.persistentId || '';
    state.measurement.selectedInputConfigured = !!selectedInput;
    state.measurement.selectedInputUnavailable = false;
    normalizeMeasurementInputChannelSelections();
    void saveMeasurementSetupSettings({
        selectedInputId: state.measurement.selectedInputId,
        selectedInputKey: state.measurement.selectedInputKey,
        selectedMicInputChannel: state.measurement.selectedMicInputChannel || '1',
        selectedReferenceInputChannel: state.measurement.selectedReferenceInputChannel || '',
    });
    renderMeasurementPanel();
}

async function fetchMeasurementInputs() {
    state.measurement.inputsLoading = true;
    renderMeasurementPanel();
    const revisionAtStart = measurementSettingsRevision;
    try {
        const resp = await fetch('/api/measurements/inputs');
        if (!resp.ok) throw new Error('Failed to fetch measurement inputs');
        const data = await resp.json();
        const inputs = Array.isArray(data.inputs) && data.inputs.length
            ? data.inputs.map((input, index) => ({
                id: String(input.id || `input-${index + 1}`),
                label: String(input.label || input.id || `Input ${index + 1}`),
                note: String(input.note || ''),
                channels: Math.max(1, Number(input.channels || 1)),
                supportedRates: Array.isArray(input.supported_rates) ? input.supported_rates.map(Number).filter(rate => Number.isFinite(rate) && rate > 0) : [],
                measurementSampleRate: Number(input.measurement_sample_rate || input.sample_rate || 0),
                nodeName: String(input.node_name || ''),
                persistentId: String(input.persistent_id || ''),
            }))
            : [];
        const previousInputId = state.measurement.selectedInputId;
        const previousInputKey = state.measurement.selectedInputKey;
        const selection = data.selection && typeof data.selection === 'object' ? data.selection : {};
        // A settings save that started while this request was in flight means
        // the response reflects pre-change settings; its selection snapshot is
        // stale and must not clobber a deliberate re-selection.
        const selectionStale = measurementSettingsRevision !== revisionAtStart;
        state.measurement.inputs = inputs;
        state.measurement.hostCaptureAvailable = !!data.capture_available && !!inputs.length;
        state.measurement.captureAvailable = state.measurement.hostCaptureAvailable;
        state.measurement.modeNote = measurementModeNoteText();
        if (!selectionStale) {
            state.measurement.selectedInputId = String(selection.input_id || '');
            state.measurement.selectedInputKey = String(selection.persistent_id || previousInputKey || '');
            state.measurement.selectedInputConfigured = !!selection.configured;
            state.measurement.selectedInputUnavailable = !!selection.unavailable;
        }
        const selectedMeasurementInput = inputs.find(input => input.id === state.measurement.selectedInputId);
        if (selectedMeasurementInput?.measurementSampleRate > 0) {
            state.measurement.measurementSampleRate = String(selectedMeasurementInput.measurementSampleRate);
        }
        normalizeMeasurementInputChannelSelections();
        if (!selectionStale && !state.measurement.startInFlight && !state.measurement.activeJobId
            && !state.measurement.selectedInputUnavailable && state.measurement.hostCaptureAvailable) {
            state.measurement.statusText = describeMeasurementScope(data.scope_note);
        }
        if (!selectionStale && state.measurement.selectedInputId && (
            !state.measurement.selectedInputConfigured
            || previousInputId !== state.measurement.selectedInputId
            || previousInputKey !== state.measurement.selectedInputKey
        )) {
            state.measurement.selectedInputConfigured = true;
            void saveMeasurementSetupSettings({
                selectedInputId: state.measurement.selectedInputId,
                selectedInputKey: state.measurement.selectedInputKey,
            });
        }
    } catch (error) {
        console.error('fetchMeasurementInputs failed', error);
        state.measurement.inputs = [];
        state.measurement.hostCaptureAvailable = false;
        state.measurement.captureAvailable = false;
        state.measurement.modeNote = measurementModeNoteText();
        if (measurementSettingsRevision === revisionAtStart) {
            state.measurement.selectedInputId = '';
            state.measurement.selectedInputUnavailable = state.measurement.selectedInputConfigured;
            state.measurement.statusText = error.message || 'Failed to load capture inputs';
        }
    } finally {
        state.measurement.inputsLoading = false;
        renderMeasurementPanel();
    }
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
        startMeasurementWindowHeartbeat();
        measurementInputScanOnFocusDone = false;
        renderMeasurementPanel();
        void fetchMeasurementInputs();
        scheduleMeasurementGraphRender();
        window.FXRouteModal?.open(elements.measurementPanel, {
            initialFocus: elements.measurementCloseBtn,
            onEscape: () => toggleMeasurementPanel(false),
        });
    } else {
        stopMeasurementWindowHeartbeat();
        window.FXRouteModal?.close(elements.measurementPanel);
    }
}

function getSelectedMeasurementInput() {
    const measurementState = state.measurement || {};
    return (measurementState.inputs || []).find(input => input.id === measurementState.selectedInputId) || null;
}

function getSelectedMeasurementInputChannelCount() {
    return Math.max(1, Number(getSelectedMeasurementInput()?.channels || 1));
}

function normalizeMeasurementInputChannelSelections() {
    const measurementState = state.measurement || {};
    const selectedInput = getSelectedMeasurementInput();
    const channelCountKnown = !!selectedInput;
    const channelCount = channelCountKnown ? Math.max(1, Number(selectedInput.channels || 1)) : 1;
    const micChannel = Math.max(1, Math.min(channelCount, Number(measurementState.selectedMicInputChannel || 1)));
    measurementState.selectedMicInputChannel = String(micChannel);
    if (measurementState.selectedReferenceInputChannel) {
        const referenceChannel = Number(measurementState.selectedReferenceInputChannel);
        measurementState.selectedReferenceInputChannel = Number.isFinite(referenceChannel) && referenceChannel >= 1 && (!channelCountKnown || referenceChannel <= channelCount)
            ? String(referenceChannel)
            : '';
    }
    if (measurementState.selectedReferenceInputChannel === measurementState.selectedMicInputChannel) {
        measurementState.selectedReferenceInputChannel = '';
    }
}

function getMeasurementReferenceWarning() {
    const measurementState = state.measurement || {};
    if (!measurementState.selectedReferenceInputChannel) return '';
    if (measurementState.selectedReferenceInputChannel === measurementState.selectedMicInputChannel) {
        return 'Electrical reference disabled: mic and reference must use different input channels.';
    }
    return '';
}

function scheduleMeasurementGraphRender() {
    return MeasurementGraph.scheduleMeasurementGraphRender();
}

function scheduleMeasurementGraphRenderForResize() {
    return MeasurementGraph.scheduleMeasurementGraphRenderForResize();
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
    if (getMeasurementActiveEditor() !== 'peq') return;
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
    return MeasurementGraph.drawMeasurementGraph(elements.measurementGraph);
}

function summarizeMeasurementBand(summary = {}, fallbackLabel = 'No points') {
    return MeasurementUI.summarizeMeasurementBand(summary, fallbackLabel);
}

function formatMeasurementUpperLimit(maxHz) {
    return MeasurementUI.formatMeasurementUpperLimit(maxHz);
}

function summarizeMeasurementEntry(measurement = {}) {
    return MeasurementUI.summarizeMeasurementEntry(measurement);
}

function formatMeasurementQualityReason(item = {}) {
    return MeasurementUI.formatMeasurementQualityReason(item);
}

function getMeasurementQualitySummary(measurement = {}) {
    return MeasurementUI.getMeasurementQualitySummary(measurement);
}

function getMeasurementQualityTitle(measurement = {}) {
    return MeasurementUI.getMeasurementQualityTitle(measurement);
}

function formatSignedMeasurementMs(valueMs, digits = 2) {
    return MeasurementUI.formatSignedMeasurementMs(valueMs, digits);
}


function getMeasurementLrRepeatGlobalDeltaMs(measurement = {}, repeat = {}) {
    return MeasurementUI.getMeasurementLrRepeatGlobalDeltaMs(measurement, repeat);
}

function formatMeasurementLrRepeatDelta(measurement = {}, repeat = {}) {
    return MeasurementUI.formatMeasurementLrRepeatDelta(measurement, repeat);
}

function getMeasurementTimingInfo(measurement = {}) {
    return MeasurementUI.getMeasurementTimingInfo(measurement);
}

function sleep(ms) {
    return new Promise(resolve => window.setTimeout(resolve, ms));
}


function getMeasurementJobStatus(job = {}) {
    return MeasurementUI.getMeasurementJobStatus(job);
}

function normalizeMeasurementKind(kind) {
    return MeasurementUI.normalizeMeasurementKind(kind);
}

function getActiveMeasurementKind() {
    const measurementState = state.measurement || {};
    if (measurementState.autoSubInFlight) return 'auto_sub';
    const normalized = normalizeMeasurementKind(measurementState.activeMeasurementKind);
    if (normalized && measurementState.activeJobId) return normalized;
    if (measurementState.repeatJobActive && measurementState.activeJobId) return 'lr_repeat';
    if (measurementState.activeJobId) return 'single';
    return '';
}

function hasActiveMeasurementJob() {
    const measurementState = state.measurement || {};
    return !!(measurementState.activeJobId || measurementState.autoSubInFlight);
}

function getMeasurementJobResultMeasurement(job = {}) {
    return MeasurementUI.getMeasurementJobResultMeasurement(job);
}

function formatMeasurementInputLevelText(inputLevel = {}) {
    return MeasurementUI.formatMeasurementInputLevelText(inputLevel);
}

function formatMeasurementJobStatusText(job = {}, fallback = 'Measurement running…') {
    const message = String(job.message || fallback);
    const levelText = formatMeasurementInputLevelText(job.input_level);
    const isLrRepeat = job.job_kind === 'lr-repeat' || !!state.measurement?.repeatJobActive;
    if (!isLrRepeat || !levelText || /\b(CLIP|dBFS)\b/.test(message)) return message;
    return `${message} · ${levelText}`;
}

function syncMeasurementStartButtonFallback() {
    if (!elements.measurementStartBtn) return;
    const measurementState = state.measurement || {};
    const activeKind = getActiveMeasurementKind();
    const activeJobRunning = hasActiveMeasurementJob();
    const singleActive = activeKind === 'single';
    const lrActive = activeKind === 'lr_repeat';
    const autoSubActive = activeKind === 'auto_sub';
    const calibrationBusy = measurementState.calibrationUpdating || measurementState.calibrationDeleting;

    elements.measurementStartBtn.disabled = calibrationBusy
        ? true
        : (activeJobRunning ? !singleActive : (measurementState.inputsLoading || !measurementModeReady()));
    elements.measurementStartBtn.textContent = singleActive
        ? 'Cancel measurement'
        : (measurementState.startInFlight ? 'Starting...' : 'Start Single Sweep');
    if (elements.measurementRepeatStartBtn) {
        elements.measurementRepeatStartBtn.disabled = calibrationBusy
            ? true
            : (activeJobRunning ? !lrActive
            : (measurementState.startInFlight || measurementState.inputsLoading || !measurementModeReady()));
        elements.measurementRepeatStartBtn.textContent = lrActive
            ? 'Cancel measurement'
            : 'Start L/R Repeat';
    }
    syncAutoSubButton();
}

function syncSubwooferControlsDuringAutoSub() {
    return MeasurementFlows.syncSubwooferControlsDuringAutoSub();
}

function syncAutoSubButton() {
    return MeasurementFlows.syncAutoSubButton();
}

async function startAutoSubOptimize() {
    return MeasurementFlows.startAutoSubOptimize();
}

async function cancelAutoSubOptimize() {
    return MeasurementFlows.cancelAutoSubOptimize();
}

async function pollAutoSubJob(jobId) {
    return MeasurementFlows.pollAutoSubJob(jobId);
}

async function handleAutoSubResult(job) {
    return MeasurementFlows.handleAutoSubResult(job);
}

function renderMeasurementPanelDefensively(context = 'measurement render') {
    try {
        renderMeasurementPanel();
        return true;
    } catch (error) {
        console.error(`${context} failed`, error);
        state.measurement.activeJobId = '';
        state.measurement.startInFlight = false;
        state.measurement.statusText = error?.message
            ? `Measurement finished, but the result could not be rendered: ${error.message}`
            : 'Measurement finished, but the result could not be rendered.';
        syncMeasurementStartButtonFallback();
        showToast(state.measurement.statusText, 'error');
        return false;
    }
}



async function startHostMeasurement() {
    if (!state.measurement.hostCaptureAvailable || !state.measurement.selectedInputId) {
        state.measurement.statusText = 'No usable host capture source is available for a real measurement on this host.';
        renderMeasurementPanel();
        showToast(state.measurement.statusText, 'error');
        return;
    }

    await flushSubwooferSettingsBeforeMeasurement();

    const formData = new FormData();
    formData.append('input_id', state.measurement.selectedInputId);
    formData.append('input_key', state.measurement.selectedInputKey || '');
    formData.append('channel', state.measurement.selectedChannel || 'left');
    normalizeMeasurementInputChannelSelections();
    const referenceWarning = getMeasurementReferenceWarning();
    formData.append('mic_input_channel', state.measurement.selectedMicInputChannel || '1');
    formData.append('reference_input_channel', referenceWarning ? '' : (state.measurement.selectedReferenceInputChannel || ''));
    const calibrationFile = elements.measurementCalibrationFile?.files?.[0];
    if (calibrationFile) {
        formData.append('calibration_file', calibrationFile);
    } else if (state.measurement.selectedCalibrationRef) {
        formData.append('calibration_ref', state.measurement.selectedCalibrationRef);
    }

    state.measurement.activeJobId = '';
    state.measurement.activeMeasurementKind = 'single';
    state.measurement.pendingRepeatMeasurements = [];
    state.measurement.currentMeasurementSaved = false;
    state.measurement.statusText = 'Starting host-local sweep…';
    renderMeasurementPanel();
    void postRuntimeDebugSnapshot('ui-before-measurement-start', { measurementKind: 'single' });

    const resp = await fetch('/api/measurements/start', { method: 'POST', body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to start measurement'));
    const job = data.job || {};
    state.measurement.activeJobId = String(job.id || '');
    state.measurement.activeMeasurementKind = normalizeMeasurementKind(job.job_kind || 'single') || 'single';
    state.measurement.statusText = formatMeasurementJobStatusText(job, 'Preparing sweep…');
    renderMeasurementPanel();
    await pollMeasurementJob(state.measurement.activeJobId);
}

async function startLrRepeatMeasurement() {
    if (!state.measurement.hostCaptureAvailable || !state.measurement.selectedInputId) {
        state.measurement.statusText = 'No usable host capture source is available for an L/R repeat measurement on this host.';
        renderMeasurementPanel();
        showToast(state.measurement.statusText, 'error');
        return;
    }
    await flushSubwooferSettingsBeforeMeasurement();
    const formData = new FormData();
    formData.append('input_id', state.measurement.selectedInputId);
    formData.append('input_key', state.measurement.selectedInputKey || '');
    formData.append('base_name', state.measurement.currentMeasurementName || '');
    normalizeMeasurementInputChannelSelections();
    const referenceWarning = getMeasurementReferenceWarning();
    formData.append('mic_input_channel', state.measurement.selectedMicInputChannel || '1');
    formData.append('reference_input_channel', referenceWarning ? '' : (state.measurement.selectedReferenceInputChannel || ''));
    const calibrationFile = elements.measurementCalibrationFile?.files?.[0];
    if (calibrationFile) {
        formData.append('calibration_file', calibrationFile);
    } else if (state.measurement.selectedCalibrationRef) {
        formData.append('calibration_ref', state.measurement.selectedCalibrationRef);
    }
    state.measurement.activeJobId = '';
    state.measurement.activeMeasurementKind = 'lr_repeat';
    state.measurement.pendingRepeatMeasurements = [];
    state.measurement.repeatJobActive = true;
    state.measurement.statusText = 'Starting L/R repeat…';
    renderMeasurementPanel();
    void postRuntimeDebugSnapshot('ui-before-measurement-start', { measurementKind: 'lr-repeat' });
    const resp = await fetch('/api/measurements/lr-repeat/start', { method: 'POST', body: formData });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to start L/R repeat measurement'));
    const job = data.job || {};
    state.measurement.activeJobId = String(job.id || '');
    state.measurement.activeMeasurementKind = normalizeMeasurementKind(job.job_kind || 'lr-repeat') || 'lr_repeat';
    state.measurement.statusText = formatMeasurementJobStatusText(job, 'Preparing L/R repeat…');
    renderMeasurementPanel();
    await pollMeasurementJob(state.measurement.activeJobId);
}

function getHybridWizardState() {
    return MeasurementFlows.getHybridWizardState();
}

function getCurrentOutputModeName() {
    return MeasurementFlows.getCurrentOutputModeName();
}

function openHybridMeasurementWizard() {
    return MeasurementFlows.openHybridMeasurementWizard();
}

async function closeHybridMeasurementWizard() {
    return MeasurementFlows.closeHybridMeasurementWizard();
}

function renderHybridRoomDiagram(step = {}, mode = 'stereo', complete = false) {
    return MeasurementFlows.renderHybridRoomDiagram(step = {}, mode = 'stereo', complete = false);
}

function renderHybridMeasurementWizard() {
    return MeasurementFlows.renderHybridMeasurementWizard();
}

function buildHybridMeasurementForm(step) {
    return MeasurementFlows.buildHybridMeasurementForm(step);
}

function hybridSpeakerName(channel) {
    return MeasurementUI.hybridSpeakerName(channel);
}

async function runHybridWizardStep(step) {
    return MeasurementFlows.runHybridWizardStep(step);
}

async function cancelHybridWizardMeasurement() {
    return MeasurementFlows.cancelHybridWizardMeasurement();
}

async function runHybridWizardSweep() {
    return MeasurementFlows.runHybridWizardSweep();
}

function openHybridProfileInConvolver() {
    return MeasurementFlows.openHybridProfileInConvolver();
}

function setupHybridMeasurementWizard() {
    return MeasurementFlows.setupHybridMeasurementWizard();
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
    state.measurement.repeatJobActive = false;
    state.measurement.activeMeasurementKind = '';
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
        if (!state.measurement.activeJobId) {
            state.measurement.activeMeasurementKind = '';
            state.measurement.repeatJobActive = false;
        }
        renderMeasurementPanel();
    }
}

async function startLrRepeat() {
    if (state.measurement.startInFlight || state.measurement.activeJobId) return;
    if (!measurementModeReady()) {
        state.measurement.statusText = 'No usable host capture source is available for a real measurement on this host.';
        renderMeasurementPanel();
        showToast(state.measurement.statusText, 'error');
        return;
    }
    state.measurement.startInFlight = true;
    state.measurement.activeMeasurementKind = '';
    state.measurement.repeatJobActive = true;
    renderMeasurementPanel();
    try {
        await startLrRepeatMeasurement();
    } catch (error) {
        console.error('startLrRepeat failed', error);
        state.measurement.statusText = error.message || 'Failed to start L/R repeat measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        state.measurement.startInFlight = false;
        if (!state.measurement.activeJobId) {
            state.measurement.activeMeasurementKind = '';
            state.measurement.repeatJobActive = false;
        }
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
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to cancel measurement'));
        state.measurement.statusText = String(data.job?.message || 'Measurement cancelled.');
        if (MEASUREMENT_JOB_CANCELLED_STATES.has(getMeasurementJobStatus(data.job || {}))) {
            state.measurement.activeJobId = '';
            state.measurement.startInFlight = false;
            state.measurement.activeMeasurementKind = '';
            state.measurement.repeatJobActive = false;
            syncMeasurementStartButtonFallback();
        }
    } catch (error) {
        console.error('cancelMeasurement failed', error);
        state.measurement.statusText = error.message || 'Failed to cancel measurement';
        showToast(state.measurement.statusText, 'error');
    } finally {
        renderMeasurementPanelDefensively('measurement cancel render');
    }
}

async function pollMeasurementJob(jobId) {
    if (!jobId) return;
    for (let attempt = 0; attempt < 360; attempt += 1) {
        const resp = await fetch(`/api/measurements/jobs/${encodeURIComponent(jobId)}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to fetch measurement job'));
        const job = data.job || {};
        const jobStatus = getMeasurementJobStatus(job);
        state.measurement.statusText = formatMeasurementJobStatusText(job, state.measurement.statusText || 'Measurement running…');
        if (MEASUREMENT_JOB_SUCCESS_STATES.has(jobStatus)) {
            state.measurement.statusText = String(job.message || 'Measurement finished.');
            state.measurement.activeJobId = '';
            state.measurement.startInFlight = false;
            const repeatMeasurements = Array.isArray(job?.result?.measurements) ? job.result.measurements : [];
            state.measurement.activeMeasurementKind = '';
            state.measurement.repeatJobActive = false;
            syncMeasurementStartButtonFallback();
            await postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                jobId,
                jobStatus,
                measurementKind: repeatMeasurements.length ? 'lr-repeat' : 'single',
            });
            if (repeatMeasurements.length) {
                state.measurement.pendingRepeatMeasurements = repeatMeasurements.map((measurement, index) => normalizeMeasurementEntry(measurement, index));
                state.measurement.currentMeasurement = state.measurement.pendingRepeatMeasurements[0] || null;
                state.measurement.currentMeasurementName = String(job?.result?.base_name || '').trim();
                state.measurement.currentMeasurementSaved = false;
                state.measurement.pendingRepeatMeasurements.forEach((measurement) => {
                    state.measurement.reviewVisibilityById[measurement.id] = !!measurement.review_traces?.length;
                });
                state.measurement.statusText = String(job.message || 'L/R repeat finished.');
                renderMeasurementPanelDefensively('L/R repeat completion render');
                showToast('L/R repeat finished. Review and save when ready.', 'success');
                return;
            }
            const resultMeasurement = getMeasurementJobResultMeasurement(job);
            if (resultMeasurement) {
                try {
                    state.measurement.currentMeasurement = normalizeMeasurementEntry(resultMeasurement, 0);
                    state.measurement.currentMeasurementName = state.measurement.currentMeasurement.name || '';
                    state.measurement.currentMeasurementSaved = false;
                    state.measurement.reviewVisibilityById[state.measurement.currentMeasurement.id] = !!state.measurement.currentMeasurement.review_traces?.length;
                    const timingInfo = getMeasurementTimingInfo(state.measurement.currentMeasurement);
                    if (timingInfo.line) state.measurement.statusText = timingInfo.line;
                } catch (error) {
                    console.error('measurement result normalization failed', error, job);
                    state.measurement.statusText = error?.message
                        ? `Measurement finished, but the result could not be displayed: ${error.message}`
                        : 'Measurement finished, but the result could not be displayed.';
                    renderMeasurementPanelDefensively('measurement completion render after normalization failure');
                    showToast(state.measurement.statusText, 'error');
                    return;
                }
            } else {
                state.measurement.statusText = String(job.message || 'Measurement finished, but no result data was returned.');
            }
            const rendered = renderMeasurementPanelDefensively('measurement completion render');
            if (resultMeasurement && rendered) showToast('Measurement finished', 'success');
            if (!resultMeasurement) showToast(state.measurement.statusText, 'warning');
            return;
        }
        if (MEASUREMENT_JOB_FAILED_STATES.has(jobStatus)) {
            state.measurement.activeJobId = '';
            state.measurement.startInFlight = false;
            state.measurement.activeMeasurementKind = '';
            state.measurement.repeatJobActive = false;
            syncMeasurementStartButtonFallback();
            await postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                jobId,
                jobStatus,
                failed: true,
            });
            throw new Error(formatTransitionErrorDetail(job.error?.detail, job.message || 'Measurement failed'));
        }
        if (MEASUREMENT_JOB_CANCELLED_STATES.has(jobStatus)) {
            state.measurement.activeJobId = '';
            state.measurement.startInFlight = false;
            state.measurement.activeMeasurementKind = '';
            state.measurement.repeatJobActive = false;
            state.measurement.statusText = String(job.message || 'Measurement cancelled.');
            await postRuntimeDebugSnapshot('ui-directly-after-measurement-end', {
                jobId,
                jobStatus,
                cancelled: true,
            });
            renderMeasurementPanelDefensively('measurement cancellation render');
            showToast('Measurement cancelled', 'success');
            return;
        }
        state.measurement.activeJobId = String(job.id || jobId);
        renderMeasurementPanelDefensively('measurement polling render');
        await sleep(800);
    }
    state.measurement.activeJobId = '';
    state.measurement.startInFlight = false;
    state.measurement.activeMeasurementKind = '';
    state.measurement.repeatJobActive = false;
    syncMeasurementStartButtonFallback();
    throw new Error('Measurement job timed out while waiting for completion');
}

async function saveCurrentMeasurement() {
    const current = state.measurement.currentMeasurement;
    const autoSubMeasurements = Array.isArray(state.measurement.autoSubMeasurements)
        ? state.measurement.autoSubMeasurements
        : [];
    const hasAutoSub = autoSubMeasurements.length > 0;
    const hasPending = !hasAutoSub && current && !state.measurement.currentMeasurementSaved;
    if (!hasAutoSub && !hasPending) return;
    if (state.measurement.saveInFlight) return;

    const repeatMeasurements = Array.isArray(state.measurement.pendingRepeatMeasurements)
        ? state.measurement.pendingRepeatMeasurements
        : [];
    const baseName = hasAutoSub
        ? ((state.measurement.currentMeasurementName || '').trim() || 'AutoSub')
        : ((state.measurement.currentMeasurementName || '').trim() || 'L/R Repeat');
    let payload;
    if (hasAutoSub) {
        // Save AutoSub measurements: split each entry by its channel traces
        const measurements = [];
        autoSubMeasurements.forEach((measurement) => {
            const traces = Array.isArray(measurement.traces) ? measurement.traces : [];
            traces.forEach((trace) => {
                const ch = String(trace.role || trace.channel || '').toLowerCase() === 'right' ? 'right' : 'left';
                const traceLabel = String(trace.label || measurement.name || 'AutoSub');
                const suffix = traceLabel.replace(/^AutoSub\s+/, '');
                const name = suffix ? `${baseName} ${suffix}` : baseName;
                measurements.push({
                    id: `${measurement.id}-${ch}`,
                    name,
                    channel: ch,
                    traces: [{...trace, channel: ch}],
                });
            });
        });
        payload = { measurements };
    } else if (repeatMeasurements.length) {
        payload = {
            measurements: repeatMeasurements.map((measurement) => {
                const item = JSON.parse(JSON.stringify(measurement));
                item.name = `${baseName} · ${String(item.channel || '').toLowerCase() === 'right' ? 'R' : 'L'}`;
                return item;
            }),
        };
    } else {
        payload = JSON.parse(JSON.stringify(current));
        payload.name = (state.measurement.currentMeasurementName || current.name || '').trim() || current.name || 'Measurement';
    }

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
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to save measurement'));
        const savedMeasurements = Array.isArray(data.measurements)
            ? data.measurements.map((measurement, index) => normalizeMeasurementEntry(measurement, index))
            : [normalizeMeasurementEntry(data.measurement || payload, 0)];
        const saved = savedMeasurements[0];
        savedMeasurements.forEach((measurement) => {
            state.measurement.visibilityById[measurement.id] = true;
            state.measurement.reviewVisibilityById[measurement.id] = false;
        });
        state.measurement.currentMeasurement = null;
        state.measurement.pendingRepeatMeasurements = [];
        state.measurement.autoSubMeasurements = [];
        state.measurement.currentMeasurementSaved = false;
        state.measurement.currentMeasurementName = '';
        state.measurement.statusText = 'Measurement saved.';
        await fetchMeasurements();
        showToast('Measurement saved', 'success');
    } catch (error) {
        console.error('saveCurrentMeasurement failed', {
            message: error?.message || String(error),
            name: error?.name || '',
            error,
        });
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
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to delete measurement'));
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
            if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, `Failed to delete ${measurement.name}`));
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
        const responseText = await resp.text();
        let data = {};
        try {
            data = responseText ? JSON.parse(responseText) : {};
        } catch (_error) {
            data = {};
        }
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, responseText.trim() || 'Failed to merge selected measurements'));
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
    normalizeMeasurementInputChannelSelections();
    measurementState.modeNote = measurementModeNoteText();
    const current = getCurrentMeasurementEntry();
    const measurements = getSavedListMeasurements();
    const graphEntries = getGraphMeasurementEntries();
    const assistMode = measurementState.assistMode === 'convolver' ? 'convolver' : 'peq';
    const activeEditor = getMeasurementActiveEditor();
    const graphView = getMeasurementGraphView();
    const frequencyView = graphView === 'freq';
    const peq = ensureMeasurementPeqState();
    const conv = ensureMeasurementConvolverState();
    const activePeqFilter = getMeasurementPeqActiveFilter();

    const ctx = { measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter };
    renderMeasurementPanelSetupSection(ctx);
    renderMeasurementPanelInputsSection(ctx);
    renderMeasurementPanelCalibrationSection(ctx);
    renderMeasurementPanelHouseCurveSection(ctx);
    renderMeasurementPanelActionsSection(ctx);
    renderMeasurementPanelViewSection(ctx);
    renderMeasurementPanelStatusSection(ctx);
    renderMeasurementPanelEditorsSection(ctx);
    renderMeasurementPanelConvolverSection(ctx);
    renderMeasurementPanelSavedListSection(ctx);
    syncAutoSubButton();
    scheduleMeasurementGraphRender();
}

function renderMeasurementPanelSetupSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
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
        button.disabled = !frequencyView;
        button.title = frequencyView ? '' : 'Only available in frequency view.';
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    document.querySelectorAll('[data-measurement-view]').forEach((button) => {
        const active = (button.getAttribute('data-measurement-view') || 'freq') === graphView;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
}

function renderMeasurementPanelInputsSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
    if (elements.measurementInputGroup) {
        elements.measurementInputGroup.classList.remove('hidden');
    }
    if (elements.measurementInputSelect && !isSelectFocused(elements.measurementInputSelect)) {
        const availableInputs = measurementState.inputs && measurementState.inputs.length
            ? measurementState.inputs
            : [{ id: '', label: measurementState.inputsLoading ? 'Loading…' : 'No host capture inputs available' }];
        const inputs = measurementState.selectedInputUnavailable
            ? [{ id: '', label: 'Previously selected microphone (unavailable)' }, ...availableInputs]
            : availableInputs;
        elements.measurementInputSelect.innerHTML = inputs.map(input => `<option value="${escapeHtml(input.id)}" ${input.id === measurementState.selectedInputId ? 'selected' : ''}>${escapeHtml(input.label)}</option>`).join('');
        elements.measurementInputSelect.disabled = measurementState.inputsLoading || !measurementState.hostCaptureAvailable;
    }
    if (elements.measurementConvolverSampleRate && !isSelectFocused(elements.measurementConvolverSampleRate)) {
        const selectedInput = getSelectedMeasurementInput();
        const rates = selectedInput?.supportedRates?.length ? selectedInput.supportedRates : [48000];
        elements.measurementConvolverSampleRate.innerHTML = rates.map(rate => `<option value="${rate}">${formatRateKhz(rate)}</option>`).join('');
        elements.measurementConvolverSampleRate.value = String(measurementState.measurementSampleRate || 48000);
        elements.measurementConvolverSampleRate.disabled = measurementState.startInFlight || !measurementModeReady();
    }
    if (elements.measurementInputRefreshBtn) {
        elements.measurementInputRefreshBtn.disabled = measurementState.startInFlight || measurementState.inputsLoading;
        elements.measurementInputRefreshBtn.textContent = measurementState.inputsLoading ? 'Detecting…' : 'Detect / refresh host microphones';
    }
    const inputChannelCount = getSelectedMeasurementInputChannelCount();
    const inputChannelOptions = Array.from({ length: inputChannelCount }, (_, index) => String(index + 1));
    if (elements.measurementMicInputChannelSelect && !isSelectFocused(elements.measurementMicInputChannelSelect)) {
        elements.measurementMicInputChannelSelect.innerHTML = inputChannelOptions
            .map(value => `<option value="${value}" ${value === measurementState.selectedMicInputChannel ? 'selected' : ''}>Input ${value}</option>`)
            .join('');
        elements.measurementMicInputChannelSelect.disabled = measurementState.startInFlight || !measurementModeReady();
    }
    if (elements.measurementReferenceInputChannelSelect && !isSelectFocused(elements.measurementReferenceInputChannelSelect)) {
        const referenceOptions = [''].concat(inputChannelOptions);
        elements.measurementReferenceInputChannelSelect.innerHTML = referenceOptions
            .map(value => `<option value="${value}" ${value === (measurementState.selectedReferenceInputChannel || '') ? 'selected' : ''}>${value ? `Input ${value}` : 'None'}</option>`)
            .join('');
        elements.measurementReferenceInputChannelSelect.disabled = measurementState.startInFlight || !measurementModeReady();
    }
    if (elements.measurementReferenceWarning) {
        const referenceWarning = getMeasurementReferenceWarning();
        elements.measurementReferenceWarning.textContent = referenceWarning;
        elements.measurementReferenceWarning.classList.toggle('hidden', !referenceWarning);
    }
}

function renderMeasurementPanelCalibrationSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
    if (elements.measurementCalibrationSelect) {
        const options = [{ id: '', filename: 'No calibration file' }, ...(measurementState.calibrationOptions || [])];
        elements.measurementCalibrationSelect.innerHTML = options.map(option => `<option value="${escapeHtml(option.id || '')}" ${(option.id || '') === (measurementState.selectedCalibrationRef || '') ? 'selected' : ''}>${escapeHtml(option.filename || 'Calibration')}</option>`).join('');
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
    if (elements.measurementHouseCurveSelect) {
        const houseCurveOptions = measurementState.houseCurveOptions || [];
        const selectedHouseCurveId = String(conv.targetCurve || '').startsWith('house:') ? String(conv.targetCurve).slice(6) : '';
        const options = houseCurveOptions.length
            ? houseCurveOptions
            : [{ id: '', filename: 'Built-in target curves only' }];
        elements.measurementHouseCurveSelect.innerHTML = options.map(option => `<option value="${escapeHtml(option.id || '')}" ${(option.id || '') === selectedHouseCurveId ? 'selected' : ''}>${escapeHtml(option.filename || 'House curve')}</option>`).join('');
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
    if (elements.measurementNameInput) {
        elements.measurementNameInput.value = measurementState.currentMeasurementName || '';
        elements.measurementNameInput.disabled = measurementState.startInFlight || measurementState.saveInFlight || !!measurementState.activeJobId;
        elements.measurementNameInput.placeholder = 'Measurement name';
    }
    if (elements.measurementStartBtn) {
        const activeKind = getActiveMeasurementKind();
        const activeJobRunning = hasActiveMeasurementJob();
        elements.measurementStartBtn.disabled = measurementState.calibrationUpdating || measurementState.calibrationDeleting
            ? true
            : (activeJobRunning ? activeKind !== 'single' : (measurementState.inputsLoading || !measurementModeReady()));
        elements.measurementStartBtn.textContent = activeKind === 'single'
            ? 'Cancel measurement'
            : (measurementState.startInFlight ? 'Starting…' : 'Start Single Sweep');
    }
    if (elements.measurementRepeatStartBtn) {
        const activeKind = getActiveMeasurementKind();
        const activeJobRunning = hasActiveMeasurementJob();
        elements.measurementRepeatStartBtn.disabled = measurementState.calibrationUpdating || measurementState.calibrationDeleting
            ? true
            : (activeJobRunning ? activeKind !== 'lr_repeat'
            : (measurementState.startInFlight || measurementState.inputsLoading || !measurementModeReady()));
        elements.measurementRepeatStartBtn.textContent = activeKind === 'lr_repeat'
            ? 'Cancel measurement'
            : 'Start L/R Repeat';
    }
    if (elements.measurementSaveBtn) {
        const hasAutoSubMeas = Array.isArray(measurementState.autoSubMeasurements) && measurementState.autoSubMeasurements.length > 0;
        const hasUnsavedContent = hasAutoSubMeas || (current && !measurementState.currentMeasurementSaved);
        elements.measurementSaveBtn.disabled = !hasUnsavedContent || measurementState.saveInFlight || measurementState.startInFlight;
        elements.measurementSaveBtn.textContent = measurementState.saveInFlight ? 'Working…' : (measurementState.currentMeasurementSaved && !hasAutoSubMeas ? 'Saved' : 'Save current');
    }
}

function renderMeasurementPanelViewSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
    if (elements.measurementAssistMode) {
        elements.measurementAssistMode.value = assistMode;
        elements.measurementAssistMode.disabled = !frequencyView;
        elements.measurementAssistMode.title = frequencyView ? '' : 'Only available in frequency view.';
    }
    if (elements.measurementTargetCurve) {
        const editingCustomHouseCurve = activeEditor === 'houseCurve' && ensureCustomHouseCurveState().displayTarget === 'editing-custom-house-curve';
        const editingOption = editingCustomHouseCurve ? '<option value="editing-custom-house-curve">Editing Custom House Curve…</option>' : '';
        elements.measurementTargetCurve.innerHTML = editingOption + getMeasurementConvolverCurveOptions()
            .map((curve) => `<option value="${escapeHtml(curve.key)}" ${!editingCustomHouseCurve && conv.targetCurve === curve.key ? 'selected' : ''}>${escapeHtml(curve.label || curve.shortLabel || curve.key)}</option>`)
            .join('') + '<option value="create-custom-house-curve">Create Custom House Curve…</option>';
        elements.measurementTargetCurve.value = editingCustomHouseCurve ? 'editing-custom-house-curve' : conv.targetCurve;
        elements.measurementTargetCurve.disabled = !frequencyView;
        elements.measurementTargetCurve.title = frequencyView ? '' : 'Only available in frequency view.';
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
            || String(conv.quality) !== defaultConv.quality
        );
        const hasResettableGraphState = !!current || !!peq.filters.length || hasConvolverResettableState || activeEditor === 'houseCurve';
        elements.measurementClearBtn.disabled = !frequencyView || !hasResettableGraphState || measurementState.startInFlight || !!measurementState.activeJobId;
        elements.measurementClearBtn.title = frequencyView ? '' : 'Only available in frequency view.';
    }
}

function renderMeasurementPanelStatusSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
    if (elements.measurementSetupStatus) {
        elements.measurementSetupStatus.textContent = measurementSetupStatusText();
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
        const irDiagnostics = buildMeasurementIrDiagnostics(graphEntries, frequencyView);
        const irSummary = buildMeasurementIrSummary(irDiagnostics);
        const irTooltip = buildMeasurementIrDiagnosticsTooltip(irDiagnostics);
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
    renderMeasurementIrDiagnostics(graphEntries, frequencyView);
}

function renderMeasurementPanelEditorsSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
    if (elements.measurementPeqPanel) {
        elements.measurementPeqPanel.classList.toggle('hidden', !frequencyView || activeEditor !== 'peq' || (!peq.enabled && !peq.filters.length));
    }
    const customHouseCurve = ensureCustomHouseCurveState();
    const activeCustomPoint = customHouseCurve.points.find((point) => point.id === customHouseCurve.activePointId) || null;
    if (elements.measurementCustomHouseCurvePanel) {
        elements.measurementCustomHouseCurvePanel.classList.toggle('hidden', !frequencyView || activeEditor !== 'houseCurve');
    }
    if (elements.measurementCustomHouseCurveChips) {
        elements.measurementCustomHouseCurveChips.innerHTML = Array.from({ length: 8 }, (_, index) => {
            const point = customHouseCurve.points.find((candidate) => Number(candidate.slot) === index) || null;
            const active = point && point.id === customHouseCurve.activePointId;
            const color = point ? getCustomHouseCurvePointColor(point, index) : '';
            return renderMeasurementSlotChip({
                label: `P${index + 1}`,
                index,
                color,
                active,
                occupied: !!point,
                attributes: `data-custom-house-curve-slot="${index}" data-custom-house-curve-point="${point ? escapeHtml(point.id) : ''}"`,
            });
        }).join('');
    }
    if (elements.measurementCustomHouseCurveEditor) {
        const activeCustomSlot = activeCustomPoint ? getCustomHouseCurvePointSlot(activeCustomPoint.id) : -1;
        const activeCustomColor = activeCustomPoint ? getCustomHouseCurvePointColor(activeCustomPoint, activeCustomSlot) : '';
        elements.measurementCustomHouseCurveEditor.innerHTML = activeCustomPoint ? `
            <div class="measurement-peq-editor-grid" data-custom-house-curve-editor-slot="${activeCustomSlot}" style="border-color:${escapeHtml(activeCustomColor)}66;">
                <div class="measurement-custom-house-curve-slot-label" style="color:${escapeHtml(activeCustomColor)};">P${activeCustomSlot + 1}</div>
                <div class="field-group measurement-peq-direct-input-field">
                    <label for="measurement-custom-house-curve-frequency">Frequency (Hz)</label>
                    <input id="measurement-custom-house-curve-frequency" class="url-input measurement-peq-number-input" type="number" min="20" max="20000" step="1" value="${Math.round(activeCustomPoint.freqHz)}" data-custom-house-curve-field="freqHz">
                </div>
                <div class="field-group measurement-peq-direct-input-field">
                    <label for="measurement-custom-house-curve-gain">Gain (dB)</label>
                    <input id="measurement-custom-house-curve-gain" class="url-input measurement-peq-number-input" type="number" min="-24" max="24" step="0.1" value="${Number(activeCustomPoint.gainDb).toFixed(1)}" data-custom-house-curve-field="gainDb">
                </div>
                <div class="measurement-peq-editor-actions">
                    <button type="button" class="btn-danger" data-custom-house-curve-delete="${escapeHtml(activeCustomPoint.id)}">Delete</button>
                </div>
            </div>` : '<div class="measurement-peq-editor-empty">Use P1-P8 to add up to 8 curve points.</div>';
    }
    if (elements.measurementCustomHouseCurveName && document.activeElement !== elements.measurementCustomHouseCurveName) {
        elements.measurementCustomHouseCurveName.value = customHouseCurve.name || '';
    }
    if (elements.measurementCustomHouseCurveCreateBtn) {
        elements.measurementCustomHouseCurveCreateBtn.disabled = customHouseCurve.points.length < 2 || !String(customHouseCurve.name || '').trim() || customHouseCurve.saving;
        elements.measurementCustomHouseCurveCreateBtn.textContent = customHouseCurve.saving ? 'Creating…' : 'Create Target Curve';
    }
    if (elements.measurementConvolverPanel) {
        elements.measurementConvolverPanel.classList.toggle('hidden', !frequencyView || assistMode !== 'convolver');
    }
    if (elements.measurementPeqChips) {
        elements.measurementPeqChips.innerHTML = Array.from({ length: 12 }, (_, index) => {
            const filter = peq.filters[index] || null;
            const active = filter && filter.id === peq.activeFilterId;
            return renderMeasurementSlotChip({
                label: `F${index + 1}`,
                index,
                color: filter ? filter.color : '',
                active,
                occupied: !!filter,
                attributes: `data-measurement-peq-slot="${index}" data-measurement-peq-chip="${filter ? escapeHtml(filter.id) : ''}"`,
            });
        }).join('');
    }
    if (elements.measurementPeqEditor) {
        if (!activePeqFilter) {
            elements.measurementPeqEditor.innerHTML = '<div class="measurement-peq-editor-empty">Use F1-F12 or the graph near the fixed 0 dB line to add up to 12 temporary filters.</div>';
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
        elements.measurementPeqDraftSummary.innerHTML = `<div>Draft: ${escapeHtml(draftMode)} · L: ${peqDraftLeftCount} bands · R: ${peqDraftRightCount} bands</div>`;
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

}

function renderMeasurementPanelConvolverSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
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
    if (elements.measurementConvolverSampleRate) elements.measurementConvolverSampleRate.value = String(measurementState.measurementSampleRate || '48000');
    if (elements.measurementConvolverPhaseMode) elements.measurementConvolverPhaseMode.value = conv.phaseMode;
    if (elements.measurementConvolverIrLength) elements.measurementConvolverIrLength.value = String(conv.irLength);
    const convolverSourceSelection = getMeasurementConvolverSourceSelectionState();
    const convAnalyses = ['left', 'right'].map((side) => analyzeMeasurementConvolverSide(side));
    const left = convAnalyses[0];
    const right = convAnalyses[1];
    const leftDraft = conv.draft?.left || null;
    const rightDraft = conv.draft?.right || null;
    const hasConvolverDraft = !!leftDraft || !!rightDraft;
    const draftPhaseMismatch = getMeasurementConvolverDraftPhaseMismatch(conv);
    const isCreatingConvolverPreset = !!conv.creatingPreset || convolverCreateInFlight;
    if (elements.measurementConvolverSummary) {
        const curve = getMeasurementConvolverCurve(conv.targetCurve);
        const hasCreatedConvolver = (state.dsp?.assistStack || []).some((item) => item.type === 'convolver');
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
        if (isCreatingConvolverPreset) {
            draftStatus = 'Creating convolver preset...';
        } else if (draftPhaseMismatch) {
            draftStatus = 'Draft phase does not match the selected phase type. Take L/R again.';
        } else if (leftDraft && rightDraft) {
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
            if (currentTimingDelta && measurementConvolverAlignedPhaseModes.includes(conv.phaseMode)) {
                draftStatus += ` · ${formatMeasurementConvolverTimingRelation(currentTimingDelta)}`;
            }
        }
        if (summaryTimingDelta && (leftDraft && rightDraft || (left && right && measurementConvolverAlignedPhaseModes.includes(conv.phaseMode)))) {
            if (summaryTimingDelta.absMs > MEASUREMENT_CONVOLVER_TIMING_SAFETY_LIMIT_MS) {
                draftDetails.push('Filter not created because timing offset exceeds safety limit.');
            }
        }
        if (conv.draft?.notice) draftDetails.push(conv.draft.notice);
        if (hasConvolverDraft) {
            const stagedPhaseModes = [...new Set([leftDraft, rightDraft].map(getMeasurementConvolverDraftPhaseMode).filter(Boolean))];
            if (stagedPhaseModes.length) {
                draftDetails.push(`Staged phase: ${stagedPhaseModes.map(getMeasurementConvolverPhaseLabel).join(' + ')}`);
            }
        }
        elements.measurementConvolverSummary.innerHTML = `
            <div><strong>${escapeHtml(curve.label)}</strong> · ${escapeHtml(getMeasurementConvolverTypeLabel(conv.quality))} · Max Boost +${conv.maxBoostDb} dB · Max Cut ${conv.maxCutDb} dB · Dip Guard ${escapeHtml(conv.dipGuard)}</div>
            <div>Range data. L: ${left ? `${left.points} pts, gain ${formatMeasurementConvolverGain(left.autoGainDb)}` : 'none'} · R: ${right ? `${right.points} pts, gain ${formatMeasurementConvolverGain(right.autoGainDb)}` : 'none'}</div>
            <div>${escapeHtml(draftStatus)}</div>
            ${draftDetails.map((detail) => `<div>${escapeHtml(detail)}</div>`).join('')}
        `;
    }
    if (elements.measurementConvolverPresetName) {
        const draftMode = getMeasurementConvolverPreviewMode(left, right, leftDraft, rightDraft);
        const draftPhaseMode = getMeasurementConvolverDraftPhaseMode(leftDraft || rightDraft) || conv.phaseMode;
        const previewGainDb = getMeasurementConvolverPreviewGain(draftMode, left, right, leftDraft, rightDraft);
        const nameValue = hasConvolverDraft
            ? (conv.draft?.presetName || getMeasurementConvolverItemName(draftMode, previewGainDb, { phaseMode: draftPhaseMode }))
            : getMeasurementConvolverItemName(draftMode, previewGainDb, { phaseMode: conv.phaseMode });
        if (document.activeElement !== elements.measurementConvolverPresetName) {
            elements.measurementConvolverPresetName.value = nameValue;
        }
        elements.measurementConvolverPresetName.disabled = !hasConvolverDraft || !!draftPhaseMismatch;
        elements.measurementConvolverPresetName.placeholder = hasConvolverDraft ? 'Preset name' : 'Preview. Take L/R/Both to stage';
    }
    if (elements.measurementConvolverWarnings) {
        const warnings = buildMeasurementConvolverWarnings(convAnalyses);
        elements.measurementConvolverWarnings.innerHTML = warnings.map((warning) => `<div>${escapeHtml(warning)}</div>`).join('');
        elements.measurementConvolverWarnings.classList.toggle('hidden', !warnings.length);
    }
    if (elements.measurementConvolverTakeLeftBtn) {
        elements.measurementConvolverTakeLeftBtn.disabled = !left || !convolverSourceSelection.take.left || isCreatingConvolverPreset;
        elements.measurementConvolverTakeLeftBtn.classList.toggle('is-active', !!left && convolverSourceSelection.take.left && !isCreatingConvolverPreset);
        elements.measurementConvolverTakeLeftBtn.setAttribute('aria-pressed', !!left && convolverSourceSelection.take.left && !isCreatingConvolverPreset ? 'true' : 'false');
    }
    if (elements.measurementConvolverTakeRightBtn) {
        elements.measurementConvolverTakeRightBtn.disabled = !right || !convolverSourceSelection.take.right || isCreatingConvolverPreset;
        elements.measurementConvolverTakeRightBtn.classList.toggle('is-active', !!right && convolverSourceSelection.take.right && !isCreatingConvolverPreset);
        elements.measurementConvolverTakeRightBtn.setAttribute('aria-pressed', !!right && convolverSourceSelection.take.right && !isCreatingConvolverPreset ? 'true' : 'false');
    }
    if (elements.measurementConvolverTakeBothBtn) {
        elements.measurementConvolverTakeBothBtn.disabled = !left || !right || !convolverSourceSelection.take.both || isCreatingConvolverPreset;
        elements.measurementConvolverTakeBothBtn.classList.toggle('is-active', !!left && !!right && convolverSourceSelection.take.both && !isCreatingConvolverPreset);
        elements.measurementConvolverTakeBothBtn.setAttribute('aria-pressed', !!left && !!right && convolverSourceSelection.take.both && !isCreatingConvolverPreset ? 'true' : 'false');
    }
    if (elements.measurementConvolverCreateBtn) {
        elements.measurementConvolverCreateBtn.disabled = !hasConvolverDraft || !!draftPhaseMismatch || !String(conv.draft?.presetName || '').trim() || isCreatingConvolverPreset;
        elements.measurementConvolverCreateBtn.textContent = isCreatingConvolverPreset ? 'Creating...' : 'Create Convolver Preset';
    }
}

function renderMeasurementPanelSavedListSection({ measurementState, current, measurements, graphEntries, assistMode, activeEditor, graphView, frequencyView, peq, conv, activePeqFilter }) {
    const selectedSavedCount = measurements.filter(measurement => measurementState.visibilityById?.[measurement.id]).length;
    const allSavedSelected = measurements.length > 0 && selectedSavedCount === measurements.length;
    const visibleMeasurementColorById = getVisibleMeasurementColorById();

    const savedItemsHtml = measurements.map((measurement) => {
        const pointsLabel = summarizeMeasurementEntry(measurement);
        const traceColor = visibleMeasurementColorById[measurement.id] || '';
        const isSelected = !!measurementState.visibilityById?.[measurement.id];
        const isVisibleInGraph = !!traceColor;
        const qualitySummary = getMeasurementQualitySummary(measurement);
        const qualityTitle = getMeasurementQualityTitle(measurement);
        const timingInfo = getMeasurementTimingInfo(measurement);
        const micInputChannel = measurement.input_channels?.mic ? ` · Mic In ${measurement.input_channels.mic}` : '';
        const referenceInputChannel = measurement.input_channels?.electrical_reference ? ` · Ref In ${measurement.input_channels.electrical_reference}` : '';
        return `
            <div class="measurement-list-item" style="${isVisibleInGraph ? `border-color:${traceColor}; box-shadow: inset 0 0 0 1px ${traceColor}33; background: linear-gradient(180deg, rgba(255,255,255,0.03), ${traceColor}12);` : ''}">
                <div class="measurement-list-row">
                    <span class="measurement-toggle">
                        <input type="checkbox" data-measurement-toggle="${escapeHtml(measurement.id)}" ${isSelected ? 'checked' : ''}>
                        <span class="measurement-swatch ${isVisibleInGraph ? '' : 'measurement-swatch-inactive'}" ${isVisibleInGraph ? `style="background:${escapeHtml(traceColor)}"` : ''}></span>
                        <span class="measurement-list-title"><a href="${escapeHtml(measurementFileUrl(measurement.id))}" title="${escapeHtml(measurement.name)}">${escapeHtml(getCompactDisplayName(measurement.name, 24))}</a></span>
                    </span>
                    <span class="measurement-list-meta">${escapeHtml(formatMeasurementDate(measurement.created_at))}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta">${escapeHtml(measurement.input_device?.label || 'Capture input')} · ${escapeHtml(String(measurement.channel || 'left'))}${escapeHtml(micInputChannel)}${escapeHtml(referenceInputChannel)}</span>
                    <span class="measurement-list-points">${escapeHtml(pointsLabel)}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta" title="${escapeHtml(timingInfo.detail)}">${escapeHtml(timingInfo.line)}</span>
                </div>
                <div class="measurement-list-row">
                    <span class="measurement-list-meta" title="${escapeHtml(qualityTitle)}">${escapeHtml(qualitySummary)} · ${isVisibleInGraph ? 'visible, dashed compare trace' : 'hidden compare trace'}</span>
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
}

function getSavedListMeasurements() {
    const measurementState = state.measurement || {};
    const current = getCurrentMeasurementEntry();
    return (measurementState.measurements || []).filter(measurement => measurement.id !== current?.id);
}

function commitCustomHouseCurveField(input) {
    const custom = ensureCustomHouseCurveState();
    if (!custom.activePointId) return;
    updateCustomHouseCurvePoint(custom.activePointId, { [input.dataset.customHouseCurveField]: Number(input.value) });
}

function commitPeqEditorField(input, reRender) {
    const activeFilter = getMeasurementPeqActiveFilter();
    if (!activeFilter) return;
    const field = input.dataset.measurementPeqField;
    const value = field === 'type' ? input.value : Number(input.value);
    updateMeasurementPeqFilter(activeFilter.id, { [field]: value });
    if (reRender) renderMeasurementPanel();
    scheduleMeasurementGraphRender();
}

function handleMeasurementPeqStepClick(button) {
    const activeFilter = getMeasurementPeqActiveFilter();
    if (!activeFilter) return;
    const container = elements.measurementPeqEditor;
    if (button.dataset.measurementPeqFrequencyStep !== undefined) {
        const input = container?.querySelector('#measurement-peq-freq');
        const step = Number(input?.step) || 1;
        const direction = Number(button.dataset.measurementPeqFrequencyStep || '0');
        const nextValue = stepMeasurementPeqFrequency(activeFilter.id, direction, step);
        if (nextValue === null) return;
        if (input) input.value = String(nextValue);
    } else if (button.dataset.measurementPeqGainStep !== undefined) {
        const input = container?.querySelector('#measurement-peq-gain');
        const step = Number(input?.step) || 0.1;
        const direction = Number(button.dataset.measurementPeqGainStep || '0');
        const nextValue = stepMeasurementPeqGain(activeFilter.id, direction, step);
        if (nextValue === null) return;
        if (input) input.value = nextValue.toFixed(1);
    } else {
        const input = container?.querySelector('#measurement-peq-q');
        const step = Number(input?.step) || 0.1;
        const direction = Number(button.dataset.measurementPeqQStep || '0');
        const nextValue = stepMeasurementPeqQ(activeFilter.id, direction, step);
        if (nextValue === null) return;
        if (input) input.value = nextValue.toFixed(2);
    }
    scheduleMeasurementGraphRender();
    focusMeasurementPeqPanelContext();
}

let measurementPanelDelegationBound = false;
// Dynamic panel regions rebuild their innerHTML on every render; their
// listeners therefore live once on the stable containers via delegation.
function bindMeasurementPanelDelegation() {
    if (measurementPanelDelegationBound) return;
    measurementPanelDelegationBound = true;
    elements.measurementPeqChips?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-measurement-peq-slot]');
        if (!button) return;
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
    elements.measurementCustomHouseCurveChips?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-custom-house-curve-slot]');
        if (!button) return;
        const custom = ensureCustomHouseCurveState();
        const pointId = button.dataset.customHouseCurvePoint;
        if (pointId) custom.activePointId = pointId;
        else if (!addCustomHouseCurvePoint({ slot: Number(button.dataset.customHouseCurveSlot) })) return;
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
    });
    elements.measurementCustomHouseCurveEditor?.addEventListener('input', (event) => {
        const input = event.target.closest('[data-custom-house-curve-field]');
        if (!input) return;
        commitCustomHouseCurveField(input);
        scheduleMeasurementGraphRender();
    });
    elements.measurementCustomHouseCurveEditor?.addEventListener('change', (event) => {
        const input = event.target.closest('[data-custom-house-curve-field]');
        if (!input) return;
        commitCustomHouseCurveField(input);
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
    });
    elements.measurementCustomHouseCurveEditor?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-custom-house-curve-delete]');
        if (!button) return;
        deleteCustomHouseCurvePoint(button.dataset.customHouseCurveDelete);
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
    });
    elements.measurementPeqEditor?.addEventListener('keydown', (event) => {
        if (!(event.target instanceof HTMLInputElement) || event.target.type !== 'number') return;
        if (!event.target.matches('[data-measurement-peq-field]')) return;
        handleMeasurementPeqNumberInputArrowKey(event);
    });
    elements.measurementPeqEditor?.addEventListener('input', (event) => {
        const input = event.target.closest('[data-measurement-peq-field]');
        if (!input) return;
        commitPeqEditorField(input, input.dataset.measurementPeqField === 'type');
    });
    elements.measurementPeqEditor?.addEventListener('change', (event) => {
        const input = event.target.closest('[data-measurement-peq-field]');
        if (!input) return;
        commitPeqEditorField(input, true);
    });
    elements.measurementPeqEditor?.addEventListener('click', (event) => {
        const stepButton = event.target.closest('[data-measurement-peq-frequency-step], [data-measurement-peq-gain-step], [data-measurement-peq-q-step]');
        if (stepButton) {
            handleMeasurementPeqStepClick(stepButton);
            return;
        }
        const deleteButton = event.target.closest('[data-measurement-peq-delete]');
        if (!deleteButton) return;
        deleteMeasurementPeqFilter(deleteButton.dataset.measurementPeqDelete);
        renderMeasurementPanel();
        scheduleMeasurementGraphRender();
    });

    const list = elements.measurementList;
    // <details> toggle does not bubble; capture phase still reaches ancestors.
    list?.addEventListener('toggle', (event) => {
        const details = event.target;
        if (!(details instanceof HTMLDetailsElement)) return;
        state.measurement.savedGroupOpen = !!details.open;
        const summary = details.querySelector('summary');
        if (summary) {
            summary.textContent = `${state.measurement.savedGroupOpen ? 'Close saved' : 'Open saved'} (${getSavedListMeasurements().length})`;
        }
    }, true);
    list?.addEventListener('change', (event) => {
        const input = event.target;
        if (input.matches('[data-measurement-toggle]')) {
            state.measurement.visibilityById[input.dataset.measurementToggle] = !!input.checked;
            state.measurement.savedGroupOpen = true;
            renderMeasurementPanel();
            return;
        }
        if (input.matches('[data-measurement-select-all]')) {
            getSavedListMeasurements().forEach((measurement) => {
                state.measurement.visibilityById[measurement.id] = !!input.checked;
            });
            state.measurement.savedGroupOpen = true;
            renderMeasurementPanel();
        }
    });
    list?.addEventListener('click', (event) => {
        const mergeButton = event.target.closest('[data-measurement-merge-selected]');
        if (mergeButton) {
            mergeSelectedMeasurements();
            return;
        }
        const deleteButton = event.target.closest('[data-measurement-delete-selected]');
        if (deleteButton) {
            deleteSelectedMeasurements();
            return;
        }
        const closeButton = event.target.closest('[data-measurement-close-saved]');
        if (closeButton) {
            state.measurement.savedGroupOpen = false;
            renderMeasurementPanel();
        }
    });
}

function setupMeasurementActions() {
    if (!elements.measurementPanel || !elements.effectsMeasureOpenBtn || !elements.measurementCloseBtn) return;
    bindMeasurementPanelDelegation();
    elements.effectsMeasureOpenBtn.addEventListener('click', () => toggleMeasurementPanel(true));
    elements.measurementCloseBtn.addEventListener('click', () => toggleMeasurementPanel(false));
    const backdrop = elements.measurementPanel.querySelector('.manage-overlay-backdrop');
    if (backdrop) backdrop.addEventListener('click', () => toggleMeasurementPanel(false));
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !elements.measurementPanel.classList.contains('hidden')) {
            toggleMeasurementPanel(false);
        }
    });
    window.addEventListener('pagehide', () => {
        if (isMeasurementPanelOpen()) stopMeasurementWindowHeartbeat(true);
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
            applyMeasurementInputSelection(event.target.value || '');
        });
    }
    if (elements.measurementInputRefreshBtn) {
        elements.measurementInputRefreshBtn.addEventListener('click', () => {
            void fetchMeasurementInputs();
        });
    }
    if (elements.measurementMicInputChannelSelect) {
        elements.measurementMicInputChannelSelect.addEventListener('change', (event) => {
            state.measurement.selectedMicInputChannel = event.target.value || '1';
            normalizeMeasurementInputChannelSelections();
            void saveMeasurementSetupSettings({
                selectedMicInputChannel: state.measurement.selectedMicInputChannel,
                selectedReferenceInputChannel: state.measurement.selectedReferenceInputChannel || '',
            });
            renderMeasurementPanel();
        });
    }
    if (elements.measurementReferenceInputChannelSelect) {
        elements.measurementReferenceInputChannelSelect.addEventListener('change', (event) => {
            state.measurement.selectedReferenceInputChannel = event.target.value || '';
            normalizeMeasurementInputChannelSelections();
            renderMeasurementPanel();
            void saveMeasurementSetupSettings({ selectedReferenceInputChannel: state.measurement.selectedReferenceInputChannel || '' });
        });
    }
    document.querySelectorAll('[data-measurement-channel]').forEach((button) => {
        button.addEventListener('click', () => {
            state.measurement.selectedChannel = button.getAttribute('data-measurement-channel') || 'left';
            renderMeasurementPanel();
        });
    });
    document.querySelectorAll('[data-measurement-smoothing]').forEach((button) => {
        button.addEventListener('click', () => {
            if (getMeasurementGraphView() === 'ir') return;
            state.measurement.displaySmoothing = button.getAttribute('data-measurement-smoothing') || '1/6-oct';
            renderMeasurementPanel();
            scheduleMeasurementGraphRender();
        });
    });
    document.querySelectorAll('[data-measurement-view]').forEach((button) => {
        button.addEventListener('click', () => {
            state.measurement.measurementView = button.getAttribute('data-measurement-view') || 'freq';
            ensureMeasurementPeqState().dragFilterId = null;
            ensureCustomHouseCurveState().dragPointId = null;
            ensureMeasurementConvolverState().dragMode = null;
            measurementGraphPointerId = null;
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
    if (elements.measurementCalibrationExportBtn) {
        elements.measurementCalibrationExportBtn.addEventListener('click', () => { void downloadSelectedMeasurementCalibration(); });
    }
    if (elements.measurementHouseCurveSelect) {
        elements.measurementHouseCurveSelect.addEventListener('change', (event) => {
            const houseCurveId = event.target.value || '';
            if (elements.measurementHouseCurveFile) elements.measurementHouseCurveFile.value = '';
            state.measurement.houseCurveFilename = '';
            setMeasurementActiveEditor('none');
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
    if (elements.measurementHouseCurveExportBtn) {
        elements.measurementHouseCurveExportBtn.addEventListener('click', () => { void downloadSelectedMeasurementHouseCurve(); });
    }
    if (elements.measurementNameInput) {
        elements.measurementNameInput.addEventListener('input', (event) => {
            state.measurement.currentMeasurementName = event.target.value || '';
        });
    }
    if (elements.measurementStartBtn) {
        elements.measurementStartBtn.addEventListener('click', () => {
            if (getActiveMeasurementKind() === 'single') {
                void cancelMeasurement();
                return;
            }
            void startMeasurement();
        });
    }
    if (elements.measurementRepeatStartBtn) {
        elements.measurementRepeatStartBtn.addEventListener('click', () => {
            if (getActiveMeasurementKind() === 'lr_repeat') {
                void cancelMeasurement();
                return;
            }
            void startLrRepeat();
        });
    }
    if (elements.measurementAutoSubStartBtn) {
        elements.measurementAutoSubStartBtn.addEventListener('click', () => {
            const measurementState = state.measurement || {};
            if (measurementState.autoSubInFlight) {
                void cancelAutoSubOptimize();
                return;
            }
            void startAutoSubOptimize();
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
        elements.measurementTargetCurve.addEventListener('change', (event) => handleMeasurementTargetCurveSelection(event.target.value));
    }
    if (elements.measurementCustomHouseCurveName) {
        elements.measurementCustomHouseCurveName.addEventListener('input', (event) => {
            const custom = ensureCustomHouseCurveState();
            custom.name = event.target.value || '';
            custom.nameTouched = true;
            if (elements.measurementCustomHouseCurveCreateBtn) {
                elements.measurementCustomHouseCurveCreateBtn.disabled = custom.points.length < 2 || !custom.name.trim() || custom.saving;
            }
        });
    }
    if (elements.measurementCustomHouseCurveCreateBtn) {
        elements.measurementCustomHouseCurveCreateBtn.addEventListener('click', () => { void createCustomHouseCurve(); });
    }
    [elements.measurementConvolverTarget, elements.measurementConvolverRangeStart, elements.measurementConvolverRangeEnd, elements.measurementConvolverMaxBoost, elements.measurementConvolverMaxCut, elements.measurementConvolverDipGuard, elements.measurementConvolverSampleRate, elements.measurementConvolverPhaseMode, elements.measurementConvolverIrLength].forEach((input) => {
        if (!input) return;
        const commit = () => {
            setMeasurementActiveEditor('none');
            updateMeasurementConvolverField(input.dataset.measurementConvolverField, input.value);
            renderMeasurementPanel();
            scheduleMeasurementGraphRender();
        };
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
                elements.measurementConvolverCreateBtn.disabled = !hasDraft || !!getMeasurementConvolverDraftPhaseMismatch(conv) || !conv.draft.presetName.trim() || convolverCreateInFlight;
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
        elements.measurementGraph.addEventListener('pointerleave', handleMeasurementGraphPointerLeave);
        elements.measurementGraph.addEventListener('wheel', handleMeasurementPeqGraphWheel, { passive: false });
    }
    const measurementGraphWrap = elements.measurementGraph?.closest('.measurement-graph-wrap');
    if (measurementGraphWrap && typeof ResizeObserver === 'function') {
        measurementGraphResizeObserver = new ResizeObserver(() => scheduleMeasurementGraphRenderForResize());
        measurementGraphResizeObserver.observe(measurementGraphWrap);
    }
    window.addEventListener('resize', scheduleMeasurementGraphRenderForResize);
    window.addEventListener('orientationchange', scheduleMeasurementGraphRenderForResize);
    document.addEventListener('fullscreenchange', scheduleMeasurementGraphRenderForResize);
    window.visualViewport?.addEventListener('resize', scheduleMeasurementGraphRenderForResize);
    renderMeasurementPanel();
}

async function fetchEffects() {
    try {
        const resp = await fetch('/api/dsp/presets');
        if (!resp.ok) throw new Error('Failed to fetch DSP presets');
        const data = await resp.json();
        const prev = state.dsp?.compare;
        const presetNames = (data.presets || []).map(p => p.name);
        state.dsp = {
            ...data,
            combineDraft: state.dsp?.combineDraft || getDefaultEffectsCombineDraft(),
            peqDraft: state.dsp?.peqDraft || {
                presetName: '',
                eqMode: 'IIR',
                loadAfterCreate: false,
                leftBands: [defaultPeqBand()],
                rightBands: [defaultPeqBand()],
            },
            assistStack: Array.isArray(state.dsp?.assistStack) ? state.dsp.assistStack : [],
            compare: resolveEffectsCompareState(data.compare || prev, presetNames, data.active_preset || ''),
        };
        state.dsp.combineDraft = normalizeEffectsCombineDraft(state.dsp.combineDraft, presetNames);
        if (!Array.isArray(state.dsp.peqDraft?.leftBands) || !state.dsp.peqDraft.leftBands.length) state.dsp.peqDraft.leftBands = [defaultPeqBand()];
        if (!Array.isArray(state.dsp.peqDraft?.rightBands) || !state.dsp.peqDraft.rightBands.length) state.dsp.peqDraft.rightBands = [defaultPeqBand()];
        state.dsp.peqDraft.eqMode = normalizePeqEqMode(state.dsp.peqDraft?.eqMode);
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
    } catch (e) {
        if (elements.effectsStatus) elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">DSP presets are unavailable</div>';
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
    state.dsp.peqDraft = getDefaultPeqDraft();
    if (elements.effectsPeqPresetName) elements.effectsPeqPresetName.value = '';
    if (elements.effectsPeqModeSelect) elements.effectsPeqModeSelect.value = 'IIR';
    renderPeqBands();
}
function addPeqBandPair() {
    if (!state.dsp?.peqDraft) {
        state.dsp.peqDraft = getDefaultPeqDraft();
    }
    const leftBands = state.dsp.peqDraft.leftBands || (state.dsp.peqDraft.leftBands = []);
    const rightBands = state.dsp.peqDraft.rightBands || (state.dsp.peqDraft.rightBands = []);
    if (leftBands.length >= 20 || rightBands.length >= 20) {
        showToast('Maximum 20 Left and Right PEQ bands supported', 'error');
        return;
    }
    leftBands.push(defaultPeqBand());
    rightBands.push(defaultPeqBand());
    renderPeqBands();
}
function removePeqBand(side, index) {
    if (!state.dsp?.peqDraft) return;
    const key = side === 'right' ? 'rightBands' : 'leftBands';
    const bands = state.dsp.peqDraft[key] || [];
    if (index < 0 || index >= bands.length) return;
    bands.splice(index, 1);
    renderPeqBands();
}
function ensurePeqBandExists(side, index) {
    if (!state.dsp?.peqDraft) return null;
    const key = side === 'right' ? 'rightBands' : 'leftBands';
    state.dsp.peqDraft[key] = state.dsp.peqDraft[key] || [];
    while (state.dsp.peqDraft[key].length <= index) {
        state.dsp.peqDraft[key].push(defaultPeqBand());
    }
    return state.dsp.peqDraft[key][index] || null;
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
    if (!state.dsp?.peqDraft) return { sourceBand: null, otherBand: null };
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
    if (!state.dsp?.peqDraft) return;
    const leftBands = state.dsp.peqDraft.leftBands || [];
    const rightBands = state.dsp.peqDraft.rightBands || [];
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
    if (!state.dsp?.peqDraft) return;
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
        const otherBands = side === 'right' ? (state.dsp?.peqDraft?.leftBands || []) : (state.dsp?.peqDraft?.rightBands || []);
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
    if (!state.dsp?.peqDraft) {
        state.dsp = state.dsp || {};
        state.dsp.peqDraft = getDefaultPeqDraft();
    }
    normalizeLinkedPeqSpecialBands();
    const draft = state.dsp.peqDraft;
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
    const draftBands = side === 'right' ? (state.dsp?.peqDraft?.rightBands || []) : (state.dsp?.peqDraft?.leftBands || []);
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
    if (!state.dsp?.peqDraft) {
        state.dsp = state.dsp || {};
        state.dsp.peqDraft = getDefaultPeqDraft();
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
    const eqMode = normalizePeqEqMode(elements.effectsPeqModeSelect?.value || state.dsp.peqDraft?.eqMode);
    state.dsp.peqDraft.leftBands = leftBands;
    state.dsp.peqDraft.rightBands = rightBands;
    state.dsp.peqDraft.eqMode = eqMode;
    if (elements.effectsPeqCreatePresetBtn) elements.effectsPeqCreatePresetBtn.disabled = true;
    if (elements.effectsStatus) elements.effectsStatus.innerHTML = `<div>Creating PEQ preset: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/dsp/presets/create-peq', {
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
        const resp = await fetch('/api/dsp/presets/import-rew-peq', {
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
    }
}
function renderEffects() {
    const fx = state.dsp;
    const presets = fx.presets || [];
    const presetNames = presets.map(p => p.name);
    elements.effectsInfo.textContent = fx.available
        ? `${fx.preset_count} presets`
        : 'DSP is not available';
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
    renderEffectsCompare();
    renderEffectsCombine();
}
function getEmptyEffectsCompareState() {
    return { presetA: '', presetB: '', activeSide: null };
}

function getEffectsCompareState() {
    const fx = state.dsp || {};
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
    const fx = state.dsp;
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
    const fx = state.dsp || {};
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
        const resp = await fetch('/api/dsp/presets/combine', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                presetName: validation.presetName,
                presetNames: validation.selectedPresets,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Combined preset save failed');
        state.dsp.combineDraft = getDefaultEffectsCombineDraft();
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
        const resp = await fetch('/api/dsp/presets/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_name: target }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Failed to load preset');
        state.dsp.active_preset = target;
        state.dsp.compare = {
            presetA: targetA,
            presetB: targetB,
            activeSide: newSide,
        };
        await saveEffectsCompareState(state.dsp.compare);
        renderEffects();
    } finally {
        setEffectsCompareLoadBusy(false);
    }
}

async function handleEffectsCompareSelectionChange(slot) {
    if (effectsCompareLoadInFlight) return;
    state.dsp.compare = normalizeEffectsCompareSelection(state.dsp.compare || getEmptyEffectsCompareState());

    const previousCompare = {
        presetA: state.dsp.compare.presetA || '',
        presetB: state.dsp.compare.presetB || '',
        activeSide: state.dsp.compare.activeSide || null,
    };
    const activePreset = state.dsp?.active_preset || '';
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
    state.dsp.compare = normalizeEffectsCompareSelection({
        presetA: targetA,
        presetB: targetB,
        activeSide: state.dsp.compare.activeSide || null,
    });
    await saveEffectsCompareState(state.dsp.compare);

    const selectedValue = slot === 'A' ? targetA : targetB;
    const shouldAutoload = !!selectedValue && selectedValue !== activePreset && (!effectiveActiveSide || effectiveActiveSide === slot);

    if (shouldAutoload) {
        await loadEffectsComparePreset(selectedValue, slot, state.dsp.compare.presetA, state.dsp.compare.presetB);
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
    if (elements.effectsLimiterEnabled) elements.effectsLimiterEnabled.checked = !!extras.limiterEnabled;
    if (elements.effectsHeadroomEnabled) elements.effectsHeadroomEnabled.checked = !!extras.headroomEnabled;
    if (elements.effectsHeadroomGainDb && !_activeEditing.has(elements.effectsHeadroomGainDb)) {
        elements.effectsHeadroomGainDb.value = String(normalizeEffectsHeadroomGainDb(extras.headroomGainDb, -3));
    }
    if (elements.effectsAutogainEnabled) elements.effectsAutogainEnabled.checked = !!extras.autogainEnabled;
    if (elements.effectsAutogainTargetDb && !_activeEditing.has(elements.effectsAutogainTargetDb)) {
        elements.effectsAutogainTargetDb.value = String(normalizeEffectsAutogainTargetDb(extras.autogainTargetDb, -12));
    }
    if (elements.effectsLoudnessEnabled) elements.effectsLoudnessEnabled.checked = !!extras.loudnessEnabled;
    if (elements.effectsLoudnessStrength && !_activeEditing.has(elements.effectsLoudnessStrength)) {
        elements.effectsLoudnessStrength.value = normalizeEffectsLoudnessStrength(extras.loudnessStrength, 10);
    }
    if (elements.effectsLoudnessFftSize && !_activeEditing.has(elements.effectsLoudnessFftSize)) {
        elements.effectsLoudnessFftSize.value = String(normalizeEffectsLoudnessFftSize(extras.loudnessFftSize, 4096));
    }
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
    if (elements.effectsLoudnessFftWrap) {
        elements.effectsLoudnessFftWrap.classList.toggle('hidden', !elements.effectsLoudnessEnabled?.checked);
    }
    if (elements.effectsLoudnessStrengthWrap) {
        elements.effectsLoudnessStrengthWrap.classList.toggle('hidden', !elements.effectsLoudnessEnabled?.checked);
    }
    if (elements.effectsBassControlsWrap) {
        elements.effectsBassControlsWrap.classList.toggle('hidden', !elements.effectsBassEnabled?.checked);
    }
    if (elements.effectsToneEffectWrap) {
        elements.effectsToneEffectWrap.classList.toggle('hidden', !elements.effectsToneEffectEnabled?.checked);
    }
}

let _subwooferPreviewDrawFrame = null;

function getSubwooferPreviewSettingsFromState() {
    const outputMode = state.settings.audioOutputs?.output_mode || {};
    return isSubwoofer22Mode(outputMode.mode)
        ? getSubwooferGlobalSettings(outputMode, outputMode.subwoofer || {})
        : normalizeSubwooferSettings(outputMode.subwoofer || {});
}

function primeSubwooferPreview() {
    if (!elements.effectsSubwooferPreview) return;
    drawSubwooferPreview(getSubwooferPreviewSettingsFromState());
}

function requestSubwooferPreviewRedrawFromState() {
    if (!elements.effectsSubwooferPreview) return;
    scheduleSubwooferPreviewDraw(getSubwooferPreviewSettingsFromState());
}

function scheduleSubwooferPreviewDraw(subwoofer) {
    if (!elements.effectsSubwooferPreview) return;
    const normalized = normalizeSubwooferSettings(subwoofer || {});
    if (_subwooferPreviewDrawFrame) window.cancelAnimationFrame(_subwooferPreviewDrawFrame);
    _subwooferPreviewDrawFrame = window.requestAnimationFrame(() => {
        _subwooferPreviewDrawFrame = null;
        drawSubwooferPreview(normalized);
        window.requestAnimationFrame(() => drawSubwooferPreview(normalized));
    });
}

function formatSubwooferDelayMs(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toFixed(2) : '0.00';
}

function renderSubwooferPanel() {
    const outputMode = state.settings.audioOutputs?.output_mode || {};
    if (!state.settings.audioOutputs?.loaded) {
        elements.effectsSubwooferCard?.classList.add('hidden');
        return;
    }
    const mode = outputMode.mode || 'stereo';
    const isSubwooferMode = isSubwooferModeName(mode);
    const is22Mode = isSubwoofer22Mode(mode);
    const is22StereoMode = mode === 'subwoofer-2.2-stereo';
    elements.effectsSubwooferCard?.classList.toggle('hidden', !isSubwooferMode);
    elements.effectsSubwooferCard?.classList.toggle('is-subwoofer-21', mode === 'subwoofer-2.1');
    elements.effectsSubwooferCard?.classList.toggle('is-subwoofer-22', is22Mode);
    elements.effectsSubwooferCard?.classList.toggle('is-subwoofer-22-stereo', is22StereoMode);
    if (!isSubwooferMode) {
        setSubwooferFeedback('');
        return;
    }
    const subwoofer = is22Mode
        ? getSubwooferGlobalSettings(outputMode, outputMode.subwoofer || {})
        : normalizeSubwooferSettings(outputMode.subwoofer || {});
    const subwoofers = normalizeSubwoofersSettings(outputMode.subwoofers || {}, subwoofer);
    if (elements.effectsSubwooferRouting) {
        const routingStatus = is22Mode
            ? (is22StereoMode ? 'Out 1/2 Main · Out 3 Left Sub · Out 4 Right Sub' : 'Out 1/2 Main · Out 3 Sub 1 · Out 4 Sub 2')
            : (outputMode.routing?.status || 'Out 1/2 Main · Out 3/4 Sub');
        const slope = subwoofer.slope || 'LR24';
        elements.effectsSubwooferRouting.textContent = `${routingStatus} · ${slope}`;
    }
    if (elements.effectsSubwooferModeBadge) {
        elements.effectsSubwooferModeBadge.textContent = is22StereoMode ? '2.2 Stereo Bass active' : is22Mode ? '2.2 active' : '2.1 active';
        elements.effectsSubwooferModeBadge.classList.toggle('is-active', true);
    }
    if (elements.effectsSubwooferLevelLabel) elements.effectsSubwooferLevelLabel.textContent = is22StereoMode ? 'Left Sub level' : is22Mode ? 'Sub 1 level' : 'Sub level';
    if (elements.effectsSubwooferDelayLabel) elements.effectsSubwooferDelayLabel.textContent = is22StereoMode ? 'Left Sub alignment' : is22Mode ? 'Sub 1 alignment' : 'Sub alignment';
    if (elements.effectsSubwooferPolarityLabel) elements.effectsSubwooferPolarityLabel.textContent = is22StereoMode ? 'Left Sub polarity' : is22Mode ? 'Sub 1 polarity' : 'Sub polarity';
    if (elements.effectsSubwooferSub1GroupLabel) elements.effectsSubwooferSub1GroupLabel.textContent = is22StereoMode ? 'Left Sub' : is22Mode ? 'Sub 1' : 'Subwoofer';
    if (elements.effectsSubwooferSub2GroupLabel) elements.effectsSubwooferSub2GroupLabel.textContent = is22StereoMode ? 'Right Sub' : 'Sub 2';
    if (elements.effectsSubwooferSub2LevelLabel) elements.effectsSubwooferSub2LevelLabel.textContent = is22StereoMode ? 'Right Sub level' : 'Sub 2 level';
    if (elements.effectsSubwooferSub2DelayLabel) elements.effectsSubwooferSub2DelayLabel.textContent = is22StereoMode ? 'Right Sub alignment' : 'Sub 2 alignment';
    if (elements.effectsSubwooferSub2PolarityLabel) elements.effectsSubwooferSub2PolarityLabel.textContent = is22StereoMode ? 'Right Sub polarity' : 'Sub 2 polarity';
    elements.effectsSubwooferSub2Fields?.forEach(field => field.classList.toggle('hidden', !is22Mode));
    elements.effectsSubwooferDerivedDelays?.classList.toggle('hidden', !is22Mode);
    if (elements.effectsSubwooferFrequencyNumber && !_activeEditing.has(elements.effectsSubwooferFrequencyNumber)) {
        elements.effectsSubwooferFrequencyNumber.value = String(subwoofer.crossover_frequency_hz);
    }
    if (elements.effectsSubwooferMainHighpass) {
        elements.effectsSubwooferMainHighpass.value = subwoofer.main_highpass_enabled ? 'on' : 'off';
    }
    if (elements.effectsSubwooferLevel && !_activeEditing.has(elements.effectsSubwooferLevel)) {
        elements.effectsSubwooferLevel.value = String(subwoofer.sub_level_db);
    }
    if (elements.effectsSubwooferDelay && !_activeEditing.has(elements.effectsSubwooferDelay)) {
        elements.effectsSubwooferDelay.value = String(subwoofer.sub_alignment_ms);
    }
    if (elements.effectsSubwooferPolarity && !_activeEditing.has(elements.effectsSubwooferPolarity)) {
        elements.effectsSubwooferPolarity.value = subwoofer.sub_polarity;
    }
    if (elements.effectsSubwooferSub2Level && !_activeEditing.has(elements.effectsSubwooferSub2Level)) {
        elements.effectsSubwooferSub2Level.value = String(subwoofers.sub2.level_db);
    }
    if (elements.effectsSubwooferSub2Delay && !_activeEditing.has(elements.effectsSubwooferSub2Delay)) {
        elements.effectsSubwooferSub2Delay.value = String(subwoofers.sub2.alignment_ms);
    }
    if (elements.effectsSubwooferSub2Polarity && !_activeEditing.has(elements.effectsSubwooferSub2Polarity)) {
        elements.effectsSubwooferSub2Polarity.value = subwoofers.sub2.polarity;
    }
    if (is22Mode) {
        if (elements.effectsSubwooferDdMain) elements.effectsSubwooferDdMain.textContent = formatSubwooferDelayMs(outputMode.derived_main_delay_ms);
        if (elements.effectsSubwooferDdSub1) elements.effectsSubwooferDdSub1.textContent = formatSubwooferDelayMs(outputMode.derived_sub1_delay_ms);
        if (elements.effectsSubwooferDdSub2) elements.effectsSubwooferDdSub2.textContent = formatSubwooferDelayMs(outputMode.derived_sub2_delay_ms);
    }
    scheduleSubwooferPreviewDraw(subwoofer);
}

function clearSubwooferActiveEditing() {
    [
        elements.effectsSubwooferFrequencyNumber,
        elements.effectsSubwooferMainHighpass,
        elements.effectsSubwooferLevel,
        elements.effectsSubwooferDelay,
        elements.effectsSubwooferPolarity,
        elements.effectsSubwooferSub2Level,
        elements.effectsSubwooferSub2Delay,
        elements.effectsSubwooferSub2Polarity,
    ].forEach((el) => {
        if (el) _activeEditing.delete(el);
    });
}

function getSubwooferPreviewLayout(width, height) {
    const pad = { left: 56, right: 24, top: 18, bottom: 24 };
    return {
        pad,
        plotW: Math.max(1, width - pad.left - pad.right),
        plotH: Math.max(1, height - pad.top - pad.bottom),
    };
}

function drawSubwooferPreview(subwoofer) {
    const canvas = elements.effectsSubwooferPreview;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const settings = normalizeSubwooferSettings(subwoofer || {});
    const displayWidth = Math.max(320, Math.round(canvas.clientWidth || canvas.width || 560));
    const displayHeight = Math.max(112, Math.round(canvas.clientHeight || canvas.height || 132));
    const dpr = window.devicePixelRatio || 1;
    const targetWidth = Math.round(displayWidth * dpr);
    const targetHeight = Math.round(displayHeight * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
        canvas.width = targetWidth;
        canvas.height = targetHeight;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const width = displayWidth;
    const height = displayHeight;
    const { pad, plotW, plotH } = getSubwooferPreviewLayout(width, height);
    const minHz = 20;
    const maxHz = 300;
    const minDb = -18;
    const maxDb = 0;
    const crossover = settings.crossover_frequency_hz;
    const xForHz = (hz) => pad.left + ((Math.log10(hz) - Math.log10(minHz)) / (Math.log10(maxHz) - Math.log10(minHz))) * plotW;
    const yForDb = (db) => pad.top + ((maxDb - db) / (maxDb - minDb)) * plotH;
    const responseDb = (hz, highpass) => {
        const ratio = highpass ? hz / crossover : crossover / hz;
        const magnitude = 1 / Math.sqrt(1 + Math.pow(ratio, -8));
        return 20 * Math.log10(Math.max(0.001, magnitude));
    };
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#08111f';
    ctx.fillRect(0, 0, width, height);
    ctx.fillStyle = 'rgba(255,255,255,0.035)';
    ctx.fillRect(pad.left, pad.top, plotW, plotH);
    ctx.strokeStyle = 'rgba(255,255,255,0.10)';
    ctx.lineWidth = 1;
    const frequencyLabels = [40, 80, 120, 200];
    const dbLabels = [0, -6, -12, -18];
    frequencyLabels.forEach((hz) => {
        const x = xForHz(hz);
        ctx.beginPath();
        ctx.moveTo(x, pad.top);
        ctx.lineTo(x, pad.top + plotH);
        ctx.stroke();
    });
    dbLabels.forEach((db) => {
        const y = yForDb(db);
        ctx.beginPath();
        ctx.moveTo(pad.left, y);
        ctx.lineTo(pad.left + plotW, y);
        ctx.stroke();
    });
    ctx.fillStyle = 'rgba(229,231,235,0.54)';
    ctx.font = '500 10px system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    dbLabels.forEach((db) => {
        const y = yForDb(db);
        ctx.fillText(`${db} dB`, pad.left - 8, y);
    });
    ctx.textAlign = 'center';
    ctx.textBaseline = 'alphabetic';
    frequencyLabels.forEach((hz) => {
        ctx.fillText(`${hz} Hz`, xForHz(hz), height - 8);
    });
    ctx.textAlign = 'start';
    const drawCurve = (highpass, color) => {
        ctx.beginPath();
        for (let i = 0; i <= 160; i += 1) {
            const t = i / 160;
            const hz = Math.pow(10, Math.log10(minHz) + t * (Math.log10(maxHz) - Math.log10(minHz)));
            const db = responseDb(hz, highpass) + (highpass ? 0 : settings.sub_level_db || 0);
            const x = xForHz(hz);
            const y = yForDb(Math.max(minDb, Math.min(maxDb, db)));
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 3;
        ctx.stroke();
    };
    drawCurve(false, '#6ee7b7');
    if (settings.main_highpass_enabled) drawCurve(true, '#93c5fd');
    const markerX = xForHz(crossover);
    ctx.strokeStyle = 'rgba(255,255,255,0.72)';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(markerX, pad.top);
    ctx.lineTo(markerX, pad.top + plotH);
    ctx.stroke();
    ctx.fillStyle = '#e5e7eb';
    ctx.font = '600 11px system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const markerLabel = `${crossover} Hz`;
    const markerLabelWidth = ctx.measureText(markerLabel).width + 12;
    const markerLabelX = Math.max(pad.left + markerLabelWidth / 2 + 2, Math.min(pad.left + plotW - markerLabelWidth / 2 - 2, markerX));
    const markerLabelY = pad.top + plotH * 0.72;
    ctx.fillStyle = 'rgba(12,18,28,0.82)';
    ctx.fillRect(markerLabelX - markerLabelWidth / 2, markerLabelY - 9, markerLabelWidth, 18);
    ctx.strokeStyle = 'rgba(255,255,255,0.16)';
    ctx.lineWidth = 1;
    ctx.strokeRect(markerLabelX - markerLabelWidth / 2, markerLabelY - 9, markerLabelWidth, 18);
    ctx.fillStyle = '#e5e7eb';
    ctx.fillText(markerLabel, markerLabelX, markerLabelY);
    ctx.textAlign = 'start';
}

let _subwooferFeedbackTimer = null;
function setSubwooferFeedback(message, cls = '') {
    if (!elements.effectsSubwooferFeedback) return;
    window.clearTimeout(_subwooferFeedbackTimer);
    elements.effectsSubwooferFeedback.textContent = message;
    elements.effectsSubwooferFeedback.className = 'effects-extras-feedback' + (cls ? ' ' + cls : '');
    if (cls === 'success') {
        _subwooferFeedbackTimer = window.setTimeout(() => {
            if (!elements.effectsSubwooferFeedback) return;
            elements.effectsSubwooferFeedback.textContent = '';
            elements.effectsSubwooferFeedback.className = 'effects-extras-feedback';
        }, 1400);
    }
}

let _subwooferSaveTimer = null;
let _subwooferSavePromise = null;
let _subwooferPendingSave = null;
let _subwooferLastRequestedSignature = '';
const SUBWOOFER_COMMIT_DEBOUNCE_MS = 600;

function updateSubwooferDraftFromControls() {
    const mode = state.settings.audioOutputs.output_mode?.mode || 'stereo';
    const settings = isSubwoofer22Mode(mode) ? collectSubwoofer22Settings() : collectSubwooferSettings();
    state.settings.audioOutputs.output_mode = {
        ...(state.settings.audioOutputs.output_mode || {}),
        ...(settings.subwoofers ? settings : { subwoofer: settings }),
    };
    renderSubwooferPanel();
    return settings;
}

function beginSubwooferSave(pending) {
    const previousSave = _subwooferSavePromise;
    const run = (async () => {
        if (previousSave) await previousSave;
        _subwooferLastRequestedSignature = pending.signature;
        setSubwooferFeedback('Applying…');
        const result = await saveAudioOutputMode(pending.mode, pending.settings, { propagateError: true });
        if (!result) throw new Error('Subwoofer settings save was superseded before commit');
        return result;
    })();
    _subwooferSavePromise = run;
    run.then(
        () => {
            if (_subwooferSavePromise === run) _subwooferSavePromise = null;
        },
        () => {
            if (_subwooferSavePromise === run) _subwooferSavePromise = null;
        },
    );
    return run;
}

function createPendingSubwooferSave(mode, settings, signature) {
    let resolvePending;
    let rejectPending;
    const promise = new Promise((resolve, reject) => {
        resolvePending = resolve;
        rejectPending = reject;
    });
    const pending = {
        mode,
        settings,
        signature,
        promise,
        started: false,
        reject: rejectPending,
        start: null,
    };
    pending.start = () => {
        if (pending.started) return pending.promise;
        pending.started = true;
        if (_subwooferPendingSave === pending) _subwooferPendingSave = null;
        _subwooferSaveTimer = null;
        beginSubwooferSave(pending).then(resolvePending, rejectPending);
        return pending.promise;
    };
    // Background debounced saves are intentionally fire-and-forget, but their
    // rejection remains observable to the measurement preflush if awaited.
    promise.catch(() => {});
    return pending;
}

function cancelPendingSubwooferSave(reason = 'Subwoofer settings save superseded') {
    if (_subwooferSaveTimer !== null) {
        window.clearTimeout(_subwooferSaveTimer);
        _subwooferSaveTimer = null;
    }
    const pending = _subwooferPendingSave;
    _subwooferPendingSave = null;
    if (pending && !pending.started) pending.reject(new Error(reason));
}

function saveSubwooferDebounced(delayMs = SUBWOOFER_COMMIT_DEBOUNCE_MS) {
    cancelPendingSubwooferSave();
    const settings = updateSubwooferDraftFromControls();
    const mode = state.settings.audioOutputs.output_mode?.mode || 'stereo';
    const signature = getAudioOutputModeSignature(mode, settings);
    if (signature === _subwooferLastRequestedSignature) {
        return _subwooferSavePromise || Promise.resolve(null);
    }
    state.settings.audioOutputs.output_mode = {
        ...(state.settings.audioOutputs.output_mode || {}),
        ...(settings.subwoofers ? settings : { subwoofer: settings }),
    };
    const pending = createPendingSubwooferSave(mode, settings, signature);
    _subwooferPendingSave = pending;
    _subwooferSaveTimer = window.setTimeout(() => pending.start(), delayMs);
    return pending.promise;
}

function loadSavedEffectsExtras() {
    const fx = state.dsp;
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
        loudnessEnabled: elements.effectsLoudnessEnabled?.checked || false,
        loudnessStrength: normalizeEffectsLoudnessStrength(elements.effectsLoudnessStrength?.value, 10),
        loudnessFftSize: normalizeEffectsLoudnessFftSize(elements.effectsLoudnessFftSize?.value, 4096),
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
        const resp = await fetch('/api/dsp/extras', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(extras),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save output extras');
        state.dsp = state.dsp || {};
        state.dsp.global_extras = data.extras || {
            limiter: { enabled: !!extras.limiterEnabled, params: { thresholdDb: -1.0, attackMs: 5.0, releaseMs: 50.0, lookaheadMs: 5.0, stereoLinkPercent: 100.0 } },
            headroom: { enabled: !!extras.headroomEnabled, params: { gainDb: extras.headroomGainDb } },
            autogain: { enabled: !!extras.autogainEnabled, params: { targetDb: extras.autogainTargetDb } },
            loudness: {
                enabled: !!extras.loudnessEnabled,
                params: {
                    ...(state.dsp?.global_extras?.loudness?.params || {}),
                    strength: extras.loudnessStrength,
                    fftSize: extras.loudnessFftSize,
                },
            },
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
        const resp = await fetch('/api/dsp/presets/import-json', {
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
        const resp = await fetch('/api/dsp/presets/import-bundle', {
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
        const resp = await fetch('/api/dsp/presets/create-with-ir', {
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
    const presetName = state.dsp.active_preset;
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
        const resp = await fetch('/api/dsp/presets/delete', {
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
function updatePowerButtonConnectionState() {
    if (!elements.powerMenuToggle) return;
    const online = !!state.wsConnected;
    elements.powerMenuToggle.classList.toggle('is-online', online);
    elements.powerMenuToggle.title = online ? 'FXRoute online' : 'FXRoute offline';
    elements.powerMenuToggle.setAttribute('aria-label', online ? 'System power (online)' : 'System power (offline)');
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
    cue.className = 'toast info now-playing-cue no-cover';
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
    if (elements.playlistDetailBack) {
        elements.playlistDetailBack.addEventListener('click', () => closePlaylistDetail());
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
// Shared JSON fetch helpers: non-2xx responses throw with the server's
// detail message (when present) instead of silently resolving to null.
async function apiFetchJson(url, options = {}) {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
        const detail = data && (data.detail || data.error || data.message);
        throw new Error(detail || `HTTP ${resp.status} ${url}`);
    }
    return data;
}

function apiPostJson(url, body) {
    return apiFetchJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
    });
}

function escapeHtml(text) {
    if (!text) return '';
    // Escapes quotes too so the result is safe inside double-quoted attributes.
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
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
    // The Spotify tab renders through the shared streaming shell
    // (streaming.js .streaming-shell), so the legacy in-tab player DOM is gone;
    // only the tab button still lives in this object.
    tabBtn: document.querySelector('[data-tab="spotify"]'),
};

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

function setSpotifyUiVisibility(installed) {
    const available = installed === true;
    const visible = available && !nonAppSourceModeActive();
    const tabPanel = document.getElementById('tab-spotify');
    if (spotifyElements.tabBtn) {
        spotifyElements.tabBtn.hidden = !available;
        spotifyElements.tabBtn.style.display = available ? '' : 'none';
        spotifyElements.tabBtn.classList.toggle('hidden', !visible);
        spotifyElements.tabBtn.setAttribute('aria-selected', visible && window.__visibleTab === 'spotify' ? 'true' : 'false');
    }
    if (tabPanel) {
        tabPanel.hidden = !available;
        tabPanel.classList.toggle('hidden', !visible);
    }
    updateTabsScrollAffordance();
    if (!visible && window.__visibleTab === 'spotify') {
        switchTab('radio');
    }
}

function handleIncomingSpotifyState(data, options = {}) {
    if (!data) return;
    const { renderTab = true, renderFooter = true } = options;
    const previousData = window.__spotifyLastData || {};
    const mergedData = mergeSpotifyState(data);
    setSpotifyUiVisibility(mergedData.installed === true);
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
    }
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
    try {
        const data = await apiPostJson(`/api/streaming/qobuz/${action}`);
        window.__qobuzLastData = data;
        reconcileFooterSource();
        updateFooterForStreamingOwner(data);
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
    if (typeof data.volume === 'number' && !volumeGestureActive) {
        state.playback.volume = data.volume;
        renderVolumeControlsFromActualVolume(data.volume);
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
    document.body.classList.remove('source-local', 'source-radio');
    document.body.classList.add('source-local');
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

// System power menu (suspend / shut down via systemd-logind / D-Bus)
// =================================================================
// The backend reports the textual logind CanSuspend / CanPowerOff state
// and exposes two narrow POST endpoints (suspend / power-off).  This
// module only fetches capabilities, toggles a tiny dropdown and asks
// for confirmation before the destructive POST.
const POWER_CAPABILITIES_REFRESH_MS = 60 * 1000;
const POWER_CONFIRM_SHUTDOWN = 'Shut down the host now? Active playback will stop.';
const POWER_CONFIRM_SUSPEND = 'Suspend the host now? Active playback will stop.';

function setupPowerMenu() {
    if (!elements.powerMenuRoot || !elements.powerMenuToggle || !elements.powerMenu) return;
    elements.powerMenuToggle.addEventListener('click', ev => {
        ev.stopPropagation();
        const open = !elements.powerMenu.classList.contains('hidden');
        setPowerMenuOpen(!open);
    });
    elements.powerMenu.addEventListener('click', ev => ev.stopPropagation());
    if (elements.powerSuspend) {
        elements.powerSuspend.addEventListener('click', () => {
            setPowerMenuOpen(false);
            handlePowerAction('suspend');
        });
    }
    if (elements.powerShutdown) {
        elements.powerShutdown.addEventListener('click', () => {
            setPowerMenuOpen(false);
            handlePowerAction('power-off');
        });
    }
    document.addEventListener('click', () => setPowerMenuOpen(false));
    document.addEventListener('keydown', ev => {
        if (ev.key === 'Escape') setPowerMenuOpen(false);
    });
    refreshPowerCapabilities();
    setInterval(refreshPowerCapabilities, POWER_CAPABILITIES_REFRESH_MS);
}

function setPowerMenuOpen(open) {
    if (!elements.powerMenu || !elements.powerMenuToggle) return;
    elements.powerMenu.classList.toggle('hidden', !open);
    elements.powerMenuToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function refreshPowerCapabilities() {
    fetch('/api/system/power')
        .then(resp => resp.ok ? resp.json() : Promise.reject(new Error(`HTTP ${resp.status}`)))
        .then(applyPowerCapabilities)
        .catch(() => applyPowerCapabilities({ available: false, suspend: 'unavailable', power_off: 'unavailable', suspend_supported: false, power_off_supported: false }));
}

function applyPowerCapabilities(caps) {
    state.powerCapabilities = caps || {};
    const suspendSupported = !!(caps && caps.suspend_supported);
    const powerOffSupported = !!(caps && caps.power_off_supported);
    if (!elements.powerMenuRoot) return;
    const anySupported = suspendSupported || powerOffSupported;
    elements.powerMenuRoot.classList.toggle('hidden', !anySupported);
    if (elements.powerSuspend) {
        elements.powerSuspend.classList.toggle('hidden', !suspendSupported);
    }
    if (elements.powerShutdown) {
        elements.powerShutdown.classList.toggle('hidden', !powerOffSupported);
    }
    // Close the menu when both items disappear mid-refresh.
    if (!anySupported) setPowerMenuOpen(false);
}

async function handlePowerAction(action) {
    const isShutdown = action === 'power-off';
    const button = isShutdown ? elements.powerShutdown : elements.powerSuspend;
    const confirmMsg = isShutdown ? POWER_CONFIRM_SHUTDOWN : POWER_CONFIRM_SUSPEND;
    if (!confirm(confirmMsg)) return;

    if (button) {
        button.dataset.pending = 'true';
        button.classList.add('active');
    }
    const endpoint = isShutdown ? '/api/system/power/power-off' : '/api/system/power/suspend';
    const pendingLabel = isShutdown ? 'Shutting down…' : 'Suspending…';

    try {
        const resp = await fetch(endpoint, { method: 'POST' });
        if (!resp.ok) {
            const detail = await resp.json().catch(() => ({}));
            const message = (detail && detail.detail) || `Power action failed (${resp.status})`;
            showToast(message, 'error');
            return;
        }
        // Expect the host to drop the websocket within a few seconds.
        showToast(pendingLabel, 'info');
    } catch (e) {
        showToast(e && e.message ? e.message : 'Power action failed', 'error');
    } finally {
        if (button) {
            button.dataset.pending = 'false';
            button.classList.remove('active');
        }
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupPowerMenu);
} else {
    setupPowerMenu();
}
