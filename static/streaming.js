// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute streaming provider UI (Spotify, Qobuz, TIDAL).
 *
 * Spotify and Qobuz own provider tabs with one capability-driven "now playing"
 * card each. The module never branches on provider identity to decide which
 * controls to show: it reads the provider's `capabilities` and omits anything
 * unsupported. Transport dispatch is a thin per-provider backend adapter
 * (Spotify -> app.js playerctl handler, Qobuz -> generic
 * /api/streaming/qobuz/...).
 *
 * TIDAL has no tab of its own: its status, favorites, search, details and
 * playlist writes are exposed to the Library through `FXRouteStreaming.tidal`.
 * Native TIDAL playback remains owned by app.js (MPV -> fxroute_dsp_sink).
 */
(function () {
    'use strict';

    let api = null;
    let showToast = function () {};
    // Track-change cue decision for the native player, owned by app.js and
    // injected here, so an explicit TIDAL play and the WebSocket playback frame
    // for that same start resolve through one state machine instead of two.
    let maybeShowNativeTrackCue = function () { return false; };
    // The queue-started cue decision is owned by app.js and injected here, so
    // the tab transport and the incoming-state/poll paths use one state
    // machine instead of two copies that can drift apart.
    let maybeShowStreamingQueueCue = function () { return false; };
    let escapeHtml = function (v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, character => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;',
        })[character]);
    };
    // Shared transition-error formatter injected by app.js. The local default
    // mirrors the canonical app.js implementation so this module never renders
    // "[object Object]" standalone: structured {message, stage} payloads keep
    // message + stage, message-less details fall back to generic text (with a
    // console.warn for diagnosis) instead of leaking raw JSON into the UI.
    let formatTransitionErrorDetail = function (detail, fallback = 'Request failed') {
        if (typeof detail === 'string') {
            const trimmedDetail = detail.trim();
            if (trimmedDetail) return trimmedDetail;
        } else if (detail && typeof detail === 'object') {
            const message = typeof detail.message === 'string' ? detail.message.trim() : '';
            if (message) {
                const stage = typeof detail.stage === 'string' ? detail.stage.trim() : '';
                if (stage && !message.toLowerCase().includes(stage.toLowerCase())) {
                    return message + ' (stage: ' + stage + ')';
                }
                return message;
            }
            // Message-less structured details stay out of the UI: keep them for
            // diagnosis via console.warn and fall through to the generic fallback.
            try {
                if (typeof console !== 'undefined' && typeof console.warn === 'function') {
                    console.warn('Suppressed message-less error detail:', detail);
                }
            } catch (_warnError) {
                // Logging must never break error rendering.
            }
        }
        return typeof fallback === 'string' ? fallback : 'Request failed';
    };
    // Canonical favorite heart: one inline SVG painted from currentColor, so
    // the muted / accent button states stay authoritative. app.js owns the
    // implementation and injects it in init; the default keeps this module
    // renderable standalone, exactly like escapeHtml above.
    let favoriteHeartSvg = function () {
        return '<svg class="fav-heart" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/></svg>';
    };

    /* Line-icon variants for the loop transport (replaces color emojis). */
    const LOOP_ALL_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>';
    const LOOP_TRACK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m17 2 4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="m7 22-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/><path d="M11 10h1v4"/></svg>';
    let formatTime = function () { return '0:00'; };
    let artworkPlaceholderUrl = function () { return '/static/artwork-placeholder.svg?v=2'; };
    // Shared detail track-row builder, supplied by app.js so the library album
    // detail and the Tidal album/playlist details render the same row.
    let trackRowHtml = function () { return ''; };
    // Shared metadata-rows and collapsible About builders (library + Tidal
    // album details) supplied by app.js — one component, no per-provider copy.
    let factsHtml = function () { return ''; };
    let aboutHtml = function () { return ''; };
    let initialized = false;

    // Shared compact content-state markup (same component — and therefore the
    // same Loading / Empty / Error vocabulary — used by Library and Radio).
    function contentState(kind, message) {
        return '<div class="content-state content-state--' + kind + '">' + escapeHtml(message || '') + '</div>';
    }

    // Provider -> transport backend adapter. The rendering is capability
    // driven; only the endpoint each provider's transport must hit is here.
    const TRANSPORT = {
        spotify: { kind: 'app', action: { toggle: 'toggle', next: 'next', previous: 'previous', shuffle: 'shuffle', loop: 'loop' } },
        qobuz: { kind: 'remote', base: '/api/streaming/qobuz', action: { toggle: 'toggle', next: 'next', previous: 'previous', shuffle: 'shuffle', loop: 'repeat' } },
    };

    // Provider-specific *content* facts (display name, whether FXRoute offers an
    // in-app connect flow). Control rendering stays capability-driven; only
    // these copy/content flags key off the provider id.
    const PROVIDER_META = {
        spotify: { name: 'Spotify', canConnect: false },
        qobuz: { name: 'Qobuz', canConnect: true },
        tidal: { name: 'Tidal', canConnect: true },
    };

    const POLL_INTERVAL_MS = 2000;

    const state = {
        providers: {},          // id -> { descriptor, root, els, tabBtn, tabPanel }
        lastData: {},           // id -> last normalized status payload
        refreshSerials: {},     // id -> latest status request or external frame
        appliedSerials: {},     // id -> latest applied response or external frame
        polls: {},              // id -> interval timer
        enabled: {},            // id -> boolean (Settings -> Providers choice)
        lastPlaybackSource: null,
        lastPlaybackAt: 0,
        tidal: {
            view: null,         // 'album' | 'playlist' | 'artist' | null (Library detail chain)
            viewStack: [],      // previous detail views so Back returns through nested details
            // Persistent playlist-build selection: tracks consciously added
            // via the row + button. Survives navigation between search
            // results, favorites and detail views; play and favorite actions
            // never touch it. Saved as a new TIDAL playlist or added to an
            // existing one via the save row.
            selectedTrackIds: new Set(),
            detailRequestId: 0,
            detailId: null,
            detailTitle: '',
            detailArt: '',
            favoriteIds: { tracks: new Set(), albums: new Set(), artists: new Set(), playlists: new Set() },
            favoriteIdsPromise: null,
            favoritesLoaded: false,   // true after at least one successful favorites/ids load
            // Persistent browse cache (server-side SQLite snapshot of the last
            // successful library load, keyed by TIDAL account). Rendered first
            // on open; a background refresh then replaces it in place.
            cache: null,            // snapshot payload {user_id, ids, tracks, albums, artists, playlists}
            cacheUser: null,        // account the snapshot/lastItems belong to
            snapshotPromise: null,  // in-flight snapshot fetch (one per account)
            lastItems: {},          // account-keyed last rendered browse payloads (per category)
            // Similar artists often arrive from MusicBrainz without a TIDAL
            // mapping. Keep successful name lookups in the browser so a later
            // card can use the result immediately and concurrent cards share
            // one request.
            artistLookupCache: new Map(),
            artistLookupPromises: new Map(),
            // Library-side collection loads (favorite albums/tracks/playlists).
            libraryPromise: null,
            libraryLoadedAt: 0,
        },
        descriptors: {},        // id -> last discovery descriptor (with or without a tab)
    };

    // Library surface that hosts TIDAL details ({ container, onOpen, onClose }).
    let libraryHost = null;

    // -----------------------------------------------------------------------
    // Public API (called by app.js)
    // -----------------------------------------------------------------------
    function init(interfaceApi) {
        if (initialized) return;
        api = interfaceApi || {};
        if (typeof api.showToast === 'function') showToast = api.showToast;
        if (typeof api.maybeShowNativeTrackCue === 'function') maybeShowNativeTrackCue = api.maybeShowNativeTrackCue;
        if (typeof api.maybeShowStreamingQueueCue === 'function') maybeShowStreamingQueueCue = api.maybeShowStreamingQueueCue;
        if (typeof api.escapeHtml === 'function') escapeHtml = api.escapeHtml;
        if (typeof api.formatTransitionErrorDetail === 'function') formatTransitionErrorDetail = api.formatTransitionErrorDetail;
        if (typeof api.favoriteHeartSvg === 'function') favoriteHeartSvg = api.favoriteHeartSvg;
        if (typeof api.formatTime === 'function') formatTime = api.formatTime;
        if (typeof api.artworkPlaceholderUrl === 'function') artworkPlaceholderUrl = api.artworkPlaceholderUrl;
        if (typeof api.trackRowHtml === 'function') trackRowHtml = api.trackRowHtml;
        if (typeof api.factsHtml === 'function') factsHtml = api.factsHtml;
        if (typeof api.aboutHtml === 'function') aboutHtml = api.aboutHtml;
        buildProviderDom();
        initialized = true;
        void loadProviders();
    }

    // Render one provider from a normalized status payload. Used by app.js for
    // Spotify and by this module's own refresh loop for Qobuz/TIDAL.
    function renderProvider(providerId, data) {
        if (!data) return;
        state.refreshSerials[providerId] = (state.refreshSerials[providerId] || 0) + 1;
        state.appliedSerials[providerId] = state.refreshSerials[providerId];
        renderProviderData(providerId, data);
    }

    function renderProviderData(providerId, data) {
        if (!data) return;
        state.lastData[providerId] = data;
        // The Library consumes TIDAL status too; it must not depend on the
        // provider tab being present.
        if (providerId === 'tidal') notifyTidalStatus();
        const entry = state.providers[providerId];
        if (!entry) return;
        applyTabVisibility(providerId, entry, data);
        renderStatusLine(providerId, entry, data);
        renderNowPlaying(providerId, entry, data);
    }

    // Native playback changed (WebSocket `playback` message). Nudge the TIDAL
    // tab so its now-playing follows the shared owner without its own poller.
    function notifyPlayback(playback) {
        if (!playback) return;
        state.lastPlaybackSource = playback?.current_track?.source || null;
        state.lastPlaybackAt = Date.now();
        const now = Date.now();
        // The global footer shows the TIDAL track favorite; the canonical
        // favorites ids must be loaded even when playback starts without the
        // Library ever being opened. The load dispatches fxroute:tidal-favorites
        // so the footer re-derives its state from the server truth.
        if (state.lastPlaybackSource === 'tidal' && !state.tidal.favoritesLoaded) {
            void ensureTidalFavoritesLoaded();
        }
        if (state.lastPlaybackSource === 'tidal' && now - (state._tidalNudgeAt || 0) > 700) {
            state._tidalNudgeAt = now;
            void refreshTidalStatus();
        }
    }

    // Switch-tab entry point: start/stop per-provider polling.
    function onTabVisible(tabId) {
        stopAllPolls();
        if (tabId === 'qobuz') startPoll('qobuz', POLL_INTERVAL_MS);
        else if (tabId === 'library') {
            // One status read on entry (no poll): the Library shows TIDAL
            // favorites next to local music and needs the login state.
            if (tidalEnabledAndInstalled()) void refreshTidalStatus();
        }
    }

    // -----------------------------------------------------------------------
    // DOM construction
    // -----------------------------------------------------------------------
    function buildProviderDom() {
        document.querySelectorAll('.streaming-shell[data-provider]').forEach((root) => {
            const providerId = root.getAttribute('data-provider');
            root.innerHTML =
                '<div class="streaming-provider">' +
                    '<span class="streaming-status" hidden></span>' +
                    '<div class="streaming-empty" hidden>' +
                        '<div class="streaming-empty-icon" aria-hidden="true"></div>' +
                        '<h2 class="streaming-empty-title"></h2>' +
                        '<p class="streaming-empty-msg"></p>' +
                        '<div class="streaming-empty-actions"></div>' +
                    '</div>' +
                    '<div class="streaming-now-playing" hidden>' +
                        '<div class="streaming-card-header">' +
                            '<span class="streaming-provider-name"></span>' +
                            '<span class="streaming-status" hidden></span>' +
                        '</div>' +
                        '<div class="streaming-cover-wrap">' +
                            '<img class="streaming-cover" alt="" />' +
                        '</div>' +
                        '<div class="streaming-meta">' +
                            '<div class="streaming-title"></div>' +
                            '<div class="streaming-artist"></div>' +
                            '<div class="streaming-album"></div>' +
                            '<div class="streaming-queue" hidden></div>' +
                        '</div>' +
                        '<div class="streaming-controls">' +
                            '<button type="button" class="streaming-btn" data-action="previous" aria-label="Previous" data-tooltip="Previous">⏮</button>' +
                            '<button type="button" class="streaming-btn streaming-btn-main" data-action="toggle" aria-label="Play / Pause" data-tooltip="Play / Pause">▶</button>' +
                            '<button type="button" class="streaming-btn" data-action="next" aria-label="Next" data-tooltip="Next">⏭</button>' +
                        '</div>' +
                        '<div class="streaming-secondary">' +
                            '<button type="button" class="streaming-btn-sm" data-action="shuffle" data-tooltip="Shuffle">' +
                                '<span class="streaming-btn-sm-icon">⇄</span><span class="streaming-btn-sm-label">Shuffle</span>' +
                            '</button>' +
                            '<button type="button" class="streaming-btn-sm" data-action="loop" data-tooltip="Loop">' +
                                '<span class="streaming-btn-sm-icon">' + LOOP_ALL_ICON + '</span><span class="streaming-btn-sm-label">Loop</span>' +
                            '</button>' +
                        '</div>' +
                        '<div class="streaming-progress-row">' +
                            '<span class="streaming-time streaming-time-current">0:00</span>' +
                            '<input type="range" class="streaming-progress" min="0" max="1000" step="1" value="0" aria-label="Seek" />' +
                            '<span class="streaming-time streaming-time-total">0:00</span>' +
                        '</div>' +
                    '</div>' +
                '</div>';

            // Player pages (Spotify/Qobuz) get a centered stage.
            const providerEl = root.querySelector('.streaming-provider');
            providerEl.classList.add('streaming-provider-player');

            const els = {
                statusLine: root.querySelector('.streaming-status'),
                providerName: root.querySelector('.streaming-provider-name'),
                empty: root.querySelector('.streaming-empty'),
                emptyIcon: root.querySelector('.streaming-empty-icon'),
                emptyTitle: root.querySelector('.streaming-empty-title'),
                emptyMsg: root.querySelector('.streaming-empty-msg'),
                emptyActions: root.querySelector('.streaming-empty-actions'),
                nowPlaying: root.querySelector('.streaming-now-playing'),
                statusChip: root.querySelector('.streaming-card-header .streaming-status'),
                coverWrap: root.querySelector('.streaming-cover-wrap'),
                cover: root.querySelector('.streaming-cover'),
                title: root.querySelector('.streaming-title'),
                artist: root.querySelector('.streaming-artist'),
                album: root.querySelector('.streaming-album'),
                queueInfo: root.querySelector('.streaming-queue'),
                controls: root.querySelector('.streaming-controls'),
                prev: root.querySelector('[data-action="previous"]'),
                toggle: root.querySelector('[data-action="toggle"]'),
                next: root.querySelector('[data-action="next"]'),
                secondary: root.querySelector('.streaming-secondary'),
                shuffle: root.querySelector('[data-action="shuffle"]'),
                loop: root.querySelector('[data-action="loop"]'),
                loopIcon: root.querySelector('[data-action="loop"] .streaming-btn-sm-icon'),
                progressRow: root.querySelector('.streaming-progress-row'),
                progress: root.querySelector('.streaming-progress'),
                timeCurrent: root.querySelector('.streaming-time-current'),
                timeTotal: root.querySelector('.streaming-time-total'),
            };

            const entry = {
                root,
                els,
                providerId,
                tabBtn: document.querySelector('.tab-btn[data-tab="' + providerId + '"]'),
                tabPanel: document.getElementById('tab-' + providerId),
                seeking: false,
            };
            // The player card header shows the provider name next to the
            // status chip; the standalone page titles above the card are gone.
            const providerName = PROVIDER_META[providerId]?.name || providerId;
            if (els.providerName) els.providerName.textContent = providerName;
            state.providers[providerId] = entry;
            wireNowPlayingTransport(providerId, entry);
        });
    }

    // -----------------------------------------------------------------------
    // Tab visibility
    // -----------------------------------------------------------------------
    function applyTabVisibility(providerId, entry, data) {
        const enabled = state.enabled[providerId] !== false;
        const available = data.installed === true;
        const nonApp = typeof nonAppSourceModeActive === 'function' && nonAppSourceModeActive();
        const visible = available && enabled && !nonApp;
        const show = available && enabled;
        const tabWasShown = isTabShown(entry.tabBtn);
        if (entry.tabBtn) {
            entry.tabBtn.hidden = !show;
            entry.tabBtn.style.display = show ? '' : 'none';
            entry.tabBtn.classList.toggle('hidden', !visible);
        }
        if (entry.tabPanel) {
            entry.tabPanel.hidden = !show;
            entry.tabPanel.classList.toggle('hidden', !visible);
        }
        if (!show && state.polls[providerId]) stopPoll(providerId);
        // A provider tab that just became hidden must not stay the active panel.
        // The switch itself lives in app.js (same pattern as source mode).
        if (!show && window.__visibleTab === providerId && typeof switchTab === 'function') {
            switchTab('radio');
        }
        // The discovery poll re-applies unchanged flags every 10s; only a tab
        // that appeared or disappeared changes the strip geometry.
        if (tabWasShown !== isTabShown(entry.tabBtn) && typeof updateTabsScrollAffordance === 'function') {
            updateTabsScrollAffordance();
        }
    }

    function isTabShown(tabBtn) {
        return !!tabBtn && !tabBtn.hidden && !tabBtn.classList.contains('hidden');
    }

    // Settings -> Providers: apply a new enabled flag immediately without
    // waiting for the next discovery poll.
    function applyProviderEnabled(providerId, enabled) {
        state.enabled[providerId] = enabled === true;
        const entry = state.providers[providerId];
        const data = { ...(state.lastData[providerId] || {}), ...(entry?.descriptor || {}) };
        if (entry) applyTabVisibility(providerId, entry, { installed: data.installed, available: data.available });
        if (providerId === 'tidal') notifyTidalStatus();
    }

    // Poll /api/streaming/providers/discovery periodically so an install or
    // uninstall started elsewhere (or in Settings) flips tab visibility.
    async function refreshEnabledFlags() {
        try {
            const resp = await fetch('/api/streaming/providers/discovery');
            if (!resp.ok) return;
            const payload = await resp.json();
            for (const descriptor of (Array.isArray(payload.providers) ? payload.providers : [])) {
                const entry = state.providers[descriptor.id];
                const prev = state.enabled[descriptor.id];
                const prevInstalled = state.descriptors[descriptor.id]?.installed;
                state.enabled[descriptor.id] = descriptor.enabled !== false;
                state.descriptors[descriptor.id] = descriptor;
                if (descriptor.id === 'tidal' && (prev !== state.enabled.tidal || prevInstalled !== descriptor.installed)) {
                    notifyTidalStatus();
                }
                if (entry) {
                    entry.descriptor = descriptor;
                    applyTabVisibility(descriptor.id, entry, {
                        ...(state.lastData[descriptor.id] || {}),
                        ...descriptor,
                    });
                } else if (prev !== undefined && prev !== state.enabled[descriptor.id]) {
                    applyProviderEnabled(descriptor.id, state.enabled[descriptor.id]);
                }
            }
        } catch (err) {
            // transient; retried by the interval
        }
    }

    // -----------------------------------------------------------------------
    // Status (backend / connected / authenticated)
    // -----------------------------------------------------------------------
    // All three providers share one status language: a single `Connected`
    // label with a dot-led pill. The detail string stays in the tooltip and
    // backend implementation names are never surfaced.
    function buildStatusBits(providerId, data) {
        if (providerId === 'spotify') {
            return data.connected === true || data.spotifyd_standby === true ? ['Connected'] : [];
        }
        if (providerId === 'qobuz') {
            return (data.connected || data.authenticated === true) ? ['Connected'] : [];
        }
        return [];
    }

    function buildStatusDetail(providerId, data) {
        if (providerId === 'spotify') {
            if (data.backend === 'desktop') return 'Spotify Desktop';
            if (data.backend === 'spotifyd') return 'spotifyd';
            return 'Spotify';
        }
        return 'Qobuz Connect';
    }

    // True while the Library shows a TIDAL album, playlist or artist detail.
    function isTidalDetailView() {
        return state.tidal.view === 'album' || state.tidal.view === 'playlist' || state.tidal.view === 'artist';
    }

    function renderStatusLine(providerId, entry, data) {
        // Player providers integrate the status into the card as a small
        // top-right pill; the provider/backend detail stays available via the
        // pill tooltip. The dot-led label is one shared rendering.
        const connected = buildStatusBits(providerId, data).length > 0;
        entry.els.statusChip.hidden = !connected;
        entry.els.statusChip.textContent = 'Connected';
        entry.els.statusChip.title = buildStatusDetail(providerId, data);
        entry.els.statusLine.hidden = true;
    }

    // -----------------------------------------------------------------------
    // Shared capability-driven now playing card
    // -----------------------------------------------------------------------
    function renderNowPlaying(providerId, entry, data) {
        const els = entry.els;
        const caps = data.capabilities || {};
        const meta = PROVIDER_META[providerId] || { name: entry.providerLabel() || providerId, canConnect: false };

        // Not installed / not available.
        if (data.installed !== true) {
            showEmpty(entry, meta.name + ' is not available.', '');
            return;
        }
        if (data.available === false) {
            showEmpty(entry, providerUnavailableMessage(providerId), '');
            return;
        }

        // Not connected / not authenticated (account-gated providers).
        if (data.authenticated === false) {
            showEmpty(entry, 'Not connected', connectMessage(providerId), meta.canConnect ? 'connect' : null);
            return;
        }

        // Standby: backend alive but FXRoute is not the active output
        // (spotifyd hides MPRIS without a Connect session; qbzd keeps the
        // last track paused after a device switch). Reads as "ready" instead
        // of a stale paused/empty card.
        if (data.spotifyd_standby || data.qbzd_standby) {
            const empty = notPlayingContent(providerId, data);
            showEmpty(entry, empty.title, empty.message);
            return;
        }

        // Stopped with no track.
        if ((data.status === 'Stopped' || !data.status) && !data.title) {
            const empty = notPlayingContent(providerId, data);
            showEmpty(entry, empty.title, empty.message);
            return;
        }

        // Normal now-playing.
        els.empty.hidden = true;
        els.nowPlaying.hidden = false;

        // Cover (capability: cover).
        const hasCover = !!caps.cover && !!data.artUrl;
        els.coverWrap.style.display = hasCover ? '' : 'none';
        if (hasCover && els.cover.getAttribute('src') !== data.artUrl) {
            els.cover.src = data.artUrl;
        }

        // Metadata.
        els.title.textContent = data.title || '';
        els.artist.textContent = data.artist || '';
        els.album.textContent = data.album || '';
        els.album.style.display = data.album ? '' : 'none';

        // Queue continuation (count + next up), data-driven from the provider
        // status; no capability flag needed and no provider-identity branch.
        const queueText = formatQueueInfo(data);
        els.queueInfo.hidden = !queueText;
        els.queueInfo.textContent = queueText;

        // Transport (capability: transport).
        els.controls.style.display = caps.transport ? '' : 'none';
        if (caps.transport) {
            const playing = data.status === 'Playing';
            const icon = playing ? '⏸' : '▶';
            els.toggle.textContent = icon;
            els.toggle.setAttribute('data-tooltip', playing ? 'Pause' : 'Play');
            els.toggle.setAttribute('aria-label', playing ? 'Pause' : 'Play');
            els.prev.disabled = false;
            els.next.disabled = false;
        }

        // Shuffle / loop (capabilities).
        const showShuffle = !!caps.shuffle;
        const showLoop = !!caps.loop;
        els.secondary.style.display = (showShuffle || showLoop) ? '' : 'none';
        if (els.shuffle) {
            els.shuffle.style.display = showShuffle ? '' : 'none';
            els.shuffle.classList.toggle('active', !!data.shuffle);
            els.shuffle.setAttribute('data-tooltip', data.shuffle ? 'Shuffle on' : 'Shuffle off');
            els.shuffle.setAttribute('aria-pressed', data.shuffle ? 'true' : 'false');
        }
        if (els.loop) {
            els.loop.style.display = showLoop ? '' : 'none';
            const loopVal = data.loop || 'none';
            const loopActive = loopVal !== 'none';
            els.loop.classList.toggle('active', loopActive);
            els.loop.setAttribute('aria-pressed', loopActive ? 'true' : 'false');
            els.loop.setAttribute('data-tooltip', loopVal === 'track' ? 'Loop: track' : (loopVal === 'playlist' ? 'Loop: playlist' : 'Loop: off'));
            if (els.loopIcon) els.loopIcon.innerHTML = loopVal === 'track' ? LOOP_TRACK_ICON : LOOP_ALL_ICON;
        }

        // Progress / seek (capabilities: progress + seek).
        const showProgress = !!caps.progress;
        els.progressRow.style.display = showProgress ? '' : 'none';
        if (showProgress && !entry.seeking) {
            const pos = Number(data.position || 0);
            const dur = Number(data.duration || 0);
            const value = dur > 0 ? Math.max(0, Math.min(1000, Math.round((pos / dur) * 1000))) : 0;
            els.progress.value = value;
            els.timeCurrent.textContent = formatTime(pos);
            els.timeTotal.textContent = formatTime(dur);
        }
        els.progress.disabled = !caps.seek;
        els.progress.setAttribute('aria-disabled', caps.seek ? 'false' : 'true');
    }

    function showEmpty(entry, title, message, action) {
        const els = entry.els;
        els.nowPlaying.hidden = true;
        els.empty.hidden = false;
        els.emptyTitle.textContent = title;
        els.emptyMsg.textContent = message;
        els.emptyIcon.textContent = '';
        els.emptyActions.innerHTML = '';
        if (action === 'connect') {
            // Disconnected account providers share one primary login action.
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn-primary';
            btn.textContent = 'Start Qobuz Login';
            btn.addEventListener('click', () => {
                if (typeof api.openQobuzLogin === 'function') api.openQobuzLogin();
            });
            els.emptyActions.appendChild(btn);
        }
    }

    function providerUnavailableMessage(providerId) {
        if (providerId === 'spotify') return 'playerctl is not installed. Install it to control Spotify.';
        if (providerId === 'qobuz') return 'qbzd is not running. Start it to control Qobuz.';
        return 'This provider is not available.';
    }

    function notPlayingContent(providerId, data) {
        // spotifyd 0.4.x hides its MPRIS interface without an active Connect
        // session; the backend flags standby so an idle daemon reads as
        // "ready" instead of "not running". qbzd reports the same through its
        // session state.
        if (providerId === 'spotify' && data && data.spotifyd_standby) {
            return {
                title: 'Ready for Spotify Connect.',
                message: '',
            };
        }
        if (providerId === 'qobuz' && data && data.qbzd_standby) {
            return {
                title: 'Ready for Qobuz Connect.',
                message: '',
            };
        }
        if (providerId === 'spotify' && data && data.connected === true) {
            return { title: 'Nothing is playing.', message: '' };
        }
        // Signed-in qbzd with its Connect renderer up and nothing loaded is
        // the ready state (after install/sign-in); the fork backend exposes
        // no device selection, so qbzd_standby never reaches this path.
        if (providerId === 'qobuz' && data && data.connected === true) {
            return { title: 'Ready for Qobuz Connect.', message: 'Start a track from the Qobuz app.' };
        }
        // Runtime-down state class (C): keep the provider-identifying
        // "not running" wording and add the same start guidance qbzd offers.
        if (providerId === 'spotify') return { title: 'Spotify is not running.', message: 'Start it to use Spotify Connect.' };
        if (providerId === 'qobuz') return { title: 'Nothing is playing. Start a track from the Qobuz app.', message: '' };
        return { title: 'Nothing is playing.', message: '' };
    }

    function connectMessage(providerId) {
        if (providerId === 'qobuz') return 'Sign in to Qobuz to start playback.';
        return 'Connect your TIDAL account to browse and play music.';
    }

    // Queue continuation line ("3/28 · Next: Survival of the Fittest"), purely
    // data-driven from the normalized provider status (queue_len/queue_index/
    // next_track). A single-track queue renders nothing.
    function formatQueueInfo(data) {
        const total = Number(data.queue_len || 0);
        if (total <= 1) return '';
        const index = Number(data.queue_index || 1);
        const parts = [index + '/' + total];
        const next = data.next_track;
        if (next && next.title) parts.push('Next: ' + next.title);
        return parts.join(' · ');
    }

    // Provider queue starts (tab transport) are decided by the injected app.js
    // maybeShowStreamingQueueCue, which owns the session-track bookkeeping and
    // reuses showStreamingQueueStarted -> showNowPlayingCue. Deliberately no
    // local copy here: two implementations of the same state machine drift.

    // -----------------------------------------------------------------------
    // Now-playing transport wiring
    // -----------------------------------------------------------------------
    function wireNowPlayingTransport(providerId, entry) {
        const els = entry.els;
        els.toggle.addEventListener('click', () => transportCommand(providerId, 'toggle'));
        els.prev.addEventListener('click', () => transportCommand(providerId, 'previous'));
        els.next.addEventListener('click', () => transportCommand(providerId, 'next'));
        els.shuffle.addEventListener('click', () => transportCommand(providerId, 'shuffle'));
        els.loop.addEventListener('click', () => transportCommand(providerId, 'loop'));

        els.progress.addEventListener('mousedown', () => { entry.seeking = true; });
        els.progress.addEventListener('touchstart', () => { entry.seeking = true; }, { passive: true });
        const commitSeek = () => {
            entry.seeking = false;
            const data = state.lastData[providerId] || {};
            const dur = Number(data.duration || 0);
            if (!dur) return;
            const pos = (Number(els.progress.value) / 1000) * dur;
            transportCommand(providerId, 'seek', { position: pos });
        };
        els.progress.addEventListener('mouseup', commitSeek);
        els.progress.addEventListener('touchend', commitSeek);
        els.progress.addEventListener('change', commitSeek);
    }

    async function transportCommand(providerId, action, extra) {
        const adapter = TRANSPORT[providerId];
        if (!adapter) return;
        try {
            if (adapter.kind === 'app') {
                // Spotify transport stays in app.js (footer takeover + poll).
                if (action === 'seek') {
                    if (typeof api.spotifySeek === 'function') api.spotifySeek(extra?.position || 0);
                } else {
                    if (typeof api.spotifyCommand === 'function') api.spotifyCommand(action);
                }
                return;
            }
            await remoteTransportCommand(adapter, action, extra);
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    async function remoteTransportCommand(adapter, action, extra) {
        const prev = state.lastData.qobuz && typeof state.lastData.qobuz === 'object'
            ? { ...state.lastData.qobuz }
            : null;
        const mapped = adapter.action[action] || action;
        let resp;
        if (action === 'seek') {
            resp = await fetch(adapter.base + '/seek', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ position: extra?.position || 0 }),
            });
        } else {
            resp = await fetch(adapter.base + '/' + mapped, { method: 'POST' });
        }
        if (!resp.ok) throw new Error(await errorDetail(resp));
        // Queue starts (play/toggle into Playing with a new track/queue) reuse
        // the shared now-playing cue with the real Qobuz metadata and queue
        // count; later poll refreshes stay silent via the last-playing key.
        if (action === 'play' || action === 'toggle') {
            try {
                const data = await (typeof resp.clone === 'function'
                    ? resp.clone().json().catch(() => null)
                    : resp.json().catch(() => null));
                if (data) {
                    maybeShowStreamingQueueCue('qobuz', prev, data);
                }
            } catch (_cueError) {
                // Cue must never break transport.
            }
        }
        // Remote state settles asynchronously; refresh once and shortly after.
        void refreshProvider('qobuz');
    }

    // -----------------------------------------------------------------------
    // Status refresh / polling
    // -----------------------------------------------------------------------
    async function refreshProvider(providerId) {
        const serial = (state.refreshSerials[providerId] || 0) + 1;
        state.refreshSerials[providerId] = serial;
        try {
            const resp = await fetch('/api/streaming/' + providerId + '/status');
            if (!resp.ok) return;
            const data = await resp.json();
            if (serial <= (state.appliedSerials[providerId] || 0)) return;
            state.appliedSerials[providerId] = serial;
            renderProviderData(providerId, data);
        } catch (err) {
            // ignore transient fetch failures; the next poll retries
        }
    }

    function refreshTidalStatus() {
        return refreshProvider('tidal');
    }

    function startPoll(providerId, intervalMs) {
        stopPoll(providerId);
        void refreshProvider(providerId);
        state.polls[providerId] = setInterval(() => {
            if (document.hidden) return;
            if (window.__visibleTab !== providerId) { stopPoll(providerId); return; }
            void refreshProvider(providerId);
        }, intervalMs);
    }

    function stopPoll(providerId) {
        if (state.polls[providerId]) {
            clearInterval(state.polls[providerId]);
            delete state.polls[providerId];
        }
    }

    function stopAllPolls() {
        Object.keys(state.polls).forEach(stopPoll);
    }

    // -----------------------------------------------------------------------
    // Providers registry -> tab visibility + initial render
    // -----------------------------------------------------------------------
    async function loadProviders() {
        try {
            const resp = await fetch('/api/streaming/providers/discovery');
            if (!resp.ok) return;
            const payload = await resp.json();
            const list = payload.providers || [];
            for (const descriptor of list) {
                state.enabled[descriptor.id] = descriptor.enabled !== false;
                state.descriptors[descriptor.id] = descriptor;
                const entry = state.providers[descriptor.id];
                if (entry) {
                    entry.descriptor = descriptor;
                    entry.providerLabel = () => descriptor.name || descriptor.id;
                    // Show the tab only for enabled, implemented, installed providers.
                    const pseudo = { installed: descriptor.installed, available: descriptor.available, capabilities: descriptor.capabilities };
                    applyTabVisibility(descriptor.id, entry, pseudo);
                }
                // Status is read for every installed, enabled provider, with
                // or without a tab (TIDAL also feeds the Library).
                if (descriptor.installed && state.enabled[descriptor.id]) {
                    void refreshProvider(descriptor.id);
                }
                if (descriptor.id === 'tidal') notifyTidalStatus();
            }
            // Re-check enabled/installed flags periodically; cheap discovery
            // (no runtime probes) keeps the Settings surface authoritative.
            if (!state._enabledPoll) {
                state._enabledPoll = setInterval(() => {
                    if (!document.hidden) void refreshEnabledFlags();
                }, 10000);
            }
        } catch (err) {
            // ignore; tabs remain hidden until the next reconnect/resync
        }
    }

    // -----------------------------------------------------------------------
    // Error normalization (provider-neutral user-facing text)
    // -----------------------------------------------------------------------
    function errorDetail(resp) {
        // Format through the shared formatter so objects never become
        // "[object Object]". Message-less details fall back to '' (with a
        // diagnostic console.warn); the caller maps that to generic UI text.
        return resp.json().then((d) => formatTransitionErrorDetail(d?.detail, '')).catch(() => '');
    }

    function friendlyError(raw) {
        // The shared formatter already trims strings and extracts message +
        // stage from objects; message-less details fall back to '' (with a
        // diagnostic console.warn) so this never shows raw JSON or internal
        // fields. No second raw-string fallback: it would let whitespace-only
        // input through as a blank UI message.
        const text = formatTransitionErrorDetail(raw, '');
        const lower = (text || '').toLowerCase();
        if (lower.includes('not authenticated') || lower.includes('not eligible') || lower.includes('unauthorized')) {
            return 'You need to sign in to continue.';
        }
        if (lower.includes('unavailable') || lower.includes('not available') || lower.includes('not found')) {
            return 'This track is currently unavailable.';
        }
        if (lower.includes('rights') || lower.includes('restricted') || lower.includes('stream not available')) {
            return 'This track is currently unavailable.';
        }
        if (lower.includes('network') || lower.includes('timeout') || lower.includes('rate limit')) {
            return 'A network error occurred. Please try again.';
        }
        return text || 'Something went wrong.';
    }

    // The browser login runs in the shared provider login dialog (same flow
    // as Qobuz); app.js injects it as openTidalLogin.
    function openTidalLogin() {
        if ((state.lastData.tidal || {}).authenticated === true) {
            // Already connected (a stale list may offer Connect): just re-read.
            void refreshTidalStatus();
            return;
        }
        if (typeof api.openTidalLogin === 'function') api.openTidalLogin();
    }

    async function refreshTidalCatalog() {
        try {
            try { await loadTidalFavoriteIds(true); } catch (err) { /* keep refreshing below */ }
            await refreshTidalStatus();
            refreshActiveTidalView();
            showToast('TIDAL refreshed', 'success');
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    function refreshActiveTidalView() {
        if (isTidalDetailView()) renderTidalSurface();
    }

    const TIDAL_SEARCH_TYPES = ['albums', 'tracks', 'artists', 'playlists'];

    function renderSearchItem(type, item, queueIds) {
        const li = document.createElement('li');
        if (type === 'artists') rememberTidalArtist(item);
        if (type === 'tracks') {
            return buildTidalTrackRow(item, { queueIds: queueIds, showAlbum: true });
        } else {
            // Albums / Artists / Playlists render as tiles that reuse the
            // Library album-card look (large square cover + name + subtitle),
            // with the TIDAL heart in the cover corner.
            li.className = 'album-card';
            const title = type === 'albums' ? item.title : item.name;
            const sub = type === 'albums' ? (item.artist || '')
                : type === 'artists' ? 'Artist'
                : (item.track_count ? item.track_count + ' tracks' : 'Playlist');
            const fallbackText = title || sub || 'Tidal';
            // Render the per-type heart inline so favorite state stays the
            // canonical tracks/albums/artists/playlists hearts.
            const heart = type === 'albums'
                ? favoriteButtonHtml('albums', item.id)
                : type === 'artists'
                ? favoriteButtonHtml('artists', item.id)
                : favoriteButtonHtml('playlists', item.id);
            li.innerHTML =
                '<div class="album-art-wrap">' + tidalCardArt(item.art_url, fallbackText) + '</div>' +
                heart +
                '<div class="album-name">' + escapeHtml(title) + '</div>' +
                '<div class="album-artist">' + escapeHtml(sub) + '</div>';
            li.setAttribute('role', 'button');
            li.setAttribute('tabindex', '0');
            if (type === 'albums') li.addEventListener('click', () => openTidalAlbum(item.id, item.title, item.art_url));
            else if (type === 'artists') li.addEventListener('click', () => openTidalArtist(item.id, item.name, item.art_url));
            else li.addEventListener('click', () => openTidalPlaylist(item.id, item.name, item.art_url));
            const onKey = (event) => {
                if (event.target !== event.currentTarget) return;
                if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); li.click(); }
            };
            li.addEventListener('keydown', onKey);
            bindTidalFavoriteButtons(li);
        }
        return li;
    }

    // -- playlist-build selection (persistent + selection, shared save row) --
    // One selection language across every TIDAL track surface (search results,
    // favorites, album/playlist/artist details): the row + toggles the track
    // in the playlist-build selection. Playback and favorite actions never
    // touch it. The selection survives view navigation and is saved either as
    // a new TIDAL playlist (name) or appended to an existing one.
    function tidalTrackAddButtonHtml(trackId, isSelected) {
        return '<button class="streaming-add' + (isSelected ? ' is-active' : '') + '" data-streaming-add="' + escapeHtml(trackId) + '" type="button"' +
            ' aria-pressed="' + (isSelected ? 'true' : 'false') + '"' +
            ' aria-label="' + (isSelected ? 'Remove track from playlist selection' : 'Add track to playlist selection') + '"' +
            ' data-tooltip="' + (isSelected ? 'Remove from selection' : 'Add to selection') + '">' + (isSelected ? '✓' : '+') + '</button>';
    }

    function toggleTidalPlaylistTrack(event, trackId) {
        event.stopPropagation();
        if (state.tidal.selectedTrackIds.has(trackId)) state.tidal.selectedTrackIds.delete(trackId);
        else state.tidal.selectedTrackIds.add(trackId);
        syncTidalTrackSelection();
    }

    function syncTidalTrackSelection() {
        document.querySelectorAll('.streaming-add[data-streaming-add]').forEach((btn) => {
            const active = state.tidal.selectedTrackIds.has(btn.dataset.streamingAdd);
            btn.classList.toggle('is-active', active);
            btn.textContent = active ? '✓' : '+';
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.setAttribute('aria-label', active ? 'Remove track from playlist selection' : 'Add track to playlist selection');
            btn.setAttribute('data-tooltip', active ? 'Remove from selection' : 'Add to selection');
        });
        document.querySelectorAll('.streaming-result[data-track-id]').forEach((row) => {
            row.classList.toggle('is-selected', state.tidal.selectedTrackIds.has(row.dataset.trackId));
        });
        dispatchTidalEvent('fxroute:tidal-selection', { count: state.tidal.selectedTrackIds.size });
    }

    function clearTidalPlaylistSelection() {
        state.tidal.selectedTrackIds.clear();
        syncTidalTrackSelection();
    }

    // Create a TIDAL playlist from the + selection (Library save row).
    async function createTidalPlaylist(name, trackIds) {
        const resp = await fetch('/api/streaming/tidal/playlists/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to save TIDAL playlist'));
        clearTidalPlaylistSelection();
        // Refresh favorite ids so the new playlist heart renders active
        // immediately; the playlists reload tells the Library.
        void loadTidalFavoriteIds(true).then(() => reloadTidalPlaylists());
        return data;
    }

    // Append the + selection to an existing TIDAL playlist.
    async function addTracksToTidalPlaylist(playlistId, trackIds) {
        const resp = await fetch('/api/streaming/tidal/playlists/' + encodeURIComponent(playlistId) + '/tracks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to add tracks to TIDAL playlist'));
        clearTidalPlaylistSelection();
        void reloadTidalPlaylists();
        return data;
    }

    async function reloadTidalPlaylists() {
        try {
            const resp = await fetch('/api/streaming/tidal/playlists');
            if (!resp.ok) return;
            const items = await resp.json();
            state.tidal.lastItems.playlists = Array.isArray(items) ? items : [];
            notifyTidalLibrary('playlists');
        } catch (e) { /* keep the already-visible playlists */ }
    }

    function tidalCardArt(url, fallbackText) {
        const fallback = tidalArtFallback(fallbackText || 'Tidal');
        if (!url) return '<img class="album-art" src="' + fallback + '" alt="" loading="lazy" />';
        return '<img class="album-art" src="' + escapeHtml(url) + '" alt="" loading="lazy" ' +
            'onerror="this.onerror=null;this.src=\'' + fallback + '\'" />';
    }

    function tidalArtFallback() {
        return artworkPlaceholderUrl();
    }

    function tidalImageFallbackUrl(url) {
        const value = String(url || '');
        return /\/480x480\.jpg$/i.test(value)
            ? value.replace(/\/480x480\.jpg$/i, '/320x320.jpg')
            : '';
    }

    function coverImg(url, loading = 'lazy') {
        const placeholder = escapeHtml(artworkPlaceholderUrl());
        if (!url) return '<img src="' + placeholder + '" alt=""' +
            (loading ? ' loading="' + escapeHtml(loading) + '"' : '') + ' decoding="async" />';
        // Some valid TIDAL picture ids reject only the 480px rendition. Retry
        // the smaller rendition once, then fall back to the neutral tile.
        const fallbackUrl = tidalImageFallbackUrl(url);
        const fallbackAttr = fallbackUrl
            ? ' data-fallback-src="' + escapeHtml(fallbackUrl) + '"'
            : '';
        const errorAttr = fallbackUrl
            ? ' onerror="if (this.dataset.fallbackSrc && this.src !== this.dataset.fallbackSrc) { this.src = this.dataset.fallbackSrc; } else { this.onerror=null; this.src=\'' + placeholder + '\'; }"'
            : ' onerror="this.onerror=null;this.src=\'' + placeholder + '\'"';
        const loadingAttr = loading ? ' loading="' + escapeHtml(loading) + '"' : '';
        return '<img src="' + escapeHtml(url) + '" alt=""' + loadingAttr + fallbackAttr + ' decoding="async"' + errorAttr + ' />';
    }

    function tidalTrackThumbHtml(url) {
        // Reuse the shared .track-thumb container; coverImg handles missing/
        // failed art so the row falls back to the neutral note glyph.
        return '<div class="track-thumb" aria-hidden="true">' + coverImg(url) + '</div>';
    }

    // -- browse cache (last successful library state per account) -------------
    function tidalUserId() {
        const user = (state.lastData.tidal || {}).user || {};
        return user.id != null ? String(user.id) : '';
    }

    // A different TIDAL account must never see another account's cached
    // library: reset the in-memory cache/heart state when the account changes.
    // A logout (empty user id) clears the previous account's in-memory state
    // the same way so no stale hearts/lists survive for the next login.
    function ensureTidalAccountState() {
        const userId = tidalUserId();
        if (state.tidal.cacheUser === userId) return;
        state.tidal.cache = null;
        state.tidal.snapshotPromise = null;
        state.tidal.lastItems = {};
        state.tidal.favoriteIds = { tracks: new Set(), albums: new Set(), artists: new Set(), playlists: new Set() };
        state.tidal.favoritesLoaded = false;
        state.tidal.favoriteIdsPromise = null;
        state.tidal.libraryLoadedAt = 0;
        state.tidal.cacheUser = userId;
        notifyFavoritesChanged();
    }

    // Load the server-side snapshot (fast local read, no TIDAL network) once
    // per account. Adopting it populates the last-known favorite ids (hearts)
    // and per-category payloads so the first render is instant.
    function loadTidalSnapshot() {
        ensureTidalAccountState();
        const userId = tidalUserId();
        if (!userId || state.tidal.cacheUser !== userId) return Promise.resolve(null);
        if (state.tidal.cache) return Promise.resolve(state.tidal.cache);
        if (state.tidal.snapshotPromise) return state.tidal.snapshotPromise;
        state.tidal.snapshotPromise = (async () => {
            try {
                const resp = await fetch('/api/streaming/tidal/library/snapshot?user=' + encodeURIComponent(userId));
                if (!resp.ok) return null;
                const data = await resp.json();
                if (!data || String(data.user_id || '') !== userId || !data.ids) {
                    state.tidal.cache = null;
                    return null;
                }
                state.tidal.cache = data;
                ['tracks', 'albums', 'artists', 'playlists'].forEach((kind) => {
                    if (Array.isArray(data[kind])) state.tidal.lastItems[kind] = data[kind];
                });
                state.tidal.favoriteIds = {
                    tracks: new Set((data.ids.tracks || []).map(String)),
                    albums: new Set((data.ids.albums || []).map(String)),
                    artists: new Set((data.ids.artists || []).map(String)),
                    playlists: new Set((data.ids.playlists || []).map(String)),
                };
                state.tidal.favoritesLoaded = true;
                notifyFavoritesChanged();
                return data;
            } catch (err) {
                return null;
            }
        })();
        return state.tidal.snapshotPromise;
    }

    // -- favorites (authoritative TIDAL state; no FXRoute shadow) -------------
    function notifyFavoritesChanged() {
        if (typeof window.dispatchEvent !== 'function') return;
        window.dispatchEvent(new CustomEvent('fxroute:tidal-favorites', { detail: state.tidal.favoriteIds }));
    }

    function tidalFavoritesReady() {
        return state.tidal.favoritesLoaded === true;
    }

    // Ensure the canonical favorite ids are loaded, fetching from the backend
    // once (twice when a stale load needs forcing). Resolves true when the
    // ids are available, false when the request failed; never throws.
    function ensureTidalFavoritesLoaded(force) {
        const existing = state.tidal.favoriteIdsPromise;
        if (!force && existing && state.tidal.favoritesLoaded) return Promise.resolve(true);
        const promise = (async () => {
            try {
                await loadTidalFavoriteIds(true);
                return true;
            } catch (e) {
                return false;
            }
        })();
        if (!state.tidal.favoritesLoaded) state.tidal.favoriteIdsPromise = promise;
        return promise;
    }

    function loadTidalFavoriteIds(force) {
        // Last-known ids (snapshot or earlier load) serve without network;
        // forced calls always re-read the authoritative state.
        if (!force && state.tidal.favoritesLoaded) {
            return Promise.resolve(state.tidal.favoriteIds);
        }
        if (!state.tidal.favoriteIdsPromise || force) {
            state.tidal.favoriteIdsPromise = (async () => {
                const resp = await fetch('/api/streaming/tidal/favorites/ids');
                if (!resp.ok) throw new Error(await errorDetail(resp));
                const data = await resp.json();
                state.tidal.favoriteIds = {
                    tracks: new Set((data.tracks || []).map(String)),
                    albums: new Set((data.albums || []).map(String)),
                    artists: new Set((data.artists || []).map(String)),
                    playlists: new Set((data.playlists || []).map(String)),
                };
                state.tidal.favoritesLoaded = true;
                notifyFavoritesChanged();
                return state.tidal.favoriteIds;
            })();
        }
        return state.tidal.favoriteIdsPromise;
    }

    function isTidalFavorite(type, id) {
        const set = state.tidal.favoriteIds[type];
        return !!(set && set.has(String(id)));
    }

    function tidalQualityLabel(quality) {
        const q = String(quality || '').toUpperCase();
        if (q === 'HI_RES_LOSSLESS' || q === 'HI_RES') return 'Hi-Res Lossless';
        if (q === 'LOSSLESS') return 'Lossless';
        if (q === 'HIGH') return 'High';
        if (q === 'LOW') return 'Low';
        return q;
    }

    function favoriteButtonHtml(type, id, className) {
        // Detail track rows pass 'track-fav' so they share the library row
        // favorite visual; other lists keep the .streaming-fav heart.
        const cls = className || 'streaming-fav';
        const active = isTidalFavorite(type, id);
        return '<button type="button" class="' + cls + (active ? ' is-active' : '') + '" ' +
            'data-fav-type="' + type + '" data-fav-id="' + escapeHtml(id) + '" ' +
            'aria-pressed="' + (active ? 'true' : 'false') + '" ' +
            'aria-label="' + (active ? 'Remove from favorites' : 'Add to favorites') + '" ' +
            'data-tooltip="' + (active ? 'Remove from favorites' : 'Add to favorites') + '">' +
            favoriteHeartSvg() + '</button>';
    }

    function bindTidalFavoriteButtons(container) {
        container.querySelectorAll('.streaming-fav, .track-fav').forEach((btn) => {
            btn.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleTidalFavorite(btn.dataset.favType, btn.dataset.favId);
            });
        });
    }

    function syncTidalFavoriteButtons(type, idStr) {
        const active = state.tidal.favoriteIds[type].has(idStr);
        document.querySelectorAll('.streaming-fav[data-fav-type="' + type + '"][data-fav-id="' + idStr + '"], .track-fav[data-fav-type="' + type + '"][data-fav-id="' + idStr + '"]').forEach((btn) => {
            btn.classList.toggle('is-active', active);
            btn.innerHTML = favoriteHeartSvg();
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.setAttribute('aria-label', active ? 'Remove from favorites' : 'Add to favorites');
            btn.setAttribute('data-tooltip', active ? 'Remove from favorites' : 'Add to favorites');
        });
    }

    // Detail headers (album / artist / playlist) use the same heart favorite
    // as the track rows, sharing the same authoritative favorites state.
    function favoriteDetailHtml(type) {
        const idStr = String(state.tidal.detailId || '');
        const active = isTidalFavorite(type, idStr);
        return '<button type="button" class="album-favorite-toggle' + (active ? ' active' : '') + '" ' +
            'data-fav-type="' + type + '" data-fav-id="' + escapeHtml(idStr) + '" ' +
            'aria-pressed="' + (active ? 'true' : 'false') + '" ' +
            'aria-label="' + (active ? 'Remove from favorites' : 'Add to favorites') + '" ' +
            'data-tooltip="' + (active ? 'Remove from favorites' : 'Add to favorites') + '">' +
            favoriteHeartSvg() + '</button>';
    }

    function bindFavoriteDetailButtons(container) {
        container.querySelectorAll('.album-favorite-toggle[data-fav-type]').forEach((btn) => {
            btn.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleTidalFavorite(btn.dataset.favType, btn.dataset.favId);
            });
        });
    }

    function syncFavoriteDetailButtons(type, idStr) {
        const active = state.tidal.favoriteIds[type].has(idStr);
        document.querySelectorAll('.album-favorite-toggle[data-fav-type="' + type + '"][data-fav-id="' + idStr + '"]').forEach((btn) => {
            btn.classList.toggle('active', active);
            btn.innerHTML = favoriteHeartSvg();
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.setAttribute('aria-label', active ? 'Remove from favorites' : 'Add to favorites');
            btn.setAttribute('data-tooltip', active ? 'Remove from favorites' : 'Add to favorites');
        });
    }

    async function toggleTidalFavorite(type, id) {
        const idStr = String(id);
        const current = isTidalFavorite(type, idStr);
        const next = !current;
        try {
            const resp = await fetch('/api/streaming/tidal/' + type + '/' + encodeURIComponent(idStr) + '/favorite', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ favorite: next }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(formatTransitionErrorDetail(data.detail, 'Failed to update favorite'));
            if (data.favorite) state.tidal.favoriteIds[type].add(idStr);
            else state.tidal.favoriteIds[type].delete(idStr);
            syncTidalFavoriteButtons(type, idStr);
            syncFavoriteDetailButtons(type, idStr);
            notifyFavoritesChanged();
            showToast(data.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
            // A newly favorited item joins the Library grids; a removed one
            // already left through the favorites event above.
            if (data.favorite && state.tidal.libraryLoadedAt && TIDAL_LIBRARY_KINDS.includes(type)) {
                void refreshTidalLibraryCollection(type);
            }
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    // -- album / playlist detail ----------------------------------------------
    // Detail views replace the browse content in place and share the window
    // scroll (no inner scroll container), so every detail open resets it to
    // the top instead of inheriting the browse position.
    function openTidalDetail(view, id, title, artUrl, options) {
        // Push the full current detail state so Back returns exactly through
        // nested details (browse -> artist -> album) with the previous view's
        // own id/title/art restored, instead of jumping straight to browse.
        // Callers pass { replace: true } to swap the current detail without
        // growing history (used by Discover Similar navigation).
        const replace = Boolean(options && options.replace);
        state.tidal.detailRequestId += 1;
        if (!replace) {
            state.tidal.viewStack.push({
                view: state.tidal.view,
                detailId: state.tidal.detailId,
                detailTitle: state.tidal.detailTitle,
                detailArt: state.tidal.detailArt,
            });
        }
        state.tidal.view = view;
        state.tidal.detailId = id;
        state.tidal.detailTitle = title;
        state.tidal.detailArt = artUrl || '';
        renderTidalSurface();
        window.scrollTo(0, 0);
    }

    function openTidalAlbum(id, title, artUrl) {
        openTidalDetail('album', id, title, artUrl);
    }

    function openTidalPlaylist(id, title, artUrl) {
        openTidalDetail('playlist', id, title, artUrl);
    }

    function openTidalArtist(id, name, artUrl, options) {
        rememberTidalArtist({ id: id, name: name, art_url: artUrl });
        openTidalDetail('artist', id, name, artUrl, options);
    }

    function closeTidalDetail() {
        state.tidal.detailRequestId += 1;
        const previous = state.tidal.viewStack.pop();
        state.tidal.view = previous ? previous.view : null;
        state.tidal.detailId = previous ? previous.detailId : null;
        state.tidal.detailTitle = previous ? previous.detailTitle : '';
        state.tidal.detailArt = previous ? previous.detailArt || '' : '';
        renderTidalSurface();
    }

    // -- Library detail host ------------------------------------------------------
    // Album/playlist/artist details render into the Library host container and
    // the host is told when the detail chain opens or closes, so it can swap
    // its grids for the detail and back.
    function renderTidalSurface() {
        renderTidalLibraryDetail();
    }

    function renderTidalLibraryDetail() {
        const host = libraryHost;
        if (!host || !host.container) return;
        const content = host.container;
        if (!isTidalDetailView()) {
            // The host first takes back its own nodes (e.g. a docked save
            // row) before the detail markup is dropped.
            if (typeof host.onClose === 'function') host.onClose();
            content.innerHTML = '';
            return;
        }
        if (typeof host.onOpen === 'function') host.onOpen();
        if (state.tidal.view === 'album') renderTidalAlbum(content);
        else if (state.tidal.view === 'playlist') renderTidalPlaylist(content);
        else renderTidalArtist(content);
        if (typeof host.onRender === 'function') host.onRender(content);
    }

    function restoreTidalDetail(snapshot) {
        state.tidal.view = snapshot ? snapshot.view : null;
        state.tidal.viewStack = snapshot ? snapshot.viewStack.slice() : [];
        state.tidal.detailId = snapshot ? snapshot.detailId : null;
        state.tidal.detailTitle = snapshot ? snapshot.detailTitle : '';
        state.tidal.detailArt = snapshot ? snapshot.detailArt || '' : '';
    }

    // The Library leaves a TIDAL detail chain through its own navigation
    // (view tabs, search): drop the chain without re-rendering the Library.
    function closeTidalLibraryDetail() {
        state.tidal.detailRequestId += 1;
        restoreTidalDetail(null);
        if (libraryHost && libraryHost.container) libraryHost.container.innerHTML = '';
    }

    function detailCoverHtml(extraClass) {
        const art = state.tidal.detailArt || '';
        const cls = 'streaming-detail-cover detail-hero-cover' + (extraClass ? ' ' + extraClass : '');
        return '<div class="' + cls + '">' + coverImg(art, 'eager') + '</div>';
    }

    function detailBackdropHtml() {
        const art = state.tidal.detailArt || '';
        const hidden = art ? '' : ' hidden';
        return '<div class="detail-header-backdrop" aria-hidden="true"' + hidden + '>' +
            (art ? coverImg(art, 'eager') : '') + '</div>';
    }

    function syncDetailBackdrop(container, artUrl) {
        const backdrop = container && container.querySelector('.detail-header-backdrop');
        if (!backdrop) return;
        const art = String(artUrl || '');
        backdrop.hidden = !art;
        backdrop.innerHTML = art ? coverImg(art, 'eager') : '';
    }

    function renderTidalAlbum(content) {
        const requestId = ++state.tidal.detailRequestId;
        // Mirrors the library album detail: cover + title/artist/facts beside
        // it, star favorite in the title row, shared back button in the header
        // row, then the compact track list. No "Play album" button. The facts
        // line stays TIDAL-primary; MusicBrainz only adds release type, country,
        // label and genres when they are missing, and the artist about renders
        // directly through the shared library description component.
        content.innerHTML =
            '<div class="streaming-detail streaming-detail--hero tidal-detail">' +
                '<div class="streaming-detail-header detail-hero-header detail-hero-header--album tidal-detail-header">' +
                    detailBackdropHtml() +
                    detailCoverHtml('tidal-detail-cover') +
                    '<div class="streaming-detail-main detail-hero-meta tidal-detail-meta">' +
                        '<div class="tidal-detail-title-row detail-hero-title-row">' +
                            '<h3 class="streaming-detail-title tidal-detail-title detail-hero-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                            favoriteDetailHtml('albums') +
                        '</div>' +
                        '<p class="streaming-detail-artist tidal-detail-artist" id="tidal-album-artist"></p>' +
                        '<div class="streaming-detail-facts tidal-detail-facts" id="tidal-album-facts"></div>' +
                        '<div id="tidal-album-about"></div>' +
                    '</div>' +
                    '<button type="button" class="album-detail-back" id="tidal-detail-back">← Back</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results">' + contentState('loading', 'Loading…') + '</div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteDetailButtons(content);
        loadTidalAlbum(content, requestId);
    }

    function renderTidalAlbumMeta(content, meta, enrichment) {
        if (meta) {
            const titleEl = content.querySelector('.tidal-detail-title');
            if (titleEl) titleEl.textContent = meta.title || state.tidal.detailTitle;
            if (meta.art_url) {
                state.tidal.detailArt = meta.art_url;
                const cover = content.querySelector('.tidal-detail-cover');
                if (cover) cover.innerHTML = coverImg(meta.art_url, 'eager');
                syncDetailBackdrop(content, meta.art_url);
            }
            const artistEl = content.querySelector('#tidal-album-artist');
            if (artistEl) artistEl.textContent = meta.artist || '';
            const factsEl = content.querySelector('#tidal-album-facts');
            if (factsEl) factsEl.innerHTML = tidalAlbumFactsHtml(meta, enrichment);
            const about = enrichment && enrichment.artist && enrichment.artist.about;
            const aboutEl = content.querySelector('#tidal-album-about');
            if (aboutEl) {
                if (about && (about || '').trim()) {
                    aboutEl.innerHTML = aboutHtml('About this artist', about);
                } else {
                    aboutEl.innerHTML = '';
                }
            }
        }
        syncFavoriteDetailButtons('albums', state.tidal.detailId);
    }

    // TIDAL album facts: the operator's own year/quality/track count come
    // first; MusicBrainz release fields are appended only when the provider does
    // not expose them, so no metadata is shown twice and TIDAL values win.
    function tidalAlbumFactsHtml(meta, enrichment) {
        const lines = [];
        const supp = (enrichment && enrichment.supplement) || {};
        const primary = [
            meta.num_tracks != null ? (meta.num_tracks + (meta.num_tracks === 1 ? ' track' : ' tracks')) : '',
            supp.release_type,
            meta.year ? String(meta.year) : '',
            supp.country,
            tidalQualityLabel(meta.audio_quality),
        ].filter(Boolean);
        lines.push(primary.join(' · '));
        const genres = (supp.genres || []).slice(0, 3).filter(Boolean);
        lines.push([
            supp.label ? 'Label: ' + supp.label : '',
            genres.length ? 'Genre: ' + genres.join(' / ') : '',
        ]);
        return factsHtml(lines);
    }

    async function loadTidalAlbum(content, requestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const [metaResp, tracksResp] = await Promise.all([
                fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId)),
                fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId) + '/tracks'),
            ]);
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
            const meta = metaResp.ok ? await metaResp.json().catch(() => null) : null;
            if (!tracksResp.ok) throw new Error(await errorDetail(tracksResp));
            const items = await tracksResp.json();
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            renderTidalAlbumMeta(content, meta, (meta && meta.enrichment) || null);
            const similar = (meta && meta.enrichment && meta.enrichment.similar) || [];
            if (Array.isArray(items) && items.length) renderDetailTracks(results, items, null);
            else results.innerHTML = '';
            const hasSimilar = renderSimilarArtists(results, similar);
            if (hasSimilar) void hydrateSimilarArtistImages(results, similar, () => requestId === state.tidal.detailRequestId && state.tidal.view === 'album');
            if ((!Array.isArray(items) || !items.length) && !hasSimilar) {
                results.innerHTML = contentState('empty', 'No tracks or similar artists available.');
            }
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            results.innerHTML = contentState('error', friendlyError(err?.message || err));
        }
    }

    function renderTidalArtist(content) {
        const requestId = ++state.tidal.detailRequestId;
        // Library-mirroring detail header (cover + name, follow star, back
        // button), then the About text directly visible under the facts line —
        // compact inside the hero, bounded with a subtle More/Less toggle for
        // long bios — followed by Top Tracks, Albums and Discover Similar.
        content.innerHTML =
            '<div class="streaming-detail streaming-detail--hero tidal-detail">' +
                '<div class="streaming-detail-header detail-hero-header tidal-detail-header">' +
                    detailBackdropHtml() +
                    detailCoverHtml('tidal-detail-cover') +
                    '<div class="streaming-detail-main detail-hero-meta tidal-detail-meta">' +
                        '<div class="tidal-detail-title-row detail-hero-title-row">' +
                            '<h3 class="streaming-detail-title tidal-detail-title detail-hero-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                            favoriteDetailHtml('artists') +
                        '</div>' +
                        '<p class="streaming-detail-artist tidal-detail-artist" id="tidal-artist-facts"></p>' +
                        '<div class="album-detail-about tidal-artist-about" id="tidal-artist-about" hidden></div>' +
                    '</div>' +
                    '<button type="button" class="album-detail-back" id="tidal-detail-back">← Back</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results">' + contentState('loading', 'Loading…') + '</div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteDetailButtons(content);
        loadTidalArtist(content, requestId);
    }

    // Short bios render fully in the hero without any toggle; only longer
    // texts start clamped (the clamp budget matches the hero text column, so
    // the available hero space is used before truncating).
    const ARTIST_ABOUT_INLINE_LIMIT = 500;

    // Directly-visible artist about, compact in the hero. Long bios are
    // clamped with a subtle More/Less toggle instead of an accordion — but
    // the toggle only stays when the text is actually truncated: after
    // layout, an untruncated clamp loses the toggle again, so short bios
    // (2Pac, The Prodigy, …) show fully without More/Less.
    function renderArtistAbout(element, about) {
        if (!element) return;
        const text = (about || '').trim();
        if (!text) {
            element.hidden = true;
            element.innerHTML = '';
            return;
        }
        element.hidden = false;
        if (text.length <= ARTIST_ABOUT_INLINE_LIMIT) {
            element.innerHTML =
                '<p class="streaming-about-text">' + escapeHtml(text) + '</p>';
            return;
        }
        element.innerHTML =
            '<p class="streaming-about-text is-clamped">' + escapeHtml(text) + '</p>' +
            '<button type="button" class="about-more-toggle" aria-expanded="false">More</button>';
        const bio = element.querySelector('.streaming-about-text');
        const toggle = element.querySelector('.about-more-toggle');
        if (!bio || !toggle) return;
        toggle.addEventListener('click', () => {
            const clamped = bio.classList.toggle('is-clamped');
            toggle.setAttribute('aria-expanded', clamped ? 'false' : 'true');
            toggle.textContent = clamped ? 'More' : 'Less';
        });
        // Drop the toggle again when the clamped text is not actually
        // truncated (the bio fits the hero budget). Runs after layout so the
        // heights reflect the clamped render; contexts without layout keep
        // the toggle via the length gate above.
        const dropToggleWhenUntruncated = () => {
            try {
                if (!bio.isConnected) return;
                if (bio.scrollHeight > bio.clientHeight + 1) return;
            } catch (err) { return; }
            bio.classList.remove('is-clamped');
            toggle.remove();
        };
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
            window.requestAnimationFrame(dropToggleWhenUntruncated);
        } else {
            dropToggleWhenUntruncated();
        }
    }

    // Top Tracks and Albums of a TIDAL artist payload; shared by the TIDAL
    // artist detail and the "On TIDAL" section of a local artist page.
    // Returns whether anything was rendered.
    function renderTidalArtistBody(results, data) {
        const tracks = Array.isArray(data.top_tracks) ? data.top_tracks : [];
        const albums = Array.isArray(data.albums) ? data.albums : [];
        if (tracks.length) {
            const heading = document.createElement('h4');
            heading.className = 'streaming-results-heading';
            heading.textContent = 'Top Tracks';
            results.appendChild(heading);
            // renderDetailTracks replaces the content of its container, so
            // the rows get their own host and the heading stays.
            const trackHost = document.createElement('div');
            results.appendChild(trackHost);
            renderDetailTracks(trackHost, tracks);
        }
        if (albums.length) {
            const heading = document.createElement('h4');
            heading.className = 'streaming-results-heading';
            heading.textContent = 'Albums';
            results.appendChild(heading);
            const list = document.createElement('ul');
            list.className = 'tidal-tiles';
            albums.forEach((item) => list.appendChild(renderSearchItem('albums', item, [])));
            results.appendChild(list);
        }
        return tracks.length > 0 || albums.length > 0;
    }

    async function fetchTidalArtist(id) {
        const resp = await fetch('/api/streaming/tidal/artists/' + encodeURIComponent(id));
        if (!resp.ok) throw new Error(await errorDetail(resp));
        const data = await resp.json();
        rememberTidalArtist(data);
        return data;
    }

    async function loadTidalArtist(content, requestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const data = await fetchTidalArtist(state.tidal.detailId);
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'artist') return;
            if (data.art_url) {
                state.tidal.detailArt = data.art_url;
                const cover = content.querySelector('.tidal-detail-cover');
                if (cover) cover.innerHTML = coverImg(data.art_url, 'eager');
                syncDetailBackdrop(content, data.art_url);
            }
            const factsEl = content.querySelector('#tidal-artist-facts');
            if (factsEl) {
                const facts = [];
                if (Array.isArray(data.albums) && data.albums.length) {
                    facts.push(data.albums.length + (data.albums.length === 1 ? ' album' : ' albums'));
                }
                if (Array.isArray(data.top_tracks) && data.top_tracks.length) {
                    facts.push(data.top_tracks.length + (data.top_tracks.length === 1 ? ' top track' : ' top tracks'));
                }
                factsEl.textContent = facts.join(' · ');
            }
            renderArtistAbout(content.querySelector('#tidal-artist-about'), data.enrichment && data.enrichment.about);
            syncFavoriteDetailButtons('artists', state.tidal.detailId);
            const hasBody = renderTidalArtistBody(results, data);
            const similar = (data.enrichment && data.enrichment.similar) || [];
            const hasSimilar = renderSimilarArtists(results, similar);
            if (hasSimilar) void hydrateSimilarArtistImages(results, similar, () => requestId === state.tidal.detailRequestId && state.tidal.view === 'artist');
            if (!hasBody && !hasSimilar) {
                results.innerHTML = contentState('empty', 'No tracks or albums available.');
            }
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'artist') return;
            results.innerHTML = contentState('error', friendlyError(err?.message || err));
        }
    }

    // One Discover Similar card: cover tile (mapped artists reuse their stored
    // art URL without a new request; everything else starts with the shared
    // neutral placeholder) plus the artist name.
    function renderSimilarArtistItem(item, onOpen) {
        const open = typeof onOpen === 'function' ? onOpen : openTidalSimilarArtist;
        const li = document.createElement('li');
        li.className = 'streaming-similar-item';
        li.setAttribute('role', 'button');
        li.setAttribute('tabindex', '0');
        li.setAttribute('aria-label', 'Open artist ' + (item.artist || ''));
        li.innerHTML =
            '<div class="streaming-result-cover">' + coverImg(item.art_url, 'eager') + '</div>' +
            '<span class="streaming-similar-name">' + escapeHtml(item.artist || 'Unknown artist') + '</span>';
        li.addEventListener('click', () => open(item));
        li.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                open(item);
            }
        });
        return li;
    }

    // The one Discover Similar renderer: TIDAL details and the Library's
    // album/artist pages share it. onOpen replaces the default TIDAL
    // navigation for a card (the Library prefers its own artists).
    function renderSimilarArtists(container, items, onOpen) {
        const similar = Array.isArray(items)
            ? items.filter((item) => item && item.artist).slice(0, 6)
            : [];
        if (!similar.length) return false;
        const heading = document.createElement('h4');
        heading.className = 'streaming-results-heading';
        heading.textContent = 'Discover Similar';
        container.appendChild(heading);
        const list = document.createElement('ul');
        list.className = 'streaming-similar-grid';
        similar.forEach((item, index) => {
            const card = renderSimilarArtistItem(item, onOpen);
            card.dataset.similarIndex = String(index);
            list.appendChild(card);
        });
        container.appendChild(list);
        return true;
    }

    function rememberTidalArtist(item) {
        if (!item) return null;
        const id = String(item.id || item.provider_artist_id || '').trim();
        const name = String(item.name || item.artist || '').trim();
        if (!id || !name) return null;
        const match = {
            id: id,
            name: name,
            art_url: String(item.art_url || ''),
            ambiguous: Boolean(item.ambiguous),
        };
        const key = tidalNameKey(name);
        if (!key) return match;
        const existing = state.tidal.artistLookupCache.get(key);
        if (!existing) {
            state.tidal.artistLookupCache.set(key, match);
            return match;
        }
        if (!existing.art_url && match.art_url) existing.art_url = match.art_url;
        if (!existing.id && match.id) existing.id = match.id;
        return existing;
    }

    async function resolveTidalArtistMatch(name) {
        const key = tidalNameKey(name);
        if (!key) return null;
        const cached = state.tidal.artistLookupCache.get(key);
        if (cached) return cached;
        const pending = state.tidal.artistLookupPromises.get(key);
        if (pending) return pending;

        const promise = (async () => {
            const resp = await fetch('/api/streaming/tidal/search?q=' + encodeURIComponent(name) + '&types=artists&limit=10');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const data = await resp.json();
            const artists = Array.isArray(data.artists) ? data.artists : [];
            let matches = artists.filter((artist) => tidalNameKey(artist.name) === key);
            if (!matches.length && artists.length === 1) matches = artists;
            if (!matches.length) return null;
            const preferred = matches.find((artist) => artist && artist.art_url) || matches[0];
            return rememberTidalArtist({
                id: preferred.id,
                name: preferred.name || name,
                art_url: preferred.art_url || '',
                ambiguous: matches.length !== 1,
            });
        })();
        state.tidal.artistLookupPromises.set(key, promise);
        try {
            return await promise;
        } finally {
            if (state.tidal.artistLookupPromises.get(key) === promise) {
                state.tidal.artistLookupPromises.delete(key);
            }
        }
    }

    // Resolve missing covers independently so each card is patched as soon as
    // its own TIDAL search returns. The detail request guard prevents a late
    // response from modifying a different view.
    function hydrateSimilarArtistImages(container, items, isCurrent) {
        const similar = Array.isArray(items)
            ? items.filter((item) => item && item.artist).slice(0, 6)
            : [];
        const cards = container ? container.querySelectorAll('.streaming-similar-item') : [];
        similar.forEach((item, index) => {
            if (item.art_url) {
                rememberTidalArtist({
                    id: item.provider_artist_id,
                    name: item.artist,
                    art_url: item.art_url,
                });
                return;
            }
            void resolveTidalArtistMatch(item.artist).then((match) => {
                if (!match || !match.art_url) return;
                item.provider_artist_id = item.provider_artist_id || match.id;
                item.art_url = match.art_url;
                if (!isCurrent()) return;
                const card = cards[index];
                if (!card || !card.isConnected) return;
                const cover = card.querySelector('.streaming-result-cover');
                if (cover) cover.innerHTML = coverImg(match.art_url, 'eager');
            }).catch(() => {});
        });
    }

    // Similar-artist navigation. A cached/mapped provider artist id opens the
    // artist directly; otherwise exactly one normal TIDAL artist search by the
    // artist name runs, opening its single unique match or falling back to the
    // existing TIDAL search-result flow. Never a live per-keystroke search.
    function openTidalSimilarArtist(item) {
        // In the Library an artist that exists locally opens its own page.
        if (libraryHost && typeof libraryHost.openLocalArtist === 'function'
            && item && libraryHost.openLocalArtist(item.artist)) {
            return;
        }
        if (item && item.provider_artist_id) {
            openTidalSimilarArtistDetail(item.provider_artist_id, item.artist || '', item.art_url || '');
            return;
        }
        const name = (item && item.artist) || '';
        if (!name) return;
        void resolveTidalArtistByName(name, state.tidal.detailRequestId);
    }

    // Discover Similar is lateral navigation, not drill-down: any open detail
    // chain collapses back to the entry context, and the new artist replaces
    // the current detail on top of it. Back therefore always returns to the
    // entry context in one step (Albums -> album -> Similar -> artists ->
    // one Back -> Albums) instead of stepping through intermediate artists.
    function openTidalSimilarArtistDetail(id, name, artUrl) {
        if (state.tidal.viewStack.length > 1) {
            state.tidal.viewStack = state.tidal.viewStack.slice(0, 1);
        }
        openTidalArtist(id, name, artUrl, { replace: true });
    }

    async function resolveTidalArtistByName(name, detailRequestId) {
        try {
            const match = await resolveTidalArtistMatch(name);
            if (detailRequestId !== state.tidal.detailRequestId || !isTidalDetailView()) return;
            if (match && !match.ambiguous) {
                openTidalSimilarArtistDetail(match.id, match.name || name, match.art_url || '');
                return;
            }
            throw new Error('no unique artist match');
        } catch (err) {
            if (detailRequestId !== state.tidal.detailRequestId || !isTidalDetailView()) return;
            if (canSearchInLibrary()) searchArtistInLibrary(name);
        }
    }

    function tidalNameKey(value) {
        return String(value == null ? '' : value).normalize('NFKC').toLowerCase()
            .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    }

    function canSearchInLibrary() {
        return !!(libraryHost && typeof libraryHost.search === 'function');
    }

    // The Library searches its own content and the TIDAL catalog for the name.
    function searchArtistInLibrary(name) {
        closeTidalLibraryDetail();
        libraryHost.search(name, 'artists');
    }

    function renderTidalPlaylist(content) {
        const requestId = ++state.tidal.detailRequestId;
        // Same library-mirroring layout as the album detail: no dedicated play
        // action (track click starts the queue), the track count is the facts
        // line, and the info area carries the description, the single-artist
        // enrichment about or a featuring line built from the actual artists.
        content.innerHTML =
            '<div class="streaming-detail streaming-detail--hero tidal-detail">' +
                '<div class="streaming-detail-header detail-hero-header tidal-detail-header">' +
                    detailBackdropHtml() +
                    detailCoverHtml('tidal-detail-cover') +
                    '<div class="streaming-detail-main detail-hero-meta tidal-detail-meta">' +
                        '<div class="tidal-detail-title-row detail-hero-title-row">' +
                            '<h3 class="streaming-detail-title tidal-detail-title detail-hero-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                            favoriteDetailHtml('playlists') +
                        '</div>' +
                        '<div class="streaming-detail-facts tidal-detail-facts" id="tidal-playlist-facts"></div>' +
                        '<div class="tidal-playlist-info" id="tidal-playlist-info"></div>' +
                    '</div>' +
                    '<button type="button" class="album-detail-back" id="tidal-detail-back">← Back</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results">' + contentState('loading', 'Loading…') + '</div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteDetailButtons(content);
        loadTidalPlaylistTracks(content, requestId);
    }

    async function loadTidalPlaylistTracks(content, requestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const [detailResp, tracksResp] = await Promise.all([
                fetch('/api/streaming/tidal/playlists/' + encodeURIComponent(state.tidal.detailId)),
                fetch('/api/streaming/tidal/playlists/' + encodeURIComponent(state.tidal.detailId) + '/tracks'),
            ]);
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
            const detail = detailResp.ok ? await detailResp.json().catch(() => null) : null;
            if (!tracksResp.ok) throw new Error(await errorDetail(tracksResp));
            const items = await tracksResp.json();
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'playlist') return;
            renderTidalPlaylistMeta(content, detail, items);
            syncFavoriteDetailButtons('playlists', state.tidal.detailId);
            renderDetailTracks(results, items);
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'playlist') return;
            results.innerHTML = contentState('error', friendlyError(err?.message || err));
        }
    }

    // Header info for a TIDAL playlist: the operator's own description first,
    // then the single-artist MusicBrainz about, then a short featuring line
    // built from the distinct artists actually present in the track list. No
    // artificial multi-artist biography is ever assembled.
    function renderTidalPlaylistMeta(content, detail, items) {
        const tracks = Array.isArray(items) ? items : [];
        const factsEl = content.querySelector('#tidal-playlist-facts');
        if (factsEl) {
            factsEl.textContent = tracks.length + ' track' + (tracks.length === 1 ? '' : 's');
        }
        const infoEl = content.querySelector('#tidal-playlist-info');
        if (!infoEl) return;
        const description = (detail && detail.description || '').trim();
        const about = (detail && detail.enrichment && detail.enrichment.available &&
            detail.enrichment.artist && detail.enrichment.artist.about || '').trim();
        if (description) {
            infoEl.innerHTML = '<p class="tidal-playlist-description">' + escapeHtml(description) + '</p>';
        } else if (about) {
            infoEl.innerHTML = aboutHtml('About this artist', about);
        } else {
            const line = tidalFeaturingLine(tracks);
            infoEl.innerHTML = line ? '<p class="tidal-playlist-description">' + escapeHtml(line) + '</p>' : '';
        }
    }

    function tidalFeaturingLine(tracks) {
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
        if (names.length < 2) return '';
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

    function renderDetailTracks(container, items) {
        container.innerHTML = '';
        if (!Array.isArray(items) || !items.length) {
            container.innerHTML = contentState('empty', 'No tracks.');
            return;
        }
        const ids = items.map((t) => String(t.id)).filter(Boolean);
        const list = document.createElement('ul');
        list.className = 'streaming-results-list';
        items.forEach((item, index) => {
            list.appendChild(buildTidalTrackRow(item, { index: index + 1, queueIds: ids, favoriteClass: 'track-fav' }));
        });
        container.appendChild(list);
    }

    // Display data of the TIDAL tracks shown so far, so a saved playlist can
    // list them later without TIDAL (see trackMeta). Bounded; oldest first out.
    const TIDAL_TRACK_META_MAX = 2000;
    const tidalTrackMeta = new Map();

    function rememberTidalTrack(item) {
        if (!item || item.id == null) return;
        const id = String(item.id);
        tidalTrackMeta.delete(id);
        tidalTrackMeta.set(id, {
            title: item.title || '',
            artist: item.artist || '',
            album: item.album || '',
            duration: Number(item.duration) > 0 ? Number(item.duration) : 0,
            art_url: item.art_url || '',
        });
        while (tidalTrackMeta.size > TIDAL_TRACK_META_MAX) {
            tidalTrackMeta.delete(tidalTrackMeta.keys().next().value);
        }
    }

    // One TIDAL track row for every surface (search results, favorites,
    // details, Library lists): shared row body, + selection, TIDAL heart.
    // A click plays the row inside its list (queueIds).
    function buildTidalTrackRow(item, options) {
        const opts = options || {};
        const queueIds = Array.isArray(opts.queueIds) ? opts.queueIds : [];
        const trackId = String(item.id);
        rememberTidalTrack(item);
        // A host list can own playback (a saved playlist plays its mixed queue).
        const playRow = typeof opts.onPlay === 'function'
            ? () => opts.onPlay(trackId)
            : () => playTidalTracks(queueIds, trackId);
        const isSelected = state.tidal.selectedTrackIds.has(trackId);
        const li = document.createElement(opts.tag || 'li');
        li.className = 'streaming-result' + (isSelected ? ' is-selected' : '');
        li.setAttribute('data-track-id', trackId);
        li.innerHTML = trackRowHtml({
            index: opts.index,
            title: escapeHtml(item.title),
            sub: escapeHtml(opts.sub != null ? opts.sub : (item.artist || '')),
            album: opts.showAlbum && item.album ? escapeHtml(item.album) : '',
            badge: opts.badge || '',
            thumb: tidalTrackThumbHtml(item.art_url),
            selectionButton: tidalTrackAddButtonHtml(trackId, isSelected),
            favoriteButton: favoriteButtonHtml('tracks', item.id, opts.favoriteClass),
            duration: formatTime(item.duration),
        });
        li.querySelector('.track-play').addEventListener('click', (event) => {
            event.stopPropagation();
            playRow();
        });
        const addBtn = li.querySelector('.streaming-add[data-streaming-add]');
        if (addBtn) addBtn.addEventListener('click', (event) => toggleTidalPlaylistTrack(event, trackId));
        li.addEventListener('click', (event) => {
            if (event.target && event.target.closest('.streaming-add, .streaming-fav, .track-fav')) return;
            playRow();
        });
        bindTidalFavoriteButtons(li);
        return li;
    }

    // -- play (native FXRoute queue owner) ------------------------------------
    async function playTidalTracks(trackIds, startId) {
        try {
            const resp = await fetch('/api/play', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                // Carry the current shuffle intent like library plays do, so a
                // fresh TIDAL queue does not silently clear an enabled shuffle.
                body: JSON.stringify({ source: 'tidal', track_id: startId, queue_track_ids: trackIds, shuffle: !!(state.lastData.tidal || {}).shuffle }),
            });
            const data = await resp.json().catch(() => null);
            if (!resp.ok) {
                showToast(friendlyError(data?.detail || 'Playback failed'), 'error');
                return;
            }
            // Same track-change cue as Local Library playLocal, with the real
            // TIDAL metadata and queue count from the play response. The shared
            // decision deduplicates the WebSocket playback frame that reports
            // the same start.
            try {
                const playedTrack = data?.playback?.current_track || null;
                const queueCount = Number(data?.playback?.queue?.count || (Array.isArray(trackIds) ? trackIds.length : 0) || 0);
                if (playedTrack) {
                    maybeShowNativeTrackCue(playedTrack, queueCount > 1 ? `Queue started · ${queueCount} tracks` : 'Now playing');
                }
            } catch (_cueError) {
                // Cue must never break playback.
            }
            // Commit the authoritative playback payload through the shared
            // native path so footer ownership (and the VU/peak gating derived
            // from it) follows the TIDAL start instead of a stale cached
            // commit. The WebSocket playback frame stays a redundant backup.
            // The commit helper lives in static/playback_core.js (single
            // owner); streaming.js only calls it at runtime, after app.js has
            // loaded, so the namespace lookup stays lazy here.
            try {
                const commitPlayback = (typeof window !== 'undefined' && window.FXRoutePlaybackCore?.applyNativePlayResponse)
                    || (typeof globalThis !== 'undefined' && globalThis.FXRoutePlaybackCore?.applyNativePlayResponse)
                    || null;
                if (data && data.playback && typeof commitPlayback === 'function') {
                    commitPlayback(data);
                }
            } catch (_commitError) {
                // The footer commit must never break playback.
            }
            void refreshTidalStatus();
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    // -----------------------------------------------------------------------
    // Library adapter: TIDAL data and rendering for the shared Library tab
    // -----------------------------------------------------------------------
    // Favorite collections the Library merges with local music.
    const TIDAL_LIBRARY_KINDS = ['albums', 'tracks', 'playlists', 'artists'];
    // First page size of a favorites listing; the rest loads behind it.
    const TIDAL_FAVORITES_PAGE = 50;
    // A Library visit re-reads the collections at most this often.
    const TIDAL_LIBRARY_REFRESH_MS = 60000;

    function dispatchTidalEvent(name, detail) {
        if (typeof window.dispatchEvent !== 'function' || typeof CustomEvent !== 'function') return;
        window.dispatchEvent(new CustomEvent(name, { detail: detail || {} }));
    }

    // The live status wins over the discovery descriptor once it arrived.
    function tidalInstalled() {
        const data = state.lastData.tidal || {};
        if (data.installed !== undefined) return data.installed === true;
        return (state.descriptors.tidal || {}).installed === true;
    }

    function tidalEnabledAndInstalled() {
        return tidalInstalled() && state.enabled.tidal === true;
    }

    function tidalConnection() {
        const data = state.lastData.tidal || {};
        const usable = tidalEnabledAndInstalled();
        return {
            installed: tidalInstalled(),
            enabled: state.enabled.tidal === true,
            usable: usable,
            available: data.available !== false,
            authenticated: usable && data.authenticated === true,
            userId: tidalUserId(),
        };
    }

    function notifyTidalStatus() {
        if (tidalConnection().authenticated) ensureTidalAccountState();
        dispatchTidalEvent('fxroute:tidal-status', tidalConnection());
    }

    function notifyTidalLibrary(kind) {
        dispatchTidalEvent('fxroute:tidal-library', { kind: kind || null });
    }

    async function fetchTidalJsonList(url) {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(await errorDetail(resp));
        const items = await resp.json();
        return Array.isArray(items) ? items : [];
    }

    // Whole favorites collection of one kind. Without a last-known list the
    // first page is handed to onPage right away and the rest loads behind it,
    // so a large collection never waits for every page before showing.
    async function fetchTidalCollection(kind, onPage) {
        if (kind === 'playlists') return fetchTidalJsonList('/api/streaming/tidal/playlists');
        const base = '/api/streaming/tidal/favorites?type=' + encodeURIComponent(kind) + '&limit=';
        if (state.tidal.lastItems[kind] === undefined && typeof onPage === 'function') {
            const first = await fetchTidalJsonList(base + TIDAL_FAVORITES_PAGE);
            if (first.length < TIDAL_FAVORITES_PAGE) return first;
            onPage(first);
        }
        return fetchTidalJsonList(base + '0');
    }

    // Adopt a collection for the current account. A complete list is the
    // authoritative favorite state of its kind; a first page only adds.
    function adoptTidalCollection(userId, kind, items, complete) {
        if (state.tidal.cacheUser !== userId) return;
        const list = Array.isArray(items) ? items : [];
        state.tidal.lastItems[kind] = list;
        const ids = list.map((item) => String(item && item.id)).filter(Boolean);
        const set = state.tidal.favoriteIds[kind];
        if (set) {
            if (complete) state.tidal.favoriteIds[kind] = new Set(ids);
            else ids.forEach((id) => set.add(id));
            notifyFavoritesChanged();
        }
        notifyTidalLibrary(kind);
    }

    async function refreshTidalLibraryCollection(kind) {
        const userId = tidalUserId();
        try {
            const items = await fetchTidalCollection(kind, (page) => adoptTidalCollection(userId, kind, page, false));
            adoptTidalCollection(userId, kind, items, true);
        } catch (err) {
            // A failed refresh keeps the last-known collection visible.
        }
    }

    // Load the Library's TIDAL collections: the cached snapshot first, then
    // fresh collections in the background. Re-reads at most once per
    // TIDAL_LIBRARY_REFRESH_MS unless forced.
    function ensureTidalLibrary(force) {
        if (!tidalConnection().authenticated) return Promise.resolve(false);
        ensureTidalAccountState();
        if (state.tidal.libraryPromise) return state.tidal.libraryPromise;
        if (!force && Date.now() - state.tidal.libraryLoadedAt < TIDAL_LIBRARY_REFRESH_MS) {
            return Promise.resolve(true);
        }
        state.tidal.libraryPromise = (async () => {
            try {
                await loadTidalSnapshot();
                notifyTidalLibrary(null);
                await Promise.all(TIDAL_LIBRARY_KINDS.map(refreshTidalLibraryCollection));
                state.tidal.libraryLoadedAt = Date.now();
                return true;
            } finally {
                state.tidal.libraryPromise = null;
            }
        })();
        return state.tidal.libraryPromise;
    }

    function tidalLibraryItems(kind) {
        const items = state.tidal.lastItems[kind];
        return Array.isArray(items) ? items : [];
    }

    function bindTidalLibraryFavoriteButtons(container) {
        if (!container) return;
        container.querySelectorAll('.streaming-fav[data-fav-type][data-fav-id]').forEach((btn) => {
            btn.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleTidalFavorite(btn.dataset.favType, btn.dataset.favId);
            });
        });
    }

    // Open a TIDAL album/playlist/artist inside the Library.
    function openTidalLibraryDetail(kind, id, title, artUrl) {
        if (state.tidal.viewStack.length || isTidalDetailView()) restoreTidalDetail(null);
        if (kind === 'artist') rememberTidalArtist({ id: id, name: title, art_url: artUrl });
        openTidalDetail(kind, id, title, artUrl);
    }

    function setTidalTrackSelection(ids, selected) {
        (ids || []).forEach((id) => {
            if (selected) state.tidal.selectedTrackIds.add(String(id));
            else state.tidal.selectedTrackIds.delete(String(id));
        });
        syncTidalTrackSelection();
    }

    // "On TIDAL" section of a local artist page: the artist's top tracks and
    // albums, rendered by the TIDAL detail code. Resolves with the artist
    // payload (enrichment included) so the page can read its MBID and bio.
    async function renderTidalArtistInto(container, id, isCurrent) {
        const data = await fetchTidalArtist(id);
        try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
        if (typeof isCurrent === 'function' && !isCurrent()) return null;
        renderTidalArtistBody(container, data);
        return data;
    }

    // Library search: one catalog request for all four result types, kept
    // per account and query so switching the type or coming back from a
    // detail does not search again.
    const TIDAL_CATALOG_SEARCH_LIMIT = 25;
    const TIDAL_CATALOG_SEARCH_TTL_MS = 5 * 60 * 1000;
    const TIDAL_CATALOG_SEARCH_MAX_ENTRIES = 12;
    const catalogSearches = new Map();

    function searchTidalCatalog(query) {
        const text = String(query || '').trim();
        const connection = tidalConnection();
        const cacheKey = (connection.userId || '') + '|' + text.toLowerCase();
        const cached = catalogSearches.get(cacheKey);
        if (cached && Date.now() - cached.at < TIDAL_CATALOG_SEARCH_TTL_MS) return cached.promise;
        const promise = (async () => {
            const resp = await fetch('/api/streaming/tidal/search?q=' + encodeURIComponent(text)
                + '&types=' + TIDAL_SEARCH_TYPES.join(',') + '&limit=' + TIDAL_CATALOG_SEARCH_LIMIT);
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const data = await resp.json();
            const results = {};
            TIDAL_SEARCH_TYPES.forEach((type) => { results[type] = Array.isArray(data[type]) ? data[type] : []; });
            results.artists.forEach(rememberTidalArtist);
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts render unfilled until state loads */ }
            return results;
        })();
        catalogSearches.set(cacheKey, { at: Date.now(), promise: promise });
        // A failed search must be retried, never replayed from the cache.
        promise.catch(() => { if (catalogSearches.get(cacheKey) && catalogSearches.get(cacheKey).promise === promise) catalogSearches.delete(cacheKey); });
        while (catalogSearches.size > TIDAL_CATALOG_SEARCH_MAX_ENTRIES) {
            catalogSearches.delete(catalogSearches.keys().next().value);
        }
        return promise;
    }

    const tidalLibraryAdapter = {
        connection: tidalConnection,
        searchCatalog: searchTidalCatalog,
        ensureLibrary: ensureTidalLibrary,
        items: tidalLibraryItems,
        isFavorite: isTidalFavorite,
        favoriteButtonHtml: (type, id) => favoriteButtonHtml(type, id),
        bindFavoriteButtons: bindTidalLibraryFavoriteButtons,
        cardArtHtml: (url, fallbackText) => tidalCardArt(url, fallbackText),
        buildTrackRow: buildTidalTrackRow,
        attachLibraryHost: (host) => { libraryHost = host || null; },
        openDetail: openTidalLibraryDetail,
        closeLibraryDetail: closeTidalLibraryDetail,
        isLibraryDetailOpen: () => isTidalDetailView(),
        selectedIds: () => Array.from(state.tidal.selectedTrackIds),
        trackMeta: (ids) => {
            const found = {};
            (ids || []).forEach((id) => {
                const meta = tidalTrackMeta.get(String(id));
                if (meta) found[String(id)] = Object.assign({}, meta);
            });
            return found;
        },
        selectTracks: (ids) => setTidalTrackSelection(ids, true),
        deselectTracks: (ids) => setTidalTrackSelection(ids, false),
        clearSelection: clearTidalPlaylistSelection,
        toggleFavorite: toggleTidalFavorite,
        resolveArtist: resolveTidalArtistMatch,
        rememberArtist: rememberTidalArtist,
        renderArtistInto: renderTidalArtistInto,
        renderSimilarArtists: renderSimilarArtists,
        hydrateSimilarArtists: hydrateSimilarArtistImages,
        searchArtist: (name) => { if (canSearchInLibrary()) searchArtistInLibrary(name); },
        createPlaylist: createTidalPlaylist,
        addToPlaylist: addTracksToTidalPlaylist,
        play: playTidalTracks,
        beginLogin: () => openTidalLogin(),
        refreshCatalog: async () => {
            await refreshTidalCatalog();
            await ensureTidalLibrary(true);
        },
        friendlyError: friendlyError,
    };

    // -----------------------------------------------------------------------
    // Public surface
    // -----------------------------------------------------------------------
    window.FXRouteStreaming = {
        init,
        renderProvider,
        notifyPlayback,
        onTabVisible,
        applyProviderEnabled,
        refreshEnabledFlags,
        refreshActiveTab: () => {
            const tab = window.__visibleTab;
            if (tab === 'qobuz') void refreshProvider(tab);
            // The Library shows TIDAL login state (header button, favorites).
            else if (tab === 'library' && tidalEnabledAndInstalled()) void refreshProvider('tidal');
        },
        // Footer / global favorite hooks: the shared footer heart favorites the
        // current TIDAL track through the same canonical state and API the
        // Library rows and details use — no second favorite source.
        isTidalFavorite,
        tidalFavoritesReady,
        ensureTidalFavoritesLoaded,
        toggleTidalFavorite,
        // TIDAL inside the shared Library tab (data, rows, details, save row).
        tidal: tidalLibraryAdapter,
    };
})();
