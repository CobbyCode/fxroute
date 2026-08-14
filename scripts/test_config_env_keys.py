#!/usr/bin/env python3
"""Configuration-source and .env-typo-detection tests.

The Settings model ignores unknown environment keys via ``extra="ignore"``;
these tests verify that unknown ``.env`` keys are still reported clearly,
that documented installer-managed keys stay accepted, and that every setting
referenced by the application has an authoritative model field.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


class ParseEnvFileKeysTests(unittest.TestCase):
    def test_parses_keys_and_ignores_comments_blanks_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n"
                "MUSIC_ROOT=/music\n"
                "\n"
                "  LOG_LEVEL=INFO\n"
                "export HOST=0.0.0.0\n"
                "SPOTIFY_AUTOSTART=on\n"
                "not-a-setting-line\n",
                encoding="utf-8",
            )
            keys = config._parse_env_file_keys(path)
        self.assertEqual(
            keys,
            {"MUSIC_ROOT", "LOG_LEVEL", "HOST", "SPOTIFY_AUTOSTART"},
        )

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(config._parse_env_file_keys(Path(tmp) / "missing"), set())


class WarnUnknownEnvFileKeysTests(unittest.TestCase):
    def _write(self, content: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".env"
        path.write_text(content, encoding="utf-8")
        return path

    def test_unknown_key_warns_with_the_key_name(self):
        path = self._write("MUSIC_ROOT=/music\nPORTS=8001\n")
        with self.assertLogs(level="WARNING") as cm:
            config.warn_unknown_env_file_keys(path)
        self.assertTrue(any("PORTS" in line for line in cm.output), cm.output)

    def test_known_and_installer_managed_keys_do_not_warn(self):
        path = self._write(
            "MUSIC_ROOT=/music\n"
            "DOWNLOADS_SUBDIR=incoming\n"
            "LOG_LEVEL=INFO\n"
            "HOST=0.0.0.0\n"
            "PORT=8000\n"
            "MAX_DOWNLOADS=1\n"
            "DOWNLOAD_TRANSCODE_FORMAT=\n"
            "AUDIO_FORMAT=\n"
            "HARDWARE_CONTROLLER_DEVICE=/dev/ttyUSB0\n"
            "SPOTIFY_AUTOSTART=on\n"
            "SPOTIFY_CACHE_CLEANUP=off\n"
            "SPOTIFY_CACHE_CLEANUP_INTERVAL_HOURS=24\n"
            "SYSTEM_AUTO_UPDATE=off\n"
            "SYSTEM_AUTO_UPDATE_INTERVAL_HOURS=24\n"
            "FXROUTE_DSP_BINARY=/opt/fxroute/fxroute-dsp\n"
            "FXROUTE_RADIO_BROWSER_URL=https://example.invalid/\n"
            "MUSIC_LIBRARY_SMB_HOSTS=nas1,nas2\n",
        )
        with self.assertNoLogs(level="WARNING"):
            config.warn_unknown_env_file_keys(path)

    def test_case_insensitive_match(self):
        path = self._write("music_root=/music\nLOG_LEVEL=info\n")
        with self.assertNoLogs(level="WARNING"):
            config.warn_unknown_env_file_keys(path)


class SettingsFieldAuthorityTests(unittest.TestCase):
    def test_hardware_controller_device_is_a_defined_field(self):
        # Referenced by main.py's optional hardware controller; it must be a
        # model field so ``extra="ignore"`` never silently drops it.
        self.assertIn("HARDWARE_CONTROLLER_DEVICE", config.Settings.model_fields)

    def test_get_settings_returns_a_single_cached_instance(self):
        class _FakeSettings:
            model_fields = {}
            LOG_LEVEL = "INFO"

        config.settings = None
        try:
            with patch.object(config, "Settings", _FakeSettings), \
                    patch.object(config, "setup_logging"):
                first = config.get_settings()
                second = config.get_settings()
        finally:
            config.settings = None
        self.assertIsInstance(first, _FakeSettings)
        self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
