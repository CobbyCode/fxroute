#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// TIDAL demo ID/detail mapping: playlist/album track lists must carry the
// correct count and order, and every track click must resolve to the exact
// song (no playlist-local or top-track alias ids, no silent fallback to a
// different track).
//
// Regression: playlist tracks used per-album ids (id + '_t' + index) that
// collided across member albums and never resolved in playback, artist top
// tracks used invented '_top' ids, and playTidal only knew a curated
// tidalTracks subset — deep album tracks, playlist tracks and top tracks
// silently played the wrong song (pool[0]).

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

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
    for (const file of [
        'demo/data/library.js',
        'demo/data/library2.js',
        'demo/data/library3.js',
        'demo/data/radio.js',
        'demo/data/measurements.js',
        'demo/state.js',
        'demo/routes.js',
    ]) {
        vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), ctx);
    }
    return ctx;
}

(async () => {
    const ctx = makeDemoContext();
    const demoFetch = ctx.fetch;
    const state = ctx.FXROUTE_DEMO_STATE;
    const lib = ctx.FXROUTE_DEMO_LIBRARY;
    const tidalAlbums = lib.tidalAlbums;
    const tidalPlaylists = lib.tidalPlaylists;
    assert.ok(tidalAlbums.length >= 10, 'demo needs several TIDAL albums');
    assert.ok(tidalPlaylists.length >= 4, 'demo needs several TIDAL playlists');

    const byId = new Map(lib.tidalTracks.map((t) => [String(t.id), t]));
    // The pool must be the full catalog so every detail/search/favorite id
    // resolves like the real provider boundary.
    const albumTrackTotal = tidalAlbums.reduce((n, a) => n + a.tracks.length, 0);
    assert.equal(lib.tidalTracks.length, albumTrackTotal, 'tidalTracks must carry every album track');

    // ── Albums: correct count, order and ids ──────────────────────────
    for (const album of tidalAlbums) {
        const meta = await (await demoFetch('/api/streaming/tidal/albums/' + album.id)).json();
        const tracks = await (await demoFetch('/api/streaming/tidal/albums/' + album.id + '/tracks')).json();
        assert.equal(meta.num_tracks, album.tracks.length, 'album meta count: ' + album.id);
        assert.equal(tracks.length, album.tracks.length, 'album track count: ' + album.id);
        tracks.forEach((t, index) => {
            const expected = album.tracks[index];
            assert.equal(String(t.id), String(expected.id), 'album order/id: ' + album.id + ' #' + (index + 1));
            assert.equal(t.title, expected.title);
            assert.equal(t.artist, album.artist);
            assert.equal(t.album, album.title);
            assert.equal(t.track_number, expected.trackNumber);
            assert.ok(t.art_url && t.art_url.endsWith('.jpg'), 'album track cover: ' + t.id);
            assert.equal(t.audio_quality, album.audio_quality, 'album track tier: ' + t.id);
        });
    }

    // ── Playlists: correct count, order, unique shared ids ────────────
    const seeds = JSON.parse(fs.readFileSync(path.join(root, 'demo', 'data', 'library.js'), 'utf8')
        .match(/const TIDAL_PLAYLIST_SEEDS = (\[.*?\]);/s)[1]);
    for (const playlist of tidalPlaylists) {
        const seed = seeds.find((s) => s[0] === playlist.id);
        assert.ok(seed, 'playlist seed for ' + playlist.id);
        const memberAlbums = seed[3].map((aid) => tidalAlbums.find((a) => a.id === aid)).filter(Boolean);
        const expectedTotal = memberAlbums.reduce((n, a) => n + a.tracks.length, 0);
        const detail = await (await demoFetch('/api/streaming/tidal/playlists/' + playlist.id)).json();
        const tracks = await (await demoFetch('/api/streaming/tidal/playlists/' + playlist.id + '/tracks')).json();
        assert.equal(detail.track_count, expectedTotal, 'playlist detail count: ' + playlist.id);
        assert.equal(tracks.length, expectedTotal, 'playlist track count: ' + playlist.id);
        assert.equal(playlist.track_count, expectedTotal);
        const ids = tracks.map((t) => String(t.id));
        assert.equal(new Set(ids).size, ids.length, 'playlist ids unique: ' + playlist.id);
        assert.ok(ids.every((id) => !id.startsWith(playlist.id + '_t')), 'no playlist-local ids: ' + playlist.id);
        let cursor = 0;
        for (const album of memberAlbums) {
            for (const t of album.tracks) {
                const row = tracks[cursor];
                assert.equal(String(row.id), String(t.id), 'playlist order: ' + playlist.id + ' #' + (cursor + 1));
                assert.equal(row.title, t.title);
                assert.equal(row.artist, album.artist);
                assert.equal(row.album, album.title);
                assert.equal(row.audio_quality, album.audio_quality);
                cursor += 1;
            }
        }
        // Every playlist row must resolve through the shared pool.
        for (const row of tracks) {
            assert.ok(byId.has(String(row.id)), 'playlist row in shared pool: ' + row.id);
        }
    }

    // ── Every click plays exactly that song ───────────────────────────
    // Mirrors the frontend detail rows: the displayed list order becomes the
    // queue (renderDetailTracks ids) and the clicked row becomes track_id.
    async function assertClickPlays(items, pickIndex, label) {
        const ids = items.map((t) => String(t.id));
        const pick = items[pickIndex];
        const played = state.playTidal(String(pick.id), ids);
        assert.ok(played, 'playTidal resolves ' + pick.id + ' (' + label + ')');
        assert.equal(String(played.id), String(pick.id), 'state plays clicked track (' + label + ')');
        assert.equal(played.title, pick.title);
        const resp = await demoFetch('/api/play', {
            method: 'POST',
            body: JSON.stringify({ source: 'tidal', track_id: String(pick.id), queue_track_ids: ids }),
        });
        assert.equal(resp.status, 200, 'play API ok for ' + pick.id + ' (' + label + ')');
        const data = await resp.json();
        assert.equal(String(data.playback.current_track.id), String(pick.id));
        assert.equal(data.playback.current_track.title, pick.title);
        assert.equal(data.playback.queue.count, ids.length, 'queue keeps the detail order (' + label + ')');
        assert.deepEqual(
            data.playback.queue.tracks.map((t) => String(t.id)),
            ids,
            'queue order matches the displayed list (' + label + ')',
        );
        state.stop();
    }

    // Several albums: opener, middle and closer (deep tracks previously fell
    // back to the pool opener).
    for (const albumId of ['t_album_01', 't_album_04', 't_album_07', 't_album_12', 't_album_18']) {
        const tracks = await (await demoFetch('/api/streaming/tidal/albums/' + albumId + '/tracks')).json();
        assert.ok(tracks.length > 2, albumId + ' must have deep tracks');
        await assertClickPlays(tracks, 0, albumId + ' opener');
        await assertClickPlays(tracks, Math.floor(tracks.length / 2), albumId + ' middle');
        await assertClickPlays(tracks, tracks.length - 1, albumId + ' closer');
    }
    // Several playlists: start, middle and end rows (previously colliding
    // playlist-local ids all played the same wrong song).
    for (const playlistId of ['t_playlist_01', 't_playlist_03', 't_playlist_04', 't_playlist_06']) {
        const tracks = await (await demoFetch('/api/streaming/tidal/playlists/' + playlistId + '/tracks')).json();
        await assertClickPlays(tracks, 0, playlistId + ' first');
        await assertClickPlays(tracks, Math.floor(tracks.length / 2), playlistId + ' middle');
        await assertClickPlays(tracks, tracks.length - 1, playlistId + ' last');
    }
    // Artist top tracks must reuse the shared ids (no '_top' aliases).
    for (const artistId of ['t_artist_01', 't_artist_05', 't_artist_14']) {
        const detail = await (await demoFetch('/api/streaming/tidal/artists/' + artistId)).json();
        assert.ok(detail.top_tracks.length > 0, 'top tracks for ' + artistId);
        for (const t of detail.top_tracks) {
            assert.ok(!String(t.id).includes('_top'), 'no alias id: ' + t.id);
            assert.ok(byId.has(String(t.id)), 'top track in shared pool: ' + t.id);
        }
        await assertClickPlays(detail.top_tracks, 0, artistId + ' top');
        await assertClickPlays(detail.top_tracks, detail.top_tracks.length - 1, artistId + ' top last');
    }

    // ── Favorites, snapshot and search stay on the shared ids ─────────
    const favIds = await (await demoFetch('/api/streaming/tidal/favorites/ids')).json();
    const favTracks = await (await demoFetch('/api/streaming/tidal/favorites?type=tracks')).json();
    assert.equal(favTracks.length, favIds.tracks.length, 'every favorited track resolves');
    for (const id of favIds.tracks) {
        assert.ok(byId.has(String(id)), 'favorite id in shared pool: ' + id);
    }
    const deepTitle = tidalAlbums[0].tracks[4].title;
    const search = await (await demoFetch('/api/streaming/tidal/search?q=' + encodeURIComponent(deepTitle))).json();
    assert.ok(
        search.tracks.some((t) => String(t.id) === String(tidalAlbums[0].tracks[4].id)),
        'search finds deep album tracks: ' + deepTitle,
    );

    // An explicit unknown id must 404, never silently play another song.
    const bad = await demoFetch('/api/play', {
        method: 'POST',
        body: JSON.stringify({ source: 'tidal', track_id: 'no-such-track', queue_track_ids: ['no-such-track'] }),
    });
    assert.equal(bad.status, 404, 'unknown TIDAL track must not play a wrong song');

    console.log('ok demo tidal track mapping');
})();
