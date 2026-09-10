#!/usr/bin/env node
// Main-catalog fixture determinism: favorites in demo/data/library.js are
// baked literal data (no runtime hash), and every flag must equal the
// deterministic formula the runtime used to compute: tracks and provider
// tracks favored when hashCode(key) % 4 === 0, albums when % 3 === 0.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');

// Mirror of the favorites formula the main catalog used before favorites
// were baked: Java-style 32-bit string hash, low 31 bits.
function favoritesHash(text) {
    let h = 0;
    for (let i = 0; i < String(text).length; i += 1) {
        h = ((h << 5) - h + String(text).charCodeAt(i)) | 0;
    }
    return h & 0x7fffffff;
}
const isTrackFavorite = (key) => favoritesHash(key) % 4 === 0;
const isAlbumFavorite = (key) => favoritesHash(key) % 3 === 0;

(async () => {
    // The fixture bakes its favorites; it must not ship a runtime hash.
    assert.ok(!librarySource.includes('hashCode('),
        'data/library.js must not compute favorites at load; they are baked');

    const ctx = { window: {} };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(librarySource, ctx);
    const lib = ctx.window.FXROUTE_DEMO_LIBRARY;

    const items = [
        ...lib.tracks.map(t => ['track', 'fav-track:' + t.id, t.favorite]),
        ...lib.albums.map(a => ['album', 'fav-album:' + a.id, a.favorite]),
        ...lib.spotifyTracks.map(t => ['track', 'fav-spotify:' + t.title, t.favorite]),
        ...lib.qobuzTracks.map(t => ['track', 'fav-qobuz:' + t.title, t.favorite]),
    ];
    let favored = 0;
    for (const [kind, key, actual] of items) {
        const expected = kind === 'album' ? isAlbumFavorite(key) : isTrackFavorite(key);
        assert.equal(actual, expected, 'favorite for ' + key + ' must equal the hash formula');
        if (actual) favored += 1;
    }
    assert.ok(favored > 0, 'fixture must keep deterministic favorites');
    assert.ok(favored < items.length, 'favorites must not be all-or-nothing');

    console.log('ok library fixture favorites');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});