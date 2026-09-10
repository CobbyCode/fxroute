# SPDX-License-Identifier: AGPL-3.0-only
"""Build the second FXRoute demo music library ("Demo Library 2").

Source: 23 AI-generated album covers (PNG, ~1254px) in
``~/ai/projects/fxroute-newdemopics``. Each cover becomes one album with
8-14 deterministically generated audio tracks (tagged FLAC test tones) and
a 500x500 JPEG ``folder.jpg`` cover.

The build is fully deterministic: same source files always yield the same
library (track counts, titles, tones, tags). Output layout mirrors the
existing ``/srv/samba/music-demo`` share::

    <out>/<Artist>/<Album>/NN - <Title>.flac
    <out>/<Artist>/<Album>/folder.jpg
    <out>/MANIFEST.json

Optional outputs for the web demo:

* ``--pool-out <dir>``: 300x300 JPEG covers named ``d2-<slug>.jpg``,
  matching the ``static/demo/`` artwork-pool convention.
* ``--webdemo-js <file>``: a ``demo/data/library2.js`` fixture generated
  from the manifest (track seeds reference the pool covers).

Everything below is synthetic demo content; the existing demo library is
never touched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

try:
    from PIL import Image
except ModuleNotFoundError:
    print("Pillow is required (pip install pillow)", file=sys.stderr)
    sys.exit(2)

try:
    from mutagen.flac import FLAC
except ModuleNotFoundError:
    print("mutagen is required (pip install mutagen)", file=sys.stderr)
    sys.exit(2)


DEFAULT_SRC = Path(os.path.expanduser("~/ai/projects/fxroute-newdemopics"))
DEFAULT_OUT = Path("/srv/samba/music-demo-2")

COVER_SIZE = 500
POOL_SIZE = 300
COVER_QUALITY = 82
POOL_QUALITY = 80
SAMPLE_RATE = 44100

# (source PNG basename, artist, album, genre, year), in sorted-filename order.
# Artist/album text transcribed from the covers themselves.
ALBUMS: list[tuple[str, str, str, str, int]] = [
    ("ChatGPT Image Sep 10, 2026, 04_11_51 AM (1).png", "Nova Static", "Midnight Relay", "Synthwave", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_11_51 AM (2).png", "Elara North", "Harbor Lights", "Indie Folk", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_11_51 AM (3).png", "Mason Vale", "Copper Sky", "Americana", 2023),
    ("ChatGPT Image Sep 10, 2026, 04_11_51 AM (4).png", "Kira Atlas", "Parallel Bloom", "Dream Pop", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_11_52 AM (5).png", "Blue Meridian", "After the Rain", "Jazz", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_15_37 AM (1).png", "Luma District", "Vector Heart", "Electronic", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_15_38 AM (2).png", "Saffron Tide", "Golden Static", "Indie Rock", 2023),
    ("ChatGPT Image Sep 10, 2026, 04_15_38 AM (3).png", "The Velvet Arcade", "Neon Weekend", "Synthpop", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_15_38 AM (4).png", "North Cascade", "Silent Orbit", "Ambient", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_15_38 AM (5).png", "Ruby Comet", "Electric Honey", "Funk", 2023),
    ("ChatGPT Image Sep 10, 2026, 04_18_19 AM.png", "Static Parade", "Color Radio", "Pop", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_20_20 AM.png", "Aural Grid", "Signal Bloom", "Ambient", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_20_33 AM.png", "Neon Vale", "Drift Circuit", "Electronic", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_20_49 AM.png", "Vector Isles", "Pulse Theory", "Electronic", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_21_00 AM.png", "Static Coast", "Modular Hearts", "Electronic", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_22_48 AM (1).png", "The Alder Quartet", "Midnight Session", "Jazz", 2023),
    ("ChatGPT Image Sep 10, 2026, 04_22_48 AM (2).png", "Iris Vale Trio", "Copper Moon", "Jazz", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_22_48 AM (3).png", "June Meridian", "Smoke & Satin", "Vocal Jazz", 2023),
    ("ChatGPT Image Sep 10, 2026, 04_22_48 AM (4).png", "Velvet Brass Union", "After Hours Mosaic", "Jazz", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_22_48 AM (5).png", "Harbor Swing", "Blue Avenue", "Jazz", 2023),
    ("ChatGPT Image Sep 10, 2026, 04_24_59 AM (1).png", "Nocturne Harbor", "Golden Tides", "Ambient", 2024),
    ("ChatGPT Image Sep 10, 2026, 04_24_59 AM (2).png", "The Marlowe Ensemble", "Velvet Skyline", "Jazz", 2025),
    ("ChatGPT Image Sep 10, 2026, 04_24_59 AM (3).png", "Aural Vector", "Prism Engine", "Electronic", 2026),
]

# The "Smoke & Satin" cover prints its own track list; honor it for the
# first six tracks so the library matches the artwork.
SMOKE_AND_SATIN_TRACKS = [
    "A Little Later",
    "Smoke & Satin",
    "The Way You Stay",
    "Somewhere in Between",
    "Soft Enough",
    "Brighter Tomorrow",
]

# Genre -> evocative title words for plausible generated track names.
TITLE_WORDS: dict[str, list[str]] = {
    "Synthwave": ["Midnight", "Neon", "Chrome", "Ultraviolet", "Afterglow", "Nightdrive", "Laser", "Mirage", "Overdrive", "Nightcall", "Glasshouse", "Runaway", "Skyline", "Relay", "Palm", "Static", "Voltage", "Sunset", "Horizon", "Interceptor", "Cassette", "Dayglow"],
    "Synthpop": ["Neon", "Weekend", "Dancer", "Ultraviolet", "Daydream", "Spotlight", "Mirror", "Electric", "Parade", "Sugar", "Velvet", "Rhythm", "Heartbeat", "Glitter", "Afterhours", "Cassette", "Dayglo", "Moonlight", "Fever", "Arcade", "Stereo", "Confetti"],
    "Indie Folk": ["Harbor", "Lantern", "Willow", "Fieldstone", "Meadow", "Ember", "Tidewater", "Sparrow", "Cedar", "Moonrise", "Gravel", "Orchard", "Lantern", "Driftwood", "Clover", "Brook", "Thistle", "Harvest", "Wren", "Fogline", "Alder", "Bramble"],
    "Americana": ["Copper", "Dustline", "Highway", "Mesquite", "Redrock", "Tailwind", "Boxcar", "Sundown", "Riverbed", "Coyote", "Badlands", "Whiskey", "Depot", "Sagebrush", "Freight", "Canyon", "Diner", "Thunderhead", "Mesa", "Lonesome", "Gravel", "Skyfire"],
    "Dream Pop": ["Bloom", "Haze", "Petal", "Glasswing", "Daydream", "Mirage", "Softfall", "Lull", "Prism", "Halo", "Drift", "Opal", "Mistral", "Serene", "Bloomline", "Airglow", "Feverdream", "Halcyon", "Moonmilk", "Velvet", "Palefire", "Silver"],
    "Jazz": ["Afterhours", "Blue", "Moon", "Satin", "Alley", "Nocturne", "Brass", "Velvet", "Rain", "Uptown", "Mellow", "Sidestreet", "Lamplight", "Evergreen", "Secondline", "Ballad", "Smokering", "Downbeat", "Starlight", "Ember", "Riverwalk", "Nightcap"],
    "Vocal Jazz": ["Satin", "Moon", "Velvet", "Whisper", "Candle", "Ember", "Lullaby", "Stardust", "Nocturne", "Blue", "Honey", "Afterglow", "Serenade", "Twilight", "Starlight", "Dreamer", "Smoke", "Midnight", "Paisley", "Amber", "Nightingale"],
    "Electronic": ["Vector", "Pulse", "Lattice", "Modular", "Circuit", "Signal", "Chrome", "Drift", "Helix", "Zero", "Prism", "Current", "Engine", "Waveform", "Ion", "Flux", "Pixel", "Nova", "Grid", "Relay", "Cipher", "Monochrome"],
    "Ambient": ["Orbit", "Bloom", "Tide", "Halo", "Drift", "Stillness", "Meridian", "Hollow", "Auric", "Dusklight", "Field", "Weightless", "Undertow", "Slowtide", "Overcast", "Ember", "Glasssea", "Nightwater", "Pale", "Sanctuary", "Lowlight", "Air"],
    "Indie Rock": ["Static", "Golden", "Highway", "Thunder", "Daybreak", "Copper", "Wildfire", "Gravel", "Sundown", "Radio", "Comet", "Dust", "Skyline", "Reverb", "Tide", "Avalanche", "Neon", "Backroad", "Storm", "Daydream", "Voltage", "Horizon"],
    "Funk": ["Honey", "Groove", "Boogie", "Sugar", "Fever", "Strut", "Pepper", "Lightning", "Velvet", "Jive", "Copper", "Shake", "Sass", "Glide", "Rumpus", "Sizzle", "Swagger", "Bump", "Fuzz", "Snap", "Slink", "Pop"],
    "Pop": ["Radio", "Color", "Parade", "Dayglo", "Confetti", "Stereo", "Bubble", "Rocket", "Cherry", "Static", "Glitter", "Sunroof", "Postcard", "Lollipop", "Firework", "Carousel", "Neon", "Daydream", "Jukebox", "Marquee", "Polaroid", "Highfive"],
}

# Pentatonic-ish ladder (Hz) for pleasant test tones.
TONE_LADDER = [110.0, 130.81, 146.83, 164.81, 196.0, 220.0, 261.63, 293.66, 329.63, 392.0, 440.0, 523.25]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def sanitize_filename(text: str) -> str:
    cleaned = re.sub(r'[\\/:\*\?"<>\|\x00-\x1f]', "-", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or "untitled"


def track_count_for(album: str) -> int:
    digest = hashlib.sha256(album.encode("utf-8")).digest()
    return 8 + (digest[0] % 7)


def track_titles(genre: str, album: str, count: int, seed_extra: str = "") -> list[str]:
    """Deterministic plausible titles; Smoke & Satin keeps its printed list."""
    if album == "Smoke & Satin":
        titles = list(SMOKE_AND_SATIN_TRACKS)
    else:
        titles = []
    words = sorted(set(TITLE_WORDS.get(genre, TITLE_WORDS["Electronic"])))
    rng = random.Random(f"fxroute-demo2:{album}:{seed_extra}")
    seen = set(titles)
    use_counts: dict[str, int] = {}
    while len(titles) < count:
        # Prefer the least-used words so no word dominates an album. The
        # candidate lists are sorted, keeping the build deterministic.
        least = min(use_counts.get(word, 0) for word in words)
        first_pool = sorted(word for word in words if use_counts.get(word, 0) == least)
        first = rng.choice(first_pool)
        use_counts[first] = use_counts.get(first, 0) + 1
        least = min(use_counts.get(word, 0) for word in words if word != first)
        second_pool = sorted(word for word in words if word != first and use_counts.get(word, 0) == least)
        second = rng.choice(second_pool)
        use_counts[second] = use_counts.get(second, 0) + 1
        title = f"{first} {second}"
        if title in seen:
            continue
        seen.add(title)
        titles.append(title)
    return titles[:count]


def hash_code(text: str) -> int:
    """Mirror the demo fixture hashCode() (library.js) used for favorites."""
    h = 0
    for ch in str(text):
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return h & 0x7FFFFFFF


def favorite_flags(albums: list[dict]) -> dict[str, bool]:
    """Deterministic favorite flags for every track/album id.

    Mirrors the main demo catalog's hashCode() favorites (library.js):
    tracks are favored when ``hashCode('fav-track:' + id) % 4 == 0``, albums
    when ``hashCode('fav-album:' + key) % 3 == 0``. Computed here in Python
    and emitted literally into the fixture, so the webdemo data carries the
    favorites instead of re-deriving them at page load.
    """
    flags: dict[str, bool] = {}
    for entry in albums:
        album_key = entry["album_key"]
        flags[f"album:{album_key}"] = hash_code("fav-album:" + album_key) % 3 == 0
        for index, _ in enumerate(entry["tracks"], start=1):
            track_id = f"{album_key}_{index:02d}"
            flags[f"track:{track_id}"] = hash_code("fav-track:" + track_id) % 4 == 0
    return flags


def build_playlists(manifest_albums: list[dict]) -> list[dict]:
    """A small themed playlist set, analog to the main demo catalog."""
    by_id = {}
    for entry in manifest_albums:
        for index, track in enumerate(entry["tracks"], start=1):
            track_id = f"{entry['album_key']}_{index:02d}"
            by_id[track_id] = (entry, track)
    ordered_ids = list(by_id)
    def genre_ids(*genres: str, limit: int = 0) -> list[str]:
        ids = [tid for tid, (entry, _) in by_id.items() if entry["genre"] in genres]
        return ids[:limit] if limit else ids
    seeds = [
        ("playlist_d2_late_night", "Late Night Drive", ordered_ids[:10]),
        ("playlist_d2_electronic", "Electronic Frontiers", genre_ids("Electronic", limit=15)),
        ("playlist_d2_jazz", "Jazz Evening", genre_ids("Jazz", "Vocal Jazz", limit=12)),
        ("playlist_d2_ambient", "Ambient Focus", genre_ids("Ambient")),
        ("playlist_d2_neon", "Neon Nights", genre_ids("Synthwave", "Synthpop", "Pop")),
        ("playlist_d2_sampler", "Demo Library 2 Sampler",
         [f"{entry['album_key']}_01" for entry in manifest_albums]),
    ]
    return [{"id": pid, "name": name, "track_ids": ids, "track_count": len(ids)}
            for pid, name, ids in seeds if ids]


def square_cover(src: Path, size: int, quality: int) -> Image.Image:
    with Image.open(src) as im:
        im = im.convert("RGB")
        side = min(im.size)
        left = (im.width - side) // 2
        top = (im.height - side) // 2
        im = im.crop((left, top, left + side, top + side))
        return im.resize((size, size), Image.LANCZOS)


def synth_track(path: Path, root_hz: float, duration_s: float) -> None:
    """Render a short two-tone FLAC test signal with soft fades."""
    fifth_hz = root_hz * 1.5
    fade_out_start = max(0.0, duration_s - 1.0)
    filter_graph = (
        f"[0:a][1:a]amix=inputs=2:duration=first:weights='1 0.35',"
        f"afade=t=in:st=0:d=0.5,afade=t=out:st={fade_out_start:.2f}:d=1.0,"
        f"aformat=sample_rates={SAMPLE_RATE}:channel_layouts=mono[a]"
    )
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency={root_hz:.2f}:duration={duration_s:.2f}:sample_rate={SAMPLE_RATE}",
        "-f", "lavfi", "-i", f"sine=frequency={fifth_hz:.2f}:duration={duration_s:.2f}:sample_rate={SAMPLE_RATE}",
        "-filter_complex", filter_graph,
        "-map", "[a]", "-c:a", "flac",
        str(path),
    ]
    subprocess.run(cmd, check=True)


def tag_flac(path: Path, *, artist: str, album: str, title: str, track_no: int, genre: str, year: int) -> None:
    audio = FLAC(str(path))
    audio["artist"] = artist
    audio["albumartist"] = artist
    audio["album"] = album
    audio["title"] = title
    audio["tracknumber"] = str(track_no)
    audio["genre"] = genre
    audio["date"] = str(year)
    audio.save()


def album_seed_entries() -> list[dict]:
    """Pure per-album manifest entries (no file I/O).

    Everything the builder and the webdemo fixture share — track titles,
    counts, durations and the tone RNG draws — is derived deterministically
    from ALBUMS, so the fixture can be re-rendered without the source
    covers. ``root_hz`` is carried along to keep the tone RNG sequence
    identical to the file-writing build(); it is stripped from the emitted
    manifest.
    """
    entries = []
    for png_name, artist, album, genre, year in ALBUMS:
        album_slug = slug(f"{artist}-{album}")
        album_key = "d2-" + slug(album)
        titles = track_titles(genre, album, track_count_for(album))
        rng = random.Random(f"fxroute-demo2-tones:{album}")
        tracks = []
        for index, title in enumerate(titles, start=1):
            root_hz = rng.choice(TONE_LADDER)
            duration_s = round(8.0 + rng.random() * 12.0, 1)
            tracks.append({
                "file": f"{index:02d} - {sanitize_filename(title)}.flac",
                "title": title,
                "duration": duration_s,
                "sample_rate_hz": SAMPLE_RATE,
                "root_hz": root_hz,
            })
        entries.append({
            "artist": artist, "album": album, "genre": genre, "year": year,
            "slug": album_slug, "album_key": album_key, "cover": "folder.jpg",
            "pool_image": f"d2-{album_slug}.jpg",
            "tracks": tracks,
        })
    return entries


def build(src_dir: Path, out_dir: Path, pool_dir: Path | None) -> dict:
    manifest: dict = {"library": "Demo Library 2", "albums": []}
    total_tracks = 0
    for entry, (png_name, artist, album, genre, year) in zip(album_seed_entries(), ALBUMS):
        src = src_dir / png_name
        if not src.is_file():
            raise FileNotFoundError(f"missing source cover: {src}")
        album_dir = out_dir / sanitize_filename(artist) / sanitize_filename(album)
        album_dir.mkdir(parents=True, exist_ok=True)

        cover = square_cover(src, COVER_SIZE, COVER_QUALITY)
        cover.save(album_dir / "folder.jpg", quality=COVER_QUALITY)

        if pool_dir is not None:
            pool = square_cover(src, POOL_SIZE, POOL_QUALITY)
            pool.save(pool_dir / entry["pool_image"], quality=POOL_QUALITY)

        for index, track in enumerate(entry["tracks"], start=1):
            track_path = album_dir / track["file"]
            synth_track(track_path, track["root_hz"], track["duration"])
            tag_flac(track_path, artist=artist, album=album, title=track["title"],
                     track_no=index, genre=genre, year=year)

        manifest_tracks = [{k: v for k, v in t.items() if k != "root_hz"}
                           for t in entry["tracks"]]
        manifest["albums"].append({**entry, "tracks": manifest_tracks})
        total_tracks += len(entry["tracks"])
        print(f"{artist} — {album}: {len(entry['tracks'])} tracks")
    manifest["album_count"] = len(manifest["albums"])
    manifest["track_count"] = total_tracks
    manifest["playlists"] = build_playlists(manifest["albums"])
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


JS_PREAMBLE = """// Demo Library 2 catalog fixture (generated by scripts/build_demo_library2.py --webdemo-js).
// Same shape as the local demo catalog in data/library.js: fictional artists,
// generated track titles, covers from the static/demo/ pool (d2-*.jpg).
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

    const tracks = [];
    const albums = [];
    SEEDS.forEach((seed) => {
        const [artist, album, genre, year, poolImage, trackSeeds] = seed;
        const albumKey = 'd2-' + slug(album);
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
                url: 'file://demo2/' + albumKey + '/' + String(index + 1).padStart(2, '0') + ' - ' + title + '.flac',
                duration,
                path: 'demo2/' + album + '/' + String(index + 1).padStart(2, '0') + ' - ' + title + '.flac',
                sample_rate_hz: 44100,
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
            release_type: 'Album',
            favorite: FAVORITES['album:' + albumKey],
            cover_source: 'folder',
            has_external_cover: false,
            coverUrl: cover,
            demo_cover_url: cover,
        });
    });

    const playlists = PLAYLISTS.map((entry) => ({ ...entry }));

    window.FXROUTE_DEMO_LIBRARY2 = { tracks, albums, playlists };
})();
"""


def render_webdemo_js(manifest: dict) -> str:
    """Render the library2.js fixture text from a manifest (pure)."""
    seeds = [
        [entry["artist"], entry["album"], entry["genre"], entry["year"],
         entry["pool_image"], [[t["title"], t["duration"]] for t in entry["tracks"]]]
        for entry in manifest["albums"]
    ]
    return JS_PREAMBLE % (
        json.dumps(seeds, ensure_ascii=False),
        json.dumps(manifest["playlists"], ensure_ascii=False),
        json.dumps(favorite_flags(manifest["albums"]), ensure_ascii=False, sort_keys=True),
    )


def emit_webdemo_js(manifest: dict, dest: Path) -> None:
    dest.write_text(render_webdemo_js(manifest), encoding="utf-8")
    print(f"webdemo fixture: {dest} ({len(manifest['playlists'])} playlists)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Demo Library 2 music share.")
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pool-out", type=Path, default=None,
                        help="Write 300x300 webdemo pool covers (d2-*.jpg) here")
    parser.add_argument("--webdemo-js", type=Path, default=None,
                        help="Write the demo/data/library2.js fixture here")
    args = parser.parse_args()

    for name, _, _, _, _ in ALBUMS:
        if not (args.src / name).is_file():
            print(f"missing source cover: {args.src / name}", file=sys.stderr)
            return 1
    args.out.mkdir(parents=True, exist_ok=True)
    if args.pool_out is not None:
        args.pool_out.mkdir(parents=True, exist_ok=True)

    manifest = build(args.src, args.out, args.pool_out)
    if args.webdemo_js is not None:
        emit_webdemo_js(manifest, args.webdemo_js)
    print(f"done: {manifest['album_count']} albums, {manifest['track_count']} tracks -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
