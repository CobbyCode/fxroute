#!/usr/bin/env python3
"""Bluetooth monitor idle-skip: no expensive overview build while idle.

Observable contracts of audio/bluetooth.py run_monitor_loop (regression fix
b04d35b: the 3 s monitor unconditionally built the full bluetoothctl/pactl/
pw-cli source overview even with app-playback selected, triggering recurring
Amlogic audio-fabric kernel bursts on .126):

- a tick with persisted mode != bluetooth-input and no active agent/linked
  source must not build the source overview at all (preventing the costly
  subprocess pipeline is itself the contract);
- a tick with bluetooth-input selected still runs the existing sync path.

Only the loop dispatch is pinned here (overview builder call count plus sync
entry); sync internals stay out of scope.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.bluetooth as bluetooth_module
from audio.bluetooth import BluetoothInputDependencies, BluetoothInputMonitor
from audio.samplerate import SOURCE_MODE_APP_PLAYBACK, SOURCE_MODE_BLUETOOTH_INPUT


def _make_monitor(*, mode: str) -> BluetoothInputMonitor:
    return BluetoothInputMonitor(
        BluetoothInputDependencies(
            sync_peak_monitor_for_source_mode_state=AsyncMock(),
            get_persisted_source_mode=lambda: mode,
        )
    )


class BluetoothMonitorIdleSkipTests(unittest.IsolatedAsyncioTestCase):
    async def _run_loop_briefly(self, monitor: BluetoothInputMonitor) -> None:
        task = asyncio.create_task(monitor.run_monitor_loop())
        try:
            await asyncio.sleep(0.2)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_idle_tick_builds_no_source_overview(self):
        monitor = _make_monitor(mode=SOURCE_MODE_APP_PLAYBACK)
        with patch.object(
            bluetooth_module, "get_audio_source_overview", new=Mock()
        ) as overview:
            await self._run_loop_briefly(monitor)
        self.assertEqual(
            overview.call_count,
            0,
            "an idle bluetooth tick must not start the subprocess overview pipeline",
        )

    async def test_active_bluetooth_mode_still_syncs(self):
        monitor = _make_monitor(mode=SOURCE_MODE_BLUETOOTH_INPUT)
        live_overview = {"mode": SOURCE_MODE_BLUETOOTH_INPUT, "bluetooth": {}}
        with (
            patch.object(
                bluetooth_module,
                "get_audio_source_overview",
                new=Mock(return_value=live_overview),
            ) as overview,
            patch.object(
                monitor, "sync", new=AsyncMock(return_value=live_overview)
            ) as sync,
        ):
            await self._run_loop_briefly(monitor)
        self.assertGreater(
            overview.call_count,
            0,
            "the selected bluetooth mode must keep building the live overview",
        )
        sync.assert_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
