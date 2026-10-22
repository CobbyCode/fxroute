// SPDX-License-Identifier: AGPL-3.0-only
/**
 * FXRoute library UI: library status/tracks/playlists fetch, folder/album/
 * playlist filtering and rendering, detail views, selection, search, upload/
 * download and playlist/library CRUD, including the track-row favorites.
 *
 * State/DOM/toast go through injected getters. Playback entry points, footer
 * mode rendering and the generic ui-helper shims (favoriteHeartSvg etc.) stay
 * in app.js behind explicit callbacks; the row-favorite, tidal-favorite and
 * library-mode request flags remain shared. Browser-loadable UMD, no build
 * step; Node-testable via require().
 */
(function (root, factory) {
    const api = factory(root);
    root.FXRouteLibraryUI = api;
    if (typeof module === 'object' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : window, function (root) {
    'use strict';

    let deps = {
        getState: () => ({ library: { viewMode: 'flat', currentFolder: '', scanStatus: null } }),
        getElements: () => ({}),
        showToast: () => {},
        escapeHtml: (value) => String(value == null ? '' : value),
        formatTime: (value) => String(value ?? ''),
        formatTransitionErrorDetail: (detail, fallback) => fallback,
        getDownloadFilenameFromResponse: (response, fallback) => fallback,
        triggerBlobDownload: () => {},
        isPageHidden: () => false,
        mergePlaybackState: () => {},
        playLocal: async () => {},
        playRefs: async () => {},
        renderFooterModeButtons: () => {},
        syncLibraryStateFromPlaybackContext: () => {},
        updatePlaybackUI: () => {},
        favoriteHeartSvg: () => '',
        artworkPlaceholderUrl: () => '',
        albumArtFallbackSvg: () => '',
        sourceGlyphHtml: () => '',
        isLibraryModeRequestInFlight: () => false,
        setLibraryModeRequestInFlight: () => {},
        isTidalFavoriteRequestInFlight: () => false,
    };

    function init(overrides) {
        deps = Object.assign(deps, overrides || {});
    }

// Grid/list layout persistence (Library albums + TIDAL tile surfaces), read
// before state init. Same localStorage mechanism as fx-debug-footer; grid is
// the default when the key is absent or storage is unavailable.
const VIEW_MODE_STORAGE_KEY = 'fx-view-mode-';

function readStoredViewMode(surface) {
    try {
        return localStorage.getItem(VIEW_MODE_STORAGE_KEY + surface) === 'list' ? 'list' : 'grid';
    } catch (e) {
        // Storage may be unavailable (private mode); grid stays the default.
        return 'grid';
    }
}

let trackFavoriteRequestInFlight = false;

// -- TIDAL in the shared Library ---------------------------------------------
// TIDAL is optional: without the streaming adapter, an installed and enabled
// provider or a login, every helper below yields nothing and the Library
// stays purely local. Data, hearts, rows and details stay owned by
// static/streaming.js (window.FXRouteStreaming.tidal).
function tidalLibrary() {
    const streaming = root && root.FXRouteStreaming;
    return (streaming && streaming.tidal) || null;
}

function libraryQuery() {
    return (deps.getState().library.searchQuery || '').trim().toLowerCase();
}

function tidalItemSearchText(kind, item) {
    if (kind === 'albums') return [item.title, item.artist, item.year];
    if (kind === 'tracks') return [item.title, item.artist, item.album];
    return [item.name];
}

// `query` is already trimmed and lower-cased (libraryQuery()).
function tidalEntryMatchesQuery(item, query, kind = 'tracks') {
    return tidalItemSearchText(kind, item).filter(Boolean).join(' ').toLowerCase().includes(query);
}

// TIDAL favorites of one kind (albums/tracks/playlists) for the Library:
// only items that are still favorited, so an un-hearted item leaves at once,
// filtered by the current search.
function tidalLibraryFavorites(kind, options = {}) {
    const tidal = tidalLibrary();
    if (!tidal || !tidal.connection().authenticated) return [];
    const query = options.ignoreQuery ? '' : libraryQuery();
    return tidal.items(kind).filter(item => {
        if (!item || item.id == null || !tidal.isFavorite(kind, item.id)) return false;
        return !query || tidalEntryMatchesQuery(item, query, kind);
    });
}

// Same order as the backend album list: "Various" last, then artist, then
// album name, compared case-insensitively.
function compareAlbumEntries(a, b) {
    if (a.various !== b.various) return a.various ? 1 : -1;
    if (a.artist !== b.artist) return a.artist < b.artist ? -1 : 1;
    if (a.name !== b.name) return a.name < b.name ? -1 : 1;
    return 0;
}

const TIDAL_CARD_FAVORITE_KINDS = { album: 'albums', artist: 'artists', playlist: 'playlists' };

function tidalCardHtml(kind, item) {
    const tidal = tidalLibrary();
    const isAlbum = kind === 'album';
    const title = (isAlbum ? item.title : item.name) || '';
    const sub = isAlbum ? (item.artist || '')
        : kind === 'artist' ? 'Artist'
        : (item.track_count ? `${item.track_count} tracks` : 'Playlist');
    return `
        <div class="album-card" data-tidal-kind="${kind}" data-tidal-id="${deps.escapeHtml(item.id)}" data-tidal-name="${deps.escapeHtml(title)}" data-tidal-art="${deps.escapeHtml(item.art_url || '')}" role="button" tabindex="0">
            <div class="album-art-wrap">${tidal.cardArtHtml(item.art_url, title)}</div>
            ${tidal.favoriteButtonHtml(TIDAL_CARD_FAVORITE_KINDS[kind] || 'playlists', item.id)}
            <div class="album-name">${deps.escapeHtml(title)}</div>
            <div class="album-artist">${deps.escapeHtml(sub)}</div>
            ${deps.sourceGlyphHtml('tidal', '', { variant: 'card', link: { kind, id: item.id } })}
        </div>`;
}

function bindTidalCards(container) {
    const tidal = tidalLibrary();
    if (!tidal || !container) return;
    container.querySelectorAll('.album-card[data-tidal-kind]').forEach(card => {
        const open = () => tidal.openDetail(card.dataset.tidalKind, card.dataset.tidalId, card.dataset.tidalName || '', card.dataset.tidalArt || '');
        card.addEventListener('click', open);
        card.addEventListener('keydown', (event) => {
            if (event.target !== event.currentTarget) return;
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            open();
        });
    });
    tidal.bindFavoriteButtons(container);
}

// TIDAL track rows inside a Library list: built by the TIDAL adapter (its
// heart, + selection and playback), marked with the TIDAL glyph. A click
// plays the TIDAL rows of the same list as one queue.
// A saved playlist entry is a local track id or a TIDAL reference "tidal:<id>".
const TIDAL_REF_PREFIX = 'tidal:';

function isTidalRef(ref) {
    return String(ref || '').startsWith(TIDAL_REF_PREFIX);
}

function tidalRefId(ref) {
    return String(ref).slice(TIDAL_REF_PREFIX.length);
}

function playlistHasTidal(playlist) {
    return (playlist?.track_ids || []).some(isTidalRef);
}

// A saved playlist in its own order: local tracks the library still knows,
// and TIDAL entries with the display data saved next to the reference (so they
// list without TIDAL). Each id appears once; unknown local ids are dropped.
function playlistEntries(playlist) {
    const byId = new Map((deps.getState().library.tracks || []).map(track => [track.id, track]));
    const meta = playlist?.track_meta || {};
    const seen = new Set();
    const entries = [];
    for (const ref of (playlist?.track_ids || [])) {
        if (seen.has(ref)) continue;
        seen.add(ref);
        if (isTidalRef(ref)) {
            const saved = meta[ref] || {};
            entries.push({
                ref,
                tidal: {
                    id: tidalRefId(ref),
                    title: saved.title || 'TIDAL track',
                    artist: saved.artist || '',
                    album: saved.album || '',
                    duration: saved.duration || 0,
                    art_url: saved.art_url || '',
                },
            });
        } else if (byId.has(ref)) {
            entries.push({ ref, track: byId.get(ref) });
        }
    }
    return entries;
}

function appendTidalTrackRows(container, items, options = {}) {
    const tidal = tidalLibrary();
    if (!tidal || !container || !items.length) return;
    const queueIds = items.map(item => String(item.id));
    const firstIndex = options.firstIndex;
    items.forEach((item, offset) => {
        const sub = options.albumContext
            ? (item.artist || '')
            : [item.artist, item.album].filter(Boolean).join(' · ');
        container.appendChild(tidal.buildTrackRow(item, {
            tag: 'div',
            index: firstIndex != null ? firstIndex + offset : undefined,
            sub,
            queueIds,
            badge: deps.sourceGlyphHtml('tidal'),
            favoriteClass: options.favoriteClass,
            onPlay: options.onPlay,
        }));
    });
}

let downloadStatusPollTimer = null;

let lastDownloadStatus = null;

const LIBRARY_SCAN_POLL_INTERVAL_MS = 1200;
let libraryScanPollTimer = null;

// Library view tabs in tab order. Albums, Artists, Playlists and Favorites are
// tile views (grids with detail pages); Tracks and Folders are lists.
const LIBRARY_VIEW_MODES = ['albums', 'tracks', 'artists', 'playlists', 'favorites', 'folders'];

const FAVORITE_TRACKS_ID = 'smart_favorite_tracks';
const FAVORITE_TRACKS_COVER = '/static/FavoriteTracks.svg?v=1';

const DOWNLOAD_STATUS_POLL_INTERVAL_MS = 1500;

function clearLibraryImportFeedbackIfIdle() {
    const uploadActive = deps.getState().upload && deps.getState().upload.status === 'uploading';
    const downloadActive = deps.getState().download && ['starting', 'downloading'].includes(deps.getState().download.status);
    if (uploadActive || downloadActive) return;

    deps.getState().upload = null;
    if (deps.getState().download && ['complete', 'error', 'cancelled'].includes(deps.getState().download.status)) {
        deps.getState().download = null;
        lastDownloadStatus = 'idle';
    }
    updateDownloadUI();
}

function closeLibraryImportPanel() {
    if (!deps.getElements().libraryImportPanel || deps.getElements().libraryImportPanel.classList.contains('hidden')) {
        if (deps.getElements().toggleImportBtn) {
            deps.getElements().toggleImportBtn.textContent = 'Import';
            deps.getElements().toggleImportBtn.setAttribute('aria-expanded', 'false');
        }
        return;
    }
    const searchWrap = deps.getElements().librarySearchInput ? deps.getElements().librarySearchInput.closest('.library-search-wrap') : null;
    const selectionToolbar = deps.getElements().selectAllTracksBtn ? deps.getElements().selectAllTracksBtn.closest('.library-selection-toolbar') : null;
    clearLibraryImportFeedbackIfIdle();
    resetUploadAreaSelection('upload-track-file');
    deps.getElements().libraryImportPanel.classList.add('hidden');
    if (searchWrap) searchWrap.classList.remove('hidden');
    if (selectionToolbar) selectionToolbar.classList.remove('hidden');
    if (deps.getElements().playlistSaveRow) updatePlaylistSaveRowVisibility();
    if (deps.getElements().toggleImportBtn) {
        deps.getElements().toggleImportBtn.textContent = 'Import';
        deps.getElements().toggleImportBtn.setAttribute('aria-expanded', 'false');
    }
}

function getTrackIdsInLibraryOrder(trackIds = []) {
    const known = new Set((deps.getState().library.tracks || []).map(track => track?.id).filter(Boolean));
    return [...new Set((trackIds || []).filter(id => id && known.has(id)))];
}

function getSelectedPlayableTrackIds() {
    return getTrackIdsInLibraryOrder(deps.getState().library.selectedTrackIds || []);
}

function getSelectedDownloadTrackIds() {
    return getTrackIdsInLibraryOrder(deps.getState().library.selectedTrackIds || []);
}

function renderLibraryModeButtons() {
    deps.renderFooterModeButtons();
}

async function toggleLibraryShuffle() {
    if (deps.isLibraryModeRequestInFlight()) return;
    deps.setLibraryModeRequestInFlight(true);
    renderLibraryModeButtons();
    try {
        const resp = await fetch('/api/playback/shuffle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !deps.getState().library.shuffle }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Shuffle update failed'));
        }
        if (data.playback) {
            deps.mergePlaybackState(data.playback);
            deps.syncLibraryStateFromPlaybackContext(true);
        }
        deps.updatePlaybackUI();
    } catch (e) {
        deps.showToast(e.message || 'Failed to update shuffle', 'error');
    } finally {
        deps.setLibraryModeRequestInFlight(false);
        renderLibraryModeButtons();
    }
}

async function toggleLibraryLoop() {
    if (deps.isLibraryModeRequestInFlight()) return;
    deps.setLibraryModeRequestInFlight(true);
    renderLibraryModeButtons();
    try {
        const resp = await fetch('/api/playback/loop', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: !deps.getState().library.loop }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Loop update failed'));
        if (data.playback) {
            deps.mergePlaybackState(data.playback);
            deps.syncLibraryStateFromPlaybackContext(true);
        }
        deps.updatePlaybackUI();
    } catch (e) {
        deps.showToast(e.message || 'Failed to update loop', 'error');
    } finally {
        deps.setLibraryModeRequestInFlight(false);
        renderLibraryModeButtons();
    }
}

function renderTrackFavoriteButton(track = deps.getState().playback.current_track) {
    const button = deps.getElements().trackFavoriteBtn;
    if (!button) return;
    const source = track?.source;
    const isLocal = source === 'local';
    const isTidal = source === 'tidal';
    const hasId = !!(track && track.id);
    const available = !!((isLocal || isTidal) && hasId);
    button.classList.toggle('hidden', !available);
    if (!available) {
        button.disabled = true;
        button.innerHTML = deps.favoriteHeartSvg();
        button.classList.remove('active');
        button.setAttribute('aria-pressed', 'false');
        return;
    }
    const inFlight = trackFavoriteRequestInFlight || deps.isTidalFavoriteRequestInFlight();
    if (isTidal) {
        // The footer heart favorites the current TIDAL track through the same
        // canonical state/API as the Library TIDAL rows. While the ids are still
        // loading the button stays disabled so it never guesses the state;
        // the fxroute:tidal-favorites event re-renders it once they arrive.
        const ready = !!(window.FXRouteStreaming && window.FXRouteStreaming.tidalFavoritesReady()
            && window.FXRouteStreaming.isTidalFavorite);
        const favorite = ready ? window.FXRouteStreaming.isTidalFavorite('tracks', track.id) : false;
        button.disabled = inFlight || !ready;
        button.innerHTML = deps.favoriteHeartSvg();
        button.classList.toggle('active', favorite);
        button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
        button.setAttribute('aria-label', favorite ? 'Remove track from favorites' : 'Add track to favorites');
        button.setAttribute('data-tooltip', favorite ? 'Remove track from favorites' : 'Add track to favorites');
        if (!ready) {
            const streaming = window.FXRouteStreaming;
            if (streaming && streaming.ensureTidalFavoritesLoaded) {
                void streaming.ensureTidalFavoritesLoaded().then(() => renderTrackFavoriteButton(deps.getState().playback.current_track));
            }
        }
        return;
    }
    // Local library track favorite (unchanged native path).
    button.disabled = inFlight;
    const favorite = !!track.favorite;
    button.innerHTML = deps.favoriteHeartSvg();
    button.classList.toggle('active', favorite);
    button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
    button.setAttribute('aria-label', favorite ? 'Remove track from favorites' : 'Add track to favorites');
    button.setAttribute('data-tooltip', favorite ? 'Remove track from favorites' : 'Add track to favorites');
}

function updateTrackFavoriteCaches(trackId, favorite) {
    const apply = (track) => {
        if (track && track.id === trackId) track.favorite = !!favorite;
    };
    apply(deps.getState().playback.current_track);
    (deps.getState().library.tracks || []).forEach(apply);
    (deps.getState().library.albumDetail?.tracks || []).forEach(apply);
}

function findTrackById(trackId) {
    if (!trackId) return null;
    if (deps.getState().playback.current_track?.id === trackId) return deps.getState().playback.current_track;
    return (deps.getState().library.albumDetail?.tracks || []).find(track => track?.id === trackId)
        || (deps.getState().library.tracks || []).find(track => track?.id === trackId)
        || null;
}

function syncTrackFavoriteRowButtons(trackId = null) {
    document.querySelectorAll('.track-row-favorite[data-track-favorite], .track-fav[data-track-favorite]').forEach(button => {
        const id = button.dataset.trackFavorite || '';
        if (trackId && id !== trackId) return;
        const track = findTrackById(id);
        const favorite = !!track?.favorite;
        button.innerHTML = deps.favoriteHeartSvg();
        button.classList.toggle('active', favorite);
        button.setAttribute('aria-pressed', favorite ? 'true' : 'false');
        button.setAttribute('aria-label', favorite ? 'Remove track from favorites' : 'Add track to favorites');
        button.setAttribute('data-tooltip', favorite ? 'Remove from favorites' : 'Add to favorites');
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
    renderTrackFavoriteButton(deps.getState().playback.current_track);
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
        renderTrackFavoriteButton(deps.getState().playback.current_track);
        syncTrackFavoriteRowButtons(track.id);
        refreshFavoritesView();
    } catch (error) {
        deps.showToast(error.message || 'Failed to update track favorite', 'error');
    } finally {
        trackFavoriteRequestInFlight = false;
        renderTrackFavoriteButton(deps.getState().playback.current_track);
        syncTrackFavoriteRowButtons();
    }
}

function scheduleLibraryStatusPoll() {
    if (libraryScanPollTimer !== null) return;
    libraryScanPollTimer = setTimeout(() => {
        libraryScanPollTimer = null;
        void fetchLibraryStatus();
    }, LIBRARY_SCAN_POLL_INTERVAL_MS);
}

async function fetchLibraryStatus() {
    try {
        const resp = await fetch('/api/library/status');
        if (!resp.ok) throw new Error('Failed to fetch library status');
        const status = await resp.json();
        const wasScanning = !!deps.getState().library.scanning;
        deps.getState().library.scanStatus = status;
        deps.getState().library.scanning = !!status.scanning;
        renderLibraryView();
        if (status.scanning) {
            scheduleLibraryStatusPoll();
        } else {
            clearTimeout(libraryScanPollTimer);
            libraryScanPollTimer = null;
            if (wasScanning) await fetchTracks();
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
        deps.getState().library.tracks = await resp.json();
        const status = await fetchLibraryStatus();
        deps.getState().library.scanning = !!status?.scanning;
        renderLibraryView();
        // Non-blocking: also load albums in background
        fetchAlbums();
    } catch (e) {
        deps.getState().library.scanning = false;
        deps.showToast('Failed to load library', 'error');
    }
}

async function fetchPlaylists() {
    try {
        const resp = await fetch('/api/playlists');
        if (!resp.ok) throw new Error('Failed to fetch playlists');
        deps.getState().playlists = await resp.json();
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
    if (deps.getState().library.viewMode === 'folders') return [];
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
    return (deps.getState().playlists || []).filter(playlist => playlistMatchesLibraryQuery(playlist, query));
}

function isTrackInCurrentFolder(track) {
    if (deps.getState().library.viewMode !== 'folders') return true;
    return getTrackFolder(track) === (deps.getState().library.currentFolder || '');
}

function getFilteredTracks() {
    const tracks = deps.getState().library.tracks || [];
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
    return tracks.filter(track => trackMatchesLibraryQuery(track, query) && isTrackInCurrentFolder(track));
}

function getTracksInFolder(folderPath) {
    const folder = folderPath || '';
    const prefix = folder ? `${folder}/` : '';
    return (deps.getState().library.tracks || []).filter(track => {
        const rel = getTrackRelativePath(track);
        return folder ? rel.startsWith(prefix) : !!rel;
    });
}

function getFolderChildren() {
    const tracks = deps.getState().library.tracks || [];
    const current = deps.getState().library.currentFolder || '';
    const prefix = current ? `${current}/` : '';
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
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
    if (searchResultsActive()) {
        renderSearchResults();
    } else if (isTileViewMode()) {
        if (deps.getState().library.albumDetail) {
            // Re-open the album detail if we were viewing one; the smart
            // playlists and artist pages rebuild through their own loaders.
            const albumId = deps.getState().library.albumDetail.album.id;
            deps.getState().library.albumDetail = null;
            if (albumId === FAVORITE_TRACKS_ID) openSmartFavoriteTracks();
            else if (albumId === 'smart_top40') openSmartTopTracks();
            else if (albumId.startsWith(ARTIST_ID_PREFIX)) openArtistDetail(albumId.slice(ARTIST_ID_PREFIX.length));
            else openAlbumDetail(albumId);
        } else if (deps.getState().library.playlistDetail) {
            const playlistId = deps.getState().library.playlistDetail.playlist.id;
            deps.getState().library.playlistDetail = null;
            openPlaylistDetail(playlistId);
        } else {
            renderTileView();
        }
    } else {
        renderTracks();
    }
}

// The collection of the current tile view (no detail open).
function renderTileView() {
    const mode = deps.getState().library.viewMode;
    if (mode === 'artists') renderArtists();
    else if (mode === 'favorites') renderFavorites();
    else renderAlbums();
}

function renderLibraryViewButtons() {
    const mode = deps.getState().library.viewMode;
    const searching = searchResultsActive();
    // While search results show, their type tabs take the place of the views.
    if (deps.getElements().libraryViewToggle) deps.getElements().libraryViewToggle.classList.toggle('hidden', searching);
    if (deps.getElements().librarySearchTypes) {
        deps.getElements().librarySearchTypes.classList.toggle('hidden', !searching);
        const activeType = searching ? searchResultType() : '';
        deps.getElements().librarySearchTypes.querySelectorAll('[data-search-type]').forEach((button) => {
            const active = button.dataset.searchType === activeType;
            button.classList.toggle('active', active);
            button.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }
    const els = deps.getElements();
    const buttons = {
        albums: els.libraryViewAlbumsBtn,
        tracks: els.libraryViewTracksBtn,
        artists: els.libraryViewArtistsBtn,
        playlists: els.libraryViewPlaylistsBtn,
        favorites: els.libraryViewFavoritesBtn,
        folders: els.libraryViewFoldersBtn,
    };
    Object.entries(buttons).forEach(([viewMode, button]) => {
        if (!button) return;
        const active = mode === viewMode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
}

function renderLibraryFolderPath() {
    if (!deps.getElements().libraryFolderPath) return;
    if (deps.getState().library.viewMode !== 'folders') {
        deps.getElements().libraryFolderPath.classList.add('hidden');
        deps.getElements().libraryFolderPath.innerHTML = '';
        return;
    }
    const current = deps.getState().library.currentFolder || '';
    const parts = current.split('/').filter(Boolean);
    let html = `<button type="button" data-folder="">Music root</button>`;
    let path = '';
    parts.forEach(part => {
        path = path ? `${path}/${part}` : part;
        html += `<span>/</span><button type="button" data-folder="${deps.escapeHtml(path)}">${deps.escapeHtml(part)}</button>`;
    });
    if (current) {
        html += '<button id="library-folder-back" class="library-folder-back" type="button" aria-label="Back to parent folder" data-tooltip="Back to parent folder">← Back</button>';
    }
    deps.getElements().libraryFolderPath.innerHTML = html;
    deps.getElements().libraryFolderPath.classList.remove('hidden');
    deps.getElements().libraryFolderPath.querySelectorAll('button[data-folder]').forEach(btn => {
        btn.addEventListener('click', () => setLibraryFolder(btn.dataset.folder || ''));
    });
    const backButton = deps.getElements().libraryFolderPath.querySelector('#library-folder-back');
    if (backButton) {
        backButton.addEventListener('click', () => {
            const parentFolder = current.split('/').filter(Boolean).slice(0, -1).join('/');
            setLibraryFolder(parentFolder);
        });
    }
}

function formatLibraryScanStatus() {
    const status = deps.getState().library.scanStatus;
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
    if (deps.getState().library.tidalDetailOpen) return;
    if (searchResultsActive()) {
        renderSearchResults();
        return;
    }
    deps.getElements().tracksList.classList.remove('is-search-results', 'is-favorites');
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    renderLibraryFolderPath();
    // Hide album/playlist views when in tracks/folders mode
    if (deps.getElements().albumsGrid) deps.getElements().albumsGrid.classList.add('hidden');
    if (deps.getElements().albumDetail) deps.getElements().albumDetail.classList.add('hidden');
    if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    deps.getElements().tracksList.classList.remove('hidden');
    const allTracks = deps.getState().library.tracks || [];
    const filteredTracks = getFilteredTracks();
    const validSelectedIds = allTracks.length > 0
        ? deps.getState().library.selectedTrackIds.filter(id => allTracks.some(track => track.id === id))
        : deps.getState().library.selectedTrackIds;
    const selectedIds = new Set(validSelectedIds);
    deps.getState().library.selectedTrackIds = Array.from(selectedIds);
    const loadingEl = document.querySelector('#tab-library .content-state');
    const scanText = formatLibraryScanStatus();
    const hasSearch = !!(deps.getState().library.searchQuery || '').trim();
    // Tracks lists TIDAL favorite tracks after the local ones; Folders stay local.
    const tidalTracks = deps.getState().library.viewMode === 'tracks' ? tidalLibraryFavorites('tracks') : [];

    // Playlists live in their own tab. The shared scan status renders above
    // the list until the scan finished.
    if (allTracks.length === 0 && tidalTracks.length === 0) {
        window.FXRouteContentState.set(loadingEl, scanText ? 'loading' : 'empty',
            scanText || (hasSearch
                ? 'No matching tracks. Try a broader search.'
                : 'No tracks yet. Import a file or URL to get started.'));
        deps.getElements().tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }
    if (scanText) {
        window.FXRouteContentState.set(loadingEl, 'loading', scanText);
    } else {
        window.FXRouteContentState.hide(loadingEl);
    }

    const folderMode = deps.getState().library.viewMode === 'folders';
    const childFolders = folderMode ? getFolderChildren() : [];
    if (filteredTracks.length === 0 && childFolders.length === 0 && tidalTracks.length === 0) {
        window.FXRouteContentState.set(loadingEl, 'empty',
            hasSearch ? 'No matching tracks. Try a broader search.' : 'No tracks in this folder.');
        deps.getElements().tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }

    let html = '';

    if (folderMode) {
        html += childFolders.map(folder => `<div class="track-item folder-item" data-folder="${deps.escapeHtml(folder.path)}">
            <button class="track-play" data-folder="${deps.escapeHtml(folder.path)}" type="button" data-tooltip="Open folder ${deps.escapeHtml(folder.name)}" aria-label="Open folder ${deps.escapeHtml(folder.name)}">▶</button>
            <div class="track-info">
                <div class="track-title">${deps.escapeHtml(folder.name)}</div>
                <div class="track-artist track-sub">${folder.count} track${folder.count === 1 ? '' : 's'}</div>
            </div>
            <div class="folder-actions" aria-label="Folder actions">
                <button class="folder-action-btn" data-folder-play="${deps.escapeHtml(folder.path)}" type="button" data-tooltip="Play folder" aria-label="Play ${deps.escapeHtml(folder.name)}">▶</button>
                <button class="folder-action-btn folder-action-btn--delete" data-folder-delete="${deps.escapeHtml(folder.path)}" type="button" data-tooltip="Delete folder" aria-label="Delete ${deps.escapeHtml(folder.name)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
            </div>
        </div>`).join('');
    }

    html += filteredTracks.map(track => localTrackRowHtml(track, selectedIds.has(track.id), folderMode)).join('');

    deps.getElements().tracksList.innerHTML = html;
    appendTidalTrackRows(deps.getElements().tracksList, tidalTracks);

    bindTrackListEvents(deps.getElements().tracksList);
    updateLibrarySelectionUI();
}

function localTrackRowHtml(track, isSelected, folderMode) {
    const artist = (track.artist || '').trim();
    const album = (track.album || '').trim();
    const metadataLine = [artist, album].filter(Boolean).join(' · ');
    const subline = metadataLine || (folderMode ? getTrackFilename(track) : getTrackFolder(track));
    return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + deps.escapeHtml(track.id) + '">' +
        detailTrackRowHtml({
            title: deps.escapeHtml(track.title || 'Unknown'),
            sub: subline ? deps.escapeHtml(subline) : '',
            thumb: trackThumbHtml(track),
            favoriteButton: libraryFavoriteButtonHtml(track.id, !!track.favorite),
            selectionButton: librarySelectionButtonHtml(track.id, isSelected),
            duration: deps.formatTime(track.duration),
        }) +
    '</div>';
}

function bindTrackListEvents(list) {
    list.querySelectorAll('.track-play[data-folder]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            setLibraryFolder(item.dataset.folder || '');
        });
    });

    list.querySelectorAll('.folder-action-btn[data-folder-play]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await playLibraryFolder(btn.dataset.folderPlay || '');
        });
    });
    list.querySelectorAll('.folder-action-btn[data-folder-delete]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const folder = btn.dataset.folderDelete || '';
            await deleteLibraryFolder(folder);
        });
    });

    list.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        row.querySelector('.track-play').addEventListener('click', (e) => {
            e.stopPropagation();
            deps.playLocal(row.dataset.trackId);
        });
    });
    bindTrackFavoriteRowButtons(list);

    list.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });

    // Whole-row clicks start the row action like the album-detail rows do,
    // while selection, favorite and folder actions keep their own
    // stopPropagation handlers.
    list.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.track-add, .track-row-favorite, .track-fav, .track-play')) return;
            deps.playLocal(row.dataset.trackId);
        });
    });
    list.querySelectorAll('.folder-item[data-folder]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.folder-actions, .track-play')) return;
            setLibraryFolder(row.dataset.folder || '');
        });
    });
}

