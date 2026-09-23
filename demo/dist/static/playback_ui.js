// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute playback UI: footer layout/render, meter, queue UI, seek UI,
 * cover detail, artwork, track cues, footer favorites and the
 * streaming-owner footer renderer.
 *
 * This module never decides footer ownership: every ownership question
 * resolves through its sibling static/playback_core.js
 * (window.FXRoutePlaybackCore) behind explicit callbacks. Transport entry
 * points, volume send-state and the shared action/lock flags are likewise
 * core-owned; the Spotify/Qobuz provider runtime stays in static/app.js.
 *
 * State/DOM/toast go through injected getters. Browser-loadable UMD, no
 * build step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRoutePlaybackUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ playback: {}, library: {}, samplerate: {}, settings: {} }),
        getElements: () => ({}),
        fetchFn: (...args) => fetch(...args),
        showToast: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
        formatTime: () => '0:00',
        formatRateKhz: (rate) => String(rate ?? ''),
        setRangeProgress: () => {},
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        nonAppSourceModeActive: () => false,
        isFooterSignalActive: () => false,
        renderSourceModeFooter: () => {},
        buildSourceSwitcherEntries: () => [],
        activateSourceSwitcherEntry: () => {},
        stepSourceSwitcher: () => {},
        spotifyCommand: async () => {},
        qobuzCommand: async () => {},
        spotifySeek: async () => {},
        qobuzSeek: async () => {},
        shouldPollQobuz: () => false,
        startQobuzPoll: () => {},
        stopQobuzPoll: () => {},
        startSpotifyPoll: () => {},
        stopSpotifyPoll: () => {},
        bumpSpotifyPollGeneration: () => {},
        bumpQobuzPollGeneration: () => {},
        isLibraryModeRequestInFlight: () => false,
        isTidalFavoriteRequestInFlight: () => false,
        setTidalFavoriteRequestInFlight: () => {},
        isSpotifyTransportInFlight: () => false,
        renderLibraryModeButtons: () => {},
        toggleLibraryShuffle: async () => {},
        toggleLibraryLoop: async () => {},
        renderTrackFavoriteButton: () => {},
        toggleTrackFavoriteById: async () => {},
        isStreamingFooterSource: (source) => source === 'spotify' || source === 'qobuz',
        streamingFooterData: () => null,
        getBackendFooterOwner: () => null,
        getEffectivePlaybackControlSource: () => 'local',
        reconcileFooterSource: () => {},
        footerDebug: () => {},
        footerContentFreezeActive: () => false,
        footerSingleTrackStartLockActive: () => false,
        shouldPollSpotify: () => false,
        applyRemoteVolume: () => {},
        renderVolumeControlsFromActualVolume: () => {},
        actualVolumeToSliderValue: (value) => value,
        sliderVolumeToActualVolume: (value) => value,
        queueVolumeSend: () => {},
        doSeek: async () => {},
        playCoverQueueIndex: async () => {},
        getLastRadioTrack: () => null,
        startPlaybackPositionPoll: () => {},
        stopPlaybackPositionPoll: () => {},
        globalTogglePlayback: () => {},
        globalPrevious: () => {},
        globalNext: () => {},
        clearQueue: () => {},
        handleVolumeChange: () => {},
        isPlaybackActionInFlight: () => false,
        getPendingOptimisticTrack: () => null,
        setLibraryModeSyncArmed: () => {},
        isVolumeGestureActive: () => false,
        setVolumeGestureActive: () => {},
        isVolumeRequestInFlight: () => false,
        getPendingVolume: () => null,
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

let volumeDisplayTimer = null;
let nowPlayingCueTimer = null;
let nowPlayingCueCoverAbort = null;
let playbackFooterResizeObserver = null;
let playbackFooterSpaceFrame = null;
let seekDragging = false;
let seekPendingPos = null;
// Slow-VU holdover cache: a single dropped WS frame must not blank the meter.
let lastValidVuSnapshot = null;
const VU_HOLDOVER_MS = 2000;
// Signature of the last rendered cover-detail queue list. Rebuilding the
// list on every status poll would drop :hover/focus, so only rebuild on change.
let coverDetailQueueSignature = null;

function syncPlaybackFooterSpace() {
    playbackFooterSpaceFrame = null;
    const bar = deps.getElements().playbackBar;
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
    if (!deps.getElements().playbackBar) return;
    if (typeof ResizeObserver === 'function') {
        playbackFooterResizeObserver = new ResizeObserver(schedulePlaybackFooterSpaceSync);
        playbackFooterResizeObserver.observe(deps.getElements().playbackBar);
    }
    window.addEventListener('resize', schedulePlaybackFooterSpaceSync);
    schedulePlaybackFooterSpaceSync();
}

function setFooterProgressState(available, readonly = false) {
    const showProgress = !!available;
    deps.getElements().playbackBar?.classList.toggle('progress-readonly', showProgress && !!readonly);
    deps.getElements().seekRow?.classList.toggle('hidden', !showProgress);
}

function showVolumeDisplayTemporarily() {
    const controls = deps.getElements().volumeSlider?.closest('.controls');
    if (!controls) return;
    controls.classList.add('is-adjusting-volume');
    clearTimeout(volumeDisplayTimer);
    volumeDisplayTimer = setTimeout(() => {
        controls.classList.remove('is-adjusting-volume');
        volumeDisplayTimer = null;
    }, 900);
}

