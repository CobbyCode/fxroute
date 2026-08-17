#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Regression check: native <select> option popups stay dark and readable.

Background: the closed selects were dark via .url-input, but Chromium
rendered the native options popup with the system light scheme because no
color-scheme was declared; the near-white text inherited from .url-input
then produced a white-on-white popup.  The fix declares color-scheme:
dark on :root and styles select option / option:checked with the theme
tokens.

This check runs in two stages:

1. Static (always): the fixing CSS facts must exist in style.css --
   color-scheme: dark on :root, a generic ``select option`` rule using the
   dark tokens, and an ``option:checked`` accent rule.  A later rule must
   not wipe option backgrounds back to transparent.
2. Computed styles (when playwright is importable): loads the real
   style.css with representative selects (same classes as the app:
   .url-input, .effects-compact-control, .compare-select, plus one bare
   select) into headless Chromium and asserts that every option computes
   a non-transparent dark background with >= 4.5:1 text contrast, that
   the checked option uses --bg-elevated/--accent, and that the closed
   select surface stays dark.

Exit codes: 0 pass, 1 fail, 77 skipped (playwright not installed; the
static stage already ran and passed).
"""

import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_PATH = ROOT / "static" / "style.css"

# Representative selects mirroring the app's markup: every app <select>
# carries url-input; the compact DSP controls add effects-compact-control,
# the compare presets add compare-select.  A bare select proves the
# generic ``select option`` rule covers selects without url-input too.
TEST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="/style.css">
</head>
<body>
  <select id="fft" class="url-input effects-compact-control">
    <option value="512">512</option>
    <option value="1024" selected>1024</option>
    <option value="2048">2048</option>
  </select>
  <select id="headroom" class="url-input effects-compact-control">
    <option value="-6">-6 dB</option>
    <option value="-3" selected>-3 dB</option>
    <option value="0">0 dB</option>
  </select>
  <select id="autogain" class="url-input effects-compact-control">
    <option value="-23">-23 LUFS</option>
    <option value="-20" selected>-20 LUFS</option>
  </select>
  <select id="output" class="url-input">
    <option value="stereo" selected>Stereo</option>
    <option value="sub">Subwoofer</option>
  </select>
  <select id="samplerate" class="url-input">
    <option value="44100">44.1 kHz</option>
    <option value="48000" selected>48 kHz</option>
  </select>
  <select id="compare" class="url-input compare-select">
    <option value="a">Preset A</option>
    <option value="b" selected>Preset B</option>
  </select>
  <select id="bare">
    <option value="x">Bare option</option>
    <option value="y" selected>Bare selected</option>
  </select>
</body>
</html>
"""


def _fail(msg):
    print(f"SELECT POPUP THEME CHECK FAILED: {msg}")
    sys.exit(1)


