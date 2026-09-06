#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""BlueZ agent runtime: system dbus/gi packages plus visible spawn failures.

Regression test for .126: audio/bluez_agent.py runs on the system python3
and needs the D-Bus bindings plus PyGObject/GLib.  Without them the agent
exits immediately, no BlueZ agent is registered and incoming A2DP/HFP
connections are rejected ("Authentication attempt without agent") even
though pairing works and the receiver looks ready.
"""

from __future__ import annotations

import asyncio
import logging
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"


def extract_bash_function(text: str, name: str) -> str:
    """Extract a bash function, skipping heredoc bodies with lone braces."""
    import re

    lines = text.splitlines(keepends=True)
    opener = f"{name}() {{"
    start = next(i for i, line in enumerate(lines) if line.rstrip("\n") == opener)
    heredoc_end: str | None = None
    depth = 0
    for index in range(start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if heredoc_end is not None:
            if stripped == heredoc_end:
                heredoc_end = None
            continue
        match = re.search(r"<<-?\s*'([^']+)'", line)
        if match:
            heredoc_end = match.group(1)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(lines[start : index + 1])
    raise AssertionError(f"missing {name}()")


def run_bash(prelude: str, body: str, extra_path: str | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
    if extra_path:
        env["PATH"] = f"{extra_path}:/usr/bin:/bin"
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", f"{prelude}\n{body}\n"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def write_fake_python3(directory: Path, succeed: bool) -> None:
    fake = directory / "python3"
    fake.write_text("#!/bin/bash\nexit 0\n" if succeed else "#!/bin/bash\nexit 1\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


INSTALL_PRELUDE = """
run_as_target_user() { "$@"; }
pkg_install() { printf '%s\\n' "$*" >> "$CALL_LOG"; }
log() { :; }
pass() { :; }
warn() { printf 'WARN %s\\n' "$*" >> "$CALL_LOG"; }
"""


class InstallerAgentPackagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.ensure = extract_bash_function(cls.install, "ensure_bluetooth_agent_packages")
        cls.deps_present = extract_bash_function(cls.install, "bluetooth_agent_deps_present")
        cls.zypper_helper = extract_bash_function(cls.install, "zypper_python_package")

    def test_per_manager_agent_package_mapping(self) -> None:
        self.assertIn("agent_packages=(python3-dbus python3-gi gir1.2-glib-2.0)", self.install)
        self.assertIn("agent_packages=(python3-dbus python3-gobject)", self.install)
        self.assertIn('"$(zypper_python_package dbus)" "$(zypper_python_package gobject)"', self.install)
        self.assertIn("agent_packages=(python-dbus python-gobject)", self.install)

    def test_zypper_agent_package_names_follow_the_openSUSE_release(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            leap = Path(td) / "leap"
            leap.write_text('ID="opensuse-leap"\nVERSION_ID="16.0"\n')
            tumbleweed = Path(td) / "tw"
            tumbleweed.write_text('ID="opensuse-tumbleweed"\nVERSION_ID="20260801"\n')
            for release, kind, expected in (
                (leap, "dbus", "python313-dbus-python"),
                (leap, "gobject", "python313-gobject"),
                (tumbleweed, "dbus", "python3-dbus-python"),
                (tumbleweed, "gobject", "python3-gobject"),
            ):
                proc = run_bash(
                    self.zypper_helper,
                    f'printf "%s\\n" "$(zypper_python_package {kind} {release})"',
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, f"{expected}\n")

    def test_ensure_installs_agent_packages_when_imports_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            call_log = tmp / "calls"
            call_log.write_text("")
            fake_bin = tmp / "fakebin"
            fake_bin.mkdir()
            write_fake_python3(fake_bin, succeed=False)
            proc = run_bash(
                f'CALL_LOG={call_log}\nexport CALL_LOG\n{INSTALL_PRELUDE}\n'
                f"{self.deps_present}\n{self.ensure}",
                "PACKAGE_MANAGER=apt\nensure_bluetooth_agent_packages",
                extra_path=str(fake_bin),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("python3-dbus python3-gi gir1.2-glib-2.0", call_log.read_text())

    def test_ensure_skips_install_when_imports_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            call_log = tmp / "calls"
            call_log.write_text("")
            fake_bin = tmp / "fakebin"
            fake_bin.mkdir()
            write_fake_python3(fake_bin, succeed=True)
            proc = run_bash(
                f'CALL_LOG={call_log}\nexport CALL_LOG\n{INSTALL_PRELUDE}\n'
                f"{self.deps_present}\n{self.ensure}",
                "PACKAGE_MANAGER=apt\nensure_bluetooth_agent_packages",
                extra_path=str(fake_bin),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(call_log.read_text(), "")


class FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeDeadProcess:
    returncode = 1

    def __init__(self) -> None:
        self.stderr = FakeStderr(b"Traceback (most recent call last):\nModuleNotFoundError: No module named 'dbus'\n")


async def _noop_peak_sync(_state) -> None:
    return None


class AgentSpawnWarningTests(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_agent_exit_logs_warning_and_raises(self) -> None:
        import audio.bluetooth as bluetooth_module

        monitor = BluetoothInputMonitor(
            BluetoothInputDependencies(sync_peak_monitor_for_source_mode_state=_noop_peak_sync)
        )

        async def _fake_create(*_args, **_kwargs):
            return FakeDeadProcess()

        with mock.patch.object(
            bluetooth_module.asyncio, "create_subprocess_exec", new=_fake_create
        ), self.assertLogs("audio.bluetooth", level="WARNING") as captured:
            with self.assertRaises(RuntimeError, msg="agent immediate exit must raise"):
                await monitor._ensure_agent()
        self.assertTrue(
            any("BlueZ audio agent exited immediately" in line for line in captured.output),
            captured.output,
        )
        self.assertIsNone(monitor.agent_process)


if __name__ == "__main__":
    unittest.main()
