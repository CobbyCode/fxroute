#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Rendered contract for the favorite hearts (inline SVG painted from currentColor).

The favorite hearts used to be the Unicode glyphs U+2665 / U+2661. Some
browser/OS combinations resolve those through a colour-emoji font, which paints
an active heart red no matter what the button's CSS colour says. Every favorite
surface now renders the shared ``.fav-heart`` SVG, so the existing muted /
accent button states stay the single source of truth and every browser draws
the same heart.

Loads the real page (static server + stubbed audio API) in headless Chromium,
builds one probe button per favorite primitive in both states — inside the same
wrapper the real surfaces use (e.g. the album-detail hero) — and pins that:

  * the button contains ``svg.fav-heart`` and no text glyph at all,
  * idle hearts are outline only (``fill: none``) in the button's muted colour,
  * active hearts are filled in the FXRoute accent green,
  * fill and stroke are inherited through ``currentColor`` (nothing baked into
    the SVG attributes), so a CSS colour change still moves the heart,
  * the demo dist ships that same markup, helper and CSS — no demo special case.

Skips cleanly when playwright or a browser is not available.
"""

import http.server
import pathlib
import re
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8198

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_favorite_heart_render.py (playwright not installed)")
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


STUB = """
const realFetch = window.fetch.bind(window);
window.fetch = (url, opts) => {
    const u = String(url);
    const json = (d) => Promise.resolve(new Response(JSON.stringify(d), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    if (u.includes('/api/audio/samplerate')) {
        return json({ available: true, active_rate: 44100, supported_rates: [44100, 48000] });
    }
    if (u.includes('/api/streaming/')) return json({ installed: false, available: false });
    return realFetch(url, opts);
};
"""

# One idle + one active probe per favorite primitive. The album detail heart is
# wrapped in its real hero container, which is what the app and the TIDAL
# detail render (the heart colours are scoped through that wrapper).
PROBE_JS = """
(() => {
    switchTab('library');
    const loader = document.querySelector('.content-state--loading');
    if (loader) loader.style.display = 'none';
    const cases = [
        ['track-row-favorite', 'active', ''],
        ['track-fav', 'active', ''],
        ['track-fav', 'is-active', ''],
        ['streaming-fav', 'active', ''],
        ['streaming-fav', 'is-active', ''],
        ['station-card-fav', 'is-active', ''],
        ['album-card-fav', 'is-active', ''],
        ['track-favorite-btn', 'active', ''],
        // The album/playlist detail heart lives in the hero wrapper, but its
        // base rule must be accent green too: no unwrapped surface may ever
        // fall back to a yellow heart.
        ['album-favorite-toggle', 'active', ''],
        ['album-favorite-toggle', 'active', 'album-detail--hero'],
    ];
    const host = document.createElement('div');
    host.id = 'heart-probe';
    host.style.cssText = 'position:fixed;top:0;left:0;z-index:0;';
    const build = (cls, stateClass) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = cls + (stateClass ? ' ' + stateClass : '');
        button.setAttribute('data-heart-case', cls + '|' + (stateClass || 'idle'));
        // The app's own helper: a probe that fails here means app.js, radio.js
        // and streaming.js no longer share one heart implementation.
        button.innerHTML = favoriteHeartSvg();
        return button;
    };
    for (const [cls, stateClass, wrapper] of cases) {
        const idle = build(cls, '');
        const active = build(cls, stateClass);
        if (wrapper) {
            for (const button of [idle, active]) {
                const wrap = document.createElement('div');
                wrap.className = wrapper;
                wrap.appendChild(button);
                host.appendChild(wrap);
            }
        } else {
            host.append(idle, active);
        }
    }
    document.body.appendChild(host);
    window.__heartCases = () => {
        const probe = document.createElement('span');
        probe.style.color = 'var(--accent)';
        document.body.appendChild(probe);
        const accent = getComputedStyle(probe).color;
        probe.remove();
        const measured = [...document.querySelectorAll('[data-heart-case]')].map((button) => {
            const svg = button.querySelector('svg.fav-heart');
            const style = svg ? getComputedStyle(svg) : null;
            return {
                name: button.getAttribute('data-heart-case'),
                hasSvg: !!svg,
                pathLength: svg ? ((svg.querySelector('path') || {}).getAttribute ? (svg.querySelector('path').getAttribute('d') || '').length : 0) : 0,
                text: (button.textContent || '').trim(),
                fillAttr: svg ? svg.getAttribute('fill') : 'missing-svg',
                strokeAttr: svg ? svg.getAttribute('stroke') : 'missing-svg',
                fill: style ? style.fill : '',
                stroke: style ? style.stroke : '',
                color: getComputedStyle(button).color,
                width: svg ? svg.getBoundingClientRect().width : 0,
            };
        });
        return { accent, measured };
    };
})();
"""


def _run():
    server = _serve()
    passed = 0

    def check(name, condition):
        assert condition, name
        nonlocal passed
        passed += 1

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 900, "height": 900})
            page.add_init_script(STUB)
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(PROBE_JS)
            page.wait_for_timeout(200)
            data = page.evaluate("() => window.__heartCases()")
            page.close()
            browser.close()
    finally:
        server.shutdown()

    accent = data["accent"]
    cases = data["measured"]
    check("every favorite primitive rendered both states", len(cases) == 20)

    for case in cases:
        name = case["name"]
        # The probe names every button "<class>|<state>"; the state class is
        # either the idle marker or the surface's own active class ("active"
        # for the library primitives, "is-active" for the TIDAL/station ones).
        active = name.split("|", 1)[1] != "idle"
        check(f"{name}: heart renders as svg.fav-heart", case["hasSvg"])
        check(f"{name}: heart path is present", case["pathLength"] > 20)
        check(f"{name}: no unicode heart glyph in the button", case["text"] == "")
        check(f"{name}: heart is visible", case["width"] > 4)
        check(f"{name}: stroke follows currentColor", case["stroke"] == case["color"])
        check(
            f"{name}: no colour baked into the svg attributes",
            case["fillAttr"] is None and case["strokeAttr"] is None,
        )
        if active:
            check(f"{name}: active heart uses the accent green {accent}", case["color"] == accent)
            check(f"{name}: active heart is filled", case["fill"] == accent)
        else:
            check(f"{name}: idle heart is outline only", case["fill"] == "none")

    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    check("app shares one heart helper", "favoriteHeartSvg" in app_js)
    check("app ships no unicode heart glyph", "\u2665" not in app_js and "\u2665" not in index)

    # radio.js and streaming.js keep a standalone default (same pattern as
    # escapeHtml) and take the app implementation through init(). Pin both to
    # the app's path so the three copies can never drift apart visually.
    path_match = re.search(r'<path d="([^"]+)"/>', app_js)
    check("app heart is one inline svg path", path_match is not None)
    heart_path = path_match.group(1)
    for module in ("radio.js", "streaming.js"):
        module_js = (ROOT / "static" / module).read_text(encoding="utf-8")
        check(f"{module} default heart matches the app heart", heart_path in module_js)
        check(f"{module} takes the app heart in init", "api.favoriteHeartSvg" in module_js)

    dist_index = (ROOT / "demo" / "dist" / "index.html").read_text(encoding="utf-8")
    dist_app = (ROOT / "demo" / "dist" / "static" / "app.js").read_text(encoding="utf-8")
    dist_css = (ROOT / "demo" / "dist" / "static" / "style.css").read_text(encoding="utf-8")
    dist_base = (ROOT / "demo" / "dist" / "static" / "css" / "_base.css").read_text(encoding="utf-8")
    check("demo ships the shared svg heart markup", 'svg class="fav-heart"' in dist_index)
    check("demo ships the shared heart helper", "favoriteHeartSvg" in dist_app)
    check("demo ships the heart CSS", ".fav-heart" in dist_css and ".fav-heart" in dist_base)
    check("demo ships no unicode heart glyph", "\u2665" not in dist_app and "\u2665" not in dist_index)

    print(f"PASS  scripts/test_favorite_heart_render.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print("FAIL  scripts/test_favorite_heart_render.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print("FAIL  scripts/test_favorite_heart_render.py")
        print(f"  {exc}")
        sys.exit(1)
