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
        renderFooterModeButtons: () => {},
        syncLibraryStateFromPlaybackContext: () => {},
        updatePlaybackUI: () => {},
        favoriteHeartSvg: () => '',
        artworkPlaceholderUrl: () => '',
        albumArtFallbackSvg: () => '',
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

let downloadStatusPollTimer = null;

let lastDownloadStatus = null;

const LIBRARY_SCAN_POLL_INTERVAL_MS = 1200;

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
        // canonical state/API as the TIDAL tab. While the ids are still
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
    } catch (error) {
        deps.showToast(error.message || 'Failed to update track favorite', 'error');
    } finally {
        trackFavoriteRequestInFlight = false;
        renderTrackFavoriteButton(deps.getState().playback.current_track);
        syncTrackFavoriteRowButtons();
    }
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
    if (deps.getState().library.viewMode === 'albums') {
        if (deps.getState().library.albumDetail) {
            // Re-open the album detail if we were viewing one
            const albumId = deps.getState().library.albumDetail.album.id;
            deps.getState().library.albumDetail = null;
            openAlbumDetail(albumId);
        } else if (deps.getState().library.playlistDetail) {
            const playlistId = deps.getState().library.playlistDetail.playlist.id;
            deps.getState().library.playlistDetail = null;
            openPlaylistDetail(playlistId);
        } else {
            renderAlbums();
        }
    } else {
        renderTracks();
    }
}

