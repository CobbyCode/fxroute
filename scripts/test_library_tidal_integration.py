#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""TIDAL inside the shared Library (headless Chromium, stubbed backend).

Covers the Library side of the TIDAL merge:

* connected: TIDAL favorite albums join the Albums grid in backend order and
  carry the TIDAL glyph; Favorites shows both smart playlists, local and
  TIDAL playlists and favorite albums of both sources; Tracks appends the
  TIDAL favorite tracks; Favorite Tracks lists local and TIDAL favorites;
* a TIDAL tile opens its detail inside the Library and Back returns to the
  grid; an un-hearted TIDAL album leaves the grid at once;
* the save row follows the selection: TIDAL-only offers Save as new and Add
  to existing, a mixed selection saves one local playlist with TIDAL
  references (see test_library_mixed_playlists.py);
* more than one page of TIDAL favorites: the first page renders, then the
  full collection (limit=0) replaces it;
* not connected: Connect TIDAL button, no TIDAL items; not installed: no
  button, a purely local Library.
"""

import http.server
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8252

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_tidal_integration.py (playwright not installed)")
    sys.exit(0)


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


def _serve():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


TRACKS = [
    {"id": "local_A/one.flac", "title": "Local One", "artist": "Alpha", "album": "Alpha Album",
     "album_artist": "Alpha", "path": "A/one.flac", "favorite": True, "source": "local"},
    {"id": "local_C/two.flac", "title": "Local Two", "artist": "Charlie", "album": "Charlie Album",
     "album_artist": "Charlie", "path": "C/two.flac", "favorite": False, "source": "local"},
]
ALBUMS = [
    {"id": "alb-a", "name": "Alpha Album", "artist": "Alpha", "track_count": 1, "favorite": True},
    {"id": "alb-c", "name": "Charlie Album", "artist": "Charlie", "track_count": 1, "favorite": False},
]
PLAYLISTS = [{"id": "local-mix", "name": "Local Mix", "track_ids": ["local_A/one.flac"], "track_count": 1}]


def _tidal_album(index, artist):
    return {"id": f"ta{index}", "title": f"Tidal Album {index}", "artist": artist, "art_url": "", "num_tracks": 2}


def _stub(tidal_mode, album_count):
    # Bravo sorts between the local Alpha and Charlie albums.
    tidal_albums = [_tidal_album(i, "Bravo") for i in range(album_count)]
    # ta1 stands for two quality variants the backend folded into one entry.
    tidal_albums[1]["variant_ids"] = ["ta1", "ta-lo"]
    tidal_tracks = [
        {"id": "9001", "title": "Tidal Song", "artist": "Bravo", "album": "Tidal Album 0", "duration": 200, "art_url": ""},
    ]
    tidal_playlists = [{"id": "tp1", "name": "Tidal Mix", "track_count": 3, "art_url": ""}]
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
    return f"""