function setLibraryViewMode(mode) {
    leaveTidalLibraryDetail();
    const library = deps.getState().library;
    library.searchType = null;
    library.viewMode = LIBRARY_VIEW_MODES.includes(mode) ? mode : 'tracks';
    albumReturnArtistKey = '';
    if (library.viewMode === 'tracks') library.currentFolder = '';
    if (library.viewMode === 'tracks' || library.viewMode === 'folders') {
        renderTracks();
    } else {
        library.albumDetail = null;
        library.playlistDetail = null;
        if (library.viewMode === 'albums' && !library.albumsLoaded) {
            fetchAlbums();
        } else {
            renderTileView();
        }
    }
    updateLibrarySearchPlaceholder();
}

function setLibraryFolder(folder) {
    leaveTidalLibraryDetail();
    updateLibrarySearchPlaceholder();
    deps.getState().library.viewMode = 'folders';
    deps.getState().library.currentFolder = folder || '';
    renderTracks();
}

// Grid/list layout for tile collections (Library albums + TIDAL
// Albums/Artists/Playlists); storage helpers live above state init so the
// stored layout is available when state is first built.
function storeViewMode(surface, mode) {
    try {
        localStorage.setItem(VIEW_MODE_STORAGE_KEY + surface, mode);
    } catch (e) { /* keep working without persistence */ }
}

function setAlbumLayout(mode) {
    if (!isTileViewMode() || deps.getState().library.albumDetail || deps.getState().library.playlistDetail) return;
    const layout = mode === 'list' ? 'list' : 'grid';
    storeViewMode('library-albums', layout);
    deps.getState().library.albumLayout = layout;
    renderLibraryView();
}

// Albums, Artists, Playlists and Favorites are tile collections sharing grid,
// layout toggle and detail pages.
function isTileViewMode() {
    if (searchResultsActive()) return searchResultType() !== 'tracks';
    return ['albums', 'artists', 'playlists', 'favorites'].includes(deps.getState().library.viewMode);
}

function updateLibraryViewModeToggle() {
    const active = isTileViewMode() && !deps.getState().library.albumDetail && !deps.getState().library.playlistDetail && !deps.getState().library.tidalDetailOpen;
    if (deps.getElements().libraryViewModeToggle) deps.getElements().libraryViewModeToggle.classList.toggle('hidden', !active);
    const layout = deps.getState().library.albumLayout || 'grid';
    if (deps.getElements().libraryViewModeGridBtn) {
        deps.getElements().libraryViewModeGridBtn.classList.toggle('active', layout === 'grid');
        deps.getElements().libraryViewModeGridBtn.setAttribute('aria-pressed', layout === 'grid' ? 'true' : 'false');
    }
    if (deps.getElements().libraryViewModeListBtn) {
        deps.getElements().libraryViewModeListBtn.classList.toggle('active', layout === 'list');
        deps.getElements().libraryViewModeListBtn.setAttribute('aria-pressed', layout === 'list' ? 'true' : 'false');
    }
}

let albumsFetchInFlight = false;
let albumsRefreshPending = false;

async function fetchAlbums(force = false) {
    if (!force && deps.getState().library.albumsLoaded && deps.getState().library.albums.length > 0) return;
    if (albumsFetchInFlight) {
        if (force) albumsRefreshPending = true;
        return;
    }
    albumsFetchInFlight = true;
    try {
        const res = await fetch('/api/albums');
        if (!res.ok) throw new Error('Failed to fetch albums');
        const albums = await res.json();
        deps.getState().library.albums = albums;
        // An empty list while the scan is still running is not an answer yet:
        // keeping the view in its scan/loading state until a fetch lands after
        // the scan finished stops the empty message from flashing while the
        // albums are still arriving.
        deps.getState().library.albumsLoaded = albums.length > 0 || !deps.getState().library.scanning;
        deps.getState().library.albumsError = null;
        deps.getState().library.albumsCacheToken = Date.now();
        // Never clobber an open album/playlist detail with a background
        // re-render; the late init fetch would otherwise close the detail.
        if (isTileViewMode() && !deps.getState().library.albumDetail && !deps.getState().library.playlistDetail) {
            renderLibraryView();
        }
    } catch (e) {
        console.warn('Failed to fetch albums', e);
        // Surface the failure instead of looping: the albums view renders the
        // error state and only retries on explicit navigation or refresh.
        deps.getState().library.albumsError = e?.message || 'Failed to fetch albums';
        deps.showToast(deps.getState().library.albumsError, 'error');
        if (isTileViewMode() && !deps.getState().library.albumDetail && !deps.getState().library.playlistDetail) {
            renderLibraryView();
        }
    } finally {
        albumsFetchInFlight = false;
        if (albumsRefreshPending) {
            albumsRefreshPending = false;
            void fetchAlbums(true);
        }
    }
}

// The albums grid of the Albums and the Playlists tab.
function renderAlbums() {
    // An open TIDAL detail owns the content area; background refreshes must
    // not bring the grid back underneath it.
    if (deps.getState().library.tidalDetailOpen) return;
    if (searchResultsActive()) {
        renderSearchResults();
        return;
    }
    deps.getElements().albumsGrid.classList.remove('is-search-results');
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    updateLibrarySelectionUI();
    const loadingEl = document.querySelector('#tab-library .content-state');
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
    // Playlists shows the Smart Mixes, the local playlists and the TIDAL
    // favorite playlists; Albums the local and the TIDAL favorite albums.
    const showPlaylists = deps.getState().library.viewMode === 'playlists';

    // Hide tracks list + detail views, show albums grid
    deps.getElements().tracksList.classList.add('hidden');
    deps.getElements().albumDetail.classList.add('hidden');
    if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    if (deps.getElements().libraryFolderPath) deps.getElements().libraryFolderPath.classList.add('hidden');

    if (!deps.getState().library.albumsLoaded && showPlaylists) {
        // Playlists come from their own list: missing albums (a running scan)
        // only cost the covers, so an existing playlist stays visible.
        if (!deps.getState().library.albumsError) fetchAlbums();
    } else if (!deps.getState().library.albumsLoaded) {
        // A failed fetch reports an error state instead of looping: the view
        // only retries on explicit navigation or library refresh.
        if (deps.getState().library.albumsError) {
            window.FXRouteContentState.set(loadingEl, 'error', deps.getState().library.albumsError);
            deps.getElements().albumsGrid.classList.add('hidden');
            return;
        }
        // Albums not yet loaded — show loading state and trigger fetch. A
        // running scan reports its progress here for the same reason as in
        // the tracks view: the albums arrive when it is done.
        window.FXRouteContentState.set(loadingEl, 'loading', formatLibraryScanStatus() || 'Loading albums…');
        deps.getElements().albumsGrid.classList.add('hidden');
        fetchAlbums();
        return;
    }

    const albums = showPlaylists ? [] : (deps.getState().library.albums || []).filter(albumMatchesLibraryQuery);
    const playlists = showPlaylists ? getFilteredPlaylists() : [];
    const tidalAlbums = showPlaylists ? [] : tidalLibraryFavorites('albums');
    const tidalPlaylists = showPlaylists ? tidalLibraryFavorites('playlists') : [];

    if (albums.length === 0 && tidalAlbums.length === 0 && !showPlaylists) {
        // A running scan is not an empty library: the shared library scan
        // status stays visible until the scan actually finished, so the empty
        // message never reports "no albums" while the scan is still filling
        // the cache. The scan poll re-renders this view as tracks arrive.
        const scanText = formatLibraryScanStatus();
        if (scanText) {
            window.FXRouteContentState.set(loadingEl, 'loading', scanText);
        } else {
            window.FXRouteContentState.set(loadingEl, 'empty',
                query ? 'No matching albums.' : 'No albums found. Import music with album tags.');
        }
        deps.getElements().albumsGrid.innerHTML = '';
        deps.getElements().albumsGrid.classList.remove('hidden');
        return;
    }
    // Like the tracks view, Playlists keeps the scan status above its cards.
    const scanText = showPlaylists ? formatLibraryScanStatus() : '';
    if (scanText) window.FXRouteContentState.set(loadingEl, 'loading', scanText);
    else window.FXRouteContentState.hide(loadingEl);

    const smartHtml = showPlaylists ? `
        <div class="album-card album-card-smart" data-smart-favorite="top40" role="button" tabindex="0">
            <div class="album-art-wrap">
                <div class="album-smart-badge">Smart Mix</div>
                <img class="album-art" src="/static/Top40.png?v=${deps.getState().library.albumsCacheToken || ''}"
                     alt="Top 40"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${deps.albumArtFallbackSvg('Top 40')}'" />
            </div>
            <div class="album-name">Top 40</div>
            <div class="album-artist">Most Played Tracks</div>
        </div>
        <div class="album-card album-card-smart" data-smart-favorite="favorite-tracks" role="button" tabindex="0">
            <div class="album-art-wrap">
                <div class="album-smart-badge">Smart Mix</div>
                <img class="album-art" src="${FAVORITE_TRACKS_COVER}"
                     alt="Favorite Tracks"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${deps.albumArtFallbackSvg('Favorite Tracks')}'" />
            </div>
            <div class="album-name">Favorite Tracks</div>
            <div class="album-artist">Hearted Tracks</div>
        </div>
    ` : '';
    // Grid and list show the same cards; only the container class changes.
    deps.getElements().albumsGrid.classList.toggle('is-list', (deps.getState().library.albumLayout || 'grid') === 'list');
    const playlistHtml = playlistCardsHtml(playlists, tidalPlaylists);
    const manualHtml = albumCardsHtml(albums, tidalAlbums);
    deps.getElements().albumsGrid.innerHTML = smartHtml + playlistHtml + manualHtml;
    deps.getElements().albumsGrid.classList.remove('hidden');

    bindAlbumGridCards(deps.getElements().albumsGrid);
}

// Favorites: the hearted albums, tracks and artists of both sources, one
// section per kind. It renders into the tracks list, so the track rows keep
// their list styling; the album and artist grids nest inside it.
function renderFavorites() {
    const library = deps.getState().library;
    if (library.tidalDetailOpen) return;
    if (searchResultsActive()) {
        renderSearchResults();
        return;
    }
    const els = deps.getElements();
    const list = els.tracksList;
    const loadingEl = document.querySelector('#tab-library .content-state');
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    // The grid may still hold the markup of another view or search results.
    els.albumsGrid.classList.add('hidden');
    els.albumsGrid.classList.remove('is-search-results');
    els.albumsGrid.innerHTML = '';
    els.albumDetail.classList.add('hidden');
    if (els.playlistDetail) els.playlistDetail.classList.add('hidden');
    if (els.libraryFolderPath) els.libraryFolderPath.classList.add('hidden');
    list.classList.remove('is-search-results');
    list.classList.add('is-favorites');
    updatePlaylistSaveRowVisibility();
    updateLibrarySelectionUI();

    // The favorite flags of the local albums come with the album list.
    if (!library.albumsLoaded) {
        list.classList.add('hidden');
        if (library.albumsError) {
            window.FXRouteContentState.set(loadingEl, 'error', library.albumsError);
        } else {
            window.FXRouteContentState.set(loadingEl, 'loading', formatLibraryScanStatus() || 'Loading favorites…');
            fetchAlbums();
        }
        return;
    }

    const albums = (library.albums || []).filter(album => !!album.favorite);
    const tidalAlbums = tidalLibraryFavorites('albums');
    const tracks = (library.tracks || []).filter(track => track && track.favorite);
    const tidalTracks = tidalLibraryFavorites('tracks');
    const artists = getArtistEntries().filter(view => !!view.tidal);
    list.classList.remove('hidden');
    if (!albums.length && !tidalAlbums.length && !tracks.length && !tidalTracks.length && !artists.length) {
        const scanText = formatLibraryScanStatus();
        window.FXRouteContentState.set(loadingEl, scanText ? 'loading' : 'empty',
            scanText || 'No favorites yet. Use the heart on an album or a track.');
        list.innerHTML = '';
        return;
    }
    window.FXRouteContentState.hide(loadingEl);

    const section = (kind, label, body) => `<section class="library-search-section" data-favorites-section="${kind}">`
        + `${searchSectionHeadingHtml(label)}${body}</section>`;
    const selectedIds = new Set(library.selectedTrackIds);
    let html = '';
    if (albums.length || tidalAlbums.length) {
        html += section('albums', 'Albums', `<div class="${searchGridClass()}">${albumCardsHtml(albums, tidalAlbums)}</div>`);
    }
    if (tracks.length || tidalTracks.length) {
        html += section('tracks', 'Tracks', `<div class="library-search-rows">`
            + `${tracks.map(track => localTrackRowHtml(track, selectedIds.has(track.id), false)).join('')}</div>`);
    }
    if (artists.length) {
        html += section('artists', 'Artists', `<div class="${searchGridClass()}">${artists.map(artistCardHtml).join('')}</div>`);
    }
    list.innerHTML = html;
    // Each section binds its own cards: the album binding would also catch
    // the artist cards, and a TIDAL heart bound twice toggles back.
    const albumSection = list.querySelector('[data-favorites-section="albums"]');
    const trackSection = list.querySelector('[data-favorites-section="tracks"]');
    const artistSection = list.querySelector('[data-favorites-section="artists"]');
    if (albumSection) bindAlbumGridCards(albumSection);
    if (trackSection) {
        appendTidalTrackRows(trackSection.querySelector('.library-search-rows'), tidalTracks);
        bindTrackListEvents(trackSection);
    }
    if (artistSection) bindArtistGridCards(artistSection);
}

