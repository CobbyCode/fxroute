#!/usr/bin/env python3
"""Regression tests for the installer's package-manager support.

Covers the 0.7.1 regression (Arch/Manjaro support was removed) and
checks the unified package-manager preparation:

- pacman detection in confirm_supported_distro()
- pkg_install() branches for apt, dnf, zypper and pacman
- refresh/upgrade preparation at most ONCE per installer run:
  apt-get update, dnf install --refresh, zypper refresh, pacman -Syu
- no automatic system upgrades in the installer
  (apt upgrade, dist-upgrade, dnf upgrade, zypper update)
- current Manjaro package lists (core, audio), native DSP build dependencies
  (gcc pkgconf libpipewire), venv handling, Avahi, LAN-IP fallback
- pacman branch in scripts/system-package-update.sh

The behavior part runs pkg_install() in a subshell with a mocked
run_cmd and counts how often refresh/upgrade commands actually run
across multiple pkg_install calls.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UPDATE_SH = ROOT / "scripts" / "system-package-update.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"


def _extract_function(text: str, name: str) -> str:
    """Extrahiert den Funktionsrumpf `name() { ... }` (bis `}` in Spalte 0)."""
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}", text, re.MULTILINE | re.DOTALL
    )
    if not match:
        raise AssertionError(f"Funktion {name}() nicht in install.sh gefunden")
    return f"{name}() {{\n{match.group(1)}\n}}"


class InstallerPkgManagerStaticTests(unittest.TestCase):
    """Static checks on the install.sh source text."""

    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL_SH.read_text()
        cls.update_text = UPDATE_SH.read_text()
        cls.uninstall_text = UNINSTALL_SH.read_text()
        cls.pkg_body = _extract_function(cls.text, "pkg_install")

    def test_pacman_distro_detection(self):
        self.assertIn('command -v pacman >/dev/null 2>&1; then\n    PACKAGE_MANAGER="pacman"', self.text)
        self.assertIn("Expected apt, dnf, zypper, or pacman.", self.text)

    def test_pkg_install_has_all_four_branches(self):
        for branch in ("apt)", "dnf)", "zypper)", "pacman)"):
            self.assertIn(f"\n    {branch}\n", self.pkg_body)

    def test_apt_prep_once(self):
        # apt-get update may appear only once (in the guard); no upgrades.
        self.assertEqual(self.pkg_body.count("apt-get update"), 1)
        for forbidden in ("apt-get upgrade", "apt-get dist-upgrade", "dist-upgrade"):
            self.assertNotIn(forbidden, self.text)

    def test_dnf_refresh_once(self):
        self.assertEqual(self.pkg_body.count("dnf install -y --refresh"), 1)
        self.assertNotIn("dnf upgrade", self.text)
        self.assertNotIn("dnf -y upgrade", self.text)

    def test_zypper_refresh_once(self):
        self.assertEqual(self.pkg_body.count("zypper --non-interactive refresh"), 1)
        self.assertNotIn("zypper --non-interactive update", self.text)

    def test_pacman_syu_once_and_no_bare_sy(self):
        self.assertEqual(self.pkg_body.count("pacman -Syu --needed --noconfirm"), 1)
        self.assertEqual(self.pkg_body.count("pacman -S --needed --noconfirm"), 1)
        # No separate `pacman -Sy` (without -u) as a standalone command.
        for line in self.pkg_body.splitlines():
            stripped = line.strip()
            if stripped.startswith("pacman -Sy") and not stripped.startswith("pacman -Syu"):
                self.fail(f"separate 'pacman -Sy' found: {stripped}")

    def test_guard_variable_used(self):
        self.assertIn("PKG_REFRESH_DONE=0", self.text)
        # Guard is set in each of the four branches after refresh/upgrade.
        self.assertGreaterEqual(self.pkg_body.count("PKG_REFRESH_DONE=1"), 4)

    def test_manjaro_package_lists(self):
        self.assertIn("core_packages=(python python-pip mpv ffmpeg playerctl)", self.text)
        self.assertIn(
            "audio_stack_packages=(bluez bluez-utils wireplumber pipewire pipewire-pulse libpulse)",
            self.text,
        )

    def test_smb_runtime_packages_for_all_supported_distros(self):
        expected = (
            'apt) echo "smbclient cifs-utils libglib2.0-bin gvfs gvfs-backends gvfs-fuse"',
            'dnf) echo "samba-client cifs-utils glib2 gvfs gvfs-smb gvfs-fuse"',
            'zypper) echo "samba-client cifs-utils glib2-tools gvfs gvfs-backend-samba gvfs-fuse"',
            'pacman) echo "smbclient cifs-utils glib2 gvfs gvfs-smb"',
        )
        for package_list in expected:
            self.assertIn(package_list, self.text)
        self.assertIn('for cmd in smbclient mount.cifs gio; do', self.text)
        self.assertIn('package_installed "$pkg" || missing+=("$pkg")', self.text)

    def test_smb_package_matrix_behavior(self):
        body = _extract_function(self.text, "smb_packages_for_manager")
        expected = {
            "apt": "smbclient cifs-utils libglib2.0-bin gvfs gvfs-backends gvfs-fuse",
            "dnf": "samba-client cifs-utils glib2 gvfs gvfs-smb gvfs-fuse",
            "zypper": "samba-client cifs-utils glib2-tools gvfs gvfs-backend-samba gvfs-fuse",
            "pacman": "smbclient cifs-utils glib2 gvfs gvfs-smb",
        }
        for manager, packages in expected.items():
            result = subprocess.run(
                ["bash", "-c", f'{body}\nsmb_packages_for_manager {manager}'],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), packages)

    def test_smb_missing_package_dry_run_for_all_managers(self):
        matrix = _extract_function(self.text, "smb_packages_for_manager")
        ensure = _extract_function(self.text, "ensure_smb_packages")
        for manager in ("apt", "dnf", "zypper", "pacman"):
            code = f'''\n{matrix}\n{ensure}\nPACKAGE_MANAGER={manager}\npackage_installed() {{ [ "$1" = gvfs ]; }}\npkg_install() {{ printf "%s\\n" "$*"; }}\nensure_smb_packages\n'''
            result = subprocess.run(["bash", "-c", code], capture_output=True, text=True, check=True)
            planned = result.stdout.strip().split()
            self.assertNotIn("gvfs", planned)
            self.assertIn("cifs-utils", planned)
            self.assertTrue("smbclient" in planned or "samba-client" in planned)

    def test_installer_installs_restricted_cifs_helper(self):
        self.assertIn("install_network_library_helper()", self.text)
        self.assertIn("/usr/local/sbin/fxroute-cifs-mount", self.text)
        self.assertIn("/etc/sudoers.d/fxroute-cifs-mount", self.text)
        self.assertIn("install_network_library_helper\n", self.text)
        self.assertIn("remove_network_library_helper()", self.uninstall_text)
        self.assertIn("/etc/sudoers.d/fxroute-cifs-mount", self.uninstall_text)
        self.assertIn('helper_path="/usr/local/sbin/fxroute-cifs-mount"', self.uninstall_text)
        self.assertIn("--remove-all", self.uninstall_text)
        self.assertIn("cifs_helper_installed_by_fxroute", self.text)
        self.assertIn("cifs_sudoers_rule_installed_by_fxroute", self.uninstall_text)
        self.assertIn("helper_verified", self.uninstall_text)

    def test_root_staged_temp_files_are_removed_with_sudo(self):
        # install_network_library_helper and configure_system_auto_update_helper
        # stage a mktemp file as the invoking user, then `sudo install` rewrites
        # it root-owned. A plain `rm -f` then fails in sticky /tmp under `set -e`,
        # aborting the install when run as a normal user with sudo. Both must
        # remove the staging file via the sudo command.
        for name in (
            "install_network_library_helper",
            "configure_system_auto_update_helper",
        ):
            body = _extract_function(self.text, name)
            self.assertEqual(
                len(re.findall(r'^\s*rm -f "\$tmp_helper"$', body, re.MULTILINE)),
                0,
                f"{name} still removes the sudo-staged temp file as the plain user",
            )
            self.assertEqual(
                len(re.findall(r'^\s*"\$\{SUDO_CMD\[@\]\}" rm -f "\$tmp_helper"$', body, re.MULTILINE)),
                3,
                f"{name} does not remove the sudo-staged temp file via sudo",
            )

    def test_system_update_cleanup_requires_recorded_ownership(self):
        self.assertIn("system_update_owned_by_fxroute", self.text)
        self.assertIn("system_update_owned_by_fxroute", self.uninstall_text)
        self.assertIn("system_update_service_sha256", self.uninstall_text)
        body = _extract_function(self.uninstall_text, "remove_optional_system_update_helper")
        self.assertLess(body.index("SYSTEM_UPDATE_HELPER_SHA256"), body.index("stop_system_update_units"))
        self.assertIn("restore_system_update_transaction", body)

    def test_system_update_changes_validate_before_stop_and_support_rollback(self):
        body = _extract_function(self.text, "configure_system_auto_update_helper")
        self.assertIn("restore_system_update_transaction", self.text)
        self.assertLess(body.index('[[ -f "$script_path"'), body.index("stop_system_update_units"))
        self.assertIn("restore_system_update_transaction", body)

    def test_cifs_cleanup_requires_recorded_helper_ownership_and_current_helper(self):
        self.assertIn('[[ $helper_owned -eq 1 && $helper_verified -eq 1 ]]', self.uninstall_text)
        self.assertNotIn("CIFS_HELPER_LEGACY_SHA256", self.uninstall_text)
        safe_path = 'PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"'
        self.assertIn(safe_path, self.text)
        self.assertIn(safe_path, self.uninstall_text)

    def test_cifs_helper_has_safe_automount_options(self):
        helper = (ROOT / "scripts" / "fxroute-cifs-mount").read_text()
        for option in (
            "_netdev",
            "nofail",
            "x-systemd.automount",
            "x-systemd.mount-timeout=10s",
        ):
            self.assertIn(option, helper)
        self.assertNotIn("192.168.", helper)
        self.assertNotIn("Music-Demo", helper)
        self.assertIn('mount_root="/var/lib/fxroute/music-libraries/$uid"', helper)
        self.assertNotIn('mount_root="$home/', helper)
        self.assertIn('if (!replaced) print entry', helper)
        self.assertIn('mv "$tmp_fstab" /etc/fstab', helper)
        self.assertIn("require_cmd systemctl", self.text)
        self.assertIn('PATH="/usr/sbin:/usr/bin:/sbin:/bin"', helper)
        self.assertIn('install -d -o root -g root -m 755 /var/lib/fxroute', helper)

    def test_native_dsp_pacman_deps(self):
        match = re.search(r"pacman\) dsp_packages=\(([^)]*)\)", self.text)
        self.assertIsNotNone(match)
        packages = set(match.group(1).split())
        self.assertTrue({"gcc", "pkgconf", "libpipewire", "lilv", "lv2",
                         "lsp-plugins", "zam-plugins", "calf", "libebur128",
                         "libsamplerate", "speexdsp"}.issubset(packages))

    def test_lv2ls_tool_package_for_each_distro(self):
        required = {
            "apt": {"lilv-utils"},
            "dnf": {"lilv"},
            "zypper": {"lilv"},
            "pacman": {"lilv"},
        }
        for manager, packages_needed in required.items():
            match = re.search(rf"{manager}\) dsp_packages=\(([^)]*)\)", self.text)
            self.assertIsNotNone(match)
            packages = set(match.group(1).split())
            self.assertTrue(
                packages_needed.issubset(packages),
                f"{manager} dsp_packages misses {packages_needed - packages}",
            )

    def test_headless_audio_services_enabled(self):
        self.assertIn("enable_user_audio_services()", self.text)
        self.assertIn("pipewire.socket", self.text)
        self.assertIn("pipewire-pulse.socket", self.text)
        self.assertIn("systemctl --user enable --now", self.text)
        self.assertIn("enable_user_audio_services\n", self.text)

    def test_headless_audio_units_existence_check(self):
        self.assertIn("user_unit_exists()", self.text)
        self.assertIn("/usr/lib/systemd/user/$unit", self.text)
        self.assertIn("wireplumber.service", self.text)

    def test_headless_audio_service_selection_behavior(self):
        exists = _extract_function(self.text, "user_unit_exists")
        user_systemctl = _extract_function(self.text, "user_systemctl")
        body = _extract_function(self.text, "enable_user_audio_services")
        harness = f'''
{exists}
{user_systemctl}
{body}
SYSTEMCTL_CALLS=
PASS=0; FAIL=0
pass() {{ PASS=$((PASS+1)); }}
fail() {{ FAIL=$((FAIL+1)); }}
warn() {{ echo "WARN $*"; }}
systemctl() {{ SYSTEMCTL_CALLS="$SYSTEMCTL_CALLS|systemctl $*"; return 0; }}
'''

        def run(mock_user_unit_exists: str) -> str:
            code = harness + mock_user_unit_exists + "\nenable_user_audio_services\necho \"CALLS:$SYSTEMCTL_CALLS\"\n"
            result = subprocess.run(
                ["bash", "-c", code], capture_output=True, text=True, check=True
            )
            return result.stdout

        sockets_only = '''user_unit_exists() {
  case "$1" in
    pipewire.socket) return 0 ;;
    wireplumber.service) return 0 ;;
    pipewire-pulse.socket) return 0 ;;
    *) return 1 ;;
  esac
}
'''
        stdout = run(sockets_only)
        calls = stdout.split("CALLS:")[-1].strip()
        self.assertIn(
            "systemctl --user enable --now pipewire.socket wireplumber.service pipewire-pulse.socket",
            calls,
        )
        self.assertNotIn("pipewire.service", calls)

        services_only = '''user_unit_exists() {
  case "$1" in
    pipewire.service) return 0 ;;
    wireplumber.service) return 0 ;;
    pipewire-pulse.service) return 0 ;;
    *) return 1 ;;
  esac
}
'''
        stdout = run(services_only)
        calls = stdout.split("CALLS:")[-1].strip()
        self.assertIn(
            "systemctl --user enable --now pipewire.service wireplumber.service pipewire-pulse.service",
            calls,
        )

        pulse_absent = '''user_unit_exists() {
  case "$1" in
    pipewire.socket) return 0 ;;
    wireplumber.service) return 0 ;;
    *) return 1 ;;
  esac
}
'''
        stdout = run(pulse_absent)
        calls = stdout.split("CALLS:")[-1].strip()
        self.assertIn(
            "systemctl --user enable --now pipewire.socket wireplumber.service",
            calls,
        )
        self.assertNotIn("pipewire-pulse", calls)
        self.assertIn("WARN PipeWire user units not found", stdout)

    def test_headless_user_session_persistence(self):
        self.assertIn("enable_user_session_persistence()", self.text)
        self.assertIn("loginctl enable-linger", self.text)
        self.assertIn("loginctl show-user", self.text)
        self.assertIn("enable_user_session_persistence\n", self.text)

    def test_rtkit_is_not_forced_as_an_audio_dependency(self):
        self.assertNotIn("rtkit", self.text.lower())

    def test_spotify_autostart_default_is_x86_64_only(self):
        self.assertIn('[[ "$(uname -m)" != "x86_64" ]]', self.text)
        self.assertIn('spotify_autostart="off"', self.text)
        self.assertIn("SPOTIFY_AUTOSTART=$spotify_autostart", self.text)

    def test_venv_pacman_branch(self):
        self.assertIn("pacman)\n        # python on Arch/Manjaro ships the venv module", self.text)

    def test_avahi_pacman(self):
        self.assertIn('dnf|zypper|pacman) avahi_pkg="avahi"', self.text)
        self.assertIn("pacman -Q avahi >/dev/null 2>&1", self.text)

    def test_lan_ip_fallback(self):
        self.assertIn("ip -4 route get 1.1.1.1", self.text)
        self.assertIn("ip -4 addr show scope global", self.text)

    def test_system_package_update_has_pacman(self):
        self.assertIn("pacman -Syu --noconfirm", self.update_text)


class InstallerPkgManagerBehaviorTests(unittest.TestCase):
    """Behavior test: refresh/upgrade preparation at most once per run.

    Runs pkg_install() repeatedly in a subshell (run_cmd mocked) and
    counts the refresh/upgrade commands actually executed.
    """

    def _count_log_lines(self, log_text: str, needle: str) -> int:
        return sum(1 for line in log_text.splitlines() if needle in line)

    def _run_pkg_install_sequence(self, pm: str, packages: list[str]) -> str:
        pkg_body = _extract_function(INSTALL_SH.read_text(), "pkg_install")
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "cmd.log"
            code = f"""
