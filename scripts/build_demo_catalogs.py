#!/usr/bin/env python3
"""Generate the web-demo music catalog fixtures from scripts/demo_covers.py.

Single source of truth for all demo music content: local library, TIDAL,
Spotify, Qobuz (demo/data/library.js) and the two SMB shares
(demo/data/library2.js = SMB_Demo_Library-1, demo/data/library3.js =
SMB_Demo_Library-2). Covers are copied from the web-optimized pool
(~/ai/projects/fxroute-newdemopics-web) into static/demo/ under
per-library names; stale pool files are removed.

Everything is deterministic: same manifest always yields the same fixtures
(track counts 8-14 per album, titles, durations, favorites). Favorites
mirror the main catalog's hash formula and are baked as literals (see
scripts/test_library_fixture_favorites.js).

Usage:
  python3 scripts/build_demo_catalogs.py [--check]
  --check: verify the committed fixtures match a fresh render (no writes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from demo_covers import COVERS, LIB_PREFIX, SMOKE_AND_SATIN_TRACKS
from demo_about import ARTIST_ABOUT, ALBUM_ABOUT

ROOT = Path(__file__).resolve().parents[1]
WEB_SRC = Path.home() / "ai" / "projects" / "fxroute-newdemopics-web"
POOL_DIR = ROOT / "static" / "demo"
DATA_DIR = ROOT / "demo" / "data"

SAMPLE_RATES = [44100, 44100, 48000, 48000, 88200, 96000]

TITLE_WORDS: dict[str, list[str]] = {
    "Synthwave": ["Midnight", "Neon", "Chrome", "Ultraviolet", "Afterglow", "Nightdrive", "Laser", "Mirage", "Overdrive", "Nightcall", "Glasshouse", "Runaway", "Skyline", "Relay", "Palm", "Static", "Voltage", "Sunset", "Horizon", "Interceptor", "Cassette", "Dayglow"],
    "Synthpop": ["Neon", "Weekend", "Dancer", "Ultraviolet", "Daydream", "Spotlight", "Mirror", "Electric", "Parade", "Sugar", "Velvet", "Rhythm", "Heartbeat", "Glitter", "Afterhours", "Cassette", "Dayglo", "Moonlight", "Fever", "Arcade", "Stereo", "Confetti"],
    "Indie Rock": ["Static", "Golden", "Highway", "Thunder", "Daybreak", "Copper", "Wildfire", "Gravel", "Sundown", "Radio", "Comet", "Dust", "Skyline", "Reverb", "Tide", "Avalanche", "Neon", "Backroad", "Storm", "Daydream", "Voltage", "Horizon"],
    "Americana": ["Copper", "Dustline", "Highway", "Mesquite", "Redrock", "Tailwind", "Boxcar", "Sundown", "Riverbed", "Coyote", "Badlands", "Whiskey", "Depot", "Sagebrush", "Freight", "Canyon", "Diner", "Thunderhead", "Mesa", "Lonesome", "Gravel", "Skyfire"],
    "Funk": ["Honey", "Groove", "Boogie", "Sugar", "Fever", "Strut", "Pepper", "Lightning", "Velvet", "Jive", "Copper", "Shake", "Sass", "Glide", "Rumpus", "Sizzle", "Swagger", "Bump", "Fuzz", "Snap", "Slink", "Pop"],
    "Electronic": ["Vector", "Pulse", "Lattice", "Modular", "Circuit", "Signal", "Chrome", "Drift", "Helix", "Zero", "Prism", "Current", "Engine", "Waveform", "Ion", "Flux", "Pixel", "Nova", "Grid", "Relay", "Cipher", "Monochrome"],
    "Ambient": ["Orbit", "Bloom", "Tide", "Halo", "Drift", "Stillness", "Meridian", "Hollow", "Auric", "Dusklight", "Field", "Weightless", "Undertow", "Slowtide", "Overcast", "Ember", "Glasssea", "Nightwater", "Pale", "Sanctuary", "Lowlight", "Air"],
    "Jazz": ["Afterhours", "Blue", "Moon", "Satin", "Alley", "Nocturne", "Brass", "Velvet", "Rain", "Uptown", "Mellow", "Sidestreet", "Lamplight", "Evergreen", "Secondline", "Ballad", "Smokering", "Downbeat", "Starlight", "Ember", "Riverwalk", "Nightcap"],
    "Vocal Jazz": ["Satin", "Moon", "Velvet", "Whisper", "Candle", "Ember", "Lullaby", "Stardust", "Nocturne", "Blue", "Honey", "Afterglow", "Serenade", "Twilight", "Starlight", "Dreamer", "Smoke", "Midnight", "Paisley", "Amber", "Nightingale"],
    "Pop": ["Radio", "Color", "Parade", "Dayglo", "Confetti", "Stereo", "Bubble", "Rocket", "Cherry", "Static", "Glitter", "Sunroof", "Postcard", "Lollipop", "Firework", "Carousel", "Neon", "Daydream", "Jukebox", "Marquee", "Polaroid", "Highfive"],
    "Soul": ["Velvet", "Honey", "Slow", "Devotion", "Mercy", "Sunday", "Glow", "Tender", "Truth", "Healing", "Amber", "Silk", "Sermon", "Candle", "Harbor", "Lullaby", "Golden", "Ember", "Satin", "Hymn", "Balm", "Reverie"],
    "Hip-Hop": ["Cipher", "Crown", "Concrete", "Block", "Rhymes", "Empire", "Royalty", "Boombox", "Mic", "Beats", "Corner", "Flow", "Hustle", "Legacy", "Street", "Vinyl", "Asphalt", "Dynasty", "Boom", "Tape", "Bronx", "Pressure"],
    "House": ["Groove", "Sunset", "Deep", "Jack", "Piano", "Warehouse", "Divas", "Loop", "Midnight", "Terrace", "Bassline", "Unity", "Vibe", "Chicago", "Sundown", "Rise", "Together", "Feel", "Move", "Bright", "Day", "Soulful"],
    "Lo-Fi": ["Coffee", "Rain", "Window", "Tape", "Dust", "Evening", "Notebook", "Lamp", "Static", "Pillow", "Fern", "Cat", "Attic", "Mug", "Sidewalk", "Juniper", "Paper", "Couch", "Drizzle", "Basement", "Polaroid", "Socks"],
    "Chillout": ["Horizon", "Drift", "Sunset", "Breeze", "Tide", "Palm", "Golden", "Mellow", "Lagoon", "Dusk", "Coral", "Sail", "Tropic", "Hammock", "Azure", "Coconut", "Shore", "Lazy", "Warm", "Gentle", "Calm", "Sunday"],
    "Garage": ["Bass", "Dark", "Night", "Shuffle", "Sub", "Reload", "Midnight", "Pressure", "Vocal", "Chop", "Rinse", "Concrete", "Stepper", "Wobble", "Underground", "Locked", "Roller", "Deep", "Siren", "Echo", "Duppy", "Bloom"],
    "Folk": ["Willow", "River", "Harvest", "Meadow", "Pine", "Sparrow", "Cedar", "Moonrise", "Gravel", "Orchard", "Driftwood", "Clover", "Brook", "Thistle", "Ember", "Fogline", "Alder", "Bramble", "Wren", "Lantern", "Field", "Acorn"],
    "Rock": ["Thunder", "Highway", "Neon", "Gasoline", "Riot", "Velvet", "Static", "Engine", "Leather", "Fever", "Rebel", "Amplifier", "Dust", "Storm", "Chrome", "Wildfire", "Midnight", "Voltage", "Comet", "Avenue", "Sparks", "Ignition"],
}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def hash_code(text: str) -> int:
    """Mirror the demo's JS favorites hash (java-style 32-bit)."""
    h = 0
    for ch in str(text):
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h & 0x7FFFFFFF


