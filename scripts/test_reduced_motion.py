#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Playwright accessibility regression check for prefers-reduced-motion.

The generic reduced-motion override (animation-duration 0.01ms !important,
iteration-count 1 !important) must win against every specific animation rule
in the stylesheet. The loading spinner (.content-state--loading::before) used
to keep running at 1.6s infinite under reduced motion; it must collapse to a
single near-instant iteration instead.

Emulates the media feature in headless Chromium. Skips cleanly when
playwright or a browser is not available.
"""

import http.server
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8196

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP  scripts/test_reduced_motion.py (playwright not installed)")
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

# Every animation selector in the stylesheet, checked for the override.
# Selectors whose elements only exist in certain views are injected by the
# page script when missing so the computed style is still exercised.
ANIMATED_SELECTORS = [
    ".content-state--loading",             # 1.6s ease-in-out infinite (::before)
    ".tab-panel.active",                   # fadeIn 0.25s
    ".banner",                             # bannerSlide 0.3s
    ".btn-spin",                           # btn-spin 0.7s linear infinite
    ".settings-inline-note.switching",     # switching-pulse 1.2s infinite
    ".hybrid-status .spinner",             # hybrid-status-spin 0.8s infinite
]

# Elements not present in the initial DOM are injected so the computed
# animation styles can be read; pseudo-element styles are read via
# getComputedStyle(el, '::before').
INJECT = """
(() => {
    const make = (html) => {
        const el = document.createElement('div');
        el.innerHTML = html;
        return el.firstElementChild;
    };
    if (!document.querySelector('.content-state--loading')) {
        const b = make('<div class="content-state--loading"></div>');
        document.body.appendChild(b);
    }
    if (!document.querySelector('.banner')) {
        const b = make('<div class="banner"></div>');
        document.body.appendChild(b);
    }
    if (!document.querySelector('.btn-spin')) {
        const b = make('<button class="btn-spin"></button>');
        document.body.appendChild(b);
    }
    if (!document.querySelector('.settings-inline-note.switching')) {
        const b = make('<div class="settings-inline-note switching"></div>');
        document.body.appendChild(b);
    }
    if (!document.querySelector('.hybrid-status .spinner')) {
        const b = make('<div class="hybrid-status"><span class="spinner"></span></div>');
        document.body.appendChild(b);
    }
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
            # The reduced-motion media feature is emulated on the context so
            # the page never sees a non-reduced state.
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                reduced_motion="reduce",
            )
            page = context.new_page()
            page.add_init_script(STUB)
            # Only static/index.html is served in production.
            page.goto(f"http://127.0.0.1:{PORT}/static/index.html")
            page.wait_for_selector("#playback-bar", state="visible")
            page.wait_for_timeout(300)
            page.evaluate(INJECT)

            check("reduced motion is emulated", page.evaluate(
                "matchMedia('(prefers-reduced-motion: reduce)').matches"
            ))

            for sel in ANIMATED_SELECTORS:
                base = sel.split("::")[0]
                pseudo = "::" + sel.split("::")[1] if "::" in sel else None
                duration = page.evaluate(
                    "(args) => getComputedStyle(document.querySelector(args[0]), args[1]).animationDuration",
                    [base, pseudo],
                )
                iters = page.evaluate(
                    "(args) => getComputedStyle(document.querySelector(args[0]), args[1]).animationIterationCount",
                    [base, pseudo],
                )
                check(
                    f"[{sel}] animation collapsed to 0.01ms (got {duration})",
                    duration == "0.01ms" or duration == "1e-05s",
                )
                check(
                    f"[{sel}] iteration count is 1 (got {iters})",
                    iters == "1",
                )

            page.close()
            context.close()
            browser.close()
    finally:
        server.shutdown()

    print(f"PASS  scripts/test_reduced_motion.py ({passed} checks)")


if __name__ == "__main__":
    try:
        _run()
    except AssertionError as exc:
        print(f"FAIL  scripts/test_reduced_motion.py")
        print(f"  {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 - report any browser/run failure
        print(f"FAIL  scripts/test_reduced_motion.py")
        print(f"  {exc}")
        sys.exit(1)
