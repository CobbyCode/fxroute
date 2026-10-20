#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Library Artists view and the shared Discover Similar (headless Chromium).

Covers:

* the Artists tab sits between Tracks and Folders; artists come from the local
  metadata (album artist, the track artist for compilations, never "Various
  Artists") and work without TIDAL;
* connected: a TIDAL favorite artist merges into the local entry of the same
  accent-folded name (no duplicate), TIDAL-only favorites get a glyph entry,
  stored mappings add the TIDAL heart/image through one batch request;
* the artist page: albums, tracks, About, an "On TIDAL" section for a matched
  artist, Back from an album returns to the artist page;
* one Discover Similar: local artists open their own page, unmapped ones stay
  plain, `?provider=tidal` only when connected;
* a TIDAL-only entry opens the TIDAL artist inside the Library;
* Select all in Tracks includes the TIDAL favorite tracks;
* not connected / not installed: purely local, no TIDAL requests.
"""

import http.server
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8253

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_artists.py (playwright not installed)")
    sys.exit(0)


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _track(track_id, title, artist, album, album_artist="", number=1):
    return {"id": track_id, "title": title, "artist": artist, "album": album, "album_artist": album_artist,
            "track_number": number, "path": track_id[6:], "favorite": False, "source": "local"}


TRACKS = [
    _track("local_A/one.flac", "Alpha One", "Alpha", "Alpha Album", "Alpha", 1),
    _track("local_A/two.flac", "Alpha Two", "Alpha", "Alpha Album", "Alpha", 2),
    _track("local_C/one.flac", "Charlie One", "Charlie", "Charlie Album", "Charlie"),
    _track("local_Z/one.flac", "Zoe One", "Zoë", "Zoe Album", "Zoë"),
    # Compilation: owned by the track artists, never by "Various Artists".
    _track("local_V/one.flac", "Delta Song", "Delta", "Mixed Bag", "", 1),
    _track("local_V/two.flac", "Other Song", "Foxtrot", "Mixed Bag", "", 2),
]
ALBUMS = [
    {"id": "alb-a", "name": "Alpha Album", "artist": "Alpha", "track_count": 2, "year": 2001,
     "mb_artist_id": "mb-alpha", "artist_description": "Alpha is a local band.", "favorite": False},
    {"id": "alb-c", "name": "Charlie Album", "artist": "Charlie", "track_count": 1, "year": 1999, "favorite": False},
    {"id": "alb-z", "name": "Zoe Album", "artist": "Zoë", "track_count": 1, "year": 2010, "favorite": False},
    {"id": "alb-v", "name": "Mixed Bag", "artist": "Various Artists", "track_count": 2, "favorite": False},
]
TIDAL_TRACK = {"id": "9001", "title": "Tidal Song", "artist": "Alpha", "album": "Tidal Album", "duration": 200, "art_url": ""}
TIDAL_FAV_ARTISTS = [
    {"id": "ta-zoe", "name": "Zoe", "art_url": "https://img.example/zoe.jpg"},
    {"id": "ta-echo", "name": "Echo", "art_url": ""},
]
ALPHA_TIDAL = {
    "id": "tp-alpha", "name": "Alpha", "art_url": "https://img.example/alpha.jpg",
    "albums": [{"id": "tal-1", "title": "Tidal Album", "artist": "Alpha", "art_url": "", "num_tracks": 1}],
    "top_tracks": [TIDAL_TRACK],
    "enrichment": {"mb_artist_id": "mb-alpha", "about": "Alpha on TIDAL.", "similar": []},
}
DISCOVER = [
    {"type": "artist", "artist": "Charlie", "artist_mbid": "mb-c"},
    {"type": "artist", "artist": "Golf", "artist_mbid": "mb-g"},
]

STUB = """
window.__fetchLog = [];
window.__posts = [];
window.__favoriteArtists = __FAV_ARTISTS__;
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    window.__fetchLog.push(u);
    const json = (data) => Promise.resolve(new Response(JSON.stringify(data), {
        status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    const post = opts && opts.method === 'POST';
    if (post) window.__posts.push([u, opts.body || '']);
    if (u.endsWith('/api/tracks')) return json(__TRACKS__);
    if (u.endsWith('/api/library/status')) return json({ ready: true, scanning: false });
    if (u.endsWith('/api/playlists')) return json([]);
    if (u.endsWith('/api/albums')) return json(__ALBUMS__);
    const albumTracks = u.match(/\\/api\\/albums\\/([^/]+)\\/tracks$/);
    if (albumTracks) {
        const album = __ALBUMS__.find(a => a.id === albumTracks[1]);
        return json(__TRACKS__.filter(t => album && t.album === album.name));
    }
    if (/\\/api\\/albums\\/[^/]+\\/discover/.test(u)) return json({ items: __DISCOVER__ });
    if (u.endsWith('/api/artists/provider-matches')) {
        const body = JSON.parse(opts.body);
        return json({ provider: 'tidal', matches: body.artists.map(a => a.name === 'Alpha'
            ? { provider_artist_id: 'tp-alpha', name: 'Alpha', art_url: 'https://img.example/alpha.jpg' } : null) });
    }
    if (u.includes('/api/streaming/providers/discovery')) return json(__DISCOVERY__);
    if (u.includes('/api/streaming/tidal/status')) return json(__STATUS__);
    if (u.includes('/api/streaming/tidal/library/snapshot')) return json({ user_id: 'u1', ids: {} });
    if (u.includes('/api/streaming/tidal/favorites/ids')) return json({
        tracks: ['9001'], albums: [], playlists: [], artists: window.__favoriteArtists.map(a => a.id) });
    if (u.includes('/api/streaming/tidal/favorites?type=artists')) return json(window.__favoriteArtists);
    if (u.includes('/api/streaming/tidal/favorites?type=tracks')) return json([__TIDAL_TRACK__]);
    if (u.includes('/api/streaming/tidal/favorites?type=albums')) return json([]);
    if (u.includes('/api/streaming/tidal/playlists') && !u.includes('/tracks')) return json([]);
    if (/\\/api\\/streaming\\/tidal\\/artists\\/[^/]+\\/favorite/.test(u)) {
        const favorite = JSON.parse(opts.body).favorite;
        return json({ favorite });
    }
    if (/\\/api\\/streaming\\/tidal\\/artists\\/tp-alpha$/.test(u)) return json(__ALPHA_TIDAL__);
    if (/\\/api\\/streaming\\/tidal\\/artists\\/[^/]+$/.test(u)) return json({
        id: 'ta-echo', name: 'Echo', albums: [], top_tracks: [],
        enrichment: { similar: [{ type: 'artist', artist: 'Charlie', provider_artist_id: 'tp-c', art_url: '' }] } });
    if (u.includes('/api/streaming/tidal/search') && u.includes('q=Foxtrot')) return json({
        artists: [{ id: 'f1', name: 'Foxtrot', art_url: '' }, { id: 'f2', name: 'Foxtrot', art_url: '' }],
        albums: [], tracks: [], playlists: [] });
    if (u.includes('/api/streaming/tidal/search')) return json({ artists: [], albums: [], tracks: [], playlists: [] });
    if (u.includes('/api/streaming/')) return json({});
    if (u.endsWith('/api/stations')) return json([]);
    if (u.endsWith('/api/status')) return json({ playing: false });
    if (u.includes('/api/audio/')) return json({});
    if (u.includes('/api/dsp/')) return json({ presets: [] });
    return realFetch(url, opts);
};
"""


def _stub(tidal_mode):
    installed = tidal_mode != "absent"
    authenticated = tidal_mode == "connected"
    status = {
        "installed": installed, "available": installed, "authenticated": authenticated,
        "user": {"id": "u1"} if authenticated else None, "capabilities": {}, "status": "Stopped",
    }
    discovery = {"providers": [
        {"id": "tidal", "name": "Tidal", "installed": installed, "available": installed, "enabled": True,
         "capabilities": {"catalog": True}},
    ]}
    replacements = {
        "__FAV_ARTISTS__": TIDAL_FAV_ARTISTS, "__TRACKS__": TRACKS, "__ALBUMS__": ALBUMS, "__DISCOVER__": DISCOVER,
        "__DISCOVERY__": discovery, "__STATUS__": status, "__TIDAL_TRACK__": TIDAL_TRACK,
        "__ALPHA_TIDAL__": ALPHA_TIDAL,
    }
    script = STUB
    for key, value in replacements.items():
        script = script.replace(key, json.dumps(value))
    return script


def _open_library(browser, tidal_mode, view="artists"):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []

    def _record(exc):
        stack = getattr(exc, "stack", "") or str(exc)
        if any(name in stack for name in ("library_ui.js", "streaming.js", "ui_helpers.js")):
            errors.append(stack.splitlines()[0])

    page.on("pageerror", _record)
    # Remote artist images answer with a 1x1 PNG so they never fall back.
    page.route("https://img.example/**", lambda route: route.fulfill(
        status=200, content_type="image/png",
        body=bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                           "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")))
    page.add_init_script(_stub(tidal_mode))
    page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
    page.click("#tab-btn-library")
    page.wait_for_selector(f"#library-view-{view}")
    page.click(f"#library-view-{view}")
    return page, errors


def _names(page):
    return page.eval_on_selector_all("#albums-grid .artist-card .album-name", "els => els.map(e => e.textContent)")


def _run():
    server = _serve()
    passed = 0

    def check(name, condition):
        nonlocal passed
        assert condition, name
        passed += 1

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            # --- connected -----------------------------------------------------
            page, errors = _open_library(browser, "connected")
            page.wait_for_selector('#albums-grid .artist-card[data-artist-key="echo"]')
            tabs = page.eval_on_selector_all(".library-view-toggle .view-tab", "els => els.map(e => e.textContent)")
            check(f"Artists sits between Tracks and Folders ({tabs})",
                  tabs == ["Albums", "Tracks", "Artists", "Folders", "Favorites"])
            names = _names(page)
            check(f"artists merge by accent-folded name, compilations by track artist ({names})",
                  names == ["Alpha", "Charlie", "Delta", "Echo", "Foxtrot", "Zoë"])
            check("Various Artists is never an artist", "Various Artists" not in names)
            check("the TIDAL favorite merges into the local entry (no duplicate Zoe)", names.count("Zoë") == 1)
            zoe = page.locator('#albums-grid .artist-card[data-artist-key="zoe"]')
            check("the merged artist keeps its local album count and shows a filled TIDAL heart",
                  zoe.locator(".album-artist").inner_text() == "1 album"
                  and zoe.locator(".streaming-fav.is-active, .streaming-fav.active").count() == 1)
            check("the merged artist shows the TIDAL image",
                  "img.example/zoe.jpg" in zoe.locator("img.album-art, img").first.get_attribute("src"))
            echo = page.locator('#albums-grid .artist-card[data-artist-key="echo"]')
            check("a TIDAL-only artist carries the TIDAL glyph", echo.locator(".source-glyph--tidal").count() == 1)
            check("a local artist carries no TIDAL glyph",
                  page.locator('#albums-grid .artist-card[data-artist-key="charlie"] .source-glyph').count() == 0)
            page.wait_for_function(
                "document.querySelector('.artist-card[data-artist-key=\"alpha\"] .streaming-fav') !== null")
            matches = [entry for entry in page.evaluate("window.__posts") if entry[0].endswith("/api/artists/provider-matches")]
            check("one batch request maps the local artists", len(matches) == 1
                  and {"name": "Alpha", "mbid": "mb-alpha"} in json.loads(matches[0][1])["artists"])
            check("an unmapped local artist has no TIDAL heart",
                  page.locator('.artist-card[data-artist-key="charlie"] .streaming-fav').count() == 0)

            # Search filters the artist grid.
            page.fill("#library-search", "cha")
            page.wait_for_function("document.querySelectorAll('#albums-grid .artist-card').length === 1")
            check("search filters the artists by name", _names(page) == ["Charlie"])
            page.fill("#library-search", "")
            page.wait_for_function("document.querySelectorAll('#albums-grid .artist-card').length === 6")

            # Artist page of a mapped artist.
            page.click('.artist-card[data-artist-key="alpha"]')
            page.wait_for_selector("#album-detail:not(.hidden) .artist-detail-tidal .streaming-result")
            check("the artist page names the artist", page.locator("#album-detail-name").inner_text() == "Alpha")
            check("the artist page counts albums and tracks",
                  page.locator("#album-detail-count").inner_text().startswith("1 album · 2 tracks"))
            check("the artist page shows the local About", "Alpha is a local band." in page.locator("#album-detail").inner_text())
            check("the artist page lists the local album", page.locator(".artist-detail-albums .album-card").count() == 1)
            check("the artist page lists the local tracks",
                  page.locator("#album-detail-tracks .track-item[data-track-id]").count() == 2)
            check("an On TIDAL section shows the TIDAL top track and album",
                  page.locator(".artist-detail-tidal .streaming-result").count() == 1
                  and page.locator(".artist-detail-tidal .tidal-tiles .album-card, .artist-detail-tidal .tidal-tiles li").count() >= 1)
            check("the On TIDAL section carries the TIDAL glyph",
                  page.locator(".artist-detail-tidal .artist-detail-source .source-glyph--tidal").count() == 1)
            heart = page.locator("#album-favorite-toggle")
            check("the artist heart hearts the artist on TIDAL (not favorited yet)",
                  heart.is_visible() and "active" not in (heart.get_attribute("class") or "")
                  and "TIDAL" in (heart.get_attribute("aria-label") or ""))
            heart.click()
            page.wait_for_function(
                "window.__posts.some(p => p[0].includes('/artists/tp-alpha/favorite'))")
            page.wait_for_function("document.querySelector('#album-favorite-toggle').classList.contains('active')")
            check("the artist heart follows the TIDAL favorite state", True)

            # Discover Similar: one renderer, local artists open their page.
            page.wait_for_selector("#album-discover .streaming-similar-item")
            discover_urls = [u for u in page.evaluate("window.__fetchLog") if "/discover" in u]
            check("a connected library asks for the TIDAL mappings",
                  discover_urls and discover_urls[-1].endswith("/discover?provider=tidal"))
            cards = page.eval_on_selector_all("#album-discover .streaming-similar-name", "els => els.map(e => e.textContent)")
            check(f"Discover Similar renders cards for the suggestions ({cards})", cards == ["Charlie", "Golf"])
            page.click('#album-discover .streaming-similar-item >> nth=0')
            page.wait_for_function("document.querySelector('#album-detail-name').textContent === 'Charlie'")
            check("a similar artist the library has opens its local page", True)
            page.click("#album-detail-back")
            page.wait_for_selector("#albums-grid .artist-card")
            check("Back returns to the artists grid", page.locator("#albums-grid .artist-card").count() == 6)

            # An album opened from the artist page returns to the artist page.
            page.click('.artist-card[data-artist-key="alpha"]')
            page.wait_for_selector(".artist-detail-albums .album-card")
            page.click(".artist-detail-albums .album-card")
            page.wait_for_function("document.querySelector('#album-detail-name').textContent === 'Alpha Album'")
            page.click("#album-detail-back")
            page.wait_for_function("document.querySelector('#album-detail-name').textContent === 'Alpha'")
            check("Back from an album returns to the artist page", page.locator(".artist-detail-albums").count() == 1)
            page.click("#album-detail-back")
            page.wait_for_selector("#albums-grid .artist-card")

            # Compilation artist: own page without TIDAL part or Discover.
            page.click('.artist-card[data-artist-key="delta"]')
            page.wait_for_selector("#album-detail:not(.hidden)")
            page.wait_for_timeout(300)
            check("a compilation artist has no album tiles but their tracks",
                  page.locator(".artist-detail-albums").count() == 0
                  and page.locator("#album-detail-tracks .track-item[data-track-id]").count() == 1)
            check("an unmatched artist has no On TIDAL section", page.locator(".artist-detail-tidal").count() == 0)
            check("an artist without a MusicBrainz seed has no Discover Similar",
                  page.locator("#album-discover").evaluate("e => e.classList.contains('hidden')"))
            page.click("#album-detail-back")
            page.wait_for_selector("#albums-grid .artist-card")

            # An ambiguous TIDAL name is never auto-linked; the page offers the search.
            page.click('.artist-card[data-artist-key="foxtrot"]')
            page.wait_for_selector(".artist-detail-tidal .artist-search-tidal")
            check("an ambiguous TIDAL name offers Search on TIDAL instead of a guess",
                  page.locator(".artist-detail-tidal .streaming-result").count() == 0
                  and page.locator("#album-favorite-toggle").is_hidden())
            page.click(".artist-search-tidal")
            page.wait_for_selector('[data-search-section="tidal"] .album-card[data-tidal-kind="artist"]')
            check("Search on TIDAL searches the Library for the name, artists first",
                  page.locator("#library-search").input_value() == "Foxtrot"
                  and page.locator("#library-search-type-artists.active").count() == 1
                  and page.locator("#tab-tidal").count() == 0)
            check("both TIDAL artists of that name are offered next to the local one",
                  page.locator('[data-search-section="tidal"] .album-card[data-tidal-kind="artist"]').count() == 2
                  and page.locator('.library-search-section .artist-card[data-artist-key="foxtrot"]').count() == 1)
            page.fill("#library-search", "")
            page.wait_for_selector("#library-view-artists")
            page.click("#library-view-artists")
            page.wait_for_selector("#albums-grid .artist-card")

            # TIDAL-only artist opens the TIDAL artist inside the Library.
            page.click('.artist-card[data-artist-key="echo"]')
            page.wait_for_selector("#library-tidal-detail:not(.hidden) .tidal-detail-title")
            check("a TIDAL-only artist opens the TIDAL detail in the Library",
                  page.locator("#library-tidal-detail .tidal-detail-title").inner_text() == "Echo")
            page.wait_for_selector("#library-tidal-detail .streaming-similar-item")
            page.click("#library-tidal-detail .streaming-similar-item")
            page.wait_for_function("document.querySelector('#album-detail-name').textContent === 'Charlie'")
            check("a similar artist inside a TIDAL page opens the local artist when the library has it",
                  page.locator("#library-tidal-detail").evaluate("e => e.classList.contains('hidden')")
                  and not page.locator("#album-detail").evaluate("e => e.classList.contains('hidden')"))

            # Select all in Tracks covers local and TIDAL favorite tracks.
            page.click("#library-view-tracks")
            page.wait_for_selector("#tracks-list .streaming-result")
            page.click("#select-all-tracks")
            page.wait_for_function(
                "document.querySelectorAll('#tracks-list .track-item.selected').length > 0")
            local_selected = page.locator("#tracks-list .track-item.selected").count()
            tidal_selected = page.locator("#tracks-list .streaming-result.is-selected").count()
            check(f"Select all selects local and TIDAL tracks ({local_selected}/{tidal_selected})",
                  local_selected >= 6 and tidal_selected == 1)
            check("the button flips to Clear selection",
                  page.locator("#select-all-tracks").inner_text() == "Clear selection")
            page.click("#select-all-tracks")
            page.wait_for_function("document.querySelectorAll('#tracks-list .streaming-result.is-selected').length === 0")
            check("Clear selection clears both sources",
                  page.locator("#tracks-list .track-item.selected").count() == 0)
            check("no unexpected script errors (connected)", errors == [])
            page.close()

            # --- not connected -------------------------------------------------
            page, errors = _open_library(browser, "signed-out")
            page.wait_for_selector("#albums-grid .artist-card")
            names = _names(page)
            check(f"signed out: only the local artists ({names})",
                  names == ["Alpha", "Charlie", "Delta", "Foxtrot", "Zoë"])
            check("signed out: no TIDAL hearts or glyphs",
                  page.locator("#albums-grid .streaming-fav, #albums-grid .source-glyph").count() == 0)
            page.click('.artist-card[data-artist-key="alpha"]')
            page.wait_for_selector("#album-discover .streaming-similar-item")
            check("signed out: no On TIDAL section", page.locator(".artist-detail-tidal").count() == 0)
            urls = page.evaluate("window.__fetchLog")
            check("signed out: Discover asks without the TIDAL provider",
                  any(u.endswith("/discover") for u in urls) and not any("provider=tidal" in u for u in urls))
            check("signed out: no mapping request",
                  not any(entry[0].endswith("/api/artists/provider-matches") for entry in page.evaluate("window.__posts")))
            check("signed out: no catalog requests",
                  not any("/api/streaming/tidal/artists/" in u or "/api/streaming/tidal/search" in u for u in urls))
            check("signed out: similar artists stay plain without an opener",
                  page.locator("#album-discover .streaming-similar-item").count() == 2)
            check("no unexpected script errors (signed out)", errors == [])
            page.close()

            # --- not installed -------------------------------------------------
            page, errors = _open_library(browser, "absent")
            page.wait_for_selector("#albums-grid .artist-card")
            check("not installed: a purely local artist list",
                  _names(page) == ["Alpha", "Charlie", "Delta", "Foxtrot", "Zoë"])
            check("not installed: no TIDAL header button", page.locator("#library-tidal-status").is_hidden())
            check("no unexpected script errors (not installed)", errors == [])
            page.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"PASS  scripts/test_library_artists.py ({passed} checks)")


if __name__ == "__main__":
    _run()