def track_count_for(album: str) -> int:
    return 8 + hashlib.sha256(album.encode("utf-8")).digest()[0] % 7


def track_titles(genre: str, album: str, count: int, used: set[str] | None = None) -> list[str]:
    if album == "Smoke & Satin":
        titles = list(SMOKE_AND_SATIN_TRACKS)
    else:
        titles = []
    words = sorted(set(TITLE_WORDS.get(genre, TITLE_WORDS["Electronic"])))
    rng = random.Random(f"fxroute-demo:{album}")
    seen = set(titles)
    if used is not None:
        seen |= used
    use_counts: dict[str, int] = {}
    guard = 0
    while len(titles) < count and guard < 20000:
        guard += 1
        least = min(use_counts.get(w, 0) for w in words)
        first = rng.choice(sorted(w for w in words if use_counts.get(w, 0) == least))
        use_counts[first] = use_counts.get(first, 0) + 1
        rest = [w for w in words if w != first]
        least2 = min(use_counts.get(w, 0) for w in rest)
        second = rng.choice(sorted(w for w in rest if use_counts.get(w, 0) == least2))
        use_counts[second] = use_counts.get(second, 0) + 1
        title = f"{first} {second}"
        if title in seen:
            continue
        seen.add(title)
        titles.append(title)
    if used is not None:
        used.update(titles)
    return titles[:count]