PKG_REFRESH_DONE=0
log() {{ printf '[fxroute] %s\\n' "$*"; }}
run_cmd() {{ printf '%s\\n' "$*" >> {log_path}; }}
{pkg_body}
PACKAGE_MANAGER={pm}
SUDO_CMD=()
pkg_install {' '.join(packages)}
pkg_install {' '.join(packages)}
pkg_install {' '.join(packages)}
"""
            result = subprocess.run(
                ["bash", "-c", code], capture_output=True, text=True, timeout=60
            )
            self.assertEqual(result.returncode, 0, f"Subshell fehlgeschlagen: {result.stderr}")
            return log_path.read_text()

    def test_apt_update_once(self):
        log = self._run_pkg_install_sequence("apt", ["pkg-a", "pkg-b"])
        self.assertEqual(self._count_log_lines(log, "apt-get update"), 1)
        self.assertEqual(self._count_log_lines(log, "apt-get install -y"), 3)

    def test_dnf_refresh_only_first(self):
        log = self._run_pkg_install_sequence("dnf", ["pkg-a", "pkg-b"])
        self.assertEqual(self._count_log_lines(log, "dnf install -y --refresh"), 1)
        self.assertEqual(self._count_log_lines(log, "dnf install -y "), 3)

    def test_zypper_refresh_once(self):
        log = self._run_pkg_install_sequence("zypper", ["pkg-a", "pkg-b"])
        self.assertEqual(self._count_log_lines(log, "zypper --non-interactive refresh"), 1)
        self.assertEqual(self._count_log_lines(log, "zypper --non-interactive install"), 3)

    def test_pacman_syu_once_then_plain_install(self):
        log = self._run_pkg_install_sequence("pacman", ["pkg-a", "pkg-b"])
        self.assertEqual(self._count_log_lines(log, "pacman -Syu --needed --noconfirm"), 1)
        self.assertEqual(self._count_log_lines(log, "pacman -S --needed --noconfirm"), 2)
        self.assertEqual(self._count_log_lines(log, "pacman -Sy "), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
