#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Regression checks for the Library-only grid/list toggle.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');

const js = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    let depth = 0;
    let quote = '';
    let escaped = false;
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
        else if (c === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const renderTracks = extractFunction(js, 'renderTracks');
assert.ok(renderTracks.includes('updateLibraryViewModeToggle()'),
    'Tracks/Folders rendering must synchronize the Library layout toggle');

const updateToggle = extractFunction(js, 'updateLibraryViewModeToggle');
assert.ok(updateToggle.includes("state.library.viewMode === 'albums'"),
    'the Library layout toggle must be restricted to Albums');
assert.ok(updateToggle.includes('!state.library.albumDetail') && updateToggle.includes('!state.library.playlistDetail'),
    'the Library layout toggle must not appear in detail views');

const setLayout = extractFunction(js, 'setAlbumLayout');
assert.ok(setLayout.includes("state.library.viewMode !== 'albums'"),
    'layout clicks outside Albums must have no effect');

console.log('PASS  scripts/test_library_layout_toggle.js');