def track_duration(album: str, title: str) -> int:
    """Realistic display duration, 150-420 s, deterministic per track."""
    return 150 + hash_code(f"dur:{album}::{title}") % 271


def album_sample_rate(album: str) -> int:
    return SAMPLE_RATES[hash_code(f"sr:{album}") % len(SAMPLE_RATES)]


def cover_name(library: str, artist: str, album: str) -> str:
    base = slug(album) if artist.lower() == album.lower() else slug(f"{artist}-{album}")
    return f"{LIB_PREFIX[library]}-{base}.jpg"


def build_album_entries() -> list[dict]:
    entries = []
    used_titles: set[str] = set()
    for src, library, artist, album, genre, year, extra in COVERS:
        count = 3 if library in ("spotify", "qobuz") else track_count_for(album)
        titles = track_titles(genre, album, count, used_titles)
        tracks = [{"title": t, "duration": track_duration(album, t)} for t in titles]
        entries.append({
            "src": src, "library": library, "artist": artist, "album": album,
            "genre": genre, "year": year, "extra": extra,
            "cover": cover_name(library, artist, album),
            "sample_rate_hz": (extra[0] if library in ("spotify", "qobuz") else album_sample_rate(album)),
            "bit_depth": (extra[1] if library == "qobuz" else None),
            "tracks": tracks,
        })
    return entries


SMB_PREFIX = {"smb1": "d2-", "smb2": "d3-"}


def favorite_flags(entries: list[dict]) -> dict[str, bool]:
    """Baked favorites in the catalog key schemes.

    Main catalog (local/providers): fav-album:/fav-track:/fav-spotify:/
    fav-qobuz: keys verified by test_library_fixture_favorites.js.
    SMB shares: album:/track: keys with the share id prefix, verified by
    the catalog determinism test.
    """
    flags: dict[str, bool] = {}
    for e in entries:
        if e["library"] == "local":
            key = slug(e["album"])
            flags[f"fav-album:{key}"] = hash_code("fav-album:" + key) % 3 == 0
            for i in range(1, len(e["tracks"]) + 1):
                tid = f"local_demo_{key}_{i}"
                flags[f"fav-track:{tid}"] = hash_code("fav-track:" + tid) % 4 == 0
        elif e["library"] in ("spotify", "qobuz"):
            for t in e["tracks"]:
                flags[f"fav-{e['library']}:{t['title']}"] = (
                    hash_code(f"fav-{e['library']}:" + t["title"]) % 4 == 0)
        elif e["library"] in SMB_PREFIX:
            prefix = SMB_PREFIX[e["library"]]
            akey = prefix + slug(e["album"])
            flags[f"album:{akey}"] = hash_code("fav-album:" + akey) % 3 == 0
            for i in range(1, len(e["tracks"]) + 1):
                tid = f"{akey}_{i:02d}"
                flags[f"track:{tid}"] = hash_code("fav-track:" + tid) % 4 == 0
    return flags


# ---------------------------------------------------------------- fixtures

def _pct(template: str) -> str:
    """Escape literal % for %-formatting, keeping %s placeholders."""
    return template.replace("%s", "\x00").replace("%", "%%").replace("\x00", "%s")


