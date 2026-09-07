#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the streaming cover detail card meta builder in static/app.js.
// Verifies source/title/artist/album/tech only use fields actually delivered
// by the provider status payload — never invented technical values — and that
// the artwork resolution mirrors the footer (streamingArtworkItem) with the
// actual provider as the artwork source (Spotify vs Qobuz).

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

const sandbox = {
    window: { __spotifyLastData: {} },
    state: { samplerate: { available: true, active_rate: 44100 } },
};
vm.createContext(sandbox);
// Dependencies for the artwork resolution chain.
vm.runInContext(extractFunction('trackCoverUrl'), sandbox);
vm.runInContext(extractFunction('trackCoverKnownAvailable'), sandbox);
vm.runInContext(extractFunction('playbackArtworkUrl'), sandbox);
vm.runInContext(extractFunction('playbackArtworkKnownAvailable'), sandbox);
vm.runInContext(extractFunction('streamingArtworkItem'), sandbox);
// Shared footer meta renderer: the cover detail tech line must use the same
// formatter as the footer (formatStreamingMetaLine -> formatRadioStreamLine).
vm.runInContext(extractFunction('formatRadioStreamLine'), sandbox);
vm.runInContext(extractFunction('formatRateKhz'), sandbox);
vm.runInContext(extractFunction('formatStreamingMetaLine'), sandbox);
vm.runInContext(extractFunction('coverDetailStreamingMeta'), sandbox);
vm.runInContext(extractFunction('mergeSpotifyState'), sandbox);

const meta = sandbox.coverDetailStreamingMeta;
const artwork = sandbox.streamingArtworkItem;
const artUrl = sandbox.playbackArtworkUrl;
const artKnown = sandbox.playbackArtworkKnownAvailable;
const merge = sandbox.mergeSpotifyState;

function canon(value) {
    if (Array.isArray(value)) return value.map(canon);
    if (value && typeof value === 'object') {
        const out = {};
        for (const key of Object.keys(value).sort()) out[key] = canon(value[key]);
        return out;
    }
    return value;
}

let passed = 0;
function check(name, actual, expected) {
    assert.deepEqual(canon(actual), canon(expected), name);
    passed += 1;
}

// ---- coverDetailStreamingMeta ----

check('null payload -> all empty', meta(null), { source: '', title: '', artist: '', album: '', tech: '' });
check('undefined payload -> all empty', meta(undefined), { source: '', title: '', artist: '', album: '', tech: '' });
check('empty payload -> Spotify source, resolved rate tech', meta({}), { source: 'Spotify', title: '', artist: '', album: '', tech: '44.1kHz' });

check('full payload', meta({
    title: 'Groove Is in the Heart',
    artist: 'Deee-Lite',
    album: 'World Clique',
    status: 'Playing',
    artUrl: 'https://i.scdn.co/image/x',
}), { source: 'Spotify', title: 'Groove Is in the Heart', artist: 'Deee-Lite', album: 'World Clique', tech: '44.1kHz' });

check('missing album -> album empty, rest kept', meta({
    title: 'Track Without Album',
    artist: 'Some Artist',
}), { source: 'Spotify', title: 'Track Without Album', artist: 'Some Artist', album: '', tech: '44.1kHz' });

check('missing title/artist -> empty text, Spotify kept', meta({
    album: 'Only Album',
}), { source: 'Spotify', title: '', artist: '', album: 'Only Album', tech: '44.1kHz' });

check('spotify never invents format facts', meta({
    title: 'T',
    artist: 'A',
    album: 'B',
    bitrate: 320,
    samplerate: 44100,
    codec: 'AAC',
    position: 50.155,
    duration: 231.786,
    volume: 31,
}), { source: 'Spotify', title: 'T', artist: 'A', album: 'B', tech: '44.1kHz' });

check('spotify without resolved rate shows no tech', (() => {
    sandbox.state.samplerate = { available: false, active_rate: null };
    const result = meta({ title: 'T', artist: 'A' });
    sandbox.state.samplerate = { available: true, active_rate: 44100 };
    return result;
})(), { source: 'Spotify', title: 'T', artist: 'A', album: '', tech: '' });

