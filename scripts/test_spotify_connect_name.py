# SPDX-License-Identifier: AGPL-3.0-only

"""Contract tests for the short unique Spotify Connect device name.

Covers the Python derivation in streaming/spotify/connect_name.py and its
bash mirror in install.sh (fxroute_spotify_connect_name). No daemon, network,
or Spotify account is required.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.spotify import connect_name

INSTALL_SH = ROOT / "install.sh"

# Shared vectors: (hostname, machine_id, expected Connect name).
VECTORS = (
    ("fxroute-wohnzimmer", "9f8e7d6c5b4a39485768796a5b4c3d2e1f", "FXRoute Wohnzimmer"),
    ("fxroute-1af688", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
    ("FXROUTE-1AF688", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
    ("fxroute-1af688.local", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
    ("keller", "9f8e7d6c5b4a39485768796a5b4c3d2e1f", "FXRoute Keller"),
    ("media-server", "9f8e7d6c5b4a39485768796a5b4c3d2e1f", "FXRoute Media Server"),
    ("fxroute", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
    ("localhost", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
    ("localhost.localdomain", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
    ("", "1af6881234567890abcdef1234567890", "FXRoute 1AF6"),
)


def extract_bash_functions(text: str, names) -> str:
    parts = []
    for name in names:
        match = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}", text, re.MULTILINE | re.DOTALL)
        if not match:
            raise AssertionError(f"missing {name}() in install.sh")
        parts.append(f"{name}() {{\n{match.group(1)}\n}}")
    return "\n".join(parts)


class DeriveTests(unittest.TestCase):
    def test_shared_vectors(self):
        for hostname, machine_id, expected in VECTORS:
            with self.subTest(hostname=hostname):
                self.assertEqual(
                    connect_name.derive_spotify_connect_name(hostname, machine_id, None),
                    expected,
                )

    def test_meaningful_suffix_is_humanized(self):
        self.assertEqual(
            connect_name.derive_spotify_connect_name("fxroute-wohn-zimmer_nord", "x" * 32, None),
            "FXRoute Wohn Zimmer Nord",
        )

    def test_result_is_never_bare_and_never_local(self):
        cases = (
            ("fxroute", ""),
            ("localhost", ""),
            ("", ""),
            ("fxroute.local", ""),
            ("FXRoute", ""),
            ("...", ""),
        )
        for hostname, machine_id in cases:
            with self.subTest(hostname=hostname):
                name = connect_name.derive_spotify_connect_name(hostname, machine_id, "AB12")
                self.assertNotEqual(name, "FXRoute")
                self.assertNotIn(".local", name.lower())
                self.assertTrue(name.startswith("FXRoute "))

    def test_short_machine_id_falls_back_to_persisted_suffix(self):
        name = connect_name.derive_spotify_connect_name("fxroute", "abc", "c0de")
        self.assertEqual(name, "FXRoute C0DE")

    def test_resolve_creates_persisted_suffix_only_when_needed(self):
        with tempfile.TemporaryDirectory() as td:
            suffix_file = Path(td) / "device-suffix"
            name = connect_name.resolve_spotify_connect_name(
                "fxroute-wohnzimmer", "1af6881234567890abcdef1234567890",
                suffix_file=suffix_file,
            )
            self.assertEqual(name, "FXRoute Wohnzimmer")
            self.assertFalse(suffix_file.exists())
            first = connect_name.resolve_spotify_connect_name("fxroute", "", suffix_file=suffix_file)
            second = connect_name.resolve_spotify_connect_name("fxroute", "", suffix_file=suffix_file)
            self.assertEqual(first, second)
            self.assertRegex(first, r"^FXRoute [0-9A-F]{4}$")
            self.assertNotEqual(first, "FXRoute")
            self.assertTrue(suffix_file.is_file())


class ManagedNameTests(unittest.TestCase):
    def test_managed_names(self):
        for name in (None, "", "FXRoute", "FXRoute 1AF6", "FXRoute Wohnzimmer"):
            self.assertTrue(connect_name.is_managed_spotify_name(name), name)

    def test_custom_names_are_preserved(self):
        for name in ("Living Room", "Kuche", "my speaker"):
            self.assertFalse(connect_name.is_managed_spotify_name(name), name)


class ConfigSyncTests(unittest.TestCase):
    def _write(self, path: Path, text: str) -> Path:
        path.write_text(text, encoding="utf-8")
        return path

    def test_bare_default_is_updated_and_settings_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            config = self._write(
                Path(td) / "spotifyd.conf",
                '[global]\ndevice_name = "FXRoute"\nbackend = "pulseaudio"\n'
                'volume_controller = "none"\nzeroconf_port = 4444\n',
            )
            result = connect_name.sync_spotifyd_device_name(config, desired="FXRoute 1AF6")
            self.assertEqual(
                result, {"changed": True, "previous": "FXRoute", "desired": "FXRoute 1AF6",
                         "config": str(config)},
            )
            text = config.read_text(encoding="utf-8")
            self.assertIn('device_name = "FXRoute 1AF6"', text)
            self.assertIn('volume_controller = "none"', text)
            self.assertIn("zeroconf_port = 4444", text)

    def test_custom_name_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            config = self._write(
                Path(td) / "spotifyd.conf", '[global]\ndevice_name = "Living Room"\n'
            )
            result = connect_name.sync_spotifyd_device_name(config, desired="FXRoute 1AF6")
            self.assertFalse(result["changed"])
            self.assertIn('"Living Room"', config.read_text(encoding="utf-8"))

    def test_current_name_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as td:
            config = self._write(
                Path(td) / "spotifyd.conf", '[global]\ndevice_name = "FXRoute 1AF6"\n'
            )
            before = config.stat().st_mtime_ns
            result = connect_name.sync_spotifyd_device_name(config, desired="FXRoute 1AF6")
            self.assertFalse(result["changed"])
            self.assertEqual(before, config.stat().st_mtime_ns)

    def test_missing_file_changes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "spotifyd.conf"
            result = connect_name.sync_spotifyd_device_name(missing, desired="FXRoute 1AF6")
            self.assertFalse(result["changed"])
            self.assertFalse(missing.exists())


class BashParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        text = INSTALL_SH.read_text()
        cls.helpers = extract_bash_functions(
            text,
            ("fxroute_spotify_humanize_label", "fxroute_spotify_persistent_suffix",
             "fxroute_spotify_connect_name"),
        )
        for token in (
            "fxroute_spotify_connect_name",
            ".local",
            "device-suffix",
            "/etc/machine-id",
        ):
            assert token in text, token

    def _bash_name(self, hostname: str, machine_id: str) -> str:
        with tempfile.TemporaryDirectory() as td:
            code = (
                f"{self.helpers}\n"
                "fxroute_spotify_connect_name \"$FX_HOST\" \"$FX_MID\" \"$FX_SUFFIX\"\n"
            )
            result = subprocess.run(
                ["bash", "-c", code],
                capture_output=True,
                text=True,
                env={**os.environ, "FX_HOST": hostname, "FX_MID": machine_id,
                     "FX_SUFFIX": f"{td}/device-suffix"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

    def test_bash_matches_python_vectors(self):
        for hostname, machine_id, expected in VECTORS:
            with self.subTest(hostname=hostname):
                self.assertEqual(self._bash_name(hostname, machine_id), expected)

    def test_bash_never_returns_bare_or_local(self):
        for hostname in ("fxroute", "localhost", "", "fxroute.local", "..."):
            with self.subTest(hostname=hostname):
                name = self._bash_name(hostname, "9f8e7d6c5b4a39485768796a5b4c3d2e1f")
                self.assertTrue(name.startswith("FXRoute "))
                self.assertNotIn(".local", name)

    def test_bash_persisted_suffix_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            suffix = f"{td}/device-suffix"
            code = (
                f"{self.helpers}\n"
                "a=\"$(fxroute_spotify_connect_name fxroute '' \"$FX_SUFFIX\")\"\n"
                "b=\"$(fxroute_spotify_connect_name fxroute '' \"$FX_SUFFIX\")\"\n"
                "printf '%s\\n%s\\n' \"$a\" \"$b\"\n"
            )
            result = subprocess.run(
                ["bash", "-c", code],
                capture_output=True,
                text=True,
                env={**os.environ, "FX_SUFFIX": suffix},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            first, second = result.stdout.strip().splitlines()
            self.assertEqual(first, second)
            self.assertRegex(first, r"^FXRoute [0-9A-F]{4}$")

    def test_installer_writes_derived_name_for_new_configs(self):
        body = extract_bash_functions(INSTALL_SH.read_text(), ("write_spotifyd_config",))
        self.assertIn("fxroute_spotify_connect_name", body)
        self.assertNotIn('device_name = "FXRoute"\n', body)

    def test_installer_sync_updates_managed_names_and_preserves_custom(self):
        text = INSTALL_SH.read_text()
        helpers = extract_bash_functions(
            text,
            ("fxroute_spotify_humanize_label", "fxroute_spotify_persistent_suffix",
             "fxroute_spotify_connect_name", "sync_spotifyd_device_name"),
        )
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            custom = home / ".config" / "spotifyd" / "spotifyd.conf"
            custom.parent.mkdir(parents=True)
            custom.write_text('[global]\ndevice_name = "Living Room"\n', encoding="utf-8")
            bare = Path(td) / "bare.conf"
            bare.write_text(
                '[global]\ndevice_name = "FXRoute"\nvolume_controller = "none"\n',
                encoding="utf-8",
            )
            code = (
                f"{helpers}\n"
                "run_as_target_user() { \"$@\"; }\n"
                "pass() { :; }\n"
                "warn() { :; }\n"
                f"HOME={home}\n"
                "SPOTIFYD_CONNECT_NAME=''\n"
                "SPOTIFYD_DEVICE_NAME_CHANGED=0\n"
                f"sync_spotifyd_device_name {custom}\n"
                f"sync_spotifyd_device_name {bare}\n"
                f"printf 'custom=%s\\n' \"$(cat {custom})\"\n"
                f"printf 'bare=%s\\n' \"$(cat {bare})\"\n"
                "printf 'changed=%s\\n' \"$SPOTIFYD_DEVICE_NAME_CHANGED\"\n"
            )
            result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('custom=[global]\ndevice_name = "Living Room"', result.stdout)
            bare_name = re.search(r'^bare=\[global\]\ndevice_name = "([^"]*)"', result.stdout,
                                  re.MULTILINE)
            self.assertIsNotNone(bare_name)
            self.assertTrue(bare_name.group(1).startswith("FXRoute "))
            self.assertNotEqual(bare_name.group(1), "FXRoute")
            self.assertNotIn(".local", bare_name.group(1))
            self.assertIn("volume_controller", result.stdout)
            self.assertIn("changed=1", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
