#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Real-input check for the Crossover graph cutoff drag.

Serves the live worktree demo (scripts/serve_demo.py) and drives it in
headless Chromium with a genuine mouse: press on the painted cutoff line of
the 2-way Low way (3 kHz in the seeded demo), drag it, and check the line,
the frequency field and the stored state. The sub-owned 80 Hz line and empty
plot space must not act as handles, on desktop and phone widths.

    python3 scripts/check_crossover_graph_drag.py [--url http://localhost:8765]
"""

import argparse
import math
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8765


def wait_for_server(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def line_x(canvas_width: float, hz: float) -> float:
    """Canvas-relative x of a cutoff line (same geometry as crossover_view.js)."""
    plot_w = max(1, max(320, round(canvas_width)) - 56 - 24)
    return 56 + (math.log10(hz) - math.log10(20)) / (math.log10(20000) - math.log10(20)) * plot_w


def open_tile(browser, url: str, viewport: dict):
    """Open the DSP tab with the crossover tile settled and scrolled into view.

    Pins the Low way: a machine left on another way tab would otherwise make
    the check drive that way's line instead.
    """
    page = browser.new_page(viewport=viewport)
    page.goto(url, wait_until="load")
    page.locator("#tab-btn-effects").click()
    page.wait_for_selector('#effects-crossover-tabs [data-crossover-way="left_low"]', timeout=15000)
    page.locator('#effects-crossover-tabs [data-crossover-way="left_low"]').click()
    page.wait_for_function(
        "() => !!document.getElementById('effects-crossover-frequency-lowpass')?.value")
    graph = page.locator("#effects-crossover-graph")
    graph.scroll_into_view_if_needed()
    return page, graph


def stored_sub_crossover(page):
    """The sub crossover frequency the Low way's derived high-pass shows."""
    return page.evaluate(
        """() => fetch('/api/audio/output-state').then(r => r.json()).then((s) => {
            const mode = s.modes[s.active_mode];
            const bass = mode?.bass_management;
            const subs = (mode?.topology?.sub_roles || []).length;
            return bass && subs && bass.main_highpass_enabled === true
                ? (bass.frequency_hz ?? null) : null;
        })""")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=f"http://localhost:{PORT}")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP  check_crossover_graph_drag.py (playwright not installed)")
        return 0

    server = None
    if not wait_for_server(args.url, timeout=1.0):
        server = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "serve_demo.py")],
                                  cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for_server(args.url):
            print("FAIL  demo server did not start")
            return 1

    failures: list[str] = []
    problems: list[str] = []

    def check(condition: bool, message: str) -> None:
        if condition:
            print(f"ok    {message}")
        else:
            failures.append(message)
            print(f"FAIL  {message}")

    def has_class(locator, name: str) -> bool:
        return locator.evaluate("(el, cls) => el.classList.contains(cls)", name)

    try:
        with sync_playwright() as play:
            browser = play.chromium.launch()

            page, graph = open_tile(browser, args.url, {"width": 1600, "height": 1000})
            page.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
            page.on("console", lambda message: problems.append(f"console.{message.type}: {message.text}")
                    if message.type == "error" else None)
            box = graph.bounding_box()
            if not box or box["width"] < 200:
                print("FAIL  crossover graph is visible")
                browser.close()
                return 1
            width = box["width"]

            # The Low way of the demo's 2-way system: a stored low-pass cutoff
            # (3 kHz in the seeded demo) and the sub-owned high-pass it
            # displays read-only. A machine left over from an earlier run may
            # carry different values, so the drag starts from what it holds.
            low = page.locator("#effects-crossover-frequency-lowpass")
            high = page.locator("#effects-crossover-frequency-highpass")
            start_hz = int(low.input_value())
            sub_hz = stored_sub_crossover(page)
            check(200 <= start_hz <= 20000, f"Low way runs a stored low-pass ({start_hz} Hz)")
            if sub_hz is None:
                check(True, "no derived sub high-pass on this system (nothing to keep read-only)")
            else:
                check(high.is_disabled() and high.input_value() == str(sub_hz),
                      f"the {sub_hz} Hz sub high-pass is displayed read-only")
            derived_hz = sub_hz or 80
            target_hz = max(20, round(start_hz / 1.5))

            def page_x(hz: float) -> float:
                return box["x"] + line_x(width, hz)

            mid = box["y"] + box["height"] / 2
            page.mouse.move(page_x(start_hz) + 6, mid)
            check(has_class(graph, "is-handle-hover"),
                  f"hovering the {start_hz} Hz line shows the resize cursor")
            check(not has_class(graph, "is-handle-drag"), "hovering does not start a drag")

            # Press on the line and drag it left.
            page.mouse.down()
            check(has_class(graph, "is-handle-drag"), "pressing the line starts the drag")
            for step in range(1, 7):
                page.mouse.move(page_x(start_hz * (target_hz / start_hz) ** (step / 6)), mid)
            check(low.input_value() != str(start_hz),
                  f"the frequency field follows the drag ({low.input_value()} Hz)")
            dragged_value = low.input_value()
            page.mouse.up()
            check(not has_class(graph, "is-handle-drag"), "releasing ends the drag")
            # Wait for the store, not the field: the field already shows the
            # dragged value while the release is still in flight.
            page.wait_for_function(
                """(target) => fetch('/api/audio/output-state').then(r => r.json()).then((s) => {
                    const way = s.modes[s.active_mode].processing.left_low?.lowpass;
                    return !!way && Math.abs(way.frequency_hz - target) <= 1;
                })""",
                arg=int(dragged_value), timeout=15000)
            stored = page.evaluate(
                "() => fetch('/api/audio/output-state').then(r => r.json())"
                ".then(s => s.modes[s.active_mode].processing.left_low.lowpass.frequency_hz)")
            check(abs(int(stored) - int(dragged_value)) <= 1,
                  f"the dragged frequency is stored ({stored} Hz)")
            # The released drag is frozen and the pointer is free again: the
            # sub-owned line is a painted reference, never a handle.
            page.mouse.move(page_x(derived_hz), mid)
            check(not has_class(graph, "is-handle-hover"),
                  "hover feedback resumes after the drag, and the derived line shows no resize cursor")
            page.mouse.down()
            page.mouse.move(page_x(derived_hz * 4), mid)
            page.mouse.up()
            check(low.input_value() == str(stored), "dragging the derived line changes nothing")

            # Plot space away from any line is not a handle either.
            page.mouse.move(page_x(12000), mid)
            check(not has_class(graph, "is-handle-hover"), "empty plot space is not a handle")
            check(not problems, f"no page errors while dragging ({problems[:2]})")
            page.close()

            # Phone width: the same line stays grabbable on the narrowest
            # layout the tile is used on.
            phone, phone_graph = open_tile(browser, args.url, {"width": 390, "height": 844})
            phone_box = phone_graph.bounding_box()
            if not phone_box or phone_box["width"] < 200:
                print("FAIL  crossover graph is visible at 390px")
                browser.close()
                return 1
            phone_low = phone.locator("#effects-crossover-frequency-lowpass")
            phone_hz = int(phone_low.input_value())
            phone_mid = phone_box["y"] + phone_box["height"] / 2
            phone_line = phone_box["x"] + line_x(phone_box["width"], phone_hz)
            phone.mouse.move(phone_line + 6, phone_mid)
            check(has_class(phone_graph, "is-handle-hover"),
                  f"390px: the {phone_hz} Hz line is grabbable")
            phone.mouse.down()
            phone.mouse.move(phone_line + 40, phone_mid)
            phone_dragged = phone_low.input_value()
            phone.mouse.up()
            phone.wait_for_function(
                """(target) => fetch('/api/audio/output-state').then(r => r.json()).then((s) => {
                    const way = s.modes[s.active_mode].processing.left_low?.lowpass;
                    return !!way && Math.abs(way.frequency_hz - target) <= 1;
                })""",
                arg=int(phone_dragged), timeout=15000)
            check(int(phone_dragged) != phone_hz,
                  f"390px: the drag moved the cutoff to {phone_dragged} Hz")
            browser.close()
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)

    if failures:
        print(f"\ncrossover graph drag check: {len(failures)} failed")
        return 1
    print("\ncrossover graph drag check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
