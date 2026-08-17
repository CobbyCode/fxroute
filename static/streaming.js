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
    let formatRateKhz = function () { return ''; };
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
        tidal: { name: 'TIDAL', canConnect: true },
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
            view: null,         // 'login' | 'browse' | 'album' | 'playlist'
            searchQuery: '',
            searchType: 'tracks',
            favoritesType: 'tracks',
            detailId: null,
            detailTitle: '',
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
        if (typeof api.formatRateKhz === 'function') formatRateKhz = api.formatRateKhz;
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
                    '<div class="streaming-status-line" hidden></div>' +
                    '<div class="streaming-empty" hidden>' +
                        '<div class="streaming-empty-icon" aria-hidden="true"></div>' +
                        '<h2 class="streaming-empty-title"></h2>' +
                        '<p class="streaming-empty-msg"></p>' +
                        '<div class="streaming-empty-actions"></div>' +
                    '</div>' +
                    '<div class="streaming-now-playing" hidden>' +
                        '<div class="streaming-cover-wrap">' +
                            '<img class="streaming-cover" alt="" />' +
                        '</div>' +
                        '<div class="streaming-meta">' +
                            '<div class="streaming-title"></div>' +
                            '<div class="streaming-artist"></div>' +
                            '<div class="streaming-album"></div>' +
                            '<div class="streaming-quality"></div>' +
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

            const els = {
                statusLine: root.querySelector('.streaming-status-line'),
                empty: root.querySelector('.streaming-empty'),
                emptyIcon: root.querySelector('.streaming-empty-icon'),
                emptyTitle: root.querySelector('.streaming-empty-title'),
                emptyMsg: root.querySelector('.streaming-empty-msg'),
                emptyActions: root.querySelector('.streaming-empty-actions'),
                nowPlaying: root.querySelector('.streaming-now-playing'),
                coverWrap: root.querySelector('.streaming-cover-wrap'),
                cover: root.querySelector('.streaming-cover'),
                title: root.querySelector('.streaming-title'),
                artist: root.querySelector('.streaming-artist'),
                album: root.querySelector('.streaming-album'),
                quality: root.querySelector('.streaming-quality'),
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
    function renderStatusLine(providerId, entry, data) {
        const bits = [];
        const backendLabel = backendDisplayName(providerId, data.backend);
        if (backendLabel) bits.push(backendLabel);
        if (providerId === 'qobuz' && data.connected) bits.push('Qobuz Connect');
        if (data.authenticated === true && providerId !== 'spotify') bits.push('Connected');
        entry.els.statusLine.hidden = bits.length === 0;
        entry.els.statusLine.textContent = bits.join(' · ');
    }

    function backendDisplayName(providerId, backend) {
        if (!backend) return '';
        if (providerId === 'spotify' && backend === 'desktop') return 'Spotify Desktop';
        if (providerId === 'spotify' && backend === 'spotifyd') return 'spotifyd';
        return backend;
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

        // Quality (audio_format / bit_depth / sample_rate), capability-gated.
        const qualityText = formatQuality(data, caps);
        els.quality.hidden = !qualityText;
        els.quality.textContent = qualityText;

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

    function formatQuality(data, caps) {
        caps = caps || {};
        const parts = [];
        if (caps.audio_format && data.audio_format) parts.push(String(data.audio_format).toUpperCase());
        if (caps.bit_depth && data.bit_depth) parts.push(data.bit_depth + ' bit');
        if (caps.sample_rate && data.sample_rate) {
            const rate = formatRateKhz(data.sample_rate);
            if (rate) parts.push(rate);
        }
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
        const els = entry.els;
        if (data.available === false || data.installed !== true) {
            els.content.hidden = true;
            return;
        }
        els.content.hidden = false;
        if (data.authenticated !== true) {
            renderTidalLogin(entry);
            return;
        }
        renderTidalBrowse(entry);
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
                    '<button type="button" class="btn-ghost" id="tidal-auth-device">Device login — limited to AAC 320 kbps</button>' +
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
        if (state.tidal.view === 'album') { renderTidalAlbum(content); return; }
        if (state.tidal.view === 'playlist') { renderTidalPlaylist(content); return; }
        content.innerHTML =
            '<div class="streaming-browse">' +
                '<div class="streaming-browse-tabs" role="tablist">' +
                    '<button type="button" class="streaming-browse-tab is-active" data-browse="search">Search</button>' +
                    '<button type="button" class="streaming-browse-tab" data-browse="favorites">Favorites</button>' +
                    '<button type="button" class="streaming-browse-tab" data-browse="playlists">Playlists</button>' +
                '</div>' +
                '<div class="streaming-browse-body" id="tidal-browse-body"></div>' +
            '</div>';
        const tabs = content.querySelectorAll('.streaming-browse-tab');
        tabs.forEach((tab) => tab.addEventListener('click', () => {
            tabs.forEach((t) => t.classList.toggle('is-active', t === tab));
            renderTidalBrowseSection(tab.dataset.browse);
        }));
        renderTidalBrowseSection('search');
    }

    function renderTidalBrowseSection(section) {
        const body = document.getElementById('tidal-browse-body');
        if (!body) return;
        if (section === 'search') renderTidalSearch(body);
        else if (section === 'favorites') renderTidalFavorites(body);
        else if (section === 'playlists') renderTidalPlaylists(body);
    }

    // -- search ---------------------------------------------------------------
    function renderTidalSearch(body) {
        body.innerHTML =
            '<div class="streaming-search">' +
                '<div class="streaming-search-row">' +
                    '<input type="search" class="streaming-search-input" id="tidal-search-input" placeholder="Search tracks, albums, artists, playlists…" autocomplete="off" />' +
                    '<button type="button" class="btn-primary" id="tidal-search-btn">Search</button>' +
                '</div>' +
                '<div class="streaming-chip-row" id="tidal-search-types" aria-label="Search type">' +
                    chip('tracks', 'Tracks', true) + chip('albums', 'Albums', false) +
                    chip('artists', 'Artists', false) + chip('playlists', 'Playlists', false) +
                '</div>' +
                '<div class="streaming-results" id="tidal-search-results"></div>' +
            '</div>';

        body.querySelectorAll('#tidal-search-types .streaming-chip').forEach((chipEl) => {
            chipEl.addEventListener('click', () => {
                body.querySelectorAll('#tidal-search-types .streaming-chip').forEach((c) => c.classList.toggle('is-active', c === chipEl));
                state.tidal.searchType = chipEl.dataset.type;
            });
        });

        const doSearch = async () => {
            const query = (body.querySelector('#tidal-search-input').value || '').trim();
            if (!query) return;
            const results = body.querySelector('#tidal-search-results');
            results.innerHTML = '<p class="streaming-note">Searching…</p>';
            try {
                const resp = await fetch('/api/streaming/tidal/search?q=' + encodeURIComponent(query) + '&types=' + encodeURIComponent(state.tidal.searchType) + '&limit=25');
                if (!resp.ok) throw new Error(await errorDetail(resp));
                const data = await resp.json();
                renderTidalSearchResults(results, data);
            } catch (err) {
                results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
            }
        };
        body.querySelector('#tidal-search-btn').addEventListener('click', doSearch);
        body.querySelector('#tidal-search-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });
    }

    function chip(type, label, active) {
        return '<button type="button" class="streaming-chip' + (active ? ' is-active' : '') + '" data-type="' + type + '">' + escapeHtml(label) + '</button>';
    }

    function renderTidalSearchResults(container, data) {
        container.innerHTML = '';
        const types = ['tracks', 'albums', 'artists', 'playlists'];
        let rendered = false;
        for (const type of types) {
            const items = data[type] || [];
            if (!items.length) continue;
            rendered = true;
            const heading = document.createElement('h4');
            heading.className = 'streaming-results-heading';
            heading.textContent = type[0].toUpperCase() + type.slice(1);
            container.appendChild(heading);
            const list = document.createElement('ul');
            list.className = 'streaming-results-list';
            for (const item of items) {
                list.appendChild(renderSearchItem(type, item));
            }
            container.appendChild(list);
        }
        if (!rendered) {
            container.innerHTML = '<p class="streaming-note">No results.</p>';
        }
    }

    function renderSearchItem(type, item) {
        const li = document.createElement('li');
        li.className = 'streaming-result';
        if (type === 'tracks') {
            li.innerHTML =
                '<button type="button" class="streaming-result-play" title="Play">▶</button>' +
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.title) + '</div>' +
                    '<div class="streaming-result-sub">' + escapeHtml(item.artist || '') + '</div>' +
                '</div>' +
                '<div class="streaming-result-album">' + escapeHtml(item.album || '') + '</div>' +
                '<div class="streaming-result-duration">' + formatTime(item.duration) + '</div>';
            li.querySelector('.streaming-result-play').addEventListener('click', () => playTidalTracks([item.id], item.id));
            li.addEventListener('click', () => playTidalTracks([item.id], item.id));
        } else if (type === 'albums') {
            li.innerHTML =
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.title) + '</div>' +
                    '<div class="streaming-result-sub">' + escapeHtml(item.artist || '') + '</div>' +
                '</div>';
            li.addEventListener('click', () => openTidalAlbum(item.id, item.title));
        } else if (type === 'artists') {
            li.innerHTML =
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.name) + '</div>' +
                '</div>';
        } else if (type === 'playlists') {
            li.innerHTML =
                '<div class="streaming-result-cover">' + coverImg(item.art_url) + '</div>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.name) + '</div>' +
                    '<div class="streaming-result-sub">' + (item.track_count ? item.track_count + ' tracks' : '') + '</div>' +
                '</div>';
            li.addEventListener('click', () => openTidalPlaylist(item.id, item.name));
        }
        return li;
    }

    function coverImg(url) {
        return url ? '<img src="' + escapeHtml(url) + '" alt="" loading="lazy" />' : '';
    }

    // -- favorites ------------------------------------------------------------
    function renderTidalFavorites(body) {
        body.innerHTML =
            '<div class="streaming-chip-row" id="tidal-fav-types" aria-label="Favorites category">' +
                chip('tracks', 'Tracks', true) + chip('albums', 'Albums', false) + chip('artists', 'Artists', false) +
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
            const resp = await fetch('/api/streaming/tidal/favorites?type=' + encodeURIComponent(type) + '&limit=50');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            if (!Array.isArray(items) || !items.length) {
                results.innerHTML = '<p class="streaming-note">No favorites yet.</p>';
                return;
            }
            renderTidalSearchResults(results, { [type]: items });
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
                    '</div>';
                li.addEventListener('click', () => openTidalPlaylist(item.id, item.name));
                list.appendChild(li);
            }
            results.innerHTML = '';
            results.appendChild(list);
        } catch (err) {
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    // -- album / playlist detail ----------------------------------------------
    function openTidalAlbum(id, title) {
        state.tidal.view = 'album';
        state.tidal.detailId = id;
        state.tidal.detailTitle = title;
        const entry = entryFor('tidal');
        if (entry) renderTidalContent(entry, state.lastData.tidal || {});
    }

    function openTidalPlaylist(id, title) {
        state.tidal.view = 'playlist';
        state.tidal.detailId = id;
        state.tidal.detailTitle = title;
        const entry = entryFor('tidal');
        if (entry) renderTidalContent(entry, state.lastData.tidal || {});
    }

    function renderTidalAlbum(content) {
        content.innerHTML =
            '<div class="streaming-detail">' +
                '<div class="streaming-detail-header">' +
                    '<button type="button" class="btn-ghost" id="tidal-detail-back">← Back</button>' +
                    '<h3 class="streaming-detail-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                    '<button type="button" class="btn-primary" id="tidal-detail-play">Play album</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results"><p class="streaming-note">Loading…</p></div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', () => { state.tidal.view = null; renderTidalBrowse(entryFor('tidal')); });
        loadTidalAlbumTracks(content);
    }

    function renderTidalPlaylist(content) {
        content.innerHTML =
            '<div class="streaming-detail">' +
                '<div class="streaming-detail-header">' +
                    '<button type="button" class="btn-ghost" id="tidal-detail-back">← Back</button>' +
                    '<h3 class="streaming-detail-title">' + escapeHtml(state.tidal.detailTitle) + '</h3>' +
                    '<button type="button" class="btn-primary" id="tidal-detail-play">Play playlist</button>' +
                '</div>' +
                '<div class="streaming-results" id="tidal-detail-results"><p class="streaming-note">Loading…</p></div>' +
            '</div>';
        content.querySelector('#tidal-detail-back').addEventListener('click', () => { state.tidal.view = null; renderTidalBrowse(entryFor('tidal')); });
        loadTidalPlaylistTracks(content);
    }

    async function loadTidalAlbumTracks(content) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/albums/' + encodeURIComponent(state.tidal.detailId) + '/tracks');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            renderDetailTracks(results, items, content.querySelector('#tidal-detail-play'));
        } catch (err) {
            results.innerHTML = '<p class="streaming-note">' + escapeHtml(friendlyError(err?.message || err)) + '</p>';
        }
    }

    async function loadTidalPlaylistTracks(content) {
        const results = content.querySelector('#tidal-detail-results');
        try {
            const resp = await fetch('/api/streaming/tidal/playlists/' + encodeURIComponent(state.tidal.detailId) + '/tracks');
            if (!resp.ok) throw new Error(await errorDetail(resp));
            const items = await resp.json();
            renderDetailTracks(results, items, content.querySelector('#tidal-detail-play'));
        } catch (err) {
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
            li.innerHTML =
                '<span class="streaming-result-index">' + (index + 1) + '</span>' +
                '<button type="button" class="streaming-result-play" title="Play">▶</button>' +
                '<div class="streaming-result-info">' +
                    '<div class="streaming-result-title">' + escapeHtml(item.title) + '</div>' +
                    '<div class="streaming-result-sub">' + escapeHtml(item.artist || '') + '</div>' +
                '</div>' +
                '<div class="streaming-result-duration">' + formatTime(item.duration) + '</div>';
            const trackId = String(item.id);
            li.querySelector('.streaming-result-play').addEventListener('click', () => playTidalTracks(ids, trackId));
            li.addEventListener('click', () => playTidalTracks(ids, trackId));
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
    };
})();