LIBRARY_JS_TEMPLATE = _pct("""// Demo music catalog fixtures: local library, TIDAL, Spotify, Qobuz.
// Generated by scripts/build_demo_catalogs.py — do not hand-edit.
// Covers are explicit per-album files in static/demo/ (see demo_covers.py);
// demoImage() remains as the fallback pool for user-created playlists and
// cover redirects.
(function () {
    'use strict';

    const IMG_KEYS = %s;

    // Deterministic, deduplicated cover fallback: every distinct key gets
    // its own slot in the artwork pool (round-robin per key namespace).
    const keySlotCache = new Map();
    const namespaceCursors = new Map();
    const NAMESPACE_OFFSETS = {
        album: 0,
        'tidal-album': 7,
        'tidal-artist': 17,
        playlist: 27,
        'tidal-playlist': 33,
        artist: 40,
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
        return '/static/demo/' + IMG_KEYS[slot] + '.jpg';
    }

    function slug(text) {
        return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    }

    // Deterministic favorites, baked at generation time (see
    // scripts/test_library_fixture_favorites.js).
    const FAVORITES = %s;

    // ---------------------------------------------------------------------
    // Local library: [title, artist, album, genre, year, duration,
    // sampleRate, poolImage]
    // ---------------------------------------------------------------------
    const TRACK_SEEDS = %s;

    // ---------------------------------------------------------------------
    // About texts (demo stand-ins for the enriched MusicBrainz artist/album
    // descriptions). Short on purpose: the real UI renders them inside a
    // collapsible <details> element. Keyed by artist name, with album-level
    // overrides to exercise the "About this album" label branch.
    // ---------------------------------------------------------------------
    const ARTIST_ABOUT = %s;
    const ALBUM_ABOUT = %s;
    const tracks = [];
    const albums = [];
    const byAlbum = new Map();
    TRACK_SEEDS.forEach((seed) => {
        const [title, artist, album, genre, year, duration, sampleRate, poolImage] = seed;
        const albumKey = slug(album);
        const existing = byAlbum.get(albumKey) || { count: 0 };
        existing.count += 1;
        byAlbum.set(albumKey, existing);
        const id = `local_demo_${albumKey}_${existing.count}`;
        const cover = '/static/demo/' + poolImage;
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
            coverUrl: first.cover_url,
            demo_cover_url: first.cover_url,
            // Same fields the enriched backend serves: the album detail
            // renders them as collapsible About blocks, no demo-only UI.
            ...(ARTIST_ABOUT[first.album_artist] ? { artist_description: ARTIST_ABOUT[first.album_artist] } : {}),
            ...(ALBUM_ABOUT[first.album] ? { album_description: ALBUM_ABOUT[first.album] } : {}),
        });
    });

    const LOCAL_PLAYLISTS = %s;
    const playlists = LOCAL_PLAYLISTS.map(pl => ({ ...pl, track_count: pl.track_ids.length }));

    // ---------------------------------------------------------------------
    // TIDAL catalog
    // ---------------------------------------------------------------------
    // Albums: [id, title, artistId, year, quality, cover, tracks [title, duration]]
    const TIDAL_ARTISTS = %s;
    TIDAL_ARTISTS.forEach((artist) => {
        artist.image_url = artist.art_url;
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
    const TIDAL_ARTIST_ABOUT = %s;
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

    const TIDAL_ALBUM_SEEDS = %s;

    const tidalAlbums = TIDAL_ALBUM_SEEDS.map((seed) => {
        const [id, title, artistId, year, quality, cover, ...trackSeeds] = seed;
        const artist = TIDAL_ARTISTS.find(a => a.id === artistId);
        const tracks = trackSeeds.map(([trackTitle, duration], idx) => ({
            id: id + '_t' + String(idx + 1),
            title: trackTitle,
            duration,
            trackNumber: idx + 1,
        }));
        const art = '/static/demo/' + cover;
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
    // first half of the catalog — a curated selection so the Tracks tab
    // feels like a real library while Albums carries the full catalog.
    const tidalTracks = [];
    tidalAlbums.forEach((album, albumIndex) => {
        if (!album.tracks.length) return;
        const picks = albumIndex < Math.ceil(tidalAlbums.length / 2) ? Math.min(2, album.tracks.length) : 1;
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

    const TIDAL_PLAYLIST_SEEDS = %s;
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
        // Playlist cover mirrors the local collage: a pre-rendered montage
        // of the member album covers (see build_demo_catalogs.py), not a
        // random pool image.
        const montage = '/static/demo/tpl-' + id + '.jpg';
        return {
            id,
            name,
            description,
            track_count: tracks.length,
            art_url: montage,
            cover_url: montage,
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
    // together instead of rotating one album's tracks.
    function buildProviderTracks(provider, seeds) {
        return seeds.map((seed, index) => {
            const [title, artist, album, genre, year, duration, sampleRate, bitDepth, poolImage] = seed;
            const art = '/static/demo/' + poolImage;
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

    const SPOTIFY_TRACK_SEEDS = %s;
    // Qobuz streams FLAC at every quality tier; the trailing bit depth
    // mirrors the real provider payload (16-bit CD quality, 24-bit hi-res).
    const QOBUZ_TRACK_SEEDS = %s;
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
""")

