#!/usr/bin/env python3
"""Contract tests for optional streaming installation support."""

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"


def extract_function(text: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise AssertionError(f"missing {name}()")
    return f"{name}() {{\n{match.group(1)}\n}}"


class InstallerStreamingStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.uninstall = UNINSTALL_SH.read_text()
        cls.base_requirements = (ROOT / "requirements.txt").read_text()
        tidal_requirements = ROOT / "requirements-tidal.txt"
        cls.tidal_requirements = tidal_requirements.read_text() if tidal_requirements.exists() else ""

    def test_provider_selection_contract_is_explicit_and_independent(self):
        for option in (
            "--providers",
            "--spotify-desktop",
            "--spotifyd",
            "--qobuz",
            "--tidal",
        ):
            self.assertIn(option, self.install)
        for variable in (
            "SELECT_SPOTIFY_DESKTOP",
            "SELECT_SPOTIFYD",
            "SELECT_QOBUZ",
            "SELECT_TIDAL",
        ):
            self.assertIn(variable, self.install)
        self.assertIn("providers", self.install.lower())

    def test_noninteractive_install_does_not_select_optional_components_by_default(self):
        self.assertIn("SELECT_SPOTIFY_DESKTOP=0", self.install)
        self.assertIn("SELECT_SPOTIFYD=0", self.install)
        self.assertIn("SELECT_QOBUZ=0", self.install)
        self.assertIn("SELECT_TIDAL=0", self.install)
        self.assertIn("-t 0", self.install)

    def test_provider_parser_accepts_all_four_names(self):
        body = extract_function(self.install, "parse_provider_selection")
        code = f"""
{body}
SELECT_SPOTIFY_DESKTOP=0
SELECT_SPOTIFYD=0
SELECT_QOBUZ=0
SELECT_TIDAL=0
parse_provider_selection spotify-desktop,spotifyd,qobuz,tidal
printf '%s %s %s %s\\n' "$SELECT_SPOTIFY_DESKTOP" "$SELECT_SPOTIFYD" "$SELECT_QOBUZ" "$SELECT_TIDAL"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1 1 1 1")

    def test_provider_list_and_component_flags_are_combined(self):
        body = extract_function(self.install, "select_optional_providers")
        parser = extract_function(self.install, "parse_provider_selection")
        code = f"""
PROVIDER_LIST=spotifyd
PROVIDER_SELECTION_EXPLICIT=1
SELECT_SPOTIFY_DESKTOP=0
SELECT_SPOTIFYD=0
SELECT_QOBUZ=1
SELECT_TIDAL=0
{parser}
{body}
select_optional_providers
printf '%s %s %s %s\\n' "$SELECT_SPOTIFY_DESKTOP" "$SELECT_SPOTIFYD" "$SELECT_QOBUZ" "$SELECT_TIDAL"
"""
        result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0 1 1 0")

    def test_spotify_desktop_is_gated_to_x86_64_desktop_sessions(self):
        self.assertIn("spotify_desktop_supported", self.install)
        body = extract_function(self.install, "spotify_desktop_supported")
        for architecture, session in (("x86_64", "wayland"), ("aarch64", "wayland"), ("x86_64", "")):
            code = f"""
{body}
HOST_ARCH={architecture}
XDG_SESSION_TYPE={session}
DISPLAY=''
WAYLAND_DISPLAY=''
XDG_CURRENT_DESKTOP=''
DESKTOP_SESSION=''
spotify_desktop_supported
"""
            result = subprocess.run(["bash", "-c", code], capture_output=True, text=True)
            if architecture == "x86_64" and session:
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self.assertNotEqual(result.returncode, 0)
        headless_desktop_env = f"""
{body}
HOST_ARCH=x86_64
XDG_SESSION_TYPE=''
DISPLAY=''
WAYLAND_DISPLAY=''
XDG_CURRENT_DESKTOP=GNOME
DESKTOP_SESSION=''
spotify_desktop_supported
"""
        result = subprocess.run(["bash", "-c", headless_desktop_env], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_spotifyd_release_matrix_uses_mpris_capable_full_build(self):
        self.assertRegex(self.install, r"SPOTIFYD_VERSION=\"0\.4\.2\"")
        self.assertIn("spotifyd-linux-x86_64-full.tar.gz", self.install)
        self.assertIn("spotifyd-linux-aarch64-full.tar.gz", self.install)
        self.assertIn("spotifyd-linux-armv7-full.tar.gz", self.install)
        self.assertNotIn("spotifyd-linux-x86_64-slim.tar.gz", self.install)
        self.assertIn("spotifyd_arch_for_host", self.install)

    def test_spotifyd_config_has_device_mpris_and_pipewire_without_credentials_or_volume(self):
        body = extract_function(self.install, "write_spotifyd_config")
        for setting in (
            'device_name = "FXRoute"',
            'backend = "pulseaudio"',
            "use_mpris = true",
            'dbus_type = "session"',
        ):
            self.assertIn(setting, body)
        for forbidden in ("username", "password", "volume_control", "volume ="):
            self.assertNotIn(forbidden, body)

    def test_qobuz_release_matrix_is_pinned(self):
        self.assertRegex(self.install, r"QBZD_VERSION=\"2\.0\.2\"")
        self.assertIn('archive="qbzd-${QBZD_VERSION}-linux-amd64.tar.gz"', self.install)
        self.assertIn('archive="qbzd-${QBZD_VERSION}-linux-aarch64.tar.gz"', self.install)
        self.assertIn("6bcdb2616f339b7905fc58f48edf7e3bc2e0e9eadc1ce6235b60c7fdf34b804c", self.install)
        self.assertIn("adade56509544c00187476d58acef78538d3e5d475370263d98397dc61f200c9", self.install)

    def test_new_spotify_apt_source_refreshes_package_metadata(self):
        body = extract_function(self.install, "install_spotify_desktop_apt")
        self.assertIn("SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1", body)
        self.assertIn("PKG_REFRESH_DONE=0", body)

    def test_spotify_apt_uses_the_current_signed_repository_key(self):
        body = extract_function(self.install, "install_spotify_desktop_apt")
        self.assertIn("pubkey_5384CE82BA52C83A.asc", self.install)
        self.assertNotIn("pubkey_C85668DF69375001.gpg", self.install)
        self.assertIn("https://repository.spotify.com", body)

    def test_provider_package_names_match_zypper(self):
        keyring = extract_function(self.install, "spotify_keyring_packages_for_manager")
        qobuz = extract_function(self.install, "qobuz_runtime_packages_for_manager")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'{keyring}\n{qobuz}\nprintf "%s\\n%s\\n" "$(spotify_keyring_packages_for_manager zypper)" "$(qobuz_runtime_packages_for_manager zypper)"',
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            "gnome-keyring libsecret-1-0\nalsa libdbus-1-3 avahi libavahi-client3 nss-mdns\n",
        )

    def test_tidal_is_an_optional_python_dependency(self):
        self.assertNotIn("tidalapi", self.base_requirements)
        self.assertIn("tidalapi==0.8.11", self.tidal_requirements)
        self.assertIn("requirements-tidal.txt", self.install)
        self.assertIn("PKCE", self.install)

    def test_install_state_records_provider_ownership(self):
        for name in (
            '"spotify_desktop"',
            '"spotifyd"',
            '"qobuz"',
            '"tidal"',
            "installed_by_fxroute",
            "service_installed_by_fxroute",
        ):
            self.assertIn(name, self.install)
        for path_field in ("binary_path", "binary_sha256", "service_path", "service_sha256"):
            self.assertIn(path_field, self.install)
        self.assertIn("installed_version", self.install)

    def test_provider_state_and_install_root_are_checkpointed_before_validation(self):
        body = extract_function(self.install, "main")
        self.assertGreaterEqual(body.count("write_install_state"), 2)
        self.assertGreaterEqual(body.count("write_install_config"), 2)
        self.assertLess(body.index("write_install_config"), body.index("configure_optional_streaming"))
        self.assertLess(body.index("write_install_state"), body.index("validate_http"))
        self.assertLess(body.index("configure_optional_streaming"), body.index("write_service_unit"))

    def test_install_state_checkpoint_is_atomic_and_covers_lan_ownership(self):
        state_start = self.install.index("write_install_state() {")
        state_body = self.install[state_start : self.install.index("write_install_config() {", state_start)]
        ownership_body = extract_function(self.install, "load_provider_ownership_state")
        self.assertIn("mktemp", state_body)
        for field in (
            "hostname_before",
            "caddy_installed_by_fxroute",
            "default_caddy_disabled_by_fxroute",
            "http_opened_by_fxroute",
            "firewall_ownership_schema",
            "legacy_firewall_ownership_present",
            "firewalld_owned_rules",
            "ufw_owned_rules",
            "power_polkit_installed",
        ):
            self.assertIn(field, ownership_body)

    def test_spotify_apt_key_fingerprint_is_pinned(self):
        self.assertIn("SPOTIFY_APT_KEY_FINGERPRINT", self.install)
        self.assertIn("E1096BCBFF6D418796DE78515384CE82BA52C83A", self.install)

    def test_uninstaller_preserves_provider_data_by_default(self):
        for path in (
            ".config/spotifyd",
            ".cache/spotifyd",
            ".config/qbzd",
            ".local/share/qbzd",
            ".var/app/com.spotify.Client",
            "tidal-session.json",
        ):
            self.assertIn(path, self.uninstall)
        self.assertIn("installed_by_fxroute", self.uninstall)
        self.assertIn("confirm", self.uninstall)
        self.assertIn("PROVIDER_DATA_PATHS", self.uninstall)
        self.assertIn("preserved", self.uninstall.lower())

    def test_uninstaller_provider_data_reporter_succeeds_when_paths_are_absent(self):
        body = extract_function(self.uninstall, "preserve_provider_data")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"{body}\n"
                "log() { :; }\n"
                "PROVIDER_DATA_PATHS=(/definitely/missing/fxroute-provider-data)\n"
                "preserve_provider_data\n",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninstaller_provider_removal_requires_ownership_and_confirmation(self):
        for provider in ("spotifyd", "qobuz"):
            self.assertIn(f"providers.{provider}.installed_by_fxroute", self.uninstall)
            self.assertIn(f"providers.{provider}.service_installed_by_fxroute", self.uninstall)
        self.assertIn("providers.spotify_desktop.installed_by_fxroute", self.uninstall)
        self.assertIn("providers.tidal.installed_by_fxroute", self.uninstall)
        self.assertIn("remove_owned_streaming_components", self.uninstall)
        self.assertIn("Remove FXRoute-owned", self.uninstall)
        self.assertIn("verify_owned_binary_identity", self.uninstall)

    def test_tidal_ownership_survives_reruns_and_cleanup_preserves_session(self):
        ownership_body = extract_function(self.install, "load_provider_ownership_state")
        tidal_cleanup_body = extract_function(self.uninstall, "remove_owned_tidal_dependency")

        self.assertIn("providers.tidal.installed_by_fxroute", ownership_body)
        self.assertIn("providers.tidal.installed_by_fxroute", tidal_cleanup_body)
        self.assertIn("uninstall -y tidalapi", tidal_cleanup_body)
        self.assertIn("confirm", tidal_cleanup_body)
        self.assertNotIn("tidal-session.json", tidal_cleanup_body)

    def test_avahi_ownership_survives_installer_reruns(self):
        ownership_body = extract_function(self.install, "load_provider_ownership_state")
        self.assertIn("lan_comfort.avahi_installed_by_fxroute", ownership_body)
        self.assertIn("lan_comfort.avahi_enabled_by_fxroute", ownership_body)

    def test_provider_ownership_loader_succeeds_without_previous_state(self):
        previous_state_body = extract_function(self.install, "previous_install_state_field")
        ownership_body = extract_function(self.install, "load_provider_ownership_state")
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"{previous_state_body}\n{ownership_body}\n"
                "INSTALL_STATE_FILE=/definitely/missing/fxroute-state.json\n"
                "load_provider_ownership_state\n",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_interactive_provider_selection_returns_success_when_optional_prompts_are_declined(self):
        body = extract_function(self.install, "select_optional_providers")
        self.assertTrue(body.rstrip().endswith("return 0\n}"))

    def test_optional_provider_prompt_treats_eof_as_declined(self):
        body = extract_function(self.install, "prompt_optional_provider")
        result = subprocess.run(
            ["bash", "-c", f"{body}\nprompt_optional_provider spotifyd < /dev/null"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_uninstaller_keeps_ownership_state_when_provider_cleanup_is_deferred(self):
        self.assertIn("PRESERVE_INSTALL_STATE=0", self.uninstall)
        self.assertIn("PRESERVE_INSTALL_STATE -eq 0", self.uninstall)
        for provider in ("spotify_desktop", "spotifyd", "qobuz", "tidal"):
            function_name = {
                "spotify_desktop": "remove_owned_spotify_desktop",
                "spotifyd": "remove_owned_spotifyd",
                "qobuz": "remove_owned_qbzd",
                "tidal": "remove_owned_tidal_dependency",
            }[provider]
            self.assertIn("PRESERVE_INSTALL_STATE=1", extract_function(self.uninstall, function_name))

    def test_uninstaller_honors_the_recorded_custom_install_root(self):
        self.assertIn("INSTALL_CONFIG_FILE", self.uninstall)
        self.assertIn("FXROUTE_INSTALL_ROOT", self.uninstall)
        self.assertIn("INSTALL_ROOT_EXPLICIT", self.uninstall)
        extract_function(self.uninstall, "load_recorded_install_root")

    def test_uninstaller_refuses_dangerous_project_targets(self):
        guard_body = extract_function(self.uninstall, "validate_install_root_for_removal")
        containment_body = extract_function(self.uninstall, "path_is_within")
        self.assertIn('[[ -e "$root/.git" ]]', guard_body)
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"{containment_body}\n{guard_body}\n"
                "warn() { :; }\n"
                "HOME=/tmp/fxroute-test-home\n"
                "INSTALL_ROOT=/\n"
                "validate_install_root_for_removal\n",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_uninstaller_help_is_free_of_heredoc_command_substitution(self):
        result = subprocess.run(
            [str(UNINSTALL_SH), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("command not found", result.stderr)

    def test_uninstaller_can_remove_owned_avahi_on_pacman(self):
        self.assertIn("command -v pacman", self.uninstall)
        self.assertIn("pacman -R --noconfirm avahi", self.uninstall)

    def test_existing_installer_matrix_remains_present(self):
        for manager in ("apt", "dnf", "zypper", "pacman"):
            self.assertIn(f"{manager})", self.install)
        for dependency in ("pipewire", "wireplumber", "pipewire-pulse", "lv2ls", "smbclient"):
            self.assertIn(dependency, self.install)


if __name__ == "__main__":
    unittest.main(verbosity=2)
