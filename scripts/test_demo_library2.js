#!/usr/bin/env node
// Demo Library 2 contract: the web demo presents "NAS Library 1" as its
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

    // NAS Library 1 (the second demo share) is the presented default,
    // NAS Library 2 second for switching.
    const libraries = await (await fetch('/api/music-libraries')).json();
    assert.equal(libraries.active_id, 'demo-library-2');
    assert.equal(libraries.active_type, 'smb');
    const ids = libraries.libraries.map(l => l.id);
    assert.ok(ids.includes('local'), 'local library listed');
    const nas1 = libraries.libraries.find(l => l.id === 'demo-library-2');
    assert.ok(nas1, 'NAS Library 1 listed');
    assert.equal(nas1.label, 'NAS Library 1');
    assert.equal(nas1.type, 'smb');
    const nas2 = libraries.libraries.find(l => l.id === 'demo-nas');
    assert.ok(nas2, 'existing NAS library untouched');
    assert.equal(nas2.label, 'NAS Library 2');
    assert.equal(nas2.type, 'smb');

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

    // ── Cross-catalog resolution while a library is inactive ───────────
    // Ids from the inactive catalog must keep resolving to their owning
    // catalog: a track that keeps playing after a library switch stays
    // fully resolvable (playback state, cover, graph rate, album tracks),
    // and endpoints must not re-filter through the active catalog.
    const playingTrack = albumTracks[0];
    await post(fetch, '/api/play', { track_id: playingTrack.id });
    await post(fetch, '/api/music-libraries/select', { id: 'local' });

    const statusAfterSwitch = await (await fetch('/api/status')).json();
    assert.equal(statusAfterSwitch.current_track.id, playingTrack.id,
        'still-playing library-2 track survives the switch');

    const coverInfoAfterSwitch = await (await fetch('/api/tracks/cover-info/' + playingTrack.id)).json();
    assert.equal(coverInfoAfterSwitch.cover_available, true, 'cover info resolves after the switch');
    assert.match(coverInfoAfterSwitch.cover_url, /^\/static\/demo\/d2-/, 'cover url still from library 2');
    const coverAfterSwitch = await (await fetch('/api/tracks/cover/' + playingTrack.id)).json();
    assert.match(coverAfterSwitch.redirect, /^\/static\/demo\/d2-/, 'cover redirect still from library 2');

    const samplerate = await (await fetch('/api/audio/samplerate')).json();
    assert.equal(samplerate.active_rate, playingTrack.sample_rate_hz,
        'graph rate follows the playing library-2 track after the switch');

    const staleAlbumTracks = await (await fetch('/api/albums/' + sample.id + '/tracks')).json();
    assert.equal(staleAlbumTracks.length, albumTracks.length,
        'inactive-catalog album tracks resolve from their owning catalog');
    assert.ok(staleAlbumTracks.every(t => t.album === sample.name), '…and belong to the album');

    // ── Playlist mutations land in the active catalog only ─────────────
    await post(fetch, '/api/music-libraries/select', { id: 'demo-library-2' });
    const created2 = await (await post(fetch, '/api/playlists',
        { name: 'Switch Contract', track_ids: [playingTrack.id] })).json();
    assert.equal(created2.status, 'ok');
    const playlistsWithNew = await (await fetch('/api/playlists')).json();
    assert.ok(playlistsWithNew.some(pl => pl.id === created2.playlist.id),
        'created playlist appears in the active (library-2) catalog');
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const localPlaylistsWithNew = await (await fetch('/api/playlists')).json();
    assert.ok(!localPlaylistsWithNew.some(pl => pl.id === created2.playlist.id),
        'library-2 playlist invisible in the local catalog');
    await post(fetch, '/api/music-libraries/select', { id: 'demo-library-2' });
    const stillThere = await (await fetch('/api/playlists')).json();
    assert.ok(stillThere.some(pl => pl.id === created2.playlist.id),
        'library-2 playlist survives a round trip');
    await fetch('/api/playlists/' + created2.playlist.id, { method: 'DELETE' });
    const afterDelete2 = await (await fetch('/api/playlists')).json();
    assert.ok(!afterDelete2.some(pl => pl.id === created2.playlist.id),
        'deleted playlist disappears from the active catalog');

    // The local catalog accepts its own playlist mutations.
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const createdLocal = await (await post(fetch, '/api/playlists',
        { name: 'Switch Contract Local', track_ids: [localTracks[0].id] })).json();
    assert.equal(createdLocal.status, 'ok');
    const localAfterCreate = await (await fetch('/api/playlists')).json();
    assert.ok(localAfterCreate.some(pl => pl.id === createdLocal.playlist.id),
        'created playlist appears in the local catalog');
    await fetch('/api/playlists/' + createdLocal.playlist.id, { method: 'DELETE' });
    const localAfterDelete = await (await fetch('/api/playlists')).json();
    assert.ok(!localAfterDelete.some(pl => pl.id === createdLocal.playlist.id),
        'deleted playlist disappears from the local catalog');

    console.log('ok demo library 2 contract');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
