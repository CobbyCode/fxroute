#!/usr/bin/env python3
"""Startup offload must keep the asyncio event loop live.

Positive regression for the P2 finding: the EasyEffects flatpak probe and
the whole MPV player start (mpv --version probe + socket wait) used to run
synchronously inside the lifespan and froze the event loop for the full
blocking duration.  Both are now executed through main._drain_worker, so
the lifespan waits for them while the loop keeps ticking.

The real main.lifespan is driven end-to-end with controlled slow sync
callables (100-200 ms) standing in for the probe/socket-wait work; a ticker
runs in parallel and must never see a gap of the blocking duration.
"""

import asyncio
import contextlib
import pathlib
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main

TICK_INTERVAL = 0.01
MAX_ACCEPTABLE_GAP = 0.1
SLOW_BLOCK_SECONDS = 0.15


class Ticker:
    def __init__(self):
        self.gaps = []
        self._last = None
        self._stop = False

    async def run(self):
        loop = asyncio.get_running_loop()
        self._last = loop.time()
        while True:
            await asyncio.sleep(TICK_INTERVAL)
            now = loop.time()
            self.gaps.append(now - self._last)
            self._last = now
            if self._stop:
                return

    def stop(self):
        self._stop = True

    def max_gap(self):
        return max(self.gaps) if self.gaps else 0.0


class SlowPlayer:
    """start() blocks 2x SLOW_BLOCK_SECONDS: mpv --version probe + socket wait."""

    def __init__(self, events):
        self._running = False
        self.events = events

    def start(self):
        self.events.append("player-start")
        time.sleep(SLOW_BLOCK_SECONDS)
        time.sleep(SLOW_BLOCK_SECONDS)
        self._running = True

    def stop(self):
        self._running = False

    def set_volume(self, volume):
        pass

    def register_callbacks(self, callback):
        pass

    async def shutdown_callbacks(self, callback):
        pass


class SlowEasyEffects:
    """Constructor blocks like the flatpak capability probe inside _detect_runtime."""

    def __init__(self):
        SLOW_EFFECTS_EVENTS.append("effects-construct")
        time.sleep(SLOW_BLOCK_SECONDS)

    def load_global_extras(self):
        return {}


SLOW_EFFECTS_EVENTS = []


class FakeScanner:
    def prepare_scan_status(self):
        pass

    def refresh(self, _force):
        return None

    def cancel_refresh(self):
        return None


class FakeDownloader:
    def register_callback(self, _callback, _loop):
        pass

    def shutdown(self):
        pass


class FakeMeasurementStore:
    def __init__(self):
        self.measurements_dir = pathlib.Path("/tmp/measurements")

    async def shutdown(self):
        pass


class FakeMeasurementSession:
    async def run_watchdog(self):
        await asyncio.Event().wait()

    async def request_close(self):
        pass


class FakeCoordinator:
    def __init__(self, *args, **kwargs):
        pass

    async def reconcile_startup_gate(self):
        return {}

    def status(self):
        return {}


class FakePeakMonitor:
    def __init__(self, **kwargs):
        pass

    async def stop(self):
        pass


class FakeSubwooferRuntime:
    async def _stop_orphan_helpers(self):
        pass

    async def stop(self):
        pass


async def _done_loop():
    return None


