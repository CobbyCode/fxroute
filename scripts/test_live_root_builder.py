#!/usr/bin/env python3
"""Live root builder contract tests."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "iso" / "scripts" / "build-live-root.sh"
LIVE_INIT = ROOT / "iso" / "scripts" / "fxroute-live-init.sh"
UDEV = ROOT / "iso" / "scripts" / "live-udev-nomount.rules"


class LiveRootBuilderTests(unittest.TestCase):
    def test_builder_exists_and_uses_docker_flat_squash(self):
        text = BUILDER.read_text(encoding="utf-8")
        self.assertIn("docker", text)
        self.assertIn("mksquashfs", text)
        self.assertIn("LiveFX", text)
        self.assertIn("fxroute-live", text)
        self.assertIn("/proc", text)

    def test_live_init_disables_first_boot(self):
        text = LIVE_INIT.read_text(encoding="utf-8")
        self.assertIn("fxroute-first-boot", text)
        self.assertIn("agama", text.lower())
        self.assertIn("fxroute.service", text)

    def test_udev_rule_ignores_internal(self):
        text = UDEV.read_text(encoding="utf-8")
        self.assertIn("UDISKS_IGNORE", text)
        lowered = text.lower()
        self.assertTrue("ata" in lowered or "nvme" in lowered)

    def test_builder_opens_live_firewall_ports(self):
        text = BUILDER.read_text(encoding="utf-8")
        self.assertIn("firewall-offline-cmd", text)
        self.assertIn("8000/tcp", text)

    def test_builder_wires_sddm_x11_session(self):
        text = BUILDER.read_text(encoding="utf-8")
        self.assertIn("plasma6-session-x11", text)
        self.assertIn("Session=default.desktop", text)
        self.assertIn("display-manager.service", text)
        self.assertIn("sddm.service", text)


if __name__ == "__main__":
    unittest.main()
