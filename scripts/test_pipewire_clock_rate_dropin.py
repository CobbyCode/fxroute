#!/usr/bin/env python3
"""Behavior tests for the canonical FXRoute PipeWire clock-rate drop-in.

Runs scripts/configure-pipewire-samplerates.sh inside a sandboxed
XDG_CONFIG_HOME with systemctl/pw-metadata/sleep shims so the file
generation and idempotency contract is testable without touching a live
audio session.

Covers:

- one canonical context.properties block in the FXRoute-owned drop-in
- canonical allowed-rates list up to and including 384 kHz (no 705600/768000)
- idempotency: repeated apply runs keep the file byte-identical and
  restart the audio services only once
- a stale 192-kHz-capped canonical file is overwritten, not appended to
- the superseded 20-audio-mini-pc-samplerates.conf drop-in and its backups
  are removed
"""

from __future__ import annotations

import pathlib
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCRIPT = ROOT / "scripts" / "configure-pipewire-samplerates.sh"
CANONICAL = "90-fxroute-clock-rate.conf"
LEGACY = "20-audio-mini-pc-samplerates.conf"

EXPECTED_CONTENT = textwrap.dedent(
    """\
    # Managed by FXRoute. Changes take effect after restarting PipeWire/session or rebooting.
    context.properties = {
        default.clock.rate = 44100
        default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 ]
    }
    """
)

STALE_192KHZ_CONTENT = textwrap.dedent(
    """\
    # Managed by FXRoute. Changes take effect after restarting PipeWire/session or rebooting.
    context.properties = {
        default.clock.rate = 44100
        default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]
    }
    """
)

LEGACY_CONTENT = textwrap.dedent(
    """\
    context.properties = {
        default.clock.rate = 48000
        default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 352800 384000 705600 768000 ]
    }
    """
)


class _Sandbox:
    """Sandboxed HOME/XDG_CONFIG_HOME with shims for the services the script touches."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        self.config_home = self.home / ".config"
        self.conf_dir = self.config_home / "pipewire" / "pipewire.conf.d"
        self.conf_dir.mkdir(parents=True)
        self.shims = self.home / "bin"
        self.shims.mkdir(parents=True)
        self.restart_log = self.home / "restarts.log"
        self._make_shim(
            "systemctl",
            f'echo "$*" >> "{self.restart_log}"; exit 0',
        )
        self._make_shim("pw-metadata", "exit 0")
        self._make_shim("sleep", "exit 0")
        self.env = dict(os_environ())
        self.env["HOME"] = str(self.home)
        self.env["XDG_CONFIG_HOME"] = str(self.config_home)
        self.env["PATH"] = f"{self.shims}:{self.env.get('PATH', '')}"

    def _make_shim(self, name: str, body: str) -> None:
        path = self.shims / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def canonical_path(self) -> pathlib.Path:
        return self.conf_dir / CANONICAL

    def legacy_path(self) -> pathlib.Path:
        return self.conf_dir / LEGACY

    def restart_count(self) -> int:
        if not self.restart_log.exists():
            return 0
        return sum(1 for line in self.restart_log.read_text().splitlines() if "restart" in line)

    def cleanup(self) -> None:
        self._tmp.cleanup()


def os_environ() -> dict:
    import os
    return dict(os.environ)


class PipeWireClockRateDropinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sandbox = _Sandbox()

    def tearDown(self) -> None:
        self.sandbox.cleanup()

    def test_apply_writes_single_canonical_block(self) -> None:
        result = self.sandbox.run("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.sandbox.canonical_path().read_text()
        self.assertEqual(content, EXPECTED_CONTENT)
        self.assertEqual(content.count("context.properties"), 1)
        self.assertNotIn("705600", content)
        self.assertNotIn("768000", content)
        # The old exclusively-192-kHz list must be gone.
        self.assertIn("384000", content)

    def test_apply_is_idempotent_across_repeated_runs(self) -> None:
        first = self.sandbox.run("apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.sandbox.canonical_path().read_text()

        second = self.sandbox.run("apply")
        third = self.sandbox.run("apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertEqual(self.sandbox.canonical_path().read_text(), before)
        self.assertEqual(self.sandbox.canonical_path().read_text(), EXPECTED_CONTENT)
        # Only the first run rewrote the file and restarted the audio services.
        self.assertEqual(self.sandbox.restart_count(), 1)
        # No duplicated blocks from repeated runs.
        self.assertEqual(self.sandbox.canonical_path().read_text().count("context.properties"), 1)

    def test_stale_canonical_file_is_overwritten_not_appended(self) -> None:
        self.sandbox.canonical_path().write_text(STALE_192KHZ_CONTENT)
        result = self.sandbox.run("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        content = self.sandbox.canonical_path().read_text()
        self.assertEqual(content, EXPECTED_CONTENT)
        self.assertEqual(content.count("context.properties"), 1)
        self.assertIn("384000", content)

    def test_legacy_dropin_and_backups_are_removed(self) -> None:
        self.sandbox.legacy_path().write_text(LEGACY_CONTENT)
        backup = self.sandbox.conf_dir / f"{LEGACY}.bak.20260420_190030"
        backup.write_text(LEGACY_CONTENT)

        result = self.sandbox.run("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.sandbox.legacy_path().exists())
        self.assertFalse(backup.exists())
        self.assertTrue(self.sandbox.canonical_path().exists())

    def test_remove_command_removes_canonical_and_legacy(self) -> None:
        self.sandbox.run("apply")
        self.sandbox.legacy_path().write_text(LEGACY_CONTENT)
        result = self.sandbox.run("remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.sandbox.canonical_path().exists())
        self.assertFalse(self.sandbox.legacy_path().exists())


if __name__ == "__main__":
    unittest.main()