SMB_JS_TEMPLATE = _pct("""// %s catalog fixture (generated by scripts/build_demo_catalogs.py --webdemo-js).
// Same shape as the local demo catalog in data/library.js: fictional artists,
// generated track titles, explicit per-album covers from static/demo/.
// Do not hand-edit; regenerate instead.
(function () {
    'use strict';

    function slug(text) {
        return String(text || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    }

    const SEEDS = %s;
    const PLAYLISTS = %s;
    // Deterministic favorites, computed in Python (favorite_flags) so the
    // fixture carries the flags instead of re-deriving them at page load.
    const FAVORITES = %s;
    // About texts (demo stand-ins for the enriched MusicBrainz artist/album
    // descriptions), same contract as the main catalog: the album detail
    // renders them as collapsible About blocks.
    const ARTIST_ABOUT = %s;
    const ALBUM_ABOUT = %s;

    const tracks = [];
    const albums = [];
    const ID_PREFIX = '%s';
    SEEDS.forEach((seed) => {
        const [artist, album, genre, year, poolImage, sampleRate, trackSeeds] = seed;
        const albumKey = ID_PREFIX + slug(album);
        const cover = '/static/demo/' + poolImage;
        trackSeeds.forEach(([title, duration], index) => {
            const id = albumKey + '_' + String(index + 1).padStart(2, '0');
            tracks.push({
                id,
                title,
                artist,
                album,
                album_artist: artist,
                genre,
                year,
                track_number: index + 1,
                disc_number: 1,
                source: 'local',
                url: 'file://%s/' + albumKey + '/' + String(index + 1).padStart(2, '0') + ' - ' + title + '.flac',
                duration,
                path: '%s/' + album + '/' + String(index + 1).padStart(2, '0') + ' - ' + title + '.flac',
                sample_rate_hz: sampleRate,
                cover_available: true,
                cover_url: cover,
                cover_info_url: '/api/tracks/cover-info/' + id,
                artwork_available: true,
                artwork_url: cover,
                artwork_source: 'library',
                favorite: FAVORITES['track:' + id],
            });
        });
        albums.push({
            id: albumKey,
            name: album,
            artist,
            track_count: trackSeeds.length,
            genres: [genre],
            years: [year],
            year,
            release_type: (sampleRate >= 96000) ? 'Album (Hi-Res)' : 'Album',
            favorite: FAVORITES['album:' + albumKey],
            cover_source: 'folder',
            has_external_cover: false,
            coverUrl: cover,
            demo_cover_url: cover,
            // Same fields the enriched backend serves: the album detail
            // renders them as collapsible About blocks, no demo-only UI.
            ...(ARTIST_ABOUT[artist] ? { artist_description: ARTIST_ABOUT[artist] } : {}),
            ...(ALBUM_ABOUT[album] ? { album_description: ALBUM_ABOUT[album] } : {}),
        });
    });

    const playlists = PLAYLISTS.map((entry) => ({ ...entry }));

    window.%s = { tracks, albums, playlists };
})();
""")


def tidal_playlist_seed_list(n_albums: int) -> list[list]:
    aids = [f"t_album_{i:02d}" for i in range(1, n_albums + 1)]
    return [
        ["t_playlist_01", "Late Night Drive", "Synth-heavy night drives and neon horizons.", aids[2:5] + aids[11:12]],
        ["t_playlist_02", "Golden Hour", "Warm, slow, sunlit.", aids[12:16] + aids[1:2]],
        ["t_playlist_03", "Corner Classics", "Bars, loops and legacy.", aids[4:5] + aids[6:11]],
        ["t_playlist_04", "Quiet Storm", "Slow jams and softer lights.", aids[:2] + aids[16:18]],
        ["t_playlist_05", "Neon Signals", "Old synths, new bodies.", aids[2:4] + aids[5:6] + aids[11:12]],
        ["t_playlist_06", "Bass Pressure", "Low-end material for 2.2 rigs.", aids[5:6] + aids[4:5] + aids[9:10]],
        ["t_playlist_07", "Sunday Blend", "Coffee, vinyl and daylight.", aids[:2] + aids[16:18]],
        ["t_playlist_08", "Discovery Mix", "First listens across the catalog.", aids[6:7] + aids[12:13] + aids[0:1] + aids[4:5]],
    ]


