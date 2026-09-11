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


if __name__ == "__main__":
    unittest.main()
