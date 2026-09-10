#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Functional + structural checks for the local library playlist save row.
//
// The save row is a single shared node. Its home is below the detail cards
// (right before #library-info), but while an album detail is open it docks
// inside the detail between header and tracks — like the TIDAL save row —
// instead of sitting misplaced under the track list. This test executes the
// real dockPlaylistSaveRow/updatePlaylistSaveRowVisibility functions
// extracted from static/app.js against a minimal parent/child DOM model and
// asserts that:
//
//   1. with an open album detail the row docks between header and tracks,
//   2. with a closed detail the row returns home before #library-info,
//   3. docking is idempotent and preserves node identity (typing in the
//      name field never moves the focused input),
//   4. visibility still follows the selection/edit state.
//
// It also asserts the desktop CSS contract: the local row uses the full
// content width with the name field absorbing the extra space while
// Save/Delete/Cancel keep compact natural widths (like the TIDAL row).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const appJs = fs.readFileSync(path.join(root, 'static', 'app.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'style.css'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const start = source.slice(Math.max(0, match.index - 6), match.index) === 'async '
        ? match.index - 6
        : match.index;
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

const dockSrc = extractFunction(appJs, 'dockPlaylistSaveRow');
const visibilitySrc = extractFunction(appJs, 'updatePlaylistSaveRowVisibility');

// --- minimal parent/child DOM model ----------------------------------------

function makeClassList(hidden) {
    const set = new Set(hidden ? ['hidden'] : []);
    return {
        add(...c) { c.forEach((x) => set.add(x)); },
        remove(...c) { c.forEach((x) => set.delete(x)); },
        toggle(c, force) {
            const on = force === undefined ? !set.has(c) : !!force;
            if (on) set.add(c); else set.delete(c);
            return on;
        },
        contains(c) { return set.has(c); },
    };
}

function makeNode(name, { hidden = false } = {}) {
    const kids = [];
    const el = {
        name,
        children: kids,
        parentElement: null,
        classList: makeClassList(hidden),
        textContent: '',
        innerHTML: '',
        get nextSibling() {
            if (!el.parentElement) return null;
            const sibs = el.parentElement.children;
            const idx = sibs.indexOf(el);
            return idx >= 0 && idx < sibs.length - 1 ? sibs[idx + 1] : null;
        },
        insertBefore(child, ref) {
            if (child.parentElement) {
                const from = child.parentElement.children;
                from.splice(from.indexOf(child), 1);
                child.parentElement = null;
            }
            child.parentElement = el;
            const at = ref ? kids.indexOf(ref) : -1;
            if (at < 0) kids.push(child);
            else kids.splice(at, 0, child);
            return child;
        },
        appendChild(child) { return el.insertBefore(child, null); },
    };
    return el;
}

function orderNames(parent) {
    return parent.children.map((c) => c.name);
}

function makeTree() {
    // Mirrors static/index.html: tracks, grid, album detail
    // (header/tracks/discover), playlist detail, save row, library info.
    const tab = makeNode('tab-library');
    const tracksList = makeNode('tracks-list');
    const albumsGrid = makeNode('albums-grid', { hidden: true });
    const albumDetail = makeNode('album-detail', { hidden: true });
    const albumHeader = makeNode('album-detail-header');
    const albumTracks = makeNode('album-detail-tracks');
    const albumDiscover = makeNode('album-discover');
    albumDetail.appendChild(albumHeader);
    albumDetail.appendChild(albumTracks);
    albumDetail.appendChild(albumDiscover);
    const playlistDetail = makeNode('playlist-detail', { hidden: true });
    const saveRow = makeNode('playlist-save-row', { hidden: true });
    const saveControls = makeNode('playlist-save-controls');
    const deleteBtn = makeNode('delete-playlist', { hidden: true });
    const libraryInfo = makeNode('library-info');
    tab.appendChild(tracksList);
    tab.appendChild(albumsGrid);
    tab.appendChild(albumDetail);
    tab.appendChild(playlistDetail);
    tab.appendChild(saveRow);
    tab.appendChild(libraryInfo);
    const elements = {
        albumDetail,
        albumDetailTracks: albumTracks,
        libraryInfo,
        playlistSaveRow: saveRow,
        playlistSaveControls: saveControls,
        deletePlaylistBtn: deleteBtn,
    };
    return { tab, elements, albumDetail, albumTracks, saveRow, libraryInfo };
}

