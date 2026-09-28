// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute playback core: native transport, footer ownership/reconcile,
 * volume, polling/metadata and queue commit paths.
 *
 * This module is the single owner of the footer-ownership decisions
 * (window.__footerSource transitions, takeover/hold/freeze windows and the
 * backend-owner resolution). Rendering and gestures live in
 * static/playback_ui.js; the Spotify/Qobuz provider runtime stays in
 * static/app.js until the streaming-runtime extraction and reaches this
 * module through explicit callbacks.
 *
 * State/DOM/toast go through injected getters; sibling UI callbacks and
 * app-owned provider/source callbacks arrive via deps. Browser-loadable
 * UMD, no build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRoutePlaybackCore = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ playback: {}, library: {}, samplerate: {}, settings: {} }),
        getElements: () => ({}),
        fetchFn: (...args) => fetch(...args),
        showToast: () => {},
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        setRangeProgress: () => {},
        nonAppSourceModeActive: () => false,
        updateLiveBanner: () => {},
        renderSettingsPanel: () => {},
        fetchAudioOutputOverview: async () => {},
        fetchAudioSourceOverview: async () => {},
        fetchMeasurements: async () => {},
        fetchProviderAdmin: async () => {},
        requestSubwooferPreviewRedrawFromState: () => {},
        spotifyCommand: async () => {},
        qobuzCommand: async () => {},
        spotifySeek: async () => {},
        qobuzSeek: async () => {},
        startSpotifyPoll: () => {},
        stopSpotifyPoll: () => {},
        startQobuzPoll: () => {},
        stopQobuzPoll: () => {},
        fetchSpotifyStatus: async () => null,
        fetchQobuzStatus: async () => null,
        handleIncomingSpotifyState: () => {},
        handleIncomingQobuzState: () => {},
        shouldPollQobuz: () => false,
        bumpSpotifyPollGeneration: () => {},
        bumpQobuzPollGeneration: () => {},
        claimWsSyncGeneration: () => 0,
        isWsSyncGenerationCurrent: () => false,
        renderLibraryModeButtons: () => {},
        fetchTracks: async () => {},
        fetchPlaylists: async () => {},
        fetchDownloadStatus: async () => {},
        fetchEffects: async () => {},
        fetchStations: async () => {},
        updatePlaybackUI: () => {},
        updateSeekUI: () => {},
        showVolumeDisplayTemporarily: () => {},
        maybeShowNativeTrackCue: () => false,
        seedNativeTrackCueKey: () => {},
        coverQueuePlayTarget: () => null,
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

let playbackActionInFlight = false;
let pendingPlaybackRequestId = 0;
let pendingFooterSingleTrackStart = null;
let pendingOptimisticTrack = null;
let lastRadioTrack = null;
let pauseActionRequestId = 0;
const FOOTER_SINGLE_TRACK_START_LOCK_MS = 5000;
let volumeTimer = null;
let volumeRequestInFlight = false;
let pendingVolume = null;
let volumeGestureActive = false;
let optimisticVolume = null;
// Baseline mirrors app.js: the confirmed volume at load. Lazily synced from
// the app state on first use because init() runs before app.js declares it.
let lastConfirmedVolume = null;
let volumeSyncGraceUntil = 0;
const VOLUME_SEND_DEBOUNCE_MS = 120;
const VOLUME_SYNC_GRACE_MS = 700;
const VOLUME_CURVE_GAMMA = 1.0;
let libraryModeSyncArmed = false;
let lastLibraryPlaybackContextSignature = null;
let metadataPollTimer = null;
let sampleratePollTimer = null;
let samplerateBurstPollTimers = [];
let peakStatusPollTimer = null;
const SAMPLERATE_POLL_INTERVAL_MS = 5000;
const SAMPLERATE_BURST_POLL_DELAYS_MS = [0, 120, 280, 520, 900, 1400, 2200, 3200];
const PEAK_STATUS_POLL_INTERVAL_MS = 1200;
let playbackPositionPollTimer = null;
let _footerContentFreezeUntil = 0;
let _footerContentFreezeTimer = null;
let _spotifyTakeoverUntil = 0;
let _localFooterHoldUntil = 0;

