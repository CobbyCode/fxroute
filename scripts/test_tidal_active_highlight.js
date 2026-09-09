#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// The running TIDAL track must highlight in TIDAL album/track lists exactly
// like a library track does in the library list.
//
// Regression: TIDAL rows render with the `streaming-result` class, but the
// highlight matcher only looked at `.track-item`, so a playing TIDAL track
// never received the active state.
//
// This executes the VERBATIM highlight function extracted from
// static/app.js (no reimplementation) against stub DOM rows.

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(source, name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const paren = source.indexOf('(', match.index);
    let pdepth = 0, quote = '', escaped = false, i = paren;
    for (; i < source.length; i += 1) {
        const c = source[i];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '(') pdepth += 1;
        else if (c === ')' && --pdepth === 0) break;
    }
    const brace = source.indexOf('{', i);
    let depth = 0;
    quote = ''; escaped = false;
    for (let j = brace; j < source.length; j += 1) {
        const c = source[j];
        if (quote) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === quote) quote = '';
            continue;
        }
        if ('\'"`'.includes(c)) quote = c;
        else if (c === '{') depth += 1;
        else if (c === '}' && --depth === 0) return source.slice(match.index, j + 1);
    }
    throw new Error(`unterminated ${name}`);
}

function makeRow(classes, trackId) {
    const active = new Set(classes.includes('active') ? ['active'] : []);
    return {
        dataset: { trackId },
        classList: {
            add: (name) => active.add(name),
            remove: (name) => active.delete(name),
            contains: (name) => active.has(name),
        },
        _classes: classes,
    };
}

function runHighlight({ footerSource = 'local', currentTrack = null, rows = [] }) {
    const bySelector = new Map();
    const sandbox = {
        window: { __footerSource: footerSource },
        document: {
            querySelectorAll: (selector) => {
                if (!bySelector.has(selector)) {
                    const parts = selector.split(',').map((s) => s.trim());
                    bySelector.set(selector, rows.filter((row) =>
                        parts.some((part) => {
                            const cls = part.startsWith('.') ? part.slice(1).split(/[.[]/)[0] : null;
                            if (part.includes('[data-track-id]')) {
                                return cls && row._classes.includes(cls) && row.dataset.trackId !== undefined;
                            }
                            return cls && row._classes.includes(cls);
                        }),
                    ));
                }
                return bySelector.get(selector);
            },
        },
        pendingOptimisticTrack: null,
        state: { playback: { current_track: currentTrack } },
    };
    vm.createContext(sandbox);
    vm.runInContext(`${extractFunction(src, 'highlightActiveTrack')}\nhighlightActiveTrack();`, sandbox);
    return rows;
}

// --- structural guard: TIDAL rows must participate in the highlight -------
assert.ok(
    /querySelectorAll\('\.track-item, \.streaming-result\[data-track-id\]'\)/.test(src),
    'highlight must match TIDAL catalog rows in addition to library rows',
);

// --- 1. playing TIDAL track highlights its TIDAL row --------------------------
{
    const tidalRow = makeRow(['streaming-result'], '777');
    const otherRow = makeRow(['streaming-result'], '778');
    const libraryRow = makeRow(['track-item'], 'local_x');
    runHighlight({
        currentTrack: { id: '777', source: 'tidal' },
        rows: [tidalRow, otherRow, libraryRow],
    });
    assert.ok(tidalRow.classList.contains('active'), 'playing TIDAL track row must be active');
    assert.ok(!otherRow.classList.contains('active'), 'other TIDAL rows must stay inactive');
    assert.ok(!libraryRow.classList.contains('active'), 'unrelated library rows must stay inactive');
}

// --- 2. stale TIDAL highlight is cleared when the track changes --------------
{
    const tidalRow = makeRow(['streaming-result', 'active'], '777');
    runHighlight({
        currentTrack: { id: '778', source: 'tidal' },
        rows: [tidalRow, makeRow(['streaming-result'], '778')],
    });
    assert.ok(!tidalRow.classList.contains('active'), 'previous TIDAL row must lose active');
}

// --- 3. library highlighting still works and clears TIDAL rows ---------------
{
    const tidalRow = makeRow(['streaming-result', 'active'], '777');
    const libraryRow = makeRow(['track-item'], 'local_x');
    runHighlight({
        currentTrack: { id: 'local_x', source: 'local' },
        rows: [tidalRow, libraryRow],
    });
    assert.ok(libraryRow.classList.contains('active'), 'playing library row must be active');
    assert.ok(!tidalRow.classList.contains('active'), 'TIDAL row must lose active for local playback');
}

// --- 4. spotify footer clears every highlight ---------------------------------
{
    const tidalRow = makeRow(['streaming-result', 'active'], '777');
    const libraryRow = makeRow(['track-item', 'active'], 'local_x');
    runHighlight({
        footerSource: 'spotify',
        currentTrack: { id: 'tidal_now', source: 'spotify' },
        rows: [tidalRow, libraryRow],
    });
    assert.ok(!tidalRow.classList.contains('active'), 'spotify footer must clear TIDAL highlights');
    assert.ok(!libraryRow.classList.contains('active'), 'spotify footer must clear library highlights');
}

console.log('PASS  scripts/test_tidal_active_highlight.js (4 highlight cases)');