// Qobuz/TIDAL: real stream facts render through the shared footer formatter
// (same tech line as library/radio and the footer pill).
check('qobuz full quality facts in tech line', meta({
    title: 'T',
    artist: 'A',
    album: 'B',
    audio_format: 'flac',
    bit_depth: 24,
    sample_rate: 44100,
}, 'qobuz'), { source: 'Qobuz', title: 'T', artist: 'A', album: 'B', tech: 'FLAC · 24bit · 44.1kHz' });

check('tidal-shaped full quality facts', meta({
    title: 'T',
    artist: 'A',
    album: 'B',
    audio_format: 'flac',
    bit_depth: 16,
    sample_rate: 48000,
}, 'tidal'), { source: 'Tidal', title: 'T', artist: 'A', album: 'B', tech: 'FLAC · 16bit · 48kHz' });

// ---- artwork resolution (mirrors footer via streamingArtworkItem) ----

const withArt = artwork({ artUrl: 'https://i.scdn.co/image/abc', artwork_url: 'https://i.scdn.co/image/abc' });
check('streamingArtworkItem with artUrl (spotify default)', withArt, {
    source: 'spotify',
    artwork_available: true,
    artwork_url: 'https://i.scdn.co/image/abc',
    artwork_source: 'spotify',
});
check('artwork URL resolves for spotify item', artUrl(withArt), 'https://i.scdn.co/image/abc');
check('artwork known available for spotify item', artKnown(withArt), true);

const withoutArt = artwork({});
check('streamingArtworkItem without artUrl', withoutArt, {
    source: 'spotify',
    artwork_available: false,
    artwork_url: '',
    artwork_source: 'none',
});
check('artwork URL empty without artUrl', artUrl(withoutArt), '');
check('artwork not known available without artUrl', artKnown(withoutArt), false);

// Qobuz artwork must never be classified as Spotify: the actual provider is
// the artwork source.
const qobuzWithArt = artwork({ artUrl: 'https://static.qobuz.com/cover.jpg' }, 'qobuz');
check('streamingArtworkItem classifies Qobuz artwork as qobuz', qobuzWithArt, {
    source: 'qobuz',
    artwork_available: true,
    artwork_url: 'https://static.qobuz.com/cover.jpg',
    artwork_source: 'qobuz',
});
check('Qobuz artwork URL resolves', artUrl(qobuzWithArt), 'https://static.qobuz.com/cover.jpg');

const qobuzWithoutArt = artwork({}, 'qobuz');
check('streamingArtworkItem without Qobuz artUrl keeps qobuz source', qobuzWithoutArt, {
    source: 'qobuz',
    artwork_available: false,
    artwork_url: '',
    artwork_source: 'none',
});

check('coverDetailStreamingMeta labels Qobuz source', meta({ title: 'T', artist: 'A', album: 'B' }, 'qobuz'), {
    source: 'Qobuz',
    title: 'T',
    artist: 'A',
    album: 'B',
    tech: '44.1kHz',
});

// artUrl alias artUrl wins over artwork_url (same as footer: artwork_url || artUrl)
const both = artwork({ artwork_url: 'https://i.scdn.co/image/main', artUrl: 'https://i.scdn.co/image/main' });
check('artwork_url field also resolves', artUrl(both), 'https://i.scdn.co/image/main');

// ---- partial status updates must not clear the common footer ----

sandbox.window.__spotifyLastData = {
    available: true,
    installed: true,
    status: 'Playing',
    trackId: 'track-1',
    title: 'Stable title',
    artist: 'Stable artist',
    album: 'Stable album',
    artUrl: 'https://i.scdn.co/image/stable',
    duration: 180,
};
const partial = merge({ available: true, installed: true, status: 'Playing', trackId: 'track-1', position: 12 });
check('partial same-track status keeps footer metadata', partial, {
    ...sandbox.window.__spotifyLastData,
    position: 12,
});

const next = merge({ available: true, installed: true, status: 'Playing', trackId: 'track-2', title: 'New title', artist: 'New artist', album: '', artUrl: '' });
check('new track starts a fresh metadata record', next, {
    available: true,
    installed: true,
    status: 'Playing',
    trackId: 'track-2',
    title: 'New title',
    artist: 'New artist',
    album: '',
    artUrl: '',
});

console.log(`PASS  scripts/test_cover_detail_spotify.js (${passed} checks)`);