// The Favorites view drops an un-hearted item at once.
function refreshFavoritesView() {
    const library = deps.getState().library;
    if (library.viewMode !== 'favorites' || library.albumDetail || library.playlistDetail) return;
    renderFavorites();
}

// Local playlists and TIDAL favorite playlists as grid cards.
function playlistCardsHtml(playlists, tidalPlaylists) {
    return playlists.map(playlist => `
        <div class="album-card playlist-card" data-playlist-id="${deps.escapeHtml(playlist.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">${playlistCoverHtml(playlist)}</div>
            <button type="button" class="album-card-fav is-active" data-playlist-fav="${deps.escapeHtml(playlist.id)}" aria-label="Delete playlist" data-tooltip="Delete playlist">${deps.favoriteHeartSvg()}</button>
            <div class="album-name">${deps.escapeHtml(playlist.name)}</div>
            <div class="album-artist">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
            ${playlistHasTidal(playlist) ? deps.sourceGlyphHtml('tidal', '', { variant: 'card', link: true }) : ''}
        </div>`).join('') + tidalPlaylists.map(item => tidalCardHtml('playlist', item)).join('');
}

// Local albums and TIDAL favorite albums as one alphabetical card list.
function albumCardsHtml(albums, tidalAlbums) {
    const albumEntries = albums.map(album => ({
        various: album.name === 'Various',
        artist: String(album.artist || '').toLowerCase(),
        name: String(album.name || '').toLowerCase(),
        html: localAlbumCardHtml(album),
    })).concat(tidalAlbums.map(item => ({
        various: false,
        artist: String(item.artist || '').toLowerCase(),
        name: String(item.title || '').toLowerCase(),
        html: tidalCardHtml('album', item),
    })));
    // Local albums arrive in this order already; only a merge needs sorting.
    if (tidalAlbums.length) albumEntries.sort(compareAlbumEntries);
    return albumEntries.map(entry => entry.html).join('');
}

function bindAlbumGridCards(grid) {
    const openAlbumCard = (card) => () => {
        if (card.dataset.playlistId) openPlaylistDetail(card.dataset.playlistId);
        else if (card.dataset.smartFavorite === 'favorite-tracks') openSmartFavoriteTracks();
        else if (card.dataset.smartFavorite) openSmartTopTracks();
        else openAlbumDetail(card.dataset.albumId);
    };
    const handleAlbumCardKeydown = (card) => (event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        openAlbumCard(card)();
    };
    grid.querySelectorAll('.album-card:not([data-tidal-kind])').forEach(card => {
        card.addEventListener('click', openAlbumCard(card));
        card.addEventListener('keydown', handleAlbumCardKeydown(card));
    });
    bindTidalCards(grid);
    // Heart toggle: reuse the same local album favorite endpoint the detail
    // view uses; the card stays visible in all views (favorite toggles don't
    // remove albums from the main grid).
    grid.querySelectorAll('.album-card-fav[data-fav-id]').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleAlbumCardFavorite(btn.dataset.favId);
        });
    });
    grid.querySelectorAll('[data-playlist-fav]').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            confirmDeletePlaylist(btn.dataset.playlistFav);
        });
    });
}

// Local playlist heart (card and detail): always active (own playlist);
// heart-off deletes the playlist via the existing delete path.
function confirmDeletePlaylist(playlistId) {
    const playlist = deps.getState().playlists.find(p => p.id === playlistId);
    if (!playlist) return;
    if (!confirm(`Delete playlist "${playlist.name}"?`)) return;
    deletePlaylistById(playlistId);
}

// -- Artists -------------------------------------------------------------------
// Artists come from the local metadata: the album artist, or the track artist
// for compilations and untagged tracks. A "feat." credit is split into the
// main artist and its guests (other joins like "&" stay as written; the track
// tags are never changed). That works without TIDAL. A TIDAL favorite artist
// with the same normalized name joins its local entry and adds the image;
// TIDAL-only favorites get entries of their own. The mapping of local artists to TIDAL artists is read from the
// stored enrichment mappings (no TIDAL request).
const ARTIST_ID_PREFIX = 'artist:';
const VARIOUS_ARTIST_KEYS = new Set(['various', 'various artists']);

let localArtistIndexCache = null;
const artistMatchCache = { userId: null, index: null, pending: null, map: new Map(), remembered: new Map() };
let artistDetailToken = 0;
// Set while an album opened from an artist page is shown: Back returns there.
let albumReturnArtistKey = '';

function isVariousArtist(name) {
    return VARIOUS_ARTIST_KEYS.has(deps.nameKey(name));
}

// "Main feat. Guest" -> [Main, Guest]; "Main (ft. A) feat. B" -> [Main, A, B].
// Only the featuring joins split; a credit starting with the word stays whole.
const FEATURING_SPLIT = /(?:\s+|\s*[(\[]\s*)(?:feat\.?|ft\.|featuring)\s+/i;

function splitArtistCredit(value) {
    const text = String(value == null ? '' : value).trim();
    const parts = text.split(FEATURING_SPLIT).map(part => part.replace(/[)\]]+\s*$/, '').trim());
    if (parts.length < 2 || !parts[0]) return text ? [text] : [];
    const seen = new Set();
    return parts.filter((part) => {
        const key = deps.nameKey(part);
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

function buildArtistIndex(albums, tracks) {
    const entries = new Map();
    const touch = (name) => {
        const key = deps.nameKey(name);
        if (!key) return null;
        let entry = entries.get(key);
        if (!entry) {
            entry = { key, name: String(name).trim(), albums: [], trackIds: [], mbid: '', description: '' };
            entries.set(key, entry);
        }
        return entry;
    };
    albums.forEach((album) => {
        splitArtistCredit(album.artist).forEach((name, position) => {
            if (isVariousArtist(name)) return;
            const entry = touch(name);
            if (!entry) return;
            entry.albums.push(album);
            // The tags describe the credited artist, not its guests.
            if (position) return;
            if (!entry.mbid && album.mb_artist_id) entry.mbid = String(album.mb_artist_id);
            if (!entry.description && album.artist_description) entry.description = String(album.artist_description);
        });
    });
    tracks.forEach((track) => {
        const album = findAlbumForTrack(track);
        const albumOwned = album && !isVariousArtist(splitArtistCredit(album.artist)[0] || '');
        // The album artist owns the track; the guests credited on the track join it.
        const credited = albumOwned
            ? splitArtistCredit(album.artist).concat(splitArtistCredit(track.artist).slice(1))
            : splitArtistCredit(track.artist);
        const owners = new Set();
        credited.forEach((name) => {
            const entry = touch(name);
            if (entry && !owners.has(entry)) owners.add(entry);
        });
        owners.forEach(owner => owner.trackIds.push(track.id));
    });
    entries.forEach((entry) => {
        entry.albums.sort((a, b) => ((a.year || 9999) - (b.year || 9999))
            || String(a.name || '').localeCompare(String(b.name || ''), undefined, { sensitivity: 'base' }));
    });
    return entries;
}

function localArtistIndex() {
    const library = deps.getState().library;
    const albums = library.albums || [];
    const tracks = library.tracks || [];
    const cache = localArtistIndexCache;
    if (cache && cache.albums === albums && cache.tracks === tracks && cache.trackCount === tracks.length) {
        return cache.entries;
    }
    const entries = buildArtistIndex(albums, tracks);
    localArtistIndexCache = { albums, tracks, trackCount: tracks.length, entries };
    return entries;
}

// A TIDAL artist the entry resolves to: the favorite, else a stored mapping.
function artistTidalRef(view) {
    if (view.tidal) return view.tidal;
    return view.match || null;
}

function getArtistEntries() {
    const connected = !!(tidalLibrary() && tidalLibrary().connection().authenticated);
    const views = new Map();
    localArtistIndex().forEach((entry, key) => {
        views.set(key, Object.assign({ tidal: null, match: null }, entry,
            connected ? { match: artistMatchCache.map.get(key) || artistMatchCache.remembered.get(key) || null } : {}));
    });
    tidalLibraryFavorites('artists', { ignoreQuery: true }).forEach((item) => {
        const key = deps.nameKey(item.name);
        if (!key) return;
        let view = views.get(key);
        if (!view) {
            view = { key, name: item.name, albums: [], trackIds: [], mbid: '', description: '', tidal: null, match: null };
            views.set(key, view);
        }
        view.tidal = item;
        // Keep the reference so an un-hearted artist can be hearted again.
        artistMatchCache.remembered.set(key, item);
    });
    return Array.from(views.values()).sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
}

// Server limit per /api/artists/provider-matches request (library/api.py).
const ARTIST_MATCH_CHUNK_SIZE = 5000;

// Reads the stored TIDAL mappings in server-sized chunks. Resolves to a map by
// artist key, or null when `isCurrent` turned false while waiting.
async function fetchArtistMatches(entries, isCurrent) {
    const map = new Map();
    for (let start = 0; start < entries.length; start += ARTIST_MATCH_CHUNK_SIZE) {
        const chunk = entries.slice(start, start + ARTIST_MATCH_CHUNK_SIZE);
        const resp = await fetch('/api/artists/provider-matches', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider: 'tidal', artists: chunk.map(entry => ({ name: entry.name, mbid: entry.mbid })) }),
        });
        if (!resp.ok) throw new Error('Failed to match artists');
        const data = await resp.json();
        if (!isCurrent()) return null;
        (data.matches || []).forEach((match, position) => {
            if (!match || !chunk[position]) return;
            map.set(chunk[position].key, { id: match.provider_artist_id, name: match.name, art_url: match.art_url || '' });
        });
    }
    return map;
}

// Batch read of the stored TIDAL mappings for all local artists; the grid
// re-renders when it lands.
function ensureArtistMatches() {
    const tidal = tidalLibrary();
    const connection = tidal ? tidal.connection() : null;
    if (!connection || !connection.authenticated) {
        if (artistMatchCache.map.size || artistMatchCache.remembered.size) {
            artistMatchCache.map = new Map();
            artistMatchCache.remembered = new Map();
        }
        artistMatchCache.userId = null;
        artistMatchCache.index = null;
        return Promise.resolve();
    }
    const index = localArtistIndex();
    if (artistMatchCache.userId !== connection.userId) {
        artistMatchCache.map = new Map();
        artistMatchCache.remembered = new Map();
        artistMatchCache.index = null;
    }
    artistMatchCache.userId = connection.userId;
    if (artistMatchCache.index === index || !index.size) return artistMatchCache.pending || Promise.resolve();
    artistMatchCache.index = index;
    const entries = Array.from(index.values());
    const userId = connection.userId;
    const request = fetchArtistMatches(entries, () => artistMatchCache.userId === userId && artistMatchCache.index === index).then((map) => {
        if (!map) return;
        artistMatchCache.map = map;
        scheduleTidalLibraryRender();
    }).catch(() => {
        // Unmatched artists stay plain local entries; retry on the next visit.
        if (artistMatchCache.index === index) artistMatchCache.index = null;
    }).finally(() => {
        if (artistMatchCache.pending === request) artistMatchCache.pending = null;
    });
    artistMatchCache.pending = request;
    return request;
}

function artistCountText(view) {
    if (view.albums.length) return `${view.albums.length} album${view.albums.length === 1 ? '' : 's'}`;
    if (view.trackIds.length) return `${view.trackIds.length} track${view.trackIds.length === 1 ? '' : 's'}`;
    return 'Artist';
}

function localArtistArtHtml(view) {
    const cover = view.albums.map(albumCoverUrl).find(Boolean) || '';
    const fallbackSvg = deps.albumArtFallbackSvg(view.name);
    return `<img class="album-art" src="${deps.escapeHtml(cover || fallbackSvg)}"
                 alt="${deps.escapeHtml(view.name)}"
                 onload="this.classList.add('loaded')"
                 onerror="this.onerror=null;this.src='${fallbackSvg}'" />`;
}

function artistCardHtml(view) {
    const tidal = tidalLibrary();
    const ref = tidal ? artistTidalRef(view) : null;
    const tidalOnly = !view.albums.length && !view.trackIds.length;
    const art = ref && ref.art_url ? tidal.cardArtHtml(ref.art_url, view.name) : localArtistArtHtml(view);
    const brand = tidalOnly ? deps.sourceGlyphHtml('tidal', '', { variant: 'card', link: { kind: 'artist', id: ref ? ref.id : null } }) : '';
    const heart = ref ? tidal.favoriteButtonHtml('artists', ref.id) : '';
    return `
        <div class="album-card artist-card" data-artist-key="${deps.escapeHtml(view.key)}" role="button" tabindex="0">
            <div class="album-art-wrap">${art}</div>
            ${heart}
            <div class="album-name">${deps.escapeHtml(view.name)}</div>
            <div class="album-artist">${deps.escapeHtml(artistCountText(view))}</div>
            ${brand}
        </div>`;
}

function renderArtists() {
    if (deps.getState().library.tidalDetailOpen) return;
    if (searchResultsActive()) {
        renderSearchResults();
        return;
    }
    deps.getElements().albumsGrid.classList.remove('is-search-results');
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    updateLibrarySelectionUI();
    const els = deps.getElements();
    const loadingEl = document.querySelector('#tab-library .content-state');
    els.tracksList.classList.add('hidden');
    els.albumDetail.classList.add('hidden');
    if (els.playlistDetail) els.playlistDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    if (els.libraryFolderPath) els.libraryFolderPath.classList.add('hidden');

    if (!deps.getState().library.albumsLoaded) {
        if (deps.getState().library.albumsError) {
            window.FXRouteContentState.set(loadingEl, 'error', deps.getState().library.albumsError);
            els.albumsGrid.classList.add('hidden');
            return;
        }
        window.FXRouteContentState.set(loadingEl, 'loading', formatLibraryScanStatus() || 'Loading artists…');
        els.albumsGrid.classList.add('hidden');
        fetchAlbums();
        return;
    }

    const query = libraryQuery();
    const views = getArtistEntries().filter(view => !query || view.name.toLowerCase().includes(query));
    if (views.length === 0) {
        const scanText = formatLibraryScanStatus();
        if (scanText) window.FXRouteContentState.set(loadingEl, 'loading', scanText);
        else window.FXRouteContentState.set(loadingEl, 'empty', query ? 'No matching artists.' : 'No artists found. Import music with artist tags.');
        els.albumsGrid.innerHTML = '';
        els.albumsGrid.classList.remove('hidden');
        return;
    }
    window.FXRouteContentState.hide(loadingEl);
    els.albumsGrid.classList.toggle('is-list', (deps.getState().library.albumLayout || 'grid') === 'list');
    els.albumsGrid.innerHTML = views.map(artistCardHtml).join('');
    els.albumsGrid.classList.remove('hidden');
    bindArtistGridCards(els.albumsGrid);
    void ensureArtistMatches();
}

function bindArtistGridCards(grid) {
    grid.querySelectorAll('.artist-card[data-artist-key]').forEach((card) => {
        const open = () => openArtistDetail(card.dataset.artistKey);
        card.addEventListener('click', open);
        card.addEventListener('keydown', (event) => {
            if (event.target !== event.currentTarget) return;
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            open();
        });
    });
    const tidal = tidalLibrary();
    if (tidal) tidal.bindFavoriteButtons(grid);
}

function clearAlbumDetailExtras() {
    deps.getElements().albumDetail.querySelectorAll('.album-detail-facts, .album-detail-about, .artist-detail-section')
        .forEach(node => node.remove());
}

function compareArtistTracks(albumOrder) {
    const albumRank = (track) => {
        const rank = albumOrder.get(String(track.album || '').trim().toLowerCase());
        return rank === undefined ? albumOrder.size : rank;
    };
    return (a, b) => (albumRank(a) - albumRank(b))
        || ((a.disc_number || 0) - (b.disc_number || 0))
        || ((a.track_number || 9999) - (b.track_number || 9999))
        || String(a.title || '').localeCompare(String(b.title || ''), undefined, { sensitivity: 'base' });
}

// Local artist page. It reuses the album detail (hero, track list with
// selection/hearts/playback, Back); the artist's albums sit above the track
// list, the TIDAL part and Discover Similar below it.
async function openArtistDetail(key) {
    const view = getArtistEntries().find(item => item.key === key);
    if (!view) return;
    const tidal = tidalLibrary();
    const ref = tidal ? artistTidalRef(view) : null;
    if (!view.albums.length && !view.trackIds.length) {
        if (ref) tidal.openDetail('artist', ref.id, view.name, ref.art_url || '');
        return;
    }
    const library = deps.getState().library;
    const els = deps.getElements();
    albumReturnArtistKey = '';
    const token = ++artistDetailToken;
    const tracksById = new Map((library.tracks || []).map(track => [track.id, track]));
    const albumOrder = new Map(view.albums.map((album, index) => [String(album.name || '').trim().toLowerCase(), index]));
    const tracks = view.trackIds.map(id => tracksById.get(id)).filter(Boolean).sort(compareArtistTracks(albumOrder));
    const album = {
        id: ARTIST_ID_PREFIX + key,
        name: view.name,
        artist: 'Artist',
        smart: true,
        artistKey: key,
        tidalArtistId: ref ? String(ref.id) : '',
        summaryPrefix: view.albums.length ? `${view.albums.length} album${view.albums.length === 1 ? '' : 's'}` : '',
    };
    const isCurrent = () => token === artistDetailToken && library.albumDetail && library.albumDetail.album.id === album.id;
    library.albumDetail = { album, tracks };
    library.playlistDetail = null;
    updateLibraryViewModeToggle();

    const artUrl = (ref && ref.art_url) || view.albums.map(albumCoverUrl).find(Boolean) || '';
    setAlbumCoverImage(els.albumDetailCover, artUrl, view.name);
    setAlbumDetailBackdrop(artUrl);
    els.albumDetailName.textContent = view.name;
    els.albumDetailArtist.textContent = 'Artist';
    updateAlbumFavoriteButton(album);
    clearAlbumDetailExtras();
    renderAlbumDetailTracks();
    if (view.description) {
        els.albumDetailCount.insertAdjacentHTML('afterend', detailAboutHtml('About this artist', view.description));
    }
    if (view.albums.length) {
        const section = document.createElement('section');
        section.className = 'artist-detail-section artist-detail-albums';
        section.innerHTML = '<h4 class="streaming-results-heading">Albums</h4>'
            + `<div class="albums-grid">${view.albums.map(localAlbumCardHtml).join('')}</div>`;
        section.querySelectorAll('.album-card[data-album-id]').forEach((card) => {
            const open = () => {
                albumReturnArtistKey = key;
                openAlbumDetail(card.dataset.albumId);
            };
            card.addEventListener('click', open);
            card.addEventListener('keydown', (event) => {
                if (event.target !== event.currentTarget) return;
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                open();
            });
        });
        section.querySelectorAll('.album-card-fav[data-fav-id]').forEach((btn) => {
            btn.addEventListener('click', (event) => {
                event.preventDefault();
                event.stopPropagation();
                toggleAlbumCardFavorite(btn.dataset.favId);
            });
        });
        els.albumDetail.insertBefore(section, els.albumDetailTracks);
    }
    if (tracks.length) {
        const heading = document.createElement('h4');
        heading.className = 'streaming-results-heading artist-detail-section';
        heading.textContent = 'Tracks';
        els.albumDetail.insertBefore(heading, els.albumDetailTracks);
    }
    if (els.albumDiscover) {
        els.albumDiscover.classList.add('hidden');
        els.albumDiscover.innerHTML = '';
    }
    els.albumsGrid.classList.add('hidden');
    if (els.playlistDetail) els.playlistDetail.classList.add('hidden');
    els.albumDetail.classList.remove('hidden');
    updatePlaylistSaveRowVisibility();
    scrollLibraryDetailToTop();
    void loadArtistExtras(view, ref, isCurrent);
}

