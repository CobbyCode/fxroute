#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// Tests for the Spotify cover detail card meta builder in static/app.js.
// Verifies source/title/artist/album/tech only use fields actually delivered
// by the Spotify status payload — never invented technical values — and that
// the artwork resolution mirrors the footer (spotifyArtworkItem).

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

const sandbox = { window: { __spotifyLastData: {} } };
vm.createContext(sandbox);
// Dependencies for the artwork resolution chain.
vm.runInContext(extractFunction('trackCoverUrl'), sandbox);
vm.runInContext(extractFunction('trackCoverKnownAvailable'), sandbox);
vm.runInContext(extractFunction('playbackArtworkUrl'), sandbox);
vm.runInContext(extractFunction('playbackArtworkKnownAvailable'), sandbox);
vm.runInContext(extractFunction('spotifyArtworkItem'), sandbox);
vm.runInContext(extractFunction('coverDetailSpotifyMeta'), sandbox);
vm.runInContext(extractFunction('mergeSpotifyState'), sandbox);

const meta = sandbox.coverDetailSpotifyMeta;
const artwork = sandbox.spotifyArtworkItem;
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

// ---- coverDetailSpotifyMeta ----

check('null payload -> all empty', meta(null), { source: '', title: '', artist: '', album: '', tech: '' });
check('undefined payload -> all empty', meta(undefined), { source: '', title: '', artist: '', album: '', tech: '' });
check('empty payload -> SPOTIFY source only', meta({}), { source: 'SPOTIFY', title: '', artist: '', album: '', tech: '' });

check('full payload', meta({
    title: 'Groove Is in the Heart',
    artist: 'Deee-Lite',
    album: 'World Clique',
    status: 'Playing',
    artUrl: 'https://i.scdn.co/image/x',
}), { source: 'SPOTIFY', title: 'Groove Is in the Heart', artist: 'Deee-Lite', album: 'World Clique', tech: '' });

check('missing album -> album empty, rest kept', meta({
    title: 'Track Without Album',
    artist: 'Some Artist',
}), { source: 'SPOTIFY', title: 'Track Without Album', artist: 'Some Artist', album: '', tech: '' });

check('missing title/artist -> empty text, SPOTIFY kept', meta({
    album: 'Only Album',
}), { source: 'SPOTIFY', title: '', artist: '', album: 'Only Album', tech: '' });

check('technical fields never leak into tech', meta({
    title: 'T',
    artist: 'A',
    album: 'B',
    bitrate: 320,
    samplerate: 44100,
    codec: 'AAC',
    position: 50.155,
    duration: 231.786,
    volume: 31,
}), { source: 'SPOTIFY', title: 'T', artist: 'A', album: 'B', tech: '' });

// ---- artwork resolution (mirrors footer via spotifyArtworkItem) ----

const withArt = artwork({ artUrl: 'https://i.scdn.co/image/abc', artwork_url: 'https://i.scdn.co/image/abc' });
check('spotifyArtworkItem with artUrl', withArt, {
    source: 'spotify',
    artwork_available: true,
    artwork_url: 'https://i.scdn.co/image/abc',
    artwork_source: 'spotify',
});
check('artwork URL resolves for spotify item', artUrl(withArt), 'https://i.scdn.co/image/abc');
check('artwork known available for spotify item', artKnown(withArt), true);

const withoutArt = artwork({});
check('spotifyArtworkItem without artUrl', withoutArt, {
    source: 'spotify',
    artwork_available: false,
    artwork_url: '',
    artwork_source: 'none',
});
check('artwork URL empty without artUrl', artUrl(withoutArt), '');
check('artwork not known available without artUrl', artKnown(withoutArt), false);

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
