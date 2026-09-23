#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Focused frontend tests for the library module: fetch/filter/render, detail
// views, selection, search, upload/download and playlist/library CRUD live in
// static/library_ui.js. Row favorites moved with the library; the footer
// favorite and the generic ui-helper shims (favoriteHeartSvg etc.) stay in
// app.js behind injected callbacks, as do the shared favorite/mode flags.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const repoRoot = path.resolve(__dirname, '..');
const appSource = fs.readFileSync(path.join(repoRoot, 'static', 'app.js'), 'utf8');
const indexSource = fs.readFileSync(path.join(repoRoot, 'static', 'index.html'), 'utf8');
const Library = require('../static/library_ui.js');

const MOVED = [
    'readStoredViewMode', 'clearLibraryImportFeedbackIfIdle', 'closeLibraryImportPanel',
    'getTrackIdsInLibraryOrder', 'getSelectedPlayableTrackIds', 'getSelectedDownloadTrackIds',
    'renderLibraryModeButtons', 'toggleLibraryShuffle', 'toggleLibraryLoop',
    'renderTrackFavoriteButton', 'updateTrackFavoriteCaches', 'findTrackById',
    'syncTrackFavoriteRowButtons', 'bindTrackFavoriteRowButtons', 'toggleTrackFavoriteById',
    'fetchLibraryStatus', 'fetchTracks', 'fetchPlaylists',
    'getTrackRelativePath', 'getTrackFolder', 'getTrackFilename',
    'trackMatchesLibraryQuery', 'playlistMatchesLibraryQuery', 'getFilteredPlaylists',
    'isTrackInCurrentFolder', 'getFilteredTracks', 'getTracksInFolder', 'getFolderChildren',
    'renderLibraryView', 'renderLibraryViewButtons', 'renderLibraryFolderPath',
    'formatLibraryScanStatus', 'renderTracks', 'setLibraryViewMode', 'setLibraryFolder',
    'storeViewMode', 'setAlbumLayout', 'updateLibraryViewModeToggle', 'fetchAlbums', 'renderAlbums',
    'albumMatchesLibraryQuery', 'albumHasCover', 'albumCoverUrl', 'setAlbumCoverImage',
    'setAlbumDetailBackdrop', 'setPlaylistDetailBackdrop', 'renderPlaylistDetailInfo',
    'playlistFeaturingLine', 'trackThumbHtml', 'findAlbumForTrack', 'playlistDistinctAlbums',
    'playlistCoverHtml', 'playlistFallbackMarkSvg', 'scrollLibraryDetailToTop',
    'openAlbumDetail', 'openSmartTopTracks', 'loadAlbumDiscover', 'renderAlbumDiscover',
    'albumDiscoverShellHtml', 'renderAlbumDetailTracks',
    'libraryFavoriteButtonHtml', 'detailFavoriteButtonHtml', 'librarySelectionButtonHtml',
    'detailTrackRowHtml', 'updateAlbumFavoriteButton', 'toggleCurrentAlbumFavorite',
    'toggleAlbumCardFavorite', 'albumFactsHtml', 'detailFactsHtml', 'albumAboutHtml', 'detailAboutHtml',
    'closeAlbumDetail', 'playTrackInAlbum', 'resolvePlaylistTracks', 'openPlaylistDetail',
    'renderPlaylistDetailTracks', 'closePlaylistDetail', 'playTrackInPlaylist',
    'playLibraryFolder', 'deleteLibraryFolder', 'toggleLibraryFolderSelection',
    'toggleTrackSelected', 'clearTrackSelection', 'cancelPlaylistSelection',
    'selectAllVisibleTracks', 'clearVisibleTrackSelection', 'toggleVisibleTrackSelection',
    'setLibrarySearchQuery', 'updateLibrarySearchControls', 'updateLibrarySearchPlaceholder',
    'clearLibrarySearch', 'syncRenderedTrackSelection', 'dockPlaylistSaveRow',
    'updatePlaylistSaveRowVisibility', 'updateLibrarySelectionUI', 'refreshLibrary',
    'uploadTrackFile', 'savePlaylist', 'loadPlaylistById', 'downloadPlaylistById',
    'deletePlaylistById', 'downloadSelectedTracks', 'deleteSelectedTracks',
    'setupDownloadActions', 'setDownloadUrlValue', 'handleDownloadUrlInput',
    'maybeStartDownloadFromInput', 'extractDroppedUrl', 'setupDownloadUrlDropArea',
    'resetUploadAreaSelection', 'setupUploadArea',
    'startDownload', 'cancelDownload', 'fetchDownloadStatus',
    'startDownloadStatusPolling', 'stopDownloadStatusPolling',
    'handleDownloadStatusTransition', 'updateDownloadUI', 'setupLibraryActions',
];

