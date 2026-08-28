// Demo music catalog fixtures, shared by state.js / routes.js.
// Artists, albums and tracks are fictional. Covers map deterministically
// onto the local demo artwork pool (static/demo/*.jpg) — albums, artists
// and playlists use separate keys so nearby items never repeat the same art.
(function () {
    'use strict';

    const IMG_KEYS = [
        '1800ECLIPSE', '863', 'CYE', 'ECLIPSE', 'EYE', 'FAREWELL', 'FLOATING',
        'FRAME', 'FREEEEEEEEE', 'GROW', 'KAYCYY', 'MANNNNNN', 'Mistake', 'OBSERVER', 'OVER!',
        'OVER2', 'REFLECT', 'SCHOLARSHIPS', 'SOTH', 'STARS', 'SWANS', 'TEARS', 'TV',
        'USER GUIDE', 'Untitled-1', 'VOICES', 'Vision', 'YOU', 'beacon', 'cake', 'daylight',
        'eyeyeyeyeyeyeyeyeyeyeye', 'fernery', 'her', 'hurricane', 'lilypad', 'match', 'mitski', 'monday',
        'peaceful', 'raimbow', 'risky', 'stoplght', 'sunflower', 'ufo',
    ];

    function hashCode(text) {
        let h = 0;
        for (let i = 0; i < String(text).length; i += 1) {
            h = ((h << 5) - h + String(text).charCodeAt(i)) | 0;
        }
        return h & 0x7fffffff;
    }

    // Deterministic, deduplicated cover assignment: every distinct key gets
    // its own slot in the artwork pool (round-robin per key namespace), so
    // albums/artists/playlists almost never share the same cover. Falls back
    // to even distribution once a namespace outgrows the pool.
    const keySlotCache = new Map();
    const namespaceCursors = new Map();
    const NAMESPACE_OFFSETS = { album: 0, 'tidal-album': 7, 'tidal-artist': 17, playlist: 27, 'tidal-playlist': 33, artist: 40 };

    function demoImage(key) {
        const text = String(key);
        const sep = text.indexOf(':');
        const namespace = sep === -1 ? '' : text.slice(0, sep);
        let slot = keySlotCache.get(text);
        if (slot === undefined) {
            const cursor = namespaceCursors.get(namespace) || 0;
            const offset = NAMESPACE_OFFSETS[namespace] || 0;
            slot = (offset + cursor) % IMG_KEYS.length;
            namespaceCursors.set(namespace, cursor + 1);
            keySlotCache.set(text, slot);
        }
        return '/static/demo/' + encodeURIComponent(IMG_KEYS[slot]) + '.jpg';
    }

    function slug(text) {
        return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    }

    // ---------------------------------------------------------------------
    // Local library
    // ---------------------------------------------------------------------
    // [title, artist, album, genre, year, duration, sampleRate]
    const TRACK_SEEDS = [
        ['Neon Rain', 'Alistair Kade', 'Neon Rain', 'Synthwave', 2023, 214, 44100],
        ['Midnight Arcade', 'Alistair Kade', 'Neon Rain', 'Synthwave', 2023, 189, 44100],
        ['Chrome Sunset', 'Alistair Kade', 'Neon Rain', 'Synthwave', 2023, 243, 44100],
        ['Analog Dreams', 'Alistair Kade', 'Neon Rain', 'Synthwave', 2023, 201, 44100],
        ['Glass Horizon', 'Alistair Kade', 'Neon Rain', 'Synthwave', 2023, 232, 44100],
        ['Paper Planes at Dawn', 'Rhea Lindqvist', 'Harbor Lights', 'Indie Pop', 2021, 197, 44100],
        ['Harbor Lights', 'Rhea Lindqvist', 'Harbor Lights', 'Indie Pop', 2021, 226, 44100],
        ['Salt & Cedar', 'Rhea Lindqvist', 'Harbor Lights', 'Indie Pop', 2021, 184, 44100],
        ['Slow Tide', 'Rhea Lindqvist', 'Harbor Lights', 'Indie Pop', 2021, 251, 44100],
        ['First Light Ferry', 'Rhea Lindqvist', 'Harbor Lights', 'Indie Pop', 2021, 208, 44100],
        ['Copper Kettle', 'The Bramble Trio', 'Old Town Sessions', 'Jazz', 2020, 287, 96000],
        ['Rain on Cobblestone', 'The Bramble Trio', 'Old Town Sessions', 'Jazz', 2020, 342, 96000],
        ['Lamplight Waltz', 'The Bramble Trio', 'Old Town Sessions', 'Jazz', 2020, 265, 96000],
        ['Old Town Sessions', 'The Bramble Trio', 'Old Town Sessions', 'Jazz', 2020, 301, 96000],
        ['Quicksilver', 'The Bramble Trio', 'Old Town Sessions', 'Jazz', 2020, 228, 96000],
        ['Gravity Well', 'Iris Nakamura', 'Gravity Well', 'Electronic', 2024, 264, 48000],
        ['Signal Lost', 'Iris Nakamura', 'Gravity Well', 'Electronic', 2024, 239, 48000],
        ['Parallax Drift', 'Iris Nakamura', 'Gravity Well', 'Electronic', 2024, 278, 48000],
        ['Ionosphere', 'Iris Nakamura', 'Gravity Well', 'Electronic', 2024, 245, 48000],
        ['Redshift Lullaby', 'Iris Nakamura', 'Gravity Well', 'Electronic', 2024, 296, 48000],
        ['Stone & Moss', 'Fern Hollow', 'Woodland Sketches', 'Folk', 2018, 219, 44100],
        ['Riverbed', 'Fern Hollow', 'Woodland Sketches', 'Folk', 2018, 194, 44100],
        ['Willow Bark', 'Fern Hollow', 'Woodland Sketches', 'Folk', 2018, 236, 44100],
        ['Hollow Log Drum', 'Fern Hollow', 'Woodland Sketches', 'Folk', 2018, 178, 44100],
        ['Canopy Walk', 'Fern Hollow', 'Woodland Sketches', 'Folk', 2018, 257, 44100],
        ['Concrete Bloom', 'Kilo City Collective', 'Concrete Bloom', 'Hip-Hop', 2022, 212, 44100],
        ['Neon Alley Run', 'Kilo City Collective', 'Concrete Bloom', 'Hip-Hop', 2022, 198, 44100],
        ['Transit Maps', 'Kilo City Collective', 'Concrete Bloom', 'Hip-Hop', 2022, 224, 44100],
        ['Rooftop Antenna', 'Kilo City Collective', 'Concrete Bloom', 'Hip-Hop', 2022, 205, 44100],
        ['Static Bloom', 'Kilo City Collective', 'Concrete Bloom', 'Hip-Hop', 2022, 231, 44100],
        ['Vantage Point', 'Sierra Reyes', 'Vantage Point', 'Rock', 2020, 254, 44100],
        ['Switchback', 'Sierra Reyes', 'Vantage Point', 'Rock', 2020, 231, 44100],
        ['Treeline', 'Sierra Reyes', 'Vantage Point', 'Rock', 2020, 276, 44100],
        ['Scree Field', 'Sierra Reyes', 'Vantage Point', 'Rock', 2020, 249, 44100],
        ['Summit Wind', 'Sierra Reyes', 'Vantage Point', 'Rock', 2020, 288, 44100],
        ['Blue Hour Drive', 'Nordkap', 'Polar Circle', 'Ambient', 2023, 324, 48000],
        ['Aurora Station', 'Nordkap', 'Polar Circle', 'Ambient', 2023, 367, 48000],
        ['Fjord Light', 'Nordkap', 'Polar Circle', 'Ambient', 2023, 295, 48000],
        ['Ice Fog', 'Nordkap', 'Polar Circle', 'Ambient', 2023, 412, 48000],
        ['Midnight Sun', 'Nordkap', 'Polar Circle', 'Ambient', 2023, 338, 48000],
        ['Tin Roof Rhythm', 'Delta Hollis', 'Mississippi Notes', 'Blues', 2017, 227, 44100],
        ['Levee Stomp', 'Delta Hollis', 'Mississippi Notes', 'Blues', 2017, 243, 44100],
        ['Harmonica Hill', 'Delta Hollis', 'Mississippi Notes', 'Blues', 2017, 199, 44100],
        ['Cypress Grove Shuffle', 'Delta Hollis', 'Mississippi Notes', 'Blues', 2017, 268, 44100],
        ['Delta Hollis', 'Delta Hollis', 'Mississippi Notes', 'Blues', 2017, 254, 44100],
        ['Crystal Towers', 'Aether Drift', 'Aether Drift', 'Electronic', 2022, 312, 96000],
        ['Flux Capacitor', 'Aether Drift', 'Aether Drift', 'Electronic', 2022, 287, 96000],
        ['Binary Sunset', 'Aether Drift', 'Aether Drift', 'Electronic', 2022, 265, 96000],
        ['Phase Shift', 'Aether Drift', 'Aether Drift', 'Electronic', 2022, 341, 96000],
        ['Zero Crossing', 'Aether Drift', 'Aether Drift', 'Electronic', 2022, 298, 96000],
        ['Sakura Blossom Dub', 'SubRoot System', 'Bass Garden', 'Dub / Reggae', 2023, 302, 44100],
        ['Echo Chamber', 'SubRoot System', 'Bass Garden', 'Dub / Reggae', 2023, 278, 44100],
        ['Roots Pressure', 'SubRoot System', 'Bass Garden', 'Dub / Reggae', 2023, 315, 44100],
        ['Tape Delay Sunrise', 'SubRoot System', 'Bass Garden', 'Dub / Reggae', 2023, 296, 44100],
        ['Threshold Lights', 'Kestrel Wire', 'Overpass', 'Post-Rock', 2022, 426, 96000],
        ['Concrete Cathedral', 'Kestrel Wire', 'Overpass', 'Post-Rock', 2022, 503, 96000],
        ['Signal Fire', 'Kestrel Wire', 'Overpass', 'Post-Rock', 2022, 384, 96000],
        ['Monorail Ghosts', 'Kestrel Wire', 'Overpass', 'Post-Rock', 2022, 449, 96000],
        ['Subterranean Rivers', 'Kestrel Wire', 'Overpass', 'Post-Rock', 2022, 517, 96000],
        ['Slow Motion Sunrise', 'Moru', 'Analog Dreams', 'Electronic', 2024, 322, 48000],
        ['Dust Bowl Hymns', 'Delta Hollis', 'Dust Bowl Hymns', 'Blues', 2019, 284, 44100],
    ];

    const tracks = [];
    const albums = [];
    const byAlbum = new Map();
    TRACK_SEEDS.forEach((seed) => {
        const [title, artist, album, genre, year, duration, sampleRate] = seed;
        const albumKey = slug(album);
        const existing = byAlbum.get(albumKey) || { count: 0 };
        existing.count += 1;
        byAlbum.set(albumKey, existing);
        const id = `local_demo_${albumKey}_${existing.count}`;
        const cover = demoImage('album:' + album);
        tracks.push({
            id,
            title,
            artist,
            album,
            album_artist: artist,
            genre,
            year,
            track_number: existing.count,
            disc_number: 1,
            source: 'local',
            url: `file://demo/${albumKey}/${String(existing.count).padStart(2, '0')} - ${title}.flac`,
            duration,
            path: `demo/${album}/${String(existing.count).padStart(2, '0')} - ${title}.flac`,
            sample_rate_hz: sampleRate,
            cover_available: true,
            cover_url: cover,
            cover_info_url: `/api/tracks/cover-info/${id}`,
            artwork_available: true,
            artwork_url: cover,
            artwork_source: 'library',
            favorite: hashCode('fav-track:' + id) % 4 === 0,
        });
    });
    byAlbum.forEach((_, albumKey) => {
        const albumTracks = tracks.filter(t => slug(t.album) === albumKey);
        const first = albumTracks[0];
        albums.push({
            id: albumKey,
            name: first.album,
            artist: first.album_artist,
            track_count: albumTracks.length,
            genres: [first.genre],
            years: [first.year],
            year: first.year,
            release_type: (first.sample_rate_hz >= 96000) ? 'Album (Hi-Res)' : 'Album',
            favorite: hashCode('fav-album:' + albumKey) % 3 === 0,
            cover_source: 'folder',
            has_external_cover: true,
            coverUrl: demoImage('album:' + first.album),
            demo_cover_url: demoImage('album:' + first.album),
        });
    });

    const playlists = [
        { id: 'playlist_morning', name: 'Morning Coffee', track_ids: tracks.slice(0, 10).map(t => t.id) },
        { id: 'playlist_night', name: 'Late Night Coding', track_ids: tracks.slice(10, 25).map(t => t.id) },
        { id: 'playlist_road', name: 'Road Trip Anthems', track_ids: tracks.slice(30, 42).map(t => t.id) },
        { id: 'playlist_focus', name: 'Focus — Ambient Selection', track_ids: tracks.slice(35, 50).map(t => t.id) },
        { id: 'playlist_jazzblues', name: 'Jazz & Blues Evening', track_ids: tracks.filter(t => t.genre === 'Jazz' || t.genre === 'Blues').slice(0, 12).map(t => t.id) },
        { id: 'playlist_electronic', name: 'Electronic Frontiers', track_ids: tracks.filter(t => t.genre === 'Electronic').slice(0, 15).map(t => t.id) },
        { id: 'playlist_hires', name: 'Hi-Res Demo Sampler', track_ids: tracks.filter(t => (t.sample_rate_hz || 0) >= 92100).slice(0, 10).map(t => t.id) },
        { id: 'playlist_postrock', name: 'Post-Rock Landscapes', track_ids: tracks.filter(t => t.genre === 'Post-Rock').map(t => t.id) },
    ].map(pl => ({ ...pl, track_count: pl.track_ids.length }));

    // ---------------------------------------------------------------------
    // TIDAL catalog
    // ---------------------------------------------------------------------
    // Albums: [id, title, artistId, year, quality, tracks [title, duration]]
    const TIDAL_ARTISTS = [
        { id: 't_artist_01', name: 'Night Arcade', genres: ['Synthwave', 'Retrowave'] },
        { id: 't_artist_02', name: 'Cassiopeia', genres: ['Indie Pop'] },
        { id: 't_artist_03', name: 'Deep Field', genres: ['Ambient', 'Electronic'] },
        { id: 't_artist_04', name: 'The Lowlights', genres: ['Indie', 'Rock'] },
        { id: 't_artist_05', name: 'Night Bus', genres: ['Lo-Fi Beats'] },
        { id: 't_artist_06', name: 'Aether Drift', genres: ['Electronic', 'IDM'] },
        { id: 't_artist_07', name: 'Opal Vanguard', genres: ['Classical', 'Modern'] },
        { id: 't_artist_08', name: 'SubRoot System', genres: ['Dub', 'Reggae'] },
        { id: 't_artist_09', name: 'Velvet Static', genres: ['Synthwave'] },
        { id: 't_artist_10', name: 'Marina Vale', genres: ['Indie Pop'] },
        { id: 't_artist_11', name: 'The Bramble Trio', genres: ['Jazz'] },
        { id: 't_artist_12', name: 'Aurora Signal', genres: ['Electronic'] },
        { id: 't_artist_13', name: 'Fern Hollow', genres: ['Folk'] },
        { id: 't_artist_14', name: 'Kilo City Collective', genres: ['Hip-Hop'] },
        { id: 't_artist_15', name: 'Sierra Reyes', genres: ['Rock'] },
        { id: 't_artist_16', name: 'Nordkap', genres: ['Ambient'] },
        { id: 't_artist_17', name: 'Delta Hollis', genres: ['Blues'] },
        { id: 't_artist_18', name: 'Kestrel Wire', genres: ['Post-Rock'] },
        { id: 't_artist_19', name: 'Glass Tiger', genres: ['Indie', 'Alternative'] },
        { id: 't_artist_20', name: 'Waves of Amber', genres: ['Chillwave', 'Indie'] },
    ];
    TIDAL_ARTISTS.forEach((artist) => {
        artist.image_url = demoImage('tidal-artist:' + artist.id);
        artist.art_url = artist.image_url;
    });

    const TIDAL_ALBUM_SEEDS = [
        ['t_album_01', 'Neon Rain', 't_artist_09', 2023, 'HI_RES_LOSSLESS', ['Neon Rain', 214], ['Midnight Arcade', 189], ['Chrome Sunset', 243], ['Analog Dreams', 201], ['Glass Horizon', 232]],
        ['t_album_02', 'Harbor Lights', 't_artist_10', 2021, 'LOSSLESS', ['Paper Planes at Dawn', 197], ['Harbor Lights', 226], ['Salt & Cedar', 184], ['Slow Tide', 251], ['First Light Ferry', 208]],
        ['t_album_03', 'Old Town Sessions', 't_artist_11', 2020, 'LOSSLESS', ['Copper Kettle', 287], ['Rain on Cobblestone', 342], ['Lamplight Waltz', 265], ['Quicksilver', 228]],
        ['t_album_04', 'Gravity Well', 't_artist_12', 2024, 'HI_RES_LOSSLESS', ['Gravity Well', 264], ['Signal Lost', 239], ['Parallax Drift', 278], ['Ionosphere', 245], ['Redshift Lullaby', 296]],
        ['t_album_05', 'Woodland Sketches', 't_artist_13', 2018, 'LOSSLESS', ['Stone & Moss', 219], ['Riverbed', 194], ['Willow Bark', 236], ['Canopy Walk', 257]],
        ['t_album_06', 'Concrete Bloom', 't_artist_14', 2022, 'HIGH', ['Concrete Bloom', 212], ['Neon Alley Run', 198], ['Transit Maps', 224], ['Rooftop Antenna', 205], ['Static Bloom', 231]],
        ['t_album_07', 'Vantage Point', 't_artist_15', 2022, 'LOSSLESS', ['Vantage Point', 254], ['Switchback', 231], ['Treeline', 276], ['Summit Wind', 288]],
        ['t_album_08', 'Polar Circle', 't_artist_16', 2023, 'HI_RES_LOSSLESS', ['Blue Hour Drive', 324], ['Aurora Station', 367], ['Fjord Light', 295], ['Ice Fog', 412]],
        ['t_album_09', 'Delta Rain Hymns', 't_artist_17', 2019, 'LOSSLESS', ['Tin Roof Rhythm', 227], ['Levee Stomp', 243], ['Harmonica Hill', 199], ['Dust Bowl', 284]],
        ['t_album_10', 'Overpass', 't_artist_18', 2020, 'LOSSLESS', ['Threshold Lights', 426], ['Concrete Cathedral', 503], ['Signal Fire', 384], ['Subterranean Rivers', 517]],
        ['t_album_11', 'Crystal Circuit', 't_artist_03', 2023, 'LOSSLESS', ['Crystal Circuit', 224], ['Pixel Sunset', 198], ['Overdrive Hearts', 241], ['Neon Circuitry', 210]],
        ['t_album_12', 'Home Recordings', 't_artist_02', 2021, 'LOSSLESS', ['Tape Hiss Morning', 187], ['Kitchen Floor Disco', 203], ['Postcard from Nowhere', 216], ['Bluebird', 194]],
        ['t_album_13', 'Night Bus Diaries', 't_artist_05', 2020, 'HIGH', ['Night Bus Window Seat', 172], ['Rainy Platform 3', 158], ['Study Lamp Glow', 181], ['Last Stop', 190]],
        ['t_album_14', 'Aether Drift', 't_artist_06', 2018, 'HI_RES_LOSSLESS', ['Aether', 312], ['Flux Capacity', 287], ['Binary Sunrise', 265], ['Phase Shift', 341], ['Zero Crossing', 298]],
        ['t_album_15', 'Crimson Tides', 't_artist_07', 2020, 'LOSSLESS', ['Crimson Tide Rhapsody', 478], ['Adagio for Lost Satellites', 512], ['Elegy at Dusk', 436], ['Thunder March', 394]],
        ['t_album_16', 'Bass Garden', 't_artist_08', 2023, 'LOSSLESS', ['Sakura Blossom Dub', 302], ['Echo Chamber', 278], ['Roots Pressure', 315], ['Tape Delay Sunrise', 296]],
        ['t_album_17', 'Synthetic Love', 't_artist_09', 2022, 'LOSSLESS', ['Digital Hearts', 217], ['Synthetic Love', 245], ['Night Drive', 231], ['Moonshot', 198]],
        ['t_album_18', 'Marina VALE Sessions', 't_artist_10', 2019, 'LOSSLESS', ['Harbor Rain', 224], ['Solo Sailing', 251], ['Harbor Lights Again', 210], ['Low Tide', 186]],
        ['t_album_19', 'Tone Poems', 't_artist_19', 2023, 'HI_RES_LOSSLESS', ['Glass Morning', 301], ['Paper Tigers', 264], ['Arcade Nights', 289], ['Dust and Chrome', 318]],
        ['t_album_20', 'Waves of Gold', 't_artist_20', 2022, 'LOSSLESS', ['Golden Hour', 214], ['Coastline', 243], ['Summer of Something', 228], ['Waves of Us', 265]],
        ['t_album_21', 'Slow Motion Sunrise', 't_artist_02', 2024, 'LOSSLESS', ['Slow Motion Sunrise', 322], ['Analog Dreams', 301], ['Paper Planes (Night)', 244]],
        ['t_album_22', 'Dust Bowl Hymns', 't_artist_17', 2021, 'HIGH', ['Grain Elevator Blues', 297], ['Red Sky Morning', 260], ['Highway 61 Revisited', 342]],
    ];

    const tidalAlbums = TIDAL_ALBUM_SEEDS.map((seed) => {
        const [id, title, artistId, year, quality, ...trackSeeds] = seed;
        const artist = TIDAL_ARTISTS.find(a => a.id === artistId);
        const tracks = trackSeeds.map(([trackTitle, duration], idx) => ({
            id: id + '_t' + String(idx + 1),
            title: trackTitle,
            duration,
            trackNumber: idx + 1,
        }));
        const art = demoImage('tidal-album:' + id);
        return {
            id,
            title,
            artist: artist.name,
            artist_id: artistId,
            year,
            audio_quality: quality,
            num_tracks: tracks.length,
            duration: 400 + (year % 500),
            cover_url: art,
            art_url: art,
            tracks,
        };
    });
    // Tracks view: a curated ~20-track selection — the opener of each of the
    // twenty core albums — so the Tracks tab stays scannable while Albums
    // carries the full catalog.
    const tidalTracks = [];
    tidalAlbums.forEach((album) => {
        if (/^t_album_(0[1-9]|1[0-9]|20)$/.test(album.id) && album.tracks.length) {
            const t = album.tracks[0];
            tidalTracks.push({
                id: t.id,
                title: t.title,
                artist: album.artist,
                album: album.title,
                duration: t.duration,
                track_number: t.trackNumber,
                art_url: album.cover_url,
            });
        }
    });

    const TIDAL_PLAYLIST_SEEDS = [
        ['t_playlist_01', 'Late Night Drive', 'Synth-heavy night drives and neon horizons.', ['t_album_01', 't_album_11', 't_album_17']],
        ['t_playlist_02', 'Morning Focus', 'Slow-burn ambient and soft electronics for deep work.', ['t_album_08', 't_album_14', 't_album_05']],
        ['t_playlist_03', 'Chill Waves', 'Downtempo moods for lazy afternoons.', ['t_album_12', 't_album_13', 't_album_20']],
        ['t_playlist_04', 'Weekend Warmup', 'Upbeat openers for a slow weekend start.', ['t_album_02', 't_album_06', 't_album_19']],
        ['t_playlist_05', 'Deep Cuts', 'Echoes and edges from the label vaults.', ['t_album_14', 't_album_18', 't_album_09']],
        ['t_playlist_06', 'Cuerry Evenings', 'Jazz and warm blues for low light.', ['t_album_03', 't_album_09', 't_album_22']],
        ['t_playlist_07', 'Road Trip', 'Long-haul playlists with open roads in mind.', ['t_album_07', 't_album_06', 't_album_01']],
        ['t_playlist_08', 'Quiet Restaurant', 'Idle and ambient classics.', ['t_album_05', 't_album_08', 't_album_16']],
        ['t_playlist_09', 'New Artists', 'Fresh voices on the feed.', ['t_album_21', 't_album_04', 't_album_19']],
        ['t_playlist_10', 'Retro Future', 'Old synths, new bodies.', ['t_album_11', 't_album_17', 't_album_01']],
        ['t_playlist_11', 'Sunday Morning', 'Soft acoustic and folk.', ['t_album_05', 't_album_12', 't_album_02']],
        ['t_playlist_12', 'Deep House Carry', 'Home vibes, low thump.', ['t_album_16', 't_album_04', 't_album_14']],
        ['t_playlist_13', 'Hi-Res Showcase', 'Lossless showpieces for quiet rooms.', ['t_album_08', 't_album_14', 't_album_19']],
        ['t_playlist_14', 'Rainy Day Indie', 'Guitars under grey skies.', ['t_album_02', 't_album_12', 't_album_19']],
        ['t_playlist_15', 'Subwoofer Check', 'Low-end material for 2.2 rigs.', ['t_album_16', 't_album_06', 't_album_09']],
        ['t_playlist_16', 'Night Shift', 'After-midnight moods and hums.', ['t_album_13', 't_album_08', 't_album_11']],
        ['t_playlist_17', 'Golden Hour', 'Warm, slow, sunlit.', ['t_album_20', 't_album_18', 't_album_02']],
        ['t_playlist_18', 'Modern Classical', 'Strings meet electronics.', ['t_album_15', 't_album_07', 't_album_19']],
        ['t_playlist_19', 'Post-Rock Peaks', 'Long builds and loud quiets.', ['t_album_10', 't_album_18', 't_album_05']],
        ['t_playlist_20', 'Discovery Mix', 'First listens across the catalog.', ['t_album_21', 't_album_22', 't_album_03', 't_album_13']],
    ];
    const tidalPlaylists = TIDAL_PLAYLIST_SEEDS.map((seed, idx) => {
        const [id, name, description, albumIds] = seed;
        const tracks = albumIds.flatMap(aid => {
            const album = tidalAlbums.find(a => a.id === aid);
            return album ? album.tracks.map((t, index) => ({
                id: id + '_t' + index,
                title: t.title,
                artist: album.artist,
                album: album.title,
                duration: t.duration,
                track_number: index + 1,
                art_url: album.cover_url,
            })) : [];
        });
        return {
            id,
            name,
            description,
            track_count: tracks.length,
            art_url: demoImage('playlist:' + id),
            cover_url: demoImage('playlist:' + id),
            owner: 'fxroute-demo',
            is_public: true,
            tracks,
        };
    });

    window.FXROUTE_DEMO_LIBRARY = {
        demoImage,
        tracks,
        albums,
        playlists,
        tidalAlbums,
        tidalTracks,
        tidalArtists: TIDAL_ARTISTS,
        tidalPlaylists,
    };
})();