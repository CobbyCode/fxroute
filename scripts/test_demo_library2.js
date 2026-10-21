#!/usr/bin/env node
// Demo library contract: the web demo presents "SMB_Demo_Library-1" as its
// active library (selectable under Settings like on a real box). Each
// library (Local, SMB-1, SMB-2) serves its own albums, tracks, covers and
// playlists; switching restores the other catalogs untouched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const librarySource = fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8');
const library2Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library2.js'), 'utf8');
const library3Source = fs.readFileSync(path.join(root, 'demo', 'data', 'library3.js'), 'utf8');
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
    vm.runInContext(library3Source, ctx);
    vm.runInContext(radioSource, ctx);
    vm.runInContext(measurementsSource, ctx);
    vm.runInContext(stateSource, ctx);
    vm.runInContext(routesSource, ctx);
    return ctx;
}

function post(fetch, url, body) {
    return fetch(url, { method: 'POST', body: JSON.stringify(body || {}) });
}

async function checkCatalog(fetch, { selectId, albums, coverRe, label }) {
    await post(fetch, '/api/music-libraries/select', { id: selectId });
    const gotAlbums = await (await fetch('/api/albums')).json();
    const gotTracks = await (await fetch('/api/tracks')).json();
    assert.equal(gotAlbums.length, albums, `${label} serves ${albums} albums, got ` + gotAlbums.length);
    assert.ok(gotTracks.length > 0, label + ' serves tracks');
    // Folders view: the id is the track's path relative to the music root
    // ("local_<Album>/<NN> - <Title>.flac"), which getTrackRelativePath() strips
    // to "<Album>/<NN> - <Title>.flac". So every track must sit inside its
    // album's folder and no track may stay loose at the root.
    const folderOf = t => t.id.slice('local_'.length).split('/').filter(Boolean).slice(0, -1).join('/');
    assert.ok(gotTracks.every(t => t.id.startsWith('local_') && folderOf(t)),
        label + ' track ids are album-relative paths');
    const folders = new Set(gotTracks.map(folderOf));
    const albumNames = new Set(gotAlbums.map(a => a.name));
    assert.equal(folders.size, albumNames.size, label + ' has exactly one folder per album');
    for (const folder of folders) {
        assert.ok(albumNames.has(folder), label + ' folder matches the album name: ' + folder);
    }
    const playlists = await (await fetch('/api/playlists')).json();
    assert.ok(playlists.length >= 2, label + ' has playlists');
    for (const pl of playlists) {
        const missing = pl.track_ids.filter(id => !gotTracks.some(t => t.id === id));
        assert.equal(missing.length, 0, 'playlist tracks resolve in ' + label + ': ' + pl.name);
    }
    const sample = gotAlbums[0];
    const albumTracks = await (await fetch('/api/albums/' + sample.id + '/tracks')).json();
    assert.ok(albumTracks.length >= 8 && albumTracks.length <= 14,
        'album track count in 8..14, got ' + albumTracks.length);
    assert.ok(albumTracks.every(t => t.album === sample.name), 'album tracks belong to the album');
    const cover = await (await fetch('/api/albums/' + sample.id + '/cover')).json();
    assert.match(cover.redirect, coverRe, `album cover from the ${label} pool: ` + cover.redirect);
    const trackCover = await (await fetch('/api/tracks/cover/' + albumTracks[0].id)).json();
    assert.equal(trackCover.redirect, cover.redirect, 'track cover matches its album cover');
    const coverInfo = await (await fetch('/api/tracks/cover-info/' + albumTracks[0].id)).json();
    assert.equal(coverInfo.cover_available, true);
    // About texts + discover similar, same contract as the main catalog.
    assert.ok(gotAlbums.every(a => a.artist_description && a.artist_description.trim()),
        `every ${label} album carries an artist about text`);
    const albumAbout = gotAlbums.find(a => a.album_description);
    if (label !== 'SMB_Demo_Library-2') {
        assert.ok(albumAbout && albumAbout.album_description.trim(),
            `at least one ${label} album carries an album-level about text`);
    }
    const discover = await (await fetch('/api/albums/' + sample.id + '/discover')).json();
    assert.ok(discover.items.length > 0 && discover.items.length <= 6,
        'discover returns 1..6 similar artists, got ' + discover.items.length);
    assert.ok(discover.items.every(a => gotAlbums.some(b => b.artist === a.artist)),
        'discover stays inside ' + label);
    assert.ok(!discover.items.some(a => a.artist === sample.artist), 'discover excludes the album artist');
    return { gotAlbums, gotTracks, playlists, sample, albumTracks };
}