function makeSandbox(tree, { selected = 0, editingPlaylist = false } = {}) {
    return {
        console: { warn() {} },
        window: { scrollTo() {} },
        elements: tree.elements,
        state: {
            library: {
                selectedTrackIds: Array.from({ length: selected }, (_, i) => `t${i}`),
                playlistDetail: editingPlaylist ? { playlist: { id: 'p1' } } : null,
            },
        },
    };
}

async function runVisibility(sandbox) {
    await vm.runInNewContext(`${dockSrc}\n${visibilitySrc}\nupdatePlaylistSaveRowVisibility();`, sandbox);
}

(async () => {
    // 1. Open album detail + selection: row docks between header and tracks.
    {
        const tree = makeTree();
        tree.albumDetail.classList.remove('hidden');
        await runVisibility(makeSandbox(tree, { selected: 2 }));
        assert.deepEqual(orderNames(tree.albumDetail),
            ['album-detail-header', 'playlist-save-row', 'album-detail-tracks', 'album-discover'],
            'the save row must dock between header and tracks while the album detail is open');
        assert.ok(!tree.saveRow.classList.contains('hidden'),
            'the save row must be visible with a track selection');
    }

    // 2. Closed detail: row returns home before #library-info.
    {
        const tree = makeTree();
        tree.albumDetail.classList.remove('hidden');
        await runVisibility(makeSandbox(tree, { selected: 2 }));
        assert.equal(tree.saveRow.parentElement, tree.albumDetail, 'precondition: row docked in detail');
        tree.albumDetail.classList.add('hidden');
        await runVisibility(makeSandbox(tree, { selected: 2 }));
        assert.equal(tree.saveRow.parentElement.name, 'tab-library',
            'the save row must return to the library tab when the detail closes');
        assert.equal(tree.saveRow.nextSibling, tree.libraryInfo,
            'the save row must sit right before #library-info at home');
    }

    // 3. Idempotent: repeated updates keep order and node identity.
    {
        const tree = makeTree();
        tree.albumDetail.classList.remove('hidden');
        const sandbox = makeSandbox(tree, { selected: 1 });
        await runVisibility(sandbox);
        const first = orderNames(tree.albumDetail).join(',');
        await runVisibility(sandbox);
        await runVisibility(sandbox);
        assert.equal(orderNames(tree.albumDetail).join(','), first,
            'repeated visibility updates must keep a stable order');
        assert.ok(tree.albumDetail.children.includes(tree.saveRow),
            'docking must preserve the save row node (and its focused input)');
    }

    // 4. Visibility still follows selection/edit state.
    {
        const tree = makeTree();
        await runVisibility(makeSandbox(tree, { selected: 0 }));
        assert.ok(tree.saveRow.classList.contains('hidden'),
            'the save row must stay hidden without a selection');
        await runVisibility(makeSandbox(tree, { selected: 0, editingPlaylist: true }));
        assert.ok(!tree.saveRow.classList.contains('hidden'),
            'the save row must stay open while a playlist is edited');
        assert.ok(!tree.elements.deletePlaylistBtn.classList.contains('hidden'),
            'Delete must be reachable while a playlist is edited');
    }

    // 5. Desktop CSS contract: full-width row, growing name field, compact
    // buttons — the local row follows the TIDAL album row language.
    const rowRule = css.match(/\.playlist-save-row\s*\{[^}]*\}/);
    assert.ok(rowRule && /width:\s*100%/.test(rowRule[0]),
        'the local save row must use the full content width');
    const controlsRule = css.match(/\.playlist-save-controls\s*\{[^}]*\}/);
    assert.ok(controlsRule && /flex:\s*1 1 auto/.test(controlsRule[0]) && /width:\s*100%/.test(controlsRule[0]),
        'the local save controls must fill the row width');
    const inputRule = css.match(/\.playlist-save-row \.url-input\s*\{[^}]*\}/);
    assert.ok(inputRule && /flex:\s*1 1 0/.test(inputRule[0]),
        'the local name field must absorb the extra row width');
    const btnRule = css.match(/\.playlist-save-row \.btn-secondary\s*\{[^}]*\}/);
    assert.ok(btnRule && /flex:\s*0 0 auto/.test(btnRule[0]) && /white-space:\s*nowrap/.test(btnRule[0]),
        'Save/Delete/Cancel must keep compact natural button widths');

    console.log('PASS scripts/test_library_save_row.js');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
