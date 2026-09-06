#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""WirePlumber Bluetooth drop-in: headless appliances must not depend on a
logind seat for A2DP endpoints.

Regression test for .126: WirePlumber's monitors/bluez.lua defers the BlueZ
monitor until a logind seat becomes active.  A headless FXRoute image (linger,
no login session) never activates a seat, so no A2DP media endpoints are
registered, the controller never advertises Audio Sink/Source and the
Bluetooth input stays unselectable.  The installer must therefore ship a
user-level WirePlumber drop-in that disables the seat gate, and the
uninstaller must remove it again.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"
UNINSTALL_SH = ROOT / "uninstall.sh"

DROPIN_RELPATH = Path(".config/wireplumber/wireplumber.conf.d/50-fxroute-bluetooth.conf")

EXPECTED_SNIPPETS = (
    "monitor.bluez.seat-monitoring = disabled",
    "wireplumber.profiles",
    "main = {",
)


def extract_function(text: str, name: str) -> str:
    # Balanced-brace extraction that skips heredoc bodies: the drop-in
    # heredoc contains lone "{" / "}" lines that would fool a naive regex.
    lines = text.splitlines(keepends=True)
    opener = f"{name}() {{"
    start = next(
        index for index, line in enumerate(lines) if line.rstrip("\n") == opener
    )
    heredoc_end: str | None = None
    depth = 0
    for index in range(start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if heredoc_end is not None:
            if stripped == heredoc_end:
                heredoc_end = None
            continue
        heredoc_match = re.search(r"<<-?\s*'([^']+)'", line)
        if heredoc_match:
            heredoc_end = heredoc_match.group(1)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            return "".join(lines[start : index + 1])
    raise AssertionError(f"missing {name}()")


def run_harness(prelude: str, body: str, home: Path) -> subprocess.CompletedProcess:
    script = f"{prelude}\n{body}\n"
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
    )


class WireplumberBluetoothDropinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = INSTALL_SH.read_text()
        cls.uninstall = UNINSTALL_SH.read_text()

    def test_installer_wires_the_bluetooth_step(self) -> None:
        self.assertIn("configure_wireplumber_bluetooth()", self.install)
        self.assertIn("50-fxroute-bluetooth.conf", self.install)
        self.assertIn("$HOME/.config/wireplumber/wireplumber.conf.d", self.install)

    def test_uninstaller_removes_the_bluetooth_step(self) -> None:
        self.assertIn("remove_wireplumber_bluetooth_config()", self.uninstall)
        self.assertIn("50-fxroute-bluetooth.conf", self.uninstall)

    def test_configure_writes_dropin_and_restarts_wireplumber(self) -> None:
        configure = extract_function(self.install, "configure_wireplumber_bluetooth")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            calls = home / "systemctl-calls"
            proc = run_harness(
                "run_as_target_user() { \"$@\"; }\n"
                f"user_systemctl() {{ printf '%s\\n' \"$*\" >> {calls}; }}\n"
                "pass() { :; }\n"
                "warn() { :; }\n",
                f"{configure}\nconfigure_wireplumber_bluetooth",
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            dropin = home / DROPIN_RELPATH
            self.assertTrue(dropin.is_file(), f"drop-in not written: {dropin}")
            content = dropin.read_text()
            for snippet in EXPECTED_SNIPPETS:
                self.assertIn(snippet, content)
            self.assertIn("restart wireplumber.service", calls.read_text())

    def test_remove_deletes_dropin_and_restarts_wireplumber(self) -> None:
        remove = extract_function(self.uninstall, "remove_wireplumber_bluetooth_config")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            calls = home / "systemctl-calls"
            dropin = home / DROPIN_RELPATH
            dropin.parent.mkdir(parents=True)
            dropin.write_text("stale")
            proc = run_harness(
                "run_as_target_user() { \"$@\"; }\n"
                f"user_systemctl() {{ printf '%s\\n' \"$*\" >> {calls}; }}\n"
                "remove_file_if_exists() { rm -f \"$1\"; }\n"
                "warn() { :; }\n"
                "log() { :; }\n",
                f"{remove}\nremove_wireplumber_bluetooth_config",
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(dropin.exists(), "drop-in not removed")
            self.assertIn("restart wireplumber.service", calls.read_text())


if __name__ == "__main__":
    unittest.main()
