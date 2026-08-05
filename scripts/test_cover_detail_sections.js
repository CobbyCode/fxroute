#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the cover detail card section builder in static/app.js.
// Verifies that history/queue sections only use data already present in
// the status payload — never invented entries, never empty headings.

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
vm.runInContext(extractFunction('coverDetailSections'), sandbox);

const sections = sandbox.coverDetailSections;

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

const radioNoHistory = {
    current_track: { source: 'radio', title: 'Some Song', artist: 'Some Artist' },
    radio_metadata: { history: [] },
};
const radioMissingHistory = {
    current_track: { source: 'radio', title: 'Some Song', artist: 'Some Artist' },
    radio_metadata: {},
};
// SomaFM delivers history (songs[1:4] with title/artist); RP/FIP/KEXP deliver []
const radioWithHistory = {
    current_track: { source: 'radio', title: 'Current Song', artist: 'Current Artist' },
    radio_metadata: {
        history: [
            { title: 'Past Song A', artist: 'Artist A' },
            { title: 'Past Song B', artist: 'Artist B' },
            { title: 'Past Song C', artist: 'Artist C' },
        ],
    },
};
// history entries without title/artist must be filtered out
const radioDirtyHistory = {
    current_track: { source: 'radio', title: 'Current Song', artist: 'Current Artist' },
    radio_metadata: {
        history: [
            { title: 'Past Song A', artist: 'Artist A' },
            {},
            { title: 'Only Title' },
            { artist: 'Only Artist' },
            null,
        ],
    },
};
const localSingle = {
    current_track: { source: 'local', title: 'Single Track', artist: 'Solo Artist' },
};
const localSingleQueueCount1 = {
    current_track: { source: 'local', title: 'Single Track', artist: 'Solo Artist' },
    queue: { active: true, index: 0, count: 1, tracks: [{ title: 'Single Track', artist: 'Solo Artist' }] },
};
const localPlaylist = {
    current_track: { source: 'local', title: 'Middle Track', artist: 'Middle Artist' },
    queue: {
        active: true,
        index: 1,
        count: 3,
        tracks: [
            { title: 'First Track', artist: 'First Artist' },
            { title: 'Middle Track', artist: 'Middle Artist' },
            { title: 'Last Track', artist: 'Last Artist' },
        ],
    },
};
const localPlaylistNoIndex = {
    current_track: { source: 'local', title: 'Middle Track', artist: 'Middle Artist' },
    queue: {
        active: true,
        count: 3,
        tracks: [
            { title: 'First Track', artist: 'First Artist' },
            { title: 'Middle Track', artist: 'Middle Artist' },
            { title: 'Last Track', artist: 'Last Artist' },
        ],
    },
};
// count > 1 but tracks missing/empty: no queue section
const localQueueNoTracks = {
    current_track: { source: 'local', title: 'Middle Track', artist: 'Middle Artist' },
    queue: { active: true, index: 1, count: 3, tracks: [] },
};
const spotify = {
    current_track: { source: 'spotify', title: 'Spotify Track', artist: 'Spotify Artist' },
};

// ---- cases ---------------------------------------------------------------

const cases = [
    ['no playback at all', undefined, { history: [], queue: null }],
    ['empty playback', {}, { history: [], queue: null }],
    ['radio without history array', radioNoHistory, { history: [], queue: null }],
    ['radio without radio_metadata', radioMissingHistory, { history: [], queue: null }],
    ['radio with SomaFM-style history', radioWithHistory, {
        history: [
            { title: 'Past Song A', artist: 'Artist A' },
            { title: 'Past Song B', artist: 'Artist B' },
            { title: 'Past Song C', artist: 'Artist C' },
        ],
        queue: null,
    }],
    ['radio history filters empty entries', radioDirtyHistory, {
        history: [
            { title: 'Past Song A', artist: 'Artist A' },
            { title: 'Only Title' },
            { artist: 'Only Artist' },
        ],
        queue: null,
    }],
    ['local single track without queue', localSingle, { history: [], queue: null }],
    ['local single track queue count 1', localSingleQueueCount1, { history: [], queue: null }],
    ['local playlist shows queue', localPlaylist, {
        history: [],
        queue: {
            index: 1,
            tracks: [
                { title: 'First Track', artist: 'First Artist' },
                { title: 'Middle Track', artist: 'Middle Artist' },
                { title: 'Last Track', artist: 'Last Artist' },
            ],
        },
    }],
    ['local playlist without index', localPlaylistNoIndex, {
        history: [],
        queue: { index: -1, tracks: localPlaylistNoIndex.queue.tracks },
    }],
    ['local queue without tracks stays hidden', localQueueNoTracks, { history: [], queue: null }],
    ['spotify out of scope', spotify, { history: [], queue: null }],
];

for (const [label, input, expected] of cases) {
    const actual = sections(input);
    assertSame(actual, expected, `coverDetailSections: ${label}`);
}

console.log(`ok — ${cases.length} coverDetailSections cases`);
