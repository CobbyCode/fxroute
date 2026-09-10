#!/usr/bin/env node
// Demo Library 2 contract: the web demo presents "Demo Library 2" as its
// active library (selectable under Settings like on a real box). Each
// library serves its own albums, tracks, covers and playlists; switching
// restores the other catalog untouched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');
const library2Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library2.js'), 'utf8');
const radioSource = fs.readFileSync(path.join(root, 'demo', 'data', 'radio.js'), 'utf8');
const measurementsSource = fs.readFileSync(path.join(root, 'demo', 'data', 'measurements.js'), 'utf8');
const stateSource = fs.readFileSync(path.join(root, 'demo', 'state.js'), 'utf8');
const routesSource = fs.readFileSync(path.join(root, 'demo', 'routes.js'), 'utf8');

function makeDemoContext() {
    const ctx = {
        window: {},
        setInterval() { return 0; },
        clearInterval() {},
        Date,
        Math,
        console,
        URLSearchParams,
        FormData,
        fetch() { return Promise.reject(new Error('unexpected real fetch')); },
    };
    ctx.window = ctx;
    vm.createContext(ctx);
    vm.runInContext(librarySource, ctx);
    vm.runInContext(library2Source, ctx);
    vm.runInContext(radioSource, ctx);
    vm.runInContext(measurementsSource, ctx);
    vm.runInContext(stateSource, ctx);
    vm.runInContext(routesSource, ctx);
    return ctx;
}

function post(fetch, url, body) {
    return fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
}

(async () => {
    const ctx = makeDemoContext();
    const fetch = ctx.fetch;

    // Demo Library 2 is the presented default.
    const libraries = await (await fetch('/api/music-libraries')).json();
    assert.equal(libraries.active_id, 'demo-library-2');
    assert.equal(libraries.active_type, 'smb');
    const ids = libraries.libraries.map(l => l.id);
    assert.ok(ids.includes('local'), 'local library listed');
    assert.ok(ids.includes('demo-nas'), 'existing NAS library untouched');
    const second = libraries.libraries.find(l => l.id === 'demo-library-2');
    assert.ok(second, 'Demo Library 2 listed');
    assert.equal(second.label, 'Demo Library 2');
    assert.equal(second.type, 'smb');

    const albums2 = await (await fetch('/api/albums')).json();
    const tracks2 = await (await fetch('/api/tracks')).json();
    assert.equal(albums2.length, 23, 'library 2 serves 23 albums, got ' + albums2.length);
    assert.equal(tracks2.length, 257, 'library 2 serves 257 tracks, got ' + tracks2.length);

    const playlists2 = await (await fetch('/api/playlists')).json();
    assert.ok(playlists2.length >= 2, 'library 2 has playlists');
    for (const pl of playlists2) {
        const missing = pl.track_ids.filter(id => !tracks2.some(t => t.id === id));
        assert.equal(missing.length, 0, 'playlist tracks resolve in library 2: ' + pl.name);
    }

    const sample = albums2[0];
    const albumTracks = await (await fetch('/api/albums/' + sample.id + '/tracks')).json();
    assert.ok(albumTracks.length >= 8 && albumTracks.length <= 14,
        'album track count in 8..14, got ' + albumTracks.length);
    assert.ok(albumTracks.every(t => t.album === sample.name), 'album tracks belong to the album');

    const cover = await (await fetch('/api/albums/' + sample.id + '/cover')).json();
    assert.match(cover.redirect, /^\/static\/demo\/d2-/, 'album cover from the library-2 pool: ' + cover.redirect);
    const trackCover = await (await fetch('/api/tracks/cover/' + albumTracks[0].id)).json();
    assert.equal(trackCover.redirect, cover.redirect, 'track cover matches its album cover');
    const coverInfo = await (await fetch('/api/tracks/cover-info/' + albumTracks[0].id)).json();
    assert.equal(coverInfo.cover_available, true);

    const status = await (await fetch('/api/library/status')).json();
    assert.equal(status.tracks_found, 257);

    const search = await (await fetch('/api/albums?query=jazz')).json();
    assert.ok(search.length > 0, 'search within library 2 finds jazz albums');
    assert.ok(search.every(a => albums2.some(b => b.id === a.id)), 'search stays inside library 2');

    // Local playback resolves against the active catalog.
    const play = await (await post(fetch, '/api/play', { track_id: albumTracks[0].id })).json();
    assert.equal(play.track.id, albumTracks[0].id, 'played the requested library-2 track');
    assert.equal(play.playback.current_track.id, albumTracks[0].id, 'playback state follows library 2');
    assert.match(play.playback.current_track.cover_url, /^\/static\/demo\/d2-/, 'playback cover from library 2');

    // Switching to local serves the main catalog (with its own playlists),
    // switching back restores library 2 untouched.
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const localAlbums = await (await fetch('/api/albums')).json();
    const localTracks = await (await fetch('/api/tracks')).json();
    assert.ok(localAlbums.length > 0 && localTracks.length > 0, 'main catalog non-empty');
    assert.ok(!localAlbums.some(a => albums2.some(b => b.id === a.id)), 'catalogs do not share album ids');
    const localPlaylists = await (await fetch('/api/playlists')).json();
    assert.ok(localPlaylists.length >= 2, 'main catalog keeps its playlists');
    assert.ok(!localPlaylists.some(p => playlists2.some(q => q.id === p.id)), 'playlists are per catalog');

    await post(fetch, '/api/music-libraries/select', { id: 'demo-library-2' });
    const restoredAlbums = await (await fetch('/api/albums')).json();
    const restoredTracks = await (await fetch('/api/tracks')).json();
    assert.deepEqual(restoredAlbums.map(a => a.id), albums2.map(a => a.id), 'switching back restores library 2 albums');
    assert.deepEqual(restoredTracks.map(t => t.id), tracks2.map(t => t.id), 'switching back restores library 2 tracks');

    console.log('ok demo library 2 contract');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
