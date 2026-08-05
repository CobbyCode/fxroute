#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the cover detail card meta builder in static/app.js.
// Verifies source/playlist, title, artist, album and tech line only use data
// already present in the status payload / loaded playlists — never invented.

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
// coverDetailMeta calls formatRadioStreamLine internally.
vm.runInContext(extractFunction('formatRadioStreamLine'), sandbox);
vm.runInContext(extractFunction('coverDetailMeta'), sandbox);

const meta = sandbox.coverDetailMeta;

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

// Radio with fresh provider metadata (SomaFM-style)
const radioFresh = {
    current_track: { source: 'radio', title: 'Groove Salad', artist: 'Radio' },
    live_title: 'Zero Cult - Sweet Apathy',
    radio_metadata: {
        artist: 'Zero Cult',
        title: 'Sweet Apathy',
        album: 'Sci Fi',
        stale: false,
    },
    stream_info: { codec: 'MP3', bitrate_kbps: 256, samplerate_hz: 44100 },
};
// Radio with stale/empty provider metadata → fall back to live_title / track title
const radioStale = {
    current_track: { source: 'radio', title: 'The Trip', artist: 'Radio' },
    live_title: 'Jens Buchert - Dawn Rider',
    radio_metadata: { stale: true, title: null, artist: null, album: null },
    stream_info: { codec: 'AAC', bitrate_kbps: 127, samplerate_hz: 44100 },
};
// Radio without live_title
const radioNoLive = {
    current_track: { source: 'radio', title: 'FIP Jazz', artist: 'Radio' },
    radio_metadata: { stale: true },
    stream_info: { codec: 'MP3', bitrate_kbps: 128, samplerate_hz: 48000 },
};
// Library single track (no queue)
const localSingle = {
    current_track: { source: 'local', title: 'Single Track', artist: 'Solo Artist', album: 'Album One' },
    stream_info: { codec: 'FLAC', profile: 'Lossless', bit_depth: 16, samplerate_hz: 44100 },
};
// Library playlist whose queue exactly matches a known playlist
const playlistDef = { id: 'mix', name: 'Mix', track_ids: ['a', 'b', 'c'] };
const localPlaylist = {
    current_track: { source: 'local', title: 'Track B', artist: 'Artist B', album: 'Album B' },
    queue: { active: true, index: 1, count: 3, tracks: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] },
    stream_info: { codec: 'MP3', bitrate_kbps: 320, samplerate_hz: 44100 },
};
// Library playlist that does NOT match any known playlist (e.g. selection)
const localSelection = {
    current_track: { source: 'local', title: 'Track X', artist: 'Artist X' },
    queue: { active: true, index: 0, count: 3, tracks: [{ id: 'x' }, { id: 'y' }, { id: 'z' }] },
    stream_info: { codec: 'MP3', bitrate_kbps: 256, samplerate_hz: 44100 },
};

// ---- cases ---------------------------------------------------------------

const cases = [
    ['no playback at all', undefined, undefined, { source: '', title: '', artist: '', album: '', tech: '' }],
    ['empty playback', {}, [], { source: '', title: '', artist: '', album: '', tech: '' }],
    ['radio with fresh metadata', radioFresh, [], {
        source: 'Groove Salad',
        title: 'Sweet Apathy',
        artist: 'Zero Cult',
        album: 'Sci Fi',
        tech: 'MP3 · 256 kbps · 44.1 kHz',
    }],
    ['radio stale falls back to live_title', radioStale, [], {
        source: 'The Trip',
        title: 'Jens Buchert - Dawn Rider',
        artist: '',
        album: '',
        tech: 'AAC · 127 kbps · 44.1 kHz',
    }],
    ['radio without live_title uses track title', radioNoLive, [], {
        source: 'FIP Jazz',
        title: 'FIP Jazz',
        artist: '',
        album: '',
        tech: 'MP3 · 128 kbps · 48 kHz',
    }],
    ['library single track', localSingle, [], {
        source: '',
        title: 'Single Track',
        artist: 'Solo Artist',
        album: 'Album One',
        tech: 'FLAC · Lossless · 16 bit · 44.1 kHz',
    }],
    ['library playlist matches known playlist', localPlaylist, [playlistDef], {
        source: 'Mix',
        title: 'Track B',
        artist: 'Artist B',
        album: 'Album B',
        tech: 'MP3 · 320 kbps · 44.1 kHz',
    }],
    ['library playlist without matching playlist', localSelection, [playlistDef], {
        source: '',
        title: 'Track X',
        artist: 'Artist X',
        album: '',
        tech: 'MP3 · 256 kbps · 44.1 kHz',
    }],
    ['no stream info yet', { current_track: { source: 'local', title: 'T' } }, [], {
        source: '',
        title: 'T',
        artist: '',
        album: '',
        tech: '',
    }],
];

for (const [label, input, playlists, expected] of cases) {
    const actual = meta(input, playlists);
    assertSame(actual, expected, `coverDetailMeta: ${label}`);
}

console.log(`ok — ${cases.length} coverDetailMeta cases`);