function setupPlaybackControls() {
    if (!deps.getElements().btnPlayPause || !deps.getElements().volumeSlider) {
        console.error('Playback controls are missing in the DOM');
        return;
    }
    if (deps.getElements().footerShuffleBtn) deps.getElements().footerShuffleBtn.addEventListener('click', toggleFooterShuffle);
    if (deps.getElements().btnPrevious) deps.getElements().btnPrevious.addEventListener('click', deps.globalPrevious);
    deps.getElements().btnPlayPause.addEventListener('click', deps.globalTogglePlayback);
    if (deps.getElements().btnNext) deps.getElements().btnNext.addEventListener('click', deps.globalNext);
    if (deps.getElements().sourcePrev) deps.getElements().sourcePrev.addEventListener('click', () => deps.stepSourceSwitcher(-1));
    if (deps.getElements().sourceNext) deps.getElements().sourceNext.addEventListener('click', () => deps.stepSourceSwitcher(1));
    if (deps.getElements().sourceSelect) deps.getElements().sourceSelect.addEventListener('change', (event) => {
        const key = event.target.value || '';
        const sourceMode = deps.getState().settings?.sourceMode || {};
        const entry = deps.buildSourceSwitcherEntries(sourceMode).find((item) => item.key === key);
        if (entry) {
            deps.activateSourceSwitcherEntry(entry);
        } else {
            deps.renderSourceModeFooter();
        }
    });
    if (deps.getElements().footerLoopBtn) deps.getElements().footerLoopBtn.addEventListener('click', toggleFooterLoop);
    if (deps.getElements().btnClearQueue) deps.getElements().btnClearQueue.addEventListener('click', deps.clearQueue);
    if (deps.getElements().trackFavoriteBtn) deps.getElements().trackFavoriteBtn.addEventListener('click', toggleCurrentTrackFavorite);
    window.addEventListener('fxroute:tidal-favorites', renderFooterFavoriteFromTidalChange);
    deps.getElements().volumeSlider.addEventListener('input', deps.handleVolumeChange);
    if (deps.getElements().playbackCover) {
        deps.getElements().playbackCover.addEventListener('click', toggleCoverDetailCard);
        deps.getElements().playbackCover.addEventListener('keydown', event => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            toggleCoverDetailCard();
        });
    }
    if (deps.getElements().coverDetailBackdrop) deps.getElements().coverDetailBackdrop.addEventListener('click', closeCoverDetailCard);
    if (deps.getElements().coverDetailQueueList) {
        deps.getElements().coverDetailQueueList.addEventListener('click', (event) => {
            const row = event.target.closest('[data-queue-index]');
            if (!row) return;
            event.preventDefault();
            event.stopPropagation();
            deps.playCoverQueueIndex(Number(row.dataset.queueIndex));
        });
        deps.getElements().coverDetailQueueList.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            const row = event.target.closest('[data-queue-index]');
            if (!row) return;
            event.preventDefault();
            event.stopPropagation();
            deps.playCoverQueueIndex(Number(row.dataset.queueIndex));
        });
    }
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && isCoverDetailOpen()) closeCoverDetailCard();
    });
    deps.getElements().volumeSlider.addEventListener('change', (e) => {
        const sliderValue = parseInt(e.target.value, 10);
        const actualVolume = deps.sliderVolumeToActualVolume(sliderValue);
        deps.setVolumeGestureActive(false);
        deps.queueVolumeSend(actualVolume, true);
    });
    updatePlaybackUI();
    deps.renderLibraryModeButtons();
}

