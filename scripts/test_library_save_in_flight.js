#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Library playlist save row and TIDAL search matching:
//   - a re-render of the save row while a save request is pending must not
//     re-enable the Save button (a second click would create a duplicate),
//   - TIDAL rows match the Library search through one shared helper.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const Library = require('../static/library_ui.js');

function makeButton() {
    return { disabled: false, textContent: '' };
}

function makeEls() {
    return {
        playlistSaveRow: { dataset: {}, classList: { toggle() {} } },
        playlistName: { value: 'Mix', placeholder: '' },
        savePlaylistBtn: makeButton(),
    };
}

function deferred() {
    let settle;
    const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
    return { promise, ...settle };
}

async function testLocalSaveKeepsButtonDisabled() {
    const els = makeEls();
    const state = { library: { tracks: [{ id: 'local_a.mp3' }], selectedTrackIds: ['local_a.mp3'], viewMode: 'flat' }, playlists: [] };
    const toasts = [];
    Library.init({ getState: () => state, getElements: () => els, showToast: (m, k) => toasts.push([m, k]) });
    globalThis.FXRouteStreaming = undefined;

    Library.updatePlaylistSaveRowVisibility();
    assert.equal(els.playlistSaveRow.dataset.saveMode, 'local');
    assert.equal(els.savePlaylistBtn.disabled, false, 'idle Save is enabled');

    const requests = [];
    const gate = deferred();
    const realFetch = globalThis.fetch;
    globalThis.fetch = (url) => {
        requests.push(String(url));
        return gate.promise;
    };
    try {
        const saving = Library.savePlaylist();
        assert.equal(els.savePlaylistBtn.disabled, true, 'Save is disabled while the request is pending');

        // Any selection/library re-render lands here while the request is out.
        Library.updatePlaylistSaveRowVisibility();
        assert.equal(els.savePlaylistBtn.disabled, true, 're-render must not re-enable a pending Save');

        await Library.savePlaylist();
        assert.equal(requests.filter((url) => url === '/api/playlists').length, 1, 'a second Save must not send a second request');

        gate.resolve({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) });
        await saving;
        assert.equal(els.savePlaylistBtn.disabled, false, 'Save is enabled again once the request settled');
        assert.deepEqual(toasts.at(-1), ['boom', 'error']);

        // Nothing sticks: the next save goes out again.
        const second = deferred();
        globalThis.fetch = (url) => { requests.push(String(url)); return second.promise; };
        const again = Library.savePlaylist();
        assert.equal(requests.filter((url) => url === '/api/playlists').length, 2, 'the next Save sends its request');
        second.resolve({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) });
        await again;
    } finally {
        globalThis.fetch = realFetch;
    }
}

async function testTidalSaveKeepsButtonDisabled() {
    const els = makeEls();
    const state = { library: { tracks: [], selectedTrackIds: [], viewMode: 'flat' }, playlists: [] };
    const toasts = [];
    const gate = deferred();
    let created = 0;
    Library.init({ getState: () => state, getElements: () => els, showToast: (m, k) => toasts.push([m, k]) });
    globalThis.FXRouteStreaming = {
        tidal: {
            connection: () => ({ authenticated: true }),
            selectedIds: () => ['t1'],
            items: () => [],
            createPlaylist: () => { created += 1; return gate.promise; },
            friendlyError: (m) => String(m),
        },
    };
    try {
        Library.updatePlaylistSaveRowVisibility();
        assert.equal(els.playlistSaveRow.dataset.saveMode, 'tidal');
        const saving = Library.savePlaylist();
        assert.equal(els.savePlaylistBtn.disabled, true);
        Library.updatePlaylistSaveRowVisibility();
        assert.equal(els.savePlaylistBtn.disabled, true, 'TIDAL save: re-render keeps Save disabled');
        await Library.savePlaylist();
        assert.equal(created, 1, 'TIDAL save: no second playlist while the first is pending');
        gate.resolve({ name: 'Mix' });
        await saving;
        assert.equal(els.savePlaylistBtn.disabled, false);
        assert.deepEqual(toasts.at(-1), ['Saved to TIDAL: Mix', 'success']);
    } finally {
        globalThis.FXRouteStreaming = undefined;
    }
}