def render_library_js(entries: list[dict]) -> str:
    by_lib = {}
    for e in entries:
        by_lib.setdefault(e["library"], []).append(e)
    local = by_lib["local"]
    tidal = by_lib["tidal"]
    spotify = by_lib["spotify"]
    qobuz = by_lib["qobuz"]

    track_seeds = [
        [t["title"], e["artist"], e["album"], e["genre"], e["year"],
         t["duration"], e["sample_rate_hz"], e["cover"]]
        for e in local for t in e["tracks"]
    ]
    track_ids = [f"local_demo_{slug(e['album'])}_{i + 1}" for e in local for i, _ in enumerate(e["tracks"])]
    n = len(track_ids)
    local_playlists = [
        {"id": "playlist_morning", "name": "Morning Coffee", "track_ids": track_ids[:min(12, n)]},
        {"id": "playlist_night", "name": "Late Night Coding", "track_ids": track_ids[min(12, n):min(30, n)] or track_ids[:8]},
        {"id": "playlist_grooves", "name": "Grooves & Voices", "track_ids": [tid for tid, e in zip(track_ids, [e for e in local for _ in e["tracks"]]) if e["genre"] in ("Funk", "Soul", "Hip-Hop", "Jazz")][:14]},
        {"id": "playlist_electronic", "name": "Electronic Frontiers", "track_ids": [tid for tid, e in zip(track_ids, [e for e in local for _ in e["tracks"]]) if e["genre"] in ("Electronic", "House", "Garage", "Chillout", "Lo-Fi", "Ambient")][:16]},
        {"id": "playlist_hires", "name": "Hi-Res Sampler", "track_ids": [tid for tid, e in zip(track_ids, [e for e in local for _ in e["tracks"]]) if e["sample_rate_hz"] >= 88200][:10] or track_ids[:6]},
        {"id": "playlist_sun", "name": "Sun & Streets", "track_ids": [tid for tid, e in zip(track_ids, [e for e in local for _ in e["tracks"]]) if e["genre"] in ("Pop", "Americana", "Hip-Hop")][:12]},
    ]

    tidal_artists = []
    for i, e in enumerate(tidal, start=1):
        genres = [e["genre"]]
        if e["genre"] in ("Hip-Hop", "Pop"):
            genres = [e["genre"], "Electronic"] if e["genre"] == "Pop" else [e["genre"], "Soul"]
        tidal_artists.append({
            "id": f"t_artist_{i:02d}", "name": e["artist"], "genres": genres,
            "art_url": "/static/demo/" + e["cover"],
        })
    tidal_album_seeds = [
        [f"t_album_{i:02d}", e["album"], f"t_artist_{i:02d}", e["year"], e["extra"], e["cover"]]
        + [[t["title"], t["duration"]] for t in e["tracks"]]
        for i, e in enumerate(tidal, start=1)
    ]
    tidal_about = {e["artist"]: ARTIST_ABOUT[e["artist"]] for e in tidal}

    spotify_seeds = [
        [t["title"], e["artist"], e["album"], e["genre"], e["year"],
         t["duration"], e["sample_rate_hz"], None, e["cover"]]
        for e in spotify for t in e["tracks"]
    ]
    qobuz_seeds = [
        [t["title"], e["artist"], e["album"], e["genre"], e["year"],
         t["duration"], e["sample_rate_hz"], e["bit_depth"], e["cover"]]
        for e in qobuz for t in e["tracks"]
    ]

    local_about = {e["artist"]: ARTIST_ABOUT[e["artist"]] for e in local}
    local_album_about = {a: t for a, t in ALBUM_ABOUT.items() if a in {e["album"] for e in local}}
    fav = {k: v for k, v in favorite_flags(entries).items()
           if k.startswith("fav-album:") or k.startswith("fav-track:")
           or k.startswith("fav-spotify:") or k.startswith("fav-qobuz:")}
    img_keys = sorted(e["cover"][:-4] for e in entries)
    return LIBRARY_JS_TEMPLATE % (
        json.dumps(img_keys, ensure_ascii=False),
        json.dumps(fav, ensure_ascii=False, sort_keys=True),
        json.dumps(track_seeds, ensure_ascii=False),
        json.dumps(local_about, ensure_ascii=False, sort_keys=True),
        json.dumps(local_album_about, ensure_ascii=False, sort_keys=True),
        json.dumps(local_playlists, ensure_ascii=False),
        json.dumps(tidal_artists, ensure_ascii=False),
        json.dumps(tidal_about, ensure_ascii=False, sort_keys=True),
        json.dumps(tidal_album_seeds, ensure_ascii=False),
        json.dumps(tidal_playlist_seed_list(len(tidal)), ensure_ascii=False),
        json.dumps(spotify_seeds, ensure_ascii=False),
        json.dumps(qobuz_seeds, ensure_ascii=False),
    )


