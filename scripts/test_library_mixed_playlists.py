#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playlists that mix local tracks and TIDAL references in the Library (headless Chromium).

Covers:

* a mixed selection saves one local playlist in the order the tracks were
  added (local ids and ``tidal:<id>`` references interleaved) with the
  references' display data, and both selections end;
* such a playlist keeps its order, carries the TIDAL glyph on its card and row
  and plays as one queue (``queue_refs``) from a local or a TIDAL row;
* what the server had to leave out is told to the user ("Starting 2/3 ...");
* Favorite Tracks plays its local and TIDAL tracks as one queue;
* the M3U8 export names the TIDAL titles it leaves out;
* not connected: TIDAL entries stay listed (dimmed, from the saved data) and a
  server refusal reads "Connect TIDAL ...";
* TIDAL not installed: the entries stay listed but cannot be played.
"""

import http.server
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8255

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_mixed_playlists.py (playwright not installed)")
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
     "album_artist": "Alpha", "path": "A/one.flac", "favorite": True, "source": "local", "duration": 100},
    {"id": "local_C/two.flac", "title": "Local Two", "artist": "Charlie", "album": "Charlie Album",
     "album_artist": "Charlie", "path": "C/two.flac", "favorite": False, "source": "local", "duration": 120},
]
ALBUMS = [
    {"id": "alb-a", "name": "Alpha Album", "artist": "Alpha", "track_count": 1, "favorite": True},
    {"id": "alb-c", "name": "Charlie Album", "artist": "Charlie", "track_count": 1, "favorite": False},
]
TIDAL_TRACK = {"id": "9001", "title": "Tidal Song", "artist": "Bravo", "album": "Tidal Album",
               "duration": 200, "art_url": ""}
ROAD_MIX = {
    "id": "road-mix", "name": "Road Mix", "track_count": 3,
    "track_ids": ["local_A/one.flac", "tidal:9001", "local_C/two.flac"],
    "track_meta": {"tidal:9001": {"title": "Tidal Song", "artist": "Bravo", "album": "Tidal Album", "duration": 200}},
}
LOCAL_MIX = {"id": "local-mix", "name": "Local Mix", "track_ids": ["local_A/one.flac"], "track_count": 1}


def _stub(tidal_mode, play_status=200):
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
window.__plays = [];
window.__saved = [];
window.__summary = null;
window.__playlists = {json.dumps([ROAD_MIX, LOCAL_MIX])};
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {{
    const u = String(url);
    const method = (opts && opts.method) || 'GET';
    const json = (data, status = 200, headers = {{}}) => Promise.resolve(new Response(JSON.stringify(data), {{
        status,
        headers: Object.assign({{ 'Content-Type': 'application/json' }}, headers),
    }}));
    if (u.endsWith('/api/tracks')) return json({json.dumps(TRACKS)});
    if (u.endsWith('/api/library/status')) return json({{ ready: true, scanning: false }});
    if (u.endsWith('/api/playlists') && method === 'POST') {{
        const body = JSON.parse(opts.body);
        window.__saved.push(body);
        const playlist = Object.assign({{ id: 'saved-' + window.__saved.length, track_count: body.track_ids.length }}, body);
        window.__playlists = window.__playlists.concat([playlist]);
        return json({{ status: 'ok', playlist }});
    }}
    if (u.endsWith('/api/playlists')) return json(window.__playlists);
    if (/\\/api\\/playlists\\/[^/]+\\/export$/.test(u)) {{
        return Promise.resolve(new Response('#EXTM3U\\n', {{
            status: 200,
            headers: {{ 'Content-Type': 'audio/x-mpegurl', 'Content-Disposition': 'attachment; filename="Road-Mix.m3u8"', 'X-FXRoute-Skipped-Tidal': '1' }},
        }}));
    }}
    if (u.endsWith('/api/play') && method === 'POST') {{
        window.__plays.push(JSON.parse(opts.body));
        if ({play_status} !== 200) return json({{ detail: 'TIDAL is not authenticated' }}, {play_status});
        const body = JSON.parse(opts.body);
        const track = {{ id: body.track_id, title: 'Playing', artist: 'X', source: body.source }};
        const data = {{ status: 'playing', track, playback: {{ current_track: track, queue: {{ count: 3 }} }} }};
        if (window.__summary) data.queue_summary = window.__summary;
        return json(data);
    }}
    if (u.endsWith('/api/albums')) return json({json.dumps(ALBUMS)});
    if (u.includes('/api/streaming/providers/discovery')) return json({json.dumps(discovery)});
    if (u.includes('/api/streaming/tidal/status')) return json({json.dumps(status)});
    if (u.includes('/api/streaming/tidal/library/snapshot')) return json({{ user_id: 'u1', ids: {{}} }});
    if (u.includes('/api/streaming/tidal/favorites/ids')) return json({{ tracks: ['9001'], albums: [], artists: [], playlists: [] }});
    if (u.includes('/api/streaming/tidal/favorites?type=tracks')) return json({json.dumps([TIDAL_TRACK])});
    if (u.includes('/api/streaming/tidal/favorites')) return json([]);
    if (u.includes('/api/streaming/tidal/playlists') && !u.includes('/tracks')) return json([]);
    if (u.includes('/api/streaming/')) return json({{}});
    if (u.endsWith('/api/stations')) return json([]);
    if (u.endsWith('/api/status')) return json({{ playing: false }});
    if (u.includes('/api/audio/')) return json({{}});
    if (u.includes('/api/dsp/')) return json({{ presets: [] }});
    return realFetch(url, opts);
}};
"""


