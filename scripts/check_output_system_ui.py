#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Browser smoke check for the Output System UI (settings, bank, tile).

Serves the live worktree demo (scripts/serve_demo.py) and drives it in
headless Chromium: opens settings, selects the multichannel device,
switches to Crossover, then verifies the bank selector and the Crossover
tile (tabs, graph, controls) on the DSP tab. Fails on uncaught page
errors or console errors from the new modules.

Run manually after output-system UI changes; not part of the test suite:

    python3 scripts/check_output_system_ui.py [--shots DIR]
"""

import argparse
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT = 8765


def wait_for_server(timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{PORT}/", timeout=2)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("demo server did not come up")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", default="/tmp/fxroute-os-shots")
    args = parser.parse_args()
    shots = pathlib.Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    server = subprocess.Popen(
        [sys.executable, "scripts/serve_demo.py"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    problems: list[str] = []
    try:
        wait_for_server()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("pageerror", lambda error: problems.append(f"pageerror: {error}"))
            page.on("console", lambda message: problems.append(f"console.{message.type}: {message.text}")
                    if message.type == "error" else None)
            page.goto(f"http://localhost:{PORT}/", wait_until="networkidle")
            page.wait_for_selector("#playback-bar", state="visible", timeout=15000)

            def check(name: str, condition: bool) -> None:
                print(("ok   " if condition else "FAIL ") + name)
                if not condition:
                    failures.append(name)

            # Settings overlay: Output System section renders.
            page.evaluate("toggleSettingsPanel(true)")
            page.wait_for_selector("#os-mode-select", state="visible", timeout=5000)
            check("os mode select visible", page.locator("#os-mode-select").is_visible())
            check("os routing grid present", page.locator("#os-routing-grid").count() == 1)

            # Multichannel device, then Crossover mode.
            # NOTE: Playwright select_option races the settings re-render
            # (rebuilt <option> nodes detach mid-action and the change is
            # lost); value+dispatch exercises the same app listener.
            def pick(selector, value):
                page.evaluate(
                    """([sel, val]) => {
                        const el = document.querySelector(sel);
                        el.value = val;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    [selector, value])

            scarlett_value = page.evaluate(
                """Array.from(document.querySelectorAll('#settings-output-select option'))
                    .find(o => o.textContent.includes('Scarlett'))?.value || ''""")
            check("scarlett device offered", bool(scarlett_value))
            if not scarlett_value:
                return 1
            pick("#settings-output-select", scarlett_value)
            page.wait_for_timeout(1500)
            pick("#os-mode-select", "crossover")
            page.wait_for_timeout(1500)
            topology = page.locator("#os-topology").inner_text()
            check(f"os topology mentions 3-Way ({topology})", "3-Way" in topology)
            page.screenshot(path=str(shots / "os-settings.png"))

            # DSP tab: bank selector and crossover tile.
            page.evaluate("toggleSettingsPanel(false)")
            page.evaluate("document.getElementById('tab-btn-effects').click()")
            page.wait_for_selector("#effects-bank-select", state="visible", timeout=5000)
            bank_options = page.locator("#effects-bank-select option").all_inner_texts()
            check(f"bank selector lists ways ({len(bank_options)} options)", len(bank_options) >= 7)
            info = page.locator("#effects-bank-info").inner_text()
            check(f"bank info shows preset ({info})", "Listening" in info)
            card = page.locator("#effects-crossover-card")
            check("crossover card visible", card.is_visible())
            tabs = page.locator("#effects-crossover-tabs button")
            check(f"six way tabs ({tabs.count()} found)", tabs.count() == 6)
            paths = page.locator("#effects-crossover-graph polyline")
            check(f"six graph paths ({paths.count()} found)", paths.count() == 6)
            check("family select populated",
                  page.locator("#effects-crossover-family option").count() >= 3)
            check("slope select populated",
                  page.locator("#effects-crossover-slope option").count() >= 2)
            page.screenshot(path=str(shots / "os-crossover.png"))

            # Switch the bank and the way tab; both must update without errors.
            pick("#effects-bank-select", "left_mid")
            page.wait_for_timeout(800)
            check("bank info follows selection",
                  "Room" in page.locator("#effects-bank-info").inner_text()
                  or "Neutral" in page.locator("#effects-bank-info").inner_text())
            page.locator("[data-crossover-way='right_high']").click()
            page.wait_for_timeout(400)
            check("way tab activates",
                  page.locator("[data-crossover-way='right_high']").get_attribute("class")
                  is not None
                  and "is-active" in (page.locator("[data-crossover-way='right_high']").get_attribute("class") or ""))

            browser.close()
    finally:
        server.terminate()
    relevant = [p for p in problems if "output" in p.lower() or "crossover" in p.lower()
                or "bank" in p.lower() or "pageerror" in p.lower()]
    for problem in problems:
        print("console:", problem[:220])
    if relevant:
        print(f"FAILED: {len(relevant)} relevant page/console errors")
        return 1
    if failures:
        print(f"FAILED: {len(failures)} checks")
        return 1
    print("output-system UI browser check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