function renderLibraryViewButtons() {
    const mode = deps.getState().library.viewMode;
    const favActive = mode === 'albums' && !!deps.getState().library.showFavoriteAlbums;
    if (deps.getElements().libraryViewTracksBtn) {
        const active = mode === 'tracks';
        deps.getElements().libraryViewTracksBtn.classList.toggle('active', active);
        deps.getElements().libraryViewTracksBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    if (deps.getElements().libraryViewFoldersBtn) {
        const active = mode === 'folders';
        deps.getElements().libraryViewFoldersBtn.classList.toggle('active', active);
        deps.getElements().libraryViewFoldersBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    if (deps.getElements().libraryViewFavoritesBtn) {
        deps.getElements().libraryViewFavoritesBtn.classList.toggle('active', favActive);
        deps.getElements().libraryViewFavoritesBtn.setAttribute('aria-pressed', favActive ? 'true' : 'false');
    }
    if (deps.getElements().libraryViewAlbumsBtn) {
        const active = mode === 'albums' && !deps.getState().library.showFavoriteAlbums;
        deps.getElements().libraryViewAlbumsBtn.classList.toggle('active', active);
        deps.getElements().libraryViewAlbumsBtn.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
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
    const filteredPlaylists = getFilteredPlaylists();
    const validSelectedIds = allTracks.length > 0
        ? deps.getState().library.selectedTrackIds.filter(id => allTracks.some(track => track.id === id))
        : deps.getState().library.selectedTrackIds;
    const selectedIds = new Set(validSelectedIds);
    deps.getState().library.selectedTrackIds = Array.from(selectedIds);
    const loadingEl = document.querySelector('#tab-library .content-state');
    const scanText = formatLibraryScanStatus();
    const hasSearch = !!(deps.getState().library.searchQuery || '').trim();

    // Playlists count as library content: an empty track list must not wipe
    // them, because a running scan (or a library whose first scan is still
    // filling the cache) would otherwise read as "nothing here". The shared
    // scan status renders above the list until the scan finished.
    if (allTracks.length === 0 && filteredPlaylists.length === 0) {
        window.FXRouteContentState.set(loadingEl, scanText ? 'loading' : 'empty',
            scanText || (hasSearch
                ? 'No matching tracks or playlists. Try a broader search.'
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
    if (filteredTracks.length === 0 && childFolders.length === 0 && filteredPlaylists.length === 0) {
        window.FXRouteContentState.set(loadingEl, 'empty',
            hasSearch ? 'No matching tracks or playlists. Try a broader search.' : 'No tracks in this folder.');
        deps.getElements().tracksList.innerHTML = '';
        updateLibrarySelectionUI();
        return;
    }

    let html = '';

    if (!folderMode && filteredPlaylists.length > 0) {
        html += filteredPlaylists.map(playlist => {
            const classes = ['track-item', 'playlist-item'];
            return `<div class="${classes.join(' ')}" data-playlist-id="${deps.escapeHtml(playlist.id)}">
                <button class="track-play" data-playlist-id="${deps.escapeHtml(playlist.id)}" type="button" data-tooltip="Play ${deps.escapeHtml(playlist.name)}" aria-label="Play playlist ${deps.escapeHtml(playlist.name)}">▶</button>
                <div class="track-info">
                    <div class="track-title">${deps.escapeHtml(playlist.name)}</div>
                    <div class="track-artist track-sub">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
                </div>
                <button class="playlist-download-btn" data-playlist-download="${deps.escapeHtml(playlist.id)}" type="button" aria-label="Export playlist as M3U8" data-tooltip="Export playlist as M3U8">⬇</button>
                <button class="playlist-delete-btn" data-playlist-delete="${deps.escapeHtml(playlist.id)}" type="button" data-tooltip="Delete playlist" aria-label="Delete playlist"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/></svg></button>
            </div>`;
        }).join('');
    }

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

    html += filteredTracks.map(track => {
        const isSelected = selectedIds.has(track.id);
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
    }).join('');

    deps.getElements().tracksList.innerHTML = html;

    deps.getElements().tracksList.querySelectorAll('.track-play[data-folder]').forEach(item => {
        item.addEventListener('click', (e) => {
            e.stopPropagation();
            setLibraryFolder(item.dataset.folder || '');
        });
    });

    deps.getElements().tracksList.querySelectorAll('.folder-action-btn[data-folder-play]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            await playLibraryFolder(btn.dataset.folderPlay || '');
        });
    });
    deps.getElements().tracksList.querySelectorAll('.folder-action-btn[data-folder-delete]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const folder = btn.dataset.folderDelete || '';
            await deleteLibraryFolder(folder);
        });
    });

    deps.getElements().tracksList.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        row.querySelector('.track-play').addEventListener('click', (e) => {
            e.stopPropagation();
            deps.playLocal(row.dataset.trackId);
        });
    });
    bindTrackFavoriteRowButtons(deps.getElements().tracksList);

    deps.getElements().tracksList.querySelectorAll('.track-play[data-playlist-id]').forEach(item => {
        item.addEventListener('click', async (e) => {
            e.stopPropagation();
            await loadPlaylistById(item.dataset.playlistId, { autoplay: true });
        });
    });

    deps.getElements().tracksList.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });

    // Whole-row clicks start the row action like the album-detail rows do,
    // while selection, favorite and folder/playlist/detail actions keep their
    // own stopPropagation handlers.
    deps.getElements().tracksList.querySelectorAll('.track-item[data-track-id]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.track-add, .track-row-favorite, .track-fav, .track-play')) return;
            deps.playLocal(row.dataset.trackId);
        });
    });
    deps.getElements().tracksList.querySelectorAll('.folder-item[data-folder]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.folder-actions, .track-play')) return;
            setLibraryFolder(row.dataset.folder || '');
        });
    });
    deps.getElements().tracksList.querySelectorAll('.playlist-item[data-playlist-id]').forEach(row => {
        row.addEventListener('click', (e) => {
            if (e.target.closest('.playlist-download-btn, .playlist-delete-btn, .track-play')) return;
            const rowId = row.dataset.playlistId;
            if (rowId) void loadPlaylistById(rowId, { autoplay: true });
        });
    });

    deps.getElements().tracksList.querySelectorAll('.playlist-download-btn[data-playlist-download]').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const playlistId = btn.dataset.playlistDownload;
            await downloadPlaylistById(playlistId);
        });
    });
    deps.getElements().tracksList.querySelectorAll('.playlist-delete-btn[data-playlist-delete]').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const playlistId = btn.dataset.playlistDelete;
            const playlist = deps.getState().playlists.find(p => p.id === playlistId);
            if (!playlist) return;
            if (!confirm(`Delete playlist "${playlist.name}"?`)) return;
            deletePlaylistById(playlistId);
        });
    });

    updateLibrarySelectionUI();
}

