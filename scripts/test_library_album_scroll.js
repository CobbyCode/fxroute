#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Functional checks for the library detail scroll reset.
//
// The library tab shares the window scroll (no inner scroll container), so
// opening a detail used to inherit the grid's scroll position and start
// mid-page at the tracks. This test executes the real
// scrollLibraryDetailToTop/openAlbumDetail/openSmartTopTracks/openPlaylistDetail
// functions extracted from static/app.js against stubbed dependencies and
// asserts that:
//
//   1. opening an album resets the window scroll to the top and shows the
//      detail (cover/title hero first),
//   2. the Top 40 pseudo-album (same detail view) resets the scroll too,
//   3. opening a playlist detail resets the scroll too,
//   4. a failed track load keeps the grid and does not touch the scroll,
//   5. the track-list-only re-render (search while in detail) never
//      touches the scroll, so typing does not yank the page.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const streamingJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'streaming.js'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    // Keep a leading `async ` so extracted async functions stay async.
    const start = source.slice(Math.max(0, match.index - 6), match.index) === 'async '
        ? match.index - 6
        : match.index;
    // Balanced-brace scan that stays blind inside strings, template literals
    // and comments (an apostrophe in a comment must not start a string).
    let depth = 0, i = source.indexOf('{', match.index);
    let quote = '';
    let escaped = false;
    let lineComment = false;
    let blockComment = false;
    for (; i < source.length; i += 1) {
        const c = source[i];
        const next = source[i + 1];
        if (lineComment) {
            if (c === '\n') lineComment = false;
            continue;
        }
        if (blockComment) {
            if (c === '*' && next === '/') { blockComment = false; i += 1; }
            continue;
        }
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if (c === '/' && next === '/') { lineComment = true; i += 1; continue; }
        if (c === '/' && next === '*') { blockComment = true; i += 1; continue; }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(start, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const scrollSrc = extractFunction(appJs, 'scrollLibraryDetailToTop');
const openAlbumSrc = extractFunction(appJs, 'openAlbumDetail');
const openTopTracksSrc = extractFunction(appJs, 'openSmartTopTracks');
const openPlaylistSrc = extractFunction(appJs, 'openPlaylistDetail');
const renderTracksSrc = extractFunction(appJs, 'renderAlbumDetailTracks');
const openTidalDetailSrc = extractFunction(streamingJs, 'openTidalDetail');

assert.ok(scrollSrc.includes('window.scrollTo(0, 0)'),
    'scrollLibraryDetailToTop must reset the window scroll to the top');
assert.ok(!renderTracksSrc.includes('scrollTo') && !renderTracksSrc.includes('scrollTop'),
    'renderAlbumDetailTracks (search while in detail) must never touch the scroll');

function makeClassList(hidden) {
    const set = new Set(hidden ? ['hidden'] : []);
    return {
        add(...c) { c.forEach((x) => set.add(x)); },
        remove(...c) { c.forEach((x) => set.delete(x)); },
        contains(c) { return set.has(c); },
    };
}

function makeElements() {
    const removable = { remove() {} };
    return {
        albumsGrid: { classList: makeClassList(false) },
        playlistDetail: { classList: makeClassList(true) },
        albumDetail: {
            classList: makeClassList(true),
            querySelectorAll() { return [removable]; },
            querySelector() { return null; },
        },
        albumDetailCover: {},
        albumDetailName: { textContent: '' },
        albumDetailArtist: { textContent: '' },
        albumDetailCount: { textContent: '', insertAdjacentHTML() {} },
        albumDiscover: { classList: makeClassList(true), innerHTML: '' },
        playlistDetailCover: { innerHTML: '' },
        playlistDetailName: { textContent: '' },
    };
}

function makeSandbox({ tracksOk = true } = {}) {
    const scrollCalls = [];
    const elements = makeElements();
    const sandbox = {
        console: { warn() {} },
        window: { scrollTo(x, y) { scrollCalls.push([x, y]); } },
        state: {
            playlists: [{ id: 'p1', name: 'Playlist', track_ids: [] }],
            library: {
                albums: [{ id: 'a1', name: 'Album', artist: 'Artist' }],
                tracks: [],
                albumDetail: null,
                playlistDetail: null,
                albumsCacheToken: 'tok',
            },
        },
        elements,
        fetch: async () => (tracksOk
            ? { ok: true, json: async () => [{ id: 't1', title: 'Track' }] }
            : { ok: false, json: async () => ({}) }),
        updateLibraryViewModeToggle() {},
        updatePlaylistSaveRowVisibility() {},
        albumCoverUrl: () => 'cover.jpg',
        setAlbumCoverImage() {},
        setAlbumDetailBackdrop() {},
        updateAlbumFavoriteButton() {},
        albumFactsHtml: () => '',
        albumAboutHtml: () => '',
        renderAlbumDetailTracks() {},
        loadAlbumDiscover() {},
        showToast() {},
        resolvePlaylistTracks: () => [],
        playlistCoverHtml: () => '',
        setPlaylistDetailBackdrop() {},
        renderPlaylistDetailInfo() {},
        renderPlaylistDetailTracks() {},
    };
    return { sandbox, elements, scrollCalls };
}

async function runOpen(fnSrc, fnName, sandbox, args) {
    const driver = `(async () => { await ${fnName}(${args.map((a) => JSON.stringify(a)).join(', ')}); })()`;
    const code = `${scrollSrc}\n${fnSrc}\n${driver}`;
    await vm.runInNewContext(code, sandbox);
}

(async () => {
    // 1. Album open resets the scroll and shows the detail.
    {
        const { sandbox, elements, scrollCalls } = makeSandbox();
        await runOpen(openAlbumSrc, 'openAlbumDetail', sandbox, ['a1']);
        assert.deepEqual(scrollCalls, [[0, 0]],
            'openAlbumDetail must reset the window scroll to exactly the top');
        assert.ok(!elements.albumDetail.classList.contains('hidden'),
            'openAlbumDetail must show the album detail');
        assert.ok(elements.albumsGrid.classList.contains('hidden'),
            'openAlbumDetail must hide the albums grid');
    }

    // 2. Top 40 opens the same detail view, so it resets the scroll too.
    {
        const { sandbox, elements, scrollCalls } = makeSandbox();
        await runOpen(openTopTracksSrc, 'openSmartTopTracks', sandbox, []);
        assert.deepEqual(scrollCalls, [[0, 0]],
            'openSmartTopTracks must reset the window scroll to exactly the top');
        assert.ok(!elements.albumDetail.classList.contains('hidden'),
            'openSmartTopTracks must show the album detail');
    }

    // 3. Playlist detail opens a detail view too, so it resets the scroll.
    {
        const { sandbox, elements, scrollCalls } = makeSandbox();
        await runOpen(openPlaylistSrc, 'openPlaylistDetail', sandbox, ['p1']);
        assert.deepEqual(scrollCalls, [[0, 0]],
            'openPlaylistDetail must reset the window scroll to exactly the top');
        assert.ok(!elements.playlistDetail.classList.contains('hidden'),
            'openPlaylistDetail must show the playlist detail');
        assert.ok(elements.albumsGrid.classList.contains('hidden'),
            'openPlaylistDetail must hide the albums grid');
    }

    // 4. Failed track load: no view switch, so no scroll reset.
    {
        const { sandbox, elements, scrollCalls } = makeSandbox({ tracksOk: false });
        await runOpen(openAlbumSrc, 'openAlbumDetail', sandbox, ['a1']);
        assert.deepEqual(scrollCalls, [],
            'openAlbumDetail must not touch the scroll when the track load fails');
        assert.ok(elements.albumDetail.classList.contains('hidden'),
            'openAlbumDetail must keep the detail hidden when the track load fails');
    }

    // 5. Unknown album id: nothing happens, scroll untouched.
    {
        const { sandbox, scrollCalls } = makeSandbox();
        await runOpen(openAlbumSrc, 'openAlbumDetail', sandbox, ['missing']);
        assert.deepEqual(scrollCalls, [],
            'openAlbumDetail must not touch the scroll for an unknown album');
    }

    // 6. Unknown playlist id: nothing happens, scroll untouched.
    {
        const { sandbox, scrollCalls } = makeSandbox();
        await runOpen(openPlaylistSrc, 'openPlaylistDetail', sandbox, ['missing']);
        assert.deepEqual(scrollCalls, [],
            'openPlaylistDetail must not touch the scroll for an unknown playlist');
    }

    // 7. TIDAL details funnel through openTidalDetail (album/artist/playlist):
    // each open resets the scroll.
    for (const view of ['album', 'artist', 'playlist']) {
        const scrollCalls = [];
        const sandbox = {
            console: { warn() {} },
            window: { scrollTo(x, y) { scrollCalls.push([x, y]); } },
            state: {
                tidal: {
                    detailRequestId: 0,
                    viewStack: [],
                    view: null,
                    detailId: null,
                    detailTitle: '',
                    detailArt: '',
                },
            },
            entryFor: () => null,
            renderTidalBrowse() { throw new Error('must not render without an entry'); },
        };
        const driver = `(async () => { await openTidalDetail(${JSON.stringify(view)}, 'id1', 'Title', ''); })()`;
        await vm.runInNewContext(`${openTidalDetailSrc}\n${driver}`, sandbox);
        assert.deepEqual(scrollCalls, [[0, 0]],
            `openTidalDetail(${view}) must reset the window scroll to exactly the top`);
        assert.equal(sandbox.state.tidal.view, view,
            `openTidalDetail(${view}) must still switch the view`);
    }

    console.log('PASS scripts/test_library_album_scroll.js');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
