#!/usr/bin/env python3
"""Visual footer check on .104: drive local/radio/tidal switches in headless
Chromium and verify the footer owner, VU meter visibility and track title
match the backend state after each switch. Screenshots land in /tmp/footer_check/.

Manual tool: run from the development box (needs python3-playwright and a
reachable .104; .104 itself has no browser). Not part of run_tests.sh - it
drives live playback and mutates the playing source. TIDAL is driven by a
page-context /api/play fetch (playTidalTracks is closure-scoped), so the
footer must update through the real WS broadcast / status-poll heal path -
exactly what a second client sees.
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = "http://192.168.178.104:8000"
OUT = "/tmp/footer_check"
RADIO_ID = "fip-hiphop"

SNAPSHOT_JS = """() => ({
    footerSource: window.__footerSource,
    owner: state.playback.playback_owner || null,
    source: state.playback.current_track?.source || null,
    trackId: state.playback.current_track?.id || null,
    title: state.playback.current_track?.title || null,
    playing: !!state.playback.playing,
    paused: !!state.playback.paused,
    titleEl: document.getElementById('track-title')?.textContent || '',
    meterActive: document.getElementById('playback-meter')?.classList.contains('is-active') || false,
    meterPeak: document.getElementById('playback-meter')?.classList.contains('is-peak') || false,
    badgeHidden: document.getElementById('output-level-badge')?.classList.contains('hidden'),
    badgeText: document.getElementById('output-level-badge')?.textContent || '',
})"""


def drive_local(page):
    page.evaluate("id => playLocal(id)", page.evaluate("state.library.tracks[0].id"))


def drive_radio(page):
    page.evaluate(f"playRadio('{RADIO_ID}')")


def drive_tidal(page):
    page.evaluate(
        """async () => {
            const resp = await fetch('/api/play', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ source: 'tidal', track_id: '225476' }),
            });
            return resp.status;
        }"""
    )


def main():
    os.makedirs(OUT, exist_ok=True)
    failures = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.error: {m.text}") if m.type == "error" else None)

        page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function(
            "document.querySelector('script[src*=\"app.js\"]')?.src.includes('0.9.153')",
            timeout=10000,
        )
        # state is a top-level const in a classic script: global lexical scope,
        # NOT a window property - reference it bare.
        page.wait_for_function("state.library && state.library.tracks.length > 0", timeout=30000)
        page.wait_for_function("state.stations && state.stations.length > 0", timeout=30000)
        page.wait_for_timeout(1500)  # let the initial status/peak poll settle

        steps = [
            ("1-local", drive_local, "local", "local"),
            ("2-radio", drive_radio, "radio", "local"),  # native sources share footer 'local'
            ("3-tidal", drive_tidal, "tidal", "local"),  # native sources share footer 'local'
            ("4-radio-restore", drive_radio, "radio", "local"),
        ]
        for name, drive, want_owner, want_footer in steps:
            drive(page)
            # Wait for the backend owner to reach the page state (via the play
            # response commit or the status-poll heal) and the footer to follow.
            page.wait_for_function(
                f"""() => state.playback.playback_owner === '{want_owner}'
                    && window.__footerSource === '{want_footer}'
                    && state.playback.playing === true""",
                timeout=20000,
            )
            page.wait_for_timeout(1200)  # meter/badge render settle
            snap = page.evaluate(SNAPSHOT_JS)
            page.locator("#playback-bar").screenshot(path=f"{OUT}/{name}.png")

            if snap["owner"] != want_owner:
                failures.append(f"{name}: owner {snap['owner']!r} != {want_owner!r}")
            if snap["footerSource"] != want_footer:
                failures.append(f"{name}: footerSource {snap['footerSource']!r} != {want_footer!r}")
            if snap["badgeHidden"] or not snap["badgeText"].strip():
                failures.append(f"{name}: VU badge hidden/empty (hidden={snap['badgeHidden']}, text={snap['badgeText']!r})")
            if not snap["meterActive"]:
                failures.append(f"{name}: meter not active")
            print(f"{name}: owner={snap['owner']} source={snap['source']} footer={snap['footerSource']} "
                  f"meterActive={snap['meterActive']} badge={snap['badgeText']!r} "
                  f"title={snap['title']!r} titleEl={snap['titleEl']!r}")

        # VU liveness: on the restored radio state the badge value must move.
        t1 = page.evaluate(SNAPSHOT_JS)["badgeText"]
        page.wait_for_timeout(2500)
        t2 = page.evaluate(SNAPSHOT_JS)["badgeText"]
        live = t1 != t2
        print(f"VU liveness: {t1!r} -> {t2!r} ({'live' if live else 'STATIC'})")
        if not live:
            failures.append("VU badge value did not change across 2.5 s (meter looks static)")

        page.screenshot(path=f"{OUT}/full-final.png", full_page=False)
        browser.close()

    if errors:
        print("\nBrowser errors captured:")
        for e in errors[:10]:
            print(" ", e[:200])
        failures.append(f"{len(errors)} browser console/page error(s)")

    print(f"\nScreenshots in {OUT}/")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("Visual footer check: OK")


if __name__ == "__main__":
    main()