function setLibraryViewMode(mode) {
    // Favorites is a tab but reuses the albums view with the existing
    // favorites filter (same position as the Playlists tab in TIDAL: fourth tab).
    if (mode === 'favorites') {
        deps.getState().library.viewMode = 'albums';
        deps.getState().library.showFavoriteAlbums = true;
    } else {
        deps.getState().library.viewMode = mode === 'folders' ? 'folders' : mode === 'albums' ? 'albums' : 'tracks';
        if (mode === 'albums') deps.getState().library.showFavoriteAlbums = false;
    }
    if (deps.getState().library.viewMode === 'tracks') deps.getState().library.currentFolder = '';
    if (deps.getState().library.viewMode === 'albums') {
        deps.getState().library.albumDetail = null;
        deps.getState().library.playlistDetail = null;
        if (!deps.getState().library.albumsLoaded) {
            fetchAlbums();
        } else {
            renderAlbums();
        }
    } else {
        renderTracks();
    }
    updateLibrarySearchPlaceholder();
}

function setLibraryFolder(folder) {
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
    if (deps.getState().library.viewMode !== 'albums' || deps.getState().library.albumDetail || deps.getState().library.playlistDetail) return;
    const layout = mode === 'list' ? 'list' : 'grid';
    storeViewMode('library-albums', layout);
    deps.getState().library.albumLayout = layout;
    renderAlbums();
}

function updateLibraryViewModeToggle() {
    const active = deps.getState().library.viewMode === 'albums' && !deps.getState().library.albumDetail && !deps.getState().library.playlistDetail;
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

async function fetchAlbums() {
    if (deps.getState().library.albumsLoaded && deps.getState().library.albums.length > 0) return;
    if (albumsFetchInFlight) return;
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
        if (deps.getState().library.viewMode === 'albums' && !deps.getState().library.albumDetail && !deps.getState().library.playlistDetail) {
            renderAlbums();
        }
    } catch (e) {
        console.warn('Failed to fetch albums', e);
        // Surface the failure instead of looping: the albums view renders the
        // error state and only retries on explicit navigation or refresh.
        deps.getState().library.albumsError = e?.message || 'Failed to fetch albums';
        deps.showToast(deps.getState().library.albumsError, 'error');
        if (deps.getState().library.viewMode === 'albums' && !deps.getState().library.albumDetail && !deps.getState().library.playlistDetail) {
            renderAlbums();
        }
    } finally {
        albumsFetchInFlight = false;
    }
}