// "On <TIDAL logo>": the logo links to the artist's TIDAL page when matched.
function artistDetailSourceHeadingHtml(tidalArtistId) {
    const logo = deps.sourceGlyphHtml('tidal', '', { variant: 'heading', link: { kind: 'artist', id: tidalArtistId }, decorative: true });
    return `<h3 class="artist-detail-source"><span>On</span>${logo}<span class="visually-hidden"> TIDAL</span></h3>`;
}

// Artist page parts that need the network: the TIDAL section (when the artist
// resolves on a connected TIDAL) and Discover Similar.
async function loadArtistExtras(view, ref, isCurrent) {
    const tidal = tidalLibrary();
    const els = deps.getElements();
    let tidalData = null;
    let tidalRef = ref;
    if (tidal && tidal.connection().authenticated) {
        if (!tidalRef) {
            let ambiguous = false;
            try {
                const found = await tidal.resolveArtist(view.name);
                if (found && !found.ambiguous) tidalRef = { id: found.id, name: found.name, art_url: found.art_url };
                else if (found) ambiguous = true;
            } catch (e) { /* no TIDAL part for this artist */ }
            if (!isCurrent()) return;
            // Several TIDAL artists share the name: never guess, offer the search.
            if (ambiguous) {
                const section = document.createElement('section');
                section.className = 'artist-detail-section artist-detail-tidal';
                section.innerHTML = artistDetailSourceHeadingHtml(null)
                    + '<p class="album-discover-note">Several TIDAL artists share this name.</p>'
                    + '<button type="button" class="btn-secondary artist-search-tidal">Search on TIDAL</button>';
                section.querySelector('.artist-search-tidal').addEventListener('click', () => tidal.searchArtist(view.name));
                els.albumDetail.insertBefore(section, els.albumDiscover || null);
            }
        }
        if (tidalRef) {
            const section = document.createElement('section');
            section.className = 'artist-detail-section artist-detail-tidal';
            section.innerHTML = artistDetailSourceHeadingHtml(tidalRef.id);
            els.albumDetail.insertBefore(section, els.albumDiscover || null);
            try {
                tidalData = await tidal.renderArtistInto(section, tidalRef.id, isCurrent);
            } catch (e) {
                section.remove();
            }
            if (!isCurrent()) return;
            const mbid = tidalData && tidalData.enrichment && tidalData.enrichment.mb_artist_id;
            if (tidalData && view.mbid && mbid && String(mbid).toLowerCase() !== view.mbid.toLowerCase()) {
                // Another artist with this name: the TIDAL part does not belong here.
                section.remove();
                tidalData = null;
            } else if (tidalData && !section.querySelector('.streaming-results-heading')) {
                section.remove();
            }
            if (tidalData) {
                const detailAlbum = deps.getState().library.albumDetail.album;
                detailAlbum.tidalArtistId = String(tidalRef.id);
                updateAlbumFavoriteButton(detailAlbum);
                const about = tidalData.enrichment && tidalData.enrichment.about;
                if (about && !view.description && !els.albumDetail.querySelector('.album-detail-about')) {
                    els.albumDetailCount.insertAdjacentHTML('afterend', detailAboutHtml('About this artist', about));
                }
            }
        }
    }
    const seed = view.mbid ? view.albums.find(album => album.mb_artist_id) : null;
    if (seed) {
        await loadAlbumDiscover(seed.id, isCurrent);
    } else if (tidalData && tidalData.enrichment && tidalData.enrichment.similar) {
        renderSimilarArtists(tidalData.enrichment.similar, isCurrent);
    }
}

// Local artist for a name, when the library has one.
function openLocalArtistByName(name) {
    const key = deps.nameKey(name);
    if (!key || !localArtistIndex().has(key)) return false;
    leaveTidalLibraryDetail();
    deps.getState().library.viewMode = 'artists';
    renderLibraryViewButtons();
    updateLibrarySearchPlaceholder();
    openArtistDetail(key);
    return true;
}

function localAlbumCardHtml(album) {
    const coverUrl = albumCoverUrl(album);
    const fallbackSvg = deps.albumArtFallbackSvg(album.name || album.artist || 'Album');
    const imageSrc = coverUrl || fallbackSvg;
    const favClass = album.favorite ? ' is-active' : '';
    return `
        <div class="album-card" data-album-id="${deps.escapeHtml(album.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">
                <img class="album-art" src="${deps.escapeHtml(imageSrc)}"
                     alt="${deps.escapeHtml(album.name)}"
                     onload="this.classList.add('loaded')"
                     onerror="this.onerror=null;this.src='${fallbackSvg}'" />
            </div>
            <button type="button" class="album-card-fav${favClass}" data-fav-id="${deps.escapeHtml(album.id)}" aria-label="${album.favorite ? 'Remove from favorites' : 'Add to favorites'}" data-tooltip="${album.favorite ? 'Remove from favorites' : 'Add to favorites'}">${deps.favoriteHeartSvg()}</button>
            <div class="album-name">${deps.escapeHtml(album.name)}</div>
            <div class="album-artist">${deps.escapeHtml(album.artist)}</div>
        </div>`;
}

function albumMatchesLibraryQuery(album) {
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
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
    if (album.demo_cover_url) return album.demo_cover_url;
    return `/api/albums/${encodeURIComponent(album.id)}/cover?v=${deps.getState().library.albumsCacheToken || ''}`;
}

function setAlbumCoverImage(img, coverUrl, fallbackText) {
    if (!img) return;
    const fallbackSvg = deps.albumArtFallbackSvg(fallbackText || 'Album');
    img.onerror = function() {
        this.onerror = null;
        this.src = fallbackSvg;
    };
    img.src = coverUrl || fallbackSvg;
}

function setAlbumDetailBackdrop(coverUrl) {
    const image = deps.getElements().albumDetailBackdrop;
    const backdrop = image?.closest('.detail-header-backdrop');
    if (!image || !backdrop) return;
    if (!coverUrl) {
        image.removeAttribute('src');
        backdrop.hidden = true;
        return;
    }
    image.onerror = function() {
        this.onerror = null;
        this.removeAttribute('src');
        backdrop.hidden = true;
    };
    backdrop.hidden = false;
    image.src = coverUrl;
}

function setPlaylistDetailBackdrop(playlist) {
    const image = deps.getElements().playlistDetailBackdrop;
    const backdrop = image?.closest('.detail-header-backdrop');
    if (!image || !backdrop) return;
    // Reuse the first distinct album cover of the collage as the blurred
    // backdrop, mirroring how the album detail blurs its own cover.
    const covers = playlistCovers(playlist);
    const coverUrl = covers.length ? covers[0].url : '';
    if (!coverUrl) {
        image.removeAttribute('src');
        backdrop.hidden = true;
        return;
    }
    image.onerror = function() {
        this.onerror = null;
        this.removeAttribute('src');
        backdrop.hidden = true;
    };
    backdrop.hidden = false;
    image.src = coverUrl;
}

// Header info line for a local playlist: a single-artist playlist reuses the
// enriched MusicBrainz artist text (same source as the album About); a
// multi-artist playlist gets a compact featuring line from the actual artists.
// No artificial multi-artist biography is ever assembled.
function renderPlaylistDetailInfo(playlist, tracks, tidalTracks = []) {
    const infoEl = deps.getElements().playlistDetailInfo;
    if (!infoEl) return;
    const names = [];
    const seen = new Set();
    for (const t of [...(tracks || []), ...(tidalTracks || [])]) {
        const name = String(t.artist || '').trim();
        const key = name.toLowerCase();
        if (name && !seen.has(key)) {
            seen.add(key);
            names.push(name);
        }
    }
    if (names.length === 1) {
        const album = tracks && tracks.length ? findAlbumForTrack(tracks[0]) : null;
        const about = ((album && album.artist_description) || '').trim();
        infoEl.innerHTML = about
            ? detailAboutHtml('About this artist', about)
            : '';
        return;
    }
    const line = playlistFeaturingLine(names);
    infoEl.innerHTML = line ? `<div>${deps.escapeHtml(line)}</div>` : '';
}

function playlistFeaturingLine(names) {
    if (!names || names.length < 2) return '';
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

// Square cover thumbnail for the left of a track row. Resolves the track's
// album cover (same lookup the playlist collage uses) and falls back to the
// neutral :empty placeholder when there is no artwork.
function trackThumbHtml(track) {
    const album = findAlbumForTrack(track);
    const coverUrl = album ? albumCoverUrl(album) : '';
    const fallback = deps.artworkPlaceholderUrl();
    if (!coverUrl) {
        return '<div class="track-thumb" aria-hidden="true"><img src="' + deps.escapeHtml(fallback) +
            '" alt="" loading="lazy" /></div>';
    }
    return '<div class="track-thumb" aria-hidden="true"><img src="' + deps.escapeHtml(coverUrl) +
        '" alt="" loading="lazy" onerror="this.onerror=null;this.src=\'' + deps.escapeHtml(fallback) + '\'" /></div>';
}

function findAlbumForTrack(track) {
    const name = (track?.album || '').trim();
    if (!name) return null;
    const artist = (track.album_artist || track.artist || '').trim();
    const albums = deps.getState().library.albums || [];
    const byName = albums.filter(a => (a.name || '').trim().toLowerCase() === name.toLowerCase());
    // Exact artist + album match first (mirrors backend album grouping).
    const byArtist = byName.find(a => (a.artist || '').trim().toLowerCase() === artist.toLowerCase());
    if (byArtist) return byArtist;
    // Fall back to a unique album name (handles compilations / Various).
    if (byName.length === 1) return byName[0];
    return null;
}

function playlistDistinctAlbums(playlist) {
    const tracksById = new Map((deps.getState().library.tracks || []).map(t => [t.id, t]));
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

// Up to four distinct covers: the local albums first, then the artwork saved
// with the playlist TIDAL entries.
function playlistCovers(playlist) {
    const covers = playlistDistinctAlbums(playlist).filter(a => a?.id)
        .map(album => ({ url: albumCoverUrl(album), name: album.name || '' }));
    const meta = playlist?.track_meta || {};
    const seen = new Set(covers.map(cover => cover.url));
    for (const ref of (playlist?.track_ids || [])) {
        if (covers.length >= 4) break;
        const saved = isTidalRef(ref) ? meta[ref] : null;
        if (!saved || !saved.art_url || seen.has(saved.art_url)) continue;
        seen.add(saved.art_url);
        covers.push({ url: saved.art_url, name: saved.album || saved.title || '' });
    }
    return covers;
}

function playlistCoverHtml(playlist) {
    const covers = playlistCovers(playlist);
    if (covers.length === 0) {
        return `<div class="playlist-collage-fallback" aria-hidden="true">${playlistFallbackMarkSvg()}</div>`;
    }
    const count = Math.min(covers.length, 4);
    const cells = covers.slice(0, count).map(cover => {
        const coverUrl = cover.url;
        const isFallback = !coverUrl;
        return `<img class="playlist-collage-cell${isFallback ? ' is-fallback' : ''}"
            src="${deps.escapeHtml(coverUrl || deps.artworkPlaceholderUrl())}"
            alt="${deps.escapeHtml(cover.name)}"
            loading="lazy"
            onerror="this.onerror=null;this.classList.add('is-fallback');this.src='${deps.escapeHtml(deps.artworkPlaceholderUrl())}';" />`;
    }).join('');
    return `<div class="playlist-collage playlist-collage--${count}">${cells}</div>`;
}

function playlistFallbackMarkSvg() {
    return `<img class="playlist-collage-fallback-mark" src="${deps.escapeHtml(deps.artworkPlaceholderUrl())}" alt="" />`;
}

// The library tab has no inner scroll container (neither #tab-library nor
// #tab-content nor their ancestors set an overflow), so the grids and the
// detail views share the window scroll. Opening a detail must reset it,
// otherwise the detail inherits the grid position and starts mid-page at
// the tracks instead of at the cover/title hero.
function scrollLibraryDetailToTop() {
    window.scrollTo(0, 0);
}

async function openAlbumDetail(albumId) {
    const album = (deps.getState().library.albums || []).find(a => a.id === albumId);
    if (!album) return;

    try {
        const res = await fetch(`/api/albums/${encodeURIComponent(albumId)}/tracks`);
        if (!res.ok) return;
        const tracks = await res.json();
        deps.getState().library.albumDetail = { album, tracks };
        deps.getState().library.playlistDetail = null;
        updateLibraryViewModeToggle();

        // Update detail header
        const coverUrl = albumCoverUrl(album);
        setAlbumCoverImage(deps.getElements().albumDetailCover, coverUrl, album.name || album.artist || 'Album');
        setAlbumDetailBackdrop(coverUrl);
        deps.getElements().albumDetailName.textContent = album.name;
        deps.getElements().albumDetailArtist.textContent = album.artist;
        updateAlbumFavoriteButton(album);
        clearAlbumDetailExtras();
        const factsHtml = albumFactsHtml(album);
        if (factsHtml) {
            deps.getElements().albumDetailCount.insertAdjacentHTML('afterend', factsHtml);
        }
        const aboutHtml = albumAboutHtml(album);
        if (aboutHtml) {
            const anchor = deps.getElements().albumDetail.querySelector('.album-detail-facts') || deps.getElements().albumDetailCount;
            anchor.insertAdjacentHTML('afterend', aboutHtml);
        }

        renderAlbumDetailTracks();
        loadAlbumDiscover(albumId, () => deps.getState().library.albumDetail?.album?.id === albumId);

        // Show detail, hide grid
        deps.getElements().albumsGrid.classList.add('hidden');
        if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.add('hidden');
        deps.getElements().albumDetail.classList.remove('hidden');
        updatePlaylistSaveRowVisibility();
        scrollLibraryDetailToTop();
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
        deps.getState().library.albumDetail = { album, tracks: Array.isArray(tracks) ? tracks : [] };
        deps.getState().library.playlistDetail = null;
        updateLibraryViewModeToggle();
        const knownIds = new Set(deps.getState().library.tracks.map(t => t.id));
        for (const track of deps.getState().library.albumDetail.tracks) {
            if (track?.id && !knownIds.has(track.id)) {
                deps.getState().library.tracks.push(track);
                knownIds.add(track.id);
            }
        }
        setAlbumCoverImage(
            deps.getElements().albumDetailCover,
            `${album.coverUrl}?v=${deps.getState().library.albumsCacheToken || ''}`,
            album.name
        );
        setAlbumDetailBackdrop(`${album.coverUrl}?v=${deps.getState().library.albumsCacheToken || ''}`);
        deps.getElements().albumDetailName.textContent = album.name;
        deps.getElements().albumDetailArtist.textContent = album.artist;
        deps.getElements().albumDetailCount.textContent = `${deps.getState().library.albumDetail.tracks.length} track${deps.getState().library.albumDetail.tracks.length === 1 ? '' : 's'}`;
        updateAlbumFavoriteButton(album);
        clearAlbumDetailExtras();
        renderAlbumDetailTracks();
        if (deps.getElements().albumDiscover) {
            deps.getElements().albumDiscover.classList.add('hidden');
            deps.getElements().albumDiscover.innerHTML = '';
        }
        deps.getElements().albumsGrid.classList.add('hidden');
        if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.add('hidden');
        deps.getElements().albumDetail.classList.remove('hidden');
        updatePlaylistSaveRowVisibility();
        scrollLibraryDetailToTop();
    } catch (e) {
        console.warn('Failed to load Top 40', e);
        deps.showToast('Failed to load Top 40', 'error');
    }
}

// Smart playlist of every hearted track: local favorites in library order,
// then the TIDAL favorite tracks. Built when opened, so it always reflects
// the current hearts.
function openSmartFavoriteTracks() {
    const els = deps.getElements();
    const tracks = (deps.getState().library.tracks || []).filter(track => track && track.favorite);
    const tidalTracks = tidalLibraryFavorites('tracks', { ignoreQuery: true });
    const album = {
        id: FAVORITE_TRACKS_ID,
        name: 'Favorite Tracks',
        artist: 'Hearted Tracks',
        smart: true,
    };
    deps.getState().library.albumDetail = { album, tracks, tidalTracks };
    deps.getState().library.playlistDetail = null;
    updateLibraryViewModeToggle();
    setAlbumCoverImage(els.albumDetailCover, FAVORITE_TRACKS_COVER, album.name);
    setAlbumDetailBackdrop(FAVORITE_TRACKS_COVER);
    els.albumDetailName.textContent = album.name;
    els.albumDetailArtist.textContent = album.artist;
    updateAlbumFavoriteButton(album);
    clearAlbumDetailExtras();
    renderAlbumDetailTracks();
    if (els.albumDiscover) {
        els.albumDiscover.classList.add('hidden');
        els.albumDiscover.innerHTML = '';
    }
    els.albumsGrid.classList.add('hidden');
    if (els.playlistDetail) els.playlistDetail.classList.add('hidden');
    els.albumDetail.classList.remove('hidden');
    updatePlaylistSaveRowVisibility();
    scrollLibraryDetailToTop();
}

// Discover Similar for album and artist pages: suggestions come from the seed
// album (/api/albums/{id}/discover) and render through the same cards as the
// TIDAL detail pages. Connected, artists that map to TIDAL carry their image.
async function loadAlbumDiscover(albumId, isCurrent = () => true) {
    const host = deps.getElements().albumDiscover;
    if (!host) return;
    const tidal = tidalLibrary();
    const connected = !!(tidal && tidal.connection().authenticated);
    host.classList.remove('hidden');
    host.innerHTML = albumDiscoverShellHtml('Looking up similar artists…');
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(albumId)}/discover${connected ? '?provider=tidal' : ''}`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to load suggestions');
        if (!isCurrent()) return;
        renderSimilarArtists(data.items || [], isCurrent);
    } catch (e) {
        console.warn('Failed to load album discover suggestions', e);
        if (!isCurrent()) return;
        host.classList.add('hidden');
        host.innerHTML = '';
    }
}

function renderSimilarArtists(items, isCurrent = () => true) {
    const host = deps.getElements().albumDiscover;
    if (!host) return;
    const tidal = tidalLibrary();
    const similar = Array.isArray(items) ? items.filter(item => item && item.artist) : [];
    if (!tidal || !similar.length) {
        host.classList.add('hidden');
        host.innerHTML = '';
        return;
    }
    // Artists the library has itself show their own cover.
    const index = localArtistIndex();
    similar.forEach((item) => {
        const entry = item.art_url ? null : index.get(deps.nameKey(item.artist));
        const cover = entry && entry.albums.map(albumCoverUrl).find(Boolean);
        if (cover) item.art_url = cover;
    });
    host.classList.remove('hidden');
    host.innerHTML = '';
    tidal.renderSimilarArtists(host, similar, openSimilarArtist);
    if (tidal.connection().authenticated) tidal.hydrateSimilarArtists(host, similar, isCurrent);
}

// A similar artist opens its local page when the library has one, else its
// TIDAL page (connected); otherwise the card stays informational.
function openSimilarArtist(item) {
    if (openLocalArtistByName(item.artist)) return;
    const tidal = tidalLibrary();
    if (!tidal || !tidal.connection().authenticated) return;
    if (item.provider_artist_id) {
        tidal.openDetail('artist', item.provider_artist_id, item.artist, item.art_url || '');
        return;
    }
    tidal.resolveArtist(item.artist).then((match) => {
        if (match && !match.ambiguous) tidal.openDetail('artist', match.id, match.name || item.artist, match.art_url || '');
        else tidal.searchArtist(item.artist);
    }).catch(() => tidal.searchArtist(item.artist));
}

function albumDiscoverShellHtml(note) {
    return `
        <h4 class="streaming-results-heading">Discover Similar</h4>
        <p class="album-discover-note">${deps.escapeHtml(note)}</p>
    `;
}

function renderAlbumDetailTracks() {
    const detail = deps.getState().library.albumDetail;
    if (!detail || !deps.getElements().albumDetailTracks) return;
    const albumId = detail.album.id;
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
    const tracks = query
        ? (detail.tracks || []).filter(track => trackMatchesLibraryQuery(track, query))
        : (detail.tracks || []);
    // Favorite Tracks also lists TIDAL favorites (TIDAL rows after local).
    const allTidalTracks = detail.tidalTracks || [];
    const tidalTracks = query
        ? allTidalTracks.filter(item => tidalEntryMatchesQuery(item, query))
        : allTidalTracks;
    const total = (detail.tracks || []).length + allTidalTracks.length;
    const trackCount = query
        ? `${tracks.length + tidalTracks.length} of ${total} track${total === 1 ? '' : 's'}`
        : `${total} track${total === 1 ? '' : 's'}`;
    const album = detail.album;
    deps.getElements().albumDetailCount.textContent = [album.summaryPrefix, trackCount, album.release_type, album.year, album.country]
        .filter(Boolean).join(' · ');
    if (tracks.length === 0 && tidalTracks.length === 0) {
        deps.getElements().albumDetailTracks.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
    // Favorite Tracks plays its local and TIDAL tracks as one mixed queue.
    const mixedRefs = albumId === FAVORITE_TRACKS_ID && allTidalTracks.length
        ? (detail.tracks || []).map(track => track.id).concat(allTidalTracks.map(item => TIDAL_REF_PREFIX + item.id))
        : null;
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    deps.getElements().albumDetailTracks.innerHTML = tracks.map((track, index) => {
        const isSelected = selectedIds.has(track.id);
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + deps.escapeHtml(track.id) + '" data-album-context="' + deps.escapeHtml(albumId) + '">' +
            detailTrackRowHtml({
                index: index + 1,
                title: deps.escapeHtml(track.title || 'Unknown'),
                sub: deps.escapeHtml((track.artist || '').trim()),
                thumb: trackThumbHtml(track),
                favoriteButton: detailFavoriteButtonHtml(track.id, !!track.favorite),
                selectionButton: librarySelectionButtonHtml(track.id, isSelected),
                duration: track.duration ? deps.formatTime(track.duration) : '',
            }) +
        '</div>';
    }).join('');
    appendTidalTrackRows(deps.getElements().albumDetailTracks, tidalTracks, {
        firstIndex: tracks.length + 1,
        albumContext: true,
        favoriteClass: 'track-fav',
        onPlay: mixedRefs ? (id) => deps.playRefs(TIDAL_REF_PREFIX + id, mixedRefs) : undefined,
    });
    // Whole-row and round play-button clicks both start the track (matching
    // the Tidal detail rows); the selection Plus and favorite button stop
    // propagation themselves.
    deps.getElements().albumDetailTracks.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        const play = () => (mixedRefs
            ? deps.playRefs(row.dataset.trackId, mixedRefs)
            : playTrackInAlbum(row.dataset.trackId, row.dataset.albumContext));
        row.querySelector('.track-play').addEventListener('click', (event) => {
            event.stopPropagation();
            play();
        });
        row.addEventListener('click', (event) => {
            if (event.target && event.target.closest('.track-add, .track-fav, .track-row-favorite')) return;
            play();
        });
    });
    deps.getElements().albumDetailTracks.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });
    bindTrackFavoriteRowButtons(deps.getElements().albumDetailTracks);
}

function libraryFavoriteButtonHtml(trackId, favorite) {
    const heart = deps.favoriteHeartSvg();
    return '<button class="track-row-favorite' + (favorite ? ' active' : '') + '" data-track-favorite="' + deps.escapeHtml(trackId) + '" type="button"' +
        ' aria-pressed="' + (favorite ? 'true' : 'false') + '"' +
        ' aria-label="' + (favorite ? 'Remove track from favorites' : 'Add track to favorites') + '"' +
        ' data-tooltip="' + (favorite ? 'Remove from favorites' : 'Add to favorites') + '">' + heart + '</button>';
}

function detailFavoriteButtonHtml(trackId, favorite) {
    const heart = deps.favoriteHeartSvg();
    return '<button class="track-fav' + (favorite ? ' active' : '') + '" data-track-favorite="' + deps.escapeHtml(trackId) + '" type="button"' +
        ' aria-pressed="' + (favorite ? 'true' : 'false') + '"' +
        ' aria-label="' + (favorite ? 'Remove track from favorites' : 'Add track to favorites') + '"' +
        ' data-tooltip="' + (favorite ? 'Remove from favorites' : 'Add to favorites') + '">' + heart + '</button>';
}

function librarySelectionButtonHtml(trackId, isSelected) {
    const mark = isSelected ? '✓' : '+';
    return '<button class="track-add' + (isSelected ? ' is-active' : '') + '" data-track-add="' + deps.escapeHtml(trackId) + '" type="button"' +
        ' aria-pressed="' + (isSelected ? 'true' : 'false') + '"' +
        ' aria-label="' + (isSelected ? 'Remove track from selection' : 'Add track to selection') + '"' +
        ' data-tooltip="' + (isSelected ? 'Remove from selection' : 'Add to selection') + '">' + mark + '</button>';
}

// Shared detail track-row body for the library list view, album / playlist
// detail, and (via the init api) TIDAL detail rows.  One row language:
// optional index, round play button, stacked title / sub, optional album
// context, selection Plus, favorite, duration. Rows with an index wrap the
// number and the play button in one leading element so narrow phones can
// share a single slot; rows without an index keep a lone play button.
function detailTrackRowHtml({ index, title, sub, album, favoriteButton, selectionButton, duration, thumb, badge }) {
    const playButton = '<button type="button" class="track-play" aria-label="Play" data-tooltip="Play">▶</button>';
    const lead = index != null
        ? '<span class="track-numplay"><span class="track-index">' + index + '</span>' + playButton + '</span>'
        : playButton;
    // Optional source mark (TIDAL rows in the Library) leads the sub line.
    return (
        lead +
        (thumb || '') +
        '<div class="track-info">' +
            '<div class="track-title">' + title + '</div>' +
            ((sub || badge) ? '<div class="track-sub">' + (badge || '') + (sub || '') + '</div>' : '') +
        '</div>' +
        (album ? '<div class="track-album">' + album + '</div>' : '') +
        (selectionButton || '') +
        favoriteButton +
        (duration ? '<span class="track-duration">' + duration + '</span>' : '')
    );
}

function updateAlbumFavoriteButton(album) {
    if (!deps.getElements().albumFavoriteToggle) return;
    const tidal = album?.tidalArtistId ? tidalLibrary() : null;
    if (tidal && tidal.connection().authenticated) {
        // An artist page with a TIDAL artist hearts that artist on TIDAL.
        const favorite = tidal.isFavorite('artists', album.tidalArtistId);
        const toggle = deps.getElements().albumFavoriteToggle;
        toggle.classList.remove('hidden');
        toggle.disabled = false;
        toggle.innerHTML = deps.favoriteHeartSvg();
        toggle.classList.toggle('active', favorite);
        toggle.setAttribute('aria-pressed', favorite ? 'true' : 'false');
        toggle.setAttribute('aria-label', favorite ? 'Remove artist from TIDAL favorites' : 'Add artist to TIDAL favorites');
        toggle.setAttribute('data-tooltip', favorite ? 'Remove from TIDAL favorites' : 'Add to TIDAL favorites');
        return;
    }
    if (album?.smart) {
        deps.getElements().albumFavoriteToggle.classList.add('hidden');
        deps.getElements().albumFavoriteToggle.disabled = true;
        return;
    }
    deps.getElements().albumFavoriteToggle.classList.remove('hidden');
    deps.getElements().albumFavoriteToggle.disabled = false;
    const favorite = !!album?.favorite;
    deps.getElements().albumFavoriteToggle.innerHTML = deps.favoriteHeartSvg();
    deps.getElements().albumFavoriteToggle.classList.toggle('active', favorite);
    deps.getElements().albumFavoriteToggle.setAttribute('aria-pressed', favorite ? 'true' : 'false');
    deps.getElements().albumFavoriteToggle.setAttribute('aria-label', favorite ? 'Remove album from favorites' : 'Add album to favorites');
    deps.getElements().albumFavoriteToggle.setAttribute('data-tooltip', favorite ? 'Remove from favorites' : 'Add to favorites');
}

async function toggleCurrentAlbumFavorite() {
    const detail = deps.getState().library.albumDetail;
    const album = detail?.album;
    if (!album || !deps.getElements().albumFavoriteToggle) return;
    const tidal = album.tidalArtistId ? tidalLibrary() : null;
    if (tidal) {
        deps.getElements().albumFavoriteToggle.disabled = true;
        try {
            await tidal.toggleFavorite('artists', album.tidalArtistId);
        } finally {
            updateAlbumFavoriteButton(album);
        }
        return;
    }
    const nextFavorite = !album.favorite;
    deps.getElements().albumFavoriteToggle.disabled = true;
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(album.id)}/favorite`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorite: nextFavorite }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to update favorite');
        album.favorite = !!data.favorite;
        const stored = (deps.getState().library.albums || []).find(item => item.id === album.id);
        if (stored) stored.favorite = album.favorite;
        updateAlbumFavoriteButton(album);
        deps.showToast(album.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
    } catch (e) {
        deps.showToast(e.message || 'Failed to update favorite', 'error');
    } finally {
        deps.getElements().albumFavoriteToggle.disabled = false;
    }
}