(async () => {
    const ctx = makeDemoContext();
    const fetch = ctx.fetch;

    // SMB_Demo_Library-1 (the first demo share) is the presented default.
    const libraries = await (await fetch('/api/music-libraries')).json();
    assert.equal(libraries.active_id, 'demo-library-2');
    assert.equal(libraries.active_type, 'smb');
    const ids = libraries.libraries.map(l => l.id);
    assert.ok(ids.includes('local'), 'local library listed');
    const smb1 = libraries.libraries.find(l => l.id === 'demo-library-2');
    assert.ok(smb1, 'SMB_Demo_Library-1 listed');
    assert.equal(smb1.label, 'SMB_Demo_Library-1');
    assert.equal(smb1.type, 'smb');
    assert.equal(smb1.mounted, true);
    const smb2 = libraries.libraries.find(l => l.id === 'demo-nas');
    assert.ok(smb2, 'SMB_Demo_Library-2 listed');
    assert.equal(smb2.label, 'SMB_Demo_Library-2');
    assert.equal(smb2.type, 'smb');
    assert.equal(smb2.server, '192.168.178.100');
    assert.equal(smb2.share, 'Music-Demo-2');

    // NFS exports of the same NAS folders are discovered alongside SMB and
    // start unmounted; the manual-entry action stays the last option.
    const nfs = libraries.libraries.find(l => l.id === 'demo-nfs');
    assert.ok(nfs, 'NFS export of the SMB-1 folder listed');
    assert.equal(nfs.type, 'nfs');
    assert.equal(nfs.server, '192.168.178.100');
    assert.equal(nfs.share, '/export/Music-Demo');
    assert.equal(nfs.mounted, false, 'a discovered NFS export starts unmounted');
    assert.match(nfs.label, /^NFS — 192\.168\.178\.100:\/export\/Music-Demo$/);
    const nfsArchive = libraries.libraries.find(l => l.id === 'demo-nfs-archive');
    assert.ok(nfsArchive, 'second NFS export listed');
    assert.equal(nfsArchive.type, 'nfs');
    assert.equal(nfsArchive.share, '/export/Archive');
    assert.equal(nfsArchive.mounted, false);
    const lastOption = libraries.libraries[libraries.libraries.length - 1];
    assert.deepEqual([lastOption.id, lastOption.type], ['manual', 'action'],
        'the manual-entry action stays the last library option');
    assert.equal(smb1.mounted, true, 'SMB shares stay mounted next to NFS');

    // The payload renders through the product selector model unchanged, so
    // both protocols and the manual entry are visible in Settings.
    const settings = require('../static/settings_system.js');
    const selectModel = settings.musicLibrarySelectModel(libraries);
    assert.match(selectModel.html, /SMB_Demo_Library-1/);
    assert.match(selectModel.html, /SMB_Demo_Library-2/);
    assert.match(selectModel.html, /NFS — 192\.168\.178\.100:\/export\/Music-Demo/);
    assert.match(selectModel.html, /NFS — 192\.168\.178\.100:\/export\/Archive/);
    assert.match(selectModel.html, /Add network share manually/);
    assert.equal(selectModel.value, 'demo-library-2');
    assert.equal(selectModel.disabled, false);
    const removalModel = settings.musicLibraryRemovalModel(libraries);
    assert.match(removalModel.html, /demo-library-2/);
    assert.match(removalModel.html, /demo-nfs/);
    assert.doesNotMatch(removalModel.html, /value="local"|value="manual"/,
        'only network shares are removable');

    const c1 = await checkCatalog(fetch, {
        selectId: 'demo-library-2', albums: 16,
        coverRe: /^\/static\/demo\/s1-/, label: 'SMB_Demo_Library-1',
    });
    const status = await (await fetch('/api/library/status')).json();
    assert.equal(status.tracks_found, c1.gotTracks.length);

    const search = await (await fetch('/api/albums?query=jazz')).json();
    assert.ok(search.length > 0, 'search within library 1 finds jazz albums');
    assert.ok(search.every(a => c1.gotAlbums.some(b => b.id === a.id)), 'search stays inside library 1');

    // Local playback resolves against the active catalog.
    const play = await (await post(fetch, '/api/play', { track_id: c1.albumTracks[0].id })).json();
    assert.equal(play.track.id, c1.albumTracks[0].id, 'played the requested library-1 track');
    assert.equal(play.playback.current_track.id, c1.albumTracks[0].id, 'playback state follows library 1');
    assert.match(play.playback.current_track.cover_url, /^\/static\/demo\/s1-/, 'playback cover from library 1');

    // The second share serves its own smaller catalog.
    const c2 = await checkCatalog(fetch, {
        selectId: 'demo-nas', albums: 12,
        coverRe: /^\/static\/demo\/s2-/, label: 'SMB_Demo_Library-2',
    });
    assert.ok(!c2.gotAlbums.some(a => c1.gotAlbums.some(b => b.id === a.id)), 'shares do not share album ids');

    // ── NFS shares resolve to the same NAS folders as SMB ──────────────
    const selectedNfs = await (await post(fetch, '/api/music-libraries/select', { id: 'demo-nfs' })).json();
    assert.equal(selectedNfs.active_id, 'demo-nfs');
    assert.equal(selectedNfs.active_type, 'nfs');
    assert.equal(selectedNfs.libraries.find(l => l.id === 'demo-nfs').mounted, true,
        'selecting an NFS export mounts it');
    assert.equal(selectedNfs.libraries.find(l => l.id === 'demo-library-2').mounted, true,
        'the SMB mount stays available next to NFS');
    const nfsAlbums = await (await fetch('/api/albums')).json();
    const nfsTracks = await (await fetch('/api/tracks')).json();
    assert.deepEqual(nfsAlbums.map(a => a.id), c1.gotAlbums.map(a => a.id),
        'the NFS export of the SMB-1 folder serves that catalog');
    assert.deepEqual(nfsTracks.map(t => t.id), c1.gotTracks.map(t => t.id));
    const nfsPlay = await (await post(fetch, '/api/play', { track_id: nfsTracks[0].id })).json();
    assert.equal(nfsPlay.track.id, nfsTracks[0].id, 'playback works over NFS');
    assert.match(nfsPlay.playback.current_track.cover_url, /^\/static\/demo\/s1-/, 'NFS playback keeps its cover');

    const archiveAlbums = await (async () => {
        const selected = await (await post(fetch, '/api/music-libraries/select', { id: 'demo-nfs-archive' })).json();
        assert.equal(selected.active_type, 'nfs');
        assert.equal(selected.libraries.find(l => l.id === 'demo-nfs-archive').mounted, true);
        return (await fetch('/api/albums')).json();
    })();
    assert.deepEqual(archiveAlbums.map(a => a.id), c2.gotAlbums.map(a => a.id),
        'the archive NFS export serves the SMB-2 catalog');

    // Local serves the main catalog (with its own playlists).
    const c0 = await checkCatalog(fetch, {
        selectId: 'local', albums: 18,
        coverRe: /^\/static\/demo\/loc-/, label: 'Local',
    });
    assert.ok(!c0.gotAlbums.some(a => c1.gotAlbums.some(b => b.id === a.id)), 'catalogs do not share album ids');
    assert.ok(!c0.playlists.some(p => c1.playlists.some(q => q.id === p.id)), 'playlists are per catalog');

    await post(fetch, '/api/music-libraries/select', { id: 'demo-library-2' });
    const restoredAlbums = await (await fetch('/api/albums')).json();
    const restoredTracks = await (await fetch('/api/tracks')).json();
    assert.deepEqual(restoredAlbums.map(a => a.id), c1.gotAlbums.map(a => a.id), 'switching back restores library 1 albums');
    assert.deepEqual(restoredTracks.map(t => t.id), c1.gotTracks.map(t => t.id), 'switching back restores library 1 tracks');

    // ── Cross-catalog resolution while a library is inactive ───────────
    // Ids from the inactive catalog must keep resolving to their owning
    // catalog: a track that keeps playing after a library switch stays
    // fully resolvable (playback state, cover, graph rate, album tracks),
    // and endpoints must not re-filter through the active catalog.
    const playingTrack = c1.albumTracks[0];
    await post(fetch, '/api/play', { track_id: playingTrack.id });
    await post(fetch, '/api/music-libraries/select', { id: 'local' });

    const statusAfterSwitch = await (await fetch('/api/status')).json();
    assert.equal(statusAfterSwitch.current_track.id, playingTrack.id,
        'still-playing library-1 track survives the switch');

    const coverInfoAfterSwitch = await (await fetch('/api/tracks/cover-info/' + playingTrack.id)).json();
    assert.equal(coverInfoAfterSwitch.cover_available, true, 'cover info resolves after the switch');
    assert.match(coverInfoAfterSwitch.cover_url, /^\/static\/demo\/s1-/, 'cover url still from library 1');
    const coverAfterSwitch = await (await fetch('/api/tracks/cover/' + playingTrack.id)).json();
    assert.match(coverAfterSwitch.redirect, /^\/static\/demo\/s1-/, 'cover redirect still from library 1');

    const samplerate = await (await fetch('/api/audio/samplerate')).json();
    assert.equal(samplerate.active_rate, playingTrack.sample_rate_hz,
        'graph rate follows the playing library-1 track after the switch');

    const staleAlbumTracks = await (await fetch('/api/albums/' + c1.sample.id + '/tracks')).json();
    assert.equal(staleAlbumTracks.length, c1.albumTracks.length,
        'inactive-catalog album tracks resolve from their owning catalog');
    assert.ok(staleAlbumTracks.every(t => t.album === c1.sample.name), '…and belong to the album');

    // ── Playlist mutations land in the active catalog only ─────────────
    await post(fetch, '/api/music-libraries/select', { id: 'demo-library-2' });
    const created2 = await (await post(fetch, '/api/playlists',
        { name: 'Switch Contract', track_ids: [playingTrack.id] })).json();
    assert.equal(created2.status, 'ok');
    const playlistsWithNew = await (await fetch('/api/playlists')).json();
    assert.ok(playlistsWithNew.some(pl => pl.id === created2.playlist.id),
        'created playlist appears in the active (library-1) catalog');
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const localPlaylistsWithNew = await (await fetch('/api/playlists')).json();
    assert.ok(!localPlaylistsWithNew.some(pl => pl.id === created2.playlist.id),
        'library-1 playlist invisible in the local catalog');
    await post(fetch, '/api/music-libraries/select', { id: 'demo-library-2' });
    const stillThere = await (await fetch('/api/playlists')).json();
    assert.ok(stillThere.some(pl => pl.id === created2.playlist.id),
        'library-1 playlist survives a round trip');
    await fetch('/api/playlists/' + created2.playlist.id, { method: 'DELETE' });
    const afterDelete2 = await (await fetch('/api/playlists')).json();
    assert.ok(!afterDelete2.some(pl => pl.id === created2.playlist.id),
        'deleted playlist disappears from the active catalog');

    // The local catalog accepts its own playlist mutations.
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const createdLocal = await (await post(fetch, '/api/playlists',
        { name: 'Switch Contract Local', track_ids: [c0.gotTracks[0].id] })).json();
    assert.equal(createdLocal.status, 'ok');
    const localAfterCreate = await (await fetch('/api/playlists')).json();
    assert.ok(localAfterCreate.some(pl => pl.id === createdLocal.playlist.id),
        'created playlist appears in the local catalog');
    await fetch('/api/playlists/' + createdLocal.playlist.id, { method: 'DELETE' });
    const localAfterDelete = await (await fetch('/api/playlists')).json();
    assert.ok(!localAfterDelete.some(pl => pl.id === createdLocal.playlist.id),
        'deleted playlist disappears from the local catalog');

    // ── Manual entry: both protocols, mount-on-select, validation ───────
    await post(fetch, '/api/music-libraries/select', { id: 'local' });
    const addedSmb = await (await post(fetch, '/api/music-libraries/manual', {
        url: 'smb://nas.local/Archive',
    })).json();
    assert.deepEqual(
        [addedSmb.entry.id, addedSmb.entry.type, addedSmb.entry.server, addedSmb.entry.share, addedSmb.entry.mounted],
        ['smb:nas.local:Archive', 'smb', 'nas.local', 'Archive', false],
        'manual SMB entry uses the backend id scheme and starts unmounted');
    assert.equal(addedSmb.libraries[addedSmb.libraries.length - 1].id, 'manual',
        'manually added shares go in front of the manual action');

    const addedNfs = await (await post(fetch, '/api/music-libraries/manual', { url: 'nas:/exports/My Music/' })).json();
    assert.deepEqual(
        [addedNfs.entry.id, addedNfs.entry.type, addedNfs.entry.share, addedNfs.entry.mounted],
        ['nfs:nas:/exports/My Music', 'nfs', '/exports/My Music', false],
        'manual NFS entry normalises slashes and starts unmounted');

    const selectedManualNfs = await (await post(fetch, '/api/music-libraries/select', { id: addedNfs.entry.id })).json();
    assert.equal(selectedManualNfs.libraries.find(entry => entry.id === addedNfs.entry.id).mounted, true,
        'selecting a manually added NFS export mounts it');
    assert.equal(selectedManualNfs.active_type, 'nfs');
    await post(fetch, '/api/music-libraries/select', { id: 'local' });

    const invalid = await post(fetch, '/api/music-libraries/manual', { url: 'ftp://nas/music' });
    assert.equal(invalid.status, 400, 'an unusable URL is rejected');
    assert.match((await invalid.json()).detail, /smb:\/\/server\/share/, 'the error names both accepted forms');

    const removedNfs = await (await post(fetch, '/api/music-libraries/remove', { id: addedNfs.entry.id })).json();
    assert.ok(!removedNfs.libraries.some(entry => entry.id === addedNfs.entry.id));
    assert.ok(removedNfs.libraries.some(entry => entry.id === 'demo-library-2'), 'removing NFS preserves SMB catalogs');
    assert.ok(removedNfs.libraries.some(entry => entry.id === 'demo-nfs'), 'discovered NFS exports stay listed');

    const removedSmb = await (await post(fetch, '/api/music-libraries/remove', { id: addedSmb.entry.id })).json();
    assert.ok(!removedSmb.libraries.some(entry => entry.id === addedSmb.entry.id));
    assert.ok(removedSmb.libraries.some(entry => entry.id === 'demo-nas'), 'removing SMB preserves the other shares');
    assert.equal(removedSmb.libraries[removedSmb.libraries.length - 1].id, 'manual');
    console.log('ok demo library contract (local + SMB-1 + SMB-2 + NFS exports + manual SMB/NFS)');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
