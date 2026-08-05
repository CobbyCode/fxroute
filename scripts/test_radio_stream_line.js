#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the radio stream tech line formatter in static/app.js.
// Fixtures are live-measured mpv values from Radio Paradise, FIP, SomaFM, KEXP.

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
vm.runInContext(extractFunction('formatRadioStreamLine'), sandbox);

const format = sandbox.formatRadioStreamLine;

const cases = [
    // live Radio Paradise aac-320
    [{ codec: 'AAC', bitrate_kbps: 320, samplerate_hz: 44100 }, 'AAC · 320 kbps · 44.1 kHz'],
    // live Radio Paradise flac
    [{ codec: 'FLAC', profile: 'Lossless', bitrate_kbps: 746, samplerate_hz: 44100 }, 'FLAC · Lossless · 44.1 kHz'],
    // live FIP midfi mp3 (48 kHz!)
    [{ codec: 'MP3', bitrate_kbps: 128, samplerate_hz: 48000 }, 'MP3 · 128 kbps · 48 kHz'],
    // live SomaFM 256 mp3
    [{ codec: 'MP3', bitrate_kbps: 256, samplerate_hz: 44100 }, 'MP3 · 256 kbps · 44.1 kHz'],
    // live KEXP 160 aac (measured 2026-08-02)
    [{ codec: 'AAC', bitrate_kbps: 161, samplerate_hz: 44100 }, 'AAC · 161 kbps · 44.1 kHz'],
    // bitrate not delivered yet: known parts stay, no empty separators
    [{ codec: 'AAC', samplerate_hz: 44100 }, 'AAC · 44.1 kHz'],
    // profile only, no samplerate
    [{ codec: 'AAC', profile: 'Low' }, 'AAC · Low'],
    // local FLAC 24 bit (decoded format s32)
    [{ codec: 'FLAC', profile: 'Lossless', bit_depth: 24, samplerate_hz: 44100 }, 'FLAC · Lossless · 24 bit · 44.1 kHz'],
    // local FLAC 16 bit
    [{ codec: 'FLAC', profile: 'Lossless', bit_depth: 16, samplerate_hz: 44100 }, 'FLAC · Lossless · 16 bit · 44.1 kHz'],
    // local WAV 24 bit
    [{ codec: 'PCM', bitrate_kbps: 1058, bit_depth: 24, samplerate_hz: 44100 }, 'PCM · 1058 kbps · 24 bit · 44.1 kHz'],
    // local lossy: no bit depth (floatp decode)
    [{ codec: 'MP3', bitrate_kbps: 320, samplerate_hz: 44100 }, 'MP3 · 320 kbps · 44.1 kHz'],
    // unknown parts must be omitted
    [{ codec: 'AAC' }, 'AAC'],
    [null, ''],
    [{}, ''],
    [undefined, ''],
];

for (const [input, expected] of cases) {
    assert.equal(format(input), expected, `formatRadioStreamLine(${JSON.stringify(input)})`);
}

console.log(`ok — ${cases.length} formatRadioStreamLine cases`);