def _root_block(css: str) -> str:
    match = re.search(r"^:root\s*\{", css, re.MULTILINE)
    if not match:
        _fail("no :root block in style.css")
    depth = 0
    for index in range(match.end() - 1, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[match.start():index + 1]
    _fail("unterminated :root block in style.css")


def _rule_for(css: str, selector: str) -> str | None:
    match = re.search(
        r"^[ \t]*" + re.escape(selector) + r"\s*\{([^}]+)\}",
        css,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _static_stage(css: str) -> None:
    root = _root_block(css)
    if not re.search(r"color-scheme\s*:\s*dark\b", root):
        _fail(":root does not declare color-scheme: dark")

    option_rule = _rule_for(css, "select option")
    if option_rule is None:
        _fail("missing generic 'select option' rule")
    if "var(--bg-input)" not in option_rule or "var(--text-primary)" not in option_rule:
        _fail("'select option' must use var(--bg-input) + var(--text-primary)")

    checked_rule = _rule_for(css, "select option:checked")
    if checked_rule is None:
        _fail("missing 'select option:checked' rule")
    if "var(--bg-elevated)" not in checked_rule or "var(--accent)" not in checked_rule:
        _fail("'select option:checked' must use var(--bg-elevated) + var(--accent)")

    # A later rule must not reset option backgrounds to transparent
    # (the original regression: options rendered transparent/white).
    later = css[css.index(option_rule):]
    for match in re.finditer(r"option\s*\{(.*?)\}", later, re.DOTALL):
        body = match.group(1)
        if "transparent" in body and "select option" not in css[:match.start()].split(";")[-1]:
            _fail(f"later option rule resets background: {body.strip()}")


# --- computed-style helpers -------------------------------------------------

def _parse_rgb(value: str):
    """Parse rgb()/rgba()/hex to (r, g, b) with alpha normalized onto a
    black backdrop (returns a 3-tuple).

    The closed selects use a translucent white overlay
    (rgba(255,255,255,.035)) over the dark page background; computed
    backgroundColor reports the raw rgba value, so the check composites it
    over the page background to judge the actual rendered surface.
    """
    value = value.strip()
    match = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?\s*\)", value)
    if match:
        r, g, b = (int(part) for part in match.groups()[:3])
        alpha = float(match.group(4)) if match.group(4) else 1.0
        return (int(round(r * alpha)), int(round(g * alpha)), int(round(b * alpha)))
    match = re.match(r"#([0-9a-fA-F]{6})", value)
    if match:
        hexv = match.group(1)
        return tuple(int(hexv[index:index + 2], 16) for index in (0, 2, 4))
    return None


def _relative_luminance(rgb) -> float:
    def channel(value: float) -> float:
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(rgb1, rgb2) -> float:
    l1, l2 = _relative_luminance(rgb1), _relative_luminance(rgb2)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _run_server():
    css_bytes = CSS_PATH.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                body = TEST_HTML.encode()
                ctype = "text/html"
            elif self.path == "/style.css":
                body = css_bytes
                ctype = "text/css"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _computed_stage() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed (static stage passed)")
        sys.exit(77)

    server = _run_server()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 900, "height": 700})
                page.goto(url, wait_until="load", timeout=60000)
                page.wait_for_timeout(300)
                data = page.evaluate("""() => {
                    const root = getComputedStyle(document.documentElement);
                    const tokens = {};
                    for (const name of ['--bg-input', '--bg-elevated', '--text-primary', '--accent']) {
                        tokens[name] = root.getPropertyValue(name).trim();
                    }
                    const selects = [...document.querySelectorAll('select')];
                    const out = { colorScheme: root.colorScheme, tokens, selects: [] };
                    for (const sel of selects) {
                        const selCs = getComputedStyle(sel);
                        const options = [...sel.options].map(opt => {
                            const cs = getComputedStyle(opt);
                            return {
                                text: opt.textContent,
                                checked: opt.selected,
                                bg: cs.backgroundColor,
                                color: cs.color,
                            };
                        });
                        out.selects.push({ id: sel.id, selectBg: selCs.backgroundColor, options });
                    }
                    return out;
                }""")
            finally:
                browser.close()
    finally:
        server.shutdown()

    if data["colorScheme"] != "dark":
        _fail(f":root computed color-scheme is {data['colorScheme']!r}, expected 'dark'")

    # Resolve token values into rgb triples for comparison.
    def token_rgb(name):
        rgb = _parse_rgb(data["tokens"][name])
        if rgb is None:
            _fail(f"token {name} has unparsable value {data['tokens'][name]!r}")
        return rgb

    bg_input = token_rgb("--bg-input")
    bg_elevated = token_rgb("--bg-elevated")
    text_primary = token_rgb("--text-primary")
    accent = token_rgb("--accent")

    for entry in data["selects"]:
        select_bg = _parse_rgb(entry["selectBg"])
        if select_bg is None:
            _fail(f"select #{entry['id']}: unparsable background {entry['selectBg']!r}")
        if _relative_luminance(select_bg) > 0.5:
            _fail(f"select #{entry['id']} (closed) is not dark: {entry['selectBg']}")
        if len(entry["options"]) == 0:
            _fail(f"select #{entry['id']} has no options")
        for opt in entry["options"]:
            bg = _parse_rgb(opt["bg"])
            color = _parse_rgb(opt["color"])
            if bg is None:
                _fail(f"#{entry['id']} option {opt['text']!r}: unparsable bg {opt['bg']!r}")
            if _relative_luminance(bg) > 0.5:
                _fail(f"#{entry['id']} option {opt['text']!r} background is not dark: {opt['bg']}")
            ratio = _contrast(color, bg)
            if ratio < 4.5:
                _fail(
                    f"#{entry['id']} option {opt['text']!r}: contrast {ratio:.1f}:1 "
                    f"({opt['color']} on {opt['bg']}) below 4.5:1"
                )
            if opt["checked"]:
                if bg != bg_elevated:
                    _fail(
                        f"#{entry['id']} checked option {opt['text']!r}: background "
                        f"{opt['bg']} != --bg-elevated"
                    )
                if color != accent:
                    _fail(
                        f"#{entry['id']} checked option {opt['text']!r}: color "
                        f"{opt['color']} != --accent"
                    )


def main() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    _static_stage(css)
    print("Static select-popup theme facts ok (color-scheme + option tokens)")
    _computed_stage()
    print("Select popup theme check passed: all options dark, readable, checked option accented")
    sys.exit(0)


if __name__ == "__main__":
    main()
