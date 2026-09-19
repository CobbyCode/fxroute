#!/usr/bin/env python3
"""Contract tests for optional streaming installation support."""

import hashlib
import os
import re
import subprocess
import tempfile
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
        cls.installer_docs = (ROOT / "docs" / "INSTALLER.md").read_text()
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

    def test_spotifyd_release_matrix_uses_mpris_capable_builds(self):
        # No pinned version and no version-bound URL: the installed release
        # is the current stable upstream tag resolved at install time.
        self.assertNotRegex(self.install, r'SPOTIFYD_VERSION="[0-9]+\.[0-9]+\.[0-9]+"')
        self.assertIn('SPOTIFYD_UPSTREAM_REPO="Spotifyd/spotifyd"', self.install)
        self.assertIn("github_stable_release_json", self.install)
        body = extract_function(self.install, "install_spotifyd_binary")
        self.assertIn("spotifyd-linux-${release_arch}-full.tar.gz", body)
        self.assertNotIn("spotifyd-linux-x86_64-slim.tar.gz", body)
        self.assertNotIn("SPOTIFYD_ARM64_ARCHIVE", self.install)
        self.assertNotIn("SPOTIFYD_ARM64_DOWNLOAD_URL", self.install)
        self.assertNotIn("SPOTIFYD_ARM64_SHA256", self.install)
        self.assertNotIn("build_spotifyd_from_source", self.install)
        self.assertNotIn("rustup", self.install.lower())
        self.assertNotIn("cargo", self.install.lower())
        self.assertIn("spotifyd_arch_for_host", self.install)

    def test_spotifyd_aarch64_uses_the_official_upstream_full_build(self):
        body = extract_function(self.install, "install_spotifyd_binary")
        self.assertIn("aarch64", extract_function(self.install, "spotifyd_arch_for_host"))
        self.assertIn(".sha512", body)
        self.assertIn("verify_github_payload", body)
        self.assertFalse((ROOT / "scripts" / "build_spotifyd_arm64.sh").exists())
        self.assertFalse((ROOT / "docs" / "SPOTIFYD-ARM64-BUILD.md").exists())

    def _provider_upstream_helpers(self) -> str:
        return "\n".join(
            extract_function(self.install, name)
            for name in (
                "github_release_tag_name",
                "github_release_asset_digest",
                "normalize_release_tag",
                "verify_github_payload",
                "provider_binary_version",
                "provider_version_is_newer",
            )
        )

    def _github_curl_stub(self) -> str:
        # Serve upstream release payloads from a fixture directory keyed by
        # asset basename, so no network is needed. A missing fixture fails
        # the download exactly like a failed upstream fetch.
        return (
            "curl() {\n"
            '  local out="" url="" want_out=0 arg\n'
            '  for arg in "$@"; do\n'
            '    if [[ "$want_out" == 1 ]]; then out="$arg"; want_out=0; continue; fi\n'
            '    if [[ "$arg" == "-o" ]]; then want_out=1; continue; fi\n'
            '    url="$arg"\n'
            "  done\n"
            '  [[ -n "$out" && -n "$url" ]] || return 1\n'
            '  cp -f "$FIXTURES/$(basename "$url")" "$out" || return 1\n'
            "}\n"
        )

    def test_spotifyd_upstream_release_is_downloaded_verified_and_installed_atomically(self):
        body = extract_function(self.install, "install_spotifyd_binary")
        arch_helper = extract_function(self.install, "spotifyd_arch_for_host")
        path_helper = extract_function(self.install, "spotifyd_binary_path")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            source_dir = root / "source"
            source_dir.mkdir()
            source_binary = source_dir / "spotifyd"
            source_binary.write_text('#!/bin/sh\necho "Spotifyd 9.9.9"\n')
            source_binary.chmod(0o755)
            archive_name = "spotifyd-linux-x86_64-full.tar.gz"
            subprocess.run(
                ["tar", "-czf", str(fixtures / archive_name), "-C", str(source_dir), "spotifyd"],
                check=True,
            )
            archive_bytes = (fixtures / archive_name).read_bytes()
            archive_sha512 = hashlib.sha512(archive_bytes).hexdigest()
            (fixtures / f"{archive_name}.sha512").write_text(f"{archive_sha512}  {archive_name}\n")
            (fixtures / "release.json").write_text(
                '{"tag_name": "v9.9.9", "assets": [{"name": "%s", "digest": "sha256:%s"}]}'
                % (archive_name, hashlib.sha256(archive_bytes).hexdigest())
            )
            target_home = root / "home"
            harness = f"""
set -Eeuo pipefail
{arch_helper}
{path_helper}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
SPOTIFYD_UPSTREAM_REPO=Spotifyd/spotifyd
SPOTIFYD_INSTALLED_BY_FXROUTE=0
SPOTIFYD_BINARY_PATH=''
SPOTIFYD_BINARY_SHA256=''
SPOTIFYD_INSTALLED_VERSION=''
SPOTIFYD_BINARY_IDENTITY_CHANGED=0
SPOTIFYD_BINARY_UPDATED=0
SPOTIFYD_PRESENT_BEFORE=0
SPOTIFYD_PROVIDER_STATUS=''
install_spotifyd_binary
test -f "$HOME/.local/bin/spotifyd"
test -x "$HOME/.local/bin/spotifyd"
"$HOME/.local/bin/spotifyd" --version
printf 'version=%s updated=%s\\n' "$SPOTIFYD_INSTALLED_VERSION" "$SPOTIFYD_BINARY_UPDATED"
printf 'hash=%s\\n' "$SPOTIFYD_BINARY_SHA256"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Spotifyd 9.9.9", result.stdout)
            self.assertIn("version=9.9.9 updated=1", result.stdout)
            self.assertIn(
                hashlib.sha256(source_binary.read_bytes()).hexdigest(),
                result.stdout,
            )
        self.assertIn("staged_binary", body)
        self.assertIn('mv -f "$staged_binary" "$destination"', body)

    def test_spotifyd_checksum_failure_leaves_no_installed_binary(self):
        body = extract_function(self.install, "install_spotifyd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            source_binary = root / "spotifyd"
            source_binary.write_text("tampered fixture\n")
            source_binary.chmod(0o755)
            archive_name = "spotifyd-linux-x86_64-full.tar.gz"
            subprocess.run(
                ["tar", "-czf", str(fixtures / archive_name), "-C", str(root), "spotifyd"],
                check=True,
            )
            (fixtures / f"{archive_name}.sha512").write_text(
                "%s  %s\n" % ("0" * 128, archive_name)
            )
            (fixtures / "release.json").write_text(
                '{"tag_name": "v9.9.9", "assets": []}'
            )
            target_home = root / "home"
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "spotifyd_arch_for_host")}
{extract_function(self.install, "spotifyd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ :; }}
warn() {{ :; }}
die() {{ return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
SPOTIFYD_UPSTREAM_REPO=Spotifyd/spotifyd
SPOTIFYD_INSTALLED_BY_FXROUTE=0
SPOTIFYD_BINARY_PATH=''
SPOTIFYD_BINARY_SHA256=''
SPOTIFYD_INSTALLED_VERSION=''
SPOTIFYD_BINARY_IDENTITY_CHANGED=0
SPOTIFYD_BINARY_UPDATED=0
SPOTIFYD_PRESENT_BEFORE=0
SPOTIFYD_PROVIDER_STATUS=''
if install_spotifyd_binary; then
  printf 'unexpected-success\\n'
else
  printf 'checksum-rejected\\n'
fi
test ! -e "$HOME/.local/bin/spotifyd"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("checksum-rejected", result.stdout)

    def test_spotifyd_owned_binary_updates_to_newer_upstream_without_uninstall(self):
        body = extract_function(self.install, "install_spotifyd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            source_dir = root / "source"
            source_dir.mkdir()
            new_binary = source_dir / "spotifyd"
            new_binary.write_text('#!/bin/sh\necho "Spotifyd 9.9.9"\n')
            new_binary.chmod(0o755)
            archive_name = "spotifyd-linux-x86_64-full.tar.gz"
            subprocess.run(
                ["tar", "-czf", str(fixtures / archive_name), "-C", str(source_dir), "spotifyd"],
                check=True,
            )
            archive_bytes = (fixtures / archive_name).read_bytes()
            (fixtures / f"{archive_name}.sha512").write_text(
                f"{hashlib.sha512(archive_bytes).hexdigest()}  {archive_name}\n"
            )
            (fixtures / "release.json").write_text('{"tag_name": "v9.9.9", "assets": []}')
            target_home = root / "home"
            owned_dir = target_home / ".local" / "bin"
            owned_dir.mkdir(parents=True)
            old_binary = owned_dir / "spotifyd"
            old_binary.write_text('#!/bin/sh\necho "Spotifyd 9.9.8"\n')
            old_binary.chmod(0o755)
            old_sha = hashlib.sha256(old_binary.read_bytes()).hexdigest()
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "spotifyd_arch_for_host")}
{extract_function(self.install, "spotifyd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
SPOTIFYD_UPSTREAM_REPO=Spotifyd/spotifyd
SPOTIFYD_INSTALLED_BY_FXROUTE=1
SPOTIFYD_BINARY_PATH="$HOME/.local/bin/spotifyd"
SPOTIFYD_BINARY_SHA256={old_sha}
SPOTIFYD_INSTALLED_VERSION=9.9.8
SPOTIFYD_BINARY_IDENTITY_CHANGED=0
SPOTIFYD_BINARY_UPDATED=0
SPOTIFYD_PRESENT_BEFORE=0
SPOTIFYD_PROVIDER_STATUS=''
install_spotifyd_binary
"$HOME/.local/bin/spotifyd" --version
printf 'version=%s updated=%s\\n' "$SPOTIFYD_INSTALLED_VERSION" "$SPOTIFYD_BINARY_UPDATED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("updating to v9.9.9", result.stdout)
            self.assertIn("Spotifyd 9.9.9", result.stdout)
            self.assertIn("version=9.9.9 updated=1", result.stdout)

    def test_spotifyd_up_to_date_binary_is_kept_without_download(self):
        body = extract_function(self.install, "install_spotifyd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "release.json").write_text('{"tag_name": "v9.9.9", "assets": []}')
            target_home = root / "home"
            owned_dir = target_home / ".local" / "bin"
            owned_dir.mkdir(parents=True)
            current_binary = owned_dir / "spotifyd"
            current_binary.write_text('#!/bin/sh\necho "Spotifyd 9.9.9"\n')
            current_binary.chmod(0o755)
            current_sha = hashlib.sha256(current_binary.read_bytes()).hexdigest()
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "spotifyd_arch_for_host")}
{extract_function(self.install, "spotifyd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
SPOTIFYD_UPSTREAM_REPO=Spotifyd/spotifyd
SPOTIFYD_INSTALLED_BY_FXROUTE=1
SPOTIFYD_BINARY_PATH="$HOME/.local/bin/spotifyd"
SPOTIFYD_BINARY_SHA256={current_sha}
SPOTIFYD_INSTALLED_VERSION=9.9.9
SPOTIFYD_BINARY_IDENTITY_CHANGED=0
SPOTIFYD_BINARY_UPDATED=0
SPOTIFYD_PRESENT_BEFORE=0
SPOTIFYD_PROVIDER_STATUS=''
install_spotifyd_binary
printf 'version=%s updated=%s\\n' "$SPOTIFYD_INSTALLED_VERSION" "$SPOTIFYD_BINARY_UPDATED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already up to date (v9.9.9)", result.stdout)
            self.assertIn("version=9.9.9 updated=0", result.stdout)

    def test_spotifyd_archive_binary_is_installed_even_without_archive_exec_bit(self):
        body = extract_function(self.install, "install_spotifyd_binary")
        self.assertIn('find "$work" -type f -name spotifyd -print -quit', body)
        self.assertNotIn('find "$work" -type f -name spotifyd -perm -u+x', body)
        self.assertIn('install -m 755 "$extracted"', body)

    def test_provider_archive_cleanup_does_not_expand_a_local_variable_after_return(self):
        self.assertIn("cleanup_active_temp_dir", self.install)
        self.assertIn("FXROUTE_ACTIVE_TEMP_DIR", self.install)
        for function_name in ("install_spotifyd_binary", "install_qbzd_binary"):
            body = extract_function(self.install, function_name)
            self.assertNotIn("trap 'rm -rf \"$work\"' RETURN", body)
            self.assertIn('trap - RETURN', body)
            self.assertIn('rm -rf "$work"', body)

    def test_active_provider_temp_dir_is_removed_on_fatal_exit(self):
        cleanup = extract_function(self.install, "cleanup_active_temp_dir")
        with tempfile.TemporaryDirectory() as td:
            active_dir = Path(td) / "active-provider-work"
            staged_binary = Path(td) / ".spotifyd.staged"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"set -Eeuo pipefail\n{cleanup}\n"
                    f"mkdir -p {active_dir}\n"
                    f"printf staged > {staged_binary}\n"
                    f"FXROUTE_ACTIVE_TEMP_DIR={active_dir}\n"
                    f"FXROUTE_ACTIVE_STAGED_BINARY={staged_binary}\n"
                    "trap cleanup_active_temp_dir EXIT\n"
                    "exit 17\n",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertFalse(active_dir.exists())
            self.assertFalse(staged_binary.exists())

    def test_spotifyd_runtime_libraries_are_checked_before_service_setup(self):
        runtime_check = extract_function(self.install, "spotifyd_runtime_missing_libraries")
        installer = extract_function(self.install, "install_spotifyd")
        self.assertIn("spotifyd_runtime_missing_libraries", installer)
        self.assertIn("if ! missing_runtime=", installer)
        self.assertNotIn("|| true", runtime_check)
        self.assertRegex(installer, r"if ! user_systemctl disable --now spotifyd\.service")
        self.assertLess(installer.index("spotifyd_runtime_missing_libraries"),
                        installer.index("write_spotifyd_config"))
        with tempfile.TemporaryDirectory() as td:
            fake_ldd = Path(td) / "ldd"
            fake_binary = Path(td) / "spotifyd"
            fake_ldd.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$FAKE_LDD_OUTPUT\"\n"
                "exit \"${FAKE_LDD_STATUS:-0}\"\n"
            )
            fake_binary.write_text("binary\n")
            fake_ldd.chmod(0o755)
            fake_binary.chmod(0o755)
            cases = (
                ("libssl.so.1.1 => not found", "0", "libssl.so.1.1"),
                (
                    "/tmp/spotifyd: /lib/libc.so.6: version `GLIBC_2.38' not found (required by /tmp/spotifyd)",
                    "0",
                    "/tmp/spotifyd: /lib/libc.so.6: version `GLIBC_2.38' not found (required by /tmp/spotifyd)",
                ),
            )
            for output, status, expected in cases:
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f'set -euo pipefail\nrun_as_target_user() {{ "$@"; }}\n{runtime_check}\nmissing="$(spotifyd_runtime_missing_libraries {fake_binary})"\nprintf "%s\\n" "$missing"\n',
                    ],
                    env={
                        **os.environ,
                        "PATH": f"{td}:/usr/bin:/bin",
                        "FAKE_LDD_OUTPUT": output,
                        "FAKE_LDD_STATUS": status,
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_spotifyd_docs_describe_official_upstream_aarch64_build(self):
        for detail in ("aarch64", ".sha512", "ldd"):
            self.assertIn(detail, self.installer_docs)
        self.assertNotIn("SPOTIFYD_ARM64_ARTIFACT_VERSION", self.install)
        self.assertNotIn("SPOTIFYD-ARM64-BUILD", self.installer_docs)
        self.assertNotIn("build_spotifyd_from_source", self.install)

    def test_spotifyd_runtime_failure_keeps_prebuilt_unavailable_without_building(self):
        body = extract_function(self.install, "install_spotifyd")
        self.assertIn("spotifyd_runtime_missing_libraries", body)
        self.assertIn("upstream prebuilt", body)
        self.assertIn("service_disable_failed", body)
        self.assertNotIn("build_spotifyd", body)

    def test_spotifyd_legacy_source_built_state_is_preserved_without_a_build_path(self):
        self.assertIn("SPOTIFYD_SOURCE_BUILT", self.install)
        state_body = extract_function(self.install, "write_install_state")
        ownership_body = extract_function(self.install, "load_provider_ownership_state")
        self.assertIn('"source_built"', state_body)
        self.assertIn("providers.spotifyd.source_built", ownership_body)

    def test_spotifyd_docs_describe_unity_volume_configuration(self):
        self.assertIn('volume_controller = "none"', self.installer_docs)
        self.assertIn("playback", self.installer_docs)
        self.assertIn("volume levels", self.installer_docs)

    def test_spotifyd_config_has_device_mpris_and_pipewire_without_credentials_or_volume(self):
        body = extract_function(self.install, "write_spotifyd_config")
        for setting in (
            "fxroute_spotify_connect_name",
            'backend = "pulseaudio"',
            "use_mpris = true",
            'dbus_type = "session"',
            # Remote Connect volume is bridged to the FXRoute master; the
            # source must never attenuate itself (unity contract).
            'volume_controller = "none"',
        ):
            self.assertIn(setting, body)
        # The Connect name is short, unique, and never the bare default.
        self.assertIn('device_name = "${connect_name}"', body)
        self.assertNotIn('device_name = "FXRoute"\n', body)
        for forbidden in ("username", "password", "volume =", ".local"):
            self.assertNotIn(forbidden, body)

    def test_spotifyd_rerun_syncs_managed_names_without_touching_hostname_or_dns(self):
        body = extract_function(self.install, "sync_spotifyd_device_name")
        self.assertIn("fxroute_spotify_connect_name", body)
        for forbidden in ("hostnamectl", "avahi", "caddy", ".local"):
            self.assertNotIn(forbidden.lower(), body.lower())
        rerun = extract_function(self.install, "install_spotifyd")
        self.assertIn("sync_spotifyd_device_name", extract_function(self.install, "write_spotifyd_config"))
        self.assertIn("SPOTIFYD_DEVICE_NAME_CHANGED", rerun)

    def test_owned_provider_services_are_reenabled_on_rerun(self):
        spotifyd = extract_function(self.install, "configure_spotifyd_service")
        qbzd = extract_function(self.install, "configure_qbzd_service")
        self.assertIn("SPOTIFYD_SERVICE_INSTALLED_BY_FXROUTE -eq 1", spotifyd)
        self.assertIn("user_systemctl enable --now spotifyd.service", spotifyd)
        self.assertIn("QBZD_SERVICE_INSTALLED_BY_FXROUTE -eq 1", qbzd)
        self.assertIn("user_systemctl enable --now qbzd.service", qbzd)

    def test_qobuz_release_matrix_is_dynamic(self):
        # No pinned version, no pinned checksum, no version-bound URL: the
        # installed release is the current stable tag of the preferred
        # available source (official upstream first, compatible fork as
        # fallback), resolved at install time like spotifyd.
        self.assertNotRegex(self.install, r'QBZD_VERSION="[0-9]+\.[0-9]+\.[0-9]+"')
        self.assertIn('QBZD_UPSTREAM_REPO="vicrodh/qbz"', self.install)
        self.assertIn('QBZD_UPSTREAM_FALLBACK_REPO="yet-another-quentin/qbzd"', self.install)
        body = extract_function(self.install, "install_qbzd_binary")
        self.assertIn('first_asset="qbzd-${upstream_version}-linux-${release_arch}.tar.gz"', body)
        self.assertIn('first_asset="qbzd-linux-${asset_arch}"', body)
        self.assertIn("releases/latest", extract_function(self.install, "github_stable_release_json"))
        self.assertIn("verify_github_payload", body)
        self.assertIn("already up to date", body)
        self.assertIn("updating to ${upstream_tag}", body)
        self.assertIn("QBZD_UPSTREAM_SOURCE", body)
        self.assertIn("provider_version_is_newer", body)

    def test_qobuz_owned_binary_updates_to_newer_upstream_without_uninstall(self):
        body = extract_function(self.install, "install_qbzd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            source_dir = root / "source"
            (source_dir / "qbzd-1.0.1-linux-amd64").mkdir(parents=True)
            new_binary = source_dir / "qbzd-1.0.1-linux-amd64" / "qbzd"
            new_binary.write_text('#!/bin/sh\necho "qbzd 1.0.1"\n')
            new_binary.chmod(0o755)
            archive_name = "qbzd-1.0.1-linux-amd64.tar.gz"
            subprocess.run(
                ["tar", "-czf", str(fixtures / archive_name), "-C", str(source_dir), "qbzd-1.0.1-linux-amd64"],
                check=True,
            )
            archive_bytes = (fixtures / archive_name).read_bytes()
            (fixtures / "release.json").write_text(
                '{"tag_name": "v1.0.1", "assets": [{"name": "%s", "digest": "sha256:%s"}]}'
                % (archive_name, hashlib.sha256(archive_bytes).hexdigest())
            )
            target_home = root / "home"
            owned_dir = target_home / ".local" / "bin"
            owned_dir.mkdir(parents=True)
            old_binary = owned_dir / "qbzd"
            old_binary.write_text('#!/bin/sh\necho "qbzd 1.0.0"\n')
            old_binary.chmod(0o755)
            old_sha = hashlib.sha256(old_binary.read_bytes()).hexdigest()
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "qbzd_arch_for_host")}
{extract_function(self.install, "qbzd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
QBZD_UPSTREAM_REPO=vicrodh/qbz
QBZD_UPSTREAM_FALLBACK_REPO=yet-another-quentin/qbzd
QBZD_INSTALLED_BY_FXROUTE=1
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_BINARY_PATH="$HOME/.local/bin/qbzd"
QBZD_BINARY_SHA256={old_sha}
QBZD_INSTALLED_VERSION=1.0.0
QBZD_UPSTREAM_SOURCE=vicrodh/qbz
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_BINARY_UPDATED=0
QBZD_PRESENT_BEFORE=0
QOBUZ_PROVIDER_STATUS=''
install_qbzd_binary
"$HOME/.local/bin/qbzd" --version
printf 'version=%s source=%s updated=%s\\n' "$QBZD_INSTALLED_VERSION" "$QBZD_UPSTREAM_SOURCE" "$QBZD_BINARY_UPDATED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("updating to v1.0.1", result.stdout)
            self.assertIn("qbzd 1.0.1", result.stdout)
            self.assertIn("version=1.0.1 source=vicrodh/qbz updated=1", result.stdout)

    def test_qobuz_up_to_date_binary_is_kept_without_download(self):
        body = extract_function(self.install, "install_qbzd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "release.json").write_text('{"tag_name": "v1.0.1", "assets": []}')
            target_home = root / "home"
            owned_dir = target_home / ".local" / "bin"
            owned_dir.mkdir(parents=True)
            current_binary = owned_dir / "qbzd"
            current_binary.write_text('#!/bin/sh\necho "qbzd 1.0.1"\n')
            current_binary.chmod(0o755)
            current_sha = hashlib.sha256(current_binary.read_bytes()).hexdigest()
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "qbzd_arch_for_host")}
{extract_function(self.install, "qbzd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
QBZD_UPSTREAM_REPO=vicrodh/qbz
QBZD_UPSTREAM_FALLBACK_REPO=yet-another-quentin/qbzd
QBZD_INSTALLED_BY_FXROUTE=1
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_BINARY_PATH="$HOME/.local/bin/qbzd"
QBZD_BINARY_SHA256={current_sha}
QBZD_INSTALLED_VERSION=1.0.1
QBZD_UPSTREAM_SOURCE=vicrodh/qbz
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_BINARY_UPDATED=0
QBZD_PRESENT_BEFORE=0
QOBUZ_PROVIDER_STATUS=''
install_qbzd_binary
printf 'version=%s updated=%s\\n' "$QBZD_INSTALLED_VERSION" "$QBZD_BINARY_UPDATED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already up to date (v1.0.1)", result.stdout)
            self.assertIn("version=1.0.1 updated=0", result.stdout)

    def test_qobuz_falls_back_to_fork_without_stable_official_release(self):
        body = extract_function(self.install, "install_qbzd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            new_binary = fixtures / "qbzd-linux-amd64"
            new_binary.write_text('#!/bin/sh\necho "qbzd 1.0.1"\n')
            new_binary.chmod(0o755)
            new_digest = hashlib.sha256(new_binary.read_bytes()).hexdigest()
            (fixtures / "release.json").write_text(
                '{"tag_name": "v1.0.1", "assets": [{"name": "qbzd-linux-amd64", "digest": "sha256:%s"}]}'
                % new_digest
            )
            target_home = root / "home"
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "qbzd_arch_for_host")}
{extract_function(self.install, "qbzd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{
  if [[ ! -f "$FIXTURES/primary-failed" ]]; then : > "$FIXTURES/primary-failed"; return 1; fi
  cat "$FIXTURES/release.json"
}}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
QBZD_UPSTREAM_REPO=vicrodh/qbz
QBZD_UPSTREAM_FALLBACK_REPO=yet-another-quentin/qbzd
QBZD_INSTALLED_BY_FXROUTE=0
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_BINARY_PATH=''
QBZD_BINARY_SHA256=''
QBZD_INSTALLED_VERSION=''
QBZD_UPSTREAM_SOURCE=''
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_BINARY_UPDATED=0
QBZD_PRESENT_BEFORE=0
QOBUZ_PROVIDER_STATUS=''
install_qbzd_binary
"$HOME/.local/bin/qbzd" --version
printf 'version=%s source=%s updated=%s\\n' "$QBZD_INSTALLED_VERSION" "$QBZD_UPSTREAM_SOURCE" "$QBZD_BINARY_UPDATED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("qbzd 1.0.1", result.stdout)
            self.assertIn("version=1.0.1 source=yet-another-quentin/qbzd updated=1", result.stdout)

    def test_qobuz_existing_install_survives_unreachable_upstreams(self):
        body = extract_function(self.install, "install_qbzd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            target_home = root / "home"
            owned_dir = target_home / ".local" / "bin"
            owned_dir.mkdir(parents=True)
            current_binary = owned_dir / "qbzd"
            current_binary.write_text('#!/bin/sh\necho "qbzd 1.0.1"\n')
            current_binary.chmod(0o755)
            before_sha = hashlib.sha256(current_binary.read_bytes()).hexdigest()
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "qbzd_arch_for_host")}
{extract_function(self.install, "qbzd_binary_path")}
{self._provider_upstream_helpers()}
{body}
github_stable_release_json() {{ return 1; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
QBZD_UPSTREAM_REPO=vicrodh/qbz
QBZD_UPSTREAM_FALLBACK_REPO=yet-another-quentin/qbzd
QBZD_INSTALLED_BY_FXROUTE=1
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_BINARY_PATH="$HOME/.local/bin/qbzd"
QBZD_BINARY_SHA256={before_sha}
QBZD_INSTALLED_VERSION=1.0.1
QBZD_UPSTREAM_SOURCE=vicrodh/qbz
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_BINARY_UPDATED=0
QBZD_PRESENT_BEFORE=0
QOBUZ_PROVIDER_STATUS=''
if install_qbzd_binary; then
  printf 'unexpected-success\\n'
else
  printf 'upstream-unreachable\\n'
fi
printf 'sha=%s updated=%s status=%s\\n' "$(sha256sum "$HOME/.local/bin/qbzd" | awk '{{print $1}}')" "$QBZD_BINARY_UPDATED" "$QOBUZ_PROVIDER_STATUS"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("upstream-unreachable", result.stdout)
            self.assertIn(f"sha={before_sha} updated=0", result.stdout)
            self.assertIn("status=unavailable; upstream release metadata unreachable", result.stdout)

    def test_qobuz_never_downgrades_to_an_older_upstream_tag(self):
        body = extract_function(self.install, "install_qbzd_binary")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            (fixtures / "release.json").write_text('{"tag_name": "v1.0.1", "assets": []}')
            target_home = root / "home"
            owned_dir = target_home / ".local" / "bin"
            owned_dir.mkdir(parents=True)
            current_binary = owned_dir / "qbzd"
            current_binary.write_text('#!/bin/sh\necho "qbzd 1.0.2"\n')
            current_binary.chmod(0o755)
            current_sha = hashlib.sha256(current_binary.read_bytes()).hexdigest()
            harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "qbzd_arch_for_host")}
{extract_function(self.install, "qbzd_binary_path")}
{self._provider_upstream_helpers()}
{body}
{self._github_curl_stub()}
github_stable_release_json() {{ cat "$FIXTURES/release.json"; }}
run_cmd() {{ "$@"; }}
run_as_target_user() {{ "$@"; }}
pass() {{ printf 'pass:%s\\n' "$*"; }}
warn() {{ printf '%s\\n' "$*" >&2; }}
die() {{ printf '%s\\n' "$*" >&2; return 1; }}
HOME={target_home}
FIXTURES={fixtures}
HOST_ARCH=x86_64
QBZD_UPSTREAM_REPO=vicrodh/qbz
QBZD_UPSTREAM_FALLBACK_REPO=yet-another-quentin/qbzd
QBZD_INSTALLED_BY_FXROUTE=1
QBZD_VOLUME_MODE_CHANGED_BY_FXROUTE=0
QBZD_BINARY_PATH="$HOME/.local/bin/qbzd"
QBZD_BINARY_SHA256={current_sha}
QBZD_INSTALLED_VERSION=1.0.2
QBZD_UPSTREAM_SOURCE=vicrodh/qbz
QBZD_BINARY_IDENTITY_CHANGED=0
QBZD_BINARY_UPDATED=0
QBZD_PRESENT_BEFORE=0
QOBUZ_PROVIDER_STATUS=''
install_qbzd_binary
printf 'version=%s updated=%s\\n' "$QBZD_INSTALLED_VERSION" "$QBZD_BINARY_UPDATED"
"""
            result = subprocess.run(
                ["bash", "-c", harness],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already up to date (v1.0.1)", result.stdout)
            self.assertIn("version=1.0.2 updated=0", result.stdout)

    def test_provider_upstream_resolution_is_shared_and_version_free(self):
        for helper in (
            "github_stable_release_json",
            "github_release_tag_name",
            "github_release_asset_digest",
            "normalize_release_tag",
            "provider_binary_version",
            "verify_github_payload",
        ):
            extract_function(self.install, helper)
        for provider_fn in ("install_spotifyd_binary", "install_qbzd_binary"):
            body = extract_function(self.install, provider_fn)
            self.assertIn("github_stable_release_json", body)
            self.assertIn("releases/download/${upstream_tag}/", body)
            self.assertIn("verify_github_payload", body)
            self.assertIn("already up to date", body)
            self.assertIn("updating to ${upstream_tag}", body)

    def test_qobuz_install_survives_a_failed_qbzd_download(self):
        """A transient prebuilt-download failure must not abort the installer.

        install_qobuz() calls install_qbzd_binary() bare (as in production,
        under set -Eeuo pipefail); it must tolerate the failure itself
        instead of letting errexit kill the whole run.
        """
        harness = f"""
set -Eeuo pipefail
{extract_function(self.install, "install_qobuz")}
qbzd_binary_path() {{ printf ''; }}
qbzd_arch_for_host() {{ return 0; }}
install_qbzd_binary() {{ return 1; }}
warn() {{ :; }}
QBZD_BINARY_IDENTITY_CHANGED=0
QOBUZ_PROVIDER_STATUS=''
install_qobuz
printf 'caller-tolerated status=<%s>\\n' "$QOBUZ_PROVIDER_STATUS"
"""
        result = subprocess.run(
            ["bash", "-c", harness],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "caller-tolerated status=<unavailable; prebuilt download or verification failed>",
            result.stdout,
        )

    def test_new_spotify_apt_source_refreshes_package_metadata(self):
        body = extract_function(self.install, "install_spotify_desktop_apt")
        self.assertIn("SPOTIFY_DESKTOP_REPO_INSTALLED_BY_FXROUTE=1", body)
        self.assertIn("PKG_REFRESH_DONE=0", body)

    def test_spotify_apt_key_cleanup_requires_owned_source_removal(self):
        body = extract_function(self.uninstall, "remove_owned_spotify_desktop")
        self.assertIn("apt_source_removed=0", body)
        self.assertIn("&& $apt_source_removed -eq 1", body)

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

    def test_qobuz_runtime_uses_debian_t64_alsa_package_when_available(self):
        qobuz = extract_function(self.install, "qobuz_runtime_packages_for_manager")
        with tempfile.TemporaryDirectory() as td:
            fake_apt_cache = Path(td) / "apt-cache"
            fake_apt_cache.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = show ] && [ \"$2\" = libasound2t64 ]; then\n"
                "  exit \"${APT_CACHE_T64_STATUS:-1}\"\n"
                "fi\n"
                "exit 1\n"
            )
            fake_apt_cache.chmod(0o755)
            for status, expected_alsa in (("0", "libasound2t64"), ("1", "libasound2")):
                result = subprocess.run(
                    [
                        "bash",
                        "-c",
                        f'{qobuz}\nprintf "%s\\n" "$(qobuz_runtime_packages_for_manager apt)"\n',
                    ],
                    env={
                        **os.environ,
                        "PATH": f"{td}:/usr/bin:/bin",
                        "APT_CACHE_T64_STATUS": status,
                    },
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(),
                    f"{expected_alsa} libdbus-1-3 avahi-daemon libavahi-client3 libnss-mdns",
                )

    def test_provider_setup_ensures_target_user_cache_ownership(self):
        # qbzd fatally requires a writable user cache dir; provider setup
        # must repair a foreign-owned ~/.cache before any daemon can start.
        body = extract_function(self.install, "configure_optional_streaming")
        self.assertIn("ensure_target_user_cache_ownership", body)
        self.assertLess(
            body.index("ensure_target_user_cache_ownership"),
            body.index("detect_existing_provider_components"),
        )
        cache = extract_function(self.install, "ensure_target_user_cache_ownership")
        self.assertIn("$HOME/.cache", cache)
        self.assertIn("run_as_target_user mkdir -p", cache)
        self.assertIn("symlink", cache)
        self.assertIn("chown", cache)

    def test_cache_ownership_repair_creates_missing_dir_and_refuses_symlinks(self):
        cache = extract_function(self.install, "ensure_target_user_cache_ownership")
        helpers = extract_function(self.install, "path_has_symlink_component")
        preamble = (
            "run_as_target_user() { \"$@\"; }\n"
            "log() { printf '[fxroute] %s\\n' \"$*\"; }\n"
            "die() { printf '[fxroute][error] %s\\n' \"$*\" >&2; exit 1; }\n"
        )
        me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        gid = subprocess.run(["id", "-gn"], capture_output=True, text=True).stdout.strip()
        uid = str(os.getuid())
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "FXROUTE_TARGET_USER": me,
                "FXROUTE_TARGET_UID": uid,
                "FXROUTE_TARGET_GROUP": gid,
            }
            # Missing cache dir is created for the target user.
            result = subprocess.run(
                ["bash", "-c", f"{preamble}\n{helpers}\n{cache}\nensure_target_user_cache_ownership"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((home / ".cache").is_dir())
            # An already correctly owned dir is accepted untouched.
            result = subprocess.run(
                ["bash", "-c", f"{preamble}\n{helpers}\n{cache}\nensure_target_user_cache_ownership"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            # A symlinked cache dir is refused (no chown across links).
            (home / ".cache").rmdir()
            (home / "real-cache").mkdir()
            (home / ".cache").symlink_to(home / "real-cache")
            result = subprocess.run(
                ["bash", "-c", f"{preamble}\n{helpers}\n{cache}\nensure_target_user_cache_ownership"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlink", result.stderr)

    def test_qconnect_enable_runs_after_volume_and_before_service(self):
        body = extract_function(self.install, "install_qobuz")
        self.assertLess(
            body.index("configure_qbzd_volume_mode"), body.index("configure_qbzd_qconnect")
        )
        self.assertLess(
            body.index("configure_qbzd_qconnect"), body.index("configure_qbzd_service")
        )

    def test_qconnect_enable_is_idempotent_and_leaves_name_alone(self):
        configurator = extract_function(self.install, "configure_qbzd_qconnect")
        self.assertIn("qconnect enable", configurator)
        self.assertIn("already enabled", configurator)
        self.assertNotIn("qconnect name", configurator)
        self.assertLess(
            configurator.index("QBZD_QCONNECT_CHANGED_BY_FXROUTE=1"),
            configurator.index("qconnect enable"),
        )

    def test_existing_service_restarts_on_qconnect_change(self):
        service = extract_function(self.install, "configure_qbzd_service")
        self.assertIn("QBZD_QCONNECT_CHANGED_BY_FXROUTE -eq 1", service)
        self.assertIn("user_systemctl restart qbzd.service", service)

    def test_install_state_records_qconnect_ownership(self):
        for field in (
            "qconnect_startup_mode_before",
            "qconnect_startup_mode_after",
            "qconnect_changed_by_fxroute",
        ):
            self.assertIn(field, self.install)

    def test_qconnect_configure_enables_and_rechecks_with_fake_qbzd(self):
        reader = extract_function(self.install, "read_qbzd_qconnect_startup_mode")
        configurator = extract_function(self.install, "configure_qbzd_qconnect")
        path_reader = extract_function(self.install, "qbzd_binary_path")
        preamble = (
            "run_as_target_user() { \"$@\"; }\n"
            "log() { :; }\n"
            "pass() { printf '[pass] %s\\n' \"$*\"; }\n"
            "die() { printf '[fxroute][error] %s\\n' \"$*\" >&2; exit 1; }\n"
        )
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            provider_dir = home / ".local" / "bin"
            provider_dir.mkdir(parents=True)
            mode_file = Path(td) / "mode"
            calls_file = Path(td) / "calls"
            fake_qbzd = provider_dir / "qbzd"
            fake_qbzd.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == settings && $2 == show ]]; then\n"
                "  printf '{\"qconnect.startup_mode\":\"%s\"}\\n' \"$(<\"$MODE_FILE\")\"\n"
                "elif [[ $1 == qconnect && $2 == enable ]]; then\n"
                "  printf 'enable\\n' >> \"$CALLS_FILE\"\n"
                "  printf '%s' 'on' > \"$MODE_FILE\"\n"
                "else\n"
                "  exit 1\n"
                "fi\n"
            )
            fake_qbzd.chmod(0o755)
            harness = (
                f"{preamble}\n{path_reader}\n{reader}\n{configurator}\n"
                "QBZD_QCONNECT_STARTUP_MODE_BEFORE=\"\"\n"
                "QBZD_QCONNECT_STARTUP_MODE_AFTER=\"\"\n"
                "QBZD_QCONNECT_CHANGED_BY_FXROUTE=0\n"
                "configure_qbzd_qconnect\n"
                "printf 'mode=%s before=%s changed=%s calls=%s\\n' "
                "\"$(<\"$MODE_FILE\")\" \"$QBZD_QCONNECT_STARTUP_MODE_BEFORE\" "
                "\"$QBZD_QCONNECT_CHANGED_BY_FXROUTE\" \"$(<\"$CALLS_FILE\")\"\n"
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
                "MODE_FILE": str(mode_file),
                "CALLS_FILE": str(calls_file),
            }
            # Disabled renderer is enabled, verified, and recorded.
            mode_file.write_text("off")
            calls_file.write_text("")
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("mode=on before=off changed=1 calls=enable", result.stdout)
            # Already enabled renderer is left untouched.
            mode_file.write_text("on")
            calls_file.write_text("")
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already enabled", result.stdout)
            self.assertIn("mode=on before= changed=0 calls=", result.stdout)
            # Unreadable settings abort the run instead of leaving Connect off.
            fake_qbzd.write_text("#!/usr/bin/env bash\nexit 1\n")
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            self.assertNotEqual(result.returncode, 0)

    def test_qbzd_alsa_pipewire_bridge_is_backports_aware(self):
        body = extract_function(self.install, "ensure_qobuz_runtime_dependencies")
        self.assertIn("ensure_qbzd_alsa_pipewire_bridge", body)
        bridge = extract_function(self.install, "ensure_qbzd_alsa_pipewire_bridge")
        self.assertIn("pipewire-alsa", bridge)
        self.assertIn("apt-get install -y -t trixie-backports pipewire-alsa", bridge)
        self.assertIn('"$PACKAGE_MANAGER" != "apt"', bridge)

    def test_qbzd_alsa_bridge_install_matches_backports_stack(self):
        bridge = extract_function(self.install, "ensure_qbzd_alsa_pipewire_bridge")
        preamble = (
            "package_installed() { return \"$PACKAGE_INSTALLED_RC\"; }\n"
            "debian_trixie_backports_available() { return \"$BACKPORTS_RC\"; }\n"
            "run_cmd() { printf 'run:%s\\n' \"$*\" >> \"$CALLS_FILE\"; return 0; }\n"
            "pkg_install() { printf 'pkg:%s\\n' \"$*\" >> \"$CALLS_FILE\"; return 0; }\n"
            "provider_privileged() { printf 'helper:%s\\n' \"$*\" >> \"$CALLS_FILE\"; return 0; }\n"
            "log() { :; }\n"
            "pass() { :; }\n"
            "die() { printf '[fxroute][error] %s\\n' \"$*\" >&2; exit 1; }\n"
        )
        with tempfile.TemporaryDirectory() as td:
            calls_file = Path(td) / "calls"
            harness = (
                f"{preamble}\n{bridge}\n"
                "PACKAGE_MANAGER=apt\n"
                "PKG_REFRESH_DONE=1\n"
                "SUDO_CMD=()\n"
                "PROVIDERS_ONLY_MODE=0\n"
                "ensure_qbzd_alsa_pipewire_bridge\n"
            )
            for installed_rc, backports_rc, expected in (
                ("0", "0", ""),
                ("1", "0", "run:apt-get install -y -t trixie-backports pipewire-alsa"),
                ("1", "1", "pkg:pipewire-alsa"),
            ):
                calls_file.write_text("")
                result = subprocess.run(
                    ["bash", "-c", harness],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": "/usr/bin:/bin",
                        "CALLS_FILE": str(calls_file),
                        "PACKAGE_INSTALLED_RC": installed_rc,
                        "BACKPORTS_RC": backports_rc,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls_file.read_text().strip(), expected)
            # Providers-only path: backports go through the fixed helper
            # action, plain installs through pkg_install -> helper packages.
            helper_harness = (
                f"{preamble}\n{bridge}\n"
                "PACKAGE_MANAGER=apt\n"
                "PKG_REFRESH_DONE=1\n"
                "SUDO_CMD=()\n"
                "PROVIDERS_ONLY_MODE=1\n"
                "ensure_qbzd_alsa_pipewire_bridge\n"
            )
            for backports_rc, expected in (
                ("0", "run:provider_privileged backports-pipewire-alsa"),
                ("1", "pkg:pipewire-alsa"),
            ):
                calls_file.write_text("")
                result = subprocess.run(
                    ["bash", "-c", helper_harness],
                    capture_output=True,
                    text=True,
                    env={
                        **os.environ,
                        "PATH": "/usr/bin:/bin",
                        "CALLS_FILE": str(calls_file),
                        "PACKAGE_INSTALLED_RC": "1",
                        "BACKPORTS_RC": backports_rc,
                    },
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls_file.read_text().strip(), expected)

    def test_qbzd_audio_output_runs_after_qconnect_and_before_service(self):
        body = extract_function(self.install, "install_qobuz")
        self.assertLess(
            body.index("configure_qbzd_qconnect"), body.index("configure_qbzd_audio_output")
        )
        self.assertLess(
            body.index("configure_qbzd_audio_output"), body.index("configure_qbzd_service")
        )

    def test_qbzd_audio_output_is_idempotent_and_leaves_name_alone(self):
        configurator = extract_function(self.install, "configure_qbzd_audio_output")
        for token in (
            "audio.backend",
            "audio.device",
            "audio.skip_sink_switch",
            "settings set --quiet",
            "already targets",
        ):
            self.assertIn(token, configurator)
        self.assertNotIn("device_name", configurator)
        self.assertNotIn("qconnect name", configurator)
        self.assertLess(
            configurator.index("QBZD_AUDIO_CHANGED_BY_FXROUTE=1"),
            configurator.index("settings set --quiet"),
        )

    def test_existing_service_restarts_on_audio_output_change(self):
        service = extract_function(self.install, "configure_qbzd_service")
        self.assertIn("QBZD_AUDIO_CHANGED_BY_FXROUTE -eq 1", service)

    def test_install_state_records_audio_output_ownership(self):
        for field in (
            "audio_backend_before",
            "audio_device_before",
            "audio_skip_sink_switch_before",
            "audio_changed_by_fxroute",
        ):
            self.assertIn(field, self.install)

    def test_qbzd_audio_configure_routes_and_rechecks_with_fake_qbzd(self):
        reader = extract_function(self.install, "read_qbzd_audio_output")
        configurator = extract_function(self.install, "configure_qbzd_audio_output")
        path_reader = extract_function(self.install, "qbzd_binary_path")
        preamble = (
            "run_as_target_user() { \"$@\"; }\n"
            "log() { :; }\n"
            "pass() { printf '[pass] %s\\n' \"$*\"; }\n"
            "die() { printf '[fxroute][error] %s\\n' \"$*\" >&2; exit 1; }\n"
        )
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            provider_dir = home / ".local" / "bin"
            provider_dir.mkdir(parents=True)
            calls_file = Path(td) / "calls"
            fake_qbzd = provider_dir / "qbzd"
            fake_qbzd.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == settings && $2 == show ]]; then\n"
                "  printf '{\"audio.backend\":\"%s\",\"audio.device\":\"%s\",\"audio.skip_sink_switch\":\"%s\"}\\n' \"$(<\"$STATE_DIR/backend\")\" \"$(<\"$STATE_DIR/device\")\" \"$(<\"$STATE_DIR/skip\")\"\n"
                "elif [[ $1 == settings && $2 == set ]]; then\n"
                "  printf 'set:%s=%s\\n' \"$4\" \"$5\" >> \"$CALLS_FILE\"\n"
                "  case \"$4\" in\n"
                "    audio.backend) printf '%s' \"$5\" > \"$STATE_DIR/backend\" ;;\n"
                "    audio.device) printf '%s' \"$5\" > \"$STATE_DIR/device\" ;;\n"
                "    audio.skip_sink_switch) printf '%s' \"$5\" > \"$STATE_DIR/skip\" ;;\n"
                "  esac\n"
                "else\n"
                "  exit 1\n"
                "fi\n"
            )
            fake_qbzd.chmod(0o755)
            harness = (
                f"{preamble}\n{path_reader}\n{reader}\n{configurator}\n"
                "QBZD_AUDIO_BACKEND_BEFORE=\"\"\n"
                "QBZD_AUDIO_DEVICE_BEFORE=\"\"\n"
                "QBZD_AUDIO_SKIP_SINK_SWITCH_BEFORE=\"\"\n"
                "QBZD_AUDIO_CHANGED_BY_FXROUTE=0\n"
                "configure_qbzd_audio_output\n"
                "printf 'backend=%s device=%s skip=%s changed=%s calls=%s\\n' "
                "\"$(<\"$STATE_DIR/backend\")\" \"$(<\"$STATE_DIR/device\")\" \"$(<\"$STATE_DIR/skip\")\" "
                "\"$QBZD_AUDIO_CHANGED_BY_FXROUTE\" \"$(<\"$CALLS_FILE\")\"\n"
            )
            # Misrouted output is routed, verified, and recorded.
            state_dir = Path(td) / "state-off"
            state_dir.mkdir()
            (state_dir / "backend").write_text("system")
            (state_dir / "device").write_text("system")
            (state_dir / "skip").write_text("false")
            calls_file.write_text("")
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
                "CALLS_FILE": str(calls_file),
                "STATE_DIR": str(state_dir),
            }
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            out = result.stdout
            self.assertIn("backend=pipewire device=fxroute_dsp_sink skip=true changed=1", out)
            self.assertIn("set:audio.backend=pipewire", out)
            self.assertIn("set:audio.device=fxroute_dsp_sink", out)
            self.assertIn("set:audio.skip_sink_switch=true", out)
            # Already routed output is left untouched.
            state_dir = Path(td) / "state-on"
            state_dir.mkdir()
            (state_dir / "backend").write_text("pipewire")
            (state_dir / "device").write_text("fxroute_dsp_sink")
            (state_dir / "skip").write_text("true")
            calls_file.write_text("")
            env.update({"STATE_DIR": str(state_dir)})
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already targets", result.stdout)
            self.assertNotIn("set:audio", result.stdout)
            # Unreadable settings abort the run instead of leaving DSP off.
            broken = provider_dir / "qbzd-broken"
            broken.write_text("#!/usr/bin/env bash\nexit 1\n")
            broken.chmod(0o755)
            (provider_dir / "qbzd").unlink()
            broken.rename(provider_dir / "qbzd")
            result = subprocess.run(
                ["bash", "-c", harness], capture_output=True, text=True, env=env
            )
            self.assertNotEqual(result.returncode, 0)

    def test_tidal_tracks_the_current_stable_upstream(self):
        self.assertNotIn("tidalapi", self.base_requirements)
        self.assertIn("tidalapi", self.tidal_requirements)
        self.assertNotRegex(self.tidal_requirements, r"tidalapi\s*==")
        self.assertIn("requirements-tidal.txt", self.install)
        self.assertIn("PKCE", self.install)
        body = extract_function(self.install, "ensure_tidal_dependency")
        self.assertIn("install --upgrade", body)
        self.assertNotIn("0.8.11", body)
        self.assertIn("update check failed", body)

    def test_install_state_records_upstream_provider_versions(self):
        state_body = extract_function(self.install, "write_install_state")
        ownership_body = extract_function(self.install, "load_provider_ownership_state")
        for field in (
            '"installed_version": "${SPOTIFYD_INSTALLED_VERSION}"',
            '"installed_version": "${QBZD_INSTALLED_VERSION}"',
            '"upstream_source": "${QBZD_UPSTREAM_SOURCE}"',
            '"installed_version": "${TIDAL_INSTALLED_VERSION}"',
        ):
            self.assertIn(field, state_body)
        for field in (
            "providers.spotifyd.installed_version",
            "providers.qobuz.installed_version",
            "providers.qobuz.upstream_source",
            "providers.tidal.installed_version",
        ):
            self.assertIn(field, ownership_body)

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
            "mdns_guard_owned_by_fxroute",
            "mdns_guard_script_sha256",
            "mdns_guard_service_sha256",
            "mdns_guard_timer_sha256",
            "mdns_guard_target_uid",
            "apt_repo_sha256",
            "apt_key_fingerprint",
            "power_polkit_installed",
        ):
            self.assertIn(field, ownership_body)
        self.assertIn('"firewalld_rule_format": "${FIREWALLD_RULE_FORMAT}"', state_body)
        self.assertIn('FIREWALLD_RULE_FORMAT="rich-priority"', self.install)

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
        for provider_label, function_name in (("spotifyd", "remove_owned_spotifyd"), ("qbzd", "remove_owned_qbzd")):
            body = extract_function(self.uninstall, function_name)
            self.assertIn('if [[ -e "$service_path" || -L "$service_path" ]]; then', body)
            self.assertIn(f"FXRoute-owned {provider_label} service is already absent", body)

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
        self.assertIn("ROOT_INSTALL_STATE_FILE", self.uninstall)
        self.assertIn("install_root", self.uninstall)
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
