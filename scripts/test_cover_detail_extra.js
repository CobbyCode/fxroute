#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the cover detail card tag-info block builder in static/app.js.
// Verifies the block only uses fields already present in the status payload
// (year, genre, track/disc number) and returns empty lines for anything
// missing — no invented values, no composer/label lookups.

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
vm.runInContext(extractFunction('coverDetailExtra'), sandbox);

const extra = sandbox.coverDetailExtra;

// Values built inside the vm sandbox live in a different realm with its own
// prototypes, so deepStrictEqual fails on otherwise identical data. Compare
// canonical JSON instead.
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

const localTrack = {
    source: 'local',
    title: 'Hammerhart (Denyo77 remix)',
    artist: 'Absolute Beginner',
    album: 'Bambule: Boombule – The Remixed Album',
    genre: 'Electronic',
    year: 2000,
    track_number: 1,
    disc_number: 1,
};

const cases = [
    ['no playback at all', undefined, { line1: '', line2: '' }],
    ['empty playback', {}, { line1: '', line2: '' }],
    ['no current track', { current_track: null }, { line1: '', line2: '' }],
    ['radio track has no tag block', { current_track: { source: 'radio', title: 'Groove Salad' } }, { line1: '', line2: '' }],
    ['local full tags', { current_track: localTrack }, { line1: '2000 · Electronic', line2: 'Disc 1 · Track 1' }],
    ['year only', { current_track: { ...localTrack, genre: null } }, { line1: '2000', line2: 'Disc 1 · Track 1' }],
    ['genre only', { current_track: { ...localTrack, year: null } }, { line1: 'Electronic', line2: 'Disc 1 · Track 1' }],
    ['no year/genre', { current_track: { ...localTrack, year: null, genre: null } }, { line1: '', line2: 'Disc 1 · Track 1' }],
    ['track number only', { current_track: { ...localTrack, disc_number: null } }, { line1: '2000 · Electronic', line2: 'Track 1' }],
    ['disc number only', { current_track: { ...localTrack, track_number: null } }, { line1: '2000 · Electronic', line2: 'Disc 1' }],
    ['no numbers at all', { current_track: { ...localTrack, track_number: null, disc_number: null } }, { line1: '2000 · Electronic', line2: '' }],
    ['local track without tags', { current_track: { source: 'local', title: 'Untagged' } }, { line1: '', line2: '' }],
    ['year zero is ignored', { current_track: { ...localTrack, year: 0 } }, { line1: 'Electronic', line2: 'Disc 1 · Track 1' }],
    ['negative numbers ignored', { current_track: { ...localTrack, year: -5, disc_number: -1 } }, { line1: 'Electronic', line2: 'Track 1' }],
    ['genre whitespace trimmed', { current_track: { ...localTrack, genre: '  German Hip-Hop  ' } }, { line1: '2000 · German Hip-Hop', line2: 'Disc 1 · Track 1' }],
    ['empty genre string', { current_track: { ...localTrack, genre: '   ' } }, { line1: '2000', line2: 'Disc 1 · Track 1' }],
    ['single track without queue', { current_track: localTrack, queue: null }, { line1: '2000 · Electronic', line2: 'Disc 1 · Track 1' }],
];

for (const [label, playback, expected] of cases) {
    const actual = extra(playback);
    assertSame(actual, expected, `coverDetailExtra: ${label}`);
}

console.log(`ok — ${cases.length} coverDetailExtra cases`);
