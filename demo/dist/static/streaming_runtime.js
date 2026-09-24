// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute streaming runtime: Spotify/Qobuz status fetch, state merge,
 * incoming-state processing, transport commands, polling with poll
 * generations and the provider-side footer/ownership sync.
 *
 * Footer ownership and window.__footerSource decisions stay exclusively in
 * static/playback_core.js; footer rendering stays in static/playback_ui.js.
 * This module never assigns window.__footerSource and never branches the
 * footer on provider identity beyond the normalized data lookup. The
 * provider tabs/browse UI in static/streaming.js is a separate layer and
 * reaches this runtime only through explicit callbacks wired in app.js.
 *
 * State/DOM go through injected callbacks where needed; document/window are
 * used directly. Browser-loadable UMD, no build step; Node-testable via
 * require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteStreamingRuntime = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        fetchFn: (...args) => fetch(...args),
        apiPostJson: async () => null,
        showToast: () => {},
        reconcileFooterSource: () => {},
        footerDebug: () => {},
        getBackendFooterOwner: () => null,
        shouldPollSpotify: () => false,
        armSpotifyTakeover: () => {},
        updateFooterForStreamingOwner: () => {},
        maybeShowStreamingQueueCue: () => false,
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

let _spotifyInstalledKnown = null;
let _spotifyPollTimer = null;
let _spotifyCommandInFlight = false;
let _spotifySeekCommitTimer = null;
let _spotifyLastRenderedTrackKey = '';
let _spotifyLastPositionUpdateAt = 0;
let _qobuzPollTimer = null;
let _qobuzPollGeneration = 0;
let _qobuzPollTimerGeneration = null;
let _spotifyPollGeneration = 0;
let _spotifyPollTimerGeneration = null;

function bumpSpotifyPollGeneration() {
    _spotifyPollGeneration += 1;
}

function bumpQobuzPollGeneration() {
    _qobuzPollGeneration += 1;
}

function isSpotifyTransportInFlight() {
    return _spotifyCommandInFlight;
}

// ---------------------------------------------------------------------------
// Fetch
// ---------------------------------------------------------------------------
async function fetchSpotifyStatus() {
    try {
        const resp = await deps.fetchFn('/api/spotify/status');
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
        const resp = await deps.fetchFn('/api/streaming/qobuz/status');
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
    deps.reconcileFooterSource();
}

function shouldAdoptSpotifyUpdate(data) {
    if (!data || !data.available) return false;
    const isPlaying = data.status === 'Playing';
    if (isPlaying) return true;
    deps.reconcileFooterSource();
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
    deps.reconcileFooterSource();
    if (renderFooter && window.__footerSource === 'qobuz') {
        deps.updateFooterForStreamingOwner(normalized);
    }
    deps.maybeShowStreamingQueueCue('qobuz', previousQobuzData, normalized);
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

    deps.footerDebug('incoming-spotify-state', {
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
    deps.reconcileFooterSource();

    if (trackChanged) {
        _spotifyLastRenderedTrackKey = nextTrackKey;
        _spotifyLastPositionUpdateAt = Date.now();
    }

    if (renderFooter && window.__footerSource === 'spotify') {
        deps.updateFooterForStreamingOwner(mergedData);
    }
    if (renderTab) {
        const spotifyTab = document.getElementById('tab-spotify');
        if (spotifyTab && spotifyTab.classList.contains('active')) {
            renderSpotifyTab(mergedData);
        }
    }        deps.maybeShowStreamingQueueCue('spotify', previousData, mergedData);
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
    if (data) deps.updateFooterForStreamingOwner(data);
}

async function forceSpotifyRefreshBurst() {
    const delays = [250, 700, 1400];
    for (const delay of delays) {
        setTimeout(async () => {
            try {
                const fresh = await fetchSpotifyStatus();
                handleIncomingSpotifyState(fresh, { renderTab: true, renderFooter: true });
                deps.reconcileFooterSource();
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
        const data = await deps.apiPostJson(`/api/streaming/qobuz/${action}`);
        window.__qobuzLastData = data;
        deps.reconcileFooterSource();
        // Only paint when Qobuz actually owns the footer: a concurrent
        // Spotify session keeps ownership and must not show Qobuz metadata.
        if (window.__footerSource === 'qobuz') deps.updateFooterForStreamingOwner(data);
        if (action === 'play' || action === 'toggle') {
            deps.maybeShowStreamingQueueCue('qobuz', prev, data);
        }
        return data;
    } catch (e) {
        deps.showToast('Qobuz transport failed', 'error');
        return null;
    }
}

async function qobuzSeek(positionSec) {
    try {
        const data = await deps.apiPostJson('/api/streaming/qobuz/seek', { position: positionSec });
        if (data) {
            window.__qobuzLastData = data;
            if (window.__footerSource === 'qobuz') deps.updateFooterForStreamingOwner(data);
        }
    } catch (e) {
        console.debug('Qobuz seek failed', e);
    }
}

async function spotifyCommand(action) {
    if (_spotifyCommandInFlight) return;
    const interactiveTakeover = ['play', 'toggle', 'next', 'previous'].includes(action);
    if (interactiveTakeover) {
        deps.armSpotifyTakeover();
    }
    const gen = _spotifyPollGeneration;
    _spotifyCommandInFlight = true;
    try {
        const data = await deps.apiPostJson(`/api/spotify/${action}`);
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
        const data = await deps.apiPostJson('/api/spotify/seek', { position: positionSec });
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
    return qobuzIsInstalled() && (window.__visibleTab === 'qobuz' || window.__footerSource === 'qobuz' || deps.getBackendFooterOwner() === 'qobuz');
}

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

function startSpotifyPoll() {
    if (!deps.shouldPollSpotify()) return;
    if (_spotifyPollTimer) {
        // The timer is running but its generation was invalidated elsewhere —
        // restart it instead of leaving a poller that can never deliver updates.
        if (_spotifyPollTimerGeneration !== _spotifyPollGeneration) stopSpotifyPoll();
        else return;
    }
    const gen = ++_spotifyPollGeneration;
    _spotifyPollTimerGeneration = gen;
    _spotifyPollTimer = setInterval(async () => {
        if (document.hidden) return;
        if (!deps.shouldPollSpotify()) {
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

// Spotify tab internal UI (cover, controls inside the tab)
function renderSpotifyTab(data) {
    renderSpotify(data);
}

async function initSpotify() {
    const data = await fetchSpotifyStatus();
    handleIncomingSpotifyState(data, { renderTab: true, renderFooter: true });
    if (deps.shouldPollSpotify()) {
        startSpotifyPoll();
    } else {
        stopSpotifyPoll();
    }
}

    return {
        init,
        fetchSpotifyStatus,
        fetchQobuzStatus,
        spotifyTrackKey,
        mergeSpotifyState,
        syncSpotifySourceOwnership,
        shouldAdoptSpotifyUpdate,
        syncSpotifyTabAvailability,
        handleIncomingQobuzState,
        handleIncomingSpotifyState,
        renderSpotify,
        updateGlobalControlsForSource,
        forceSpotifyRefreshBurst,
        qobuzCommand,
        qobuzSeek,
        spotifyCommand,
        spotifySeek,
        qobuzIsInstalled,
        shouldPollQobuz,
        stopQobuzPoll,
        startQobuzPoll,
        stopSpotifyPoll,
        startSpotifyPoll,
        renderSpotifyTab,
        initSpotify,
        bumpSpotifyPollGeneration,
        bumpQobuzPollGeneration,
        isSpotifyTransportInFlight
    };
});
