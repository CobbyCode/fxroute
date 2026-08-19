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
    // Shared detail track-row builder, supplied by app.js so the library album
    // detail and the Tidal album/playlist details render the same row.
    let trackRowHtml = function () { return ''; };
    let initialized = false;

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
            trackSelectionMode: false,
            selectedTrackIds: new Set(),
            detailRequestId: 0,
            favoritesType: 'tracks',
            detailId: null,
            detailTitle: '',
            detailArt: '',
            contentKey: null,   // availability/auth mode last rendered into .streaming-content
            browseSection: 'favorites',   // 'favorites' | 'playlists'; search results are a separate temporary overlay
            favoriteIds: { tracks: new Set(), albums: new Set(), artists: new Set(), playlists: new Set() },
            favoriteIdsPromise: null,
            favoritesLoaded: false,   // true after at least one successful favorites/ids load
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
        if (typeof api.trackRowHtml === 'function') trackRowHtml = api.trackRowHtml;
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
        else if (tabId === 'tidal') startPoll('tidal', TIDAL_POLL_INTERVAL_MS);
    }

    // -----------------------------------------------------------------------
    // DOM construction
    // -----------------------------------------------------------------------
    function buildProviderDom() {
        document.querySelectorAll('.streaming-shell[data-provider]').forEach((root) => {
            const providerId = root.getAttribute('data-provider');
            root.innerHTML =
                '<div class="streaming-provider">' +
                    '<div class="streaming-status-line tidal-status-slot" hidden></div>' +
                    '<div class="streaming-empty" hidden>' +
                        '<div class="streaming-empty-icon" aria-hidden="true"></div>' +
                        '<h2 class="streaming-empty-title"></h2>' +
                        '<p class="streaming-empty-msg"></p>' +
                        '<div class="streaming-empty-actions"></div>' +
                    '</div>' +
                    '<div class="streaming-now-playing" hidden>' +
                        '<div class="streaming-card-header">' +
                            '<span class="streaming-provider-name"></span>' +
                            '<div class="streaming-status-chip" hidden></div>' +
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
                statusLine: root.querySelector('.streaming-status-line'),
                providerName: root.querySelector('.streaming-provider-name'),
                empty: root.querySelector('.streaming-empty'),
                emptyIcon: root.querySelector('.streaming-empty-icon'),
                emptyTitle: root.querySelector('.streaming-empty-title'),
                emptyMsg: root.querySelector('.streaming-empty-msg'),
                emptyActions: root.querySelector('.streaming-empty-actions'),
                nowPlaying: root.querySelector('.streaming-now-playing'),
                statusChip: root.querySelector('.streaming-status-chip'),
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
    // Status line (backend / connected / authenticated)
    // -----------------------------------------------------------------------
    // Provider status bits (visible text) and the detail string kept in the
    // chip tooltip. Backend implementation names are never surfaced.
    function buildStatusBits(providerId, data) {
        const bits = [];
        if (providerId === 'spotify') {
            bits.push('Connected');
        } else if (providerId === 'qobuz') {
            if (data.connected || data.authenticated === true) bits.push('Connected');
        } else if (providerId === 'tidal') {
            bits.push(PROVIDER_META.tidal.name);
            if (data.authenticated === true) bits.push('Connected');
        }
        return bits;
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
    // authenticated the line (and the login surface) still render.
    function isTidalDetailView() {
        return state.tidal.view === 'album' || state.tidal.view === 'playlist' || state.tidal.view === 'artist';
    }

    // Catalog providers keep the standalone line; the in-tab player card is
    // not rendered for them (the footer is the player). Shared by the status
    // render path and by browse/detail navigation, so the line reflects the
    // current view immediately instead of waiting for the next poll.
    function applyCatalogStatusLine(entry, providerId, data) {
        const bits = buildStatusBits(providerId, data);
        entry.els.statusLine.textContent = bits.join(' · ');
        entry.els.statusLine.title = '';
        const healthyDetail = providerId === 'tidal' && isTidalDetailView() && data.authenticated === true;
        entry.els.statusLine.hidden = bits.length === 0 || healthyDetail;
    }

    function renderStatusLine(providerId, entry, data) {
        const catalogProvider = PROVIDER_META[providerId]?.catalog === true;
        if (catalogProvider) {
            applyCatalogStatusLine(entry, providerId, data);
            return;
        }
        // Player providers integrate the status into the card as a small
        // top-right chip; the standalone line above the card is gone. The
        // provider/backend detail stays available via the chip tooltip.
        const bits = buildStatusBits(providerId, data);
        const text = bits.length ? '● ' + bits.join(' · ') : '';
        entry.els.statusChip.hidden = !text;
        entry.els.statusChip.textContent = text;
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

        // Stopped with no track.
        if ((data.status === 'Stopped' || !data.status) && !data.title) {
            showEmpty(entry, notPlayingMessage(providerId), '');
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

    function notPlayingMessage(providerId) {
        if (providerId === 'spotify') return 'Spotify is not running.';
        if (providerId === 'qobuz') return 'Nothing is playing. Start a track from the Qobuz app.';
        return 'Nothing is playing.';
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
            const resp = await fetch('/api/streaming/providers');
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

    // -- browse (search / favorites / playlists) ------------------------------
    function renderTidalBrowse(entry) {
        const content = entry.els.content;
        // Keep the status line consistent while navigating between the main
        // surface and detail views (the poll refreshes it as well).
        applyCatalogStatusLine(entry, 'tidal', state.lastData.tidal || {});
        if (state.tidal.view === 'album') { renderTidalAlbum(content); return; }
        if (state.tidal.view === 'playlist') { renderTidalPlaylist(content); return; }
        if (state.tidal.view === 'artist') { renderTidalArtist(content); return; }
        // The search bar is a permanent part of the browse surface, above the
        // navigation. Search results only ever replace the browse body; the bar
        // itself survives every status refresh (contentKey guard in
        // renderTidalContent), so a poll never resets an in-progress query.
        content.innerHTML =
            '<div class="streaming-browse">' +
                '<div class="tidal-toolbar">' +
                    '<h2 class="section-title tidal-toolbar-title">Tidal</h2>' +
                    '<div class="streaming-search">' +
                        '<div class="streaming-search-row">' +
                            '<input type="search" class="streaming-search-input" id="tidal-search-input" placeholder="Search" autocomplete="off" />' +
                            '<button type="button" class="btn-secondary" id="tidal-search-btn">Search</button>' +
                        '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="streaming-browse-tabs" role="tablist">' +
                    '<button type="button" class="streaming-browse-tab' + (state.tidal.browseSection === 'favorites' ? ' is-active' : '') + '" data-browse="favorites">Favorites</button>' +
                    '<button type="button" class="streaming-browse-tab' + (state.tidal.browseSection === 'playlists' ? ' is-active' : '') + '" data-browse="playlists">Playlists</button>' +
                '</div>' +
                '<div class="streaming-browse-body" id="tidal-browse-body"></div>' +
            '</div>';
        const toolbar = content.querySelector('.tidal-toolbar');
        if (toolbar && entry.els.statusLine) toolbar.appendChild(entry.els.statusLine);
        bindTidalSearchBar(content);
        const input = content.querySelector('#tidal-search-input');
        if (input && state.tidal.searchExecuted) input.value = state.tidal.searchQuery;
        const tabs = content.querySelectorAll('.streaming-browse-tab');
        tabs.forEach((tab) => tab.addEventListener('click', () => {
            tabs.forEach((t) => t.classList.toggle('is-active', t === tab));
            resetTidalSearch();
            renderTidalBrowseSection(tab.dataset.browse);
        }));
        if (state.tidal.searchExecuted && state.tidal.searchResults) {
            renderTidalSearchResults(document.getElementById('tidal-browse-body'), state.tidal.searchResults);
        } else {
            renderTidalBrowseSection(state.tidal.browseSection);
        }
    }

    function renderTidalBrowseSection(section) {
        const body = document.getElementById('tidal-browse-body');
        if (!body) return;
        state.tidal.browseSection = section;
        if (section === 'favorites') renderTidalFavorites(body);
        else if (section === 'playlists') renderTidalPlaylists(body);
    }

    // -- search (permanent bar above the browse navigation) ------------------
    // Executing a search replaces the browse body with results; clearing it
    // returns to the current browse section (Favorites by default). The bar
    // lives in the browse surface, so the contentKey guard keeps a running
    // search (and its results) intact across status refreshes.
    function bindTidalSearchBar(root) {
        const input = root.querySelector('#tidal-search-input');
        const doSearch = async () => {
            const query = (input.value || '').trim();
            if (!query) {
                clearTidalSearch();
                return;
            }
            state.tidal.searchQuery = query;
            state.tidal.searchExecuted = true;
            await executeTidalSearch(state.tidal.searchResultType);
        };
        root.querySelector('#tidal-search-btn').addEventListener('click', doSearch);
        input.addEventListener('input', () => {
            if ((input.value || '').trim() || !state.tidal.searchExecuted) return;
            clearTidalSearch();
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                doSearch();
            } else if (e.key === 'Escape') {
                e.preventDefault();
                clearTidalSearch();
            }
        });
    }

    function resetTidalSearch() {
        // Invalidate a response that is still resolving after clear/navigation.
        state.tidal.searchRequestId += 1;
        state.tidal.searchQuery = '';
        state.tidal.searchExecuted = false;
        state.tidal.searchResults = null;
        state.tidal.trackSelectionMode = false;
        state.tidal.selectedTrackIds.clear();
        const input = document.getElementById('tidal-search-input');
        if (input) input.value = '';
    }

    function clearTidalSearch() {
        resetTidalSearch();
        renderTidalBrowseSection(state.tidal.browseSection);
    }

    function chip(type, label, active) {
        return '<button type="button" class="streaming-chip' + (active ? ' is-active' : '') + '" data-type="' + type + '">' + escapeHtml(label) + '</button>';
    }

    const TIDAL_SEARCH_TYPES = ['artists', 'tracks', 'albums', 'playlists'];
    const TIDAL_SEARCH_TYPE_LABELS = { artists: 'Artists', tracks: 'Tracks', albums: 'Albums', playlists: 'Playlists' };

    async function executeTidalSearch(type) {
        if (!state.tidal.searchExecuted || !state.tidal.searchQuery) return;
        const body = document.getElementById('tidal-browse-body');
        if (!body) return;
        const query = state.tidal.searchQuery;
        const requestId = ++state.tidal.searchRequestId;
        state.tidal.searchResultType = type;
        state.tidal.trackSelectionMode = false;
        state.tidal.selectedTrackIds.clear();
        body.innerHTML = '<p class="streaming-note">Searching…</p>';
        try {
            const resp = await fetch('/api/streaming/tidal/search?q=' + encodeURIComponent(query) + '&types=' + encodeURIComponent(type) + '&limit=25');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const data = await resp.json();
            if (requestId !== state.tidal.searchRequestId) return;
            state.tidal.searchResults = data;
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts render unfilled until state loads */ }
            if (requestId === state.tidal.searchRequestId) {
                const currentBody = document.getElementById('tidal-browse-body');
                if (currentBody) renderTidalSearchResults(currentBody, data);
            }
        } catch (err) {
            if (requestId !== state.tidal.searchRequestId) return;
            const currentBody = document.getElementById('tidal-browse-body');
            if (currentBody) currentBody.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
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
        const typeButtons = TIDAL_SEARCH_TYPES.map((value) =>
            '<button type="button" class="streaming-chip' + (value === type ? ' is-active' : '') + '" id="tidal-search-type-' + value + '" data-search-type="' + value + '">' + TIDAL_SEARCH_TYPE_LABELS[value] + '</button>'
        ).join('');
        const selectionControls = type === 'tracks'
            ? '<div class="tidal-track-selection" id="tidal-track-selection-controls">' +
                '<button type="button" class="btn-ghost" id="tidal-track-selection-toggle">Select</button>' +
                (state.tidal.trackSelectionMode
                    ? '<button type="button" class="btn-ghost" id="tidal-select-all">Select all</button>' +
                      '<button type="button" class="btn-ghost" id="tidal-clear-selection">Clear</button>' +
                      '<button type="button" class="btn-primary" id="tidal-play-selected"' + (state.tidal.selectedTrackIds.size ? '' : ' disabled') + '>Play selected</button>'
                    : '') +
              '</div>'
            : '';
        container.innerHTML =
            '<div class="tidal-search-results-header">' +
                '<h3 class="streaming-results-title">Search results for &quot;' + escapeHtml(state.tidal.searchQuery) + '&quot;</h3>' +
                '<div class="streaming-chip-row" id="tidal-search-result-types" aria-label="Search result type">' + typeButtons + '</div>' +
                selectionControls +
            '</div>' +
            '<div class="streaming-results" id="tidal-search-items"></div>';
        const itemsContainer = container.querySelector('#tidal-search-items');
        bindTidalSearchResultControls(container, items);
        if (!items.length) {
            itemsContainer.innerHTML = '<p class="streaming-note">No results.</p>';
            return;
        }
        const queueIds = type === 'tracks' ? items.map((item) => String(item.id)).filter(Boolean) : [];
        const list = document.createElement('ul');
        list.className = 'streaming-results-list';
        items.forEach((item) => list.appendChild(renderSearchItem(type, item, queueIds)));
        itemsContainer.appendChild(list);
    }

    function bindTidalSearchResultControls(container, items) {
        container.querySelectorAll('#tidal-search-result-types .streaming-chip').forEach((chipEl) => {
            chipEl.addEventListener('click', () => runTidalSearch(chipEl.dataset.searchType));
        });
        if (state.tidal.searchResultType !== 'tracks') return;
        const selectToggle = container.querySelector('#tidal-track-selection-toggle');
        if (selectToggle) {
            selectToggle.addEventListener('click', () => {
                state.tidal.trackSelectionMode = true;
                renderTidalSearchResults(container, state.tidal.searchResults || {});
            });
        }
        const selectAll = container.querySelector('#tidal-select-all');
        if (selectAll) {
            selectAll.addEventListener('click', () => {
                state.tidal.selectedTrackIds = new Set(items.map((item) => String(item.id)));
                renderTidalSearchResults(container, state.tidal.searchResults || {});
            });
        }
        const clear = container.querySelector('#tidal-clear-selection');
        if (clear) {
            clear.addEventListener('click', () => {
                state.tidal.selectedTrackIds.clear();
                renderTidalSearchResults(container, state.tidal.searchResults || {});
            });
        }
        const playSelected = container.querySelector('#tidal-play-selected');
        if (playSelected) {
            playSelected.addEventListener('click', () => {
                const selectedIds = items.map((item) => String(item.id)).filter((id) => state.tidal.selectedTrackIds.has(id));
                if (selectedIds.length) void playTidalTracks(selectedIds, selectedIds[0]);
            });
        }
    }

    function renderTidalFavoriteResults(container, type, items) {
        container.innerHTML = '';
        const list = document.createElement('ul');
        list.className = 'streaming-results-list';
        const queueIds = type === 'tracks' ? items.map((item) => String(item.id)).filter(Boolean) : [];
        items.forEach((item) => list.appendChild(renderSearchItem(type, item, queueIds)));
        container.appendChild(list);
    }

    function renderSearchItem(type, item, queueIds) {
        const li = document.createElement('li');
        li.className = 'streaming-result';
        if (type === 'tracks') {
            const trackId = String(item.id);
            li.innerHTML =
                (state.tidal.trackSelectionMode ? '<input type="checkbox" class="tidal-track-select"' + (state.tidal.selectedTrackIds.has(trackId) ? ' checked' : '') + ' aria-label="Select track" />' : '') +
                '<button type="button" class="streaming-result-play" title="Play">▶</button>' +
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.title) + '</div>' +
                    '<div class="streaming-result-sub">' + escapeHtml(item.artist || '') + '</div>' +
                '</div>' +
                '<div class="streaming-result-album">' + escapeHtml(item.album || '') + '</div>' +
                favoriteButtonHtml('tracks', item.id) +
                '<div class="streaming-result-duration">' + formatTime(item.duration) + '</div>';
            li.querySelector('.streaming-result-play').addEventListener('click', (event) => {
                event.stopPropagation();
                playTidalTracks(queueIds, trackId);
            });
            if (state.tidal.trackSelectionMode) {
                const select = li.querySelector('.tidal-track-select');
                select.addEventListener('click', (event) => event.stopPropagation());
                select.addEventListener('change', (event) => {
                    const id = trackId;
                    if (event.target.checked) state.tidal.selectedTrackIds.add(id);
                    else state.tidal.selectedTrackIds.delete(id);
                    renderTidalSearchResults(document.getElementById('tidal-browse-body'), state.tidal.searchResults || {});
                });
            }
            li.addEventListener('click', () => playTidalTracks(queueIds, trackId));
            bindTidalFavoriteButtons(li);
        } else if (type === 'albums') {
            li.innerHTML =
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.title) + '</div>' +
                    '<div class="streaming-result-sub">' + escapeHtml(item.artist || '') + '</div>' +
                '</div>' +
                favoriteButtonHtml('albums', item.id);
            li.addEventListener('click', () => openTidalAlbum(item.id, item.title, item.art_url));
            bindTidalFavoriteButtons(li);
        } else if (type === 'artists') {
            li.innerHTML =
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.name) + '</div>' +
                '</div>' +
                favoriteButtonHtml('artists', item.id);
            li.addEventListener('click', () => openTidalArtist(item.id, item.name, item.art_url));
            bindTidalFavoriteButtons(li);
        } else if (type === 'playlists') {
            li.innerHTML =
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.name) + '</div>' +
                    '<div class="streaming-result-sub">' + (item.track_count ? item.track_count + ' tracks' : '') + '</div>' +
                '</div>' +
                favoriteButtonHtml('playlists', item.id);
            li.addEventListener('click', () => openTidalPlaylist(item.id, item.name, item.art_url));
            bindTidalFavoriteButtons(li);
        }
        return li;
    }

    function coverImg(url) {
        if (!url) return '';
        // A cover that fails to load (stale/missing TIDAL picture) removes
        // itself so the row falls back to the neutral :empty placeholder
        // instead of showing a broken-image icon.
        return '<img src="' + escapeHtml(url) + '" alt="" loading="lazy" onerror="this.remove()" />';
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

    // Detail headers (album + playlist) use the library's star favorite (not a
    // heart). It shares the same authoritative favorites state as the hearts.
    function favoriteStarHtml(type) {
        const idStr = String(state.tidal.detailId || '');
        const active = isTidalFavorite(type, idStr);
        return '<button type="button" class="album-favorite-toggle' + (active ? ' active' : '') + '" ' +
            'data-fav-type="' + type + '" data-fav-id="' + escapeHtml(idStr) + '" ' +
            'aria-pressed="' + (active ? 'true' : 'false') + '" ' +
            'aria-label="' + (active ? 'Remove from favorites' : 'Add to favorites') + '" ' +
            'title="' + (active ? 'Remove from favorites' : 'Add to favorites') + '">' +
            (active ? '★' : '☆') + '</button>';
    }

    function bindFavoriteStars(container) {
        container.querySelectorAll('.album-favorite-toggle[data-fav-type]').forEach((btn) => {
            btn.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleTidalFavorite(btn.dataset.favType, btn.dataset.favId);
            });
        });
    }

    function syncFavoriteStars(type, idStr) {
        const active = state.tidal.favoriteIds[type].has(idStr);
        document.querySelectorAll('.album-favorite-toggle[data-fav-type="' + type + '"][data-fav-id="' + idStr + '"]').forEach((btn) => {
            btn.classList.toggle('active', active);
            btn.textContent = active ? '★' : '☆';
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
            syncFavoriteStars(type, idStr);
            notifyFavoritesChanged();
            showToast(data.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
            // The favorites list must stay authoritative: refresh it so an
            // unfavorited item leaves and a newly favorited item appears.
            if (state.tidal.view !== 'album' && state.tidal.view !== 'playlist' && state.tidal.view !== 'artist' && !state.tidal.searchExecuted) {
                if (state.tidal.browseSection === 'favorites') loadTidalFavorites();
                else if (state.tidal.browseSection === 'playlists') renderTidalBrowseSection('playlists');
            }
        } catch (err) {
            showToast(friendlyError(err?.message || err), 'error');
        }
    }

    // -- favorites ------------------------------------------------------------
    function renderTidalFavorites(body) {
        body.innerHTML =
            '<div class="streaming-chip-row" id="tidal-fav-types" aria-label="Favorites category">' +
                chip('tracks', 'Tracks', state.tidal.favoritesType === 'tracks') +
                chip('albums', 'Albums', state.tidal.favoritesType === 'albums') +
                chip('artists', 'Artists', state.tidal.favoritesType === 'artists') +
            '</div>' +
            '<div class="streaming-results" id="tidal-fav-results"></div>';

        body.querySelectorAll('#tidal-fav-types .streaming-chip').forEach((chipEl) => {
            chipEl.addEventListener('click', () => {
                body.querySelectorAll('#tidal-fav-types .streaming-chip').forEach((c) => c.classList.toggle('is-active', c === chipEl));
                state.tidal.favoritesType = chipEl.dataset.type;
                loadTidalFavorites();
            });
        });
        loadTidalFavorites();
    }

    async function loadTidalFavorites() {
        const results = document.getElementById('tidal-fav-results');
        if (!results) return;
        const type = state.tidal.favoritesType;
        results.innerHTML = '<p class="streaming-note">Loading…</p>';
        try {
            // Re-read the real TIDAL favorite state so external app changes
            // appear on refresh (no shadow state).
            await loadTidalFavoriteIds(true);
            const resp = await fetch('/api/streaming/tidal/favorites?type=' + encodeURIComponent(type) + '&limit=50');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (!Array.isArray(items) || !items.length) {
                results.innerHTML = '<p class="streaming-note">No favorites yet.</p>';
                return;
            }
            renderTidalFavoriteResults(results, type, items);
        } catch (err) {
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    // -- playlists ------------------------------------------------------------
    async function renderTidalPlaylists(body) {
        body.innerHTML = '<div class="streaming-results" id="tidal-playlists-results"><p class="streaming-note">Loading…</p></div>';
        const results = body.querySelector('#tidal-playlists-results');
        try {
            const resp = await fetch('/api/streaming/tidal/playlists');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (!Array.isArray(items) || !items.length) {
                results.innerHTML = '<p class="streaming-note">No playlists yet.</p>';
                return;
            }
            const list = document.createElement('ul');
            list.className = 'streaming-results-list';
            for (const item of items) {
                const li = document.createElement('li');
                li.className = 'streaming-result';
                li.innerHTML =
                    '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                    '<div class="streaming-result-info">' +
                        '<div class="streaming-result-title">' + escapeHtml(item.name) + '</div>' +
                        '<div class="streaming-result-sub">' + (item.track_count ? item.track_count + ' tracks' : '') + '</div>' +
                    '</div>' +
                    favoriteButtonHtml('playlists', item.id);
                li.addEventListener('click', () => openTidalPlaylist(item.id, item.name, item.art_url));
                bindTidalFavoriteButtons(li);
                list.appendChild(li);
            }
            results.innerHTML = '';
            results.appendChild(list);
        } catch (err) {
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
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
        if (!art) return '';
        const cls = 'streaming-detail-cover' + (extraClass ? ' ' + extraClass : '');
        return '<div class="' + cls + '">' + coverImg(art) + '</div>';
    }

    function renderTidalAlbum(content) {
        const requestId = ++state.tidal.detailRequestId;
        // Mirrors the library album detail: cover + title/artist/facts beside
        // it, star favorite in the title row, shared back button in the header
        // row, then the compact track list. No "Play album" button.
        content.innerHTML =
            '<div class="streaming-detail tidal-detail">' +
                '<div class="streaming-detail-header tidal-detail-header">' +
                    detailCoverHtml('tidal-detail-cover') +
                    '<div class="streaming-detail-main tidal-detail-meta">' +
                        '<div class="tidal-detail-title-row">' +
                            '<h3 class="streaming-detail-title tidal-detail-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                            favoriteStarHtml('albums') +
                        '</div>' +
                        '<p class="streaming-detail-artist tidal-detail-artist" id="tidal-album-artist"></p>' +
                        '<div class="streaming-detail-facts tidal-detail-facts" id="tidal-album-facts"></div>' +
                    '</div>' +
                    '<button type="button" class="album-detail-back" id="tidal-detail-back">← Back</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results"><p class="streaming-note">Loading…</p></div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteStars(content);
        loadTidalAlbum(content, requestId);
    }

    function renderTidalAlbumMeta(content, meta) {
        if (meta) {
            const titleEl = content.querySelector('.tidal-detail-title');
            if (titleEl) titleEl.textContent = meta.title || state.tidal.detailTitle;
            if (meta.art_url) {
                const cover = content.querySelector('.tidal-detail-cover');
                if (cover) cover.innerHTML = coverImg(meta.art_url);
            }
            const artistEl = content.querySelector('#tidal-album-artist');
            if (artistEl) artistEl.textContent = meta.artist || '';
            const facts = [
                meta.year ? String(meta.year) : '',
                tidalQualityLabel(meta.audio_quality),
                meta.num_tracks ? (meta.num_tracks + ' tracks') : '',
            ].filter(Boolean).join(' · ');
            const factsEl = content.querySelector('#tidal-album-facts');
            if (factsEl) factsEl.textContent = facts;
        }
        syncFavoriteStars('albums', state.tidal.detailId);
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
            renderTidalAlbumMeta(content, meta);
            renderDetailTracks(results, items, null);
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    function renderTidalArtist(content) {
        const requestId = ++state.tidal.detailRequestId;
        // Same library-mirroring layout as the album/playlist detail: cover +
        // name, follow star in the title row, shared back button in the header,
        // then Top Tracks and Albums sections.
        content.innerHTML =
            '<div class="streaming-detail tidal-detail">' +
                '<div class="streaming-detail-header tidal-detail-header">' +
                    detailCoverHtml('tidal-detail-cover') +
                    '<div class="streaming-detail-main tidal-detail-meta">' +
                        '<div class="tidal-detail-title-row">' +
                            '<h3 class="streaming-detail-title tidal-detail-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                            favoriteStarHtml('artists') +
                        '</div>' +
                        '<p class="streaming-detail-artist tidal-detail-artist" id="tidal-artist-facts"></p>' +
                    '</div>' +
                    '<button type="button" class="album-detail-back" id="tidal-detail-back">← Back</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results"><p class="streaming-note">Loading…</p></div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteStars(content);
        loadTidalArtist(content, requestId);
    }

    async function loadTidalArtist(content, requestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/artists/' + encodeURIComponent(state.tidal.detailId));
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const data = await resp.json();
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'artist') return;
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
            syncFavoriteStars('artists', state.tidal.detailId);
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
                list.className = 'streaming-results-list';
                albums.forEach((item) => list.appendChild(renderSearchItem('albums', item, [])));
                results.appendChild(list);
            }
            if (!tracks.length && !albums.length) {
                results.innerHTML = '<p class="streaming-note">No tracks or albums available.</p>';
            }
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'artist') return;
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    function renderTidalPlaylist(content) {
        const requestId = ++state.tidal.detailRequestId;
        // Same library-mirroring layout as the album detail; the playlist adds
        // its Play playlist action and shows the track count as the facts line.
        content.innerHTML =
            '<div class="streaming-detail tidal-detail">' +
                '<div class="streaming-detail-header tidal-detail-header">' +
                    detailCoverHtml('tidal-detail-cover') +
                    '<div class="streaming-detail-main tidal-detail-meta">' +
                        '<div class="tidal-detail-title-row">' +
                            '<h3 class="streaming-detail-title tidal-detail-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                            favoriteStarHtml('playlists') +
                        '</div>' +
                        '<div class="streaming-detail-facts tidal-detail-facts" id="tidal-playlist-facts"></div>' +
                        '<button type="button" class="btn-primary" id="tidal-detail-play">Play playlist</button>' +
                    '</div>' +
                    '<button type="button" class="album-detail-back" id="tidal-detail-back">← Back</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results"><p class="streaming-note">Loading…</p></div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', closeTidalDetail);
        bindFavoriteStars(content);
        loadTidalPlaylistTracks(content, requestId);
    }

    async function loadTidalAlbumTracks(content, requestId = state.tidal.detailRequestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId) + '/tracks');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            renderDetailTracks(results, items, content.querySelector('#tidal-detail-play'));
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'album') return;
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    async function loadTidalPlaylistTracks(content, requestId) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/playlists/' + encodeURIComponent(state.tidal.detailId) + '/tracks');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            try { await loadTidalFavoriteIds(); } catch (e) { /* hearts degrade to unfilled */ }
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'playlist') return;
            const factsEl = content.querySelector('#tidal-playlist-facts');
            if (factsEl && Array.isArray(items)) {
                factsEl.textContent = items.length + ' track' + (items.length === 1 ? '' : 's');
            }
            syncFavoriteStars('playlists', state.tidal.detailId);
            renderDetailTracks(results, items, content.querySelector('#tidal-detail-play'));
        } catch (err) {
            if (requestId !== state.tidal.detailRequestId || state.tidal.view !== 'playlist') return;
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    function renderDetailTracks(container, items, playAllBtn) {
        container.innerHTML = '';
        if (!Array.isArray(items) || !items.length) {
            container.innerHTML = '<p class="streaming-note">No tracks.</p>';
            if (playAllBtn) playAllBtn.disabled = true;
            return;
        }
        const ids = items.map((t) => String(t.id)).filter(Boolean);
        if (playAllBtn) {
            playAllBtn.disabled = false;
            playAllBtn.onclick = () => playTidalTracks(ids, ids[0]);
        }
        const list = document.createElement('ul');
        list.className = 'streaming-results-list';
        items.forEach((item, index) => {
            const li = document.createElement('li');
            li.className = 'streaming-result';
            li.innerHTML = trackRowHtml({
                index: index + 1,
                title: escapeHtml(item.title),
                sub: escapeHtml(item.artist || ''),
                favoriteButton: favoriteButtonHtml('tracks', item.id, 'track-fav'),
                duration: formatTime(item.duration),
            });
            const trackId = String(item.id);
            li.querySelector('.track-play').addEventListener('click', (event) => {
                event.stopPropagation();
                playTidalTracks(ids, trackId);
            });
            li.addEventListener('click', () => playTidalTracks(ids, trackId));
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
    };
})();
