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
};
vm.createContext(sandbox);
vm.runInContext(extractFunction('formatRadioStreamLine'), sandbox);
vm.runInContext(extractFunction('formatRateKhz'), sandbox);
vm.runInContext(extractFunction('formatStreamingMetaLine'), sandbox);

const format = sandbox.formatStreamingMetaLine;

let passed = 0;
function check(label, input, expected) {
    assert.equal(format(input), expected, `formatStreamingMetaLine: ${label}`);
    passed += 1;
}

// Qobuz/TIDAL: real provider stream facts, same renderer as library/radio.
check('qobuz flac 24/44.1', { audio_format: 'flac', bit_depth: 24, sample_rate: 44100 }, 'FLAC · 24bit · 44.1kHz');
check('qobuz aac no depth', { audio_format: 'aac', sample_rate: 44100 }, 'AAC · 44.1kHz');
check('tidal-shaped flac 16/48', { audio_format: 'flac', bit_depth: 16, sample_rate: 48000 }, 'FLAC · 16bit · 48kHz');
check('qobuz missing audio_format keeps known parts', { bit_depth: 24, sample_rate: 44100 }, '24bit · 44.1kHz');

// Spotify: no format facts may be invented; only the resolved rate.
check('spotify with resolved rate', { title: 't', status: 'Playing' }, '44.1kHz');
check('spotify rate only, no codec/bit depth', {}, '44.1kHz');

// No resolved rate -> no tag at all.
sandbox.state.samplerate = { available: false, active_rate: null };
check('spotify without resolved rate shows nothing', { title: 't' }, '');
sandbox.state.samplerate = { available: true, active_rate: 44100 };

// Provider facts win over the resolved rate (they are the real quality data).
check('qobuz facts win over hardware rate', { audio_format: 'flac', bit_depth: 24, sample_rate: 44100 }, 'FLAC · 24bit · 44.1kHz');

// Empty / absent payloads.
check('null payload', null, '44.1kHz');
check('undefined payload', undefined, '44.1kHz');

// The streaming footer must reuse the library/radio renderer, not a parallel
// formatting path, and must not cache last-rendered strings client-side: the
// backend keeps the track's stream facts complete across transient gaps, so
// the payload is authoritative (see playback/stream_info.StreamInfoLedger and
// the Qobuz provider's sticky stream facts).
assert.ok(appJs.includes("formatStreamingMetaLine(data)"),
    'updateFooterForStreamingOwner must render through the shared renderer');
assert.ok(!appJs.includes('renderStreamingFooterMeta'),
    'no UI-side remember-last-string caching of the footer meta tag');
assert.ok(!appJs.includes('__streamingMetaStable'),
    'no client-side stable-string cache for the footer meta tag');

// TIDAL (native playback) shares the radio/local footer branch.
const renderSamplerate = extractFunction('renderSamplerateUI');
assert.ok(/activeSource === 'radio' \|\| activeSource === 'local' \|\| activeSource === 'tidal'/.test(renderSamplerate),
    'renderSamplerateUI must treat tidal like radio/local stream facts');
// A general samplerate/rate refresh must never overwrite the footer pill
// while a streaming source owns it: renderSamplerateUI must early-return for
// streaming footer owners (the Qobuz flicker was this path writing the bare
// hardware rate over the full 'FLAC · 16bit · 44.1kHz' line).
assert.ok(/isStreamingFooterSource\(window\.__footerSource\)\s*\)\s*\{[\s\S]*?return;/.test(renderSamplerate),
    'renderSamplerateUI must not touch the footer pill while a streaming source owns it');

// One shared footer pill element for all sources.
const footerMatch = html.match(/<footer id="playback-bar" class="playback-bar">[\s\S]*?<\/footer>/);
assert.ok(footerMatch, 'missing #playback-bar markup');
const footer = footerMatch[0];
assert.equal((footer.match(/id="samplerate-status"/g) || []).length, 1,
    'footer must carry exactly one shared samplerate-status pill');
assert.ok(footer.includes('playback-meta-row'), 'footer pill must live in the shared playback-meta-row');

console.log(`PASS  scripts/test_streaming_footer_meta.js (${passed} format cases)`);
