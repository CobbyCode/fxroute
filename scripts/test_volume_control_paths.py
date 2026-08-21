#!/usr/bin/env python3
"""Silent-active recovery uses the live volume; /api/volume never blocks the loop."""

import asyncio
import copy
import pathlib
from contextlib import ExitStack
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import dsp.api as dsp_api

dsp_api.configure_dsp_api(main._make_dsp_api_deps())
import audio.system_volume as system_volume
import playback.silent_active as silent_active


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


class _FakePeakMonitor:
    def snapshot(self):
        return {"vu_db": -60.0, "vu_fresh": False}


class SilentActiveLiveVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_silent_attempts = dict(main.silent_active_recovery.recovery_attempts)
        main.silent_active_recovery.recovery_attempts.clear()

    async def asyncTearDown(self):
        main.silent_active_recovery.recovery_attempts.clear()
        main.silent_active_recovery.recovery_attempts.update(self.original_silent_attempts)

    def _patches(self):
        return [
            mock.patch.object(main.runtime, "peak_monitor", _FakePeakMonitor()),
            mock.patch.object(main.runtime, "player_instance", _FakePlayer()),
            mock.patch.object(main, "_current_track_matches", return_value=True),
            mock.patch.object(main, "_list_mpv_sink_inputs", return_value=[{"muted": False}]),
        ]

    async def test_status_cache_says_100_but_live_volume_is_zero_blocks_recovery(self):
        with mock.patch.object(silent_active, "get_output_volume", return_value=0) as live, mock.patch.object(
            silent_active, "get_audio_output_overview", return_value={}
        ) as overview, mock.patch.object(main, "_run_debug_command", return_value={"stdout": "", "stderr": ""}) as debug, mock.patch.object(
            main.silent_active_recovery, "_source_links_present", return_value=False
        ), ExitStack() as stack:
            for patch in self._patches():
                stack.enter_context(patch)
            await main.silent_active_recovery._check_and_recover(
                source="local", signature="sig-live-zero", track={"id": "x"}
            )
        live.assert_called()
        overview.assert_not_called()
        debug.assert_not_called()

    async def test_status_cache_says_zero_but_live_volume_is_positive_continues(self):
        with mock.patch.object(silent_active, "get_output_volume", return_value=50) as live, mock.patch.object(
            silent_active, "get_audio_output_overview", return_value={"output_mode": {}}
        ) as overview, mock.patch.object(main, "_run_debug_command", return_value={"stdout": "", "stderr": ""}) as debug, mock.patch.object(
            main.silent_active_recovery, "_source_links_present", return_value=False
        ), ExitStack() as stack:
            for patch in self._patches():
                stack.enter_context(patch)
            await main.silent_active_recovery._check_and_recover(
                source="local", signature="sig-live-positive", track={"id": "x"}
            )
        live.assert_called()
        overview.assert_called()
        debug.assert_called()

    async def test_live_read_failure_uses_safe_fallback_like_before(self):
        with mock.patch.object(
            silent_active, "get_output_volume", side_effect=RuntimeError("wpctl wedged")
        ), mock.patch.object(
            silent_active, "get_audio_output_overview", return_value={}
        ) as overview, mock.patch.object(
            main.silent_active_recovery, "_source_links_present", return_value=True
        ), ExitStack() as stack:
            for patch in self._patches():
                stack.enter_context(patch)
            await main.silent_active_recovery._check_and_recover(
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

        with mock.patch.object(main.runtime, "player_instance", _FakePlayer()), mock.patch.object(
            main, "ensure_local_source_volume"
        ), mock.patch.object(main, "dsp_manager", None), mock.patch.object(
            main, "build_playback_payload", return_value={"volume": 50}
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch(
            "audio.system_volume.subprocess.run", side_effect=blocking_run
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
        main.runtime.canonical_volume_write_lock = None

    async def asyncTearDown(self):
        main.runtime.canonical_volume_write_lock = None
        main.dsp_manager = None
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

        with mock.patch.object(main, "dsp_manager", None), mock.patch.object(
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

    async def test_volume_write_never_touches_loudness(self):
        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def get_active_preset(self):
                return "Neutral"

            def set_loudness_volume_db(self, volume_db):
                raise AssertionError("the footer slider must not rewrite the Loudness work point")

        fake = FakeManager()
        sync = mock.AsyncMock()
        with mock.patch.object(main, "dsp_manager", fake), mock.patch.object(
            main.dsp_orchestrator, "sync_runtime", sync
        ), mock.patch.object(main, "set_output_volume", return_value=60):
            result = await main._set_canonical_output_volume(60)

        self.assertEqual(result, {"volume": 60})
        sync.assert_not_awaited()

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

        with mock.patch.object(main, "dsp_manager", None), mock.patch(
            "audio.system_volume.subprocess.run", side_effect=fake_run
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


    async def test_loudness_enable_serializes_and_never_writes_master(self):
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
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}
            PURE_PRESET = "Direct"

            def __init__(self):
                self.extras = {"loudness": {"enabled": False, "params": {}}}

            def load_global_extras(self):
                return copy.deepcopy(self.extras)

            def apply_autogain_loudness_runtime(self, previous, extras):
                self.extras = copy.deepcopy(extras)
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return "Neutral"

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": True}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch.object(
            main.dsp_orchestrator, "sync_runtime", mock.AsyncMock()
        ), mock.patch(
            "audio.system_volume.subprocess.run", side_effect=blocking_run
        ):
            extras_task = asyncio.create_task(dsp_api.save_dsp_extras(FakeExtrasRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            write_task = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            # The Loudness enable owns the canonical lock; the parallel write
            # must wait, and the enable itself never writes the master.
            self.assertEqual(calls, ["get"])

            release.set()
            await asyncio.gather(extras_task, write_task)

        # enable reads the master for state capture only; then the parallel
        # write sets and reads it back.  No master=100 write anywhere.
        self.assertEqual(calls, ["get", "set", "get"])

    async def test_loudness_disable_serializes_and_never_writes_master(self):
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
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}
            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def apply_global_extras_to_all_presets(self, extras):
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return "Neutral"

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": False}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch.object(
            main.dsp_orchestrator, "sync_runtime", mock.AsyncMock()
        ), mock.patch(
            "audio.system_volume.subprocess.run", side_effect=blocking_run
        ):
            extras_task = asyncio.create_task(dsp_api.save_dsp_extras(FakeExtrasRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            write_task = asyncio.create_task(main._set_canonical_output_volume(60))
            await asyncio.sleep(0.05)
            self.assertEqual(calls, ["get"])

            release.set()
            await asyncio.gather(extras_task, write_task)

        self.assertEqual(calls, ["get", "set", "get"])

    async def test_loudness_enable_acquires_canonical_before_mutation_lock(self):
        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}
            PURE_PRESET = "Direct"

            def __init__(self):
                self.extras = {"loudness": {"enabled": False, "params": {}}}

            def load_global_extras(self):
                return copy.deepcopy(self.extras)

            def apply_autogain_loudness_runtime(self, previous, extras):
                self.extras = copy.deepcopy(extras)
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return "Neutral"

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": True}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch.object(
            main, "get_output_volume", return_value=50
        ), mock.patch.object(
            main, "set_output_volume", return_value=50
        ):
            canonical = main._canonical_volume_write_lock()
            await canonical.acquire()
            try:
                extras_task = asyncio.create_task(dsp_api.save_dsp_extras(FakeExtrasRequest()))
                await asyncio.sleep(0.05)
                # Waiting at the canonical lock means the mutation lock is
                # not held yet: the enable path acquires canonical first.
                self.assertFalse(extras_task.done())
                self.assertFalse(main._dsp_mutation_lock().locked())
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

        with mock.patch.object(main, "dsp_manager", None), mock.patch(
            "audio.system_volume.subprocess.run", side_effect=blocking_run
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


class DSPExtrasVolumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        main.dsp_manager = None

    async def test_loudness_disable_does_not_transfer_to_master(self):
        order = []

        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}
            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def apply_global_extras_to_all_presets(self, extras):
                order.append("apply")
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def apply_autogain_loudness_runtime(self, previous, extras):
                order.append("apply")
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return "Neutral"

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": False}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(
            main, "set_output_volume", side_effect=lambda value: order.append(f"set-{value}") or value
        ), mock.patch.object(
            main, "get_output_volume", return_value=55
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            await dsp_api.save_dsp_extras(FakeExtrasRequest())

        # The disable only applies the extras change; it never rewrites the master.
        self.assertEqual(order, ["apply"])

    async def test_autogain_change_reloads_active_native_preset(self):
        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

            def load_global_extras(self):
                return {"autogain": {"enabled": False, "params": {"targetDb": -12.0}},
                        "loudness": {"enabled": False, "params": {}}}

            def normalize_effects_extras(self, extras):
                return extras

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": []}

            def get_active_preset(self):
                return "Neutral"

            def get_status(self):
                return {"status": "ok"}

        class FakeRequest:
            async def json(self):
                return {"autogain_enabled": True}

        fake = FakeManager()
        reload_preset = mock.AsyncMock()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(main, "_load_dsp_preset", reload_preset), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            await dsp_api.save_dsp_extras(FakeRequest())

        reload_preset.assert_awaited_once_with("Neutral", _locks_held=True)

    async def test_loudness_disable_failure_restores_original_master(self):
        order = []

        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {"volumeDb": -10.0}}}

            def get_active_preset(self):
                return "Neutral"

            def apply_global_extras_to_all_presets(self, extras):
                order.append("apply")
                raise RuntimeError("preset write failed")

            def apply_autogain_loudness_runtime(self, previous, extras):
                order.append("apply")
                raise RuntimeError("preset write failed")

            def save_global_extras(self, extras):
                return extras

            def normalize_effects_extras(self, extras):
                return extras

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": False}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(
            main, "set_output_volume", side_effect=lambda value: order.append(f"set-{value}") or value
        ), mock.patch.object(
            main, "get_output_volume", return_value=42
        ):
            with self.assertRaises(RuntimeError):
                await dsp_api.save_dsp_extras(FakeExtrasRequest())

        # The failed disable restores the live master captured before the
        # change, never a hardcoded 100%.
        self.assertEqual(order, ["apply", "set-42"])

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
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}
            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def apply_global_extras_to_all_presets(self, extras):
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def apply_autogain_loudness_runtime(self, previous, extras):
                return {"extras": extras, "updated": 1, "skipped": [], "runtime_applied": True}

            def normalize_effects_extras(self, extras):
                return extras

            def get_active_preset(self):
                return "Neutral"

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"loudness_enabled": True}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "dsp_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch(
            "audio.system_volume.subprocess.run", side_effect=blocking_run
        ):
            extras_task = asyncio.create_task(dsp_api.save_dsp_extras(FakeExtrasRequest()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)
            self.assertGreater(len(ticks), 0)
            tick.cancel()

            release.set()
            await extras_task


NATIVE_STEREO_LINKS = (
    "\tmpv:output_FL\n"
    "  |-> fxroute_dsp_sink:playback_FL\n"
    "\tmpv:output_FR\n"
    "  |-> fxroute_dsp_sink:playback_FR\n"
    "\tfxroute_dsp_sink:monitor_FL\n"
    "  |-> fxroute_dsp:input_1\n"
    "\tfxroute_dsp_sink:monitor_FR\n"
    "  |-> fxroute_dsp:input_2\n"
    "\tfxroute_dsp:output_1\n"
    "  |-> alsa_output.hw:playback_FL\n"
    "\tfxroute_dsp:output_2\n"
    "  |-> alsa_output.hw:playback_FR\n"
)


class SilentActiveSourceLinkTests(unittest.TestCase):
    """Stereo silent-active link check uses the native DSP topology."""

    def stereo(self, **overrides):
        return {"mode": "stereo", "effective_output_key": "alsa_output.hw", **overrides}

    def test_stereo_native_topology_is_recognized(self):
        self.assertTrue(main.silent_active_recovery._source_links_present(
            "local", NATIVE_STEREO_LINKS, self.stereo()))

    def test_stereo_spotify_source_is_recognized(self):
        text = NATIVE_STEREO_LINKS.replace("mpv:output_FL", "spotify:output_FL") \
                                  .replace("mpv:output_FR", "spotify:output_FR")
        self.assertTrue(main.silent_active_recovery._source_links_present(
            "spotify", text, self.stereo()))

    def test_stereo_without_dsp_to_hardware_link_is_not_recognized(self):
        text = NATIVE_STEREO_LINKS.replace(
            "\tfxroute_dsp:output_1\n  |-> alsa_output.hw:playback_FL\n", "")
        self.assertFalse(main.silent_active_recovery._source_links_present(
            "local", text, self.stereo()))

    def test_stereo_without_source_to_sink_link_is_not_recognized(self):
        text = NATIVE_STEREO_LINKS.replace(
            "\tmpv:output_FL\n  |-> fxroute_dsp_sink:playback_FL\n", "")
        self.assertFalse(main.silent_active_recovery._source_links_present(
            "local", text, self.stereo()))

    def test_stereo_obsolete_legacy_ports_are_not_recognized(self):
        # The pre-native-DSP stereo check would have passed on these legacy
        # native check must not.
        text = (
            "\tmpv:output_FL\n"
            "  |-> fxroute_dsp_sink:playback_FL\n"
            "\tee_soe_output_level:output_FL\n"
            "  |-> alsa_output.hw:playback_FL\n"
            "\tee_soe_output_level:output_FR\n"
            "  |-> alsa_output.hw:playback_FR\n"
        )
        self.assertFalse(main.silent_active_recovery._source_links_present(
            "local", text, self.stereo()))

    def test_stereo_interleaved_links_are_recognized(self):
        text = (
            "\tfxroute_dsp_sink:playback_FL\n"
            "  |<- spotify:output_FL\n"
            "  |<- mpv:output_FL\n"
            "\tfxroute_dsp:output_1\n"
            "  |-> alsa_output.hw:playback_FL\n"
            "\tfxroute_dsp:output_2\n"
            "  |-> alsa_output.hw:playback_FR\n"
        )
        self.assertTrue(main.silent_active_recovery._source_links_present(
            "local", text, self.stereo()))

    def test_subwoofer_mode_only_requires_source_link(self):
        self.assertTrue(main.silent_active_recovery._source_links_present(
            "local", "\tmpv:output_FL\n  |-> fxroute_dsp_sink:playback_FL\n",
            {"mode": "subwoofer-2.1", "effective_output_key": "alsa_output.hw"}))


class _FakeVolumeManager:
    """Minimal DSPManager stand-in for volume-ownership tests."""

    PURE_PRESET = "Direct"
    EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

    def __init__(self, *, loudness_enabled=True, active_preset="Neutral", volume_db=-20.0):
        self.extras = {
            "loudness": {"enabled": loudness_enabled,
                         "params": {"volumeDb": volume_db, "calibration": {}, "calibrationProfiles": {}}}
        }
        self.active_preset = active_preset
        self.loudness_volume_writes = []
        self.saved = []

    def load_global_extras(self):
        return copy.deepcopy(self.extras)

    def get_active_preset(self):
        return self.active_preset

    def loudness_db_from_percent(self, percent):
        return system_volume.volume_percent_to_db(percent)

    def loudness_percent_from_db(self, db):
        return system_volume.volume_db_to_percent(db)

    def set_loudness_volume_db(self, volume_db):
        self.loudness_volume_writes.append(float(volume_db))
        self.extras["loudness"]["params"]["volumeDb"] = float(volume_db)
        return {"extras": copy.deepcopy(self.extras), "runtime_applied": True,
                "updated": 1, "skipped": []}

    def save_global_extras(self, extras):
        self.extras = copy.deepcopy(extras)
        self.saved.append(copy.deepcopy(extras))

    def load_preset(self, preset_name, **_kwargs):
        self.active_preset = preset_name


class VolumeOwnershipDirectTests(unittest.IsolatedAsyncioTestCase):
    """The footer slider drives only the global master; presets never do."""

    def setUp(self):
        self.manager = _FakeVolumeManager()
        self.volume_writes = []

        def recording_set_volume(value):
            self.volume_writes.append(int(round(float(value))))
            self.live_master = int(round(float(value)))

        self.live_master = 100
        patcher = mock.patch.object(main, "set_output_volume", new=recording_set_volume)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_get = mock.patch.object(main, "get_output_volume", new=lambda: self.live_master)
        patcher_get.start()
        self.addCleanup(patcher_get.stop)
        patcher2 = mock.patch.object(main, "dsp_manager", self.manager)
        patcher2.start()
        self.addCleanup(patcher2.stop)

    def use_manager(self, **kwargs):
        self.manager = _FakeVolumeManager(**kwargs)
        main.dsp_manager = self.manager
        return self.manager

    async def test_volume_slider_in_direct_controls_only_the_master(self):
        self.use_manager(loudness_enabled=True, active_preset="Direct")
        result = await main._set_canonical_output_volume(30)
        self.assertEqual(result, {"volume": 30})
        self.assertEqual(self.volume_writes, [30])
        self.assertEqual(self.manager.loudness_volume_writes, [])

    async def test_volume_slider_in_neutral_controls_only_the_master(self):
        self.use_manager(loudness_enabled=True, active_preset="Neutral")
        result = await main._set_canonical_output_volume(40)
        self.assertEqual(result, {"volume": 40})
        self.assertEqual(self.volume_writes, [40])
        self.assertEqual(self.manager.loudness_volume_writes, [])

    async def test_preset_load_never_moves_master(self):
        manager = self.use_manager(loudness_enabled=True, active_preset="Neutral", volume_db=-20.0)
        self.live_master = 65
        await main._load_dsp_preset("Direct")
        self.assertEqual(self.volume_writes, [])
        self.assertEqual(self.live_master, 65)
        self.assertEqual(manager.active_preset, "Direct")
        self.assertAlmostEqual(
            float(manager.extras["loudness"]["params"]["volumeDb"]), -20.0, places=9
        )


if __name__ == "__main__":
    unittest.main()