// Generic ui-helper shims stay shared in app.js (same pattern as escapeHtml).
const STAYING_SHIMS = ['favoriteHeartSvg', 'artworkPlaceholderUrl', 'albumArtFallbackSvg'];
const libSourcePending = fs.readFileSync(path.join(repoRoot, 'static', 'library_ui.js'), 'utf8');

// Module loads before the app shell and owns every moved function.
assert.ok(
    indexSource.indexOf('library_ui.js?v=') < indexSource.indexOf('app.js?v='),
    'library_ui.js must load before app.js',
);
assert.match(indexSource, /library_ui\.js\?v=\d+\.\d+\.\d+/);
for (const name of MOVED) {
    assert.equal(typeof Library[name], 'function', `module must export ${name}`);
    assert.doesNotMatch(
        appSource,
        new RegExp(`\\n(?:async function|function) ${name}\\(`),
        `no app.js duplicate: ${name} lives in library_ui.js`,
    );
    assert.doesNotMatch(
        appSource,
        new RegExp(`(?<![\\w.$'\`"])${name}\\s*\\(`),
        `no bare app.js call: ${name} goes through LibraryUI.`,
    );
}
for (const name of STAYING_SHIMS) {
    assert.match(appSource, new RegExp(`^function ${name}\\(`, 'm'), `${name} shim stays in app.js`);
    assert.doesNotMatch(
        libSourcePending,
        new RegExp(`(?:function|const) ${name}\\(`),
        `${name} is injected, not redefined, by the library module`,
    );
}
// Module-only state: moved declarations left app.js, shared flags stayed.
for (const decl of [
    'const VIEW_MODE_STORAGE_KEY', 'const LIBRARY_SCAN_POLL_INTERVAL_MS',
    'const DOWNLOAD_STATUS_POLL_INTERVAL_MS', 'let albumsFetchInFlight',
    'let downloadStatusPollTimer', 'let lastDownloadStatus', 'let trackFavoriteRequestInFlight',
]) {
    assert.ok(
        new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm').test(
            fs.readFileSync(path.join(repoRoot, 'static', 'library_ui.js'), 'utf8'),
        ),
        `library module must own: ${decl}`,
    );
    assert.doesNotMatch(appSource, new RegExp(`^${decl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`, 'm'), `app.js must not keep: ${decl}`);
}
assert.match(appSource, /^let libraryModeRequestInFlight = false;/m, 'mode flag stays shared in app.js');
assert.match(appSource, /^let tidalFavoriteRequestInFlight = false;/m, 'tidal favorite flag stays shared in app.js');
assert.doesNotMatch(appSource, /^let trackFavoriteRequestInFlight = false;/m, 'row favorite flag moved into the library module');

