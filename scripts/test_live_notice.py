#!/usr/bin/env python3
"""Live-mode notice contract tests."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LiveNoticeTests(unittest.TestCase):
    def test_status_has_live_flag(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("/etc/fxroute-live", src)
        self.assertIn("fxroute.live=1", src)
        self.assertIn('"live"', src)

    def test_banner_in_frontend(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("Live Mode", html + js)
        self.assertIn("live-banner", html + js + css)

    def test_banner_is_bottom_pill_with_close(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        self.assertIn("live-banner-close", html)
        self.assertIn("is-hidden", html)
        self.assertIn("bottom:", css)
        banner_block = css.split(".live-banner")[1].split("}")[0]
        self.assertNotIn("top: 0", banner_block)

    def test_banner_auto_hides_and_parks_over_footer(self):
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("playback-bar", js)
        self.assertIn("fxroute-live-banner-hidden", js)
        self.assertIn("LIVE_BANNER_AUTOHIDE_MS", js)


if __name__ == "__main__":
    unittest.main()
