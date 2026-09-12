#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Verify the Library folder breadcrumb and one-level Back navigation."""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8220

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_library_folder_back.py (playwright not installed)")
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
    {"id": "local_Records/Live/first.flac", "title": "First", "path": "Records/Live/first.flac"},
    {"id": "local_Records/Live/second.flac", "title": "Second", "path": "Records/Live/second.flac"},
    {"id": "local_Records/Studio/master.flac", "title": "Master", "path": "Records/Studio/master.flac"},
]

STUB = f"""
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {{
    const u = String(url);
    const json = (data) => Promise.resolve(new Response(JSON.stringify(data), {{
        status: 200,
        headers: {{ 'Content-Type': 'application/json' }},
    }}));
    if (u.endsWith('/api/tracks')) return json({TRACKS!r});
    if (u.endsWith('/api/library/status')) return json({{ ready: true, scanning: false }});
    if (u.endsWith('/api/playlists')) return json([]);
    if (u.includes('/api/albums')) return json([]);
    if (u.endsWith('/api/stations')) return json([]);
    if (u.endsWith('/api/status')) return json({{ playing: false }});
    if (u.includes('/api/audio/')) return json({{}});
    if (u.includes('/api/dsp/')) return json({{ presets: [] }});
    return realFetch(url, opts);
}};
"""


def _run():
    html = (ROOT / "static" / "index.html").read_text()
    app_js = (ROOT / "static" / "app.js").read_text()
    css = (ROOT / "static" / "style.css").read_text()
    assert 'id="library-folder-path"' in html, "Library folder path markup is missing"
    render_path_start = app_js.index("function renderLibraryFolderPath()")
    render_path_end = app_js.index("function formatLibraryScanStatus()", render_path_start)
    render_path = app_js[render_path_start:render_path_end]
    assert "library-folder-back" in render_path, "Folder renderer has no Back button"
    assert "slice(0, -1).join('/')" in render_path, "Back must remove exactly one folder level"
    assert ".library-folder-back" in css, "Folder Back button has no scoped style"
    assert "margin-left: auto" in css, "Folder Back button is not in the right action zone"

    server = _serve()
    passed = 0

    def check(name, condition):
        nonlocal passed
        assert condition, name
        passed += 1

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.click("#tab-btn-library")
            page.wait_for_selector("#library-view-folders")
            page.click("#library-view-folders")
            page.wait_for_function("document.querySelectorAll('#tracks-list .folder-item').length > 0")

            # Desktop offers the round play button in the folder row; phones
            # hide it (054107f) and open the folder through the row itself.
            # Each viewport drives the affordance its breakpoint actually
            # offers, so a hidden button is asserted, never clicked.
            for width, height, via_row in ((1440, 900, False), (390, 844, True)):
                page.set_viewport_size({"width": width, "height": height})
                page.click("#library-view-folders")
                page.wait_for_selector('#tracks-list .folder-item[data-folder="Records"]')
                check(f"[{width}px] root has no Back button", page.locator("#library-folder-back").count() == 0)
                check(f"[{width}px] root breadcrumb is visible", page.locator("#library-folder-path").inner_text() == "Music root")

                folder_play = page.locator('#tracks-list .track-play[data-folder="Records"]')
                if via_row:
                    check(f"[{width}px] phone row hides the separate play button", not folder_play.is_visible())
                    # The row itself is the tap trigger on phones; the title is
                    # inside the row and away from the folder action buttons.
                    page.click('#tracks-list .folder-item[data-folder="Records"] .track-title')
                else:
                    check(f"[{width}px] desktop keeps the round play button", folder_play.is_visible())
                    folder_play.click()
                page.wait_for_selector("#library-folder-back")
                page.wait_for_selector('#tracks-list .folder-item[data-folder="Records/Live"]')
                breadcrumb = page.locator("#library-folder-path").inner_text()
                check(f"[{width}px] child path keeps the breadcrumb ({breadcrumb!r})", breadcrumb.splitlines()[:3] == ["Music root", "/", "Records"])
                path_box = page.locator("#library-folder-path").bounding_box()
                back_box = page.locator("#library-folder-back").bounding_box()
                check(f"[{width}px] Back stays in the folder action zone", back_box["x"] + back_box["width"] <= path_box["x"] + path_box["width"] + 1)

                page.click("#library-folder-back")
                page.wait_for_selector('#tracks-list .folder-item[data-folder="Records"]')
                check(f"[{width}px] Back returns exactly to Music root", page.locator("#library-folder-path").inner_text() == "Music root")
                check(f"[{width}px] Back disappears at root", page.locator("#library-folder-back").count() == 0)

            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_library_folder_back.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_library_folder_back.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report browser setup failures
        print("FAIL  scripts/test_library_folder_back.py")
        print(f"  {exc}")
        sys.exit(1)
