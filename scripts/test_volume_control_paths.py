#!/usr/bin/env python3
"""Silent-active recovery uses the live volume; /api/volume never blocks the loop."""

import asyncio
import pathlib
from contextlib import ExitStack
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import system_volume


class _FakePlayer:
    _running = True

    def __init__(self, state=None):
        self.state = state or {
            "current_file": "/music/current.flac",
            "playing": True,
            "paused": False,
            "ended": False,
            "volume": 100,
        }


class SilentActiveLiveVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_silent_attempts = dict(main.silent_active_recovery_attempts)
        main.silent_active_recovery_attempts.clear()
        self.original_cache = system_volume._status_volume_cache

    async def asyncTearDown(self):
        main.silent_active_recovery_attempts.clear()
        main.silent_active_recovery_attempts.update(self.original_silent_attempts)
        system_volume._status_volume_cache = self.original_cache

    def _patches(self):
        return [
            mock.patch.object(main, "peak_monitor", object()),
            mock.patch.object(main, "player_instance", _FakePlayer()),
            mock.patch.object(main, "_current_track_matches", return_value=True),
            mock.patch.object(main, "_is_local_playback_active", return_value=True),
            mock.patch.object(main, "_list_mpv_sink_inputs", return_value=[{"muted": False}]),
            mock.patch.object(main, "_active_unmuted_sink_inputs", return_value=True),
        ]

    async def test_status_cache_says_100_but_live_volume_is_zero_blocks_recovery(self):
        system_volume._status_volume_cache = (100, 1.0)
        with mock.patch.object(main, "get_output_volume", return_value=0) as live, mock.patch.object(
            main, "get_audio_output_overview", return_value={}
        ) as overview, mock.patch.object(main, "_run_debug_command", return_value={"stdout": "", "stderr": ""}) as debug, mock.patch.object(
            main, "_silent_active_source_links_present", return_value=False
        ), ExitStack() as stack:
            for patch in self._patches():
                stack.enter_context(patch)
            await main._check_and_recover_silent_active(
                source="local", signature="sig-live-zero", track={"id": "x"}
            )
        live.assert_called()
        overview.assert_not_called()
        debug.assert_not_called()

    async def test_status_cache_says_zero_but_live_volume_is_positive_continues(self):
        system_volume._status_volume_cache = (0, 1.0)
        with mock.patch.object(main, "get_output_volume", return_value=50) as live, mock.patch.object(
            main, "get_audio_output_overview", return_value={"output_mode": {}}
        ) as overview, mock.patch.object(main, "_run_debug_command", return_value={"stdout": "", "stderr": ""}) as debug, mock.patch.object(
            main, "_silent_active_source_links_present", return_value=False
        ), ExitStack() as stack:
            for patch in self._patches():
                stack.enter_context(patch)
            await main._check_and_recover_silent_active(
                source="local", signature="sig-live-positive", track={"id": "x"}
            )
        live.assert_called()
        overview.assert_called()
        debug.assert_called()

    async def test_live_read_failure_uses_safe_fallback_like_before(self):
        with mock.patch.object(
            main, "get_output_volume", side_effect=RuntimeError("wpctl wedged")
        ), mock.patch.object(
            main, "get_audio_output_overview", return_value={}
        ) as overview, ExitStack() as stack:
            for patch in self._patches():
                stack.enter_context(patch)
            await main._check_and_recover_silent_active(
                source="local", signature="sig-live-failure", track={"id": "x"}
            )
            # The fallback value 100 keeps the diagnosis path alive (same
            # safe default semantics as the previous get_output_volume_safe).
            overview.assert_called()


class VolumeEndpointEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_wpctl_does_not_stop_event_loop(self):
        entered = threading.Event()
        release = threading.Event()
        ticks = []

        def blocking_run(args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.005)

        class FakeRequest:
            async def json(self):
                return {"volume": 50}

        with mock.patch.object(main, "player_instance", _FakePlayer()), mock.patch.object(
            main, "ensure_local_source_volume"
        ), mock.patch.object(main, "easyeffects_manager", None), mock.patch.object(
            main, "build_playback_payload", return_value={"volume": 50}
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch(
            "system_volume.subprocess.run", side_effect=blocking_run
        ):
            volume_task = asyncio.create_task(main.set_volume(FakeRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)
            self.assertGreater(len(ticks), 0)
            tick.cancel()

            release.set()
            result = await volume_task

        self.assertEqual(result["volume"], 50)



class CanonicalVolumeSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_cache = system_volume._status_volume_cache
        system_volume._status_volume_cache = None
        main.canonical_volume_write_lock = None

    async def asyncTearDown(self):
        main.canonical_volume_write_lock = None
        main.easyeffects_manager = None
        system_volume._status_volume_cache = self.original_cache

    async def test_concurrent_canonical_volume_writes_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        def fake_set_output_volume(value):
            order.append(f"set-{value}")
            if value == 50:
                entered.set()
                release.wait(timeout=5)
            return value

        with mock.patch.object(main, "easyeffects_manager", None), mock.patch.object(
            main, "set_output_volume", side_effect=fake_set_output_volume
        ):
            first = asyncio.create_task(main._set_canonical_output_volume(50))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            second = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            # The second write must not start while the first is in flight.
            self.assertEqual(order, ["set-50"])

            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(order, ["set-50", "set-60"])

    async def test_set_readback_sequence_never_interleaves(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def fake_run(args, **kwargs):
            command = args[1]
            if command == "set-volume":
                calls.append("set")
                if "50%" in args[3]:
                    entered.set()
                    release.wait(timeout=5)
            else:
                calls.append("get")
            return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")

        with mock.patch.object(main, "easyeffects_manager", None), mock.patch(
            "system_volume.subprocess.run", side_effect=fake_run
        ):
            first = asyncio.create_task(main._set_canonical_output_volume(50))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            second = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            self.assertEqual(calls, ["set"])

            release.set()
            await asyncio.gather(first, second)

        # Each canonical write owns its set -> verified get sequence.
        self.assertEqual(calls, ["set", "get", "set", "get"])

    def test_stale_monitor_publish_cannot_overshadow_newer_set_publish(self):
        system_volume._publish_status_volume(55, 1.2)
        system_volume._publish_status_volume(37, 1.0)
        self.assertEqual(system_volume.get_status_volume(), 55)

    def test_concurrent_threaded_publishes_are_atomic(self):
        system_volume._status_volume_cache = None
        errors = []

        def publisher(percent, started_at):
            try:
                for _ in range(500):
                    system_volume._publish_status_volume(percent, started_at)
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=publisher, args=(37, 1.0)),
            threading.Thread(target=publisher, args=(55, 1.2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(errors)
        self.assertEqual(system_volume.get_status_volume(), 55)


    async def test_loudness_enable_serializes_against_parallel_write(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_run(args, **kwargs):
            if args[1] == "get-volume":
                calls.append("get")
                entered.set()
                release.wait(timeout=5)
                return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")
            calls.append("set")
            return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def loudness_db_from_percent(self, percent):
                return -float(percent)

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": []}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return ""

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": True}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch(
            "system_volume.subprocess.run", side_effect=blocking_run
        ):
            extras_task = asyncio.create_task(main.save_easyeffects_extras(FakeExtrasRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            write_task = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            # The parallel canonical write must not interleave while the
            # Loudness enable owns the canonical lock.
            self.assertEqual(calls, ["get"])

            release.set()
            await asyncio.gather(extras_task, write_task)

        # enable: live get, loudness mutation, master set+readback; then the
        # parallel write: set+readback.  No interleaving.
        self.assertEqual(calls, ["get", "set", "get", "set", "get"])

    async def test_loudness_disable_serializes_against_parallel_write(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_run(args, **kwargs):
            if args[1] == "set-volume":
                calls.append("set")
                entered.set()
                release.wait(timeout=5)
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            calls.append("get")
            return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def loudness_percent_from_db(self, volume_db):
                return 44

            def loudness_db_from_percent(self, percent):
                return -float(percent)

            def set_loudness_volume_db(self, volume_db):
                return {"extras": {"loudness": {"params": {"volumeDb": volume_db}}}}

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": []}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return ""

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": False}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch(
            "system_volume.subprocess.run", side_effect=blocking_run
        ):
            extras_task = asyncio.create_task(main.save_easyeffects_extras(FakeExtrasRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            write_task = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            # The Loudness->master transfer owns the canonical lock; the
            # parallel write must wait.
            self.assertEqual(calls, ["set"])

            release.set()
            await asyncio.gather(extras_task, write_task)

        # transfer set+readback, loudness mutation, then the parallel write.
        self.assertEqual(calls, ["set", "get", "set", "get"])

    async def test_loudness_enable_acquires_canonical_before_mutation_lock(self):
        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def loudness_db_from_percent(self, percent):
                return -float(percent)

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": []}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return ""

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": True}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch.object(
            main, "get_output_volume", return_value=50
        ), mock.patch.object(
            main, "set_output_volume", return_value=50
        ):
            canonical = main._canonical_volume_write_lock()
            await canonical.acquire()
            try:
                extras_task = asyncio.create_task(main.save_easyeffects_extras(FakeExtrasRequest()))
                await asyncio.sleep(0.05)
                # Waiting at the canonical lock means the mutation lock is
                # not held yet: the enable path acquires canonical first.
                self.assertFalse(extras_task.done())
                self.assertFalse(main._easyeffects_mutation_lock().locked())
            finally:
                canonical.release()
            await extras_task

    async def test_cancelled_canonical_write_holds_lock_until_worker_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_run(args, **kwargs):
            if args[1] == "set-volume":
                calls.append("set")
                entered.set()
                release.wait(timeout=5)
                return subprocess.CompletedProcess([], 0, stdout="", stderr="")
            calls.append("get")
            return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")

        with mock.patch.object(main, "easyeffects_manager", None), mock.patch(
            "system_volume.subprocess.run", side_effect=blocking_run
        ):
            first = asyncio.create_task(main._set_canonical_output_volume(50))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            first.cancel()
            await asyncio.sleep(0.05)

            second = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            # The cancelled caller must still own the canonical lock until the
            # worker write actually finished.
            self.assertEqual(calls, ["set"])

            release.set()
            await asyncio.gather(first, second, return_exceptions=True)

            self.assertTrue(first.cancelled())
            self.assertEqual(calls, ["set", "get", "set", "get"])


class EasyEffectsExtrasVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        main.easyeffects_manager = None

    async def test_loudness_disable_transfer_order_is_preserved(self):
        order = []

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def loudness_percent_from_db(self, volume_db):
                return 44

            def apply_global_extras_to_all_presets(self, extras):
                order.append("apply")
                return {"extras": extras, "updated": 1, "skipped": []}

            def apply_autogain_loudness_runtime(self, previous, extras):
                order.append("apply")
                return {"extras": extras, "updated": 1, "skipped": []}

            def apply_loudness_strength_runtime(self, previous, extras):
                order.append("apply")
                return {"extras": extras, "updated": 1, "skipped": []}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return ""

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": False}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main, "set_output_volume", side_effect=lambda value: order.append(f"set-{value}") or value
        ), mock.patch.object(
            main, "get_output_volume", side_effect=AssertionError("no read expected")
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            await main.save_easyeffects_extras(FakeExtrasRequest())

        # System master transfer happens before the manager mutation.
        self.assertEqual(order, ["set-44", "apply"])

    async def test_loudness_disable_failure_rolls_back_to_100(self):
        order = []

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def loudness_percent_from_db(self, volume_db):
                return 44

            def apply_global_extras_to_all_presets(self, extras):
                order.append("apply")
                raise RuntimeError("preset write failed")

            def apply_autogain_loudness_runtime(self, previous, extras):
                order.append("apply")
                raise RuntimeError("preset write failed")

            def normalize_effects_extras(self, extras):
                return extras

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": False}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main, "set_output_volume", side_effect=lambda value: order.append(f"set-{value}") or value
        ):
            with self.assertRaises(RuntimeError):
                await main.save_easyeffects_extras(FakeExtrasRequest())

        self.assertEqual(order, ["set-44", "apply", "set-100"])

    async def test_blocking_wpctl_does_not_stop_event_loop(self):
        entered = threading.Event()
        release = threading.Event()
        ticks = []

        def blocking_run(args, **kwargs):
            entered.set()
            release.wait(timeout=5)
            return subprocess.CompletedProcess([], 0, stdout="Volume: 0.50\n", stderr="")

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.005)

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def loudness_db_from_percent(self, percent):
                return -float(percent)

            def apply_global_extras_to_all_presets(self, extras):
                return {"extras": extras, "updated": 1, "skipped": []}

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": []}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return ""

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": True}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch(
            "system_volume.subprocess.run", side_effect=blocking_run
        ):
            extras_task = asyncio.create_task(main.save_easyeffects_extras(FakeExtrasRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)
            self.assertGreater(len(ticks), 0)
            tick.cancel()

            release.set()
            await extras_task


if __name__ == "__main__":
    unittest.main(verbosity=2)