def smb_playlists(prefix: str, albums: list[dict], sampler_name: str) -> list[dict]:
    by_id = {}
    for e in albums:
        akey = prefix + slug(e["album"])
        for i in range(len(e["tracks"])):
            by_id[f"{akey}_{i + 1:02d}"] = e
    ordered = list(by_id)
    def genre_ids(*genres: str, limit: int = 0) -> list[str]:
        ids = [tid for tid, e in by_id.items() if e["genre"] in genres]
        return ids[:limit] if limit else ids
    genre_lists = sorted({e["genre"] for e in albums})
    main_genre = max(genre_lists, key=lambda g: sum(1 for e in albums if e["genre"] == g))
    seeds = [
        (f"playlist_{prefix}late_night", "Late Night Drive", ordered[:10]),
        (f"playlist_{prefix}spotlight", f"{main_genre} Spotlight", genre_ids(main_genre, limit=15)),
        (f"playlist_{prefix}sampler", sampler_name, [f"{prefix}{slug(e['album'])}_01" for e in albums]),
    ]
    return [{"id": pid, "name": name, "track_ids": ids, "track_count": len(ids)}
            for pid, name, ids in seeds if ids]


def render_smb_js(entries: list[dict], title: str, prefix: str, uri: str, global_name: str,
                  albums: list[dict], sampler_name: str) -> str:
    seeds = [
        [e["artist"], e["album"], e["genre"], e["year"], e["cover"],
         e["sample_rate_hz"], [[t["title"], t["duration"]] for t in e["tracks"]]]
        for e in albums
    ]
    flags = favorite_flags(entries)
    fav = {k: v for k, v in flags.items()
           if k.startswith(f"album:{prefix}") or k.startswith(f"track:{prefix}")}
    about = {e["artist"]: ARTIST_ABOUT[e["artist"]] for e in albums}
    album_about = {a: t for a, t in ALBUM_ABOUT.items() if a in {e["album"] for e in albums}}
    return SMB_JS_TEMPLATE % (
        title, json.dumps(seeds, ensure_ascii=False),
        json.dumps(smb_playlists(prefix, albums, sampler_name), ensure_ascii=False),
        json.dumps(fav, ensure_ascii=False, sort_keys=True),
        json.dumps(about, ensure_ascii=False, sort_keys=True),
        json.dumps(album_about, ensure_ascii=False, sort_keys=True),
        prefix, uri, uri, global_name,
    )


def sync_covers(entries: list[dict], extra_names: list[str]) -> tuple[int, int, int]:
    POOL_DIR.mkdir(parents=True, exist_ok=True)
    wanted = {}
    for e in entries:
        src = WEB_SRC / e["src"]
        if not src.is_file():
            raise FileNotFoundError(f"missing source cover: {src}")
        wanted[e["cover"]] = src
    wanted.update({name: None for name in extra_names})
    removed = 0
    for existing in POOL_DIR.glob("*.jpg"):
        if existing.name not in wanted:
            existing.unlink()
            removed += 1
    copied = 0
    for name, src in wanted.items():
        dest = POOL_DIR / name
        if not dest.is_file() and src is not None:
            shutil.copyfile(src, dest)
            copied += 1
    return copied, removed, len(wanted)


