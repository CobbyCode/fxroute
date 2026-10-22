#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Unified Library search (headless Chromium).

Covers:

* a query in Albums/Tracks/Artists/Playlists/Favorites opens a results view
  with the type tabs Albums / Tracks / Artists / Playlists (starting at the
  category the search was typed in, Albums for Favorites) in place of the
  view tabs;
* every type lists "In your library" (local items and TIDAL favorites) first,
  then "TIDAL" (catalog results without what the library section lists);
* one debounced catalog request for all four types, kept per query, and a
  slow answer of an older query never replaces a newer one;
* Enter searches at once, Esc clears and returns to the previous view;
* a failed search offers Retry; signed out the section is a Connect hint and
  nothing is requested; not installed there is no TIDAL section at all;
* Folders and an open local album filter in place; typing on an artist page
  or a TIDAL detail leaves it for the results; Back returns to the results;
* Select all in the Tracks results covers local, favorite and found tracks.
"""

import http.server
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8254

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_unified_search.py (playwright not installed)")
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
    _track("local_B/one.flac", "Beta One", "Beta", "Beta Album", "Beta"),
]
ALBUMS = [
    {"id": "alb-a", "name": "Alpha Album", "artist": "Alpha", "track_count": 2, "year": 2001, "favorite": False},
    {"id": "alb-b", "name": "Beta Album", "artist": "Beta", "track_count": 1, "year": 1999, "favorite": False},
]
PLAYLISTS = [{"id": "pl-1", "name": "Alpha Mix", "track_ids": ["local_A/one.flac"], "track_count": 1}]

TIDAL_FAV_ALBUM = {"id": "fa-1", "title": "Alphabet Soup", "artist": "Zed", "art_url": ""}
TIDAL_FAV_TRACK = {"id": "9001", "title": "Alpha Tidal Fav", "artist": "Zed", "album": "Soup", "duration": 200, "art_url": ""}
CATALOG = {
    "albums": [TIDAL_FAV_ALBUM, {"id": "ca-2", "title": "Alpha Waves", "artist": "Wave", "art_url": ""}],
    "tracks": [TIDAL_FAV_TRACK, {"id": "9002", "title": "Alpha Search Hit", "artist": "Wave", "album": "Waves",
                                 "duration": 180, "art_url": ""}],
    "artists": [{"id": "ta-1", "name": "Alpha", "art_url": ""}, {"id": "ta-2", "name": "Alpha Centauri", "art_url": ""}],
    "playlists": [{"id": "tp-1", "name": "Alpha Radio", "art_url": "", "track_count": 12}],
}

STUB = """
window.__fetchLog = [];
window.__boomCalls = 0;
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    window.__fetchLog.push(u);
    const respond = (data, status = 200, delay = 0) => new Promise((resolve) => setTimeout(() => resolve(new Response(
        JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })), delay));
    if (u.endsWith('/api/tracks')) return respond(__TRACKS__);
    if (u.endsWith('/api/library/status')) return respond({ ready: true, scanning: false });
    if (u.endsWith('/api/playlists')) return respond(__PLAYLISTS__);
    if (u.endsWith('/api/albums')) return respond(__ALBUMS__);
    const albumTracks = u.match(/\\/api\\/albums\\/([^/]+)\\/tracks$/);
    if (albumTracks) {
        const album = __ALBUMS__.find(a => a.id === albumTracks[1]);
        return respond(__TRACKS__.filter(t => album && t.album === album.name));
    }
    if (/\\/api\\/albums\\/[^/]+\\/discover/.test(u)) return respond({ items: [] });
    if (u.endsWith('/api/artists/provider-matches')) return respond({ provider: 'tidal', matches: [] });
    if (u.includes('/api/streaming/providers/discovery')) return respond(__DISCOVERY__);
    if (u.includes('/api/streaming/tidal/status')) return respond(__STATUS__);
    if (u.includes('/api/streaming/tidal/library/snapshot')) return respond({ user_id: 'u1', ids: {} });
    if (u.includes('/api/streaming/tidal/favorites/ids')) return respond({
        tracks: ['9001'], albums: ['fa-1'], playlists: [], artists: [] });
    if (u.includes('/api/streaming/tidal/favorites?type=artists')) return respond([]);
    if (u.includes('/api/streaming/tidal/favorites?type=tracks')) return respond([__FAV_TRACK__]);
    if (u.includes('/api/streaming/tidal/favorites?type=albums')) return respond([__FAV_ALBUM__]);
    if (u.includes('/api/streaming/tidal/playlists') && !u.includes('/tracks')) return respond([]);
    if (/\\/api\\/streaming\\/tidal\\/albums\\/[^/]+\\/tracks$/.test(u)) return respond([]);
    if (/\\/api\\/streaming\\/tidal\\/albums\\/[^/]+$/.test(u)) return respond({ id: 'ca-2', title: 'Alpha Waves', artist: 'Wave' });
    if (/\\/api\\/streaming\\/tidal\\/artists\\/[^/]+$/.test(u)) return respond({
        id: 'ta-2', name: 'Alpha Centauri', albums: [], top_tracks: [], enrichment: { similar: [] } });
    if (u.includes('/api/streaming/tidal/search')) {
        const query = new URL(u, location.href).searchParams.get('q');
        if (query === 'slow') return respond({ albums: [{ id: 's1', title: 'STALE', artist: 'x', art_url: '' }], tracks: [], artists: [], playlists: [] }, 200, 700);
        if (query === 'slowx') return respond({ albums: [{ id: 's2', title: 'FRESH', artist: 'x', art_url: '' }], tracks: [], artists: [], playlists: [] });
        if (query === 'boom') {
            window.__boomCalls += 1;
            if (window.__boomCalls === 1) return respond({ detail: 'TIDAL is unavailable' }, 502);
            return respond({ albums: [{ id: 'b1', title: 'Recovered', artist: 'x', art_url: '' }], tracks: [], artists: [], playlists: [] });
        }
        if (query === 'alpha') return respond(__CATALOG__);
        return respond({ albums: [], tracks: [], artists: [], playlists: [] });
    }
    if (u.includes('/api/streaming/')) return respond({});
    if (u.endsWith('/api/stations')) return respond([]);
    if (u.endsWith('/api/status')) return respond({ playing: false });
    if (u.includes('/api/audio/')) return respond({});
    if (u.includes('/api/dsp/')) return respond({ presets: [] });
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
        "__TRACKS__": TRACKS, "__ALBUMS__": ALBUMS, "__PLAYLISTS__": PLAYLISTS, "__DISCOVERY__": discovery,
        "__STATUS__": status, "__FAV_TRACK__": TIDAL_FAV_TRACK, "__FAV_ALBUM__": TIDAL_FAV_ALBUM, "__CATALOG__": CATALOG,
    }
    script = STUB
    for key, value in replacements.items():
        script = script.replace(key, json.dumps(value))
    return script