async function toggleAlbumCardFavorite(albumId) {
    const stored = (deps.getState().library.albums || []).find(item => item.id === albumId);
    if (!stored) return;
    const nextFavorite = !stored.favorite;
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(albumId)}/favorite`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorite: nextFavorite }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to update favorite');
        stored.favorite = !!data.favorite;
        // Also update the detail-page album if it's the same one.
        const detailAlbum = deps.getState().library.albumDetail?.album;
        if (detailAlbum && detailAlbum.id === albumId) {
            detailAlbum.favorite = stored.favorite;
            updateAlbumFavoriteButton(detailAlbum);
        }
        // Update the grid buttons for this album id.
        document.querySelectorAll('.album-card-fav[data-fav-id="' + CSS.escape(albumId) + '"]').forEach((btn) => {
            const f = stored.favorite;
            btn.classList.toggle('is-active', f);
            btn.innerHTML = deps.favoriteHeartSvg();
            btn.setAttribute('aria-label', f ? 'Remove from favorites' : 'Add to favorites');
            btn.setAttribute('data-tooltip', f ? 'Remove from favorites' : 'Add to favorites');
        });
        // The Favorites view must drop an unfavorited album immediately;
        // re-render it so the filter stays authoritative.
        refreshFavoritesView();
        deps.showToast(stored.favorite ? 'Added to favorites' : 'Removed from favorites', 'success');
    } catch (e) {
        deps.showToast(e.message || 'Failed to update favorite', 'error');
    }
}

function albumFactsHtml(album) {
    const label = album.label ? `Label: ${album.label}` : '';
    const genres = (album.genres || []).slice(0, 3).filter(Boolean);
    const genreLine = genres.length ? `Genre: ${genres.join(' / ')}` : '';
    return detailFactsHtml([[label, genreLine]]);
}

// Shared metadata rows: optional fields wrap as units, without empty rows or
// dangling separators. Streaming receives the same builder via the init api.
function detailFactsHtml(lines) {
    const rows = (lines || []).map(line => {
        const fields = (Array.isArray(line) ? line : [line]).filter(Boolean);
        if (!fields.length) return '';
        return `<div class="detail-fact-row">${fields.map(field => `<span>${deps.escapeHtml(field)}</span>`).join('')}</div>`;
    }).join('');
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

// Short enrichment text is directly readable, with an accessible section label
// instead of an extra heading row or an expansion-driven layout change.
function detailAboutHtml(label, description) {
    if (!description || !description.trim()) return '';
    return `
        <section class="album-detail-about detail-description" aria-label="${deps.escapeHtml(label)}">
            <p>${deps.escapeHtml(description)}</p>
        </section>
    `;
}

function closeAlbumDetail() {
    deps.getState().library.albumDetail = null;
    // An album opened from an artist page returns to that page.
    const returnKey = albumReturnArtistKey;
    albumReturnArtistKey = '';
    if (returnKey) {
        openArtistDetail(returnKey);
        return;
    }
    // Re-render the grid (instead of just unhiding it) so newly saved
    // playlists appear as tiles as soon as the user leaves the album.
    renderLibraryView();
}

async function playTrackInAlbum(trackId, albumId) {
    const album = deps.getState().library.albumDetail;
    if (!album) return;
    const albumTrackIds = (album.tracks || []).map(t => t.id);
    // Make sure album tracks are known to the library state
    const knownIds = new Set(deps.getState().library.tracks.map(t => t.id));
    for (const t of (album.tracks || [])) {
        if (!knownIds.has(t.id)) {
            deps.getState().library.tracks.push(t);
            knownIds.add(t.id);
        }
    }
    await deps.playLocal(trackId, albumTrackIds);
}

function resolvePlaylistTracks(playlist) {
    const ids = getTrackIdsInLibraryOrder(playlist?.track_ids || []);
    const byId = new Map((deps.getState().library.tracks || []).map(t => [t.id, t]));
    return ids.map(id => byId.get(id)).filter(Boolean);
}

function openPlaylistDetail(playlistId) {
    const playlist = (deps.getState().playlists || []).find(p => p.id === playlistId);
    if (!playlist) return;
    const entries = playlistEntries(playlist);
    const tracks = entries.filter(entry => entry.track).map(entry => entry.track);
    deps.getState().library.playlistDetail = { playlist, tracks, entries };
    deps.getState().library.albumDetail = null;
    updateLibraryViewModeToggle();

    if (deps.getElements().playlistDetailCover) {
        deps.getElements().playlistDetailCover.innerHTML = playlistCoverHtml(playlist);
    }
    if (deps.getElements().playlistDetailName) deps.getElements().playlistDetailName.textContent = playlist.name;
    setPlaylistDetailBackdrop(playlist);
    renderPlaylistDetailInfo(playlist, tracks, entries.filter(entry => entry.tidal).map(entry => entry.tidal));
    renderPlaylistDetailTracks();

    deps.getElements().albumsGrid.classList.add('hidden');
    deps.getElements().albumDetail.classList.add('hidden');
    if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.remove('hidden');
    updatePlaylistSaveRowVisibility();
    scrollLibraryDetailToTop();
}

// A TIDAL entry of a saved playlist when the TIDAL module is not installed:
// listed from its saved data, not playable.
function unavailableTidalRowEl(entry, index) {
    const item = entry.tidal;
    const row = document.createElement('div');
    row.className = 'track-item is-unavailable';
    row.innerHTML = detailTrackRowHtml({
        index,
        title: deps.escapeHtml(item.title),
        sub: deps.escapeHtml([item.artist, item.album].filter(Boolean).join(' · ')),
        badge: deps.sourceGlyphHtml('tidal'),
        favoriteButton: '',
        duration: item.duration ? deps.formatTime(item.duration) : '',
    });
    return row;
}

function renderPlaylistDetailTracks() {
    const detail = deps.getState().library.playlistDetail;
    const host = deps.getElements().playlistDetailTracks;
    if (!detail || !host) return;
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
    const entries = detail.entries || (detail.tracks || []).map(track => ({ ref: track.id, track }));
    const shown = query
        ? entries.filter(entry => (entry.track ? trackMatchesLibraryQuery(entry.track, query) : tidalEntryMatchesQuery(entry.tidal, query)))
        : entries;
    const total = entries.length;
    if (deps.getElements().playlistDetailCount) {
        deps.getElements().playlistDetailCount.textContent = query
            ? `${shown.length} of ${total} track${total === 1 ? '' : 's'}`
            : `${total} track${total === 1 ? '' : 's'}`;
    }
    if (shown.length === 0) {
        host.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    host.innerHTML = shown.map((entry, index) => {
        if (!entry.track) return '<div data-tidal-slot="' + deps.escapeHtml(entry.ref) + '"></div>';
        const track = entry.track;
        const isSelected = selectedIds.has(track.id);
        const sub = deps.escapeHtml([(track.artist || '').trim(), (track.album || '').trim()].filter(Boolean).join(' · '));
        return '<div class="track-item' + (isSelected ? ' selected' : '') + '" data-track-id="' + deps.escapeHtml(track.id) + '">' +
            detailTrackRowHtml({
                index: index + 1,
                title: deps.escapeHtml(track.title || 'Unknown'),
                sub,
                thumb: trackThumbHtml(track),
                favoriteButton: detailFavoriteButtonHtml(track.id, !!track.favorite),
                selectionButton: librarySelectionButtonHtml(track.id, isSelected),
                duration: track.duration ? deps.formatTime(track.duration) : '',
            }) +
        '</div>';
    }).join('');
    // TIDAL entries are built by the TIDAL adapter (heart, + selection); a
    // click plays the whole playlist as one mixed queue. Without a connection
    // they stay listed but dimmed: the queue leaves them out.
    const tidal = tidalLibrary();
    const installed = !!(tidal && tidal.connection().installed);
    const connected = !!(tidal && tidal.connection().authenticated);
    const positions = new Map(shown.map((entry, index) => [entry.ref, index + 1]));
    const entriesByRef = new Map(shown.map(entry => [entry.ref, entry]));
    host.querySelectorAll('[data-tidal-slot]').forEach(slot => {
        const entry = entriesByRef.get(slot.dataset.tidalSlot);
        if (!entry) return;
        const index = positions.get(entry.ref);
        let row;
        if (installed) {
            row = tidal.buildTrackRow(entry.tidal, {
                tag: 'div',
                index,
                sub: [entry.tidal.artist, entry.tidal.album].filter(Boolean).join(' · '),
                queueIds: [],
                badge: deps.sourceGlyphHtml('tidal'),
                favoriteClass: 'track-fav',
                onPlay: () => playTrackInPlaylist(entry.ref),
            });
            if (!connected) row.classList.add('is-offline');
        } else {
            row = unavailableTidalRowEl(entry, index);
        }
        slot.replaceWith(row);
    });
    host.querySelectorAll('.track-item[data-track-id]').forEach(row => {
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
    host.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });
    bindTrackFavoriteRowButtons(host);
}

function closePlaylistDetail() {
    deps.getState().library.playlistDetail = null;
    renderLibraryView();
}

async function playTrackInPlaylist(trackId) {
    const detail = deps.getState().library.playlistDetail;
    if (!detail) return;
    // A playlist with TIDAL entries plays as one mixed queue.
    const refs = (detail.entries || []).map(entry => entry.ref);
    if (refs.some(isTidalRef)) {
        await deps.playRefs(trackId, refs);
        return;
    }
    const trackIds = (detail.tracks || []).map(t => t.id);
    // Make sure playlist tracks are known to the library state
    const knownIds = new Set(deps.getState().library.tracks.map(t => t.id));
    for (const t of (detail.tracks || [])) {
        if (!knownIds.has(t.id)) {
            deps.getState().library.tracks.push(t);
            knownIds.add(t.id);
        }
    }
    await deps.playLocal(trackId, trackIds);
}

async function playLibraryFolder(folder) {
    const tracks = getTracksInFolder(folder);
    if (tracks.length === 0) {
        deps.showToast('Folder has no playable tracks', 'error');
        return;
    }
    await deps.playLocal(tracks[0].id, tracks.map(track => track.id));
}

async function deleteLibraryFolder(folder) {
    const tracks = getTracksInFolder(folder);
    if (tracks.length === 0) {
        deps.showToast('Folder is already empty', 'error');
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
        deps.getState().library.currentFolder = parentFolder;
        deps.getState().library.selectedTrackIds = deps.getState().library.selectedTrackIds.filter(id => !tracks.some(track => track.id === id));
        deps.getState().library.albumsLoaded = false;
        deps.getState().library.albumsError = null;
        deps.getState().library.albumDetail = null;
        deps.showToast(`Deleted ${tracks.length} track${tracks.length === 1 ? '' : 's'}`, 'success');
        await fetchTracks();
    } catch (e) {
        deps.showToast(`Failed to delete folder: ${e.message}`, 'error');
    }
}

function toggleLibraryFolderSelection(folder) {
    const folderTrackIds = getTracksInFolder(folder).map(track => track.id);
    if (folderTrackIds.length === 0) {
        deps.showToast('Folder has no tracks to select', 'error');
        return;
    }
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    const allSelected = folderTrackIds.every(id => selectedIds.has(id));
    folderTrackIds.forEach(id => {
        if (allSelected) {
            selectedIds.delete(id);
        } else {
            selectedIds.add(id);
        }
    });
    deps.getState().library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
    deps.showToast(allSelected ? 'Folder selection cleared' : `Selected ${folderTrackIds.length} folder tracks`, 'info');
}

function toggleTrackSelected(trackId) {
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    if (selectedIds.has(trackId)) {
        selectedIds.delete(trackId);
    } else {
        selectedIds.add(trackId);
    }
    deps.getState().library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}

function clearTrackSelection() {
    deps.getState().library.selectedTrackIds = [];
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}

function tidalSelectedTrackIds() {
    const tidal = tidalLibrary();
    return tidal ? tidal.selectedIds() : [];
}

// Order in which tracks were added to the + selection, local ids and TIDAL
// references interleaved, so a mixed playlist is saved in that order. Entries
// that left the selection drop out; new ones go to the end.
let selectionOrder = [];

function orderedSelectionRefs(localIds, tidalRefs) {
    const selected = localIds.concat(tidalRefs);
    const stillSelected = new Set(selected);
    const kept = selectionOrder.filter(ref => stillSelected.has(ref));
    const known = new Set(kept);
    selectionOrder = kept.concat(selected.filter(ref => !known.has(ref)));
    return selectionOrder.slice();
}

// Save row variant per selection: local tracks save a local playlist, TIDAL
// tracks save to TIDAL (new or existing playlist), a mix saves a local playlist
// that keeps the TIDAL tracks as references.
const PLAYLIST_SAVE_MODES = {
    local: { placeholder: 'Save as playlist…', save: 'Save', hint: '' },
    tidal: { placeholder: 'New TIDAL playlist…', save: 'Save as new', hint: '' },
    mixed: {
        placeholder: 'Save as playlist…',
        save: 'Save',
        hint: 'Saved as one FXRoute playlist, in the order you added the tracks.',
    },
};

// A save request owns the Save button until it settles; row re-renders
// during that time must not re-enable it.
let playlistSaveInFlight = false;

function setPlaylistSaveInFlight(active) {
    playlistSaveInFlight = active;
    const button = deps.getElements().savePlaylistBtn;
    if (button) button.disabled = active;
}

function applyPlaylistSaveMode(mode) {
    const els = deps.getElements();
    const config = PLAYLIST_SAVE_MODES[mode] || PLAYLIST_SAVE_MODES.local;
    if (els.playlistSaveRow) els.playlistSaveRow.dataset.saveMode = mode;
    if (els.playlistName) els.playlistName.placeholder = config.placeholder;
    if (els.savePlaylistBtn) {
        els.savePlaylistBtn.textContent = config.save;
        els.savePlaylistBtn.disabled = playlistSaveInFlight;
    }
    const tidalMode = mode === 'tidal';
    if (els.playlistTidalTarget) {
        els.playlistTidalTarget.classList.toggle('hidden', !tidalMode);
        if (tidalMode) populateTidalPlaylistTarget();
    }
    if (els.playlistTidalAddBtn) els.playlistTidalAddBtn.classList.toggle('hidden', !tidalMode);
    if (els.playlistSaveHint) {
        els.playlistSaveHint.textContent = config.hint;
        els.playlistSaveHint.classList.toggle('hidden', !config.hint);
    }
}

function populateTidalPlaylistTarget() {
    const select = deps.getElements().playlistTidalTarget;
    const tidal = tidalLibrary();
    if (!select || !tidal) return;
    const current = select.value;
    const options = tidal.items('playlists')
        .filter(item => item && item.id != null)
        .map(item => `<option value="${deps.escapeHtml(item.id)}">${deps.escapeHtml(item.name || 'Playlist')}</option>`)
        .join('');
    select.innerHTML = '<option value="">Add to existing TIDAL playlist…</option>' + options;
    if (current) select.value = current;
}

async function saveTidalPlaylistFromSelection() {
    const tidal = tidalLibrary();
    const els = deps.getElements();
    const name = (els.playlistName?.value || '').trim();
    const trackIds = tidalSelectedTrackIds();
    if (!tidal) return;
    if (!name) {
        deps.showToast('Please enter a playlist name', 'error');
        return;
    }
    if (!trackIds.length) {
        deps.showToast('Select at least 1 track', 'error');
        return;
    }
    setPlaylistSaveInFlight(true);
    try {
        const data = await tidal.createPlaylist(name, trackIds);
        if (els.playlistName) els.playlistName.value = '';
        deps.showToast(data?.warning || `Saved to TIDAL: ${data?.name || name}`, data?.warning ? 'error' : 'success');
    } catch (e) {
        deps.showToast(tidal.friendlyError(e?.message || e), 'error');
    } finally {
        setPlaylistSaveInFlight(false);
        updatePlaylistSaveRowVisibility();
    }
}

async function addSelectionToTidalPlaylist() {
    const tidal = tidalLibrary();
    const els = deps.getElements();
    const select = els.playlistTidalTarget;
    const playlistId = select?.value || '';
    const trackIds = tidalSelectedTrackIds();
    if (!tidal) return;
    if (!playlistId) {
        deps.showToast('Choose a TIDAL playlist', 'error');
        return;
    }
    if (!trackIds.length) {
        deps.showToast('Select at least 1 track', 'error');
        return;
    }
    if (els.playlistTidalAddBtn) els.playlistTidalAddBtn.disabled = true;
    try {
        await tidal.addToPlaylist(playlistId, trackIds);
        const label = select.options?.[select.selectedIndex]?.textContent || 'playlist';
        deps.showToast(`Added ${trackIds.length} track${trackIds.length === 1 ? '' : 's'} to ${label}`, 'success');
    } catch (e) {
        deps.showToast(tidal.friendlyError(e?.message || e), 'error');
    } finally {
        if (els.playlistTidalAddBtn) els.playlistTidalAddBtn.disabled = false;
        updatePlaylistSaveRowVisibility();
    }
}

// Conscious end of the playlist-build selection: clears the cross-album
// selectedTrackIds (+/check marks), the name field, and the save row.
// Used by the Cancel button; Save calls clearTrackSelection on success.
function cancelPlaylistSelection() {
    const tidal = tidalLibrary();
    if (tidal && tidal.selectedIds().length) tidal.clearSelection();
    clearTrackSelection();
    if (deps.getElements().playlistName) deps.getElements().playlistName.value = '';
    updatePlaylistSaveRowVisibility();
}

// TIDAL favorite tracks listed after the local ones in Tracks join the
// selection actions; Folders and every other view stay local.
function visibleTidalTrackIds() {
    if (searchResultsActive()) {
        if (searchResultType() !== 'tracks') return [];
        return tidalLibraryFavorites('tracks').map(item => String(item.id)).concat(tidalSearch.trackIds);
    }
    if (deps.getState().library.viewMode !== 'tracks') return [];
    return tidalLibraryFavorites('tracks').map(item => String(item.id));
}

function selectAllVisibleTracks() {
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    getFilteredTracks().forEach(track => selectedIds.add(track.id));
    deps.getState().library.selectedTrackIds = Array.from(selectedIds);
    const tidal = tidalLibrary();
    const tidalIds = visibleTidalTrackIds();
    if (tidal && tidalIds.length) tidal.selectTracks(tidalIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}

function clearVisibleTrackSelection() {
    const visibleIds = new Set(getFilteredTracks().map(track => track.id));
    deps.getState().library.selectedTrackIds = deps.getState().library.selectedTrackIds.filter(id => !visibleIds.has(id));
    const tidal = tidalLibrary();
    const tidalIds = visibleTidalTrackIds();
    if (tidal && tidalIds.length) tidal.deselectTracks(tidalIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}

function toggleVisibleTrackSelection() {
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    const tidal = tidalLibrary();
    const tidalSelected = new Set(tidal ? tidal.selectedIds() : []);
    const allVisibleSelected = getFilteredTracks().every(track => selectedIds.has(track.id))
        && visibleTidalTrackIds().every(id => tidalSelected.has(id))
        && (getFilteredTracks().length + visibleTidalTrackIds().length) > 0;
    if (allVisibleSelected) {
        clearVisibleTrackSelection();
    } else {
        selectAllVisibleTracks();
    }
}

// -- Unified search ---------------------------------------------------------
// A query in Albums, Tracks, Artists or Favorites opens a results view with the
// result types Albums / Tracks / Artists / Playlists, starting at the category
// the search was typed in. Each type lists "In your library" first (local items
// and the TIDAL favorites, filtered on the client), then "TIDAL": one catalog
// request for all four types, only while TIDAL is connected. Folders and an
// open local album or playlist keep filtering in place.
const SEARCH_RESULT_TYPES = ['albums', 'tracks', 'artists', 'playlists'];
const TIDAL_SEARCH_DEBOUNCE_MS = 350;
const tidalSearch = { generation: 0, key: '', status: 'idle', data: null, error: '', timer: null, trackIds: [] };

function isArtistPageOpen() {
    const detail = deps.getState().library.albumDetail;
    return !!(detail && String(detail.album.id).startsWith(ARTIST_ID_PREFIX));
}

function searchResultsActive() {
    const library = deps.getState().library;
    if (!libraryQuery() || library.viewMode === 'folders') return false;
    return !library.albumDetail && !library.playlistDetail && !library.tidalDetailOpen;
}

function searchResultType() {
    const library = deps.getState().library;
    if (!SEARCH_RESULT_TYPES.includes(library.searchType)) {
        // Favorites starts at its first section, Albums.
        library.searchType = SEARCH_RESULT_TYPES.includes(library.viewMode) ? library.viewMode : 'albums';
    }
    return library.searchType;
}

// A type tab also leaves an open detail that was opened from the results.
function setSearchResultType(type) {
    if (!SEARCH_RESULT_TYPES.includes(type)) return;
    const library = deps.getState().library;
    library.searchType = type;
    leaveTidalLibraryDetail();
    library.albumDetail = null;
    library.playlistDetail = null;
    albumReturnArtistKey = '';
    if (searchResultsActive()) renderSearchResults();
}

// Search the Library for a name from elsewhere (a "Search on TIDAL" button).
function searchLibraryFor(query, type) {
    const library = deps.getState().library;
    leaveTidalLibraryDetail();
    library.albumDetail = null;
    library.playlistDetail = null;
    albumReturnArtistKey = '';
    if (library.viewMode === 'folders') library.viewMode = 'albums';
    library.searchType = type;
    const input = deps.getElements().librarySearchInput;
    if (input) input.value = query;
    setLibrarySearchQuery(query);
    ensureTidalSearch({ immediate: true });
}

function tidalSearchKey(connection) {
    const query = libraryQuery();
    return query ? `${connection.userId || ''}|${query}` : '';
}

function resetTidalSearch() {
    clearTimeout(tidalSearch.timer);
    Object.assign(tidalSearch, { generation: tidalSearch.generation + 1, key: '', status: 'idle', data: null, error: '', timer: null, trackIds: [] });
}

// One debounced catalog request per query; the generation keeps a slow answer
// of an older query from replacing a newer one.
function ensureTidalSearch(options = {}) {
    const tidal = tidalLibrary();
    const connection = tidal ? tidal.connection() : null;
    if (!connection || !connection.usable || !connection.authenticated) return;
    const key = tidalSearchKey(connection);
    if (!key) return;
    // A failed search is not repeated by re-rendering; only Retry or a new query searches again.
    const settled = tidalSearch.key === key && tidalSearch.status !== 'idle';
    if (settled && !options.retry) {
        if (options.immediate && tidalSearch.timer !== null) {
            clearTimeout(tidalSearch.timer);
            startTidalSearch(tidalSearch.generation, key);
        }
        return;
    }
    clearTimeout(tidalSearch.timer);
    const generation = tidalSearch.generation + 1;
    Object.assign(tidalSearch, { generation, key, status: 'loading', data: null, error: '', timer: null, trackIds: [] });
    if (options.immediate || options.retry) {
        startTidalSearch(generation, key);
    } else {
        tidalSearch.timer = setTimeout(() => startTidalSearch(generation, key), TIDAL_SEARCH_DEBOUNCE_MS);
    }
}

async function startTidalSearch(generation, key) {
    tidalSearch.timer = null;
    const tidal = tidalLibrary();
    if (!tidal || generation !== tidalSearch.generation) return;
    try {
        const data = await tidal.searchCatalog((deps.getState().library.searchQuery || '').trim());
        if (generation !== tidalSearch.generation) return;
        Object.assign(tidalSearch, { status: 'ready', data, error: '' });
    } catch (err) {
        if (generation !== tidalSearch.generation) return;
        Object.assign(tidalSearch, { status: 'error', data: null, error: tidal.friendlyError(err && err.message ? err.message : err) });
    }
    if (searchResultsActive()) fillTidalSearchSection(document.querySelector('#tab-library [data-search-section="tidal"]'), searchResultType());
}

// With a mark the label stays as screen-reader text: the logo is the heading.
function searchSectionHeadingHtml(label, glyph) {
    return `<h4 class="streaming-results-heading library-search-heading">${glyph || ''}<span${glyph ? ' class="visually-hidden"' : ''}>${deps.escapeHtml(label)}</span></h4>`;
}

function searchEmptyHtml(text) {
    return `<p class="library-search-empty">${deps.escapeHtml(text)}</p>`;
}

function searchGridClass() {
    return `albums-grid library-search-grid${(deps.getState().library.albumLayout || 'grid') === 'list' ? ' is-list' : ''}`;
}

function fillTidalSearchSection(section, type) {
    if (!section) return;
    const tidal = tidalLibrary();
    const connection = tidal ? tidal.connection() : null;
    tidalSearch.trackIds = [];
    // Optional: nothing without an installed, enabled provider.
    if (!connection || !connection.usable) {
        section.innerHTML = '';
        section.classList.add('hidden');
        return;
    }
    section.classList.remove('hidden');
    const heading = searchSectionHeadingHtml('TIDAL', deps.sourceGlyphHtml('tidal', '', { variant: 'heading', link: true, decorative: true }));
    if (!connection.authenticated) {
        section.innerHTML = heading + searchEmptyHtml('Connect TIDAL to search its catalog.')
            + '<button type="button" class="btn-secondary library-search-action">Connect TIDAL</button>';
        section.querySelector('.library-search-action').addEventListener('click', () => void tidal.beginLogin());
        return;
    }
    if (tidalSearch.status === 'error') {
        section.innerHTML = heading + searchEmptyHtml(tidalSearch.error || 'TIDAL search failed.')
            + '<button type="button" class="btn-secondary library-search-action">Retry</button>';
        section.querySelector('.library-search-action').addEventListener('click', () => {
            ensureTidalSearch({ retry: true });
            fillTidalSearchSection(section, type);
        });
        return;
    }
    if (tidalSearch.status !== 'ready' || tidalSearch.key !== tidalSearchKey(connection)) {
        section.innerHTML = heading + searchEmptyHtml('Searching TIDAL…');
        return;
    }
    // What the library section lists already is not repeated; a TIDAL artist
    // that only shares a name with a local one stays, it may be another artist.
    const listedIds = new Set(tidalLibraryFavorites(type).map(item => String(item.id)));
    if (type === 'artists') {
        searchLocalArtists().map(artistTidalRef).filter(Boolean).forEach(ref => listedIds.add(String(ref.id)));
    }
    const items = (tidalSearch.data && tidalSearch.data[type] || []).filter(item => !listedIds.has(String(item.id)));
    if (!items.length) {
        section.innerHTML = heading + searchEmptyHtml('No TIDAL results.');
        return;
    }
    if (type === 'tracks') {
        section.innerHTML = heading + '<div class="library-search-rows"></div>';
        appendTidalTrackRows(section.querySelector('.library-search-rows'), items);
        tidalSearch.trackIds = items.map(item => String(item.id));
        updateLibrarySelectionUI();
        return;
    }
    const kind = { albums: 'album', artists: 'artist', playlists: 'playlist' }[type];
    section.innerHTML = heading + `<div class="${searchGridClass()}">${items.map(item => tidalCardHtml(kind, item)).join('')}</div>`;
    bindTidalCards(section);
}

function searchLocalArtists() {
    const query = libraryQuery();
    return getArtistEntries().filter(view => view.name.toLowerCase().includes(query));
}

function renderSearchResults() {
    const library = deps.getState().library;
    const els = deps.getElements();
    const type = searchResultType();
    const tiles = type !== 'tracks';
    const loadingEl = document.querySelector('#tab-library .content-state');
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    els.albumDetail.classList.add('hidden');
    if (els.playlistDetail) els.playlistDetail.classList.add('hidden');
    if (els.libraryFolderPath) els.libraryFolderPath.classList.add('hidden');
    els.tracksList.classList.toggle('hidden', tiles);
    els.albumsGrid.classList.toggle('hidden', !tiles);
    // The other list may still hold the markup of an earlier result type.
    const unused = tiles ? els.tracksList : els.albumsGrid;
    unused.innerHTML = '';
    unused.classList.remove('is-search-results');
    els.tracksList.classList.remove('is-favorites');
    updatePlaylistSaveRowVisibility();
    // Albums and artists come from the album list.
    if ((type === 'albums' || type === 'artists') && !library.albumsLoaded) {
        if (library.albumsError) {
            window.FXRouteContentState.set(loadingEl, 'error', library.albumsError);
            els.albumsGrid.classList.add('hidden');
        } else {
            window.FXRouteContentState.set(loadingEl, 'loading', formatLibraryScanStatus() || 'Loading albums…');
            els.albumsGrid.classList.add('hidden');
            fetchAlbums();
        }
        return;
    }
    window.FXRouteContentState.hide(loadingEl);
    ensureTidalSearch();
    const libraryHeading = searchSectionHeadingHtml('In your library');
    const tidalSection = '<section class="library-search-section" data-search-section="tidal"></section>';
    if (type === 'tracks') {
        const selectedIds = new Set(library.selectedTrackIds);
        const tracks = getFilteredTracks();
        const tidalTracks = tidalLibraryFavorites('tracks');
        els.tracksList.classList.add('is-search-results');
        els.tracksList.innerHTML = `<section class="library-search-section">${libraryHeading}<div class="library-search-rows">`
            + `${tracks.map(track => localTrackRowHtml(track, selectedIds.has(track.id), false)).join('')}</div></section>${tidalSection}`;
        const rows = els.tracksList.querySelector('.library-search-rows');
        appendTidalTrackRows(rows, tidalTracks);
        if (!tracks.length && !tidalTracks.length) rows.innerHTML = searchEmptyHtml('No matches in your library.');
        bindTrackListEvents(els.tracksList);
    } else {
        let local;
        if (type === 'albums') {
            const albums = (library.albums || []).filter(albumMatchesLibraryQuery);
            const tidalAlbums = tidalLibraryFavorites('albums');
            local = { count: albums.length + tidalAlbums.length, html: albumCardsHtml(albums, tidalAlbums) };
        } else if (type === 'playlists') {
            const playlists = getFilteredPlaylists();
            const tidalPlaylists = tidalLibraryFavorites('playlists');
            local = { count: playlists.length + tidalPlaylists.length, html: playlistCardsHtml(playlists, tidalPlaylists) };
        } else {
            const views = searchLocalArtists();
            local = { count: views.length, html: views.map(artistCardHtml).join('') };
        }
        els.albumsGrid.classList.add('is-search-results');
        els.albumsGrid.classList.remove('is-list');
        els.albumsGrid.innerHTML = `<section class="library-search-section">${libraryHeading}`
            + (local.count ? `<div class="${searchGridClass()}">${local.html}</div>` : searchEmptyHtml('No matches in your library.'))
            + `</section>${tidalSection}`;
        if (type === 'artists') {
            bindArtistGridCards(els.albumsGrid);
            void ensureArtistMatches();
        } else {
            bindAlbumGridCards(els.albumsGrid);
        }
    }
    const section = document.querySelector('#tab-library [data-search-section="tidal"]');
    fillTidalSearchSection(section, type);
    updateLibrarySelectionUI();
}

function setLibrarySearchQuery(value) {
    const library = deps.getState().library;
    library.searchQuery = value || '';
    updateLibrarySearchControls();
    renderLibraryViewButtons();
    if (!libraryQuery()) {
        library.searchType = null;
        resetTidalSearch();
    } else if (library.viewMode !== 'folders') {
        // Typing on an artist page or a TIDAL detail leaves it for the results.
        if (library.tidalDetailOpen) leaveTidalLibraryDetail();
        if (isArtistPageOpen()) {
            library.albumDetail = null;
            albumReturnArtistKey = '';
        }
    } else if (library.tidalDetailOpen) {
        // Folders stay local; a TIDAL detail is not filtered.
        return;
    }
    if (searchResultsActive()) {
        renderSearchResults();
    } else if (isTileViewMode() && library.albumDetail) {
        renderAlbumDetailTracks();
    } else if (isTileViewMode() && library.playlistDetail) {
        renderPlaylistDetailTracks();
    } else if (isTileViewMode()) {
        renderTileView();
    } else {
        renderTracks();
    }
}

function updateLibrarySearchControls() {
    if (deps.getElements().librarySearchClear) {
        deps.getElements().librarySearchClear.disabled = !(deps.getState().library.searchQuery || '').trim();
    }
}

function updateLibrarySearchPlaceholder() {
    if (!deps.getElements().librarySearchInput) return;
    const compact = window.matchMedia && window.matchMedia("(max-width: 600px)").matches;
    const isArtists = deps.getState().library.viewMode === "artists";
    const isPlaylists = deps.getState().library.viewMode === "playlists";
    const isAlbums = ["albums", "favorites"].includes(deps.getState().library.viewMode);
    if (isArtists) {
        deps.getElements().librarySearchInput.placeholder = "Search artists…";
        deps.getElements().librarySearchInput.setAttribute('aria-label', "Search artists");
        return;
    }
    if (isPlaylists) {
        deps.getElements().librarySearchInput.placeholder = "Search playlists…";
        deps.getElements().librarySearchInput.setAttribute('aria-label', "Search playlists");
        return;
    }
    const fullText = isAlbums
        ? (deps.getElements().librarySearchInput.dataset.placeholderAlbumsFull || "Search album, artist, genre, year…")
        : (deps.getElements().librarySearchInput.dataset.placeholderFull || "Search folder, artist, track…");
    const compactText = isAlbums
        ? (deps.getElements().librarySearchInput.dataset.placeholderAlbumsCompact || "Search albums…")
        : (deps.getElements().librarySearchInput.dataset.placeholderCompact || "Search…");
    deps.getElements().librarySearchInput.placeholder = compact ? compactText : fullText;
    deps.getElements().librarySearchInput.setAttribute('aria-label', fullText);
}

function clearLibrarySearch() {
    if (!deps.getElements().librarySearchInput && !deps.getState().library.searchQuery) return;
    if (deps.getElements().librarySearchInput) {
        deps.getElements().librarySearchInput.value = '';
        deps.getElements().librarySearchInput.focus();
    }
    setLibrarySearchQuery('');
}

function syncRenderedTrackSelection() {
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    [deps.getElements().tracksList, deps.getElements().albumDetailTracks, deps.getElements().playlistDetailTracks].forEach(container => {
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
            btn.setAttribute('data-tooltip', active ? 'Remove from selection' : 'Add to selection');
        });
    });
}

// The save row is a single shared node. Its home is above the content lists
// (right before #tracks-list, hence above the Tracks/Folders list and above
// the Albums/Favorites grid at the same full content width), but while an
// album or playlist detail is open it docks inside the detail between header
// and tracks — like the TIDAL save row — instead of sitting misplaced under
// the track list. Placement is idempotent, so typing in the name field never
// moves the focused input.
function dockPlaylistSaveRow() {
    const els = deps.getElements();
    const row = els.playlistSaveRow;
    if (!row || !els.albumDetail || !els.albumDetailTracks) return;
    // An open TIDAL detail docks the row right below its header.
    const tidalHeader = deps.getState().library.tidalDetailOpen && els.libraryTidalDetail
        ? els.libraryTidalDetail.querySelector('.tidal-detail-header')
        : null;
    if (tidalHeader && tidalHeader.parentElement) {
        if (row.parentElement !== tidalHeader.parentElement || row.previousSibling !== tidalHeader) {
            tidalHeader.parentElement.insertBefore(row, tidalHeader.nextSibling);
        }
        return;
    }
    if (!els.albumDetail.classList.contains('hidden')) {
        if (row.parentElement !== els.albumDetail || row.nextSibling !== els.albumDetailTracks) {
            els.albumDetail.insertBefore(row, els.albumDetailTracks);
        }
        return;
    }
    if (els.playlistDetail && els.playlistDetailTracks && !els.playlistDetail.classList.contains('hidden')) {
        if (row.parentElement !== els.playlistDetail || row.nextSibling !== els.playlistDetailTracks) {
            els.playlistDetail.insertBefore(row, els.playlistDetailTracks);
        }
        return;
    }
    dockPlaylistSaveRowHome();
}

// Home above the lists: the same logical position (before the content) and
// the same full width as the docked album-detail row, for Tracks, Folders,
// Albums grid and Favorites alike. Also used to move the row out of a TIDAL
// detail before that detail re-renders (its markup is replaced wholesale).
function dockPlaylistSaveRowHome() {
    const els = deps.getElements();
    const row = els.playlistSaveRow;
    const home = els.tracksList || els.libraryInfo;
    if (!row || !home) return;
    if (row.parentElement !== home.parentElement || row.nextSibling !== home) {
        home.parentElement.insertBefore(row, home);
    }
}

function updatePlaylistSaveRowVisibility() {
    if (!deps.getElements().playlistSaveRow) return;
    const localCount = deps.getState().library.selectedTrackIds.length;
    const tidalCount = tidalSelectedTrackIds().length;
    const count = localCount + tidalCount;
    // The playlist-build selection is independent of the view mode: as soon
    // as at least one track is consciously added via the + action (never via
    // playback), the save-playlist row is reachable. Playback never touches
    // selectedTrackIds, so this trigger stays exclusive to the + selection.
    // The playlist detail follows the same selection state as every other
    // view: with no track selected the row stays hidden. Local and TIDAL
    // tracks keep separate selections; the row variant follows their mix.
    applyPlaylistSaveMode(localCount && tidalCount ? 'mixed' : tidalCount ? 'tidal' : 'local');
    orderedSelectionRefs(getSelectedPlayableTrackIds(), tidalSelectedTrackIds().map(id => TIDAL_REF_PREFIX + id));
    const hasPlaylistSelection = count >= 1;
    const showRow = hasPlaylistSelection;
    deps.getElements().playlistSaveRow.classList.toggle('hidden', !showRow);
    if (deps.getElements().playlistSaveControls) {
        deps.getElements().playlistSaveControls.classList.toggle('hidden', !showRow);
    }
    dockPlaylistSaveRow();
}

function updateLibrarySelectionUI() {
    const allTracks = deps.getState().library.tracks || [];
    const filteredTracks = getFilteredTracks();
    const filteredPlaylists = getFilteredPlaylists();
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    const visibleIds = filteredTracks.map(track => track.id);
    const tidal = tidalLibrary();
    const tidalSelected = new Set(tidal ? tidal.selectedIds() : []);
    const tidalVisibleIds = visibleTidalTrackIds();
    const visibleCount = visibleIds.length + tidalVisibleIds.length;
    const selectedVisibleCount = visibleIds.filter(id => selectedIds.has(id)).length
        + tidalVisibleIds.filter(id => tidalSelected.has(id)).length;
    const totalSelectedCount = selectedIds.size;
    const hasSearch = !!(deps.getState().library.searchQuery || '').trim();
    const isTracksMode = deps.getState().library.viewMode === 'tracks';

    // Select all: visible in tracks and folders mode, hidden in albums
    if (deps.getElements().selectAllTracksBtn) {
        const isAlbumsMode = isTileViewMode();
        const allVisibleSelected = visibleCount > 0 && selectedVisibleCount === visibleCount;
        deps.getElements().selectAllTracksBtn.classList.toggle('hidden', isAlbumsMode);
        deps.getElements().selectAllTracksBtn.disabled = isAlbumsMode || visibleCount === 0;
        if (allVisibleSelected) {
            deps.getElements().selectAllTracksBtn.textContent = hasSearch ? 'Clear visible' : 'Clear selection';
        } else {
            deps.getElements().selectAllTracksBtn.textContent = hasSearch ? 'Select visible' : 'Select all';
        }
    }

    // Download: visible in all modes when tracks selected
    if (deps.getElements().downloadSelectedTracksBtn) {
        deps.getElements().downloadSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
        deps.getElements().downloadSelectedTracksBtn.disabled = totalSelectedCount === 0 || deps.getState().library.selectionDownloadPending;
        deps.getElements().downloadSelectedTracksBtn.classList.toggle('is-busy', deps.getState().library.selectionDownloadPending);
    }

    // Delete: visible in all modes when tracks selected
    if (deps.getElements().deleteSelectedTracksBtn) {
        deps.getElements().deleteSelectedTracksBtn.classList.toggle('hidden', totalSelectedCount === 0);
    }

    if (deps.getElements().libraryInfo) {
        const playlistText = filteredPlaylists.length > 0
            ? `, ${filteredPlaylists.length} playlist${filteredPlaylists.length === 1 ? '' : 's'}`
            : '';
        const baseText = hasSearch
            ? `${filteredTracks.length} of ${allTracks.length} tracks${playlistText}`
            : `${allTracks.length} tracks`;
        if (totalSelectedCount === 0) {
            deps.getElements().libraryInfo.textContent = baseText;
        } else if (hasSearch && totalSelectedCount !== selectedVisibleCount) {
            deps.getElements().libraryInfo.textContent = `${baseText}, ${selectedVisibleCount} visible selected (${totalSelectedCount} total)`;
        } else {
            deps.getElements().libraryInfo.textContent = `${baseText}, ${totalSelectedCount} selected`;
        }
    }
    updatePlaylistSaveRowVisibility();
}

async function refreshLibrary() {
    if (deps.getState().library.scanning) return;
    deps.getState().library.scanning = true;
    deps.getState().library.scanStatus = { scanning: true, tracks_found: 0, files_seen: 0 };
    deps.getState().library.albumsLoaded = false;
    deps.getState().library.albumsError = null;
    deps.getState().library.albums = [];
    renderTracks();
    try {
        const resp = await fetch('/api/library/refresh', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok || data.status === 'error') throw new Error(data.message || 'Refresh failed');
        deps.getState().library.scanStatus = data;
        scheduleLibraryStatusPoll();
    } catch (e) {
        deps.showToast('Failed to refresh library', 'error');
        deps.getState().library.scanning = false;
        renderTracks();
    }
}

function uploadTrackFile() {
    const file = deps.getElements().uploadTrackFile.files[0];
    if (!file) {
        deps.showToast('Please choose an audio file, playlist, or ZIP', 'error');
        return;
    }
    const formData = new FormData();
    formData.append('file', file);
    const filename = file.name;
    deps.getState().upload = { filename, status_text: `Uploading ${filename}… 0%`, progress_percent: 0, status: 'uploading' };
    updateDownloadUI();
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/library/upload', true);
    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const pct = (e.loaded / e.total * 100).toFixed(1);
            deps.getState().upload.progress_percent = parseFloat(pct);
            deps.getState().upload.status_text = `Uploading ${filename}… ${pct}%`;
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
            deps.getState().upload = { filename: data.filename, status_text: successMessage, progress_percent: 100, status: 'complete' };
            updateDownloadUI();
            deps.showToast(successMessage, 'success');
            refreshLibrary();
            fetchPlaylists();
            setTimeout(() => {
                deps.getState().upload = null;
                updateDownloadUI();
            }, 2000);
        } else {
            let msg = 'Upload failed';
            try { msg = JSON.parse(xhr.responseText).detail || msg; } catch (_) {}
            resetUploadAreaSelection('upload-track-file');
            deps.getState().upload = { filename, status_text: msg, progress_percent: 0, status: 'error' };
            updateDownloadUI();
            deps.showToast(msg, 'error');
        }
    });
    xhr.addEventListener('error', () => {
        resetUploadAreaSelection('upload-track-file');
        deps.getState().upload = { filename, status_text: 'Upload failed', progress_percent: 0, status: 'error' };
        updateDownloadUI();
        deps.showToast('Upload failed', 'error');
    });
    xhr.send(formData);
}

async function savePlaylist() {
    if (playlistSaveInFlight) return;
    const mode = deps.getElements().playlistSaveRow?.dataset.saveMode || 'local';
    if (mode === 'tidal') {
        await saveTidalPlaylistFromSelection();
        return;
    }
    // A mix keeps the TIDAL tracks as references (with the display data TIDAL
    // gave us), in the order the tracks were added.
    const tidal = mode === 'mixed' ? tidalLibrary() : null;
    const tidalIds = tidal ? tidalSelectedTrackIds() : [];
    const trackIds = orderedSelectionRefs(getSelectedPlayableTrackIds(), tidalIds.map(id => TIDAL_REF_PREFIX + id));
    const name = (deps.getElements().playlistName?.value || '').trim();
    if (!name) {
        deps.showToast('Please enter a playlist name', 'error');
        return;
    }
    if (trackIds.length < 1) {
        deps.showToast('Select at least 1 track', 'error');
        return;
    }
    const body = { name, track_ids: trackIds };
    if (tidalIds.length) {
        const meta = tidal.trackMeta(tidalIds);
        body.track_meta = {};
        Object.keys(meta).forEach(id => { body.track_meta[TIDAL_REF_PREFIX + id] = meta[id]; });
    }
    setPlaylistSaveInFlight(true);
    try {
        const resp = await fetch('/api/playlists', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save playlist');
        // Success: finalize the action. Clear the name field, the selection
        // (selectedTrackIds + rendered +/check marks), and the save row.
        if (deps.getElements().playlistName) deps.getElements().playlistName.value = '';
        clearTrackSelection();
        if (tidalIds.length) tidal.clearSelection();
        await fetchPlaylists();
        deps.showToast(`Saved: ${data.playlist?.name || name}`, 'success');
    } catch (e) {
        deps.showToast(e.message || 'Failed to save playlist', 'error');
    } finally {
        setPlaylistSaveInFlight(false);
        updatePlaylistSaveRowVisibility();
    }
}

async function loadPlaylistById(playlistId, options = {}) {
    const { autoplay = false } = options;
    const playlist = deps.getState().playlists.find(item => item.id === playlistId);
    if (!playlist) {
        deps.showToast('Playlist not found', 'error');
        return;
    }
    // Local ids the library still has, in playlist order, plus every TIDAL
    // reference: the server leaves out what cannot play right now.
    const refs = playlistEntries(playlist).map(entry => entry.ref);
    if (refs.length === 0) {
        deps.showToast(`Playlist "${playlist.name}" has no playable tracks`, 'error');
        return;
    }
    const missingCount = playlist.track_ids.length - refs.length;
    if (autoplay) {
        if (missingCount > 0) {
            deps.showToast(`Starting ${refs.length}/${playlist.track_ids.length} tracks from ${playlist.name}`, 'info');
        }
        if (refs.some(isTidalRef)) await deps.playRefs(refs[0], refs);
        else await deps.playLocal(refs[0], refs);
        return;
    }
    deps.showToast(missingCount > 0
        ? `Loaded ${refs.length}/${playlist.track_ids.length} tracks from ${playlist.name}`
        : `Loaded: ${playlist.name}`, 'info');
}

async function downloadPlaylistById(playlistId) {
    const playlist = deps.getState().playlists.find(item => item.id === playlistId);
    if (!playlist) return;
    try {
        const resp = await fetch(`/api/playlists/${encodeURIComponent(playlistId)}/export`);
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            // A fail-closed export reports a structured detail object
            // ({error, message, missing_track_ids}). String() on it would
            // surface "[object Object]", so route it through the shared
            // formatter: the backend message wins, string details keep
            // working, and a message-less/absent detail falls back.
            throw new Error(deps.formatTransitionErrorDetail(data.detail, 'Playlist export failed'));
        }
        const blob = await resp.blob();
        const filename = deps.getDownloadFilenameFromResponse(resp, `${playlist.name || 'playlist'}.m3u8`);
        deps.triggerBlobDownload(blob, filename);
        // TIDAL titles cannot be exported; the file names them in comments.
        const skippedTidal = Number(resp.headers.get('X-FXRoute-Skipped-Tidal')) || 0;
        deps.showToast(skippedTidal
            ? `Downloading ${filename} — ${skippedTidal} TIDAL track${skippedTidal === 1 ? '' : 's'} not included`
            : `Downloading ${filename}`, skippedTidal ? 'info' : 'success');
    } catch (e) {
        deps.showToast(e.message || 'Playlist export failed', 'error');
    }
}

async function deletePlaylistById(playlistId) {
    const playlist = deps.getState().playlists.find(item => item.id === playlistId);
    if (!playlist) return;
    try {
        const resp = await fetch(`/api/playlists/${encodeURIComponent(playlistId)}`, { method: 'DELETE' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Delete failed');
        // Deleted from its own detail: the refresh returns to the Playlists.
        const library = deps.getState().library;
        if (library.playlistDetail?.playlist?.id === playlistId) library.playlistDetail = null;
        await Promise.all([fetchPlaylists(), fetchTracks()]);
        // renderTracks is called by both fetchPlaylists() and fetchTracks()
        deps.showToast(`Deleted: ${playlist.name}`, 'success');
    } catch (e) {
        deps.showToast(e.message || 'Failed to delete playlist', 'error');
    }
}

async function downloadSelectedTracks() {
    const trackIds = getSelectedDownloadTrackIds();
    if (trackIds.length === 0) {
        deps.showToast('Please select tracks first', 'error');
        return;
    }
    deps.getState().library.selectionDownloadPending = true;
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
        const filename = deps.getDownloadFilenameFromResponse(resp, trackIds.length === 1 ? 'track' : 'fxroute-library-selection.zip');
        deps.triggerBlobDownload(blob, filename);
        deps.showToast(trackIds.length === 1 ? `Downloading ${filename}` : `Downloading ${trackIds.length} tracks`, 'success');
    } catch (e) {
        deps.showToast(e.message || 'Download failed', 'error');
    } finally {
        deps.getState().library.selectionDownloadPending = false;
        updateLibrarySelectionUI();
    }
}

async function deleteSelectedTracks() {
    const trackIds = [...(deps.getState().library.selectedTrackIds || [])];
    if (trackIds.length === 0) {
        deps.showToast('Please select tracks first', 'error');
        return;
    }
    const label = trackIds.length === 1 ? 'this track' : `${trackIds.length} tracks`;
    if (!confirm(`Delete ${label} from the library?`)) return;
    deps.getElements().deleteSelectedTracksBtn.disabled = true;
    try {
        const resp = await fetch('/api/tracks/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Delete failed');
        const deletedCount = (data.deleted || []).length;
        deps.getState().library.selectedTrackIds = [];
        await fetchTracks();
        deps.showToast(`Deleted ${deletedCount} track${deletedCount === 1 ? '' : 's'}`, 'success');
        if ((data.errors || []).length > 0) {
            deps.showToast(`Some tracks could not be deleted`, 'error');
        }
    } catch (e) {
        deps.showToast(e.message || 'Delete failed', 'error');
    } finally {
        deps.getElements().deleteSelectedTracksBtn.disabled = false;
        updateLibrarySelectionUI();
    }
}

// Download
function setupDownloadActions() {
    if (deps.getElements().downloadUrlDropArea) {
        setupDownloadUrlDropArea();
    }
    if (deps.getElements().downloadUrl) {
        deps.getElements().downloadUrl.addEventListener('input', handleDownloadUrlInput);
        deps.getElements().downloadUrl.addEventListener('paste', () => {
            requestAnimationFrame(() => maybeStartDownloadFromInput('Pasted URL'));
        });
        deps.getElements().downloadUrl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                maybeStartDownloadFromInput('Entered URL');
            }
        });
    }
    if (deps.getElements().cancelDownloadBtn) {
        deps.getElements().cancelDownloadBtn.addEventListener('click', cancelDownload);
    }
}

function setDownloadUrlValue(url, sourceLabel = '') {
    const cleaned = (url || '').trim();
    if (!cleaned || !deps.getElements().downloadUrl) return;
    deps.getElements().downloadUrl.value = cleaned;
    if (deps.getElements().downloadUrlHint) {
        deps.getElements().downloadUrlHint.textContent = sourceLabel ? `${sourceLabel}: ${cleaned}` : cleaned;
    }
}

function handleDownloadUrlInput() {
    const value = (deps.getElements().downloadUrl?.value || '').trim();
    if (!deps.getElements().downloadUrlHint) return;
    deps.getElements().downloadUrlHint.textContent = value
        ? `URL: ${value}`
        : 'YouTube or direct media link.';
}

async function maybeStartDownloadFromInput(sourceLabel = '') {
    const value = (deps.getElements().downloadUrl?.value || '').trim();
    const match = value.match(/https?:\/\/\S+/i);
    if (!match) {
        deps.showToast('No valid URL found', 'error');
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
    if (!match) return '';
    return match[0].trim().replace(/[),.;:"'!\]]+$/, '');
}

function setupDownloadUrlDropArea() {
    const area = deps.getElements().downloadUrlDropArea;
    if (!area) return;
    const activate = () => deps.getElements().downloadUrl?.focus();
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
            deps.showToast('No URL found in dropped content', 'error');
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
        if (files.length > 1) deps.showToast(`Using first file only: ${file.name}`, 'warning');
        onFile(file);
    });
    input.addEventListener('change', () => {
        const files = Array.from(input.files || []);
        const file = files[0] || null;
        if (filenameEl) filenameEl.textContent = describeFileSelection(files);
        if (file) onFile(file);
    });
}

async function startDownload(urlOverride = null) {
    const url = (urlOverride || deps.getElements().downloadUrl.value || '').trim();
    if (!url) {
        deps.showToast('Please enter a URL', 'error');
        return;
    }
    if (deps.getState().download && ['starting', 'downloading'].includes(deps.getState().download.status)) {
        deps.showToast('Download already in progress', 'error');
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
        deps.getState().download = {
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
        deps.getElements().downloadUrl.value = '';
        if (deps.getElements().downloadUrlHint) {
            deps.getElements().downloadUrlHint.textContent = 'YouTube or direct media link.';
        }
        deps.showToast('Download started', 'info');
    } catch (e) {
        deps.showToast(e.message, 'error');
    }
}

async function cancelDownload() {
    try {
        const resp = await fetch('/api/download/cancel', { method: 'POST' });
        if (!resp.ok) throw new Error('Cancel failed');
    } catch (e) {
        deps.showToast('Failed to cancel download', 'error');
    }
}

async function fetchDownloadStatus() {
    if (deps.isPageHidden()) return;
    try {
        const resp = await fetch('/api/download/status');
        if (!resp.ok) throw new Error('Failed to fetch download status');
        const data = await resp.json();
        if (data.status === 'idle') {
            if (deps.getState().download && ['starting', 'downloading', 'complete', 'error', 'cancelled'].includes(deps.getState().download.status)) {
                stopDownloadStatusPolling();
            }
            if (!deps.getState().download || ['starting', 'downloading'].includes(deps.getState().download.status)) {
                deps.getState().download = null;
                updateDownloadUI();
            }
            lastDownloadStatus = 'idle';
            return;
        }
        deps.getState().download = data;
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
        deps.showToast(`Download complete: ${dl.filename || 'file saved'}`, 'success');
        refreshLibrary();
    } else if (dl.status === 'error' && previous !== 'error') {
        deps.showToast(`Download error: ${dl.error || 'Unknown error'}`, 'error');
    } else if (dl.status === 'cancelled' && previous !== 'cancelled') {
        deps.showToast('Download cancelled', 'info');
    }
}

function updateDownloadUI() {
    const dl = deps.getState().upload || deps.getState().download;
    if (!deps.getElements().downloadStatus || !deps.getElements().cancelDownloadBtn) return;
    if (!dl) {
        deps.getElements().downloadStatus.innerHTML = '';
        deps.getElements().downloadStatus.classList.add('hidden');
        deps.getElements().cancelDownloadBtn.classList.add('hidden');
        return;
    }
    let html = '';
    if (dl.status === 'uploading' || dl.status === 'starting' || dl.status === 'downloading') {
        const isUpload = dl.status === 'uploading';
        const progress = Number(dl.progress_percent || 0).toFixed(1);
        const label = isUpload ? 'Uploading' : 'Downloading';
        html = `
            <div class="download-progress">
                <div><strong>${deps.escapeHtml(dl.filename || label)}</strong></div>
                <div style="color: var(--text-secondary); margin-bottom: 0.35rem;">${deps.escapeHtml(dl.status_text || (isUpload ? `${label}…` : 'Preparing download…'))}</div>
                ${dl.progress_percent >= 0 ? `
                <div class="progress-bar">
                    <div class="progress-fill" style="width: ${progress}%"></div>
                </div>
                <div style="text-align: center; color: var(--text-secondary);">${progress}%</div>` : ''}
            </div>
        `;
        if (!isUpload) {
            deps.getElements().cancelDownloadBtn.classList.remove('hidden');
        }
    } else if (dl.status === 'complete') {
        html = `<div style="color: var(--success);">${deps.escapeHtml(dl.status_text || (deps.getState().upload ? 'Upload complete' : 'Download complete'))}</div>`;
        deps.getElements().cancelDownloadBtn.classList.add('hidden');
    } else if (dl.status === 'error') {
        html = `<div style="color: var(--danger);"><strong>${deps.getState().upload ? 'Upload failed' : 'Download failed'}</strong><br>${deps.escapeHtml(dl.error || dl.status_text || 'Unknown error')}</div>`;
        deps.getElements().cancelDownloadBtn.classList.add('hidden');
    } else if (dl.status === 'cancelled') {
        html = `<div style="color: var(--text-secondary);">${deps.getState().upload ? 'Upload cancelled' : 'Download cancelled'}</div>`;
        deps.getElements().cancelDownloadBtn.classList.add('hidden');
    }
    deps.getElements().downloadStatus.innerHTML = html;
    deps.getElements().downloadStatus.classList.toggle('hidden', !html.trim());
}

// -- TIDAL detail host, header button and live updates ------------------------
// TIDAL album/playlist/artist details render into #library-tidal-detail. The
// streaming module calls onOpen before each render and onClose when the
// detail chain ends; the save row is moved home first because a TIDAL render
// replaces the container markup wholesale.
function attachTidalLibraryHost() {
    const tidal = tidalLibrary();
    const container = deps.getElements().libraryTidalDetail;
    if (!tidal || !container) return;
    tidal.attachLibraryHost({
        container,
        onOpen: showTidalLibraryDetail,
        onClose: hideTidalLibraryDetail,
        onRender: () => updatePlaylistSaveRowVisibility(),
        // A similar artist the library has opens its local page.
        openLocalArtist: openLocalArtistByName,
        // The Library searches by itself (catalog included).
        search: searchLibraryFor,
    });
}

function showTidalLibraryDetail() {
    const els = deps.getElements();
    dockPlaylistSaveRowHome();
    deps.getState().library.tidalDetailOpen = true;
    [els.albumsGrid, els.tracksList, els.albumDetail, els.playlistDetail, els.libraryFolderPath].forEach(el => {
        if (el) el.classList.add('hidden');
    });
    root.FXRouteContentState?.hide(document.querySelector('#tab-library .content-state'));
    if (els.libraryTidalDetail) els.libraryTidalDetail.classList.remove('hidden');
    updateLibraryViewModeToggle();
}

function hideTidalLibraryDetail() {
    const els = deps.getElements();
    deps.getState().library.tidalDetailOpen = false;
    dockPlaylistSaveRowHome();
    if (els.libraryTidalDetail) els.libraryTidalDetail.classList.add('hidden');
    renderLibraryView();
}

// Library navigation (view tabs, folders) leaves an open TIDAL detail
// without re-rendering it; the caller renders the target view.
function leaveTidalLibraryDetail() {
    const library = deps.getState().library;
    if (!library.tidalDetailOpen) return;
    dockPlaylistSaveRowHome();
    library.tidalDetailOpen = false;
    const tidal = tidalLibrary();
    if (tidal) tidal.closeLibraryDetail();
    deps.getElements().libraryTidalDetail?.classList.add('hidden');
}

function renderTidalHeaderButton() {
    const btn = deps.getElements().libraryTidalButton;
    if (!btn) return;
    const tidal = tidalLibrary();
    const connection = tidal ? tidal.connection() : null;
    // Hidden without an installed, enabled provider: TIDAL stays optional.
    const usable = !!(connection && connection.usable);
    btn.classList.toggle('hidden', !usable);
    if (!usable) return;
    const connected = connection.authenticated === true;
    btn.classList.toggle('is-connected', connected);
    const label = btn.querySelector('.library-tidal-status-label');
    if (label) label.textContent = connected ? 'TIDAL' : 'Connect TIDAL';
    const text = connected ? 'TIDAL connected · refresh favorites' : 'Connect TIDAL';
    btn.setAttribute('aria-label', text);
    btn.setAttribute('data-tooltip', text);
}

async function onTidalHeaderButtonClick() {
    const tidal = tidalLibrary();
    const btn = deps.getElements().libraryTidalButton;
    if (!tidal || !btn || btn.disabled) return;
    if (!tidal.connection().authenticated) {
        tidal.beginLogin();
        return;
    }
    btn.disabled = true;
    try {
        await tidal.refreshCatalog();
    } finally {
        btn.disabled = false;
    }
}

let tidalConnectionKey = '';
let tidalRefreshTimer = null;

// Status events arrive on every TIDAL status read; only a changed login
// re-renders, while a visible Library always (throttled) re-reads the
// favorite collections.
function onTidalStatusChanged() {
    renderTidalHeaderButton();
    const tidal = tidalLibrary();
    const connection = tidal ? tidal.connection() : null;
    const connected = !!(connection && connection.authenticated);
    if (connected && root.__visibleTab === 'library') void tidal.ensureLibrary();
    const key = connected ? `on:${connection.userId}` : 'off';
    if (key === tidalConnectionKey) return;
    tidalConnectionKey = key;
    if (!connected && deps.getState().library.tidalDetailOpen) {
        leaveTidalLibraryDetail();
        renderLibraryView();
        return;
    }
    scheduleTidalLibraryRender();
}

// Coalesce bursts of TIDAL data events (snapshot, collections, hearts) into
// one grid/list render. Open details keep their content.
function scheduleTidalLibraryRender() {
    if (tidalRefreshTimer !== null) return;
    tidalRefreshTimer = setTimeout(() => {
        tidalRefreshTimer = null;
        const library = deps.getState().library;
        if (library.tidalDetailOpen || library.albumDetail || library.playlistDetail) {
            syncArtistDetailHeart();
            return;
        }
        if (library.viewMode === 'tracks') renderTracks();
        else if (library.viewMode !== 'folders') renderTileView();
    }, 60);
}

// The artist page heart follows the TIDAL favorite state without a reload.
function syncArtistDetailHeart() {
    const album = deps.getState().library.albumDetail?.album;
    if (album && album.tidalArtistId) updateAlbumFavoriteButton(album);
}

function setupTidalLibrary() {
    attachTidalLibraryHost();
    deps.getElements().libraryTidalButton?.addEventListener('click', () => void onTidalHeaderButtonClick());
    deps.getElements().playlistTidalAddBtn?.addEventListener('click', () => void addSelectionToTidalPlaylist());
    if (typeof root.addEventListener === 'function') {
        root.addEventListener('fxroute:tidal-status', onTidalStatusChanged);
        root.addEventListener('fxroute:tidal-library', scheduleTidalLibraryRender);
        root.addEventListener('fxroute:tidal-favorites', scheduleTidalLibraryRender);
        root.addEventListener('fxroute:tidal-selection', () => {
            updatePlaylistSaveRowVisibility();
            updateLibrarySelectionUI();
        });
    }
    renderTidalHeaderButton();
}

// Library actions
function setupLibraryActions() {
    deps.getElements().refreshLibraryBtn.addEventListener('click', refreshLibrary);
    if (deps.getElements().libraryViewTracksBtn) {
        deps.getElements().libraryViewTracksBtn.addEventListener('click', () => setLibraryViewMode('tracks'));
    }
    if (deps.getElements().libraryViewArtistsBtn) {
        deps.getElements().libraryViewArtistsBtn.addEventListener('click', () => setLibraryViewMode('artists'));
    }
    if (deps.getElements().libraryViewPlaylistsBtn) {
        deps.getElements().libraryViewPlaylistsBtn.addEventListener('click', () => setLibraryViewMode('playlists'));
    }
    if (deps.getElements().libraryViewFavoritesBtn) {
        deps.getElements().libraryViewFavoritesBtn.addEventListener('click', () => setLibraryViewMode('favorites'));
    }
    if (deps.getElements().libraryViewFoldersBtn) {
        deps.getElements().libraryViewFoldersBtn.addEventListener('click', () => setLibraryViewMode('folders'));
    }
    if (deps.getElements().libraryViewAlbumsBtn) {
        deps.getElements().libraryViewAlbumsBtn.addEventListener('click', () => setLibraryViewMode('albums'));
    }
    if (deps.getElements().libraryViewModeGridBtn) {
        deps.getElements().libraryViewModeGridBtn.addEventListener('click', () => setAlbumLayout('grid'));
    }
    if (deps.getElements().libraryViewModeListBtn) {
        deps.getElements().libraryViewModeListBtn.addEventListener('click', () => setAlbumLayout('list'));
    }
    if (deps.getElements().albumDetailBack) {
        deps.getElements().albumDetailBack.addEventListener('click', () => closeAlbumDetail());
    }
    if (deps.getElements().playlistDetailBack) {
        deps.getElements().playlistDetailBack.addEventListener('click', () => closePlaylistDetail());
    }
    const openPlaylistId = () => deps.getState().library.playlistDetail?.playlist?.id;
    if (deps.getElements().playlistDetailDownload) {
        deps.getElements().playlistDetailDownload.addEventListener('click', () => {
            const playlistId = openPlaylistId();
            if (playlistId) void downloadPlaylistById(playlistId);
        });
    }
    if (deps.getElements().playlistFavoriteToggle) {
        deps.getElements().playlistFavoriteToggle.addEventListener('click', () => {
            const playlistId = openPlaylistId();
            if (playlistId) confirmDeletePlaylist(playlistId);
        });
    }
    deps.getElements().toggleImportBtn.addEventListener('click', () => {
        const shouldOpen = deps.getElements().libraryImportPanel.classList.contains('hidden');
        if (!shouldOpen) {
            closeLibraryImportPanel();
            return;
        }
        const searchWrap = deps.getElements().librarySearchInput ? deps.getElements().librarySearchInput.closest('.library-search-wrap') : null;
        const selectionToolbar = deps.getElements().selectAllTracksBtn ? deps.getElements().selectAllTracksBtn.closest('.library-selection-toolbar') : null;
        deps.getElements().libraryImportPanel.classList.remove('hidden');
        if (searchWrap) {
            searchWrap.classList.add('hidden');
        }
        if (selectionToolbar) {
            selectionToolbar.classList.add('hidden');
        }
        if (deps.getElements().playlistSaveRow) {
            deps.getElements().playlistSaveRow.classList.add('hidden');
        }
        clearLibraryImportFeedbackIfIdle();
        resetUploadAreaSelection('upload-track-file');
        deps.getElements().toggleImportBtn.textContent = 'Close Import';
        deps.getElements().toggleImportBtn.setAttribute('aria-expanded', 'true');
    });
    if (deps.getElements().librarySearchInput) {
        updateLibrarySearchPlaceholder();
        deps.getElements().librarySearchInput.addEventListener('input', (event) => setLibrarySearchQuery(event.target.value));
        deps.getElements().librarySearchInput.addEventListener('search', (event) => setLibrarySearchQuery(event.target.value));
        deps.getElements().librarySearchInput.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                // Enter searches TIDAL at once instead of waiting for the debounce.
                event.preventDefault();
                if (searchResultsActive()) ensureTidalSearch({ immediate: true });
            } else if (event.key === 'Escape' && (deps.getState().library.searchQuery || '').trim()) {
                event.preventDefault();
                clearLibrarySearch();
            }
        });
    }
    if (deps.getElements().librarySearchTypes) {
        deps.getElements().librarySearchTypes.querySelectorAll('[data-search-type]').forEach((button) => {
            button.addEventListener('click', () => setSearchResultType(button.dataset.searchType));
        });
    }
    if (deps.getElements().librarySearchClear) {
        deps.getElements().librarySearchClear.addEventListener('click', clearLibrarySearch);
    }
    if (window.matchMedia) {
        const searchPlaceholderQuery = window.matchMedia('(max-width: 600px)');
        if (searchPlaceholderQuery.addEventListener) {
            searchPlaceholderQuery.addEventListener('change', updateLibrarySearchPlaceholder);
        } else if (searchPlaceholderQuery.addListener) {
            searchPlaceholderQuery.addListener(updateLibrarySearchPlaceholder);
        }
    }
    if (deps.getElements().selectAllTracksBtn) {
        deps.getElements().selectAllTracksBtn.addEventListener('click', toggleVisibleTrackSelection);
    }
    if (deps.getElements().albumFavoriteToggle) {
        deps.getElements().albumFavoriteToggle.addEventListener('click', toggleCurrentAlbumFavorite);
    }
    if (deps.getElements().downloadSelectedTracksBtn) {
        deps.getElements().downloadSelectedTracksBtn.addEventListener('click', downloadSelectedTracks);
    }
    if (deps.getElements().savePlaylistBtn) {
        deps.getElements().savePlaylistBtn.addEventListener('click', savePlaylist);
    }
    if (deps.getElements().cancelPlaylistSelectionBtn) {
        deps.getElements().cancelPlaylistSelectionBtn.addEventListener('click', cancelPlaylistSelection);
    }
    setupUploadArea('upload-track-area', 'upload-track-file', (file) => {
        uploadTrackFile();
    });
    deps.getElements().deleteSelectedTracksBtn.addEventListener('click', deleteSelectedTracks);
    setupTidalLibrary();
}

    return {
        init,
        albumAboutHtml,
        albumCoverUrl,
        albumDiscoverShellHtml,
        albumFactsHtml,
        albumHasCover,
        albumMatchesLibraryQuery,
        bindTrackFavoriteRowButtons,
        cancelDownload,
        cancelPlaylistSelection,
        clearLibraryImportFeedbackIfIdle,
        clearLibrarySearch,
        clearTrackSelection,
        clearVisibleTrackSelection,
        closeAlbumDetail,
        closeLibraryImportPanel,
        closePlaylistDetail,
        deleteLibraryFolder,
        deletePlaylistById,
        deleteSelectedTracks,
        detailAboutHtml,
        detailFactsHtml,
        detailFavoriteButtonHtml,
        detailTrackRowHtml,
        dockPlaylistSaveRow,
        dockPlaylistSaveRowHome,
        downloadPlaylistById,
        downloadSelectedTracks,
        extractDroppedUrl,
        fetchAlbums,
        fetchDownloadStatus,
        fetchLibraryStatus,
        fetchPlaylists,
        fetchTracks,
        findAlbumForTrack,
        findTrackById,
        formatLibraryScanStatus,
        getFilteredPlaylists,
        getFilteredTracks,
        getFolderChildren,
        getSelectedDownloadTrackIds,
        getSelectedPlayableTrackIds,
        getTrackFilename,
        getTrackFolder,
        getTrackIdsInLibraryOrder,
        getTrackRelativePath,
        getTracksInFolder,
        handleDownloadStatusTransition,
        handleDownloadUrlInput,
        isTrackInCurrentFolder,
        libraryFavoriteButtonHtml,
        librarySelectionButtonHtml,
        loadAlbumDiscover,
        loadPlaylistById,
        maybeStartDownloadFromInput,
        openAlbumDetail,
        openPlaylistDetail,
        openSmartFavoriteTracks,
        openSmartTopTracks,
        playLibraryFolder,
        playTrackInAlbum,
        playTrackInPlaylist,
        playlistCoverHtml,
        playlistDistinctAlbums,
        playlistFallbackMarkSvg,
        playlistFeaturingLine,
        playlistMatchesLibraryQuery,
        readStoredViewMode,
        refreshLibrary,
        renderAlbumDetailTracks,
        renderSimilarArtists,
        buildArtistIndex,
        splitArtistCredit,
        searchResultsActive,
        searchResultType,
        setSearchResultType,
        searchLibraryFor,
        renderArtists,
        openArtistDetail,
        renderAlbums,
        renderFavorites,
        renderLibraryFolderPath,
        renderLibraryModeButtons,
        renderLibraryView,
        renderLibraryViewButtons,
        renderPlaylistDetailInfo,
        renderPlaylistDetailTracks,
        renderTrackFavoriteButton,
        renderTidalHeaderButton,
        renderTracks,
        resetUploadAreaSelection,
        resolvePlaylistTracks,
        playlistEntries,
        isTidalRef,
        savePlaylist,
        scrollLibraryDetailToTop,
        selectAllVisibleTracks,
        setAlbumCoverImage,
        setAlbumDetailBackdrop,
        setAlbumLayout,
        setDownloadUrlValue,
        setLibraryFolder,
        setLibrarySearchQuery,
        setLibraryViewMode,
        setPlaylistDetailBackdrop,
        setupDownloadActions,
        setupDownloadUrlDropArea,
        setupLibraryActions,
        setupUploadArea,
        startDownload,
        startDownloadStatusPolling,
        stopDownloadStatusPolling,
        storeViewMode,
        syncRenderedTrackSelection,
        syncTrackFavoriteRowButtons,
        toggleAlbumCardFavorite,
        toggleCurrentAlbumFavorite,
        toggleLibraryFolderSelection,
        toggleLibraryLoop,
        toggleLibraryShuffle,
        toggleTrackFavoriteById,
        toggleTrackSelected,
        toggleVisibleTrackSelection,
        tidalLibraryFavorites,
        trackMatchesLibraryQuery,
        trackThumbHtml,
        updateAlbumFavoriteButton,
        updateDownloadUI,
        updateLibrarySearchControls,
        updateLibrarySearchPlaceholder,
        updateLibrarySelectionUI,
        updateLibraryViewModeToggle,
        updatePlaylistSaveRowVisibility,
        updateTrackFavoriteCaches,
        uploadTrackFile,
    };
});