def build_montages(entries: list[dict], playlist_seeds: list[list]) -> list[str]:
    """Pre-rendered 2x2 playlist cover montages (local-collage look).

    One 500px JPEG per TIDAL playlist from its distinct member album
    covers, so multi-album playlists show their content instead of a
    random pool image. Deterministic: same inputs, same pixels.
    """
    from PIL import Image
    tidal_by_idx = [e for e in entries if e["library"] == "tidal"]
    aid_to_entry = {f"t_album_{i:02d}": e for i, e in enumerate(tidal_by_idx, start=1)}
    names = []
    for seed in playlist_seeds:
        pid, album_ids = seed[0], seed[3]
        covers = []
        for aid in dict.fromkeys(album_ids):
            entry = aid_to_entry.get(aid)
            if entry and entry["cover"] not in covers:
                covers.append(entry["cover"])
        name = f"tpl-{pid}.jpg"
        dest = POOL_DIR / name
        if len(covers) == 1:
            square_cover(WEB_SRC / by_album_cover_src(entries, covers[0]), 500).save(
                dest, "JPEG", quality=80, optimize=True, progressive=True)
        else:
            cells = [covers[i % len(covers)] for i in range(4)]
            canvas = Image.new("RGB", (500, 500))
            for i, cover in enumerate(cells):
                canvas.paste(
                    square_cover(WEB_SRC / by_album_cover_src(entries, cover), 250),
                    ((i % 2) * 250, (i // 2) * 250))
            canvas.save(dest, "JPEG", quality=80, optimize=True, progressive=True)
        names.append(name)
    return names


def by_album_cover_src(entries: list[dict], cover: str) -> str:
    for e in entries:
        if e["cover"] == cover:
            return e["src"]
    raise KeyError(f"no source for cover {cover}")


def square_cover(src_path, size: int):
    from PIL import Image
    with Image.open(src_path) as im:
        rgb = im.convert("RGB")
        side = min(rgb.size)
        left = (rgb.width - side) // 2
        top = (rgb.height - side) // 2
        return rgb.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the demo catalog fixtures.")
    parser.add_argument("--check", action="store_true",
                        help="verify committed fixtures match a fresh render")
    args = parser.parse_args()

    entries = build_album_entries()
    by_lib = {}
    for e in entries:
        by_lib.setdefault(e["library"], []).append(e)
    playlist_seeds = tidal_playlist_seed_list(len(by_lib["tidal"]))
    montage_names = [f"tpl-{seed[0]}.jpg" for seed in playlist_seeds]
    outputs = {
        DATA_DIR / "library.js": render_library_js(entries),
        DATA_DIR / "library2.js": render_smb_js(
            entries,
            "SMB_Demo_Library-1", "d2-", "demo2", "FXROUTE_DEMO_LIBRARY2",
            by_lib["smb1"], "SMB Demo Library 1 Sampler"),
        DATA_DIR / "library3.js": render_smb_js(
            entries,
            "SMB_Demo_Library-2", "d3-", "demo3", "FXROUTE_DEMO_LIBRARY3",
            by_lib["smb2"], "SMB Demo Library 2 Sampler"),
    }
    if args.check:
        failed = False
        for dest, text in outputs.items():
            if not dest.is_file() or dest.read_text(encoding="utf-8") != text:
                print(f"STALE: {dest}", file=sys.stderr)
                failed = True
        # covers
        wanted = {e["cover"] for e in entries} | set(montage_names)
        actual = {p.name for p in POOL_DIR.glob("*.jpg")}
        if wanted != actual:
            print(f"STALE: static/demo pool differs "
                  f"(missing={sorted(wanted - actual)[:5]}, extra={sorted(actual - wanted)[:5]})",
                  file=sys.stderr)
            failed = True
        else:
            from PIL import Image
            for name in montage_names:
                with Image.open(POOL_DIR / name) as im:
                    if im.size != (500, 500):
                        print(f"STALE: montage {name} is {im.size}", file=sys.stderr)
                        failed = True
        print("check: " + ("FAIL" if failed else "ok"))
        return 1 if failed else 0

    build_montages(entries, playlist_seeds)
    copied, removed, total = sync_covers(entries, montage_names)
    for dest, text in outputs.items():
        dest.write_text(text, encoding="utf-8")
    counts = {lib: (len(alb), sum(len(e["tracks"]) for e in alb)) for lib, alb in by_lib.items()}
    print(f"covers: {copied} copied, {removed} removed, {total} total")
    for lib, (na, nt) in counts.items():
        print(f"{lib}: {na} albums, {nt} tracks")
    for dest in outputs:
        print(f"fixture: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
