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

    // Deterministic favorites, baked at authoring time: every flag is the
    // value the favorites hash formula produced (tracks and provider
    // tracks % 4, albums % 3), stored as literal data so the fixture is
    // the single source of truth. scripts/test_library_fixture_favorites.js
    // pins these flags to the formula.
    const FAVORITES = {
      "fav-album:aether-drift": true,
      "fav-album:analog-dreams": false,
      "fav-album:bass-garden": true,
      "fav-album:concrete-bloom": false,
      "fav-album:cover-art": true,
      "fav-album:dust-bowl-hymns": false,
      "fav-album:gravity-well": true,
      "fav-album:harbor-lights": false,
      "fav-album:mississippi-notes": true,
      "fav-album:neon-rain": false,
      "fav-album:old-town-sessions": false,
      "fav-album:overpass": true,
      "fav-album:polar-circle": true,
      "fav-album:vantage-point": false,
      "fav-album:woodland-sketches": true,
      "fav-qobuz:Aurora Borealis": false,
      "fav-qobuz:Barometric": false,
      "fav-qobuz:Blue Hour": false,
      "fav-qobuz:Cyclogenesis": false,
      "fav-qobuz:Dernier Métro": false,
      "fav-qobuz:Foyer": false,
      "fav-qobuz:Midnight Glacier": false,
      "fav-qobuz:Polar Dawn": false,
      "fav-qobuz:Platform Soul": false,
      "fav-qobuz:Rainy Shinjuku": false,
      "fav-qobuz:Shibuya Rain": true,
      "fav-qobuz:Storm Cell": true,
      "fav-qobuz:String Theory": false,
      "fav-qobuz:Velvet Circuit": true,
      "fav-qobuz:Velvet Hours": false,
      "fav-spotify:Afterglow Drive": false,
      "fav-spotify:Analog Heart": true,
      "fav-spotify:Carousel Days": false,
      "fav-spotify:Electric Bloom": false,
      "fav-spotify:FM Static": false,
      "fav-spotify:Glass Horizon": false,
      "fav-spotify:Golden Gate Lights": false,
      "fav-spotify:Harbor Lights": false,
      "fav-spotify:Low Signal": false,
      "fav-spotify:Neon Static": false,
      "fav-spotify:Night Service": false,
      "fav-spotify:Paper Satellites": false,
      "fav-spotify:Platform Nine": false,
      "fav-spotify:Sandglass": false,
      "fav-spotify:Silver Linings": false,
      "fav-spotify:Tape Ghost": false,
      "fav-spotify:Tide Pools": false,
      "fav-spotify:Window Seat": false,
      "fav-track:local_demo_aether-drift_1": false,
      "fav-track:local_demo_aether-drift_2": false,
      "fav-track:local_demo_aether-drift_3": true,
      "fav-track:local_demo_aether-drift_4": false,
      "fav-track:local_demo_aether-drift_5": false,
      "fav-track:local_demo_analog-dreams_1": true,
      "fav-track:local_demo_bass-garden_1": false,
      "fav-track:local_demo_bass-garden_2": false,
      "fav-track:local_demo_bass-garden_3": true,
      "fav-track:local_demo_bass-garden_4": false,
      "fav-track:local_demo_concrete-bloom_1": true,
      "fav-track:local_demo_concrete-bloom_2": false,
      "fav-track:local_demo_concrete-bloom_3": false,
      "fav-track:local_demo_concrete-bloom_4": false,
      "fav-track:local_demo_concrete-bloom_5": true,
      "fav-track:local_demo_cover-art_1": true,
      "fav-track:local_demo_dust-bowl-hymns_1": true,
      "fav-track:local_demo_gravity-well_1": false,
      "fav-track:local_demo_gravity-well_2": false,
      "fav-track:local_demo_gravity-well_3": true,
      "fav-track:local_demo_gravity-well_4": false,
      "fav-track:local_demo_gravity-well_5": false,
      "fav-track:local_demo_harbor-lights_1": false,
      "fav-track:local_demo_harbor-lights_2": false,
      "fav-track:local_demo_harbor-lights_3": false,
      "fav-track:local_demo_harbor-lights_4": true,
      "fav-track:local_demo_harbor-lights_5": false,
      "fav-track:local_demo_mississippi-notes_1": true,
      "fav-track:local_demo_mississippi-notes_2": false,
      "fav-track:local_demo_mississippi-notes_3": false,
      "fav-track:local_demo_mississippi-notes_4": false,
      "fav-track:local_demo_mississippi-notes_5": true,
      "fav-track:local_demo_neon-rain_1": false,
      "fav-track:local_demo_neon-rain_2": false,
      "fav-track:local_demo_neon-rain_3": true,
      "fav-track:local_demo_neon-rain_4": false,
      "fav-track:local_demo_neon-rain_5": false,
      "fav-track:local_demo_old-town-sessions_1": false,
      "fav-track:local_demo_old-town-sessions_2": false,
      "fav-track:local_demo_old-town-sessions_3": false,
      "fav-track:local_demo_old-town-sessions_4": true,
      "fav-track:local_demo_old-town-sessions_5": false,
      "fav-track:local_demo_overpass_1": false,
      "fav-track:local_demo_overpass_2": false,
      "fav-track:local_demo_overpass_3": true,
      "fav-track:local_demo_overpass_4": false,
      "fav-track:local_demo_overpass_5": false,
      "fav-track:local_demo_polar-circle_1": true,
      "fav-track:local_demo_polar-circle_2": false,
      "fav-track:local_demo_polar-circle_3": false,
      "fav-track:local_demo_polar-circle_4": false,
      "fav-track:local_demo_polar-circle_5": true,
      "fav-track:local_demo_vantage-point_1": true,
      "fav-track:local_demo_vantage-point_2": false,
      "fav-track:local_demo_vantage-point_3": false,
      "fav-track:local_demo_vantage-point_4": false,
      "fav-track:local_demo_vantage-point_5": true,
      "fav-track:local_demo_woodland-sketches_1": false,
      "fav-track:local_demo_woodland-sketches_2": false,
      "fav-track:local_demo_woodland-sketches_3": true,
      "fav-track:local_demo_woodland-sketches_4": false,
      "fav-track:local_demo_woodland-sketches_5": false
    };

    // Deterministic, deduplicated cover assignment: every distinct key gets
    // its own slot in the artwork pool (round-robin per key namespace), so
    // albums/artists/playlists almost never share the same cover. Falls back
    // to even distribution once a namespace outgrows the pool.
    const keySlotCache = new Map();
    const namespaceCursors = new Map();
    const NAMESPACE_OFFSETS = {
        album: 0,
        'tidal-album': 7,
        'tidal-artist': 17,
        playlist: 27,
        'tidal-playlist': 33,
        artist: 40,
        // Streaming providers get their own artwork sequences so the Spotify
        // and Qobuz tabs never show the same covers.
        spotify: 3,
        qobuz: 21,
        track: 12,
    };

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
        // File names with spaces (e.g. "USER GUIDE.jpg") resolve on the
        // demo server and static hosts without percent-encoding; encoded
        // URLs 404 on the live demo route.
        return '/static/demo/' + IMG_KEYS[slot] + '.jpg';
    }

    function slug(text) {
        return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    }

    // ---------------------------------------------------------------------
    // Local library
    // ---------------------------------------------------------------------
    // [title, artist, album, genre, year, duration, sampleRate]
    const TRACK_SEEDS = [
        // Cover-art credit: the demo artwork pool is by jmort125; this entry
        // surfaces the credit in the library without a separate credit UI.
        ['Cover Art', 'jmort125', 'Cover Art', 'Electronic', 2024, 210, 48000],
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

    // ---------------------------------------------------------------------
    // About texts (demo stand-ins for the enriched MusicBrainz artist/album
    // descriptions). Short on purpose: the real UI renders them inside a
    // collapsible <details> element, so two sentences are enough to show
    // the open/close behavior. Keyed by artist name, with one album-level
    // override to exercise the "About this album" label branch.
    // ---------------------------------------------------------------------
    const ARTIST_ABOUT = {
        'Alistair Kade': 'Alistair Kade builds neon-lit synthwave around analog arpeggios and night-drive drums. The Neon Rain sessions were tracked live to tape with a wall of late-80s polysynths.',
        'Rhea Lindqvist': 'Rhea Lindqvist writes quiet indie pop about harbors, ferries and slow tides. Her band records in single takes in a wooden boathouse north of Gothenburg.',
        'The Bramble Trio': 'The Bramble Trio is a working jazz trio with two decades of club dates behind it. Old Town Sessions captures their late set: brushed drums, upright bass and unhurried piano.',
        'Iris Nakamura': 'Iris Nakamura makes dense, melodic electronic music from modular sketches and field recordings. Gravity Well layers shifting polyrhythms under wide-open synth chords.',
        'Fern Hollow': 'Fern Hollow is a folk project built on fingerpicked guitar and forest-floor percussion. Woodland Sketches was recorded in a cabin with the windows open.',
        'Kilo City Collective': 'Kilo City Collective is a rotating hip-hop crew trading verses over dusty loops. Concrete Bloom maps their city block by block, rooftop by rooftop.',
        'Sierra Reyes': 'Sierra Reyes plays high-altitude rock with room-sized guitars and close harmonies. Vantage Point was cut live on a mountain lodge porch at dusk.',
        'Nordkap': 'Nordkap composes slow ambient pieces from bowed strings, tape loops and shortwave static. Polar Circle follows a full Arctic night from blue hour to midnight sun.',
        'Delta Hollis': 'Delta Hollis carries the hill-country blues forward with stomp, slide and harmonica. Mississippi Notes collects juke-joint originals and one title-track confession.',
        'Aether Drift': 'Aether Drift explores the seam between IDM and stargazing synth music. The self-titled record pairs crisp machine rhythms with weightless pads.',
        'SubRoot System': 'SubRoot System runs a hand-built dub rig of tape echoes and spring reverbs. Bass Garden is four extended takes with the faders ridden live.',
        'Kestrel Wire': 'Kestrel Wire stretches post-rock guitars across long instrumental arcs. Overpass was recorded in a concrete underpass for its natural seven-second decay.',
        'Moru': 'Moru folds downtempo beats into hazy analog synth loops. Analog Dreams moves at walking pace through dusty drums and soft-focus chords.',
        'Nova Circuit': 'Nova Circuit pairs bright synthpop hooks with restless sequencers. Their live show runs the whole rig without a laptop in sight.',
        'Eiko Maru': 'Eiko Maru blends city pop grooves with modern funk guitar. Her songs switch between Japanese and English mid-chorus without slowing down.',
        'Marisol Vega': 'Marisol Vega sings direct indie pop with mariachi-brass accents. Her choruses are written for open windows and full-volume car stereos.',
        'Helios Strings': 'Helios Strings is a crossover ensemble mixing classical technique with film-score drama. Their readings favor slow builds and wide vibrato.',
    };
    const ALBUM_ABOUT = {
        // Album-level override: renders as "About this album" instead of
        // "About this artist", exactly like the real albumAboutHtml branch.
        'Old Town Sessions': 'Old Town Sessions documents one November evening at the trio\u2019s residency club: two sets, no overdubs, applause left in. The room\u2019s upright piano had not been tuned in a year, which the band calls its fourth member.',
    };
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
            favorite: FAVORITES['fav-track:' + id],
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
            favorite: FAVORITES['fav-album:' + albumKey],
            cover_source: 'folder',
            has_external_cover: true,
            coverUrl: demoImage('album:' + first.album),
            demo_cover_url: demoImage('album:' + first.album),
            // Same fields the enriched backend serves: the album detail
            // renders them as collapsible About blocks, no demo-only UI.
            ...(ARTIST_ABOUT[first.album_artist] ? { artist_description: ARTIST_ABOUT[first.album_artist] } : {}),
            ...(ALBUM_ABOUT[first.album] ? { album_description: ALBUM_ABOUT[first.album] } : {}),
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
        { id: 't_artist_21', name: 'Mirage Motel', genres: ['Synthwave'] },
        { id: 't_artist_22', name: 'Sable & Finch', genres: ['Indie Folk'] },
        { id: 't_artist_23', name: 'Cobalt Arcade', genres: ['Electro'] },
        { id: 't_artist_24', name: 'Velvet Hour', genres: ['Neo-Soul'] },
        { id: 't_artist_25', name: 'The Morning Line', genres: ['Alternative'] },
        { id: 't_artist_26', name: 'Iris Bloom', genres: ['Art Pop'] },
        { id: 't_artist_27', name: 'Static River', genres: ['Post-Punk'] },
        { id: 't_artist_28', name: 'Nova Format', genres: ['IDM'] },
        { id: 't_artist_29', name: 'Harbor Glass', genres: ['Chillwave'] },
        { id: 't_artist_30', name: 'Onyx Chapel', genres: ['Dark Ambient'] },
    ];
    TIDAL_ARTISTS.forEach((artist) => {
        artist.image_url = demoImage('tidal-artist:' + artist.id);
        artist.art_url = artist.image_url;
    });

    // ---------------------------------------------------------------------
    // TIDAL About + Discover Similar (static demo stand-ins for the shared
    // artist enrichment: MusicBrainz about text + ListenBrainz similar
    // artists). Short invented bios keyed by artist name; similar lists
    // reuse only artists that exist in this TIDAL catalog so every card
    // resolves to a real demo artist with stored art. Same-genre artists
    // first, then the rest in catalog order, max 6 — mirroring the real
    // renderSimilarArtists cap.
    // ---------------------------------------------------------------------
    const TIDAL_ARTIST_ABOUT = {
        'Night Arcade': 'Night Arcade channel neon-soaked synthwave through vintage drum machines and chorus-drenched guitars. Their records play like lost arcade soundtracks from a summer that never ended.',
        'Cassiopeia': 'Cassiopeia writes starry indie pop with fingerpicked guitars and double-tracked harmonies. Home Recordings collects four-track sketches polished just enough to shine.',
        'Deep Field': 'Deep Field maps deep space in sound: long ambient swells over faint electronic pulses. Crystal Circuit is their most melodic transmission yet.',
        'The Lowlights': 'The Lowlights play barroom indie rock with the lights turned low. Expect ringing Telecasters, close harmonies and choruses built for last call.',
        'Night Bus': 'Night Bus makes lo-fi beats for empty back seats and rainy windows. Night Bus Diaries loops dusty piano figures under soft vinyl crackle.',
        'Aether Drift': 'Aether Drift explores the seam between IDM and stargazing synth music. Intricate programming dissolves into weightless pads without warning.',
        'Opal Vanguard': 'Opal Vanguard drags classical forms into the present with electronics and heavy dynamics. Crimson Tides moves from whispered strings to full-orchestra thunder.',
        'SubRoot System': 'SubRoot System runs a hand-built dub rig of tape echoes and spring reverbs. Bass Garden is four extended takes with the faders ridden live.',
        'Velvet Static': 'Velvet Static layers hazy synths over driving retro beats. Synthetic Love is chrome-plated pop with the top down and the volume up.',
        'Marina Vale': 'Marina Vale sings salt-air indie pop about harbors and low tides. Her songs pair fingerpicked guitar with wide-open choruses.',
        'The Bramble Trio': 'The Bramble Trio is a working jazz trio with two decades of club dates behind it. Old Town Sessions captures their late set: brushed drums, upright bass and unhurried piano.',
        'Aurora Signal': 'Aurora Signal transmits melodic electronica from somewhere above the treeline. Gravity Well pairs shifting polyrhythms with wide-open synth chords.',
        'Fern Hollow': 'Fern Hollow is a folk project built on fingerpicked guitar and forest-floor percussion. Woodland Sketches was recorded in a cabin with the windows open.',
        'Kilo City Collective': 'Kilo City Collective is a rotating hip-hop crew trading verses over dusty loops. Concrete Bloom maps their city block by block, rooftop by rooftop.',
        'Sierra Reyes': 'Sierra Reyes plays high-altitude rock with room-sized guitars and close harmonies. Vantage Point was cut live on a mountain lodge porch at dusk.',
        'Nordkap': 'Nordkap composes slow ambient pieces from bowed strings, tape loops and shortwave static. Polar Circle follows a full Arctic night from blue hour to midnight sun.',
        'Delta Hollis': 'Delta Hollis carries the hill-country blues forward with stomp, slide and harmonica. Delta Rain Hymns collects juke-joint originals and slow storm songs.',
        'Kestrel Wire': 'Kestrel Wire stretches post-rock guitars across long instrumental arcs. Overpass was recorded in a concrete underpass for its natural seven-second decay.',
        'Glass Tiger': 'Glass Tiger sharpens indie guitars into chrome-edged alternative anthems. Tone Poems balances paper-dry verses against arena-sized choruses.',
        'Waves of Amber': 'Waves of Amber drift between chillwave haze and indie songcraft. Waves of Gold pairs golden-hour synths with saltwater harmonies.',
        'Mirage Motel': 'Mirage Motel checks into poolside synthwave: chrome palms, neon lobbies, endless summer nights. Every chorus arrives with the top down.',
        'Sable & Finch': 'Sable & Finch sing close-harmony indie folk for cedar porches and quiet rooms. Hollow & Home keeps the arrangements spare and the voices forward.',
        'Cobalt Arcade': 'Cobalt Arcade wires electro-funk basslines into pixel-bright synth hooks. Their records sound like a high score with a backbeat.',
        'Velvet Hour': 'Velvet Hour pours neo-soul slow jams for the last hour of the night. Silk Rhodes chords slide under unhurried, honeyed vocals.',
        'The Morning Line': 'The Morning Line writes commuter-belt alternative rock about platforms and departures. Big choruses, bigger hopes, first train out.',
        'Iris Bloom': 'Iris Bloom grows art pop like a greenhouse: strange, colorful and carefully tended. Petal Logic pairs odd meters with disarming melodies.',
        'Static River': 'Static River drags post-punk basslines through grey concrete landscapes. Static River is all tension: wire guitars over relentless drums.',
        'Nova Format': 'Nova Format programs precision IDM with a human pulse underneath. Nova Format flickers between signal drift and sudden melodic clarity.',
        'Harbor Glass': 'Harbor Glass bottles chillwave panoramas: tidewater synths, marina lights, slow-motion summers. Best played loud with the windows down.',
        'Onyx Chapel': 'Onyx Chapel holds dark-ambient services in imaginary stone chapels. Candle Frequency is organ drone, deep aisles and slow-burning air.',
    };
    function tidalSimilarFor(artistId) {
        const self = TIDAL_ARTISTS.find(a => a.id === artistId);
        if (!self) return [];
        const sameGenre = (a) => (a.genres || []).some(g => (self.genres || []).includes(g));
        return TIDAL_ARTISTS
            .filter(a => a.id !== artistId)
            .sort((a, b) => (sameGenre(a) ? 0 : 1) - (sameGenre(b) ? 0 : 1))
            .slice(0, 6)
            .map(a => ({ type: 'artist', artist: a.name, provider_artist_id: a.id, art_url: a.art_url || a.image_url || '' }));
    }

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
        ['t_album_23', 'Mirage Motel', 't_artist_21', 2023, 'HI_RES_LOSSLESS', ['Mirage Motel', 243], ['Chrome Palm', 218], ['Neon Lobby', 251], ['Pool Light', 197]],
        ['t_album_24', 'Hollow & Home', 't_artist_22', 2021, 'LOSSLESS', ['Hollow & Home', 226], ['Cedar Porch', 198], ['River Smoke', 244], ['Quiet Rooms', 231]],
        ['t_album_25', 'Cobalt Arcade', 't_artist_23', 2022, 'LOSSLESS', ['Cobalt Arcade', 217], ['Voltage Bloom', 204], ['Pixel Rain', 232], ['Arcade Sunset', 189]],
        ['t_album_26', 'Velvet Hour', 't_artist_24', 2023, 'LOSSLESS', ['Velvet Hour', 268], ['Silk Static', 245], ['Golden Last Call', 289]],
        ['t_album_27', 'The Morning Line', 't_artist_25', 2020, 'LOSSLESS', ['The Morning Line', 214], ['Half Past Dawn', 238], ['Platform Lights', 251], ['Departures', 227]],
        ['t_album_28', 'Iris Bloom', 't_artist_26', 2024, 'HI_RES_LOSSLESS', ['Iris Bloom', 233], ['Petal Logic', 209], ['Chromatic Garden', 276], ['Soft Focus', 244]],
        ['t_album_29', 'Static River', 't_artist_27', 2019, 'LOSSLESS', ['Static River', 254], ['Grey Bridge', 231], ['Concrete Current', 268]],
        ['t_album_30', 'Nova Format', 't_artist_28', 2023, 'HI_RES_LOSSLESS', ['Nova Format', 297], ['Signal Drift', 263], ['Lumen Field', 312], ['Zero Hour', 284]],
        ['t_album_31', 'Harbor Glass', 't_artist_29', 2021, 'LOSSLESS', ['Harbor Glass', 224], ['Tidewater Static', 208], ['Marina Blue', 241]],
        ['t_album_32', 'Onyx Chapel', 't_artist_30', 2022, 'LOSSLESS', ['Onyx Chapel', 346], ['Candle Frequency', 312], ['Deep Aisle', 374]],
        ['t_album_33', 'Glass Tiger', 't_artist_19', 2022, 'HI_RES_LOSSLESS', ['Neon Glass', 228], ['Window Seat', 215], ['Reflection Run', 243]],
        ['t_album_34', 'Waves of Amber (Live)', 't_artist_20', 2023, 'HI_RES_LOSSLESS', ['Golden Hour (Live)', 232], ['Coastline (Live)', 251], ['Summer of Something (Live)', 239]],
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
    // Tracks view: the opener of every album plus the second track of the
    // first half of the catalog — a curated ~48-track selection so the Tracks
    // tab feels like a real library while Albums carries the full catalog.
    const tidalTracks = [];
    tidalAlbums.forEach((album, albumIndex) => {
        if (!album.tracks.length) return;
        const picks = albumIndex < 14 ? Math.min(2, album.tracks.length) : 1;
        for (let index = 0; index < picks; index += 1) {
            const t = album.tracks[index];
            tidalTracks.push({
                id: t.id,
                title: t.title,
                artist: album.artist,
                album: album.title,
                duration: t.duration,
                track_number: t.trackNumber,
                // Per-track TIDAL quality tier (HI_RES_LOSSLESS / LOSSLESS /
                // HIGH): the playback state derives the footer stream facts
                // (codec/bit depth/rate) from it per track.
                audio_quality: album.audio_quality,
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

    // ---------------------------------------------------------------------
    // Streaming provider catalogs (Spotify / Qobuz)
    // ---------------------------------------------------------------------
    // Each provider gets its own small queue drawn from distinct albums and
    // artists, so switching tracks changes title, artist, album and cover
    // together instead of rotating one album's tracks. Covers use the
    // provider namespace so the same title never reuses the local art.
    function buildProviderTracks(provider, seeds) {
        return seeds.map((seed, index) => {
            const [title, artist, album, genre, year, duration, sampleRate, bitDepth] = seed;
            const art = demoImage(provider + ':' + title);
            return {
                id: 'demo_' + provider + '_' + (index + 1),
                title,
                artist,
                album,
                album_artist: artist,
                genre,
                year,
                duration,
                sample_rate_hz: sampleRate || 44100,
                bit_depth: bitDepth || null,
                source: provider,
                url: 'file://demo/' + provider + '/' + title + '.flac',
                cover_url: art,
                art_url: art,
                cover_available: true,
                artwork_available: true,
                artwork_url: art,
                artwork_source: provider,
                favorite: FAVORITES['fav-' + provider + ':' + title],
            };
        });
    }

    const SPOTIFY_TRACK_SEEDS = [
        ['Electric Bloom', 'Nova Circuit', 'Electric Bloom', 'Synthpop', 2024, 231, 48000],
        ['Night Service', 'Nova Circuit', 'Electric Bloom', 'Synthpop', 2024, 204, 48000],
        ['Neon Static', 'Nova Circuit', 'Electric Bloom', 'Synthpop', 2024, 218, 48000],
        ['Glass Horizon', 'Nova Circuit', 'Electric Bloom', 'Synthpop', 2024, 247, 48000],
        ['Afterglow Drive', 'Nova Circuit', 'Electric Bloom', 'Synthpop', 2024, 225, 48000],
        ['Paper Satellites', 'Marisol Vega', 'Orbit Hours', 'Indie Pop', 2023, 198, 44100],
        ['Golden Gate Lights', 'Marisol Vega', 'Orbit Hours', 'Indie Pop', 2023, 226, 44100],
        ['Silver Linings', 'Marisol Vega', 'Orbit Hours', 'Indie Pop', 2023, 211, 44100],
        ['Carousel Days', 'Marisol Vega', 'Orbit Hours', 'Indie Pop', 2023, 194, 44100],
        ['Window Seat', 'The Amber Rooks', 'Midnight Commute', 'Indie Rock', 2022, 203, 44100],
        ['Platform Nine', 'The Amber Rooks', 'Midnight Commute', 'Indie Rock', 2022, 232, 44100],
        ['Low Signal', 'The Amber Rooks', 'Midnight Commute', 'Indie Rock', 2022, 189, 44100],
        ['Tide Pools', 'Dune Harbor', 'Saltwater Static', 'Dream Pop', 2021, 254, 48000],
        ['Sandglass', 'Dune Harbor', 'Saltwater Static', 'Dream Pop', 2021, 217, 48000],
        ['Harbor Lights', 'Dune Harbor', 'Saltwater Static', 'Dream Pop', 2021, 241, 48000],
        ['Analog Heart', 'Kai & The Frequencies', 'Analog Hearts', 'Alternative', 2020, 226, 44100],
        ['FM Static', 'Kai & The Frequencies', 'Analog Hearts', 'Alternative', 2020, 208, 44100],
        ['Tape Ghost', 'Kai & The Frequencies', 'Analog Hearts', 'Alternative', 2020, 235, 44100],
    ];
    // Qobuz streams FLAC at every quality tier; the trailing bit depth
    // mirrors the real provider payload (16-bit CD quality, 24-bit hi-res).
    const QOBUZ_TRACK_SEEDS = [
        ['Aurora Borealis', 'Helios Strings', 'Northern Skies', 'Classical Crossover', 2022, 312, 96000, 24],
        ['Midnight Glacier', 'Helios Strings', 'Northern Skies', 'Classical Crossover', 2022, 287, 96000, 24],
        ['Polar Dawn', 'Helios Strings', 'Northern Skies', 'Classical Crossover', 2022, 296, 96000, 24],
        ['Velvet Circuit', 'Eiko Maru', 'Neon Kaidan', 'City Pop', 2021, 243, 44100, 16],
        ['Rainy Shinjuku', 'Eiko Maru', 'Neon Kaidan', 'City Pop', 2021, 265, 44100, 16],
        ['Shibuya Rain', 'Eiko Maru', 'Neon Kaidan', 'City Pop', 2021, 252, 44100, 16],
        ['String Theory', 'Marta Voss', 'Kammersaal', 'Chamber Jazz', 2023, 284, 96000, 24],
        ['Blue Hour', 'Marta Voss', 'Kammersaal', 'Chamber Jazz', 2023, 267, 96000, 24],
        ['Foyer', 'Marta Voss', 'Kammersaal', 'Chamber Jazz', 2023, 239, 96000, 24],
        ['Cyclogenesis', 'Ironwave', 'Heavy Weather', 'Progressive Metal', 2020, 301, 96000, 24],
        ['Barometric', 'Ironwave', 'Heavy Weather', 'Progressive Metal', 2020, 276, 96000, 24],
        ['Storm Cell', 'Ironwave', 'Heavy Weather', 'Progressive Metal', 2020, 322, 96000, 24],
        ['Dernier Métro', 'Theo Laurent', 'Night Trains', 'Neo Soul', 2022, 248, 48000, 24],
        ['Platform Soul', 'Theo Laurent', 'Night Trains', 'Neo Soul', 2022, 226, 48000, 24],
        ['Velvet Hours', 'Theo Laurent', 'Night Trains', 'Neo Soul', 2022, 233, 48000, 24],
    ];
    const spotifyTracks = buildProviderTracks('spotify', SPOTIFY_TRACK_SEEDS);
    const qobuzTracks = buildProviderTracks('qobuz', QOBUZ_TRACK_SEEDS);

    window.FXROUTE_DEMO_LIBRARY = {
        demoImage,
        tracks,
        albums,
        playlists,
        tidalAlbums,
        tidalTracks,
        tidalArtists: TIDAL_ARTISTS,
        tidalPlaylists,
        tidalArtistAbout: TIDAL_ARTIST_ABOUT,
        tidalSimilarFor,
        spotifyTracks,
        qobuzTracks,
    };
})();