// app.js wiring reaches the module through the LibraryUI alias; the effects
// module's upload-area dependency is repointed at the library module.
for (const snippet of [
    'const LibraryUI = window.FXRouteLibraryUI || {};',
    'window.FXRouteLibraryUI?.init({',
    'LibraryUI.setupLibraryActions()',
    'LibraryUI.setupDownloadActions()',
    'LibraryUI.fetchTracks()',
    'renderLibraryView: () => LibraryUI.renderLibraryView()',
    'fetchLibraryStatus: () => LibraryUI.fetchLibraryStatus()',
    'setupUploadArea: (areaId, fileInputId, onFile) => LibraryUI.setupUploadArea(',
    'isLibraryModeRequestInFlight: () => libraryModeRequestInFlight',
    'setLibraryModeRequestInFlight: (active) => { libraryModeRequestInFlight = active; }',
    'isTidalFavoriteRequestInFlight: () => tidalFavoriteRequestInFlight',
    'window.FXRouteBankUI.appendBankBindingFields(',
]) {
    assert.ok(appSource.includes(snippet), `app.js wiring must reference ${snippet}`);
}
const libSource = libSourcePending;
assert.match(libSource, /deps\.getState\(\)/, 'state goes through injected getter');
assert.match(libSource, /deps\.getElements\(\)/, 'elements go through injected getter');
assert.doesNotMatch(libSource, /(?<![\w.$])state\./, 'no raw app state access');
assert.doesNotMatch(libSource, /(?<![\w.$])elements\./, 'no raw app elements access');
assert.match(libSource, /deps\.isLibraryModeRequestInFlight\(\)/, 'module reads the shared mode flag via deps');
assert.match(libSource, /deps\.isTidalFavoriteRequestInFlight\(\)/, 'module reads the shared tidal flag via deps');
assert.match(libSource, /deps\.favoriteHeartSvg\(/, 'module renders hearts via the staying shim dep');

// Pure helper contracts stay intact.
assert.equal(Library.getTrackRelativePath({ id: 'local_Rock/song.mp3' }), 'Rock/song.mp3');
assert.equal(Library.getTrackRelativePath({ path: '/a/b/c.mp3' }), 'c.mp3');
assert.equal(Library.getTrackFolder({ id: 'local_Rock/song.mp3' }), 'Rock');
assert.equal(Library.getTrackFilename({ id: 'local_Rock/song.mp3' }), 'song.mp3');
assert.equal(Library.getTrackFilename({ title: 'Solo' }), 'Solo');
assert.equal(Library.trackMatchesLibraryQuery({ artist: 'Led Zeppelin' }, 'zeppelin'), true);
assert.equal(Library.trackMatchesLibraryQuery({ artist: 'Led Zeppelin' }, 'beatles'), false);
assert.equal(Library.trackMatchesLibraryQuery({ artist: 'X' }, ''), true);
assert.equal(Library.playlistMatchesLibraryQuery({ name: 'Road Trip' }, 'road'), true);
assert.equal(Library.playlistMatchesLibraryQuery({ name: 'Road Trip' }, 'jazz'), false);
assert.equal(Library.albumHasCover({ cover_source: 'folder' }), true);
assert.equal(Library.albumHasCover({}), false);
assert.equal(Library.albumHasCover(undefined), false);

// State-dependent helpers render through the injected state getter.
const state = { library: { scanStatus: null, viewMode: 'flat', currentFolder: '' } };
Library.init({ getState: () => state, getElements: () => ({}) });
assert.equal(Library.formatLibraryScanStatus(), '');
state.library.scanStatus = { scanning: true, tracks_found: 5, files_seen: 10, current_dir: '/mnt/nas' };
assert.equal(
    Library.formatLibraryScanStatus(),
    'Scanning library… 5 audio tracks found, 10 files checked · /mnt/nas',
);
state.library.scanStatus = { error: 'boom' };
assert.equal(Library.formatLibraryScanStatus(), 'Library scan error: boom');
state.library.scanStatus = null;
assert.equal(Library.isTrackInCurrentFolder({ id: 'local_Rock/song.mp3' }), true);
state.library.viewMode = 'folders';
state.library.currentFolder = 'Rock';
assert.equal(Library.isTrackInCurrentFolder({ id: 'local_Rock/song.mp3' }), true);
assert.equal(Library.isTrackInCurrentFolder({ id: 'local_Jazz/song.mp3' }), false);

console.log('library ui frontend: ok');
