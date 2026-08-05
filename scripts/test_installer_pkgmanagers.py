#!/usr/bin/env python3
"""Regressionstests für die Paketmanager-Unterstützung des Installers.

Deckt die 0.7.1-Regression ab (Arch/Manjaro-Support wurde entfernt) und
prüft die vereinheitlichte Paketmanager-Vorbereitung:

- pacman-Erkennung in confirm_supported_distro()
- pkg_install()-Zweige für apt, dnf, zypper und pacman
- Refresh-/Upgrade-Vorbereitung pro Installerlauf höchstens EINMAL:
  apt-get update, dnf install --refresh, zypper refresh, pacman -Syu
- keine automatischen System-Upgrades im Installer
  (apt upgrade, dist-upgrade, dnf upgrade, zypper update)
- aktuelle Manjaro-Paketlisten (core, audio), stage-1-Abhängigkeiten
  (gcc pkgconf libpipewire), venv-Behandlung, Avahi, LAN-IP-Fallback
- pacman-Zweig in scripts/system-package-update.sh

Der Verhaltensteil führt pkg_install() in einer Subshell mit gemocktem
run_cmd aus und zählt, wie oft Refresh-/Upgrade-Befehle bei mehreren
pkg_install-Aufrufen tatsächlich ausgeführt werden.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UPDATE_SH = ROOT / "scripts" / "system-package-update.sh"


def _extract_function(text: str, name: str) -> str:
    """Extrahiert den Funktionsrumpf `name() { ... }` (bis `}` in Spalte 0)."""
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}", text, re.MULTILINE | re.DOTALL
    )
    if not match:
        raise AssertionError(f"Funktion {name}() nicht in install.sh gefunden")
    return f"{name}() {{\n{match.group(1)}\n}}"


class InstallerPkgManagerStaticTests(unittest.TestCase):
    """Statische Prüfungen auf dem install.sh-Quelltext."""

    @classmethod
    def setUpClass(cls):
        cls.text = INSTALL_SH.read_text()
        cls.update_text = UPDATE_SH.read_text()
        cls.pkg_body = _extract_function(cls.text, "pkg_install")

    def test_pacman_distro_detection(self):
        self.assertIn('command -v pacman >/dev/null 2>&1; then\n    PACKAGE_MANAGER="pacman"', self.text)
        self.assertIn("Expected apt, dnf, zypper, or pacman.", self.text)

    def test_pkg_install_has_all_four_branches(self):
        for branch in ("apt)", "dnf)", "zypper)", "pacman)"):
            self.assertIn(f"\n    {branch}\n", self.pkg_body)

    def test_apt_prep_once(self):
        # apt-get update darf nur einmal (im Guard) vorkommen; keine Upgrades.
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
        # Kein separates `pacman -Sy` (ohne -u) als eigenständiger Befehl.
        for line in self.pkg_body.splitlines():
            stripped = line.strip()
            if stripped.startswith("pacman -Sy") and not stripped.startswith("pacman -Syu"):
                self.fail(f"separate 'pacman -Sy' gefunden: {stripped}")

    def test_guard_variable_used(self):
        self.assertIn("PKG_REFRESH_DONE=0", self.text)
        # Guard wird in jedem der vier Zweige nach dem Refresh/Upgrade gesetzt.
        self.assertGreaterEqual(self.pkg_body.count("PKG_REFRESH_DONE=1"), 4)

    def test_manjaro_package_lists(self):
        self.assertIn("core_packages=(python python-pip mpv ffmpeg playerctl flatpak)", self.text)
        self.assertIn(
            "audio_stack_packages=(bluez bluez-utils wireplumber pipewire pipewire-pulse libpulse)",
            self.text,
        )

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
    """Verhaltenstest: Refresh/Upgrade-Vorbereitung max. einmal pro Lauf.

    Führt pkg_install() mehrfach in einer Subshell aus (run_cmd gemockt)
    und zählt die tatsächlich ausgeführten Refresh-/Upgrade-Befehle.
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