function ensureVolumeBaseline() {
    if (lastConfirmedVolume !== null) return;
    try {
        lastConfirmedVolume = deps.getState().playback.volume;
    } catch (e) {
        lastConfirmedVolume = null;
    }
}

function isPlaybackActionInFlight() {
    return playbackActionInFlight;
}

function setPlaybackActionInFlight(active) {
    playbackActionInFlight = !!active;
}

function getPendingOptimisticTrack() {
    return pendingOptimisticTrack;
}

function setLibraryModeSyncArmed(active) {
    libraryModeSyncArmed = !!active;
}

function isVolumeGestureActive() {
    return volumeGestureActive;
}

function setVolumeGestureActive(active) {
    volumeGestureActive = !!active;
}

function isVolumeRequestInFlight() {
    return volumeRequestInFlight;
}

function getPendingVolume() {
    return pendingVolume;
}

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

function getBackendFooterOwner(playback = deps.getState().playback) {
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
    if (localPlaybackHasFooterContext(deps.getState().playback) || localEndedPlaybackHasFooterContext(deps.getState().playback)) return 'local';
    if (spotifyPausedHasFooterContext()) return 'spotify';
    return isStreamingFooterSource(window.__footerSource) ? window.__footerSource : 'local';
}

function globalTogglePlayback() {
    const source = getEffectivePlaybackControlSource();
    if (source === 'spotify') {
        deps.spotifyCommand('toggle');
    } else if (source === 'qobuz') {
        deps.qobuzCommand('toggle');
    } else {
        togglePlayback();
    }
}

function globalPrevious() {
    const source = getEffectivePlaybackControlSource();
    if (source === 'spotify') {
        deps.spotifyCommand('previous');
    } else if (source === 'qobuz') {
        deps.qobuzCommand('previous');
    } else {
        previousInQueue();
    }
}

function globalNext() {
    const source = getEffectivePlaybackControlSource();
    if (source === 'spotify') {
        deps.spotifyCommand('next');
    } else if (source === 'qobuz') {
        deps.qobuzCommand('next');
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
            const posSec = (parseFloat(deps.getElements().seekSlider.value) / 1000) * streamingData.duration;
            if (window.__footerSource === 'qobuz') deps.qobuzSeek(posSec);
            else deps.spotifySeek(posSec);
        }
    }
}

async function stopPlayback() {
    try {
        const resp = await deps.fetchFn('/api/stop', { method: 'POST' });
        if (!resp.ok) throw new Error('Stop failed');
    } catch (e) {
        deps.showToast('Failed to stop playback', 'error');
    }
}

