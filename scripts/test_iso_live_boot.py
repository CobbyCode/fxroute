#!/usr/bin/env python3
"""ISO live-boot option contract tests (Try FXRoute)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_OPTS = ROOT / "iso" / "scripts" / "live-boot-options.txt"
BUILD = ROOT / "iso" / "build-leap-16-iso.sh"


class IsoLiveBootTests(unittest.TestCase):
    def test_live_boot_options_has_no_inst_auto(self):
        text = LIVE_OPTS.read_text(encoding="utf-8")
        self.assertIn("rd.live.dir=LiveFX", text)
        self.assertIn("rd.live.squashimg=squashfs.img", text)
        self.assertIn("rd.live.overlay.overlayfs=1", text)
        self.assertIn("graphical.target", text)
        self.assertIn("systemd.mask=agama.service", text)
        self.assertIn("systemd.mask=fxroute-first-boot.service", text)
        self.assertIn("fxroute.live=1", text)
        self.assertNotIn("inst.auto", text)

    def test_build_script_has_try_entry(self):
        text = BUILD.read_text(encoding="utf-8")
        self.assertIn("Try FXRoute", text)
        self.assertIn("LiveFX", text)

    def test_build_stages_livefx(self):
        text = BUILD.read_text(encoding="utf-8")
        self.assertIn("LiveFX/squashfs.img", text)
        self.assertTrue(
            "FXROUTE_LIVE_SQUASH" in text or "build-live-from-installed" in text,
            "build must stage LiveFX squash",
        )
        self.assertNotIn("build-live-root.sh", text)
        self.assertIn('menuentry "Try FXRoute"', text)


if __name__ == "__main__":
    unittest.main()
