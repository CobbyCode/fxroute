#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the cover detail queue jump target builder in static/app.js.
// Verifies that jumping to a queue index uses the canonical /api/play payload
// (track_id + full queue in order, shuffle/loop preserved) and that invalid or
// already-active indexes return null (no restart of the current track).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `missing ${name}`);
    const brace = source.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < source.length; index += 1) {
        const char = source[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'\"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return source.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(extractFunction('coverQueuePlayTarget'), sandbox);

const target = sandbox.coverQueuePlayTarget;

// Values built inside the vm sandbox live in a different realm with its own
// prototypes, so deepStrictEqual fails on otherwise identical data. Compare
// canonical JSON instead (sorted keys, arrays in order).
function canon(value) {
    if (Array.isArray(value)) return value.map(canon);
    if (value && typeof value === 'object') {
        const out = {};
        for (const key of Object.keys(value).sort()) out[key] = canon(value[key]);
        return out;
    }
    return value;
}

function assertSame(actual, expected, label) {
    const a = JSON.stringify(canon(actual));
    const e = JSON.stringify(canon(expected));
    assert.equal(a, e, `${label}\nactual:   ${a}\nexpected: ${e}`);
}

// ---- fixtures ------------------------------------------------------------

const queueTracks = [
    { id: 'track_a', title: 'Track A', artist: 'Artist A' },
    { id: 'track_b', title: 'Track B', artist: 'Artist B' },
    { id: 'track_c', title: 'Track C', artist: 'Artist C' },
    { id: 'track_d', title: 'Track D', artist: 'Artist D' },
];

const playlistQueue = {
    current_track: { source: 'local', id: 'track_a' },
    queue: {
        active: true,
        index: 0,
        count: 4,
        tracks: queueTracks,
        loop: true,
        shuffle: false,
    },
};

const shuffledQueue = {
    current_track: { source: 'local', id: 'track_b' },
    queue: {
        active: true,
        index: 1,
        count: 4,
        tracks: queueTracks,
        loop: false,
        shuffle: true,
    },
};

// ---- cases ---------------------------------------------------------------

const cases = [
    ['no playback at all', undefined, 1, null],
    ['empty playback', {}, 1, null],
    ['no queue tracks', { current_track: { source: 'local' }, queue: {} }, 0, null],
    ['negative index', playlistQueue, -1, null],
    ['index beyond queue', playlistQueue, 99, null],
    ['non-integer index', playlistQueue, 1.5, null],
    ['current track index is a no-op', playlistQueue, 0, null],
    ['jump forward to index 2', playlistQueue, 2, {
        source: 'local',
        track_id: 'track_c',
        queue_track_ids: ['track_a', 'track_b', 'track_c', 'track_d'],
        shuffle: false,
        loop: true,
    }],
    ['jump backward to index 0 from index 1', shuffledQueue, 0, {
        source: 'local',
        track_id: 'track_a',
        queue_track_ids: ['track_a', 'track_b', 'track_c', 'track_d'],
        shuffle: true,
        loop: false,
    }],
    ['track without id is not playable', {
        current_track: { source: 'local' },
        queue: { index: 0, tracks: [{ title: 'No ID' }, { id: 'track_b' }] },
    }, 0, null],
];

for (const [label, playback, index, expected] of cases) {
    const actual = target(playback, index);
    assertSame(actual, expected, `coverQueuePlayTarget: ${label}`);
}

console.log(`ok — ${cases.length} coverQueuePlayTarget cases`);
