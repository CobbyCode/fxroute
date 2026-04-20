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
    },
    library: {
        tracks: [],
        scanning: false,
        selectedTrackIds: [],
    },
    stations: [],
    download: null,
    easyeffects: {
        available: false,
        preset_count: 0,
        active_preset: null,
        presets: [],
        ir_count: 0,
        irs: [],
    },
    wsConnected: false,
};
// WebSocket
let ws = null;
let reconnectAttempts = 0;
let playbackActionInFlight = false;
let pendingPlaybackRequestId = 0;
let pauseActionRequestId = 0;
let volumeTimer = null;
let volumeRequestInFlight = false;
let pendingVolume = null;
let volumeGestureActive = false;
let optimisticVolume = null;
let lastConfirmedVolume = state.playback.volume;
let volumeSyncGraceUntil = 0;
// Seek - globals
let seekDragging = false;
let seekPendingPos = null;
const VOLUME_SEND_DEBOUNCE_MS = 120;
const VOLUME_SYNC_GRACE_MS = 700;
// DOM elements
const elements = {
    offlineIndicator: document.getElementById('offline-indicator'),
    tabs: document.querySelectorAll('.tab-btn'),
    tabPanels: document.querySelectorAll('.tab-panel'),
    stationsGrid: document.getElementById('stations-grid'),
    toggleStationManageBtn: document.getElementById('toggle-station-manage'),
    closeStationManageBtn: document.getElementById('close-station-manage'),
    radioManagePanel: document.getElementById('radio-manage-panel'),
    stationName: document.getElementById('station-name'),
    stationUrl: document.getElementById('station-url'),
    stationSaveBtn: document.getElementById('station-save'),
    stationDeleteSelect: document.getElementById('station-delete-select'),
    stationDeleteBtn: document.getElementById('station-delete'),
    stationFormStatus: document.getElementById('station-form-status'),
    toggleImportBtn: document.getElementById('toggle-import'),
    libraryImportPanel: document.getElementById('library-import-panel'),
    refreshLibraryBtn: document.getElementById('refresh-library'),
    libraryInfo: document.getElementById('library-info'),
    deleteSelectedTracksBtn: document.getElementById('delete-selected-tracks'),
    tracksList: document.getElementById('tracks-list'),
    downloadUrl: document.getElementById('download-url'),
    downloadBtn: document.getElementById('download-btn'),
    cancelDownloadBtn: document.getElementById('cancel-download'),
    uploadTrackFile: document.getElementById('upload-track-file'),
    uploadTrackBtn: document.getElementById('upload-track-btn'),
    downloadStatus: document.getElementById('download-status'),
    refreshEffectsBtn: document.getElementById('refresh-effects'),
    effectsInfo: document.getElementById('effects-info'),
    effectsPresetSelect: document.getElementById('effects-preset-select'),
    effectsApplyBtn: document.getElementById('effects-apply'),
    effectsDeleteBtn: document.getElementById('effects-delete'),
    effectsIrUpload: document.getElementById('effects-ir-upload'),
    effectsNewPresetName: document.getElementById('effects-new-preset-name'),
    effectsLoadAfterCreate: document.getElementById('effects-load-after-create'),
    effectsCreatePresetBtn: document.getElementById('effects-create-preset'),
    effectsStatus: document.getElementById('effects-status'),
    playbackBar: document.getElementById('playback-bar'),
    trackTitle: document.getElementById('track-title'),
    trackArtist: document.getElementById('track-artist'),
    playbackEq: document.getElementById('playback-eq'),
    connDot: document.getElementById('connection-dot'),
    connText: document.getElementById('connection-text'),
    btnPlayPause: document.getElementById('btn-play-pause'),
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
    try { initSeek(); } catch(e) { console.error('initSeek crashed:', e); }
    try { setupStationActions(); } catch(e) { console.error('setupStationActions crashed:', e); }
    try { setupLibraryActions(); } catch(e) { console.error('setupLibraryActions crashed:', e); }
    try { setupDownloadActions(); } catch(e) { console.error('setupDownloadActions crashed:', e); }
    try { setupEffectsActions(); } catch(e) { console.error('setupEffectsActions crashed:', e); }
    try { fetchInitialData(); } catch(e) { console.error('fetchInitialData crashed:', e); }
});
// WebSocket
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
    ws = new WebSocket(CONFIG.wsUrl);
    ws.onopen = () => {
        console.log('WebSocket connected');
        state.wsConnected = true;
        reconnectAttempts = 0;
        updateConnectionBadge(true);
        elements.offlineIndicator.classList.add('hidden');
        stopMetadataPolling();
    };
    ws.onclose = () => {
        console.log('WebSocket disconnected');
        state.wsConnected = false;
        updateConnectionBadge(false);
        elements.offlineIndicator.classList.remove('hidden');
        startMetadataPolling();
        scheduleReconnect();
    };
    ws.onerror = (err) => {
        console.error('WebSocket error:', err);
    };
    ws.onmessage = (event) => {
        try {
            const message = JSON.parse(event.data);
            handleWebSocketMessage(message);
        } catch (e) {
            console.error('Failed to parse WS message:', e);
        }
    };
}
function scheduleReconnect() {
    reconnectAttempts++;
    console.log(`Reconnecting in ${CONFIG.reconnectInterval}ms (attempt ${reconnectAttempts})`);
    setTimeout(connectWebSocket, CONFIG.reconnectInterval);
}
function handleWebSocketMessage(msg) {
    const { type, data } = msg;
    switch (type) {
        case 'init':
            // Initial state
            if (data.player) {
                mergePlaybackState(data.player.state);
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
            if (data.player && data.player.state && data.player.state.easyeffects) {
                state.easyeffects = data.player.state.easyeffects;
                renderEffects();
            }
            break;
        case 'playback':
            mergePlaybackState(data);
            updatePlaybackUI();
            break;
        case 'download':
            state.download = data;
            updateDownloadUI();
            break;
        case 'easyeffects':
            state.easyeffects = data;
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
function switchTab(tabId) {
    elements.tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === tabId));
    elements.tabPanels.forEach(p => p.classList.toggle('active', p.id === `tab-${tabId}`));
}
// Playback controls
function setupPlaybackControls() {
    if (!elements.btnPlayPause || !elements.volumeSlider) {
        console.error('Playback controls are missing in the DOM');
        return;
    }
    elements.btnPlayPause.addEventListener('click', togglePlayback);
    elements.volumeSlider.addEventListener('input', handleVolumeChange);
    elements.volumeSlider.addEventListener('change', (e) => {
        const volume = parseInt(e.target.value, 10);
        volumeGestureActive = false;
        queueVolumeSend(volume, true);
    });
    updatePlaybackUI();
}
async function stopPlayback() {
    try {
        const resp = await fetch('/api/stop', { method: 'POST' });
        if (!resp.ok) throw new Error('Stop failed');
    } catch (e) {
        showToast('Failed to stop playback', 'error');
    }
}
async function togglePlayback() {
    if (playbackActionInFlight || !state.playback.current_track) return;
    const requestId = ++pauseActionRequestId;
    playbackActionInFlight = true;
    const previousPlaying = !!state.playback.playing;
    const previousPaused = !!state.playback.paused;
    const previousEnded = !!state.playback.ended;
    const canTogglePause = !!state.playback.current_file && !previousEnded;
    if (canTogglePause) {
        state.playback.playing = previousPaused;
        state.playback.paused = previousPlaying;
    } else {
        state.playback.playing = true;
        state.playback.paused = false;
        state.playback.ended = false;
    }
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
function setLocalVolume(volume) {
    state.playback.volume = volume;
    elements.volumeSlider.value = volume;
    elements.volumeDisplay.textContent = `${volume}%`;
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
    const volume = parseInt(e.target.value, 10);
    volumeGestureActive = true;
    optimisticVolume = volume;
    volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
    setLocalVolume(volume);
    queueVolumeSend(volume);
}
// Metadata polling for radio ICY tags
let metadataPollTimer = null;
function startMetadataPolling() {
    if (metadataPollTimer !== null) return;
    metadataPollTimer = setInterval(fetchMetadata, 10000);
}
function stopMetadataPolling() {
    if (metadataPollTimer === null) return;
    clearInterval(metadataPollTimer);
    metadataPollTimer = null;
}
async function fetchMetadata() {
    if (!state.playback.playing && !state.playback.paused) return;
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.metadata && Object.keys(data.metadata).length > 0) {
            const meta = data.metadata;
            const title = (meta['icy-title'] || meta['title'] || '').trim();
            if (title && state.playback.current_track && state.playback.current_track.source === 'radio') {
                state.playback.live_title = title;
                updatePlaybackUI();
            }
        }
        // Update volume from state if changed
        if (data.volume !== undefined) {
            applyRemoteVolume(data.volume);
            if (!volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
                elements.volumeSlider.value = state.playback.volume;
                elements.volumeDisplay.textContent = state.playback.volume + '%';
            }
        }
    } catch (e) {}
}
function updatePlaybackUI() {
    const { current_track, volume, playing, paused, live_title } = state.playback;
    // Set body dataset for CSS radio/song rules
    const isRadio = current_track && current_track.source === 'radio';
    document.body.classList.remove('source-local', 'source-radio');
    document.body.classList.add(isRadio ? 'source-radio' : 'source-local');
    // Hide seek-row on radio, show on local
    const seekRow = document.querySelector('.seek-row');
    if (seekRow) seekRow.style.display = isRadio ? 'none' : '';
    // Handle seek-row visibility (JS beats CSS for inline styles)
    document.body.classList.remove('is-playing', 'is-paused');
    document.body.classList.add(playing ? 'is-playing' : 'is-paused');
    // Track info
    if (current_track) {
        elements.trackTitle.textContent = isRadio && live_title ? live_title : current_track.title;
        elements.trackTitle.classList.remove('placeholder');
        // Hide left-column title text for radio (EQ bars stay visible in .track-info)
        elements.trackTitle.style.display = 'none';
        elements.trackTitle.classList.add('placeholder');
        // Also update center column song info
        const scArtist = document.getElementById('sc-artist');
        const scTitle = document.getElementById('sc-title');
        if (scArtist) scArtist.textContent = isRadio && live_title ? current_track.title : (current_track.artist || '');
        if (scTitle) scTitle.textContent = isRadio && live_title ? live_title : current_track.title;
        elements.trackArtist.textContent = isRadio && live_title
            ? current_track.title
            : (current_track.artist || '');
        // Hide left-column artist text for radio (EQ bars stay visible)
        elements.trackArtist.style.display = 'none';
    } else {
        elements.trackTitle.textContent = 'Not playing';
        elements.trackTitle.classList.add('placeholder');
        elements.trackArtist.textContent = '';
        // Show left-column when not playing
        if (elements.trackTitle) elements.trackTitle.style.display = '';
        if (elements.trackArtist) elements.trackArtist.style.display = '';
    }
    // EQ bar & bar glow
    if (elements.playbackEq) {
        elements.playbackEq.style.display = playing ? 'inline-flex' : 'none';
    }
    if (elements.playbackBar) {
        elements.playbackBar.classList.toggle('is-playing', !!playing);
    }
    // Play/pause button
    updatePlayPauseButton(playing ? 'playing' : (paused ? 'paused' : 'stopped'));
    // Seek bar
    updateSeekUI();
    // Volume
    if (!volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
        elements.volumeSlider.value = volume;
    }
    elements.volumeDisplay.textContent = `${volume}%`;
    // Highlight active
    highlightActiveTrack();
}
function updatePlayPauseButton(playbackState) {
    elements.btnPlayPause.textContent = playbackState === 'playing' ? '⏸' : '▶';
    elements.btnPlayPause.disabled = playbackActionInFlight || (!state.playback.current_track && playbackState === 'stopped');
}
function highlightActiveTrack() {
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
    await Promise.all([fetchStations(), fetchTracks(), fetchEffects(), fetchPlaybackStatus()]);
}
async function fetchPlaybackStatus() {
    try {
        const resp = await fetch('/api/status');
        if (!resp.ok) throw new Error('Failed to fetch playback status');
        const data = await resp.json();
        mergePlaybackState(data);
        updatePlaybackUI();
    } catch (e) {
        console.debug('Playback status unavailable on load', e);
    }
}
function setupStationActions() {
    elements.stationSaveBtn.addEventListener('click', saveStation);
    elements.stationDeleteBtn.addEventListener('click', deleteSelectedStation);
    elements.toggleStationManageBtn.addEventListener('click', () => toggleStationManagePanel(true));
    elements.closeStationManageBtn.addEventListener('click', () => toggleStationManagePanel(false));
    elements.radioManagePanel.querySelector('.manage-overlay-backdrop').addEventListener('click', () => toggleStationManagePanel(false));
}
function toggleStationManagePanel(forceOpen = null) {
    const shouldOpen = forceOpen === null
        ? elements.radioManagePanel.classList.contains('hidden')
        : !!forceOpen;
    elements.radioManagePanel.classList.toggle('hidden', !shouldOpen);
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
function renderStations() {
    const loadingEl = document.querySelector('#tab-radio .loading');
    if (state.stations.length === 0) {
        if (loadingEl) loadingEl.textContent = 'No stations found';
        elements.stationsGrid.innerHTML = '';
        renderStationDeleteOptions();
        return;
    }
    if (loadingEl) loadingEl.style.display = 'none';
    elements.stationsGrid.innerHTML = state.stations.map(station => `
        <div class="station-card" data-station-id="${escapeHtml(station.id)}" role="button" tabindex="0">
            <div class="station-name">${escapeHtml(station.title)}</div>
            <div class="station-genre">${escapeHtml(station.artist || 'Radio')}</div>
        </div>
    `).join('');
    elements.stationsGrid.querySelectorAll('.station-card').forEach(card => {
        card.addEventListener('click', () => playRadio(card.dataset.stationId));
    });
}
function renderStationDeleteOptions() {
    if (!elements.stationDeleteSelect) return;
    if (state.stations.length === 0) {
        elements.stationDeleteSelect.innerHTML = '<option value="">No stations saved</option>';
        elements.stationDeleteBtn.disabled = true;
        return;
    }
    elements.stationDeleteSelect.innerHTML = ['<option value="">Select a station…</option>']
        .concat(state.stations.map(station => `<option value="${escapeHtml(station.id)}">${escapeHtml(station.title)}</option>`))
        .join('');
    elements.stationDeleteBtn.disabled = false;
}
function resetStationForm() {
    elements.stationName.value = '';
    elements.stationUrl.value = '';
    elements.stationFormStatus.textContent = '';
}
async function saveStation() {
    const name = elements.stationName.value.trim();
    const streamUrl = elements.stationUrl.value.trim();
    if (!name) {
        showToast('Please enter a station name', 'error');
        return;
    }
    if (!streamUrl) {
        showToast('Please enter a stream URL', 'error');
        return;
    }
    elements.stationSaveBtn.disabled = true;
    elements.stationFormStatus.textContent = 'Adding station…';
    try {
        const resp = await fetch('/api/stations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, stream_url: streamUrl }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save station');
        await fetchStations();
        resetStationForm();
        showToast(`Added station: ${name}`, 'success');
    } catch (e) {
        elements.stationFormStatus.textContent = e.message || 'Failed to save station';
        showToast(e.message || 'Failed to save station', 'error');
    } finally {
        elements.stationSaveBtn.disabled = false;
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
        elements.stationDeleteSelect.value = '';
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
function renderTracks() {
    const tracks = state.library.tracks;
    const selectedIds = new Set(state.library.selectedTrackIds.filter(id => tracks.some(track => track.id === id)));
    state.library.selectedTrackIds = Array.from(selectedIds);
    const loadingEl = document.querySelector('#tab-library .loading');
    if (tracks.length === 0) {
        if (loadingEl) { loadingEl.textContent = 'No tracks found'; loadingEl.style.display = ''; }
        elements.tracksList.innerHTML = '';
        elements.libraryInfo.textContent = '0 tracks';
        updateLibrarySelectionUI();
        return;
    }
    if (loadingEl) loadingEl.style.display = 'none';
    elements.tracksList.innerHTML = tracks.map(track => {
        const isSelected = selectedIds.has(track.id);
        return `
            <div class="track-item ${isSelected ? 'selected' : ''}" data-track-id="${escapeHtml(track.id)}">
                <label class="track-select">
                    <input type="checkbox" class="track-checkbox" data-track-id="${escapeHtml(track.id)}" ${isSelected ? 'checked' : ''}>
                    <span class="track-select-box"></span>
                </label>
                <button class="track-play-button" data-track-id="${escapeHtml(track.id)}" type="button">
                    <span class="track-item-icon">♫</span>
                    <div class="track-title">${escapeHtml(track.title)}</div>
                    <div class="track-artist">${escapeHtml(track.artist || 'Unknown')}</div>
                </button>
            </div>
        `;
    }).join('');
    elements.libraryInfo.textContent = `${tracks.length} tracks`;
    elements.tracksList.querySelectorAll('.track-play-button').forEach(item => {
        item.addEventListener('click', () => playLocal(item.dataset.trackId));
    });
    elements.tracksList.querySelectorAll('.track-checkbox').forEach(input => {
        input.addEventListener('change', () => toggleTrackSelection(input.dataset.trackId, input.checked));
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
}
function clearTrackSelection() {
    state.library.selectedTrackIds = [];
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
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
function updateLibrarySelectionUI() {
    const count = state.library.selectedTrackIds.length;
    if (elements.deleteSelectedTracksBtn) {
        elements.deleteSelectedTracksBtn.classList.toggle('hidden', count === 0);
    }
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
async function uploadTrackFile() {
    const file = elements.uploadTrackFile.files[0];
    if (!file) {
        showToast('Please choose an audio file', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('file', file);
    elements.uploadTrackBtn.disabled = true;
    try {
        const resp = await fetch('/api/library/upload', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Upload failed');
        elements.uploadTrackFile.value = '';
        showToast(`Uploaded: ${data.filename}`, 'success');
        await fetchTracks();
    } catch (e) {
        showToast(e.message || 'Upload failed', 'error');
    } finally {
        elements.uploadTrackBtn.disabled = false;
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
    const station = state.stations.find(s => s.id === stationId);
    if (!station || playbackActionInFlight) {
        if (!station) showToast('Station not found', 'error');
        return;
    }
    const requestId = ++pendingPlaybackRequestId;
    playbackActionInFlight = true;
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
    if (!track || playbackActionInFlight) {
        if (!track) showToast('Track not found', 'error');
        return;
    }
    const requestId = ++pendingPlaybackRequestId;
    playbackActionInFlight = true;
    state.playback.current_track = track;
    state.playback.live_title = null;
    state.playback.playing = true;
    state.playback.paused = false;
    updatePlaybackUI();
    try {
        const resp = await fetch('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: 'local', track_id: track.id }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Play command failed');
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        if (data.playback) {
            mergePlaybackState(data.playback);
        }
        updatePlaybackUI();
        showToast(`Now playing: ${track.title}`, 'info');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        state.playback.playing = false;
        state.playback.paused = false;
        updatePlaybackUI();
        showToast('Failed to start playback', 'error');
    }
}
// Download
function setupDownloadActions() {
    if (elements.downloadBtn) {
        elements.downloadBtn.addEventListener('click', startDownload);
    }
    if (elements.cancelDownloadBtn) {
        elements.cancelDownloadBtn.addEventListener('click', cancelDownload);
    }
}
function setupEffectsActions() {
    elements.refreshEffectsBtn.addEventListener('click', fetchEffects);
    elements.effectsApplyBtn.addEventListener('click', applyEffectsPreset);
    elements.effectsDeleteBtn.addEventListener('click', deleteEffectsPreset);
    elements.effectsCreatePresetBtn.addEventListener('click', createConvolverPreset);
    elements.effectsIrUpload.addEventListener('change', syncPresetNameFromUpload);
}
async function startDownload() {
    const url = elements.downloadUrl.value.trim();
    if (!url) {
        showToast('Please enter a URL', 'error');
        return;
    }
    if (state.download && state.download.status === 'downloading') {
        showToast('Download already in progress', 'error');
        return;
    }
    try {
        const resp = await fetch('/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url }),
        });
        if (!resp.ok) {
            const err = await resp.json();
            throw new Error(err.detail || 'Download failed');
        }
        elements.downloadUrl.value = '';
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
function updateDownloadUI() {
    const dl = state.download;
    if (!dl) {
        elements.downloadStatus.innerHTML = '';
        elements.cancelDownloadBtn.classList.add('hidden');
        return;
    }
    let html = '';
    if (dl.status === 'downloading') {
        const progress = dl.progress_percent.toFixed(1);
        html = `
            <div class="download-progress">
                <div><strong>${escapeHtml(dl.filename || 'Downloading…')}</strong></div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${progress}%"></div>
                </div>
                <div style="text-align: center; color: var(--text-secondary);">${progress}%</div>
            </div>
        `;
        elements.cancelDownloadBtn.classList.remove('hidden');
        elements.downloadBtn.disabled = true;
    } else if (dl.status === 'complete') {
        html = `<div style="color: var(--success);">Download complete</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
        elements.downloadBtn.disabled = false;
    } else if (dl.status === 'error') {
        html = `<div style="color: var(--danger);">Error: ${escapeHtml(dl.error)}</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
        elements.downloadBtn.disabled = false;
    } else if (dl.status === 'cancelled') {
        html = `<div style="color: var(--text-secondary);">Download cancelled</div>`;
        elements.cancelDownloadBtn.classList.add('hidden');
        elements.downloadBtn.disabled = false;
    }
    elements.downloadStatus.innerHTML = html;
}
async function fetchEffects() {
    try {
        const resp = await fetch('/api/easyeffects/presets');
        if (!resp.ok) throw new Error('Failed to fetch EasyEffects presets');
        state.easyeffects = await resp.json();
        renderEffects();
    } catch (e) {
        elements.effectsStatus.innerHTML = '<div style="color: var(--danger);">EasyEffects presets are unavailable</div>';
    }
}
function renderEffects() {
    const fx = state.easyeffects;
    const presets = fx.presets || [];
    elements.effectsInfo.textContent = fx.available
        ? `${fx.preset_count} presets, ${fx.ir_count || 0} IRs`
        : 'EasyEffects is not available';
    if (!fx.available) {
        elements.effectsPresetSelect.innerHTML = '<option value="">EasyEffects output path is unavailable</option>';
        elements.effectsPresetSelect.disabled = true;
        elements.effectsApplyBtn.disabled = true;
        elements.effectsDeleteBtn.disabled = true;
        elements.effectsCreatePresetBtn.disabled = true;
        elements.effectsStatus.innerHTML = '';
        return;
    }
    if (presets.length === 0) {
        elements.effectsPresetSelect.innerHTML = '<option value="">No presets available</option>';
        elements.effectsPresetSelect.disabled = true;
        elements.effectsApplyBtn.disabled = true;
        elements.effectsDeleteBtn.disabled = true;
    } else {
        elements.effectsPresetSelect.innerHTML = presets.map(preset => {
            const selected = preset.name === fx.active_preset ? 'selected' : '';
            return `<option value="${escapeHtml(preset.name)}" ${selected}>${escapeHtml(preset.name)}</option>`;
        }).join('');
        elements.effectsPresetSelect.disabled = false;
        elements.effectsApplyBtn.disabled = false;
        elements.effectsDeleteBtn.disabled = false;
    }
    elements.effectsCreatePresetBtn.disabled = false;
    elements.effectsStatus.innerHTML = fx.active_preset
        ? `<div>Active preset: <strong>${escapeHtml(fx.active_preset)}</strong></div><div>IR upload supports .irs and .wav files. WAV uploads are converted automatically for EasyEffects.</div>`
        : '<div>No active preset</div><div>IR upload supports .irs and .wav files. WAV uploads are converted automatically for EasyEffects.</div>';
}
async function applyEffectsPreset() {
    const presetName = elements.effectsPresetSelect.value;
    if (!presetName) return;
    elements.effectsApplyBtn.disabled = true;
    elements.effectsStatus.innerHTML = `<div>Applying preset: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/easyeffects/presets/load', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ preset_name: presetName }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Failed to apply preset');
        state.easyeffects.active_preset = presetName;
        renderEffects();
        showToast(`Applied preset: ${presetName}`, 'success');
    } catch (e) {
        elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'Failed to apply preset', 'error');
    } finally {
        elements.effectsApplyBtn.disabled = false;
    }
}
function syncPresetNameFromUpload() {
    const file = elements.effectsIrUpload.files[0];
    if (!file) return;
    if (!elements.effectsNewPresetName.value.trim()) {
        const baseName = file.name.replace(/\.[^.]+$/, '');
        elements.effectsNewPresetName.value = baseName;
    }
}
async function createConvolverPreset() {
    const file = elements.effectsIrUpload.files[0];
    const presetName = elements.effectsNewPresetName.value.trim() || (file ? file.name.replace(/\.[^.]+$/, '') : '');
    const loadAfterCreate = elements.effectsLoadAfterCreate.checked;
    if (!file) {
        showToast('Please select an IR file first', 'error');
        return;
    }
    if (!presetName) {
        showToast('Please enter a preset name', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('preset_name', presetName);
    formData.append('load_after_create', loadAfterCreate ? 'true' : 'false');
    formData.append('file', file);
    elements.effectsCreatePresetBtn.disabled = true;
    elements.effectsStatus.innerHTML = `<div>Uploading IR and creating preset: <strong>${escapeHtml(presetName)}</strong>…</div>`;
    try {
        const resp = await fetch('/api/easyeffects/presets/create-with-ir', {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'Preset creation failed');
        await fetchEffects();
        elements.effectsPresetSelect.value = data.preset.name;
        elements.effectsNewPresetName.value = '';
        elements.effectsIrUpload.value = '';
        elements.effectsStatus.innerHTML = data.loaded
            ? `<div>Preset created and loaded: <strong>${escapeHtml(data.preset.name)}</strong></div>`
            : `<div>Preset created: <strong>${escapeHtml(data.preset.name)}</strong></div>`;
        showToast(`Created preset: ${data.preset.name}`, 'success');
    } catch (e) {
        elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message)}</div>`;
        showToast(e.message || 'Preset creation failed', 'error');
    } finally {
        elements.effectsCreatePresetBtn.disabled = false;
    }
}
async function deleteEffectsPreset() {
    const presetName = elements.effectsPresetSelect.value;
    if (!presetName) return;
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
        elements.effectsStatus.innerHTML = `<div style="color: var(--danger);">${escapeHtml(e.message)}</div>`;
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
        elements.libraryImportPanel.classList.toggle('hidden', !shouldOpen);
        elements.toggleImportBtn.textContent = shouldOpen ? '− Close' : '＋ Import';
    });
    elements.uploadTrackBtn.addEventListener('click', uploadTrackFile);
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
}
function seekEnd() {
    seekDragging = false;
    if (seekPendingPos !== null && state.playback.duration > 0) {
        doSeek(seekPendingPos);
        seekPendingPos = null;
    }
}
function seekChange() {
    const pos = parseInt(elements.seekSlider.value, 10) || 0;
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
