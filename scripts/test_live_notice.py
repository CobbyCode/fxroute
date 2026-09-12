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

    def test_status_live_duplication_is_documented_as_contract(self):
        """state["live"] AND state["system"]["live"] are both intentional.

        The frontend banner accepts either shape (data.live === true ||
        data.system.live === true) and demo/test transports synthesize one
        or the other, so /api/status serves both keys on purpose. This test
        keeps a future cleanup from "simplifying" one of them away.
        """
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('state["system"] = {"version": _read_version_file(), "live": is_live_mode()}', src)
        self.assertIn('state["live"] = state["system"]["live"]', src)
        # The both-shapes comment sits right above the assignment.
        anchor = src.index('state["system"] = {"version": _read_version_file(), "live": is_live_mode()}')
        comment_block = src[max(0, anchor - 900):anchor]
        self.assertIn("intentionally provided twice", comment_block)
        self.assertIn("data.system.live === true", comment_block)
        self.assertIn("Do NOT remove either key", comment_block)
        js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data.live === true || (data.system && data.system.live === true)", js)

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
