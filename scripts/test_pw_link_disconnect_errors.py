#!/usr/bin/env python3
"""disconnect_ports: best-effort on operational failures, loud on bugs.

Contracts (no production code changed here):

- a transient pw-link failure (RuntimeError/OSError) on one source port
  does not abort the disconnect: remaining sources are still tried and the
  call returns normally (best-effort teardown preserved);
- a programming error (TypeError) propagates instead of vanishing as a
  silent success that would leave stale DSP links behind;
- the Bluetooth teardown path keeps the same contract: operational link
  failures stay best-effort, programming errors propagate.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.pw_link as pw_link
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor


async def _noop_peak_sync(_state) -> None:
    return None


class DisconnectPortsTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failure_tries_remaining_sources(self):
        calls: list[tuple] = []

        async def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("pw-link link failed")
            return ""

        with patch.object(pw_link, "run_pw_link_command", new=flaky):
            result = await pw_link.disconnect_ports(("src:1", "src:2"), "sink:1")
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)

    async def test_transient_failure_is_logged_for_diagnosis(self):
        async def flaky(*args):
            raise OSError("pw-link wedged")

        with patch.object(pw_link, "run_pw_link_command", new=flaky), \
                self.assertLogs("audio.pw_link", level="DEBUG") as captured:
            await pw_link.disconnect_ports(("src:1",), "sink:1")
        self.assertTrue(
            any("disconnect" in line for line in captured.output),
            captured.output,
        )

    async def test_programming_error_propagates(self):
        async def broken(*args):
            raise TypeError("bad port type")

        with patch.object(pw_link, "run_pw_link_command", new=broken):
            with self.assertRaises(TypeError):
                await pw_link.disconnect_ports(("src:1",), "sink:1")


class BluetoothDisconnectTests(unittest.IsolatedAsyncioTestCase):
    def _monitor(self) -> BluetoothInputMonitor:
        return BluetoothInputMonitor(
            BluetoothInputDependencies(
                sync_peak_monitor_for_source_mode_state=_noop_peak_sync
            )
        )

    async def test_operational_link_failure_stays_best_effort(self):
        monitor = self._monitor()
        with patch.object(
            monitor,
            "_link_source_to_dsp",
            new=AsyncMock(side_effect=RuntimeError("pw-link down")),
        ):
            await monitor._disconnect_source("bluez:dev")

    async def test_programming_error_propagates(self):
        monitor = self._monitor()
        with patch.object(
            monitor,
            "_link_source_to_dsp",
            new=AsyncMock(side_effect=TypeError("bad port")),
        ):
            with self.assertRaises(TypeError):
                await monitor._disconnect_source("bluez:dev")


if __name__ == "__main__":
    unittest.main(verbosity=2)