async function testTidalPartialSaveShowsWarning() {
    // Exercise the same save consumer in the canonical and shipped demo asset.
    for (const library of [Library, require('../demo/dist/static/library_ui.js')]) {
        const els = makeEls();
        const state = { library: { tracks: [], selectedTrackIds: [], viewMode: 'flat' }, playlists: [] };
        const toasts = [];
        const warning = 'TIDAL playlist "Mix" (pl-new) was created, but adding tracks could not be confirmed. Check this playlist before adding tracks again; do not create it again.';
        library.init({ getState: () => state, getElements: () => els, showToast: (m, k) => toasts.push([m, k]) });
        let created = 0;
        globalThis.FXRouteStreaming = {
            tidal: {
                connection: () => ({ authenticated: true }),
                selectedIds: () => ['t1'],
                items: () => [],
                createPlaylist: async () => { created += 1; return { id: 'pl-new', name: 'Mix', warning, seed_status: 'unknown', added_track_ids: null }; },
                friendlyError: (m) => String(m),
            },
        };
        try {
            library.updatePlaylistSaveRowVisibility();
            await library.savePlaylist();
            assert.deepEqual(toasts.at(-1), [warning, 'error'], 'partial creation must not show full success');
            assert.equal(els.playlistName.value, '', 'confirmed creation consumes the name even when seeding is uncertain');
            await library.savePlaylist();
            assert.equal(created, 1, 'the next Save must not recreate the confirmed playlist');
        } finally {
            globalThis.FXRouteStreaming = undefined;
        }
    }
}

function testTidalSearchUsesOneHelper() {
    const items = {
        tracks: [
            { id: '1', title: 'Blue Monday', artist: 'New Order', album: 'Power' },
            { id: '2', title: 'Other', artist: 'Someone', album: 'Else' },
        ],
        albums: [{ id: '3', title: 'Power', artist: 'New Order', year: 1983 }],
        playlists: [{ id: '4', name: 'Dance Mix' }],
    };
    let query = '';
    const state = () => ({ library: { searchQuery: query } });
    Library.init({ getState: state, getElements: () => ({}) });
    globalThis.FXRouteStreaming = {
        tidal: {
            connection: () => ({ authenticated: true }),
            items: (kind) => items[kind] || [],
            isFavorite: () => true,
        },
    };
    try {
        const ids = (kind, search) => {
            query = search;
            return Library.tidalLibraryFavorites(kind).map((item) => item.id);
        };
        assert.deepEqual(ids('tracks', ''), ['1', '2']);
        assert.deepEqual(ids('tracks', 'new order'), ['1'], 'artist matches');
        assert.deepEqual(ids('tracks', 'else'), ['2'], 'album matches');
        assert.deepEqual(ids('albums', '1983'), ['3'], 'album year matches');
        assert.deepEqual(ids('playlists', 'dance'), ['4'], 'playlist name matches');
        assert.deepEqual(ids('tracks', 'nothing'), []);
    } finally {
        globalThis.FXRouteStreaming = undefined;
    }

    // The match expression lives in one helper; the call sites reuse it.
    const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'library_ui.js'), 'utf8');
    const expression = /tidalItemSearchText\([^)]*\)\.filter\(Boolean\)\.join\(' '\)\.toLowerCase\(\)\.includes\(/g;
    assert.equal((source.match(expression) || []).length, 1, 'the TIDAL match expression must exist exactly once');
    assert.equal((source.match(/tidalEntryMatchesQuery\(/g) || []).length, 4, 'definition plus three call sites');
}

(async () => {
    await testLocalSaveKeepsButtonDisabled();
    await testTidalSaveKeepsButtonDisabled();
    await testTidalPartialSaveShowsWarning();
    testTidalSearchUsesOneHelper();
    console.log('library save in flight: ok');
})().catch((error) => {
    console.error(error);
    process.exit(1);
});
