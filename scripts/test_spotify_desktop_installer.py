#!/usr/bin/env python3
"""Regression tests for the Spotify Desktop installer findings.

Spotify Desktop (desktop app) and spotifyd (Connect daemon) are separate
components with independent lifecycles:

1. A spotifyd admin action must never remove Spotify Desktop and vice
   versa (scoped uninstall splits ``spotify``/``spotify-desktop``).
2. ``--providers-only --spotify-desktop`` must converge to the same
   complete desktop setup state as the full installer (desktop autostart).
3. The SPOTIFY_AUTOSTART default must follow the supported desktop
   session, not the architecture alone (no autostart default headless).
"""

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"
STREAMING_API = ROOT / "streaming" / "api.py"
PROVIDER_SETTINGS_JS = ROOT / "static" / "provider_settings.js"


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


def case_branch(body: str, label: str) -> str:
    """Return the case-branch body for ``label)`` inside a case statement."""
    match = re.search(
        rf"^\s*{re.escape(label)}\)\n(.*?)(?=^\s*(?:[a-z_*|-]+\))|^\s*esac)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing case branch {label})")
    return match.group(1)


class SpotifyLifecycleSeparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.uninstall = UNINSTALL_SH.read_text()

    def test_scoped_spotify_removal_touches_only_spotifyd(self):
        body = extract_function(self.uninstall, "remove_single_provider")
        branch = case_branch(body, "spotify")
        self.assertIn("remove_owned_spotifyd", branch)
        self.assertNotIn("remove_owned_spotify_desktop", branch)

    def test_scoped_desktop_removal_touches_only_desktop(self):
        body = extract_function(self.uninstall, "remove_single_provider")
        branch = case_branch(body, "spotify-desktop")
        self.assertIn("remove_owned_spotify_desktop", branch)
        self.assertNotIn("remove_owned_spotifyd", branch)

    def test_provider_flag_accepts_spotify_desktop(self):
        self.assertIn("spotify|spotify-desktop|qobuz|tidal|privilege", self.uninstall)
        self.assertIn("spotify-desktop", extract_function(self.uninstall, "usage"))

    def test_full_uninstall_still_removes_both(self):
        body = extract_function(self.uninstall, "remove_owned_streaming_components")
        self.assertIn("remove_owned_spotify_desktop", body)
        self.assertIn("remove_owned_spotifyd", body)

    def test_combined_install_still_covers_both(self):
        body = extract_function(self.install, "configure_optional_streaming")
        self.assertIn("install_spotify_desktop", body)
        self.assertIn("install_spotifyd", body)

    def test_settings_install_maps_spotify_to_spotifyd_only(self):
        api = STREAMING_API.read_text()
        self.assertIn('"spotify": "--spotifyd"', api)
        self.assertNotIn("--spotify-desktop", api)

    def test_settings_uninstall_confirm_no_longer_claims_desktop_removal(self):
        js = PROVIDER_SETTINGS_JS.read_text()
        self.assertNotIn("spotifyd and desktop integration", js)


class ProvidersOnlyDesktopAutostartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()

    def test_providers_only_converges_desktop_autostart(self):
        body = extract_function(self.install, "main_providers_only")
        self.assertIn("setup_spotify_autostart", body)
        self.assertLess(
            body.index("write_install_state"),
            body.index("setup_spotify_autostart"),
        )

    def test_full_installer_still_configures_autostart(self):
        self.assertIn("setup_spotify_autostart", self.install)


class SpotifyAutostartDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()

    def test_default_follows_supported_session_predicate(self):
        body = extract_function(self.install, "create_env_if_missing")
        self.assertIn("if ! spotify_desktop_supported; then", body)
        self.assertNotIn('$(uname -m)" != "x86_64"', body)

    def test_supported_predicate_gates_arch_and_session(self):
        body = extract_function(self.install, "spotify_desktop_supported")
        self.assertIn("x86_64", body)
        self.assertIn("DISPLAY", body)
        self.assertIn("WAYLAND_DISPLAY", body)
        self.assertIn("XDG_SESSION_TYPE", body)

    def _probe_default(self, env: dict) -> str:
        supported = extract_function(self.install, "spotify_desktop_supported")
        script = (
            f"{supported}\n"
            "probe_default() {\n"
            '  local spotify_autostart="on"\n'
            "  if ! spotify_desktop_supported; then\n"
            '    spotify_autostart="off"\n'
            "  fi\n"
            '  printf "%s" "$spotify_autostart"\n'
            "}\n"
            "probe_default\n"
        )
        run_env = {
            "PATH": "/usr/bin:/bin",
            "HOST_ARCH": env.get("HOST_ARCH", ""),
            "DISPLAY": env.get("DISPLAY", ""),
            "WAYLAND_DISPLAY": env.get("WAYLAND_DISPLAY", ""),
            "XDG_SESSION_TYPE": env.get("XDG_SESSION_TYPE", ""),
        }
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=run_env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_desktop_session_defaults_to_on(self):
        self.assertEqual(
            self._probe_default({"HOST_ARCH": "x86_64", "DISPLAY": ":0"}), "on"
        )
        self.assertEqual(
            self._probe_default(
                {"HOST_ARCH": "x86_64", "XDG_SESSION_TYPE": "wayland"}
            ),
            "on",
        )

    def test_headless_x86_64_defaults_to_off(self):
        self.assertEqual(self._probe_default({"HOST_ARCH": "x86_64"}), "off")

    def test_non_x86_64_defaults_to_off_even_with_session(self):
        self.assertEqual(
            self._probe_default({"HOST_ARCH": "aarch64", "DISPLAY": ":0"}), "off"
        )


if __name__ == "__main__":
    unittest.main()