async function togglePlayback() {
    if (playbackActionInFlight) return;
    const previousPlaying = !!deps.getState().playback.playing;
    const previousPaused = !!deps.getState().playback.paused;
    const previousEnded = !!deps.getState().playback.ended;
    const previousTrack = deps.getState().playback.current_track ? { ...state.playback.current_track } : null;
    const replayRadioTrack = !deps.getState().playback.current_track ? getLastRadioTrack() : null;
    const canTogglePause = !!deps.getState().playback.current_track && !!deps.getState().playback.current_file && !previousEnded;
    if (!canTogglePause) {
        if (replayRadioTrack) {
            deps.getState().playback.current_track = replayRadioTrack;
        } else if (!deps.getState().playback.current_track) {
            return;
        }
    }
    const requestId = ++pauseActionRequestId;
    playbackActionInFlight = true;
    if (canTogglePause) {
        deps.getState().playback.playing = previousPaused;
        deps.getState().playback.paused = previousPlaying;
    } else {
        deps.getState().playback.playing = true;
        deps.getState().playback.paused = false;
        deps.getState().playback.ended = false;
    }
    window.__footerSource = 'local';
    deps.bumpSpotifyPollGeneration();
    deps.updatePlaybackUI();
    try {
        const resp = await deps.fetchFn('/api/playback/toggle', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Playback toggle failed'));
        }
        if (requestId !== pauseActionRequestId) return;
        playbackActionInFlight = false;
        if (data.playback) {
            mergePlaybackState(data.playback);
        } else {
            deps.getState().playback.playing = data.status === 'playing';
            deps.getState().playback.paused = data.status === 'paused';
        }
        deps.updatePlaybackUI();
        if (deps.getState().playback.playing && deps.getState().playback.current_track?.source === 'radio') {
            void fetchMetadata();
        }
    } catch (e) {
        if (requestId !== pauseActionRequestId) return;
        deps.getState().playback.playing = previousPlaying;
        deps.getState().playback.paused = previousPaused;
        deps.getState().playback.ended = previousEnded;
        deps.getState().playback.current_track = previousTrack;
        playbackActionInFlight = false;
        deps.updatePlaybackUI();
        deps.showToast(e.message || 'Failed to toggle playback', 'error');
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
    deps.getElements().volumeSlider.value = sliderValue;
    deps.getElements().volumeDisplay.textContent = `${sliderValue}%`;
    deps.setRangeProgress(deps.getElements().volumeSlider, sliderValue / 100);
}

function setLocalVolume(sliderValue) {
    const clampedSliderValue = clampVolumeValue(sliderValue);
    const actualVolume = sliderVolumeToActualVolume(clampedSliderValue);
    deps.getState().playback.volume = actualVolume;
    deps.getElements().volumeSlider.value = clampedSliderValue;
    deps.getElements().volumeDisplay.textContent = `${clampedSliderValue}%`;
    deps.setRangeProgress(deps.getElements().volumeSlider, clampedSliderValue / 100);
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

function mergePlaybackState(data, { snapshot = false } = {}) {
    if (!data) return;
    const incomingSeq = typeof data._seq === 'number' ? data._seq : null;
    const currentSeq = typeof deps.getState().playback?._seq === 'number' ? deps.getState().playback._seq : null;
    // The player sequence restarts at 0 with every FXRoute process.  A
    // snapshot (the WebSocket init of a (re)connected socket) is the full
    // state of the serving process: it replaces the ordering baseline instead
    // of being judged stale against a previous process, which would otherwise
    // drop every later playback update until the new counter caught up.
    if (!snapshot && incomingSeq !== null && currentSeq !== null && incomingSeq < currentSeq) {
        footerDebug('ignore-stale-playback-state', { incomingSeq, currentSeq });
        return;
    }
    const nextPlayback = { ...data };
    if (snapshot) nextPlayback._seq = incomingSeq;
    const previousRadioMetadata = deps.getState().playback?.radio_metadata;
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
    deps.getState().playback = { ...deps.getState().playback, ...nextPlayback };
    rememberLastRadioTrack(deps.getState().playback.current_track);
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
        mode: deps.getState().playback.queue?.mode || 'app_replace',
        tracks: track ? [track] : [],
        loop: !!deps.getState().library.loop,
        shuffle: false,
    };
}

function footerSingleTrackStartLockActive(playback = deps.getState().playback) {
    const pending = pendingFooterSingleTrackStart;
    if (!pending) return false;
    if (pending.expiresAt && Date.now() > pending.expiresAt) {
        pendingFooterSingleTrackStart = null;
        return false;
    }
    const track = playback?.current_track;
    return !!(track && track.source === 'local' && track.id === pending.trackId);
}

function activeLocalPlaybackBlocksSpotifyOwnership(playback = deps.getState().playback) {
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

function getLibraryPlaybackContext(playback = deps.getState().playback) {
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
        if (deps.getState().library.shuffle || deps.getState().library.loop) {
            deps.getState().library.shuffle = false;
            deps.getState().library.loop = false;
            deps.renderLibraryModeButtons();
        }
        return;
    }

    deps.getState().library.shuffle = context.shuffle;
    deps.getState().library.loop = context.loop;
    deps.renderLibraryModeButtons();
}

function applyRemoteVolume(remoteVolume) {
    ensureVolumeBaseline();
    const matchesOptimistic = optimisticVolume !== null && remoteVolume === optimisticVolume;
    const shouldHoldRemoteVolume = volumeGestureActive || volumeRequestInFlight || pendingVolume !== null || Date.now() < volumeSyncGraceUntil;
    if (shouldHoldRemoteVolume && !matchesOptimistic) {
        return;
    }
    lastConfirmedVolume = remoteVolume;
    deps.getState().playback.volume = remoteVolume;
    if (matchesOptimistic && !volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
        optimisticVolume = null;
    }
}

async function sendVolume() {
    ensureVolumeBaseline();
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
            const resp = await deps.fetchFn('/api/volume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ volume: nextVolume }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Volume change failed');
            lastConfirmedVolume = typeof data.volume === 'number' ? data.volume : nextVolume;
            deps.getState().playback.volume = lastConfirmedVolume;
            volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
            if (!volumeGestureActive && pendingVolume === null) {
                optimisticVolume = null;
            }
        } catch (e) {
            pendingVolume = null;
            volumeGestureActive = false;
            optimisticVolume = null;
            deps.showToast(e.message || 'Failed to set volume', 'error');
            break;
        }
    }
    volumeRequestInFlight = false;
    deps.updatePlaybackUI();
}