def _open_library(browser, tidal_mode, play_status=200):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []

    def _record(exc):
        # Only errors raised inside the modules under test count (top frame);
        # the stubbed backend leaves unrelated panels with empty payloads.
        lines = (getattr(exc, "stack", "") or str(exc)).splitlines()
        top = lines[1] if len(lines) > 1 else ""
        if any(name in top for name in ("library_ui.js", "streaming.js", "ui_helpers.js", "playback_core.js")):
            errors.append(lines[0])

    page.on("pageerror", _record)
    page.add_init_script(_stub(tidal_mode, play_status))
    page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
    page.click("#tab-btn-library")
    page.wait_for_selector("#library-view-albums")
    return page, errors


def _toasts(page):
    return page.eval_on_selector_all(".toast", "els => els.map(e => e.textContent)")


def _last_play(page):
    return page.evaluate("window.__plays[window.__plays.length - 1]")


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
            page.click("#library-view-tracks")
            page.wait_for_selector("#tracks-list .streaming-result")

            # A mixed selection saves one playlist with references.
            page.click('#tracks-list .track-item[data-track-id="local_C/two.flac"] .track-add')
            page.click("#tracks-list .streaming-result .streaming-add")
            page.wait_for_selector("#playlist-save-row:not(.hidden)")
            check("a mixed selection is saved with the plain Save button",
                  page.locator("#save-playlist").inner_text() == "Save"
                  and not page.locator("#save-playlist").is_disabled())
            page.fill("#playlist-name", "New Mix")
            page.click("#save-playlist")
            page.wait_for_function("window.__saved.length === 1")
            saved = page.evaluate("window.__saved[0]")
            check(f"the added order is kept ({saved['track_ids']})",
                  saved["track_ids"] == ["local_C/two.flac", "tidal:9001"])
            check("the TIDAL reference carries its display data",
                  saved["track_meta"] == {"tidal:9001": {"title": "Tidal Song", "artist": "Bravo",
                                                         "album": "Tidal Album", "duration": 200, "art_url": ""}})
            page.wait_for_selector("#playlist-save-row.hidden", state="attached")
            check("saving ends both selections",
                  page.locator("#tracks-list .track-add.is-active").count() == 0
                  and page.locator("#tracks-list .streaming-add.is-active").count() == 0)
            check("the playlist list shows the new mixed playlist with the TIDAL glyph",
                  page.locator('.playlist-item[data-playlist-id="saved-1"] .source-glyph--tidal').count() == 1
                  and page.locator('.playlist-item[data-playlist-id="local-mix"] .source-glyph').count() == 0)

            # Local and TIDAL tracks interleave: the order of adding wins, not
            # "local first".
            page.click('#tracks-list .track-item[data-track-id="local_C/two.flac"] .track-add')
            page.click("#tracks-list .streaming-result .streaming-add")
            page.click('#tracks-list .track-item[data-track-id="local_A/one.flac"] .track-add')
            page.wait_for_selector("#playlist-save-row:not(.hidden)")
            check("the save row explains the order",
                  "order you added" in page.locator("#playlist-save-row").inner_text())
            page.fill("#playlist-name", "Interleaved")
            page.click("#save-playlist")
            page.wait_for_function("window.__saved.length === 2")
            interleaved = page.evaluate("window.__saved[1]")
            check(f"TIDAL between the local tracks keeps its place ({interleaved['track_ids']})",
                  interleaved["track_ids"] == ["local_C/two.flac", "tidal:9001", "local_A/one.flac"])
            page.wait_for_selector("#playlist-save-row.hidden", state="attached")

            # Playlists: the card carries the glyph; the detail keeps the order.
            page.click("#library-view-playlists")
            page.wait_for_selector('#albums-grid .playlist-card[data-playlist-id="road-mix"]')
            check("a mixed playlist card carries the TIDAL glyph",
                  page.locator('.playlist-card[data-playlist-id="road-mix"] .source-glyph--tidal').count() == 1
                  and page.locator('.playlist-card[data-playlist-id="local-mix"] .source-glyph').count() == 0)
            page.click('.playlist-card[data-playlist-id="road-mix"]')
            page.wait_for_selector("#playlist-detail:not(.hidden) #playlist-detail-tracks .streaming-result")
            order = page.eval_on_selector_all(
                "#playlist-detail-tracks > *",
                "els => els.map(e => e.classList.contains('streaming-result') ? 'tidal' : e.dataset.trackId)")
            check(f"the detail lists the entries in playlist order ({order})",
                  order == ["local_A/one.flac", "tidal", "local_C/two.flac"])
            check("the playlist detail counts every entry",
                  page.locator("#playlist-detail-count").inner_text() == "3 tracks")
            check("the TIDAL entry carries the glyph and its saved title",
                  page.locator("#playlist-detail-tracks .streaming-result .source-glyph--tidal").count() == 1
                  and "Tidal Song" in page.locator("#playlist-detail-tracks .streaming-result").inner_text())
            check("a connected TIDAL entry is not dimmed",
                  page.locator("#playlist-detail-tracks .streaming-result.is-offline").count() == 0)

            # Playing: one queue by reference, from either kind of row.
            page.click("#playlist-detail-tracks .streaming-result .track-play")
            page.wait_for_function("window.__plays.length === 1")
            play = _last_play(page)
            check(f"a TIDAL row plays the whole playlist by reference ({play})",
                  play["source"] == "tidal" and play["track_id"] == "9001"
                  and play["queue_refs"] == ["local_A/one.flac", "tidal:9001", "local_C/two.flac"]
                  and "queue_track_ids" not in play)
            page.click('#playlist-detail-tracks .track-item[data-track-id="local_C/two.flac"] .track-play')
            page.wait_for_function("window.__plays.length === 2")
            play = _last_play(page)
            check("a local row plays the same queue starting at itself",
                  play["source"] == "local" and play["track_id"] == "local_C/two.flac"
                  and play["queue_refs"] == ["local_A/one.flac", "tidal:9001", "local_C/two.flac"])

            # What the server left out is told.
            page.evaluate("window.__summary = { requested: 3, queued: 2, skipped: { tidal_not_connected: 1 } }")
            page.click('#playlist-detail-tracks .track-item[data-track-id="local_A/one.flac"] .track-play')
            page.wait_for_function("window.__plays.length === 3")
            page.wait_for_function("Array.from(document.querySelectorAll('.toast')).some(t => t.textContent.includes('Starting 2/3'))")
            check("a skipped TIDAL entry is announced",
                  any(text == "Starting 2/3 tracks — TIDAL is not connected" for text in _toasts(page)))
            page.evaluate("window.__summary = null")

            # The M3U8 export says what it leaves out.
            page.click("#library-view-tracks")
            page.wait_for_selector('.playlist-item[data-playlist-id="road-mix"] .playlist-download-btn')
            page.click('.playlist-item[data-playlist-id="road-mix"] .playlist-download-btn')
            page.wait_for_function("Array.from(document.querySelectorAll('.toast')).some(t => t.textContent.includes('not included'))")
            check("the export toast names the omitted TIDAL tracks",
                  any("1 TIDAL track not included" in text for text in _toasts(page)))

            # Favorite Tracks plays local and TIDAL favorites as one queue.
            page.click("#library-view-playlists")
            page.click('#albums-grid [data-smart-favorite="favorite-tracks"]')
            page.wait_for_selector("#album-detail:not(.hidden) #album-detail-tracks .streaming-result")
            before = page.evaluate("window.__plays.length")
            page.click('#album-detail-tracks .track-item[data-track-id="local_A/one.flac"] .track-play')
            page.wait_for_function(f"window.__plays.length === {before + 1}")
            play = _last_play(page)
            check(f"a Favorite Tracks local row plays the mixed queue ({play})",
                  play["source"] == "local" and play["queue_refs"] == ["local_A/one.flac", "tidal:9001"])
            page.click("#album-detail-tracks .streaming-result .track-play")
            page.wait_for_function(f"window.__plays.length === {before + 2}")
            play = _last_play(page)
            check("a Favorite Tracks TIDAL row plays the same queue",
                  play["source"] == "tidal" and play["track_id"] == "9001"
                  and play["queue_refs"] == ["local_A/one.flac", "tidal:9001"])
            check(f"no page errors ({errors})", not errors)
            page.close()

            # --- installed, not connected -------------------------------------
            page, errors = _open_library(browser, "signed-out", play_status=401)
            page.click("#library-view-playlists")
            page.wait_for_selector('#albums-grid .playlist-card[data-playlist-id="road-mix"]')
            page.click('.playlist-card[data-playlist-id="road-mix"]')
            page.wait_for_selector("#playlist-detail-tracks .streaming-result")
            row = page.locator("#playlist-detail-tracks .streaming-result")
            check("without a connection the TIDAL entry stays listed from its saved data",
                  "Tidal Song" in row.inner_text() and "Bravo" in row.inner_text())
            check("without a connection the TIDAL entry is dimmed", row.evaluate("e => e.classList.contains('is-offline')"))
            page.click("#playlist-detail-tracks .streaming-result .track-play")
            page.wait_for_function("window.__plays.length === 1")
            page.wait_for_function("Array.from(document.querySelectorAll('.toast')).some(t => t.textContent.includes('Connect TIDAL'))")
            check("the queue still goes to the server, which leaves TIDAL out",
                  _last_play(page)["queue_refs"] == ["local_A/one.flac", "tidal:9001", "local_C/two.flac"])
            check("a refused TIDAL-only start reads Connect TIDAL",
                  any("Connect TIDAL to play these tracks" in text for text in _toasts(page)))
            check(f"no page errors ({errors})", not errors)
            page.close()

            # --- not installed -------------------------------------------------
            page, errors = _open_library(browser, "absent")
            page.click("#library-view-playlists")
            page.wait_for_selector('#albums-grid .playlist-card[data-playlist-id="road-mix"]')
            page.click('.playlist-card[data-playlist-id="road-mix"]')
            page.wait_for_selector("#playlist-detail-tracks .track-item.is-unavailable")
            check("without TIDAL its entries are listed but cannot be played",
                  "Tidal Song" in page.locator("#playlist-detail-tracks .track-item.is-unavailable").inner_text()
                  and page.locator("#playlist-detail-tracks .streaming-result").count() == 0)
            page.click('#playlist-detail-tracks .track-item[data-track-id="local_A/one.flac"] .track-play')
            page.wait_for_function("window.__plays.length === 1")
            check("the local entries still play the whole saved queue",
                  _last_play(page)["queue_refs"] == ["local_A/one.flac", "tidal:9001", "local_C/two.flac"])
            check(f"no page errors ({errors})", not errors)
            page.close()

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_library_mixed_playlists.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_library_mixed_playlists.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report browser setup failures
        print("FAIL  scripts/test_library_mixed_playlists.py")
        print(f"  {exc}")
        sys.exit(1)
