#!/usr/bin/env python3
"""Startup rollback and lifespan ownership regression tests."""

import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
from player import MPVWrapper


class FakePlayer:
    def __init__(self):
        self._running = False
        self.start = MagicMock()
        self.stop = MagicMock()
        self.register_callbacks = MagicMock()
        self.unregister_callbacks = MagicMock()
        self.shutdown_callbacks = AsyncMock()


class FakeScanner:
    def __init__(self):
        self.prepare_scan_status = MagicMock()

    def refresh(self, _force):
        return None

    def cancel_refresh(self):
        return None


class FakeDownloader:
    def __init__(self):
        self.register_callback = MagicMock()
        self.shutdown = MagicMock()


class FakeMeasurementStore:
    def __init__(self):
        self.measurements_dir = pathlib.Path("/tmp/measurements")
        self.shutdown = AsyncMock()


class FakeMeasurementSession:
    def __init__(self):
        self.request_close = AsyncMock()

    async def run_watchdog(self):
        await asyncio.Event().wait()


class LifespanOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_is_raised_and_prior_player_is_stopped(self):
        player = FakePlayer()
        settings = SimpleNamespace(MUSIC_ROOT="/music", download_dir=pathlib.Path("/downloads"))
        with patch.object(main, "get_settings", return_value=settings), patch.object(
            main, "get_player", return_value=player
        ), patch.object(main, "LibraryScanner", FakeScanner), patch.object(
            main, "Downloader", side_effect=RuntimeError("downloader failed")
        ):
            context = main.lifespan(main.app)
            with self.assertRaisesRegex(RuntimeError, "downloader failed"):
                await context.__aenter__()

        player.stop.assert_called_once()
        self.assertIsNone(main.player_instance)
        self.assertIsNone(main.library_scan_task)

    async def test_startup_cancellation_drains_watchdog_and_owned_managers(self):
        player = FakePlayer()
        downloader = FakeDownloader()
        store = FakeMeasurementStore()
        session = FakeMeasurementSession()
        reconcile_entered = asyncio.Event()

        class Coordinator:
            async def reconcile_startup_gate(self):
                reconcile_entered.set()
                await asyncio.Event().wait()

            def status(self):
                return {}

        settings = SimpleNamespace(MUSIC_ROOT="/music", download_dir=pathlib.Path("/downloads"))
        effects = SimpleNamespace(load_global_extras=lambda: {})
        with patch.object(main, "get_settings", return_value=settings), patch.object(
            main, "get_player", return_value=player
        ), patch.object(main, "LibraryScanner", FakeScanner), patch.object(
            main, "Downloader", return_value=downloader
        ), patch.object(main, "EasyEffectsManager", return_value=effects), patch.object(
            main, "MeasurementStore", return_value=store
        ), patch.object(main, "MeasurementSampleRateSession", return_value=session), patch.object(
            main, "PlaybackTransitionCoordinator", return_value=Coordinator()
        ):
            context = main.lifespan(main.app)
            enter_task = asyncio.create_task(context.__aenter__())
            await reconcile_entered.wait()
            enter_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await enter_task

        store.shutdown.assert_awaited_once()
        session.request_close.assert_awaited_once()
        downloader.shutdown.assert_called_once()
        player.stop.assert_called_once()
        self.assertIsNone(main.measurement_watchdog_task)
        self.assertIsNone(main.measurement_store)

    async def test_external_input_partial_link_is_rolled_back(self):
        connect = AsyncMock(side_effect=[None, RuntimeError("FR failed")])
        disconnect = AsyncMock()
        with patch.object(main, "_disable_external_input_loopback", AsyncMock()), patch.object(
            main, "_connect_ports", connect
        ), patch.object(main, "_disconnect_external_input_source", disconnect):
            with self.assertRaisesRegex(RuntimeError, "FR failed"):
                await main._ensure_external_input_loopback("source")
        disconnect.assert_awaited_once_with("source")

    async def test_bluetooth_partial_link_is_rolled_back(self):
        disconnect = AsyncMock()
        with patch.object(main, "_clear_bluetooth_input_monitoring_links", AsyncMock()), patch.object(
            main, "_link_bluetooth_source_to_easyeffects", AsyncMock(side_effect=RuntimeError("FR failed"))
        ), patch.object(main, "_disconnect_bluetooth_input_source", disconnect):
            with self.assertRaisesRegex(RuntimeError, "FR failed"):
                await main._ensure_bluetooth_input_loopback("bluez-source")
        disconnect.assert_awaited_once_with("bluez-source")

    async def test_measurement_release_task_created_during_shutdown_is_drained(self):
        started = asyncio.Event()
        cleanup_order = []

        class Session:
            async def request_close(self):
                cleanup_order.append("measurement-session")
                async def delayed_repair():
                    started.set()
                    await asyncio.Event().wait()

                main._create_lifecycle_background_task(
                    delayed_repair(),
                    name="test-delayed-repair",
                )

        main.measurement_sr_session = Session()
        spl_shutdown = AsyncMock(side_effect=lambda: cleanup_order.append("spl-calibration"))
        with patch.object(main, "set_bluetooth_receiver_enabled"), patch.object(
            main.spl_calibration, "shutdown", spl_shutdown
        ):
            await main._shutdown_lifespan_resources()

        spl_shutdown.assert_awaited_once()
        self.assertLess(
            cleanup_order.index("spl-calibration"),
            cleanup_order.index("measurement-session"),
        )
        self.assertFalse(main.lifecycle_background_tasks)
        self.assertIsNone(main.measurement_sr_session)

    def test_player_callback_unregister_and_killed_process_reap(self):
        player = MPVWrapper()
        callback = lambda _state: None
        player.register_callbacks(callback)
        player.register_callbacks(callback)
        player.unregister_callbacks(callback)
        self.assertEqual(player._callbacks, [])

        process = MagicMock()
        process.poll.return_value = None
        process.wait.side_effect = [__import__("subprocess").TimeoutExpired("mpv", 5), 0]
        player.process = process
        with patch("player.os.unlink"):
            player.stop()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertIsNone(player.process)

    async def test_player_shutdown_drains_queued_callback_tasks(self):
        player = MPVWrapper()
        entered = asyncio.Event()

        async def callback(_state):
            entered.set()
            await asyncio.Event().wait()

        player.register_callbacks(callback)
        player._notify_callbacks()
        await entered.wait()
        await player.shutdown_callbacks(callback)

        self.assertEqual(player._callbacks, [])
        self.assertFalse(player._callback_tasks)

    async def test_unregistered_player_callback_snapshot_is_not_dispatched(self):
        player = MPVWrapper()
        called = False

        async def callback(_state):
            nonlocal called
            called = True

        player.register_callbacks(callback)
        token = player._callbacks[0][2]
        player.unregister_callbacks(callback)
        player._schedule_callback_task(callback, {}, token)
        await asyncio.sleep(0)

        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main(verbosity=2)
