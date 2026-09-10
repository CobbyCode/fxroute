#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Functional checks for the library album-detail scroll reset.
//
// The library tab shares the window scroll (no inner scroll container), so
// opening an album used to inherit the album grid's scroll position and
// start mid-page at the tracks. This test executes the real
// scrollAlbumDetailToTop/openAlbumDetail/openSmartTopTracks functions
// extracted from static/app.js against stubbed dependencies and asserts
// that:
//
//   1. opening an album resets the window scroll to the top and shows the
//      detail (cover/title hero first),
//   2. the Top 40 pseudo-album (same detail view) resets the scroll too,
//   3. a failed track load keeps the grid and does not touch the scroll,
//   4. the track-list-only re-render (search while in detail) never
//      touches the scroll, so typing does not yank the page.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    // Keep a leading `async ` so extracted async functions stay async.
    const start = source.slice(Math.max(0, match.index - 6), match.index) === 'async '
        ? match.index - 6
        : match.index;
    const brace = source.indexOf('{', match.index);
    let depth = 0, quote = '', escaped = false;
    for (let i = brace; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(start, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const scrollSrc = extractFunction(appJs, 'scrollAlbumDetailToTop');
const openAlbumSrc = extractFunction(appJs, 'openAlbumDetail');
const openTopTracksSrc = extractFunction(appJs, 'openSmartTopTracks');
const renderTracksSrc = extractFunction(appJs, 'renderAlbumDetailTracks');

assert.ok(scrollSrc.includes('window.scrollTo(0, 0)'),
    'scrollAlbumDetailToTop must reset the window scroll to the top');
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
    };
}

function makeSandbox({ tracksOk = true } = {}) {
    const scrollCalls = [];
    const elements = makeElements();
    const sandbox = {
        console: { warn() {} },
        window: { scrollTo(x, y) { scrollCalls.push([x, y]); } },
        state: {
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

    // 3. Failed track load: no view switch, so no scroll reset.
    {
        const { sandbox, elements, scrollCalls } = makeSandbox({ tracksOk: false });
        await runOpen(openAlbumSrc, 'openAlbumDetail', sandbox, ['a1']);
        assert.deepEqual(scrollCalls, [],
            'openAlbumDetail must not touch the scroll when the track load fails');
        assert.ok(elements.albumDetail.classList.contains('hidden'),
            'openAlbumDetail must keep the detail hidden when the track load fails');
    }

    // 4. Unknown album id: nothing happens, scroll untouched.
    {
        const { sandbox, scrollCalls } = makeSandbox();
        await runOpen(openAlbumSrc, 'openAlbumDetail', sandbox, ['missing']);
        assert.deepEqual(scrollCalls, [],
            'openAlbumDetail must not touch the scroll for an unknown album');
    }

    console.log('PASS scripts/test_library_album_scroll.js');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