function renderAlbums() {
    renderLibraryViewButtons();
    updateLibraryViewModeToggle();
    updateLibrarySelectionUI();
    const loadingEl = document.querySelector('#tab-library .content-state');
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();

    // Hide tracks list + detail views, show albums grid
    deps.getElements().tracksList.classList.add('hidden');
    deps.getElements().albumDetail.classList.add('hidden');
    if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.add('hidden');
    updatePlaylistSaveRowVisibility();
    if (deps.getElements().libraryFolderPath) deps.getElements().libraryFolderPath.classList.add('hidden');

    if (!deps.getState().library.albumsLoaded) {
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

    let albums = deps.getState().library.albums || [];
    if (query) {
        albums = albums.filter(albumMatchesLibraryQuery);
    }
    if (deps.getState().library.showFavoriteAlbums) {
        albums = albums.filter(album => !!album.favorite);
    }
    const showSmartFavorites = deps.getState().library.showFavoriteAlbums;
    // Playlists live with the personal collections (Favorites), not in the
    // plain albums overview.
    const playlists = showSmartFavorites ? getFilteredPlaylists() : [];

    if (albums.length === 0 && playlists.length === 0 && !showSmartFavorites) {
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
    window.FXRouteContentState.hide(loadingEl);

    const smartHtml = showSmartFavorites ? `
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
    ` : '';
    // Grid and list show the same cards; only the container class changes.
    deps.getElements().albumsGrid.classList.toggle('is-list', (deps.getState().library.albumLayout || 'grid') === 'list');
    const playlistHtml = playlists.map(playlist => `
        <div class="album-card playlist-card" data-playlist-id="${deps.escapeHtml(playlist.id)}" role="button" tabindex="0">
            <div class="album-art-wrap">${playlistCoverHtml(playlist)}</div>
            <button type="button" class="album-card-fav is-active" data-playlist-fav="${deps.escapeHtml(playlist.id)}" aria-label="Delete playlist" data-tooltip="Delete playlist">${deps.favoriteHeartSvg()}</button>
            <div class="album-name">${deps.escapeHtml(playlist.name)}</div>
            <div class="album-artist">${playlist.track_count} track${playlist.track_count === 1 ? '' : 's'}</div>
        </div>`).join('');
    const manualHtml = albums.length > 0 ? albums.map(album => {
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
    }).join('') : '';
    deps.getElements().albumsGrid.innerHTML = smartHtml + playlistHtml + manualHtml;
    deps.getElements().albumsGrid.classList.remove('hidden');

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
    deps.getElements().albumsGrid.querySelectorAll('.album-card').forEach(card => {
        card.addEventListener('click', openAlbumCard(card));
        card.addEventListener('keydown', handleAlbumCardKeydown(card));
    });
    // Heart toggle: reuse the same local album favorite endpoint the detail
    // view uses; the card stays visible in all views (favorite toggles don't
    // remove albums from the main grid).
    deps.getElements().albumsGrid.querySelectorAll('.album-card-fav').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleAlbumCardFavorite(btn.dataset.favId);
        });
    });
    // Local playlist heart: always active (own playlist); heart-off deletes
    // the playlist via the existing delete path.
    deps.getElements().albumsGrid.querySelectorAll('[data-playlist-fav]').forEach((btn) => {
        btn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            const playlistId = btn.dataset.playlistFav;
            const playlist = deps.getState().playlists.find(p => p.id === playlistId);
            if (!playlist) return;
            if (!confirm(`Delete playlist "${playlist.name}"?`)) return;
            deletePlaylistById(playlistId);
        });
    });
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
    const albums = playlistDistinctAlbums(playlist).filter(a => a?.id);
    const coverUrl = albums.length ? albumCoverUrl(albums[0]) : '';
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
function renderPlaylistDetailInfo(playlist, tracks) {
    const infoEl = deps.getElements().playlistDetailInfo;
    if (!infoEl) return;
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
            src="${deps.escapeHtml(coverUrl || deps.artworkPlaceholderUrl())}"
            alt="${deps.escapeHtml(album.name || '')}"
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
        deps.getElements().albumDetail.querySelectorAll('.album-detail-facts, .album-detail-about').forEach(node => node.remove());
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
        loadAlbumDiscover(albumId);

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
        deps.getElements().albumDetail.querySelectorAll('.album-detail-facts, .album-detail-about').forEach(node => node.remove());
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

async function loadAlbumDiscover(albumId) {
    if (!deps.getElements().albumDiscover) return;
    deps.getElements().albumDiscover.classList.remove('hidden');
    deps.getElements().albumDiscover.innerHTML = albumDiscoverShellHtml('loading');
    try {
        const resp = await fetch(`/api/albums/${encodeURIComponent(albumId)}/discover`);
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to load suggestions');
        renderAlbumDiscover(data.items || []);
    } catch (e) {
        console.warn('Failed to load album discover suggestions', e);
        deps.getElements().albumDiscover.classList.add('hidden');
        deps.getElements().albumDiscover.innerHTML = '';
    }
}

function renderAlbumDiscover(items) {
    if (!deps.getElements().albumDiscover) return;
    if (!Array.isArray(items) || items.length === 0) {
        deps.getElements().albumDiscover.classList.add('hidden');
        deps.getElements().albumDiscover.innerHTML = '';
        return;
    }
    const rows = items.slice(0, 6).map((item) => {
        const artist = deps.escapeHtml(item.artist || 'Unknown artist');
        return `
            <li class="album-discover-item">
                <span class="album-discover-title">${artist}</span>
            </li>
        `;
    }).join('');
    deps.getElements().albumDiscover.classList.remove('hidden');
    deps.getElements().albumDiscover.innerHTML = `
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
                <small>${deps.escapeHtml(note)}</small>
            </summary>
        </details>
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
    const total = (detail.tracks || []).length;
    const trackCount = query
        ? `${tracks.length} of ${total} track${total === 1 ? '' : 's'}`
        : `${total} track${total === 1 ? '' : 's'}`;
    const album = detail.album;
    deps.getElements().albumDetailCount.textContent = [trackCount, album.release_type, album.year, album.country]
        .filter(Boolean).join(' · ');
    if (tracks.length === 0) {
        deps.getElements().albumDetailTracks.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
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
    // Whole-row and round play-button clicks both start the track (matching
    // the Tidal detail rows); the selection Plus and favorite button stop
    // propagation themselves.
    deps.getElements().albumDetailTracks.querySelectorAll('.track-item').forEach(row => {
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
function detailTrackRowHtml({ index, title, sub, album, favoriteButton, selectionButton, duration, thumb }) {
    const playButton = '<button type="button" class="track-play" aria-label="Play" data-tooltip="Play">▶</button>';
    const lead = index != null
        ? '<span class="track-numplay"><span class="track-index">' + index + '</span>' + playButton + '</span>'
        : playButton;
    return (
        lead +
        (thumb || '') +
        '<div class="track-info">' +
            '<div class="track-title">' + title + '</div>' +
            (sub ? '<div class="track-sub">' + sub + '</div>' : '') +
        '</div>' +
        (album ? '<div class="track-album">' + album + '</div>' : '') +
        (selectionButton || '') +
        favoriteButton +
        (duration ? '<span class="track-duration">' + duration + '</span>' : '')
    );
}

function updateAlbumFavoriteButton(album) {
    if (!deps.getElements().albumFavoriteToggle) return;
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
        // re-render the grid so the filter stays authoritative.
        if (deps.getState().library.showFavoriteAlbums && !deps.getState().library.albumDetail) {
            renderAlbums();
        }
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
    // Re-render the grid (instead of just unhiding it) so newly saved
    // playlists appear as tiles as soon as the user leaves the album.
    renderAlbums();
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
    const tracks = resolvePlaylistTracks(playlist);
    deps.getState().library.playlistDetail = { playlist, tracks };
    deps.getState().library.albumDetail = null;
    updateLibraryViewModeToggle();

    if (deps.getElements().playlistDetailCover) {
        deps.getElements().playlistDetailCover.innerHTML = playlistCoverHtml(playlist);
    }
    if (deps.getElements().playlistDetailName) deps.getElements().playlistDetailName.textContent = playlist.name;
    setPlaylistDetailBackdrop(playlist);
    renderPlaylistDetailInfo(playlist, tracks);
    renderPlaylistDetailTracks();

    deps.getElements().albumsGrid.classList.add('hidden');
    deps.getElements().albumDetail.classList.add('hidden');
    if (deps.getElements().playlistDetail) deps.getElements().playlistDetail.classList.remove('hidden');
    updatePlaylistSaveRowVisibility();
    scrollLibraryDetailToTop();
}

function renderPlaylistDetailTracks() {
    const detail = deps.getState().library.playlistDetail;
    if (!detail || !deps.getElements().playlistDetailTracks) return;
    const query = (deps.getState().library.searchQuery || '').trim().toLowerCase();
    const tracks = query
        ? (detail.tracks || []).filter(track => trackMatchesLibraryQuery(track, query))
        : (detail.tracks || []);
    const total = (detail.tracks || []).length;
    if (deps.getElements().playlistDetailCount) {
        deps.getElements().playlistDetailCount.textContent = query
            ? `${tracks.length} of ${total} track${total === 1 ? '' : 's'}`
            : `${total} track${total === 1 ? '' : 's'}`;
    }
    if (tracks.length === 0) {
        deps.getElements().playlistDetailTracks.innerHTML = '<div class="track-item track-item-empty">No matching tracks.</div>';
        return;
    }
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    deps.getElements().playlistDetailTracks.innerHTML = tracks.map((track, index) => {
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
    deps.getElements().playlistDetailTracks.querySelectorAll('.track-item').forEach(row => {
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
    deps.getElements().playlistDetailTracks.querySelectorAll('.track-add[data-track-add]').forEach(btn => {
        btn.addEventListener('click', (event) => {
            event.stopPropagation();
            toggleTrackSelected(btn.dataset.trackAdd);
        });
    });
    bindTrackFavoriteRowButtons(deps.getElements().playlistDetailTracks);
}

function closePlaylistDetail() {
    deps.getState().library.playlistDetail = null;
    renderAlbums();
}

async function playTrackInPlaylist(trackId) {
    const detail = deps.getState().library.playlistDetail;
    if (!detail) return;
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

// Conscious end of the playlist-build selection: clears the cross-album
// selectedTrackIds (+/check marks), the name field, and the save row.
// Used by the Cancel button; Save calls clearTrackSelection on success.
function cancelPlaylistSelection() {
    clearTrackSelection();
    if (deps.getElements().playlistName) deps.getElements().playlistName.value = '';
    updatePlaylistSaveRowVisibility();
}

function selectAllVisibleTracks() {
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    getFilteredTracks().forEach(track => selectedIds.add(track.id));
    deps.getState().library.selectedTrackIds = Array.from(selectedIds);
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}

function clearVisibleTrackSelection() {
    const visibleIds = new Set(getFilteredTracks().map(track => track.id));
    deps.getState().library.selectedTrackIds = deps.getState().library.selectedTrackIds.filter(id => !visibleIds.has(id));
    updateLibrarySelectionUI();
    syncRenderedTrackSelection();
}

function toggleVisibleTrackSelection() {
    const filteredTracks = getFilteredTracks();
    const selectedIds = new Set(deps.getState().library.selectedTrackIds);
    const visibleIds = filteredTracks.map(track => track.id);
    const allVisibleSelected = visibleIds.length > 0 && visibleIds.every(id => selectedIds.has(id));
    if (allVisibleSelected) {
        clearVisibleTrackSelection();
    } else {
        selectAllVisibleTracks();
    }
}

function setLibrarySearchQuery(value) {
    deps.getState().library.searchQuery = value || '';
    updateLibrarySearchControls();
    if (deps.getState().library.viewMode === 'albums' && deps.getState().library.albumDetail) {
        renderAlbumDetailTracks();
    } else if (deps.getState().library.viewMode === 'albums' && deps.getState().library.playlistDetail) {
        renderPlaylistDetailTracks();
    } else if (deps.getState().library.viewMode === 'albums') {
        renderAlbums();
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
    const isAlbums = deps.getState().library.viewMode === "albums";
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
    // Home above the lists: the same logical position (before the content)
    // and the same full width as the docked album-detail row, for Tracks,
    // Folders, Albums grid and Favorites alike.
    const home = els.tracksList || els.libraryInfo;
    if (!home) return;
    if (row.parentElement !== home.parentElement || row.nextSibling !== home) {
        home.parentElement.insertBefore(row, home);
    }
}

function updatePlaylistSaveRowVisibility() {
    if (!deps.getElements().playlistSaveRow) return;
    const count = deps.getState().library.selectedTrackIds.length;
    // The playlist-build selection is independent of the view mode: as soon
    // as at least one track is consciously added via the + action (never via
    // playback), the save-playlist row is reachable. Playback never touches
    // selectedTrackIds, so this trigger stays exclusive to the + selection.
    // The playlist detail follows the same selection state as every other
    // view: with no track selected the row stays hidden.
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
    const selectedVisibleCount = visibleIds.filter(id => selectedIds.has(id)).length;
    const totalSelectedCount = selectedIds.size;
    const hasSearch = !!(deps.getState().library.searchQuery || '').trim();
    const isTracksMode = deps.getState().library.viewMode === 'tracks';

    // Select all: visible in tracks and folders mode, hidden in albums
    if (deps.getElements().selectAllTracksBtn) {
        const isAlbumsMode = deps.getState().library.viewMode === 'albums';
        const allVisibleSelected = filteredTracks.length > 0 && selectedVisibleCount === filteredTracks.length;
        deps.getElements().selectAllTracksBtn.classList.toggle('hidden', isAlbumsMode);
        deps.getElements().selectAllTracksBtn.disabled = isAlbumsMode || filteredTracks.length === 0;
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
        setTimeout(fetchLibraryStatus, LIBRARY_SCAN_POLL_INTERVAL_MS);
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
    const trackIds = getSelectedPlayableTrackIds();
    const name = (deps.getElements().playlistName?.value || '').trim();
    if (!name) {
        deps.showToast('Please enter a playlist name', 'error');
        return;
    }
    if (trackIds.length < 1) {
        deps.showToast('Select at least 1 track', 'error');
        return;
    }        deps.getElements().savePlaylistBtn.disabled = true;
    try {
        const resp = await fetch('/api/playlists', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, track_ids: trackIds }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || 'Failed to save playlist');
        // Success: finalize the action. Clear the name field, the selection
        // (selectedTrackIds + rendered +/check marks), and the save row.
        if (deps.getElements().playlistName) deps.getElements().playlistName.value = '';
        clearTrackSelection();
        await fetchPlaylists();
        deps.showToast(`Saved: ${data.playlist?.name || name}`, 'success');
    } catch (e) {
        deps.showToast(e.message || 'Failed to save playlist', 'error');
    } finally {
        deps.getElements().savePlaylistBtn.disabled = false;
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
    const validTrackIds = getTrackIdsInLibraryOrder(playlist.track_ids);
    if (validTrackIds.length === 0) {
        deps.showToast(`Playlist "${playlist.name}" has no playable tracks`, 'error');
        return;
    }
    const missingCount = playlist.track_ids.length - validTrackIds.length;
    if (autoplay) {
        if (missingCount > 0) {
            deps.showToast(`Starting ${validTrackIds.length}/${playlist.track_ids.length} tracks from ${playlist.name}`, 'info');
        }
        await deps.playLocal(validTrackIds[0], validTrackIds);
        return;
    }
    deps.showToast(missingCount > 0
        ? `Loaded ${validTrackIds.length}/${playlist.track_ids.length} tracks from ${playlist.name}`
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
        deps.showToast(`Downloading ${filename}`, 'success');
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

// Library actions
function setupLibraryActions() {
    deps.getElements().refreshLibraryBtn.addEventListener('click', refreshLibrary);
    if (deps.getElements().libraryViewTracksBtn) {
        deps.getElements().libraryViewTracksBtn.addEventListener('click', () => setLibraryViewMode('tracks'));
    }
    if (deps.getElements().libraryViewFoldersBtn) {
        deps.getElements().libraryViewFoldersBtn.addEventListener('click', () => setLibraryViewMode('folders'));
    }
    if (deps.getElements().libraryViewFavoritesBtn) {
        deps.getElements().libraryViewFavoritesBtn.addEventListener('click', () => setLibraryViewMode('favorites'));
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
        renderAlbumDiscover,
        renderAlbums,
        renderLibraryFolderPath,
        renderLibraryModeButtons,
        renderLibraryView,
        renderLibraryViewButtons,
        renderPlaylistDetailInfo,
        renderPlaylistDetailTracks,
        renderTrackFavoriteButton,
        renderTracks,
        resetUploadAreaSelection,
        resolvePlaylistTracks,
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