def _lifespan_patches(slow_player, slow_effects):
    return [
        mock.patch.object(main, "get_settings", return_value=SimpleNamespace(
            MUSIC_ROOT="/music", download_dir=pathlib.Path("/downloads")
        )),
        mock.patch.object(main, "get_player", return_value=slow_player),
        mock.patch.object(main, "LibraryScanner", FakeScanner),
        mock.patch.object(main, "Downloader", FakeDownloader),
        mock.patch.object(main, "EasyEffectsManager", slow_effects),
        mock.patch.object(main, "MeasurementStore", FakeMeasurementStore),
        mock.patch.object(main, "MeasurementSampleRateSession", FakeMeasurementSession),
        mock.patch.object(main, "PlaybackTransitionCoordinator", FakeCoordinator),
        mock.patch.object(main, "HardwareController", None),
        mock.patch.object(main, "EasyEffectsPeakMonitor", FakePeakMonitor),
        mock.patch.object(main, "Subwoofer21Runtime", FakeSubwooferRuntime),
        mock.patch.object(main, "start_volume_read_monitor", lambda: asyncio.create_task(asyncio.sleep(0.01))),
        mock.patch.object(main, "get_spotify_ui_state", mock.AsyncMock(return_value={})),
        mock.patch.object(main, "sync_peak_monitor_for_spotify_state", mock.AsyncMock()),
        mock.patch.object(main, "apply_persisted_audio_output_selection", return_value=None),
        mock.patch.object(main.samplerate, "load_sample_rate_policy", return_value={"mode": "auto"}),
        mock.patch.object(main, "_sync_subwoofer_runtime", mock.AsyncMock()),
        mock.patch.object(main, "get_audio_output_overview", return_value={}),
        mock.patch.object(main, "get_audio_source_overview", return_value={"mode": "app-playback"}),
        mock.patch.object(main, "_sync_external_input_monitoring", mock.AsyncMock(side_effect=lambda overview: overview)),
        mock.patch.object(main, "_sync_bluetooth_input_monitoring", mock.AsyncMock(side_effect=lambda overview: overview)),
        mock.patch.object(main, "_bluetooth_input_monitor_loop", _done_loop),
        mock.patch.object(main, "_spotify_playerctl_watch_loop", _done_loop),
        mock.patch.object(main, "_spotify_state_poll_loop", _done_loop),
        mock.patch.object(main, "set_bluetooth_receiver_enabled", lambda *a, **k: None),
    ]


class StartupOffloadLivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_waits_for_offloaded_startup_while_loop_ticks(self):
        events = []
        SLOW_EFFECTS_EVENTS.clear()
        slow_player = SlowPlayer(events)
        slow_effects_class = SlowEasyEffects

        real_drain_worker = main._drain_worker
        drain_calls = []

        async def drain_spy(func, *args, **kwargs):
            drain_calls.append(func)
            return await real_drain_worker(func, *args, **kwargs)

        ticker = Ticker()
        ticker_task = asyncio.create_task(ticker.run())
        try:
            with contextlib.ExitStack() as stack:
                for patch in _lifespan_patches(slow_player, slow_effects_class):
                    stack.enter_context(patch)
                stack.enter_context(mock.patch.object(main, "_drain_worker", side_effect=drain_spy))
                start = time.perf_counter()
                async with main.lifespan(main.app):
                    elapsed_enter = time.perf_counter() - start
                    self.assertTrue(slow_player._running)
        finally:
            ticker.stop()
            await ticker_task

        self.assertGreaterEqual(elapsed_enter, 3 * SLOW_BLOCK_SECONDS - 0.05)
        self.assertEqual(events, ["player-start"])
        self.assertEqual(SLOW_EFFECTS_EVENTS, ["effects-construct"])
        self.assertTrue(any(call == slow_player.start for call in drain_calls))
        self.assertTrue(any(call is slow_effects_class for call in drain_calls))
        self.assertLess(ticker.max_gap(), MAX_ACCEPTABLE_GAP)
        self.assertIsNone(main.player_instance)
        self.assertIsNone(main.easyeffects_manager)

    async def test_slow_socket_wait_inside_start_does_not_block_loop(self):
        class SocketWaitPlayer:
            _running = False

            def start(self):
                time.sleep(SLOW_BLOCK_SECONDS)
                self._running = True

            def stop(self):
                self._running = False

        player_instance = SocketWaitPlayer()
        ticker = Ticker()
        ticker_task = asyncio.create_task(ticker.run())
        try:
            await main._drain_worker(player_instance.start)
        finally:
            ticker.stop()
            await ticker_task
        self.assertTrue(player_instance._running)
        self.assertLess(ticker.max_gap(), MAX_ACCEPTABLE_GAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