async function handleVolumeChange(e) {
    const sliderValue = parseInt(e.target.value, 10);
    const actualVolume = sliderVolumeToActualVolume(sliderValue);
    volumeGestureActive = true;
    optimisticVolume = actualVolume;
    volumeSyncGraceUntil = Date.now() + VOLUME_SYNC_GRACE_MS;
    setLocalVolume(sliderValue);
    deps.showVolumeDisplayTemporarily();
    queueVolumeSend(actualVolume);
}

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
    if (!deps.getState().playback.playing && !deps.getState().playback.paused && !isStreamingFooterSource(window.__footerSource)) return;
    try {
        const resp = await deps.fetchFn('/api/status');
        if (!resp.ok) return;
        const data = await resp.json();
        let needsUiRefresh = false;
        if (data.metadata && Object.keys(data.metadata).length > 0) {
            const meta = data.metadata;
            const title = (meta['icy-title'] || meta['title'] || '').trim();
            if (title && deps.getState().playback.current_track && deps.getState().playback.current_track.source === 'radio') {
                deps.getState().playback.live_title = title;
                needsUiRefresh = true;
            }
        }
        if (data.output_peak_warning) {
            deps.getState().playback.output_peak_warning = data.output_peak_warning;
            needsUiRefresh = true;
        }
        // Update volume from state if changed
        if (data.volume !== undefined) {
            applyRemoteVolume(data.volume);
            if (!volumeGestureActive && !volumeRequestInFlight && pendingVolume === null) {
                renderVolumeControlsFromActualVolume(deps.getState().playback.volume);
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
            deps.updatePlaybackUI();
        }
    } catch (e) {}
}

// Library
async function fetchInitialData() {
    startSampleratePolling();
    void deps.fetchProviderAdmin();
    await Promise.all([deps.fetchStations(), deps.fetchTracks(), deps.fetchEffects(), deps.fetchMeasurements(), fetchPlaybackStatus(), fetchSamplerateStatus(), deps.fetchDownloadStatus(), deps.fetchAudioOutputOverview(), deps.fetchAudioSourceOverview()]);
    deps.requestSubwooferPreviewRedrawFromState();
    await deps.fetchPlaylists();
}

