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
- current Manjaro package lists (core, audio), stage-1 dependencies
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
        self.assertIn("core_packages=(python python-pip mpv ffmpeg playerctl flatpak)", self.text)
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
        self.assertIn("fxroute-cifs-mount --remove-all", self.uninstall_text)
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

    def test_stage1_pacman_deps(self):
        self.assertIn("stage1_packages=(gcc pkgconf libpipewire)", self.text)

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