window.__fetchLog = [];
window.__tidalAlbums = {json.dumps(tidal_albums)};
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {{
    const u = String(url);
    window.__fetchLog.push(u);
    const json = (data) => Promise.resolve(new Response(JSON.stringify(data), {{
        status: 200,
        headers: {{ 'Content-Type': 'application/json' }},
    }}));
    if (u.endsWith('/api/tracks')) return json({json.dumps(TRACKS)});
    if (u.endsWith('/api/library/status')) return json({{ ready: true, scanning: false }});
    if (u.endsWith('/api/playlists')) return json({json.dumps(PLAYLISTS)});
    if (u.endsWith('/api/albums')) return json({json.dumps(ALBUMS)});
    if (/\\/api\\/albums\\/[^/]+\\/discover/.test(u)) return json({{ items: [] }});
    if (u.includes('/api/streaming/providers/discovery')) return json({json.dumps(discovery)});
    if (u.includes('/api/streaming/tidal/status')) return json({json.dumps(status)});
    if (u.includes('/api/streaming/tidal/library/snapshot')) return json({{ user_id: 'u1', ids: {{}} }});
    if (u.includes('/api/streaming/tidal/favorites/ids')) return json({{ tracks: ['9001'], albums: window.__tidalAlbums.flatMap(a => a.variant_ids || [a.id]), artists: [], playlists: ['tp1'] }});
    if (u.includes('/api/streaming/tidal/favorites?type=albums')) {{
        const limit = Number(new URL(u, location.href).searchParams.get('limit'));
        return json(limit > 0 ? window.__tidalAlbums.slice(0, limit) : window.__tidalAlbums);
    }}
    if (u.includes('/api/streaming/tidal/favorites?type=tracks')) return json({json.dumps(tidal_tracks)});
    if (u.includes('/api/streaming/tidal/playlists') && !u.includes('/tracks')) return json({json.dumps(tidal_playlists)});
    if (/\\/api\\/streaming\\/tidal\\/albums\\/[^/]+\\/tracks/.test(u)) return json({json.dumps(tidal_tracks)});
    if (/\\/api\\/streaming\\/tidal\\/albums\\/[^/]+$/.test(u)) return json({{ title: 'Tidal Album 0', artist: 'Bravo', num_tracks: 1 }});
    if (/\\/api\\/streaming\\/tidal\\/albums\\/[^/]+\\/favorite/.test(u)) {{
        const gone = window.__tidalAlbums.find(a => u.includes('/' + a.id + '/'));
        window.__tidalAlbums = window.__tidalAlbums.filter(a => a !== gone);
        return json({{ favorite: false, ...(gone && gone.variant_ids ? {{ variant_ids: gone.variant_ids }} : {{}}) }});
    }}
    if (u.includes('/api/streaming/')) return json({{}});
    if (u.endsWith('/api/stations')) return json([]);
    if (u.endsWith('/api/status')) return json({{ playing: false }});
    if (u.includes('/api/audio/')) return json({{}});
    if (u.includes('/api/dsp/')) return json({{ presets: [] }});
    return realFetch(url, opts);
}};
"""


def _open_library(browser, tidal_mode, album_count=2):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    # Only the modules under test count; the stubbed backend leaves unrelated
    # panels (DSP, measurement) with empty payloads.
    def _record(exc):
        stack = getattr(exc, "stack", "") or str(exc)
        if any(name in stack for name in ("library_ui.js", "streaming.js", "ui_helpers.js")):
            errors.append(stack.splitlines()[0])
    page.on("pageerror", _record)
    page.add_init_script(_stub(tidal_mode, album_count))
    page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
    page.click("#tab-btn-library")
    page.wait_for_selector("#library-view-albums")
    page.click("#library-view-albums")
    page.wait_for_function("document.querySelectorAll('#albums-grid .album-card').length > 0")
    return page, errors


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

            # --- connected ---------------------------------------------------
            page, errors = _open_library(browser, "connected")
            page.wait_for_function("document.querySelectorAll('#albums-grid [data-tidal-kind=\"album\"]').length === 2")
            order = page.eval_on_selector_all("#albums-grid .album-card .album-name", "els => els.map(e => e.textContent)")
            check(f"TIDAL albums merge in backend order ({order})",
                  order == ["Alpha Album", "Tidal Album 0", "Tidal Album 1", "Charlie Album"])
            check("TIDAL tiles carry the TIDAL glyph",
                  page.locator('#albums-grid [data-tidal-kind="album"] .source-glyph--tidal').count() == 2)
            check("local tiles carry no TIDAL glyph",
                  page.locator('#albums-grid .album-card:not([data-tidal-kind]) .source-glyph').count() == 0)
            button = page.locator("#library-tidal-status")
            check("connected header button is visible", button.is_visible())
            check("connected header button reads TIDAL",
                  button.locator(".library-tidal-status-label").inner_text() == "TIDAL")

            page.click("#library-view-favorites")
            page.wait_for_selector('#albums-grid [data-smart-favorite="favorite-tracks"]')
            names = page.eval_on_selector_all("#albums-grid .album-card .album-name", "els => els.map(e => e.textContent)")
            check(f"Favorites lists smart playlists, both playlists and both favorite albums ({names})",
                  names[:4] == ["Top 40", "Favorite Tracks", "Local Mix", "Tidal Mix"]
                  and "Alpha Album" in names and "Tidal Album 0" in names and "Charlie Album" not in names)

            page.click('#albums-grid [data-smart-favorite="favorite-tracks"]')
            page.wait_for_selector("#album-detail:not(.hidden)")
            local_rows = page.locator("#album-detail-tracks .track-item[data-track-id]").count()
            tidal_rows = page.locator("#album-detail-tracks .streaming-result").count()
            check(f"Favorite Tracks lists local and TIDAL favorites ({local_rows}+{tidal_rows})",
                  local_rows == 1 and tidal_rows == 1)
            check("Favorite Tracks counts both sources",
                  page.locator("#album-detail-count").inner_text().startswith("2 tracks"))
            page.click("#album-detail-back")

            page.click("#library-view-tracks")
            page.wait_for_selector("#tracks-list .streaming-result")
            check("Tracks appends the TIDAL favorite tracks after the local ones",
                  page.eval_on_selector_all("#tracks-list > .track-item[data-track-id], #tracks-list > .streaming-result",
                                            "els => els.map(e => e.classList.contains('streaming-result'))")
                  == [False, False, True])
            check("TIDAL track rows carry the TIDAL glyph",
                  page.locator("#tracks-list .streaming-result .track-sub .source-glyph--tidal").count() == 1)

            # Save row variants: TIDAL only, then mixed with a local track.
            page.click("#tracks-list .streaming-result .streaming-add")
            page.wait_for_selector("#playlist-save-row:not(.hidden)")
            check("a TIDAL selection saves as a new TIDAL playlist",
                  page.locator("#save-playlist").inner_text() == "Save as new")
            check("a TIDAL selection offers an existing TIDAL playlist",
                  page.locator("#playlist-tidal-target").is_visible()
                  and page.locator('#playlist-tidal-target option[value="tp1"]').count() == 1)
            page.click('#tracks-list .track-item[data-track-id="local_A/one.flac"] .track-add')
            check("a mixed selection can be saved", not page.locator("#save-playlist").is_disabled())
            check("a mixed selection says how it is saved", page.locator("#playlist-save-hint").is_visible())
            page.click("#cancel-playlist-selection")
            check("Cancel clears both selections", page.locator("#playlist-save-row").is_hidden())

            # TIDAL detail inside the Library, then Back.
            page.click("#library-view-albums")
            page.click('#albums-grid [data-tidal-kind="album"] .album-name >> nth=0')
            page.wait_for_selector("#library-tidal-detail:not(.hidden) .tidal-detail-header")
            check("TIDAL detail opens inside the Library", page.locator("#albums-grid").is_hidden())
            page.click("#library-tidal-detail #tidal-detail-back")
            page.wait_for_selector("#albums-grid:not(.hidden)")
            check("Back returns to the Albums grid", page.locator("#library-tidal-detail").is_hidden())

            # Un-hearting a TIDAL album removes it from the grid.
            fav = "(id) => window.FXRouteStreaming.tidal.isFavorite('albums', id)"
            check("a folded quality variant keeps its heart after the Library load",
                  page.evaluate(fav, "ta-lo") and page.evaluate(fav, "ta1"))
            page.click('#albums-grid [data-tidal-id="ta1"] .streaming-fav')
            page.wait_for_function("!document.querySelector('#albums-grid [data-tidal-id=\"ta1\"]')")
            check("an un-hearted TIDAL album leaves the grid", True)
            check("un-hearting the album also clears its quality variant",
                  not page.evaluate(fav, "ta-lo") and not page.evaluate(fav, "ta1"))
            check(f"no page errors ({errors})", not errors)
            page.close()

            # --- more than one page of favorites ------------------------------
            page, errors = _open_library(browser, "connected", album_count=60)
            page.wait_for_function("document.querySelectorAll('#albums-grid [data-tidal-kind=\"album\"]').length === 60")
            log = page.evaluate("window.__fetchLog.filter(u => u.includes('favorites?type=albums'))")
            check(f"first page, then the full collection ({log})",
                  any(u.endswith("limit=50") for u in log) and any(u.endswith("limit=0") for u in log))
            check(f"no page errors ({errors})", not errors)
            page.close()

            # --- installed, not connected -------------------------------------
            page, errors = _open_library(browser, "signed-out")
            page.wait_for_selector("#library-tidal-status:not(.hidden)")
            check("signed-out header button offers Connect TIDAL",
                  page.locator("#library-tidal-status .library-tidal-status-label").inner_text() == "Connect TIDAL")
            check("signed-out Library shows no TIDAL tiles",
                  page.locator("#albums-grid [data-tidal-kind]").count() == 0)
            check(f"no page errors ({errors})", not errors)
            page.close()

            # --- not installed -------------------------------------------------
            page, errors = _open_library(browser, "absent")
            page.wait_for_timeout(300)
            check("without TIDAL the header button stays hidden", page.locator("#library-tidal-status").is_hidden())
            check("without TIDAL the Library stays local",
                  page.locator("#albums-grid [data-tidal-kind]").count() == 0
                  and page.locator("#albums-grid .album-card").count() == 2)
            check(f"no page errors ({errors})", not errors)
            page.close()

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_library_tidal_integration.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_library_tidal_integration.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report browser setup failures
        print("FAIL  scripts/test_library_tidal_integration.py")
        print(f"  {exc}")
        sys.exit(1)