function renderFooterModeButtons() {
    const shuffleBtn = deps.getElements().footerShuffleBtn;
    const loopBtn = deps.getElements().footerLoopBtn;
    if (!shuffleBtn && !loopBtn) return;

    if (deps.isStreamingFooterSource(window.__footerSource)) {
        const data = deps.streamingFooterData() || {};
        const caps = data.capabilities || {};
        const hasMedia = !!(data.available && (data.title || data.artist || data.album || data.status !== 'Stopped'));
        const showShuffle = hasMedia && !!caps.shuffle;
        const showLoop = hasMedia && !!caps.loop;
        const transportInFlight = window.__footerSource === 'spotify' && deps.isSpotifyTransportInFlight();
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

    const track = deps.getState().playback.current_track;
    const nativeQueueActive = !!(track && (track.source === 'local' || track.source === 'tidal'));
    const queue = deps.getState().playback.queue || {};
    const hasActiveQueue = Number(queue.count || 0) > 1;
    const showShuffle = nativeQueueActive && hasActiveQueue;
    const showLoop = nativeQueueActive && hasActiveQueue;
    if (shuffleBtn) {
        shuffleBtn.classList.toggle('hidden', !showShuffle);
        shuffleBtn.classList.toggle('active', showShuffle && !!deps.getState().library.shuffle);
        shuffleBtn.disabled = !showShuffle || deps.isLibraryModeRequestInFlight();
        shuffleBtn.setAttribute('aria-pressed', showShuffle && deps.getState().library.shuffle ? 'true' : 'false');
        shuffleBtn.title = deps.getState().library.shuffle ? 'Shuffle on' : 'Shuffle off';
    }
    if (loopBtn) {
        loopBtn.classList.toggle('hidden', !showLoop);
        loopBtn.classList.toggle('active', showLoop && !!deps.getState().library.loop);
        loopBtn.disabled = !showLoop || deps.isLibraryModeRequestInFlight();
        loopBtn.setAttribute('aria-pressed', showLoop && deps.getState().library.loop ? 'true' : 'false');
        loopBtn.textContent = '↻';
        loopBtn.title = deps.getState().library.loop ? 'Repeat on' : 'Repeat off';
    }
}

function toggleFooterShuffle() {
    if (window.__footerSource === 'spotify') {
        void deps.spotifyCommand('shuffle');
        return;
    }
    if (window.__footerSource === 'qobuz') {
        void deps.qobuzCommand('shuffle');
        return;
    }
    void deps.toggleLibraryShuffle();
}

function toggleFooterLoop() {
    if (window.__footerSource === 'spotify') {
        void deps.spotifyCommand('loop');
        return;
    }
    if (window.__footerSource === 'qobuz') {
        void deps.qobuzCommand('repeat');
        return;
    }
    void deps.toggleLibraryLoop();
}

function updatePlaybackUI() {
    const { current_track, volume, playing, paused, live_title } = deps.getState().playback;
    const freezeActive = deps.footerContentFreezeActive();
    deps.reconcileFooterSource();
    if (deps.shouldPollSpotify()) {
        deps.startSpotifyPoll();
    } else {
        deps.bumpSpotifyPollGeneration();
        deps.stopSpotifyPoll();
    }
    if (deps.shouldPollQobuz()) {
        deps.startQobuzPoll();
    } else {
        deps.bumpQobuzPollGeneration();
        deps.stopQobuzPoll();
    }
    // When an external renderer (Spotify/Qobuz) owns the footer, local UI must
    // NOT touch footer elements at all. Refresh from the owner's normalized
    // state and return — the streaming state owns the footer exclusively.
    if (deps.isStreamingFooterSource(window.__footerSource)) {
        deps.stopPlaybackPositionPoll();
        const streamingData = deps.streamingFooterData();
        if (!freezeActive && streamingData) updateFooterForStreamingOwner(streamingData);
        highlightActiveTrack();
        return;
    }
    // The footer is laid out exclusively from the data-backed visibility classes.
    const isRadio = current_track && current_track.source === 'radio';
    if (!freezeActive) {
        const radioMetadata = isRadio ? deps.getState().playback.radio_metadata : null;
        deps.getElements().playbackBar?.classList.toggle('has-media', !!current_track);
        // Track info
        if (current_track) {
            deps.getElements().trackTitle.textContent = isRadio && live_title ? live_title : current_track.title;
            deps.getElements().trackTitle.classList.remove('placeholder');
            deps.getElements().trackTitle.style.display = 'none';
            deps.getElements().trackTitle.classList.add('placeholder');
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
            deps.getElements().trackArtist.textContent = isRadio && live_title ? current_track.title : (current_track.artist || '');
            deps.getElements().trackArtist.style.display = 'none';
        } else {
            deps.getElements().trackTitle.textContent = 'Not playing';
            deps.getElements().trackTitle.classList.add('placeholder');
            deps.getElements().trackArtist.textContent = '';
            const scArtist = document.getElementById('sc-artist');
            const scTitle = document.getElementById('sc-title');
            const scAlbum = document.getElementById('sc-album');
            if (scArtist) scArtist.textContent = '';
            if (scTitle) scTitle.textContent = '';
            if (scAlbum) {
                scAlbum.textContent = '';
                scAlbum.style.display = 'none';
            }
            if (deps.getElements().trackTitle) deps.getElements().trackTitle.style.display = '';
            if (deps.getElements().trackArtist) deps.getElements().trackArtist.style.display = '';
        }
    }
    deps.renderTrackFavoriteButton(current_track);
    const activeRadioMetadata = isRadio ? deps.getState().playback.radio_metadata : null;
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
    if (deps.getElements().playbackBar) {
        deps.getElements().playbackBar.classList.toggle('is-playing', !!playing);
        deps.getElements().playbackBar.classList.toggle('is-paused', !!paused && !playing);
    }
    // Play/pause + seek
    updatePlayPauseButton(playing ? 'playing' : (paused ? 'paused' : 'stopped'));
    updateSeekUI();
    renderQueueUI();
    renderSamplerateUI();
    renderPeakWarningBadge();
    // Volume
    if (!deps.isVolumeGestureActive() && !deps.isVolumeRequestInFlight() && deps.getPendingVolume() === null) {
        deps.renderVolumeControlsFromActualVolume(volume);
    } else {
        deps.getElements().volumeDisplay.textContent = `${deps.actualVolumeToSliderValue(volume)}%`;
    }
    // Bluetooth / external-input modes reuse this footer: transport is
    // replaced by the source switcher while volume and meter keep updating.
    // deps.reconcileFooterSource() above pinned ownership to 'local' in these
    // modes, so no streaming gate is needed here — consulting it would delay
    // the switcher by one poll on first paint after entering a source mode.
    if (deps.nonAppSourceModeActive()) {
        deps.renderSourceModeFooter();
    }
    // Highlight active
    highlightActiveTrack();
    // Keep the cover detail card in sync while it is open
    if (isCoverDetailOpen()) renderCoverDetailCard();
    // Start/stop position polling for local playback
    if (playing && !deps.isStreamingFooterSource(window.__footerSource)) {
        deps.startPlaybackPositionPoll();
    } else {
        deps.stopPlaybackPositionPoll();
    }
}

function updatePlayPauseButton(playbackState) {
    deps.getElements().btnPlayPause.textContent = playbackState === 'playing' ? '⏸' : '▶';
    const hasPlayableContext = !!(deps.getState().playback.current_track || deps.getLastRadioTrack());
    deps.getElements().btnPlayPause.disabled = deps.isPlaybackActionInFlight() || (!hasPlayableContext && playbackState === 'stopped');
}

function highlightActiveTrack() {
    if (window.__footerSource === 'spotify') {
        document.querySelectorAll('.station-card.active, .track-item.active, .streaming-result.active').forEach(item => item.classList.remove('active'));
        return;
    }
    // A play request holds its optimistic target until the server confirms the
    // commit, so a stale pre-commit WebSocket push cannot bounce the highlight
    // back to the previous station while the transition is still running.
    const displayTrack = deps.getPendingOptimisticTrack()?.track || deps.getState().playback.current_track;
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

function renderQueueUI() {
    const queue = deps.getState().playback.queue || {};
    const footerSingleTrackOverride = deps.footerSingleTrackStartLockActive();
    const hasQueue = footerSingleTrackOverride ? false : queue.count > 1;
    const queueIndex = footerSingleTrackOverride ? -1 : (typeof queue.index === 'number' ? queue.index : -1);
    const currentTrack = deps.getState().playback.current_track;
    const hasNativeQueueTrack = !!(currentTrack && (currentTrack.source === 'local' || currentTrack.source === 'tidal'));
    deps.getState().library.shuffle = hasNativeQueueTrack ? !!queue.shuffle : false;
    deps.getState().library.loop = hasNativeQueueTrack ? !!queue.loop : false;
    deps.setLibraryModeSyncArmed(false);
    deps.renderLibraryModeButtons();

    if (deps.getElements().queueStatus) {
        if (hasQueue && queueIndex >= 0) {
            deps.getElements().queueStatus.textContent = `${queueIndex + 1} / ${queue.count}`;
            deps.getElements().queueStatus.classList.remove('hidden');
        } else {
            deps.getElements().queueStatus.classList.add('hidden');
        }
    }

    if (deps.getElements().btnPrevious && !deps.isStreamingFooterSource(window.__footerSource)) {
        deps.getElements().btnPrevious.classList.toggle('hidden', !hasQueue);
        deps.getElements().btnPrevious.disabled = deps.isPlaybackActionInFlight() || !hasQueue || queueIndex <= 0;
    }
    if (deps.getElements().btnNext && !deps.isStreamingFooterSource(window.__footerSource)) {
        deps.getElements().btnNext.classList.toggle('hidden', !hasQueue);
        deps.getElements().btnNext.disabled = deps.isPlaybackActionInFlight() || !hasQueue || queueIndex < 0 || (queueIndex >= queue.count - 1 && !queue.loop && !queue.shuffle);
    }
    if (deps.getElements().btnClearQueue) {
        deps.getElements().btnClearQueue.classList.toggle('hidden', !hasQueue);
        deps.getElements().btnClearQueue.disabled = deps.isPlaybackActionInFlight() || !hasQueue;
    }
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
    if (!deps.getElements().playbackMeter) return;
    const playbackActive = !!active;
    rememberValidVu(warning, playbackActive);
    const liveFresh = !!warning?.available && warning?.vu_fresh === true && playbackActive;
    const held = !liveFresh && playbackActive ? heldVuSnapshot() : null;
    deps.getElements().playbackMeter.classList.toggle('is-active', liveFresh || !!held);
    deps.getElements().playbackMeter.classList.toggle('is-peak', !!(warning?.detected_l || warning?.detected_r || warning?.detected));
    renderMeterChannel(deps.getElements().meterLeft, liveFresh ? warning?.vu_db_l : (held ? held.vu_db_l : null), liveFresh && !!warning?.detected_l);
    renderMeterChannel(deps.getElements().meterRight, liveFresh ? warning?.vu_db_r : (held ? held.vu_db_r : null), liveFresh && !!warning?.detected_r);
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
    const warning = deps.getState().playback.output_peak_warning || {};
    const title = warning.target?.description || warning.target?.source_name || 'DSP output monitor';
    const vuDb = isFiniteVuDb(warning.vu_db) ? Number(warning.vu_db) : null;
    const playbackActive = activeOverride === null ? deps.isFooterSignalActive() : !!activeOverride;
    const showPeak = !!warning.detected && playbackActive;
    const liveShowVu = !!warning.available && warning.vu_fresh === true
        && playbackActive && vuDb !== null;
    rememberValidVu(warning, playbackActive);
    // Single invalid sample while playing: hold the last valid dB text
    // instead of hiding the badge. Peak keeps its live-only fallback.
    const held = !liveShowVu && playbackActive && !showPeak ? heldVuSnapshot() : null;
    const showVu = liveShowVu || !!held;
    const effVuDb = liveShowVu ? vuDb : (held ? held.vu_db : null);

    if (deps.getElements().outputLevelBadge) {
        deps.getElements().outputLevelBadge.classList.toggle('hidden', !(showPeak || showVu));
        deps.getElements().outputLevelBadge.style.visibility = '';
        deps.getElements().outputLevelBadge.classList.toggle('is-peak', showPeak);
        /* Peak only recolors the badge: keep the exact VU text/format so the
           display never changes width when the peak state toggles. */
        const vuText = showVu ? formatOutputLevelBadgeDb(effVuDb) : '';
        deps.getElements().outputLevelBadge.textContent = showPeak
            ? (vuText || formatOutputLevelBadgeDb(0))
            : vuText;
        deps.getElements().outputLevelBadge.title = showPeak
            ? `Post-DSP output peak detected on ${title}`
            : (showVu ? `Post-DSP output level (slow VU) on ${title}` : '');
    }

    renderStereoMeter(warning, playbackActive);
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
    return !!(deps.getElements().coverDetailCard && !deps.getElements().coverDetailCard.classList.contains('hidden'));
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

function renderCoverDetailCard() {
    const playback = deps.getState().playback || {};
    const track = playback.current_track || null;
    // An external renderer (Spotify/Qobuz) owns the footer (and thus the
    // detail card) from its streaming state; /api/status carries no external
    // track. Mirror the footer source so the card shows the same data.
    const streamingSource = deps.getEffectivePlaybackControlSource();
    const streamingData = streamingSource === 'spotify'
        ? (window.__spotifyLastData || null)
        : streamingSource === 'qobuz' ? (window.__qobuzLastData || null) : null;
    // Meta block: source/playlist, current title, artist, album, tech line.
    const meta = streamingData ? coverDetailStreamingMeta(streamingData, streamingSource) : coverDetailMeta(playback);
    setCoverDetailText(deps.getElements().coverDetailSource, meta.source);
    setCoverDetailText(deps.getElements().coverDetailTitle, meta.title);
    setCoverDetailText(deps.getElements().coverDetailArtist, meta.artist);
    setCoverDetailText(deps.getElements().coverDetailAlbum, meta.album);
    setCoverDetailText(deps.getElements().coverDetailTech, meta.tech);
    // Tag-info block under the cover: year/genre + disc/track position only
    // when present; hide the whole block when both lines are empty.
    const extra = coverDetailExtra(playback);
    setCoverDetailText(deps.getElements().coverDetailExtraLine1, extra.line1);
    setCoverDetailText(deps.getElements().coverDetailExtraLine2, extra.line2);
    if (deps.getElements().coverDetailExtra) {
        deps.getElements().coverDetailExtra.classList.toggle('hidden', !extra.line1 && !extra.line2);
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
        deps.getElements().coverDetailCover.src = coverUrl;
        deps.getElements().coverDetailCover.classList.remove('hidden');
    } else {
        deps.getElements().coverDetailCover.removeAttribute('src');
        deps.getElements().coverDetailCover.classList.add('hidden');
    }
    const sections = coverDetailSections(playback);
    // Radio: provider-delivered history only (no empty headings, no invented entries).
    if (sections.history.length > 0) {
        deps.getElements().coverDetailHistoryList.innerHTML = sections.history.map((entry, index) => `
            <li class="cover-detail-row">
                <span class="cover-detail-row-index">${index + 1}</span>
                <span class="cover-detail-row-body">
                    <span class="cover-detail-row-title">${deps.escapeHtml(entry.title || '')}</span>
                    ${entry.artist ? `<span class="cover-detail-row-artist">${deps.escapeHtml(entry.artist)}</span>` : ''}
                </span>
            </li>`).join('');
        deps.getElements().coverDetailHistory.classList.remove('hidden');
    } else {
        deps.getElements().coverDetailHistoryList.innerHTML = '';
        deps.getElements().coverDetailHistory.classList.add('hidden');
    }
    // Library: active queue with current track highlighted (if any). Rows are
    // keyboard-accessible buttons that jump via the canonical play path.
    if (sections.queue) {
        const signature = sections.queue.tracks.map(item => item.id).join('|') + '|' + sections.queue.index;
        if (signature !== coverDetailQueueSignature) {
            coverDetailQueueSignature = signature;
            deps.getElements().coverDetailQueueList.innerHTML = sections.queue.tracks.map((item, index) => {
                const playable = coverQueuePlayTarget(playback, index) !== null;
                const attrs = playable
                    ? ` role="button" tabindex="0" data-queue-index="${index}" aria-label="Play ${deps.escapeHtml(item.title || '')}"`
                    : '';
                return `<li class="cover-detail-row${index === sections.queue.index ? ' current' : ''}"${attrs}>
                    <span class="cover-detail-row-index">${index + 1}</span>
                    ${index === sections.queue.index ? '<span class="cover-detail-row-current-mark">▶</span>' : ''}
                    <span class="cover-detail-row-body">
                        <span class="cover-detail-row-title">${deps.escapeHtml(item.title || '')}</span>
                        ${item.artist ? `<span class="cover-detail-row-artist">${deps.escapeHtml(item.artist)}</span>` : ''}
                    </span>
                </li>`;
            }).join('');
        }
        deps.getElements().coverDetailQueue.classList.remove('hidden');
    } else {
        coverDetailQueueSignature = null;
        deps.getElements().coverDetailQueueList.innerHTML = '';
        deps.getElements().coverDetailQueue.classList.add('hidden');
    }
}

function openCoverDetailCard() {
    renderCoverDetailCard();
    deps.getElements().coverDetailBackdrop.classList.remove('hidden');
    deps.getElements().coverDetailCard.classList.remove('hidden');
    window.FXRouteModal?.open(deps.getElements().coverDetailCard, {
        dialog: deps.getElements().coverDetailCard,
        initialFocus: deps.getElements().coverDetailCard,
        siblingRoots: [deps.getElements().coverDetailBackdrop],
        onEscape: closeCoverDetailCard,
    });
}

function closeCoverDetailCard() {
    deps.getElements().coverDetailBackdrop.classList.add('hidden');
    deps.getElements().coverDetailCard.classList.add('hidden');
    window.FXRouteModal?.close(deps.getElements().coverDetailCard);
}

function toggleCoverDetailCard() {
    if (isCoverDetailOpen()) closeCoverDetailCard();
    else openCoverDetailCard();
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
    if (!deps.getElements().playbackCover) return;
    const coverUrl = playbackArtworkKnownAvailable(track) ? playbackArtworkUrl(track) : '';
    if (!coverUrl) {
        deps.getElements().playbackCover.removeAttribute('src');
        deps.getElements().playbackCover.classList.add('hidden');
        deps.getElements().playbackCover.classList.remove('is-ready');
        deps.getElements().playbackBar?.classList.remove('has-cover');
        return;
    }
    deps.getElements().playbackCover.onerror = function() {
        const fallbackUrl = track?.artwork_fallback_url || '';
        if (fallbackUrl && this.getAttribute('src') !== fallbackUrl) {
            this.src = fallbackUrl;
            return;
        }
        this.onerror = null;
        this.removeAttribute('src');
        this.classList.add('hidden');
        this.classList.remove('is-ready');
        deps.getElements().playbackBar?.classList.remove('has-cover');
    };
    deps.getElements().playbackCover.onload = function() {
        this.classList.remove('hidden');
        this.classList.add('is-ready');
        deps.getElements().playbackBar?.classList.add('has-cover');
    };
    if (deps.getElements().playbackCover.getAttribute('src') !== coverUrl) {
        deps.getElements().playbackCover.classList.remove('is-ready');
        deps.getElements().playbackCover.src = coverUrl;
    } else {
        deps.getElements().playbackCover.classList.remove('hidden');
        deps.getElements().playbackBar?.classList.add('has-cover');
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
            const infoResp = await deps.fetchFn(coverInfoUrl, { signal: controller.signal, cache: 'no-store' });
            if (!infoResp.ok) return;
            const info = await infoResp.json();
            if (!info.available) return;
        }
        if (isExternalCover) {
            // External covers load directly via <img>; deps.fetchFn() would hit CORS.
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
            const resp = await deps.fetchFn(coverUrl, { signal: controller.signal, cache: 'force-cache' });
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
    deps.getElements().toastContainer.querySelectorAll('.now-playing-cue').forEach(item => item.remove());
    const cue = document.createElement('div');
    cue.className = 'toast info now-playing-cue';
    const coverUrl = playbackArtworkUrl(track);
    cue.innerHTML = `
        <img class="now-playing-cover" alt="">
        <div class="now-playing-text">
            <div class="now-playing-label">${deps.escapeHtml(message)}</div>
            <div class="now-playing-title">${deps.escapeHtml(track.title || 'Unknown track')}</div>
            <div class="now-playing-meta">${deps.escapeHtml([track.artist, track.album].filter(Boolean).join(' · '))}</div>
        </div>
    `;
    deps.getElements().toastContainer.appendChild(cue);
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

// Seek
function initSeek() {
    if (!deps.getElements().seekSlider) return;
    deps.getElements().seekSlider.addEventListener('input', seekChange);
    deps.getElements().seekSlider.addEventListener('mousedown', seekStart);
    deps.getElements().seekSlider.addEventListener('touchstart', seekStart, { passive: true });
    deps.getElements().seekSlider.addEventListener('mouseup', seekEnd);
    deps.getElements().seekSlider.addEventListener('touchend', seekEnd);
}

function seekStart() {
    if (deps.getElements().playbackBar?.classList.contains('progress-readonly')) return;
    seekDragging = true;
    if (deps.isStreamingFooterSource(window.__footerSource)) window.__streamingSeeking = true;
}

function seekEnd() {
    if (deps.getElements().playbackBar?.classList.contains('progress-readonly')) return;
    seekDragging = false;
    if (deps.isStreamingFooterSource(window.__footerSource)) {
        window.__streamingSeeking = false;
        const streamingData = deps.streamingFooterData();
        if (streamingData && streamingData.duration) {
            const posSec = (parseInt(deps.getElements().seekSlider.value, 10) / 1000) * streamingData.duration;
            if (window.__footerSource === 'qobuz') deps.qobuzSeek(posSec);
            else deps.spotifySeek(posSec);
        }
        return;
    }
    if (seekPendingPos !== null && deps.getState().playback.duration > 0) {
        deps.doSeek(seekPendingPos);
        seekPendingPos = null;
    }
}

function seekChange() {
    if (deps.getElements().playbackBar?.classList.contains('progress-readonly')) return;
    const pos = parseInt(deps.getElements().seekSlider.value, 10) || 0;
    deps.setRangeProgress(deps.getElements().seekSlider, pos / 1000);
    if (deps.isStreamingFooterSource(window.__footerSource)) {
        const streamingData = deps.streamingFooterData();
        const duration = streamingData?.duration || 0;
        const current = (pos / 1000) * duration;
        if (deps.getElements().seekCurrent) deps.getElements().seekCurrent.textContent = deps.formatTime(current);
        return;
    }
    const duration = deps.getState().playback.duration || 0;
    const current = (pos / 1000) * duration;
    if (deps.getElements().seekCurrent) deps.getElements().seekCurrent.textContent = deps.formatTime(current);
    seekPendingPos = current;
}

function updateSeekUI() {
    if (!deps.getElements().seekSlider || !deps.getElements().seekCurrent || !deps.getElements().seekDuration) return;
    const currentTrack = deps.getState().playback.current_track;
    const isRadio = currentTrack?.source === 'radio';
    const radioMetadata = isRadio ? deps.getState().playback.radio_metadata : null;
    const radioDuration = Number(radioMetadata?.duration_seconds);
    const radioProgress = radioMetadata?.progress_seconds === null || radioMetadata?.progress_seconds === undefined
        ? Number.NaN
        : Number(radioMetadata.progress_seconds);
    const radioStartedAt = radioMetadata?.started_at === null || radioMetadata?.started_at === undefined
        ? Number.NaN
        : Number(radioMetadata.started_at);
    const radioTimed = !!(radioMetadata && !radioMetadata.stale && radioDuration > 0
        && (Number.isFinite(radioProgress) || Number.isFinite(radioStartedAt)));
    const duration = radioTimed ? radioDuration : (isRadio ? 0 : Number(deps.getState().playback.duration || 0));
    let position = radioTimed && Number.isFinite(radioProgress)
        ? radioProgress
        : (isRadio ? 0 : Number(deps.getState().playback.position || 0));
    if (radioTimed && Number.isFinite(radioStartedAt) && deps.getState().playback.playing && !deps.getState().playback.paused) {
        position = Math.min(duration, Math.max(0, Date.now() / 1000 - radioStartedAt));
    }
    const hasProgress = !!currentTrack && Number.isFinite(duration) && duration > 0;
    setFooterProgressState(hasProgress, radioTimed);
    deps.getElements().seekSlider.disabled = hasProgress && radioTimed;
    deps.getElements().seekSlider.setAttribute('aria-disabled', hasProgress && radioTimed ? 'true' : 'false');
    if (!hasProgress) {
        deps.getElements().seekCurrent.textContent = '0:00';
        deps.getElements().seekDuration.textContent = '0:00';
        deps.getElements().seekSlider.value = 0;
        deps.setRangeProgress(deps.getElements().seekSlider, 0);
        return;
    }
    deps.getElements().seekDuration.textContent = deps.formatTime(duration);
    if (!seekDragging) {
        deps.getElements().seekCurrent.textContent = deps.formatTime(position);
        if (duration > 0) {
            deps.getElements().seekSlider.value = Math.round((position / duration) * 1000);
        } else {
            deps.getElements().seekSlider.value = 0;
        }
        deps.setRangeProgress(deps.getElements().seekSlider, Number(deps.getElements().seekSlider.value || 0) / 1000);
    }
}

function renderSamplerateUI() {
    if (!deps.getElements().samplerateStatus) return;
    // A streaming source (Spotify/Qobuz) owns the footer pill exclusively via
    // updateFooterForStreamingOwner. The general samplerate poll runs
    // independently (every SAMPLERATE_POLL_INTERVAL_MS) and must never
    // overwrite the provider's quality line with the bare hardware rate — that
    // is the Qobuz footer flicker between "FLAC · 16bit · 44.1kHz" and
    // "44.1kHz". No state is cached here: this path simply does not own the
    // pill while a streaming source does.
    if (deps.isStreamingFooterSource(window.__footerSource)) {
        deps.footerDebug('samplerate-ui-streaming-owner', {
            skipped: true,
            owner: window.__footerSource,
        });
        return;
    }
    // Keep source codec/bitrate facts, but the kHz value always describes the
    // effective graph/hardware output rate.
    const activeSource = deps.getState().playback.current_track?.source;
    if (activeSource === 'radio' || activeSource === 'local' || activeSource === 'tidal') {
        const streamLine = formatRadioStreamLine(deps.getState().playback.stream_info, deps.getState().samplerate?.active_rate);
        if (streamLine) {
            deps.getElements().samplerateStatus.textContent = streamLine;
            deps.getElements().samplerateStatus.classList.remove('hidden');
        } else {
            deps.getElements().samplerateStatus.textContent = '';
            deps.getElements().samplerateStatus.classList.add('hidden');
        }
        return;
    }
    const samplerate = deps.getState().samplerate || {};
    if (!samplerate.available || !samplerate.active_rate) {
        deps.getElements().samplerateStatus.textContent = 'Auto';
        deps.getElements().samplerateStatus.classList.add('hidden');
        return;
    }
    // The footer badge always shows the resolved/active hardware rate only;
    // the policy mode (Auto/Fixed) never belongs into this badge.
    deps.getElements().samplerateStatus.textContent = deps.formatRateKhz(samplerate.active_rate);
    deps.getElements().samplerateStatus.classList.remove('hidden');
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
    const samplerate = deps.getState().samplerate || {};
    return samplerate.available && samplerate.active_rate
        ? deps.formatRateKhz(samplerate.active_rate)
        : '';
}

// Footer update for an external streaming owner (Spotify or Qobuz) — single
// source of truth for the shared normalized streaming state shape.
function updateFooterForStreamingOwner(data) {
    if (!deps.isStreamingFooterSource(window.__footerSource)) return;
    if (deps.footerContentFreezeActive()) return;
    const hasMedia = !!(data?.available && (data.title || data.artist || data.album || data.status !== 'Stopped'));
    deps.renderTrackFavoriteButton(null);
    updatePlaybackCover(hasMedia ? streamingArtworkItem(data, window.__footerSource) : null);
    deps.getElements().playbackBar?.classList.toggle('has-media', hasMedia);
    deps.getElements().playbackBar?.classList.toggle('is-playing', hasMedia && data.status === 'Playing');
    deps.getElements().playbackBar?.classList.toggle('is-paused', hasMedia && data.status === 'Paused');
    if (typeof data.volume === 'number') {
        deps.applyRemoteVolume(data.volume);
        if (!deps.isVolumeGestureActive() && !deps.isVolumeRequestInFlight() && deps.getPendingVolume() === null) {
            deps.renderVolumeControlsFromActualVolume(deps.getState().playback.volume);
        }
    }
    if (!hasMedia) {
        renderFooterModeButtons();
        setFooterProgressState(false);
        if (deps.getElements().btnPlayPause) {
            deps.getElements().btnPlayPause.disabled = true;
            deps.getElements().btnPlayPause.textContent = '▶';
        }
        if (deps.getElements().btnPrevious) deps.getElements().btnPrevious.classList.add('hidden');
        if (deps.getElements().btnNext) deps.getElements().btnNext.classList.add('hidden');
        if (deps.getElements().btnClearQueue) deps.getElements().btnClearQueue.classList.add('hidden');
        if (deps.getElements().queueStatus) deps.getElements().queueStatus.classList.add('hidden');
        if (deps.getElements().samplerateStatus) deps.getElements().samplerateStatus.classList.add('hidden');
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
    if (deps.getElements().btnPlayPause) {
        deps.getElements().btnPlayPause.disabled = false;
        deps.getElements().btnPlayPause.textContent = data.status === 'Playing' ? '⏸' : '▶';
    }
    if (deps.getElements().btnPrevious) { deps.getElements().btnPrevious.classList.remove('hidden'); deps.getElements().btnPrevious.disabled = false; }
    if (deps.getElements().btnNext) { deps.getElements().btnNext.classList.remove('hidden'); deps.getElements().btnNext.disabled = false; }
    if (deps.getElements().btnClearQueue) { deps.getElements().btnClearQueue.classList.add('hidden'); }
    if (deps.getElements().queueStatus) { deps.getElements().queueStatus.classList.add('hidden'); }
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
    if (deps.getElements().seekSlider && deps.getElements().seekCurrent && deps.getElements().seekDuration) {
        const pos = Number(data.position || 0);
        const dur = Number(data.duration || 0);
        const hasProgress = Number.isFinite(dur) && dur > 0;
        setFooterProgressState(hasProgress, false);
        deps.getElements().seekSlider.disabled = false;
        deps.getElements().seekSlider.setAttribute('aria-disabled', 'false');
        if (!hasProgress) {
            deps.getElements().seekCurrent.textContent = '0:00';
            deps.getElements().seekDuration.textContent = '0:00';
            deps.getElements().seekSlider.value = 0;
            deps.setRangeProgress(deps.getElements().seekSlider, 0);
        } else if (!window.__streamingSeeking) {
            deps.getElements().seekCurrent.textContent = deps.formatTime(pos);
            deps.getElements().seekDuration.textContent = deps.formatTime(dur);
            deps.getElements().seekSlider.value = Math.round((pos / dur) * 1000);
            deps.setRangeProgress(deps.getElements().seekSlider, Number(deps.getElements().seekSlider.value || 0) / 1000);
        }
    }
    if (deps.getElements().samplerateStatus) {
        // Shared library/radio meta-tag renderer: Qobuz contributes its real
        // stream facts, Spotify only the resolved rate (no invented format).
        // No UI-side caching: the backend keeps the track's stream facts
        // complete across transient gaps, so the payload is authoritative.
        deps.footerDebug('streaming-footer-meta', {
            owner: window.__footerSource,
            source: data?.source || null,
            trackId: data?.trackId || null,
            audio_format: data?.audio_format ?? null,
            bit_depth: data?.bit_depth ?? null,
            bitrate: data?.bitrate ?? data?.bitrate_kbps ?? null,
            sample_rate: data?.sample_rate ?? null,
        });
        const samplerateLine = formatStreamingMetaLine(data);
        deps.getElements().samplerateStatus.textContent = samplerateLine;
        deps.getElements().samplerateStatus.classList.toggle('hidden', !samplerateLine);
    }
    renderPeakWarningBadge(data.status === 'Playing');
    renderFooterModeButtons();
}

function renderFooterFavoriteFromTidalChange() {
    // The footer heart mirrors the current TIDAL track favorite. Called on
    // every canonical favorites change so the footer never drifts from the
    // state the TIDAL tab/detail rows use.
    deps.renderTrackFavoriteButton(deps.getState().playback.current_track);
}

async function toggleTidalFooterFavorite(track) {
    const streaming = window.FXRouteStreaming;
    if (!streaming || !streaming.toggleTidalFavorite || !track?.id || deps.isTidalFavoriteRequestInFlight()) return;
    if (!streaming.tidalFavoritesReady()) {
        // State not loaded yet — request it and let the state render path
        // re-enable the button; do not guess a half-hearted toggle.
        void streaming.ensureTidalFavoritesLoaded().then(() => deps.renderTrackFavoriteButton(deps.getState().playback.current_track));
        return;
    }
    deps.setTidalFavoriteRequestInFlight(true);
    deps.renderTrackFavoriteButton(track);
    try {
        await streaming.toggleTidalFavorite('tracks', track.id);
    } catch (error) {
        deps.showToast((error && error.message) || 'Failed to update favorite', 'error');
    } finally {
        deps.setTidalFavoriteRequestInFlight(false);
        // Re-derive from canonical state: on success the toggle flipped the
        // ids, on failure they are unchanged, so the button never shows a
        // stale optimistic value.
        deps.renderTrackFavoriteButton(deps.getState().playback.current_track);
    }
}

async function toggleCurrentTrackFavorite() {
    const track = deps.getState().playback.current_track;
    if (!track || !track.id) return;
    if (track.source === 'tidal') {
        await toggleTidalFooterFavorite(track);
        return;
    }
    if (track.source !== 'local') return;
    await deps.toggleTrackFavoriteById(track.id);
}

    return {
        init,
        syncPlaybackFooterSpace,
        schedulePlaybackFooterSpaceSync,
        initPlaybackFooterLayout,
        setFooterProgressState,
        showVolumeDisplayTemporarily,
        setupPlaybackControls,
        renderFooterModeButtons,
        toggleFooterShuffle,
        toggleFooterLoop,
        updatePlaybackUI,
        updatePlayPauseButton,
        highlightActiveTrack,
        renderQueueUI,
        meterLitCount,
        responsiveMeterSegmentCount,
        renderMeterChannel,
        isFiniteVuDb,
        rememberValidVu,
        heldVuSnapshot,
        renderStereoMeter,
        formatOutputLevelBadgeDb,
        renderPeakWarningBadge,
        coverDetailSections,
        isCoverDetailOpen,
        coverDetailMeta,
        coverDetailStreamingMeta,
        setCoverDetailText,
        coverDetailExtra,
        coverQueuePlayTarget,
        renderCoverDetailCard,
        openCoverDetailCard,
        closeCoverDetailCard,
        toggleCoverDetailCard,
        trackCoverUrl,
        trackCoverInfoUrl,
        trackCoverKnownAvailable,
        playbackArtworkUrl,
        playbackArtworkKnownAvailable,
        streamingArtworkItem,
        updatePlaybackCover,
        scheduleNowPlayingCueRemoval,
        revealNowPlayingCoverWhenReady,
        showNowPlayingCue,
        streamingCueTrack,
        streamingCueTrackId,
        streamingCueKey,
        showStreamingQueueStarted,
        lastPlayingQueueKey,
        recordPlayingQueueKey,
        maybeShowStreamingQueueCue,
        nativeTrackCueKey,
        maybeShowNativeTrackCue,
        seedNativeTrackCueKey,
        maybeCueNativePlaybackTrack,
        initSeek,
        seekStart,
        seekEnd,
        seekChange,
        updateSeekUI,
        renderSamplerateUI,
        formatRadioStreamLine,
        formatStreamingMetaLine,
        updateFooterForStreamingOwner,
        renderFooterFavoriteFromTidalChange,
        toggleTidalFooterFavorite,
        toggleCurrentTrackFavorite
    };
});
