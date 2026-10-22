#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playlist management in the Library (headless Chromium, stubbed backend).

Covers:

* local and TIDAL playlist details carry an active heart next to the title;
  removing asks first: a local heart deletes the playlist through
  DELETE /api/playlists/{id}, a TIDAL heart removes it from the TIDAL
  favorites; a dismissed question changes nothing (cards and details alike);
* the local playlist detail exports M3U8 with a button left of Back, while
  Back keeps the spot it has in the TIDAL playlist detail; TIDAL playlists
  and Smart Mixes show no export and Smart Mixes no heart;
* the Tracks tab lists single titles only, the search still finds playlists.
"""

import http.server
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8256

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_playlist_management.py (playwright not installed)")
    sys.exit(0)


class _StaticHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, *args):
        pass


TRACKS = [
    {"id": "local_A/one.flac", "title": "Local One", "artist": "Alpha", "album": "Alpha Album",
     "album_artist": "Alpha", "path": "A/one.flac", "favorite": True, "source": "local", "duration": 100},
    {"id": "local_C/two.flac", "title": "Local Two", "artist": "Charlie", "album": "Charlie Album",
     "album_artist": "Charlie", "path": "C/two.flac", "favorite": False, "source": "local", "duration": 120},
]
ALBUMS = [
    {"id": "alb-a", "name": "Alpha Album", "artist": "Alpha", "track_count": 1, "favorite": True},
    {"id": "alb-c", "name": "Charlie Album", "artist": "Charlie", "track_count": 1, "favorite": False},
]
PLAYLISTS = [
    {"id": "local-mix", "name": "Local Mix", "track_ids": ["local_A/one.flac", "local_C/two.flac"], "track_count": 2},
    {"id": "spare", "name": "Spare List", "track_ids": ["local_C/two.flac"], "track_count": 1},
]
TIDAL_PLAYLIST = {"id": "tp1", "name": "Tidal Mix", "track_count": 1, "art_url": "", "description": ""}
TIDAL_TRACK = {"id": "9001", "title": "Tidal Song", "artist": "Bravo", "album": "Tidal Album", "duration": 200, "art_url": ""}

STUB = f"""
window.__requests = [];
window.__playlists = {json.dumps(PLAYLISTS)};
window.__tidalPlaylists = ['tp1'];
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {{
    const u = String(url);
    const method = (opts && opts.method) || 'GET';
    if (method !== 'GET' || u.includes('/export')) window.__requests.push([method, u.replace(location.origin, '')]);
    const json = (data, status = 200) => Promise.resolve(new Response(JSON.stringify(data), {{
        status, headers: {{ 'Content-Type': 'application/json' }},
    }}));
    const crud = u.match(/\\/api\\/playlists\\/([^/]+)$/);
    if (crud && method === 'DELETE') {{
        window.__playlists = window.__playlists.filter(p => p.id !== decodeURIComponent(crud[1]));
        return json({{ status: 'ok' }});
    }}
    if (/\\/api\\/playlists\\/[^/]+\\/export$/.test(u)) {{
        return Promise.resolve(new Response('#EXTM3U\\n', {{
            status: 200,
            headers: {{ 'Content-Type': 'audio/x-mpegurl', 'Content-Disposition': 'attachment; filename="Local Mix.m3u8"' }},
        }}));
    }}
    if (u.endsWith('/api/playlists')) return json(window.__playlists);
    if (u.endsWith('/api/tracks')) return json({json.dumps(TRACKS)});
    if (u.endsWith('/api/library/status')) return json({{ ready: true, scanning: false }});
    if (u.endsWith('/api/albums')) return json({json.dumps(ALBUMS)});
    if (u.includes('/api/smart/top-tracks')) return json({json.dumps(TRACKS)});
    if (u.includes('/api/streaming/providers/discovery')) return json({{ providers: [{{
        id: 'tidal', name: 'Tidal', installed: true, available: true, enabled: true, capabilities: {{ catalog: true }} }}] }});
    if (u.includes('/api/streaming/tidal/status')) return json({{ installed: true, available: true, authenticated: true,
        user: {{ id: 'u1' }}, capabilities: {{}}, status: 'Stopped' }});
    if (u.includes('/api/streaming/tidal/library/snapshot')) return json({{ user_id: 'u1', ids: {{}} }});
    if (u.includes('/api/streaming/tidal/favorites/ids')) return json({{ tracks: ['9001'], albums: [], artists: [], playlists: window.__tidalPlaylists }});
    if (u.includes('/api/streaming/tidal/favorites?type=tracks')) return json({json.dumps([TIDAL_TRACK])});
    if (u.includes('/api/streaming/tidal/favorites')) return json([]);
    const tidalFavorite = u.match(/\\/api\\/streaming\\/tidal\\/playlists\\/([^/]+)\\/favorite$/);
    if (tidalFavorite && method === 'POST') {{
        const favorite = JSON.parse(opts.body).favorite;
        window.__tidalPlaylists = favorite ? ['tp1'] : [];
        return json({{ type: 'playlist', id: tidalFavorite[1], favorite }});
    }}
    if (/\\/api\\/streaming\\/tidal\\/playlists\\/tp1\\/tracks/.test(u)) return json({json.dumps([TIDAL_TRACK])});
    if (/\\/api\\/streaming\\/tidal\\/playlists\\/tp1$/.test(u)) return json({json.dumps(TIDAL_PLAYLIST)});
    if (u.includes('/api/streaming/tidal/playlists')) return json(window.__tidalPlaylists.length ? {json.dumps([TIDAL_PLAYLIST])} : []);
    if (u.includes('/api/streaming/')) return json({{}});
    if (u.endsWith('/api/stations')) return json([]);
    if (u.endsWith('/api/status')) return json({{ playing: false }});
    if (u.includes('/api/audio/')) return json({{}});
    if (u.includes('/api/dsp/')) return json({{ presets: [] }});
    if (u.includes('/api/')) return json({{}});
    return realFetch(url, opts);
}};
"""

# Back measured against its own hero header.
BACK_OFFSET = """sel => {
    const back = document.querySelector(sel);
    const header = back.closest('.detail-hero-header').getBoundingClientRect();
    const rect = back.getBoundingClientRect();
    return [Math.round(header.right - rect.right), Math.round(rect.top - header.top), Math.round(rect.height)];
}"""


def _run():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    passed = 0

    def check(name, condition):
        nonlocal passed
        assert condition, name
        passed += 1

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []

            # Only the modules under test count; the stubbed backend leaves
            # unrelated panels (DSP, measurement) with empty payloads.
            def _record(exc):
                stack = getattr(exc, "stack", "") or str(exc)
                if any(name in stack for name in ("library_ui.js", "streaming.js", "ui_helpers.js")):
                    errors.append(stack.splitlines()[0])

            page.on("pageerror", _record)
            dialogs = []
            answer = {"accept": False}

            def _dialog(dialog):
                dialogs.append(dialog.message)
                if answer["accept"]:
                    dialog.accept()
                else:
                    dialog.dismiss()

            page.on("dialog", _dialog)
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            # Toasts stack over the top-right Back; clicks go through them here.
            page.add_style_tag(content="#toast-container{pointer-events:none!important}")
            page.click("#tab-btn-library")

            def writes():
                return [r for r in page.evaluate("window.__requests") if r[0] != "GET"]

            # Tracks: single titles only, no playlist rows.
            page.click("#library-view-tracks")
            page.wait_for_selector('#tracks-list .track-item[data-track-id="local_C/two.flac"]')
            page.wait_for_selector("#tracks-list .streaming-result")
            check("Tracks lists no playlist rows",
                  page.locator("#tracks-list [data-playlist-id], #tracks-list .playlist-item").count() == 0)
            check("Tracks lists the local and the TIDAL titles",
                  page.locator("#tracks-list .track-item[data-track-id]").count() == 2)
            page.fill("#library-search", "Mix")
            page.click('#library-search-types [data-search-type="playlists"]')
            page.wait_for_selector('#albums-grid .playlist-card[data-playlist-id="local-mix"]')
            check("the search still finds the playlist under Playlists", True)
            page.click("#library-search-clear")

            # Local playlist detail: heart next to the title, export left of Back.
            page.click("#library-view-playlists")
            page.wait_for_selector('#albums-grid .playlist-card[data-playlist-id="local-mix"]')
            page.click('#albums-grid .playlist-card[data-playlist-id="local-mix"]')
            page.wait_for_selector("#playlist-detail:not(.hidden) #playlist-detail-tracks .track-item")
            heart = page.locator("#playlist-favorite-toggle")
            check("the local playlist heart sits in the title row, active",
                  heart.is_visible() and "active" in (heart.get_attribute("class") or "")
                  and page.locator("#playlist-detail .detail-hero-title-row #playlist-favorite-toggle").count() == 1)
            download = page.locator("#playlist-detail-download").bounding_box()
            back = page.locator("#playlist-detail-back").bounding_box()
            check("the export sits left of Back on the same top line",
                  download["x"] + download["width"] <= back["x"] and abs(download["y"] - back["y"]) < 1)
            local_back = page.evaluate(BACK_OFFSET, "#playlist-detail-back")

            page.click("#playlist-detail-download")
            page.wait_for_function("window.__requests.some(r => r[1].endsWith('/export'))")
            check("the export downloads the open playlist",
                  page.evaluate("window.__requests").count(["GET", "/api/playlists/local-mix/export"]) == 1)
            page.wait_for_function("Array.from(document.querySelectorAll('.toast')).some(t => t.textContent.includes('Downloading Local Mix.m3u8'))")

            heart.click()
            check(f"removing a local playlist asks first ({dialogs})", dialogs == ['Delete playlist "Local Mix"?'])
            page.wait_for_timeout(200)
            check("a dismissed question keeps the playlist", writes() == [] and page.locator("#playlist-detail").is_visible())

            # Local card heart: same question, same delete path.
            page.click("#playlist-detail-back")
            page.wait_for_selector('#albums-grid:not(.hidden) [data-playlist-fav="spare"]')
            page.click('#albums-grid [data-playlist-fav="spare"]')
            page.wait_for_timeout(200)
            check("the card heart asks the same question",
                  dialogs[-1] == 'Delete playlist "Spare List"?' and writes() == [])

            # Accepted from the detail: deleted, back to Playlists.
            page.click('#albums-grid .playlist-card[data-playlist-id="local-mix"]')
            page.wait_for_selector("#playlist-detail:not(.hidden)")
            answer["accept"] = True
            heart.click()
            page.wait_for_selector('#albums-grid:not(.hidden) [data-playlist-fav="spare"]')
            page.wait_for_function("!document.querySelector('#albums-grid [data-playlist-id=\"local-mix\"]')")
            check("an accepted heart deletes through the playlist endpoint",
                  writes() == [["DELETE", "/api/playlists/local-mix"]])
            check("the deleted playlist closes its detail and leaves Playlists",
                  page.locator("#playlist-detail").is_hidden()
                  and page.locator("#library-view-playlists.active").count() == 1)
            answer["accept"] = False

            # TIDAL card heart: asks before removing from the TIDAL favorites.
            page.wait_for_selector('#albums-grid [data-tidal-id="tp1"] .streaming-fav.is-active')
            page.click('#albums-grid [data-tidal-id="tp1"] .streaming-fav')
            page.wait_for_timeout(200)
            check(f"the TIDAL card heart asks first ({dialogs[-1]})",
                  dialogs[-1] == 'Remove playlist "Tidal Mix" from TIDAL favorites?'
                  and len(writes()) == 1
                  and page.locator('#albums-grid [data-tidal-id="tp1"] .streaming-fav.is-active').count() == 1)

            # TIDAL playlist detail: heart next to the title, no export.
            page.click('#albums-grid [data-tidal-id="tp1"]')
            page.wait_for_selector("#library-tidal-detail:not(.hidden) #tidal-detail-results .streaming-result")
            tidal_heart = page.locator('#library-tidal-detail .detail-hero-title-row .album-favorite-toggle[data-fav-type="playlists"]')
            check("the TIDAL playlist heart sits in the title row, active",
                  tidal_heart.is_visible() and "active" in (tidal_heart.get_attribute("class") or ""))
            check("a TIDAL playlist offers no export",
                  page.locator("#playlist-detail-download").is_hidden()
                  and page.locator("#library-tidal-detail .detail-hero-action").count() == 0)
            tidal_back = page.evaluate(BACK_OFFSET, "#tidal-detail-back")
            check(f"Back keeps the spot of the TIDAL playlist Back ({local_back} vs {tidal_back})",
                  local_back == tidal_back)
            tidal_heart.click()
            page.wait_for_timeout(200)
            check("the TIDAL detail heart asks first and a dismissal keeps it",
                  dialogs[-1] == 'Remove playlist "Tidal Mix" from TIDAL favorites?'
                  and len(writes()) == 1 and "active" in (tidal_heart.get_attribute("class") or ""))
            answer["accept"] = True
            tidal_heart.click()
            page.wait_for_function("document.querySelector('#library-tidal-detail .album-favorite-toggle[data-fav-type=\"playlists\"]:not(.active)')")
            check("an accepted TIDAL heart removes the playlist from the TIDAL favorites",
                  writes()[-1][0] == "POST" and writes()[-1][1] == "/api/streaming/tidal/playlists/tp1/favorite")
            answer["accept"] = False
            page.click("#tidal-detail-back")
            page.wait_for_function("!document.querySelector('#albums-grid [data-tidal-id=\"tp1\"]')")
            check("the removed TIDAL playlist leaves Playlists", True)

            # Smart Mixes: no heart, no export.
            for smart, title in (("top40", "Top 40"), ("favorite-tracks", "Favorite Tracks")):
                page.click(f'#albums-grid [data-smart-favorite="{smart}"]')
                page.wait_for_function(f"document.querySelector('#album-detail-name').textContent === '{title}'")
                check(f"{title} keeps no heart and no export",
                      page.locator("#album-favorite-toggle").is_hidden()
                      and page.locator("#playlist-detail-download").is_hidden()
                      and page.locator("#album-detail .detail-hero-action").count() == 0)
                page.click("#album-detail-back")
                page.wait_for_selector('#albums-grid:not(.hidden) [data-smart-favorite="top40"]')

            check(f"no page errors ({errors})", not errors)
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_library_playlist_management.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_library_playlist_management.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print("FAIL  scripts/test_library_playlist_management.py")
        print(f"  {exc}")
        sys.exit(1)