def _open_library(browser, tidal_mode, view="albums"):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []

    def _record(exc):
        stack = getattr(exc, "stack", "") or str(exc)
        if any(name in stack for name in ("library_ui.js", "streaming.js", "ui_helpers.js")):
            errors.append(stack.splitlines()[0])

    page.on("pageerror", _record)
    page.add_init_script(_stub(tidal_mode))
    page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
    page.click("#tab-btn-library")
    page.wait_for_selector(f"#library-view-{view}")
    page.click(f"#library-view-{view}")
    return page, errors


def _searches(page):
    return [u for u in page.evaluate("window.__fetchLog") if "/api/streaming/tidal/search" in u]


def _titles(page, section):
    return page.eval_on_selector_all(
        f'{section} .album-card .album-name', "els => els.map(e => e.textContent)")


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

            # --- connected: Albums ------------------------------------------------
            page, errors = _open_library(browser, "connected")
            page.wait_for_selector("#albums-grid .album-card")
            check("without a query the view tabs show and the result tabs do not",
                  page.locator(".library-view-toggle").is_visible() and page.locator("#library-search-types").is_hidden())
            page.click("#library-search")
            page.keyboard.type("alpha")
            page.wait_for_selector('[data-search-section="tidal"] .album-card[data-tidal-kind="album"]')
            check("a query swaps the view tabs for the four result types",
                  page.locator(".library-view-toggle").is_hidden()
                  and page.eval_on_selector_all("#library-search-types .view-tab", "els => els.map(e => e.textContent)")
                  == ["Albums", "Tracks", "Artists", "Playlists"])
            check("the search starts at the category it was typed in",
                  page.locator("#library-search-type-albums.active").count() == 1)
            check("typing fast makes exactly one catalog request for all four types",
                  len(_searches(page)) == 1
                  and "types=albums,tracks,artists,playlists" in _searches(page)[0] and "q=alpha" in _searches(page)[0])
            heads = page.eval_on_selector_all(".library-search-heading", "els => els.map(e => e.textContent.trim())")
            check(f"In your library comes first, then TIDAL ({heads})", heads == ["In your library", "TIDAL"])
            library_titles = _titles(page, '.library-search-section:first-child')
            check(f"the library section lists the local album and the TIDAL favorite ({library_titles})",
                  sorted(library_titles) == ["Alphabet Soup", "Alpha Album"] or sorted(library_titles) == ["Alpha Album", "Alphabet Soup"])
            check("a local album has no TIDAL glyph, the favorite has one",
                  page.locator('.library-search-section:first-child .album-card[data-album-id] .source-glyph').count() == 0
                  and page.locator('.library-search-section:first-child .album-card[data-tidal-kind="album"] .source-glyph--tidal').count() == 1)
            tidal_titles = _titles(page, '[data-search-section="tidal"]')
            check(f"the TIDAL section skips what the library section lists ({tidal_titles})", tidal_titles == ["Alpha Waves"])
            check("local albums that do not match stay out", "Beta Album" not in library_titles)

            # Switching the type does not search again.
            page.click("#library-search-type-tracks")
            page.wait_for_selector('[data-search-section="tidal"] .streaming-result')
            check("switching the type reuses the catalog answer", len(_searches(page)) == 1)
            check("Tracks lists local tracks, the favorite and the found track",
                  page.locator('.library-search-section:first-child .track-item[data-track-id]').count() == 2
                  and page.locator('.library-search-section:first-child .streaming-result').count() == 1
                  and page.locator('[data-search-section="tidal"] .streaming-result').count() == 1)
            check("TIDAL track rows carry the TIDAL glyph",
                  page.locator('[data-search-section="tidal"] .streaming-result .source-glyph--tidal').count() == 1)
            check("Select all is offered for tracks", page.locator("#select-all-tracks").is_visible()
                  and page.locator("#select-all-tracks").is_enabled())
            page.click("#select-all-tracks")
            selected_tidal = page.evaluate("window.FXRouteStreaming.tidal.selectedIds()")
            check(f"Select all covers local, favorite and found tracks ({selected_tidal})",
                  page.locator('.library-search-section:first-child .track-item[data-track-id].selected').count() == 2
                  and sorted(selected_tidal) == ["9001", "9002"])
            page.click("#select-all-tracks")
            check("Clear visible empties both selections",
                  page.evaluate("window.FXRouteStreaming.tidal.selectedIds().length") == 0)

            page.click("#library-search-type-artists")
            page.wait_for_selector('[data-search-section="tidal"] .album-card[data-tidal-kind="artist"]')
            local_artists = _titles(page, '.library-search-section:first-child')
            check(f"Artists lists the local artist ({local_artists})", local_artists == ["Alpha"])
            check("a TIDAL namesake of a local artist stays visible, it may be another artist",
                  _titles(page, '[data-search-section="tidal"]') == ["Alpha", "Alpha Centauri"])

            page.click("#library-search-type-playlists")
            page.wait_for_selector('[data-search-section="tidal"] .album-card[data-tidal-kind="playlist"]')
            check("Playlists lists the local playlist, then the TIDAL one",
                  _titles(page, '.library-search-section:first-child') == ["Alpha Mix"]
                  and _titles(page, '[data-search-section="tidal"]') == ["Alpha Radio"])
            check("still one catalog request for the whole search", len(_searches(page)) == 1)

            # A TIDAL result opens inside the Library; Back returns to the results.
            page.click("#library-search-type-albums")
            page.click('[data-search-section="tidal"] .album-card[data-tidal-kind="album"]')
            page.wait_for_selector("#library-tidal-detail:not(.hidden) #tidal-detail-back")
            check("a TIDAL result opens in the Library",
                  page.locator("#library-tidal-detail").is_visible() and page.locator("#albums-grid").is_hidden())
            page.click("#tidal-detail-back")
            page.wait_for_selector('[data-search-section="tidal"] .album-card')
            check("Back returns to the results of the same query",
                  page.locator("#library-search").input_value() == "alpha"
                  and page.locator("#library-search-type-albums.active").count() == 1)
            check("coming back does not search again", len(_searches(page)) == 1)

            # A local album opens; typing there filters in place.
            page.click('.library-search-section:first-child .album-card[data-album-id="alb-a"]')
            page.wait_for_selector("#album-detail:not(.hidden)")
            check("opened from the results the album shows its tracks for the query",
                  page.locator("#album-detail-tracks .track-item[data-track-id]").count() == 2)
            page.fill("#library-search", "alpha o")
            page.wait_for_function("document.querySelectorAll('#album-detail-tracks .track-item[data-track-id]').length === 1")
            check("an open local album filters in place, the view tabs return",
                  page.locator(".library-view-toggle").is_visible() and page.locator("#library-search-types").is_hidden())
            check("filtering in the album asks TIDAL for nothing", len(_searches(page)) == 1)
            page.click("#album-detail-back")
            page.wait_for_selector('.library-search-section')
            check("Back from the album returns to the results of the current query",
                  page.locator("#library-search-type-albums.active").count() == 1)
            page.fill("#library-search", "alpha")
            page.wait_for_selector('[data-search-section="tidal"] .album-card[data-tidal-kind="album"]')

            # A result type tab leaves an open detail for the results.
            page.click('.library-search-section:first-child .album-card[data-album-id="alb-a"]')
            page.wait_for_selector("#album-detail:not(.hidden)")
            page.click("#library-search-type-playlists")
            page.wait_for_selector('[data-search-section="tidal"] .album-card[data-tidal-kind="playlist"]')
            check("a result type tab in an open detail returns to the results",
                  page.locator("#album-detail").is_hidden() and page.locator("#library-search-type-playlists.active").count() == 1)
            page.click("#library-search-type-albums")

            # Esc clears and returns to the previous view.
            page.click("#library-search")
            page.keyboard.press("Escape")
            page.wait_for_selector(".library-view-toggle:not(.hidden)")
            check("Esc clears the query and brings the view tabs back",
                  page.locator("#library-search").input_value() == "" and page.locator("#library-search-types").is_hidden()
                  and page.locator("#albums-grid .library-search-section").count() == 0)
            check("the previous view returns with all albums",
                  page.locator("#albums-grid > .album-card[data-album-id]").count() == 2)
            check("no unexpected script errors (albums)", errors == [])
            page.close()

            # --- connected: start category, Enter, stale answers, errors -------------
            page, errors = _open_library(browser, "connected", view="tracks")
            page.wait_for_selector("#tracks-list .track-item")
            page.fill("#library-search", "alpha")
            check("typed in Tracks the search starts at Tracks",
                  page.locator("#library-search-type-tracks.active").count() == 1)
            page.fill("#library-search", "")
            page.click("#library-view-playlists")
            page.wait_for_selector("#albums-grid .album-card")
            page.fill("#library-search", "alpha")
            check("typed in Playlists the search starts at Playlists",
                  page.locator("#library-search-type-playlists.active").count() == 1)
            page.fill("#library-search", "")
            page.wait_for_selector("#library-view-playlists.active")
            check("clearing the search returns to Playlists", page.locator("#albums-grid [data-smart-favorite]").count() == 2)
            page.click("#library-view-favorites")
            page.wait_for_selector("#tracks-list.is-favorites:not(.hidden)")
            page.fill("#library-search", "alpha")
            check("typed in Favorites the search starts at Albums",
                  page.locator("#library-search-type-albums.active").count() == 1
                  and page.locator("#tracks-list").is_hidden())
            page.fill("#library-search", "")
            page.wait_for_selector("#library-view-favorites.active")
            check("clearing the search returns to Favorites",
                  page.locator("#tracks-list.is-favorites:not(.hidden) [data-favorites-section]").count() > 0
                  and page.locator("#tab-library [data-search-section]").count() == 0)

            page.click("#library-view-albums")
            page.fill("#library-search", "alpha")
            page.keyboard.press("Enter")
            page.wait_for_function("window.__fetchLog.some(u => u.includes('/search') && u.includes('q=alpha'))", timeout=250)
            check("Enter searches at once instead of waiting for the debounce", True)
            page.fill("#library-search", "")

            # A slow answer of an older query never replaces the newer one.
            page.fill("#library-search", "slow")
            page.keyboard.press("Enter")
            page.fill("#library-search", "slowx")
            page.keyboard.press("Enter")
            page.wait_for_selector('[data-search-section="tidal"] .album-name:text("FRESH")')
            page.wait_for_timeout(900)
            check("the slow older answer is dropped",
                  _titles(page, '[data-search-section="tidal"]') == ["FRESH"])
            page.fill("#library-search", "")

            # A failed search offers Retry.
            page.fill("#library-search", "boom")
            page.keyboard.press("Enter")
            page.wait_for_selector(".library-search-action")
            check("a failed TIDAL search shows the error with Retry",
                  page.locator('[data-search-section="tidal"] .library-search-action').inner_text() == "Retry"
                  and "unavailable" in page.locator('[data-search-section="tidal"]').inner_text())
            page.click("#library-search-type-tracks")
            page.click("#library-search-type-albums")
            page.wait_for_selector(".library-search-action")
            page.wait_for_timeout(600)
            check("re-rendering after a failure does not search again", page.evaluate("window.__boomCalls") == 1)
            page.click(".library-search-action")
            page.wait_for_selector('[data-search-section="tidal"] .album-name:text("Recovered")')
            check("Retry searches again and shows the answer", page.evaluate("window.__boomCalls") == 2)
            page.fill("#library-search", "")

            # Folders filter in place, no results view, no catalog request.
            before = len(_searches(page))
            page.click("#library-view-folders")
            page.fill("#library-search", "alpha")
            page.wait_for_timeout(600)
            check("Folders keep filtering in place",
                  page.locator("#library-search-types").is_hidden() and page.locator(".library-view-toggle").is_visible()
                  and len(_searches(page)) == before)
            page.fill("#library-search", "")

            # Typing on an artist page leaves it for the results.
            page.click("#library-view-artists")
            page.wait_for_selector("#albums-grid .artist-card")
            page.click('.artist-card[data-artist-key="alpha"]')
            page.wait_for_selector("#album-detail:not(.hidden)")
            page.fill("#library-search", "alpha")
            page.wait_for_selector("#library-search-types:not(.hidden)")
            check("typing on an artist page opens the results",
                  page.locator("#album-detail").is_hidden() and page.locator("#library-search-type-artists.active").count() == 1)
            check("no unexpected script errors (tracks/artists)", errors == [])
            page.close()

            # --- signed out: a Connect hint, nothing requested --------------------------
            page, errors = _open_library(browser, "signed-out")
            page.wait_for_selector("#albums-grid .album-card")
            page.fill("#library-search", "alpha")
            page.wait_for_selector('[data-search-section="tidal"] .library-search-action')
            check("signed out the TIDAL section is a Connect hint",
                  page.locator('[data-search-section="tidal"] .library-search-action').inner_text() == "Connect TIDAL")
            page.wait_for_timeout(600)
            check("signed out no catalog request is made", _searches(page) == [])
            check("signed out the library section still works",
                  _titles(page, '.library-search-section:first-child') == ["Alpha Album"])
            check("no unexpected script errors (signed out)", errors == [])
            page.close()

            # --- not installed: purely local -----------------------------------------------
            page, errors = _open_library(browser, "absent")
            page.wait_for_selector("#albums-grid .album-card")
            page.fill("#library-search", "alpha")
            page.wait_for_selector(".library-search-section")
            page.wait_for_timeout(600)
            check("not installed there is no TIDAL section",
                  page.locator('[data-search-section="tidal"]').count() == 0
                  or page.locator('[data-search-section="tidal"]').inner_text().strip() == "")
            check("not installed no catalog request is made", _searches(page) == [])
            check("not installed the local results work",
                  _titles(page, '.library-search-section:first-child') == ["Alpha Album"])
            page.click("#library-search-type-tracks")
            check("not installed local tracks list",
                  page.locator('.library-search-section:first-child .track-item[data-track-id]').count() == 2)
            check("no unexpected script errors (not installed)", errors == [])
            page.close()
            browser.close()
    finally:
        server.shutdown()
    print(f"PASS  scripts/test_library_unified_search.py ({passed} checks)")


if __name__ == "__main__":
    _run()