async function fetchPlaybackStatus() {
    try {
        const resp = await deps.fetchFn('/api/status');
        if (!resp.ok) throw new Error('Failed to fetch playback status');
        const data = await resp.json();
        mergePlaybackState(data);
        deps.updateLiveBanner(data);
        syncFooterOwnershipFromPlayback(data);
        syncLibraryStateFromPlaybackContext(true);
        deps.updatePlaybackUI();
    } catch (e) {
        console.debug('Playback status unavailable on load', e);
    }
}

async function fetchSamplerateStatus() {
    if (isPageHidden()) return;
    try {
        const resp = await deps.fetchFn('/api/audio/samplerate');
        if (!resp.ok) throw new Error('Failed to fetch samplerate status');
        const data = await resp.json();
        deps.getState().samplerate = { ...state.samplerate, ...data };
        deps.renderSamplerateUI();
        deps.renderSettingsPanel();
    } catch (e) {
        console.debug('Samplerate status unavailable', e);
        deps.getState().samplerate = { ...state.samplerate, available: false, active_rate: null };
        deps.renderSamplerateUI();
        deps.renderSettingsPanel();
    }
}

async function previousInQueue() {
    if (playbackActionInFlight || !deps.getElements().btnPrevious || deps.getElements().btnPrevious.disabled) return;
    playbackActionInFlight = true;
    armFooterContentFreeze();
    deps.updatePlaybackUI();
    try {
        const resp = await deps.fetchFn('/api/playback/previous', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Previous failed'));
        if (data.playback) mergePlaybackState(data.playback);
        deps.updatePlaybackUI();
        triggerSamplerateBurstPolling();
    } catch (e) {
        deps.showToast(e.message || 'Failed to jump to previous track', 'error');
    } finally {
        playbackActionInFlight = false;
        deps.updatePlaybackUI();
    }
}

async function nextInQueue() {
    if (playbackActionInFlight || !deps.getElements().btnNext || deps.getElements().btnNext.disabled) return;
    playbackActionInFlight = true;
    armFooterContentFreeze();
    deps.updatePlaybackUI();
    try {
        const resp = await deps.fetchFn('/api/playback/next', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Next failed'));
        if (data.playback) mergePlaybackState(data.playback);
        deps.updatePlaybackUI();
        triggerSamplerateBurstPolling();
    } catch (e) {
        deps.showToast(e.message || 'Failed to jump to next track', 'error');
    } finally {
        playbackActionInFlight = false;
        deps.updatePlaybackUI();
    }
}

async function clearQueue() {
    if (playbackActionInFlight || !deps.getElements().btnClearQueue || deps.getElements().btnClearQueue.disabled) return;
    playbackActionInFlight = true;
    libraryModeSyncArmed = true;
    deps.updatePlaybackUI();
    try {
        const resp = await deps.fetchFn('/api/playback/clear-queue', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Clear queue failed');
        if (data.playback) mergePlaybackState(data.playback);
        deps.getState().library.shuffle = false;
        deps.getState().library.loop = false;
        lastLibraryPlaybackContextSignature = JSON.stringify({ shuffle: false, loop: false });
        deps.renderLibraryModeButtons();
        deps.showToast('Queue cleared', 'info');
    } catch (e) {
        deps.showToast(e.message || 'Failed to clear queue', 'error');
    } finally {
        playbackActionInFlight = false;
        deps.updatePlaybackUI();
    }
}

// Playback actions
async function playRadio(stationId) {
    pendingFooterSingleTrackStart = null;
    const station = deps.getState().stations.find(s => s.id === stationId);
    if (!station) {
        deps.showToast('Station not found', 'error');
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
    deps.getState().playback.current_track = optimisticRadioTrack;
    pendingOptimisticTrack = { requestId, track: optimisticRadioTrack };
    deps.getState().playback.live_title = null;
    deps.getState().playback.radio_metadata = null;
    deps.getState().playback.playing = true;
    deps.getState().playback.paused = false;
    _spotifyTakeoverUntil = 0;
    if (window.__spotifyLastData && window.__spotifyLastData.status === 'Playing') {
        window.__spotifyLastData = { ...window.__spotifyLastData, status: 'Paused' };
    }
    window.__footerSource = 'local';
    deps.bumpSpotifyPollGeneration();
    deps.updatePlaybackUI();
    try {
        const resp = await deps.fetchFn('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source: 'radio', track_id: station.id, url: station.stream_url }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Play command failed'));
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
        deps.maybeShowNativeTrackCue(playedTrack, 'Now playing');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        clearPendingOptimisticTrack(requestId);
        deps.getState().playback.playing = false;
        deps.getState().playback.paused = false;
        deps.updatePlaybackUI();
        deps.showToast('Failed to start playback', 'error');
    }
}

async function playLocal(trackId, queueTrackIds = null) {
    const track = deps.getState().library.tracks.find(t => t.id === trackId);
    if (!track) {
        deps.showToast('Track not found', 'error');
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
    deps.getState().playback.current_track = track;
    pendingOptimisticTrack = { requestId, track };
    deps.getState().playback.live_title = null;
    deps.getState().playback.playing = true;
    deps.getState().playback.paused = false;
    deps.getState().playback.queue = shouldUseQueue
        ? {
            active: true,
            index: Math.max(0, queueTrackIds.indexOf(track.id)),
            count: queueTrackIds.length,
            mode: deps.getState().playback.queue?.mode || 'app_replace',
            tracks: queueTrackIds
                .map(id => deps.getState().library.tracks.find(item => item.id === id))
                .filter(Boolean),
            loop: !!deps.getState().library.loop,
            shuffle: !!deps.getState().library.shuffle,
        }
        : buildOptimisticSingleTrackQueue(track);
    _spotifyTakeoverUntil = 0;
    if (window.__spotifyLastData && window.__spotifyLastData.status === 'Playing') {
        window.__spotifyLastData = { ...window.__spotifyLastData, status: 'Paused' };
    }
    window.__footerSource = 'local';
    deps.bumpSpotifyPollGeneration();
    deps.updatePlaybackUI();
    try {
        const resp = await deps.fetchFn('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source: 'local',
                track_id: track.id,
                queue_track_ids: shouldUseQueue ? queueTrackIds : undefined,
                shuffle: !!deps.getState().library.shuffle,
                loop: !!deps.getState().library.loop,
            }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Play command failed'));
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
        deps.maybeShowNativeTrackCue(playedTrack, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing');
    } catch (e) {
        if (requestId !== pendingPlaybackRequestId) return;
        playbackActionInFlight = false;
        clearPendingOptimisticTrack(requestId);
        clearPendingFooterSingleTrackStart(requestId);
        libraryModeSyncArmed = false;
        deps.getState().playback.playing = false;
        deps.getState().playback.paused = false;
        deps.updatePlaybackUI();
        deps.showToast('Failed to start playback', 'error');
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
    syncFooterOwnershipFromPlayback(deps.getState().playback);
    if (!shouldPollSpotify()) {
        deps.bumpSpotifyPollGeneration();
        deps.stopSpotifyPoll();
    }
    deps.updatePlaybackUI();
}

function footerContentFreezeActive() {
    return Date.now() < _footerContentFreezeUntil;
}

function armFooterContentFreeze(ms = 900) {
    _footerContentFreezeUntil = Date.now() + ms;
    if (_footerContentFreezeTimer) clearTimeout(_footerContentFreezeTimer);
    _footerContentFreezeTimer = setTimeout(() => {
        _footerContentFreezeTimer = null;
        deps.updatePlaybackUI();
    }, ms + 20);
}

function footerDebug(event, details = {}) {
    if (!window.__fxDebugFooter) return;
    try {
        console.log('[footer-debug]', event, {
            footerSource: window.__footerSource,
            local: {
                source: deps.getState().playback?.current_track?.source || null,
                title: deps.getState().playback?.current_track?.title || null,
                liveTitle: deps.getState().playback?.live_title || null,
                playing: !!deps.getState().playback?.playing,
                paused: !!deps.getState().playback?.paused,
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

function localPlaybackHasFooterContext(playback = deps.getState().playback) {
    const track = playback?.current_track;
    // Native MPV sources share one footer context: TIDAL rides the same
    // engine as local/radio, so live TIDAL playback owns the footer (and the
    // VU/peak gating derived from it) even when no backend commit is cached.
    if (!(track && (track.source === 'radio' || track.source === 'local' || track.source === 'tidal'))) return false;
    if (spotifyPlayingOwnsFooter()) return false;
    if (playback?.paused && window.__footerSource === 'spotify' && spotifyPausedHasFooterContext()) return false;
    return !!(playback?.playing || playback?.paused);
}

function localEndedPlaybackHasFooterContext(playback = deps.getState().playback) {
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

function localFooterHoldHasContext(playback = deps.getState().playback) {
    const track = playback?.current_track;
    if (!(track && (track.source === 'radio' || track.source === 'local' || track.source === 'tidal'))) return false;
    return Date.now() < _localFooterHoldUntil;
}

function reconcileFooterSource() {
    // Line-source modes own the footer exclusively with the source
    // switcher. Entering them pauses app playback backend-side, so any
    // retained streaming context (notably a Spotify Paused state) is stale
    // and must not pull the footer back to the app layout.
    if (deps.nonAppSourceModeActive()) {
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
    if (localPlaybackHasFooterContext(deps.getState().playback)) {
        setFooterSource('local', 'local-playback-has-context');
        return;
    }
    if (localFooterHoldHasContext(deps.getState().playback)) {
        setFooterSource('local', 'local-footer-hold', { holdUntil: _localFooterHoldUntil });
        return;
    }
    if (localEndedPlaybackHasFooterContext(deps.getState().playback)) {
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

function syncFooterOwnershipFromPlayback(playback = deps.getState().playback) {
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
            deps.bumpSpotifyPollGeneration();
            deps.stopSpotifyPoll();
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
            deps.bumpSpotifyPollGeneration();
            deps.stopSpotifyPoll();
        }
        return;
    }
    if (localEndedPlaybackHasFooterContext(playback)) {
        _spotifyTakeoverUntil = 0;
        setFooterSource('local', 'sync-playback-local-ended-context');
        if (!shouldPollSpotify()) {
            deps.bumpSpotifyPollGeneration();
            deps.stopSpotifyPoll();
        }
        return;
    }
    reconcileFooterSource();
    if (!shouldPollSpotify()) {
        deps.bumpSpotifyPollGeneration();
        deps.stopSpotifyPoll();
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
            const resp = await deps.fetchFn('/api/status');
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
            deps.updateSeekUI();
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

function _isSpotifyActive() {
    return window.__footerSource === 'spotify';
}

async function playCoverQueueIndex(index) {
    const payload = deps.coverQueuePlayTarget(deps.getState().playback, index);
    if (!payload) return;
    try {
        const resp = await deps.fetchFn('/api/play', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Play command failed'));
        if (data.playback) {
            mergePlaybackState(data.playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        deps.updatePlaybackUI();
    } catch (e) {
        console.warn('Cover queue play failed', e);
        deps.showToast('Failed to play track', 'error');
    }
}

async function doSeek(seconds) {
    try {
        const resp = await deps.fetchFn('/api/playback/seek', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ position: seconds }),
        });
        if (!resp.ok) console.debug('Seek result:', await resp.json().catch(() => '??'));
    } catch (e) { /* silent for seek */ }
}

async function resyncPlaybackAfterReconnect() {
    const generation = deps.claimWsSyncGeneration();
    try {
        const [playback, spotify, qobuz] = await Promise.all([
            deps.fetchFn('/api/status')
                .then(resp => resp.ok ? resp.json() : null)
                .catch(() => null),
            deps.fetchSpotifyStatus(),
            deps.fetchQobuzStatus(),
        ]);
        if (!deps.isWsSyncGenerationCurrent(generation)) return;

        if (playback) {
            mergePlaybackState(playback);
            // Reconnect: adopt the running track silently, never cue it.
            deps.seedNativeTrackCueKey(playback.current_track);
            deps.updateLiveBanner(playback);
            syncFooterOwnershipFromPlayback(playback);
            syncLibraryStateFromPlaybackContext(true);
        }
        if (spotify) {
            deps.handleIncomingSpotifyState(spotify, { renderTab: true, renderFooter: true });
        }
        if (qobuz) {
            deps.handleIncomingQobuzState(qobuz, { renderFooter: true });
        }
        reconcileFooterSource();
        deps.updatePlaybackUI();
        if (shouldPollSpotify()) {
            deps.startSpotifyPoll();
        } else {
            deps.stopSpotifyPoll();
        }
        if (deps.shouldPollQobuz()) {
            deps.startQobuzPoll();
        } else {
            deps.stopQobuzPoll();
        }
    } catch (e) {
        console.debug('Reconnect state sync failed', e);
    }
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

    return {
        init,
        isStreamingFooterSource,
        streamingFooterData,
        getBackendFooterOwner,
        getEffectivePlaybackControlSource,
        globalTogglePlayback,
        globalPrevious,
        globalNext,
        globalSeekChange,
        globalSeekEnd,
        stopPlayback,
        togglePlayback,
        clampVolumeValue,
        sliderVolumeToActualVolume,
        actualVolumeToSliderValue,
        renderVolumeControlsFromActualVolume,
        setLocalVolume,
        queueVolumeSend,
        mergePlaybackState,
        rememberLastRadioTrack,
        getLastRadioTrack,
        buildOptimisticSingleTrackQueue,
        footerSingleTrackStartLockActive,
        activeLocalPlaybackBlocksSpotifyOwnership,
        footerSingleTrackStartLockSatisfied,
        clearPendingFooterSingleTrackStart,
        clearPendingOptimisticTrack,
        getLibraryPlaybackContext,
        syncLibraryStateFromPlaybackContext,
        applyRemoteVolume,
        sendVolume,
        handleVolumeChange,
        isPageHidden,
        startMetadataPolling,
        stopMetadataPolling,
        startPeakStatusPolling,
        stopPeakStatusPolling,
        startSampleratePolling,
        stopSampleratePolling,
        triggerSamplerateBurstPolling,
        fetchMetadata,
        fetchInitialData,
        fetchPlaybackStatus,
        fetchSamplerateStatus,
        previousInQueue,
        nextInQueue,
        clearQueue,
        playRadio,
        playLocal,
        applyNativePlayResponse,
        footerContentFreezeActive,
        armFooterContentFreeze,
        footerDebug,
        setFooterSource,
        localPlaybackHasFooterContext,
        localEndedPlaybackHasFooterContext,
        spotifyPlayingOwnsFooter,
        spotifyPausedHasFooterContext,
        qobuzPlayingOwnsFooter,
        localFooterHoldHasContext,
        reconcileFooterSource,
        spotifyIsInstalled,
        shouldPollSpotify,
        syncFooterOwnershipFromPlayback,
        startPlaybackPositionPoll,
        stopPlaybackPositionPoll,
        _isSpotifyActive,
        playCoverQueueIndex,
        doSeek,
        resyncPlaybackAfterReconnect,
        armSpotifyTakeover,
        armLocalFooterHold,
        isPlaybackActionInFlight,
        setPlaybackActionInFlight,
        getPendingOptimisticTrack,
        setLibraryModeSyncArmed,
        isVolumeGestureActive,
        setVolumeGestureActive,
        isVolumeRequestInFlight,
        getPendingVolume
    };
});
