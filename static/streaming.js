// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute streaming provider UI (Spotify, Qobuz, TIDAL).
 *
 * This module owns the provider tabs and renders one capability-driven
 * "now playing" card per provider. It never branches on provider identity to
 * decide which controls to show — it reads the provider's `capabilities` and
 * omits anything unsupported. Transport dispatch is a thin per-provider
 * backend adapter (Spotify -> app.js playerctl handler, Qobuz -> generic
 * /api/streaming/qobuz/..., TIDAL -> native /api/playback/...); catalog and
 * auth are TIDAL-only and live behind the provider's own content area.
 *
 * Native TIDAL playback remains owned by app.js (MPV -> fxroute_dsp_sink).
 */
(function () {
    'use strict';

    let api = null;
    let showToast = function () {};
    let escapeHtml = function (v) { return String(v == null ? '' : v); };
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
        tidal: { kind: 'native', action: { toggle: 'toggle', next: 'next', previous: 'previous', shuffle: 'shuffle', loop: 'loop' } },
    };

    // Provider-specific *content* facts (display name, whether FXRoute offers an
    // in-app connect flow). Control rendering stays capability-driven; only
    // these copy/content flags key off the provider id.
    const PROVIDER_META = {
        spotify: { name: 'Spotify', canConnect: false },
        qobuz: { name: 'Qobuz', canConnect: false },
        tidal: { name: 'Tidal', canConnect: true, catalog: true },
    };

    const POLL_INTERVAL_MS = 2000;
    const TIDAL_POLL_INTERVAL_MS = 2500;
    const TIDAL_SEARCH_DEBOUNCE_MS = 300;

    const state = {
        providers: {},          // id -> { descriptor, root, els, tabBtn, tabPanel }
        lastData: {},           // id -> last normalized status payload
        polls: {},              // id -> interval timer
        lastPlaybackSource: null,
        lastPlaybackAt: 0,
        tidal: {
            view: null,         // 'login' | 'browse' | 'album' | 'playlist' | 'artist'
            viewStack: [],      // previous detail views so Back returns through nested details
            searchQuery: '',
            searchExecuted: false,
            searchResultType: 'tracks',
            searchResults: null,
            searchRequestId: 0,
            searchDebounceTimer: null,
            searchInFlight: false,
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
            contentKey: null,   // availability/auth mode last rendered into .streaming-content
            browseCategory: 'albums',   // 'albums' | 'tracks' | 'artists' | 'playlists'; search results are a separate temporary overlay
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
            // Grid/list layout per browse surface; persisted in localStorage
            // (fx-view-mode-<surface>, same mechanism as the library toggle).
            albumLayouts: {
                albums: readStoredViewMode('tidal-albums'),
                artists: readStoredViewMode('tidal-artists'),
                playlists: readStoredViewMode('tidal-playlists'),
            },
        },
    };

    // -----------------------------------------------------------------------
    // Public API (called by app.js)
    // -----------------------------------------------------------------------
    function init(interfaceApi) {
        if (initialized) return;
        initialized = true;
        api = interfaceApi || {};
        if (typeof api.showToast === 'function') showToast = api.showToast;
        if (typeof api.escapeHtml === 'function') escapeHtml = api.escapeHtml;
        if (typeof api.formatTime === 'function') formatTime = api.formatTime;
        if (typeof api.artworkPlaceholderUrl === 'function') artworkPlaceholderUrl = api.artworkPlaceholderUrl;
        if (typeof api.trackRowHtml === 'function') trackRowHtml = api.trackRowHtml;
        if (typeof api.factsHtml === 'function') factsHtml = api.factsHtml;
        if (typeof api.aboutHtml === 'function') aboutHtml = api.aboutHtml;
        buildProviderDom();
        void loadProviders();
    }

    // Render one provider from a normalized status payload. Used by app.js for
    // Spotify and by this module's own refresh loop for Qobuz/TIDAL.
    function renderProvider(providerId, data) {
        if (!data) return;
        state.lastData[providerId] = data;
        const entry = state.providers[providerId];
        if (!entry) return;
        applyTabVisibility(providerId, entry, data);
        renderStatusLine(providerId, entry, data);
        renderNowPlaying(providerId, entry, data);
        if (providerId === 'tidal') renderTidalContent(entry, data);
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
        // TIDAL tab ever being opened. The load dispatches fxroute:tidal-favorites
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
        else if (tabId === 'tidal') {
            // Kick the local snapshot read early (it is faster than the
            // TIDAL-backed status poll) so the browse renders from cache.
            void loadTidalSnapshot();
            startPoll('tidal', TIDAL_POLL_INTERVAL_MS);
            // Re-opening the tab re-syncs the active browse section in the
            // background: the last-known state stays visible and fresh TIDAL
            // data replaces it in place when it arrives. Live search results
            // and open detail views are left untouched.
            if (state.tidal.contentKey === 'browse' && !state.tidal.searchExecuted && !isTidalDetailView()) {
                renderTidalBrowseSection(state.tidal.browseCategory);
            }
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
                            '<button type="button" class="streaming-btn" data-action="previous" title="Previous">⏮</button>' +
                            '<button type="button" class="streaming-btn streaming-btn-main" data-action="toggle" title="Play / Pause">▶</button>' +
                            '<button type="button" class="streaming-btn" data-action="next" title="Next">⏭</button>' +
                        '</div>' +
                        '<div class="streaming-secondary">' +
                            '<button type="button" class="streaming-btn-sm" data-action="shuffle" title="Shuffle">' +
                                '<span class="streaming-btn-sm-icon">⇄</span><span class="streaming-btn-sm-label">Shuffle</span>' +
                            '</button>' +
                            '<button type="button" class="streaming-btn-sm" data-action="loop" title="Loop">' +
                                '<span class="streaming-btn-sm-icon">🔁</span><span class="streaming-btn-sm-label">Loop</span>' +
                            '</button>' +
                        '</div>' +
                        '<div class="streaming-progress-row">' +
                            '<span class="streaming-time streaming-time-current">0:00</span>' +
                            '<input type="range" class="streaming-progress" min="0" max="1000" step="1" value="0" aria-label="Seek" />' +
                            '<span class="streaming-time streaming-time-total">0:00</span>' +
                        '</div>' +
                    '</div>' +
                    '<div class="streaming-content" hidden></div>' +
                '</div>';

            // Player pages (Spotify/Qobuz) get a centered stage; catalog
            // providers (TIDAL) keep a full-width browser surface.
            const providerEl = root.querySelector('.streaming-provider');
            const isCatalog = PROVIDER_META[providerId]?.catalog === true;
            providerEl.classList.toggle('streaming-provider-player', !isCatalog);
            providerEl.classList.toggle('streaming-provider-catalog', isCatalog);

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
                content: root.querySelector('.streaming-content'),
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
        const available = data.installed === true;
        const nonApp = typeof nonAppSourceModeActive === 'function' && nonAppSourceModeActive();
        const visible = available && !nonApp;
        if (entry.tabBtn) {
            entry.tabBtn.hidden = !available;
            entry.tabBtn.style.display = available ? '' : 'none';
            entry.tabBtn.classList.toggle('hidden', !visible);
        }
        if (entry.tabPanel) {
            entry.tabPanel.hidden = !available;
            entry.tabPanel.classList.toggle('hidden', !visible);
        }
        if (typeof updateTabsScrollAffordance === 'function') updateTabsScrollAffordance();
    }

    // -----------------------------------------------------------------------
    // Status (backend / connected / authenticated)
    // -----------------------------------------------------------------------
    // All three providers share one status language: a single `Connected`
    // label with a dot-led pill. The detail string stays in the tooltip and
    // backend implementation names are never surfaced.
    function buildStatusBits(providerId, data) {
        if (providerId === 'spotify') return ['Connected'];
        if (providerId === 'qobuz') {
            return (data.connected || data.authenticated === true) ? ['Connected'] : [];
        }
        if (providerId === 'tidal') {
            return data.authenticated === true ? ['Connected'] : [];
        }
        return [];
    }

    function buildStatusDetail(providerId, data) {
        if (providerId === 'spotify') {
            if (data.backend === 'desktop') return 'Spotify Desktop';
            if (data.backend === 'spotifyd') return 'spotifyd';
            return 'Spotify';
        }
        if (providerId === 'qobuz') return 'Qobuz Connect';
        return PROVIDER_META.tidal.name;
    }

    // The permanent Connected status belongs on the main catalog surface only;
    // detail views keep a clean header. Real problems stay visible: while not
    // authenticated the pill (and the login surface) still render.
    function isTidalDetailView() {
        return state.tidal.view === 'album' || state.tidal.view === 'playlist' || state.tidal.view === 'artist';
    }

    // Catalog providers keep the pill in the browse toolbar; the in-tab player
    // card is not rendered for them (the footer is the player). Shared by the
    // status render path and by browse/detail navigation, so the pill reflects
    // the current view immediately instead of waiting for the next poll.
    function applyCatalogStatusLine(entry, providerId, data) {
        const connected = buildStatusBits(providerId, data).length > 0;
        entry.els.statusLine.textContent = 'Connected';
        entry.els.statusLine.title = buildStatusDetail(providerId, data);
        const healthyDetail = providerId === 'tidal' && isTidalDetailView() && data.authenticated === true;
        entry.els.statusLine.hidden = !connected || healthyDetail;
    }

    function renderStatusLine(providerId, entry, data) {
        const catalogProvider = PROVIDER_META[providerId]?.catalog === true;
        if (catalogProvider) {
            applyCatalogStatusLine(entry, providerId, data);
            return;
        }
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
        const catalogProvider = meta.catalog === true;

        // Catalog providers (TIDAL) have no in-tab player: the global footer
        // is the player, so keep no now-playing card and no empty-player
        // surface — browse/search stays the focus of the tab.
        if (catalogProvider) {
            els.empty.hidden = true;
            els.nowPlaying.hidden = true;
            return;
        }

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
            els.toggle.title = playing ? 'Pause' : 'Play';
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
            els.shuffle.title = data.shuffle ? 'Shuffle on' : 'Shuffle off';
            els.shuffle.setAttribute('aria-pressed', data.shuffle ? 'true' : 'false');
        }
        if (els.loop) {
            els.loop.style.display = showLoop ? '' : 'none';
            const loopVal = data.loop || 'none';
            const loopActive = loopVal !== 'none';
            els.loop.classList.toggle('active', loopActive);
            els.loop.setAttribute('aria-pressed', loopActive ? 'true' : 'false');
            els.loop.title = loopVal === 'track' ? 'Loop: track' : (loopVal === 'playlist' ? 'Loop: playlist' : 'Loop: off');
            if (els.loopIcon) els.loopIcon.textContent = loopVal === 'track' ? '🔂' : '🔁';
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
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'btn-primary';
            btn.textContent = 'Connect TIDAL';
            btn.addEventListener('click', () => openTidalLogin());
            els.emptyActions.appendChild(btn);
        }
    }

    function providerUnavailableMessage(providerId) {
        if (providerId === 'spotify') return 'playerctl is not installed. Install it to control Spotify.';
        if (providerId === 'qobuz') return 'qbzd is not running. Start it to control Qobuz.';
        if (providerId === 'tidal') return 'The TIDAL backend is not available on this system.';
        return 'This provider is not available.';
    }

    function notPlayingContent(providerId, data) {
        // spotifyd 0.4.x hides its MPRIS interface without an active Connect
        // session; the backend flags standby so an idle daemon reads as
        // "ready" instead of "not running". qbzd reports the same through its
        // session state.
        if (providerId === 'spotify' && data && data.spotifyd_standby) {
            return {
                title: 'Spotify is ready.',
                message: '',
            };
        }
        if (providerId === 'qobuz' && data && data.qbzd_standby) {
            return {
                title: 'Qobuz is ready.',
                message: '',
            };
        }
        if (providerId === 'spotify') return { title: 'Spotify is ready.', message: '' };
        if (providerId === 'qobuz') return { title: 'Qobuz is ready.', message: '' };
        if (providerId === 'tidal') return { title: 'TIDAL is ready.', message: '' };
        return { title: 'Nothing is playing.', message: '' };
    }

    function connectMessage(providerId) {
        if (providerId === 'qobuz') return 'Sign in to Qobuz through qbzd to start playback.';
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
            if (adapter.kind === 'native') {
                await nativeTransportCommand(action, extra);
                return;
            }
            await remoteTransportCommand(adapter, action, extra);
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    async function remoteTransportCommand(adapter, action, extra) {
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
        // Remote state settles asynchronously; refresh once and shortly after.
        void refreshProvider('qobuz');
    }

    async function nativeTransportCommand(action, extra) {
        let resp;
        if (action === 'seek') {
            resp = await fetch('/api/playback/seek', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ position: extra?.position || 0 }),
            });
        } else if (action === 'shuffle' || action === 'loop') {
            const data = state.lastData.tidal || {};
            const current = action === 'shuffle' ? !!data.shuffle : (data.loop && data.loop !== 'none');
            resp = await fetch('/api/playback/' + action, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: !current }),
            });
        } else {
            const mapped = TRANSPORT.tidal.action[action] || action;
            resp = await fetch('/api/playback/' + mapped, { method: 'POST' });
        }
        if (!resp.ok) throw new Error(await errorDetail(resp));
        void refreshTidalStatus();
    }

    // -----------------------------------------------------------------------
    // Status refresh / polling
    // -----------------------------------------------------------------------
    async function refreshProvider(providerId) {
        try {
            const resp = await fetch('/api/streaming/' + providerId + '/status');
            if (!resp.ok) return;
            renderProvider(providerId, await resp.json());
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
                const entry = state.providers[descriptor.id];
                if (!entry) continue;
                entry.descriptor = descriptor;
                entry.providerLabel = () => descriptor.name || descriptor.id;
                // Show the tab only for implemented, installed providers.
                const pseudo = { installed: descriptor.installed, available: descriptor.available, capabilities: descriptor.capabilities };
                applyTabVisibility(descriptor.id, entry, pseudo);
                if (descriptor.installed) {
                    void refreshProvider(descriptor.id);
                }
            }
        } catch (err) {
            // ignore; tabs remain hidden until the next reconnect/resync
        }
    }

    // -----------------------------------------------------------------------
    // Error normalization (provider-neutral user-facing text)
    // -----------------------------------------------------------------------
    function errorDetail(resp) {
        return resp.json().then((d) => d?.detail || '').catch(() => '');
    }

    function friendlyError(raw) {
        const text = String(raw || '');
        const lower = text.toLowerCase();
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

    // -----------------------------------------------------------------------
    // TIDAL content (auth + catalog). Qobuz/Spotify have no catalog here yet.
    // -----------------------------------------------------------------------
    function renderTidalContent(entry, data) {
        // Provider content must not be rebuilt on every status refresh: a
        // poll/transport/playback nudge calls renderProvider frequently, and
        // rebuilding here would wipe the user's search/favorites/playlists
        // view and any in-progress login input. Rebuild only when the content
        // mode actually changes (first render, availability, or login state);
        // navigation between browse sections renders directly via
        // renderTidalBrowse() / renderTidalLogin().
        const els = entry.els;
        const unavailable = data.available === false || data.installed !== true;
        const key = unavailable ? 'unavailable' : (data.authenticated === true ? 'browse' : 'login');
        if (key === 'unavailable') {
            els.content.hidden = true;
            state.tidal.contentKey = key;
            return;
        }
        els.content.hidden = false;
        if (state.tidal.contentKey === key) return;
        state.tidal.contentKey = key;
        if (data.authenticated === true) {
            renderTidalBrowse(entry);
        } else {
            renderTidalLogin(entry);
        }
    }

    // -- login -----------------------------------------------------------------
    function renderTidalLogin(entry) {
        const content = entry.els.content;
        if (state.tidal.view === 'pkce') { renderTidalPkce(content); return; }
        if (state.tidal.view === 'device') { renderTidalDevice(content); return; }
        content.innerHTML =
            '<div class="streaming-auth">' +
                '<h3 class="streaming-auth-title">Connect TIDAL</h3>' +
                '<p class="streaming-auth-hint">For Lossless and Hi-Res, use the secure browser login.</p>' +
                '<div class="streaming-auth-actions">' +
                    '<button type="button" class="btn-primary" id="tidal-auth-pkce">Start TIDAL Login</button>' +
                    '<button type="button" class="btn-ghost" id="tidal-auth-device">Device login (limited to AAC 320 kbps)</button>' +
                '</div>' +
            '</div>';
        content.querySelector('#tidal-auth-pkce').addEventListener('click', () => { state.tidal.view = 'pkce'; renderTidalPkce(content); });
        content.querySelector('#tidal-auth-device').addEventListener('click', () => { state.tidal.view = 'device'; renderTidalDevice(content); });
    }

    function renderTidalPkce(content) {
        content.innerHTML =
            '<div class="streaming-auth">' +
                '<h3 class="streaming-auth-title">TIDAL browser login</h3>' +
                '<ol class="streaming-auth-steps">' +
                    '<li>Open this link on any device and sign in to TIDAL.</li>' +
                '</ol>' +
                '<div class="streaming-auth-url-row">' +
                    '<code class="streaming-auth-url" id="tidal-pkce-url">Generating link…</code>' +
                    '<button type="button" class="btn-secondary" id="tidal-pkce-copy">Copy</button>' +
                '</div>' +
                '<ol class="streaming-auth-steps" start="2">' +
                    '<li>After login, your browser may show an unreachable page.</li>' +
                    '<li>Copy the complete URL from the address bar and paste it here.</li>' +
                '</ol>' +
                '<input type="text" class="url-input streaming-auth-input" id="tidal-pkce-input" placeholder="https://tidal.com/android/login/auth?code=…" autocomplete="off" />' +
                '<div class="streaming-auth-actions">' +
                    '<button type="button" class="btn-primary" id="tidal-pkce-complete">Complete Login</button>' +
                    '<button type="button" class="btn-ghost" id="tidal-pkce-back">Back</button>' +
                '</div>' +
                '<p class="streaming-auth-error" id="tidal-pkce-error" hidden></p>' +
            '</div>';

        const urlEl = content.querySelector('#tidal-pkce-url');
        const inputEl = content.querySelector('#tidal-pkce-input');
        const errorEl = content.querySelector('#tidal-pkce-error');

        void fetch('/api/streaming/tidal/auth/pkce', { method: 'POST' })
            .then((r) => r.json())
            .then((d) => { urlEl.textContent = d.url || 'Could not generate a login link.'; })
            .catch(() => { urlEl.textContent = 'Could not generate a login link.'; });

        content.querySelector('#tidal-pkce-copy').addEventListener('click', () => {
            const text = urlEl.textContent || '';
            navigator.clipboard?.writeText(text).then(() => showToast('Login link copied', 'success'));
        });
        content.querySelector('#tidal-pkce-back').addEventListener('click', () => { state.tidal.view = null; renderTidalLogin(entryFor('tidal')); });
        content.querySelector('#tidal-pkce-complete').addEventListener('click', async () => {
            const url = (inputEl.value || '').trim();
            if (!url) { errorEl.textContent = 'Paste the redirect URL first.'; errorEl.hidden = false; return; }
            errorEl.hidden = true;
            try {
                const resp = await fetch('/api/streaming/tidal/auth/pkce/finish', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ redirect_url: url }),
                });
                const d = await resp.json().catch(() => null);
                if (!resp.ok) throw new Error(d?.detail || 'Login failed');
                showToast('TIDAL connected', 'success');
                state.tidal.view = null;
                await refreshTidalStatus();
            } catch (err) {
                errorEl.textContent = friendlyError(err?.message || err);
                errorEl.hidden = false;
            }
        });
    }

    function renderTidalDevice(content) {
        content.innerHTML =
            '<div class="streaming-auth">' +
                '<h3 class="streaming-auth-title">TIDAL device login</h3>' +
                '<p class="streaming-auth-hint">Limited to AAC 320 kbps. For Lossless/Hi-Res use the browser login instead.</p>' +
                '<div class="streaming-auth-device-code" id="tidal-device-code">—</div>' +
                '<p class="streaming-auth-hint">Open <strong id="tidal-device-uri">link.tidal.com</strong> and enter the code above.</p>' +
                '<div class="streaming-auth-actions">' +
                    '<button type="button" class="btn-primary" id="tidal-device-finish">I\'ve entered the code</button>' +
                    '<button type="button" class="btn-ghost" id="tidal-device-back">Back</button>' +
                '</div>' +
                '<p class="streaming-auth-error" id="tidal-device-error" hidden></p>' +
            '</div>';

        void fetch('/api/streaming/tidal/auth/device', { method: 'POST' })
            .then((r) => r.json())
            .then((d) => {
                content.querySelector('#tidal-device-code').textContent = d.user_code || '—';
                content.querySelector('#tidal-device-uri').textContent = d.verification_uri_complete || 'link.tidal.com';
            })
            .catch(() => { content.querySelector('#tidal-device-code').textContent = 'Could not start device login.'; });

        content.querySelector('#tidal-device-back').addEventListener('click', () => { state.tidal.view = null; renderTidalLogin(entryFor('tidal')); });
        content.querySelector('#tidal-device-finish').addEventListener('click', async () => {
            const errorEl = content.querySelector('#tidal-device-error');
            errorEl.hidden = true;
            try {
                const resp = await fetch('/api/streaming/tidal/auth/device/finish', { method: 'POST' });
                const d = await resp.json().catch(() => null);
                if (!resp.ok) throw new Error(d?.detail || 'Login failed');
                showToast('TIDAL connected', 'success');
                state.tidal.view = null;
                await refreshTidalStatus();
            } catch (err) {
                errorEl.textContent = friendlyError(err?.message || err);
                errorEl.hidden = false;
            }
        });
    }

    function openTidalLogin() {
        state.tidal.view = null;
        const entry = entryFor('tidal');
        if (entry) renderTidalLogin(entry);
    }

    function entryFor(providerId) {
        return state.providers[providerId] || null;
    }

    // -- browse (categories + search) ---------------------------------------
    // The four browse categories (Tracks / Albums / Artists / Playlists) live
    // in one shared navigation row rendered with the compact view-tab
    // component. Search results only ever replace the browse body; the bar
    // itself survives every status refresh (contentKey guard in
    // renderTidalContent), so a poll never resets an in-progress query.
    const TIDAL_BROWSE_CATEGORIES = ['albums', 'tracks', 'artists', 'playlists'];
    const TIDAL_BROWSE_LABELS = { tracks: 'Tracks', albums: 'Albums', artists: 'Artists', playlists: 'Playlists' };
    // Surfaces that offer the grid/list toggle; tracks stay row-only.
    const TIDAL_LAYOUT_SURFACES = { albums: 'tidal-albums', artists: 'tidal-artists', playlists: 'tidal-playlists' };

    // -- shared grid/list toggle ------------------------------------------------
    // Same component and persistence mechanism as the library albums toggle:
    // fx-view-mode-<surface> in localStorage, grid is the default. The toggle
    // only flips a container class, so grid and list render the same objects
    // with the same actions.
    function readStoredViewMode(surface) {
        try {
            return localStorage.getItem('fx-view-mode-' + surface) === 'list' ? 'list' : 'grid';
        } catch (e) {
            return 'grid';
        }
    }

    function storeViewMode(surface, mode) {
        try {
            localStorage.setItem('fx-view-mode-' + surface, mode);
        } catch (e) { /* keep working without persistence */ }
    }

    function viewModeButtonsHtml(storageSurface) {
        const key = Object.keys(TIDAL_LAYOUT_SURFACES).find((k) => TIDAL_LAYOUT_SURFACES[k] === storageSurface);
        const layout = (key && state.tidal.albumLayouts[key]) || 'grid';
        return '<button type="button" class="view-mode-btn' + (layout === 'grid' ? ' active' : '') + '" data-layout-toggle="grid" data-layout-surface="' + storageSurface + '" aria-pressed="' + (layout === 'grid' ? 'true' : 'false') + '" title="Grid view" aria-label="Grid view">▦</button>' +
            '<button type="button" class="view-mode-btn' + (layout === 'list' ? ' active' : '') + '" data-layout-toggle="list" data-layout-surface="' + storageSurface + '" aria-pressed="' + (layout === 'list' ? 'true' : 'false') + '" title="List view" aria-label="List view">☰</button>';
    }

    function bindViewModeToggle(root) {
        root.querySelectorAll('.view-mode-btn[data-layout-toggle]').forEach((btn) => {
            btn.addEventListener('click', () => setTidalLayout(btn.dataset.layoutSurface, btn.dataset.layoutToggle));
        });
    }

    // The toggle controls whichever tile surface is currently displayed:
    // the active browse category, or — during an executed search — the
    // matching search result type. Tracks never get a toggle.
    function tidalActiveStorageSurface() {
        if (state.tidal.searchExecuted) return TIDAL_LAYOUT_SURFACES[state.tidal.searchResultType] || '';
        return TIDAL_LAYOUT_SURFACES[state.tidal.browseCategory] || '';
    }

    function syncTidalViewModeToggle() {
        const toggle = document.getElementById('tidal-view-mode-toggle');
        if (!toggle) return;
        const storageSurface = tidalActiveStorageSurface();
        toggle.hidden = !storageSurface;
        const key = Object.keys(TIDAL_LAYOUT_SURFACES).find((k) => TIDAL_LAYOUT_SURFACES[k] === storageSurface);
        const layout = (key && state.tidal.albumLayouts[key]) || 'grid';
        toggle.querySelectorAll('.view-mode-btn').forEach((btn) => {
            const active = btn.dataset.layoutToggle === layout;
            btn.classList.toggle('active', active);
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.dataset.layoutSurface = storageSurface;
        });
    }

    // External callers (shared toggle logic in app.js) address surfaces by
    // their storage key; internally the browse category is used.
    function setTidalLayout(storageSurface, mode) {
        const surface = Object.keys(TIDAL_LAYOUT_SURFACES).find((key) => TIDAL_LAYOUT_SURFACES[key] === storageSurface);
        if (!surface) return;
        const layout = mode === 'list' ? 'list' : 'grid';
        if (state.tidal.albumLayouts[surface] === layout) return;
        state.tidal.albumLayouts[surface] = layout;
        storeViewMode(storageSurface, layout);
        applyTidalLayout(surface);
    }

    // Re-render the active browse surface (or running search results) so the
    // new layout applies without touching search state or favorites.
    function applyTidalLayout(surface) {
        if (state.tidal.browseCategory !== surface) return;
        const body = document.getElementById('tidal-browse-body');
        if (!body) return;
        if (state.tidal.searchExecuted && state.tidal.searchResults && state.tidal.searchResultType === surface) {
            renderTidalSearchResults(body, state.tidal.searchResults);
        } else {
            renderTidalBrowseSection(surface);
        }
    }

    function tidalLayoutClass(surface) {
        return (state.tidal.albumLayouts[surface] || 'grid') === 'list' ? 'tidal-tiles is-list' : 'tidal-tiles';
    }

    function renderTidalBrowse(entry) {
        const content = entry.els.content;
        // Keep the status line consistent while navigating between the main
        // surface and detail views (the poll refreshes it as well).
        applyCatalogStatusLine(entry, 'tidal', state.lastData.tidal || {});
        if (state.tidal.view === 'album') { renderTidalAlbum(content); return; }
        if (state.tidal.view === 'playlist') { renderTidalPlaylist(content); return; }
        if (state.tidal.view === 'artist') { renderTidalArtist(content); return; }
        // The browse surface follows the library header logic: row 1 is the
        // page title with the connected pill + refresh on the right, row 2 is
        // the single Tracks/Albums/Artists/Playlists navigation with the
        // search group on the right. Search results only ever replace the
        // browse body; the bar itself survives every status refresh
        // (contentKey guard in renderTidalContent), so a poll never resets an
        // in-progress query.
        content.innerHTML =
            '<div class="streaming-browse">' +
                '<div class="tidal-toolbar">' +
                    '<h2 class="section-title tidal-toolbar-title">Tidal</h2>' +
                    '<div class="tidal-toolbar-actions">' +
                        '<button type="button" class="btn-secondary btn-icon" id="tidal-refresh-btn" title="Refresh TIDAL" aria-label="Refresh TIDAL">⟳</button>' +
                        '<div class="view-mode-toggle" id="tidal-view-mode-toggle" role="group" aria-label="View mode">' +
                            viewModeButtonsHtml(tidalActiveStorageSurface()) +
                        '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="tidal-subbar">' +
                    '<div class="view-tabs" role="tablist" aria-label="TIDAL browse">' +
                        TIDAL_BROWSE_CATEGORIES.map((cat) =>
                            '<button type="button" class="view-tab' + (state.tidal.browseCategory === cat ? ' is-active' : '') + '" data-browse="' + cat + '">' + TIDAL_BROWSE_LABELS[cat] + '</button>'
                        ).join('') +
                    '</div>' +
                    '<div class="streaming-search">' +
                        '<div class="streaming-search-row">' +
                            '<input type="search" class="streaming-search-input" id="tidal-search-input" placeholder="Search albums, tracks, artists, playlists…" data-placeholder-full="Search albums, tracks, artists, playlists…" data-placeholder-compact="Search…" autocomplete="off" />' +
                        '</div>' +
                    '</div>' +
                '</div>' +
                tidalPlaylistSaveRowHtml() +
                '<div class="streaming-browse-body" id="tidal-browse-body"></div>' +
            '</div>';
        const actions = content.querySelector('.tidal-toolbar-actions');
        if (actions && entry.els.statusLine) actions.prepend(entry.els.statusLine);
        bindTidalSearchBar(content);
        bindTidalRefresh(content);
        bindViewModeToggle(content);
        bindTidalPlaylistSaveRow(content);
        syncTidalViewModeToggle();
        updateTidalPlaylistSaveRow();
        const input = content.querySelector('#tidal-search-input');
        if (input && state.tidal.searchExecuted) input.value = state.tidal.searchQuery;
        const tabs = content.querySelectorAll('.tidal-subbar .view-tab[data-browse]');
        tabs.forEach((tab) => tab.addEventListener('click', () => {
            tabs.forEach((t) => t.classList.toggle('is-active', t === tab));
            resetTidalSearch();
            renderTidalBrowseSection(tab.dataset.browse);
        }));
        // Preload the last-known library state so the first section render is
        // instant; the section load itself refreshes in the background.
        void loadTidalSnapshot();
        if (state.tidal.searchExecuted && state.tidal.searchResults) {
            renderTidalSearchResults(document.getElementById('tidal-browse-body'), state.tidal.searchResults);
        } else if (state.tidal.searchExecuted && state.tidal.searchQuery) {
            const body = document.getElementById('tidal-browse-body');
            if (body) {
                body.innerHTML = contentState('loading', 'Searching…');
                if (state.tidal.searchDebounceTimer === null && !state.tidal.searchInFlight) {
                    void executeTidalSearch(state.tidal.searchResultType);
                }
            }
        } else {
            renderTidalBrowseSection(state.tidal.browseCategory);
        }
    }

    function renderTidalBrowseSection(section) {
        const body = document.getElementById('tidal-browse-body');
        if (!body) return;
        state.tidal.browseCategory = section;
        syncTidalViewModeToggle();
        if (section === 'playlists') renderTidalPlaylists(body);
        else renderTidalFavorites(body, section);
    }

    // -- search (permanent bar sharing the second header row) ----------------
    // Executing a search replaces the browse body with results; clearing it
    // returns to the current browse category (Albums by default). The bar
    // lives in the browse surface, so the contentKey guard keeps a running
    // search (and its results) intact across status refreshes.
    function cancelTidalSearchDebounce() {
        if (state.tidal.searchDebounceTimer !== null) {
            clearTimeout(state.tidal.searchDebounceTimer);
            state.tidal.searchDebounceTimer = null;
        }
    }

    function bindTidalSearchBar(root) {
        const input = root.querySelector('#tidal-search-input');
        const updatePlaceholder = () => {
            const compact = window.matchMedia && window.matchMedia('(max-width: 600px)').matches;
            input.placeholder = compact
                ? (input.dataset.placeholderCompact || 'Search…')
                : (input.dataset.placeholderFull || 'Search albums, tracks, artists, playlists…');
        };
        updatePlaceholder();
        if (window.matchMedia) {
            const placeholderQuery = window.matchMedia('(max-width: 600px)');
            if (placeholderQuery.addEventListener) {
                placeholderQuery.addEventListener('change', updatePlaceholder);
            } else if (placeholderQuery.addListener) {
                placeholderQuery.addListener(updatePlaceholder);
            }
        }
        const startSearch = (immediate) => {
            const query = (input.value || '').trim();
            cancelTidalSearchDebounce();
            if (!query) {
                clearTidalSearch();
                return;
            }
            // Invalidate the previous generation before waiting, not only when
            // the next request starts. A late response must not render during
            // the debounce window for a newer query.
            state.tidal.searchRequestId += 1;
            state.tidal.searchQuery = query;
            state.tidal.searchExecuted = true;
            state.tidal.searchResults = null;
            state.tidal.searchInFlight = false;
            const body = root.querySelector('#tidal-browse-body');
            if (body) body.innerHTML = contentState('loading', immediate ? 'Searching…' : 'Waiting to search…');
            if (immediate) {
                void executeTidalSearch(state.tidal.searchResultType);
                return;
            }
            state.tidal.searchDebounceTimer = setTimeout(() => {
                state.tidal.searchDebounceTimer = null;
                void executeTidalSearch(state.tidal.searchResultType);
            }, TIDAL_SEARCH_DEBOUNCE_MS);
        };
        input.addEventListener('input', () => startSearch(false));
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                startSearch(true);
            } else if (e.key === 'Escape') {
                e.preventDefault();
                clearTidalSearch();
            }
        });
    }

    function resetTidalSearch() {
        // Invalidate a response that is still resolving after clear/navigation.
        cancelTidalSearchDebounce();
        state.tidal.searchRequestId += 1;
        state.tidal.searchQuery = '';
        state.tidal.searchExecuted = false;
        state.tidal.searchResults = null;
        state.tidal.searchInFlight = false;
        const input = document.getElementById('tidal-search-input');
        if (input) input.value = '';
    }

    function clearTidalSearch() {
        resetTidalSearch();
        renderTidalBrowseSection(state.tidal.browseCategory);
    }

    // -- refresh (manual cache invalidation + re-render) ---------------------
    // The refresh button reuses the existing TIDAL provider logic: it forces
    // a fresh authoritative favorite-ids load (the source of heart/browse
    // state), reloads the provider status, then re-renders whatever view is
    // active. It never logs out, resets the session, touches playback or the
    // queue, or clears any unrelated FXRoute cache.
    function bindTidalRefresh(root) {
        const btn = root.querySelector('#tidal-refresh-btn');
        if (!btn) return;
        btn.addEventListener('click', () => {
            if (btn.disabled) return;
            btn.disabled = true;
            void refreshTidalCatalog().finally(() => { btn.disabled = false; });
        });
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
        if (state.tidal.contentKey !== 'browse') return;
        if (isTidalDetailView()) {
            const entry = entryFor('tidal');
            if (entry) renderTidalBrowse(entry);
            return;
        }
        if (state.tidal.searchExecuted && state.tidal.searchQuery) {
            void executeTidalSearch(state.tidal.searchResultType);
            return;
        }
        renderTidalBrowseSection(state.tidal.browseCategory);
    }

    const TIDAL_SEARCH_TYPES = ['artists', 'tracks', 'albums', 'playlists'];
    const TIDAL_SEARCH_TYPE_LABELS = { artists: 'Artists', tracks: 'Tracks', albums: 'Albums', playlists: 'Playlists' };

    async function executeTidalSearch(type) {
        cancelTidalSearchDebounce();
        if (!state.tidal.searchExecuted || !state.tidal.searchQuery) return;
        const body = document.getElementById('tidal-browse-body');
        if (!body) return;
        const query = state.tidal.searchQuery;
        const requestId = ++state.tidal.searchRequestId;
        state.tidal.searchResultType = type;
        state.tidal.searchInFlight = true;
        body.innerHTML = contentState('loading', 'Searching…');
        try {
            const resp = await fetch('/api/streaming/tidal/search?q=' + encodeURIComponent(query) + '&types=' + encodeURIComponent(type) + '&limit=25');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const data = await resp.json();
            if (requestId !== state.tidal.searchRequestId) return;
            state.tidal.searchResults = data;
            state.tidal.searchInFlight = false;
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts render unfilled until state loads */ }
            if (requestId === state.tidal.searchRequestId) {
                const currentBody = document.getElementById('tidal-browse-body');
                if (currentBody) renderTidalSearchResults(currentBody, data);
            }
        } catch (err) {
            if (requestId !== state.tidal.searchRequestId) return;
            state.tidal.searchInFlight = false;
            const currentBody = document.getElementById('tidal-browse-body');
            if (currentBody) currentBody.innerHTML = contentState('error', friendlyError(err?.message || err));
        }
    }

    function runTidalSearch(type) {
        if (!state.tidal.searchExecuted || !state.tidal.searchQuery) return;
        void executeTidalSearch(type);
    }

    function renderTidalSearchResults(container, data) {
        const type = state.tidal.searchResultType;
        const items = data[type] || [];
        state.tidal.searchResults = data;
        container.innerHTML = '';
        syncTidalViewModeToggle();
        const typeButtons = TIDAL_SEARCH_TYPES.map((value) =>
            '<button type="button" class="view-tab' + (value === type ? ' is-active' : '') + '" id="tidal-search-type-' + value + '" data-search-type="' + value + '">' + TIDAL_SEARCH_TYPE_LABELS[value] + '</button>'
        ).join('');
        container.innerHTML =
            '<div class="tidal-search-results-header">' +
                '<h3 class="streaming-results-title">Search results for &quot;' + escapeHtml(state.tidal.searchQuery) + '&quot;</h3>' +
                '<div class="view-tabs" id="tidal-search-result-types" aria-label="Search result type">' + typeButtons + '</div>' +
            '</div>' +
            '<div class="streaming-results" id="tidal-search-items"></div>';
        const itemsContainer = container.querySelector('#tidal-search-items');
        bindTidalSearchResultControls(container);
        if (!items.length) {
            itemsContainer.innerHTML = contentState('empty', 'No results.');
            return;
        }
        const queueIds = type === 'tracks' ? items.map((item) => String(item.id)).filter(Boolean) : [];
        const list = document.createElement('ul');
        list.className = type === 'tracks' ? 'streaming-results-list' : tidalLayoutClass(type);
        items.forEach((item) => list.appendChild(renderSearchItem(type, item, queueIds)));
        itemsContainer.appendChild(list);
    }

    function bindTidalSearchResultControls(container) {
        container.querySelectorAll('#tidal-search-result-types .view-tab').forEach((chipEl) => {
            chipEl.addEventListener('click', () => runTidalSearch(chipEl.dataset.searchType));
        });
    }

    function renderTidalFavoriteResults(container, type, items) {
        container.innerHTML = '';
        const list = document.createElement('ul');
        list.className = type === 'tracks' ? 'streaming-results-list' : tidalLayoutClass(type);
        const queueIds = type === 'tracks' ? items.map((item) => String(item.id)).filter(Boolean) : [];
        items.forEach((item) => list.appendChild(renderSearchItem(type, item, queueIds)));
        container.appendChild(list);
    }

    function renderSearchItem(type, item, queueIds) {
        const li = document.createElement('li');
        if (type === 'artists') rememberTidalArtist(item);
        if (type === 'tracks') {
            const trackId = String(item.id);
            const isSelected = state.tidal.selectedTrackIds.has(trackId);
            li.className = 'streaming-result' + (isSelected ? ' is-selected' : '');
            li.setAttribute('data-track-id', trackId);
            li.innerHTML = trackRowHtml({
                title: escapeHtml(item.title),
                sub: escapeHtml(item.artist || ''),
                album: item.album ? escapeHtml(item.album) : '',
                thumb: tidalTrackThumbHtml(item.art_url),
                selectionButton: tidalTrackAddButtonHtml(trackId, isSelected),
                favoriteButton: favoriteButtonHtml('tracks', item.id),
                duration: formatTime(item.duration),
            });
            li.querySelector('.track-play').addEventListener('click', (event) => {
                event.stopPropagation();
                playTidalTracks(queueIds, trackId);
            });
            const addBtn = li.querySelector('.streaming-add[data-streaming-add]');
            if (addBtn) addBtn.addEventListener('click', (event) => toggleTidalPlaylistTrack(event, trackId));
            li.addEventListener('click', (event) => {
                if (event.target && event.target.closest('.streaming-add, .streaming-fav, .track-fav')) return;
                playTidalTracks(queueIds, trackId);
            });
            bindTidalFavoriteButtons(li);
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
            const onKey = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); li.click(); } };
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
            ' title="' + (isSelected ? 'Remove from selection' : 'Add to selection') + '">' + (isSelected ? '✓' : '+') + '</button>';
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
            btn.title = active ? 'Remove from selection' : 'Add to selection';
        });
        document.querySelectorAll('.streaming-result[data-track-id]').forEach((row) => {
            row.classList.toggle('is-selected', state.tidal.selectedTrackIds.has(row.dataset.trackId));
        });
        updateTidalPlaylistSaveRow();
    }

    function clearTidalPlaylistSelection() {
        state.tidal.selectedTrackIds.clear();
        const nameInput = document.getElementById('tidal-playlist-name');
        if (nameInput) nameInput.value = '';
        clearTidalPlaylistSaveError();
        syncTidalTrackSelection();
    }

    function tidalPlaylistSaveRowHtml() {
        return '<div class="playlist-save-row tidal-playlist-save-row hidden" id="tidal-playlist-save-row">' +
            '<div class="playlist-save-controls">' +
                '<input type="text" id="tidal-playlist-name" class="url-input" placeholder="New playlist name…" aria-label="New TIDAL playlist name" autocomplete="off" />' +
                '<button id="tidal-save-playlist" class="btn-secondary" type="button">Save as new</button>' +
                '<select id="tidal-playlist-target" class="url-input" aria-label="Add to existing TIDAL playlist">' +
                    '<option value="">Add to existing playlist…</option>' +
                '</select>' +
                '<button id="tidal-add-to-playlist" class="btn-secondary" type="button">Add</button>' +
                '<button id="tidal-cancel-playlist-selection" class="btn-secondary" type="button">Cancel</button>' +
            '</div>' +
            '<div class="tidal-playlist-save-error" id="tidal-playlist-save-error" hidden></div>' +
        '</div>';
    }

    function setTidalPlaylistSaveError(message) {
        const el = document.getElementById('tidal-playlist-save-error');
        if (!el) return;
        el.textContent = message || '';
        el.hidden = !message;
    }

    function clearTidalPlaylistSaveError() {
        setTidalPlaylistSaveError('');
    }

    function updateTidalPlaylistSaveRow() {
        const row = document.getElementById('tidal-playlist-save-row');
        if (!row) return;
        const hasSelection = state.tidal.selectedTrackIds.size > 0;
        row.classList.toggle('hidden', !hasSelection);
        if (!hasSelection) clearTidalPlaylistSaveError();
        if (hasSelection) void ensureTidalPlaylistTargetOptions();
    }

    function populateTidalPlaylistTarget(items) {
        const select = document.getElementById('tidal-playlist-target');
        if (!select) return;
        const current = select.value;
        const lists = Array.isArray(items) ? items : [];
        select.innerHTML = '<option value="">Add to existing playlist…</option>' +
            lists.map((item) => '<option value="' + escapeHtml(item.id) + '">' + escapeHtml(item.name || 'Playlist') + '</option>').join('');
        if (current) select.value = current;
    }

    let tidalPlaylistTargetPromise = null;
    async function ensureTidalPlaylistTargetOptions() {
        if (tidalPlaylistTargetPromise) return tidalPlaylistTargetPromise;
        const select = document.getElementById('tidal-playlist-target');
        if (!select || (select.options && select.options.length > 1)) return;
        tidalPlaylistTargetPromise = (async () => {
            try {
                const resp = await fetch('/api/streaming/tidal/playlists');
                if (!resp.ok) return;
                const items = await resp.json();
                if (Array.isArray(items)) populateTidalPlaylistTarget(items);
            } catch (e) { /* options stay minimal; new-playlist save still works */ }
            finally { tidalPlaylistTargetPromise = null; }
        })();
        return tidalPlaylistTargetPromise;
    }

    function bindTidalPlaylistSaveRow(root) {
        const row = root.querySelector('#tidal-playlist-save-row');
        if (!row) return;
        const nameInput = row.querySelector('#tidal-playlist-name');
        const saveNew = row.querySelector('#tidal-save-playlist');
        const target = row.querySelector('#tidal-playlist-target');
        const addBtn = row.querySelector('#tidal-add-to-playlist');
        const cancel = row.querySelector('#tidal-cancel-playlist-selection');
        if (saveNew) saveNew.addEventListener('click', () => void saveNewTidalPlaylist(nameInput, saveNew));
        if (addBtn) addBtn.addEventListener('click', () => void addTidalPlaylistTracks(target, addBtn));
        if (cancel) cancel.addEventListener('click', () => { clearTidalPlaylistSelection(); showToast('TIDAL selection cleared', 'info'); });
        if (nameInput) {
            nameInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (saveNew) saveNew.click();
                }
            });
        }
    }

    async function saveNewTidalPlaylist(nameInput, btn) {
        const name = (nameInput?.value || '').trim();
        const trackIds = Array.from(state.tidal.selectedTrackIds);
        if (!name) { setTidalPlaylistSaveError('Enter a playlist name'); return; }
        if (!trackIds.length) { setTidalPlaylistSaveError('Select at least one track with +'); return; }
        btn.disabled = true;
        try {
            const resp = await fetch('/api/streaming/tidal/playlists/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, track_ids: trackIds }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to save TIDAL playlist');
            clearTidalPlaylistSelection();
            showToast('Saved: ' + (data.name || name), 'success');
            // Refresh favorite ids so the new playlist heart renders active
            // immediately; reloadPlaylists re-renders the browse view.
            void loadTidalFavoriteIds(true).then(() => reloadTidalPlaylists());
        } catch (err) {
            setTidalPlaylistSaveError(friendlyError(err?.message || err));
        } finally {
            btn.disabled = false;
        }
    }

    async function addTidalPlaylistTracks(target, btn) {
        const playlistId = target?.value || '';
        const trackIds = Array.from(state.tidal.selectedTrackIds);
        if (!playlistId) { setTidalPlaylistSaveError('Choose a TIDAL playlist'); return; }
        if (!trackIds.length) { setTidalPlaylistSaveError('Select at least one track with +'); return; }
        btn.disabled = true;
        try {
            const resp = await fetch('/api/streaming/tidal/playlists/' + encodeURIComponent(playlistId) + '/tracks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track_ids: trackIds }),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) throw new Error(data.detail || 'Failed to add tracks to TIDAL playlist');
            clearTidalPlaylistSelection();
            const label = target.options?.[target.selectedIndex]?.textContent || 'playlist';
            showToast('Added ' + trackIds.length + ' track' + (trackIds.length === 1 ? '' : 's') + ' to ' + label, 'success');
            void reloadTidalPlaylists();
        } catch (err) {
            setTidalPlaylistSaveError(friendlyError(err?.message || err));
        } finally {
            btn.disabled = false;
        }
    }

    async function reloadTidalPlaylists() {
        try {
            const resp = await fetch('/api/streaming/tidal/playlists');
            if (!resp.ok) return;
            const items = await resp.json();
            state.tidal.lastItems.playlists = Array.isArray(items) ? items : [];
            populateTidalPlaylistTarget(state.tidal.lastItems.playlists);
            if (!state.tidal.searchExecuted && state.tidal.browseCategory === 'playlists' && !isTidalDetailView()) {
                renderTidalBrowseSection('playlists');
            }
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
    function ensureTidalAccountState() {
        const userId = tidalUserId();
        if (!userId || state.tidal.cacheUser === userId) return;
        state.tidal.cache = null;
        state.tidal.snapshotPromise = null;
        state.tidal.lastItems = {};
        state.tidal.favoriteIds = { tracks: new Set(), albums: new Set(), artists: new Set(), playlists: new Set() };
        state.tidal.favoritesLoaded = false;
        state.tidal.favoriteIdsPromise = null;
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
            'title="' + (active ? 'Remove from favorites' : 'Add to favorites') + '">' +
            (active ? '♥' : '♡') + '</button>';
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
            btn.innerHTML = active ? '♥' : '♡';
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.setAttribute('aria-label', active ? 'Remove from favorites' : 'Add to favorites');
            btn.title = active ? 'Remove from favorites' : 'Add to favorites';
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
            'title="' + (active ? 'Remove from favorites' : 'Add to favorites') + '">' +
            (active ? '♥' : '♡') + '</button>';
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
            btn.textContent = active ? '♥' : '♡';
            btn.setAttribute('aria-pressed', active ? 'true' : 'false');
            btn.setAttribute('aria-label', active ? 'Remove from favorites' : 'Add to favorites');
            btn.title = active ? 'Remove from favorites' : 'Add to favorites';
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
            if (!resp.ok) throw new Error(data.detail || 'Failed to update favorite');
            if (data.favorite) state.tidal.favoriteIds[type].add(idStr);
            else state.tidal.favoriteIds[type].delete(idStr);
            syncTidalFavoriteButtons(type, idStr);
            syncFavoriteDetailButtons(type, idStr);
            notifyFavoritesChanged();
            showToast(data.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
            // The favorites list must stay authoritative: refresh it so an
            // unfavorited item leaves and a newly favorited item appears.
            if (state.tidal.view !== 'album' && state.tidal.view !== 'playlist' && state.tidal.view !== 'artist' && !state.tidal.searchExecuted) {
                if (state.tidal.browseCategory === 'playlists') renderTidalBrowseSection('playlists');
                else loadTidalFavorites();
            }
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    // -- favorites ------------------------------------------------------------
    // Tracks / Albums / Artists render the favorite lists of the selected
    // category (the category itself is the top navigation row); Playlists has
    // its own dedicated section.
    function renderTidalFavorites(body, type) {
        state.tidal.browseCategory = type;
        body.innerHTML = '<div class="streaming-results" id="tidal-fav-results"></div>';
        loadTidalFavorites();
    }

    function renderTidalFavoritesContent(results, type, items) {
        if (!Array.isArray(items) || !items.length) {
            results.innerHTML = contentState('empty', 'No favorites yet.');
            return;
        }
        renderTidalFavoriteResults(results, type, items);
    }

    async function loadTidalFavorites() {
        const results = document.getElementById('tidal-fav-results');
        if (!results) return;
        const type = state.tidal.browseCategory;
        ensureTidalAccountState();
        // Render the last-known payload instantly; the live refresh below
        // replaces it in place when fresh data arrives.
        const cached = state.tidal.lastItems[type];
        let rendered = false;
        if (cached !== undefined) {
            renderTidalFavoritesContent(results, type, cached);
            rendered = true;
        } else {
            results.innerHTML = contentState('loading', 'Loading…');
        }
        try {
            // Adopt last-known ids (hearts) before the authoritative load.
            if (state.tidal.favoritesLoaded !== true) await loadTidalSnapshot();
            if (state.tidal.searchExecuted || state.tidal.browseCategory !== type) return;
            // The snapshot may have arrived while we waited: render it now so
            // the last-known library appears before the live refresh returns.
            if (!rendered) {
                const late = state.tidal.lastItems[type];
                if (late !== undefined) {
                    renderTidalFavoritesContent(results, type, late);
                    rendered = true;
                }
            }
            // Re-read the real TIDAL favorite state so external app changes
            // appear on refresh (no shadow state).
            await loadTidalFavoriteIds(true);
            if (state.tidal.searchExecuted || state.tidal.browseCategory !== type) return;
            const resp = await fetch('/api/streaming/tidal/favorites?type=' + encodeURIComponent(type) + '&limit=50');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (state.tidal.searchExecuted || state.tidal.browseCategory !== type) return;
            state.tidal.lastItems[type] = Array.isArray(items) ? items : [];
            renderTidalFavoritesContent(results, type, state.tidal.lastItems[type]);
        } catch (err) {
            // A failed refresh must never empty the visible library: keep the
            // content that is already rendered (cache or earlier load).
            if (!rendered) {
                results.innerHTML = contentState('error', friendlyError(err?.message || err));
            }
        }
    }

    // -- playlists ------------------------------------------------------------
    async function renderTidalPlaylists(body) {
        body.innerHTML = '<div class="streaming-results" id="tidal-playlists-results"></div>';
        const results = body.querySelector('#tidal-playlists-results');
        ensureTidalAccountState();
        const cached = state.tidal.lastItems.playlists;
        let rendered = false;
        if (cached !== undefined) {
            renderTidalPlaylistsContent(results, cached);
            rendered = true;
        } else {
            results.innerHTML = contentState('loading', 'Loading…');
        }
        try {
            if (state.tidal.favoritesLoaded !== true) await loadTidalSnapshot();
            if (state.tidal.searchExecuted || state.tidal.browseCategory !== 'playlists') return;
            if (!rendered) {
                const late = state.tidal.lastItems.playlists;
                if (late !== undefined) {
                    renderTidalPlaylistsContent(results, late);
                    rendered = true;
                }
            }
            const resp = await fetch('/api/streaming/tidal/playlists');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (state.tidal.searchExecuted || state.tidal.browseCategory !== 'playlists') return;
            state.tidal.lastItems.playlists = Array.isArray(items) ? items : [];
            renderTidalPlaylistsContent(results, state.tidal.lastItems.playlists);
        } catch (err) {
            // Keep the already-rendered playlists on a failed refresh.
            if (!rendered) {
                results.innerHTML = contentState('error', friendlyError(err?.message || err));
            }
        }
    }

    function renderTidalPlaylistsContent(results, items) {
        if (!Array.isArray(items) || !items.length) {
            results.innerHTML = contentState('empty', 'No playlists yet.');
            return;
        }
        populateTidalPlaylistTarget(items);
        const list = document.createElement('ul');
        list.className = tidalLayoutClass('playlists');
        for (const item of items) {
            const li = document.createElement('li');
            li.className = 'album-card';
            li.setAttribute('role', 'button');
            li.setAttribute('tabindex', '0');
            const sub = item.track_count ? item.track_count + ' tracks' : 'Playlist';
            li.innerHTML =
                '<div class="album-art-wrap">' + tidalCardArt(item.art_url, item.name || 'Playlist') + '</div>' +
                favoriteButtonHtml('playlists', item.id) +
                '<div class="album-name">' + escapeHtml(item.name) + '</div>' +
                '<div class="album-artist">' + escapeHtml(sub) + '</div>';
            const open = () => openTidalPlaylist(item.id, item.name, item.art_url);
            li.addEventListener('click', open);
            li.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } });
            bindTidalFavoriteButtons(li);
            list.appendChild(li);
        }
        results.innerHTML = '';
        results.appendChild(list);
    }

    // -- album / playlist detail ----------------------------------------------
    function openTidalDetail(view, id, title, artUrl) {
        // Push the full current detail state so Back returns exactly through
        // nested details (browse -> artist -> album) with the previous view's
        // own id/title/art restored, instead of jumping straight to browse.
        state.tidal.detailRequestId += 1;
        state.tidal.viewStack.push({
            view: state.tidal.view,
            detailId: state.tidal.detailId,
            detailTitle: state.tidal.detailTitle,
            detailArt: state.tidal.detailArt,
        });
        state.tidal.view = view;
        state.tidal.detailId = id;
        state.tidal.detailTitle = title;
        state.tidal.detailArt = artUrl || '';
        const entry = entryFor('tidal');
        if (entry) renderTidalBrowse(entry);
    }

    function openTidalAlbum(id, title, artUrl) {
        openTidalDetail('album', id, title, artUrl);
    }

    function openTidalPlaylist(id, title, artUrl) {
        openTidalDetail('playlist', id, title, artUrl);
    }

    function openTidalArtist(id, name, artUrl) {
        rememberTidalArtist({ id: id, name: name, art_url: artUrl });
        openTidalDetail('artist', id, name, artUrl);
    }

    function closeTidalDetail() {
        state.tidal.detailRequestId += 1;
        const previous = state.tidal.viewStack.pop();
        state.tidal.view = previous ? previous.view : null;
        state.tidal.detailId = previous ? previous.detailId : null;
        state.tidal.detailTitle = previous ? previous.detailTitle : '';
        state.tidal.detailArt = previous ? previous.detailArt || '' : '';
        renderTidalBrowse(entryFor('tidal'));
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
        // as the same collapsible library "About" component.
        content.innerHTML =
            '<div class="streaming-detail streaming-detail--hero tidal-detail">' +
                '<div class="streaming-detail-header detail-hero-header tidal-detail-header">' +
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
                tidalPlaylistSaveRowHtml() +
                '<div class="streaming-results" id="tidal-detail-results">' + contentState('loading', 'Loading…') + '</div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteDetailButtons(content);
        bindTidalPlaylistSaveRow(content);
        updateTidalPlaylistSaveRow();
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
        const primary = [
            meta.year ? String(meta.year) : '',
            tidalQualityLabel(meta.audio_quality),
            meta.num_tracks ? (meta.num_tracks + ' tracks') : '',
        ].filter(Boolean);
        if (primary.length) lines.push(primary.join(' · '));
        const supp = (enrichment && enrichment.supplement) || {};
        const headline = [supp.release_type, supp.country].filter(Boolean).join(' · ');
        if (headline) lines.push(headline);
        if (supp.label) lines.push('Label: ' + supp.label);
        const genres = (supp.genres || []).slice(0, 3).filter(Boolean);
        if (genres.length) lines.push('Genre: ' + genres.join(' / '));
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
            if (hasSimilar) void hydrateSimilarArtistImages(results, similar, requestId, 'album');
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
                tidalPlaylistSaveRowHtml() +
                '<div class="streaming-results" id="tidal-detail-results">' + contentState('loading', 'Loading…') + '</div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteDetailButtons(content);
        bindTidalPlaylistSaveRow(content);
        updateTidalPlaylistSaveRow();
        loadTidalArtist(content, requestId);
    }

    // Directly-visible artist about, compact in the hero. Long bios are clamped
    // to a few lines with a subtle More/Less toggle instead of an accordion.
    function renderArtistAbout(element, about) {
        if (!element) return;
        const text = (about || '').trim();
        if (!text) {
            element.hidden = true;
            element.innerHTML = '';
            return;
        }
        const long = text.length > 200;
        element.hidden = false;
        element.innerHTML =
            '<p class="streaming-about-text' + (long ? ' is-clamped' : '') + '">' + escapeHtml(text) + '</p>' +
            (long ? '<button type="button" class="about-more-toggle" aria-expanded="false">More</button>' : '');
        if (long) {
            const bio = element.querySelector('.streaming-about-text');
            const toggle = element.querySelector('.about-more-toggle');
            toggle.addEventListener('click', () => {
                const clamped = bio.classList.toggle('is-clamped');
                toggle.setAttribute('aria-expanded', clamped ? 'false' : 'true');
                toggle.textContent = clamped ? 'More' : 'Less';
            });
        }
    }

    async function loadTidalArtist(content, requestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/artists/' + encodeURIComponent(state.tidal.detailId));
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const data = await resp.json();
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'artist') return;
            rememberTidalArtist(data);
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
            const tracks = Array.isArray(data.top_tracks) ? data.top_tracks : [];
            const albums = Array.isArray(data.albums) ? data.albums : [];
            if (tracks.length) {
                const heading = document.createElement('h4');
                heading.className = 'streaming-results-heading';
                heading.textContent = 'Top Tracks';
                results.appendChild(heading);
                renderDetailTracks(results, tracks, null);
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
            const similar = (data.enrichment && data.enrichment.similar) || [];
            const hasSimilar = renderSimilarArtists(results, similar);
            if (hasSimilar) void hydrateSimilarArtistImages(results, similar, requestId, 'artist');
            if (!tracks.length && !albums.length && !hasSimilar) {
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
    function renderSimilarArtistItem(item) {
        const li = document.createElement('li');
        li.className = 'streaming-similar-item';
        li.setAttribute('role', 'button');
        li.setAttribute('tabindex', '0');
        li.setAttribute('aria-label', 'Open artist ' + (item.artist || ''));
        li.innerHTML =
            '<div class="streaming-result-cover">' + coverImg(item.art_url, 'eager') + '</div>' +
            '<span class="streaming-similar-name">' + escapeHtml(item.artist || 'Unknown artist') + '</span>';
        li.addEventListener('click', () => openTidalSimilarArtist(item));
        li.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                openTidalSimilarArtist(item);
            }
        });
        return li;
    }

    function renderSimilarArtists(container, items) {
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
            const card = renderSimilarArtistItem(item);
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
    function hydrateSimilarArtistImages(container, items, requestId, view) {
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
                if (requestId !== state.tidal.detailRequestId || state.tidal.view !== view) return;
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
        if (item && item.provider_artist_id) {
            openTidalArtist(item.provider_artist_id, item.artist || '', item.art_url || '');
            return;
        }
        const name = (item && item.artist) || '';
        if (!name) return;
        void resolveTidalArtistByName(name, state.tidal.detailRequestId);
    }

    async function resolveTidalArtistByName(name, detailRequestId) {
        try {
            const match = await resolveTidalArtistMatch(name);
            if (detailRequestId !== state.tidal.detailRequestId || !isTidalDetailView()) return;
            if (match && !match.ambiguous) {
                openTidalArtist(match.id, match.name || name, match.art_url || '');
                return;
            }
            throw new Error('no unique artist match');
        } catch (err) {
            if (detailRequestId !== state.tidal.detailRequestId || !isTidalDetailView()) return;
            showTidalSearchResultsFor(name);
        }
    }

    function tidalNameKey(value) {
        return String(value == null ? '' : value).normalize('NFKC').toLowerCase()
            .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    }

    // Reuse the existing TIDAL executed-search flow: close the detail, seed the
    // permanent search bar with the artist name and render artist results.
    function showTidalSearchResultsFor(name) {
        state.tidal.detailRequestId += 1;
        state.tidal.view = null;
        state.tidal.viewStack = [];
        state.tidal.searchQuery = name;
        state.tidal.searchExecuted = true;
        state.tidal.searchResultType = 'artists';
        state.tidal.searchResults = null;
        state.tidal.searchInFlight = false;
        cancelTidalSearchDebounce();
        const entry = entryFor('tidal');
        if (entry) {
            renderTidalBrowse(entry);
        } else {
            void executeTidalSearch('artists');
        }
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
                tidalPlaylistSaveRowHtml() +
                '<div class="streaming-results" id="tidal-detail-results">' + contentState('loading', 'Loading…') + '</div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteDetailButtons(content);
        bindTidalPlaylistSaveRow(content);
        updateTidalPlaylistSaveRow();
        loadTidalPlaylistTracks(content, requestId);
    }

    async function loadTidalAlbumTracks(content, requestId = state.tidal.detailRequestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId) + '/tracks');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            renderDetailTracks(results, items);
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            results.innerHTML = contentState('error', friendlyError(err?.message || err));
        }
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
            const li = document.createElement('li');
            const trackId = String(item.id);
            const isSelected = state.tidal.selectedTrackIds.has(trackId);
            li.className = 'streaming-result' + (isSelected ? ' is-selected' : '');
            li.setAttribute('data-track-id', trackId);
            li.innerHTML = trackRowHtml({
                index: index + 1,
                title: escapeHtml(item.title),
                sub: escapeHtml(item.artist || ''),
                thumb: tidalTrackThumbHtml(item.art_url),
                selectionButton: tidalTrackAddButtonHtml(trackId, isSelected),
                favoriteButton: favoriteButtonHtml('tracks', item.id, 'track-fav'),
                duration: formatTime(item.duration),
            });
            li.querySelector('.track-play').addEventListener('click', (event) => {
                event.stopPropagation();
                playTidalTracks(ids, trackId);
            });
            const addBtn = li.querySelector('.streaming-add[data-streaming-add]');
            if (addBtn) addBtn.addEventListener('click', (event) => toggleTidalPlaylistTrack(event, trackId));
            li.addEventListener('click', (event) => {
                if (event.target && event.target.closest('.streaming-add, .streaming-fav, .track-fav')) return;
                playTidalTracks(ids, trackId);
            });
            bindTidalFavoriteButtons(li);
            list.appendChild(li);
        });
        container.appendChild(list);
    }

    // -- play (native FXRoute queue owner) ------------------------------------
    async function playTidalTracks(trackIds, startId) {
        try {
            const resp = await fetch('/api/play', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: 'tidal', track_id: startId, queue_track_ids: trackIds }),
            });
            const data = await resp.json().catch(() => null);
            if (!resp.ok) {
                showToast(friendlyError(data?.detail || 'Playback failed'), 'error');
                return;
            }
            void refreshTidalStatus();
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    // -----------------------------------------------------------------------
    // Public surface
    // -----------------------------------------------------------------------
    window.FXRouteStreaming = {
        init,
        renderProvider,
        notifyPlayback,
        onTabVisible,
        refreshActiveTab: () => { if (window.__visibleTab === 'qobuz' || window.__visibleTab === 'tidal') void refreshProvider(window.__visibleTab); },
        // Footer / global favorite hooks: the shared footer heart favorites the
        // current TIDAL track through the same canonical state and API the
        // TIDAL tab and detail rows use — no second favorite source.
        isTidalFavorite,
        tidalFavoritesReady,
        ensureTidalFavoritesLoaded,
        toggleTidalFavorite,
        // Shared grid/list layout control (library toggle bridges here).
        setTidalLayout,
    };
})();
