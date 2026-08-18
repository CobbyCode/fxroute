#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the shared streaming footer meta-tag.
//
// Streaming sources (Spotify/Qobuz/TIDAL) must render their footer quality
// line through the exact same renderer as library/radio
// (formatStreamingMetaLine -> formatRadioStreamLine) into the same
// samplerate-status pill. Only facts the provider actually delivered may be
// shown: Qobuz/TIDAL contribute audio_format/bit_depth/sample_rate, Spotify
// contributes none (its tag is the resolved rate alone — no invented codec
// or bit depth).

const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const appJs = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`function\\s+${name}\\s*\\(`).exec(appJs);
    assert.ok(match, `missing ${name}`);
    const brace = appJs.indexOf('{', match.index);
    assert.notEqual(brace, -1, `missing body ${name}`);
    let depth = 0, quote = '', escaped = false;
    for (let index = brace; index < appJs.length; index += 1) {
        const char = appJs[index];
        if (quote) {
            if (escaped) escaped = false;
            else if (char === '\\') escaped = true;
            else if (char === quote) quote = '';
            continue;
        }
        if (`'"\``.includes(char)) quote = char;
        else if (char === '{') depth += 1;
        else if (char === '}' && --depth === 0) return appJs.slice(match.index, index + 1);
    }
    throw new Error(`unterminated ${name}`);
}

const sandbox = {
    state: { samplerate: { available: true, active_rate: 44100 } },
    window: {},
};
vm.createContext(sandbox);
vm.runInContext(extractFunction('formatRadioStreamLine'), sandbox);
vm.runInContext(extractFunction('formatRateKhz'), sandbox);
vm.runInContext(extractFunction('formatStreamingMetaLine'), sandbox);
vm.runInContext(extractFunction('renderStreamingFooterMeta'), sandbox);

const format = sandbox.formatStreamingMetaLine;
const renderStable = sandbox.renderStreamingFooterMeta;

let passed = 0;
function check(label, input, expected) {
    assert.equal(format(input), expected, `formatStreamingMetaLine: ${label}`);
    passed += 1;
}
function checkStable(label, input, expected) {
    assert.equal(renderStable(input), expected, `renderStreamingFooterMeta: ${label}`);
    passed += 1;
}

// Qobuz/TIDAL: real provider stream facts, same renderer as library/radio.
check('qobuz flac 24/44.1', { audio_format: 'flac', bit_depth: 24, sample_rate: 44100 }, 'FLAC · 24 bit · 44.1 kHz');
check('qobuz aac no depth', { audio_format: 'aac', sample_rate: 44100 }, 'AAC · 44.1 kHz');
check('tidal-shaped flac 16/48', { audio_format: 'flac', bit_depth: 16, sample_rate: 48000 }, 'FLAC · 16 bit · 48 kHz');
check('qobuz missing audio_format keeps known parts', { bit_depth: 24, sample_rate: 44100 }, '24 bit · 44.1 kHz');

// Spotify: no format facts may be invented; only the resolved rate.
check('spotify with resolved rate', { title: 't', status: 'Playing' }, '44.1 kHz');
check('spotify rate only, no codec/bit depth', {}, '44.1 kHz');

// No resolved rate -> no tag at all.
sandbox.state.samplerate = { available: false, active_rate: null };
check('spotify without resolved rate shows nothing', { title: 't' }, '');
sandbox.state.samplerate = { available: true, active_rate: 44100 };

// Provider facts win over the resolved rate (they are the real quality data).
check('qobuz facts win over hardware rate', { audio_format: 'flac', bit_depth: 24, sample_rate: 44100 }, 'FLAC · 24 bit · 44.1 kHz');

// Empty / absent payloads.
check('null payload', null, '44.1 kHz');
check('undefined payload', undefined, '44.1 kHz');

// The streaming footer must reuse the library/radio renderer, not a parallel
// formatting path.
assert.ok(appJs.includes("renderStreamingFooterMeta(data)"),
    'updateFooterForStreamingOwner must render through the shared renderer');
assert.ok(appJs.includes("formatStreamingMetaLine(info)"),
    'the stable footer meta renderer must call the shared quality renderer');

// A transient provider gap must not shrink the footer tag mid-track: the
// last full quality line is kept until the track identity changes.
sandbox.window.__streamingMetaStable = null;
const qobuzTrack = { source: 'qobuz', trackId: '107361972', title: 'Sidonie', artist: 'Scratchophone Orchestra', audio_format: 'flac', bit_depth: 16, sample_rate: 44100 };
checkStable('full line rendered first', qobuzTrack, 'FLAC · 16 bit · 44.1 kHz');
const gap = { source: 'qobuz', trackId: '107361972', title: 'Sidonie', artist: 'Scratchophone Orchestra' };
checkStable('same-track provider gap keeps the full line', gap, 'FLAC · 16 bit · 44.1 kHz');
const nextTrack = { source: 'qobuz', trackId: '107361973', title: 'Miss Annie', artist: 'Jive Me', audio_format: 'flac', bit_depth: 24, sample_rate: 44100 };
checkStable('new track renders its own facts', nextTrack, 'FLAC · 24 bit · 44.1 kHz');
const gapNext = { source: 'qobuz', trackId: '107361973', title: 'Miss Annie', artist: 'Jive Me' };
checkStable('same-track gap on the new track keeps its line', gapNext, 'FLAC · 24 bit · 44.1 kHz');

// A different track without facts must not inherit the previous line.
sandbox.window.__streamingMetaStable = { trackKey: 'qobuz|old|Old|Artist', line: 'FLAC · 24 bit · 44.1 kHz' };
checkStable('different track without facts falls back to the rate',
    { source: 'qobuz', trackId: 'new', title: 'New', artist: 'A' }, '44.1 kHz');

// TIDAL (native playback) shares the radio/local footer branch.
const renderSamplerate = extractFunction('renderSamplerateUI');
assert.ok(/activeSource === 'radio' \|\| activeSource === 'local' \|\| activeSource === 'tidal'/.test(renderSamplerate),
    'renderSamplerateUI must treat tidal like radio/local stream facts');

// One shared footer pill element for all sources.
const footerMatch = html.match(/<footer id="playback-bar" class="playback-bar">[\s\S]*?<\/footer>/);
assert.ok(footerMatch, 'missing #playback-bar markup');
const footer = footerMatch[0];
assert.equal((footer.match(/id="samplerate-status"/g) || []).length, 1,
    'footer must carry exactly one shared samplerate-status pill');
assert.ok(footer.includes('playback-meta-row'), 'footer pill must live in the shared playback-meta-row');

console.log(`PASS  scripts/test_streaming_footer_meta.js (${passed} format cases)`);
