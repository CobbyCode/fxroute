#!/usr/bin/env python3
"""Packaging contract for the FXRoute-owned native DSP path."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DspPackagingTests(unittest.TestCase):
    def test_installer_builds_native_dsp_without_easyeffects_runtime(self):
        script = (ROOT / "install.sh").read_text()
        self.assertIn('build_native_dsp_engine()', script)
        self.assertIn('native_dsp/build.sh', script)
        self.assertNotIn('flatpak install --user', script)
        self.assertNotIn('install_watchdog_if_needed', script)
        self.assertNotIn('setup_easyeffects_autostart', script)

    def test_pipewire_build_does_not_apply_pedantic_to_system_headers(self):
        script = (ROOT / "native_dsp/build.sh").read_text().splitlines()
        pipewire_command = next(line for line in script if "pipewire_engine.c" in line)
        self.assertIn("-std=gnu11", pipewire_command)
        self.assertNotIn("-pedantic", pipewire_command)

    def test_updater_rebuilds_native_dsp_from_all_engine_sources(self):
        script = (ROOT / "scripts/update_fxroute.sh").read_text()
        self.assertIn('build_native_dsp_if_needed()', script)
        self.assertIn('native_dsp/build/fxroute-dsp', script)
        self.assertIn('find "$source_dir" -type f -newer "$binary"', script)

    def test_runtime_integration_uses_fxroute_owned_nodes(self):
        transition = (ROOT / "playback_transition.py").read_text()
        peak_monitor = (ROOT / "peak_monitor.py").read_text()
        samplerate = (ROOT / "samplerate.py").read_text()
        self.assertIn('DSP_TRANSPORT_SINK = "fxroute_dsp_sink"', transition)
        self.assertIn('DSP_OUTPUT_NODE_NAME = "fxroute_dsp"', peak_monitor)
        self.assertIn('"fxroute_dsp_sink"', samplerate)
        self.assertNotIn('"easyeffects_sink"', samplerate)

    def test_legacy_watchdog_assets_are_removed(self):
        self.assertFalse((ROOT / "scripts/easyeffects-stale-watchdog.sh").exists())
        self.assertFalse((ROOT / "systemd-user/easyeffects-stale-watchdog.service").exists())
        self.assertFalse((ROOT / "systemd-user/easyeffects-stale-watchdog.timer").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
