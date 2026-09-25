#!/usr/bin/env python3
"""Extras reload failures must not acknowledge unsynchronized DSP state."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from dsp import api as dsp_api
from dsp import preset_loading
from dsp.manager import DSPManager


class ExtrasReloadRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_worker_after_persist_restores_previous_runtime(self):
        with tempfile.TemporaryDirectory() as home:
            manager = DSPManager(home=Path(home))
            previous = manager.load_global_extras()
            running_extras = previous
            real_drain = main._drain_worker
            reloads = []

            async def cancel_after_commit(func, *args, **kwargs):
                result = await real_drain(func, *args, **kwargs)
                if func == manager.apply_global_extras_to_all_presets:
                    raise asyncio.CancelledError
                return result

            async def reload_active(_preset, **_kwargs):
                nonlocal running_extras
                running_extras = manager.load_global_extras()
                reloads.append(running_extras)

            class Request:
                async def json(self):
                    return {"headroomGainDb": -6.0}

            main.runtime.canonical_volume_write_lock = None
            main.runtime.dsp_mutation_lock = None
            with mock.patch.object(main, "_require_dsp_manager", return_value=manager), mock.patch.object(
                main, "_drain_worker", new=cancel_after_commit
            ), mock.patch.object(main, "_load_dsp_preset", new=reload_active), mock.patch.object(
                main, "_volume_state_for_manager", new=mock.AsyncMock()
            ), mock.patch.object(
                preset_loading, "_restore_volume_state", new=mock.AsyncMock()
            ), mock.patch.object(main.manager, "broadcast", new=mock.AsyncMock()) as broadcast, mock.patch.object(
                main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
            ) as refresh:
                with self.assertRaises(asyncio.CancelledError):
                    await dsp_api.save_dsp_extras(Request())

            self.assertEqual(manager.load_global_extras(), previous)
            self.assertEqual(running_extras, previous)
            self.assertEqual(len(reloads), 1)
            broadcast.assert_not_awaited()
            refresh.assert_not_called()

    async def test_repeated_reload_failure_stops_uncertain_dsp_runtime(self):
        for recovery_failure in (RuntimeError("reload failed"), asyncio.CancelledError()):
            with self.subTest(recovery_failure=type(recovery_failure).__name__), tempfile.TemporaryDirectory() as home:
                await self._assert_failed_recovery_stops_runtime(home, recovery_failure)

    async def _assert_failed_recovery_stops_runtime(self, home, recovery_failure):
        manager = DSPManager(home=Path(home))
        previous = manager.load_global_extras()
        runtime = mock.Mock(stop=mock.AsyncMock())
        attempts = 0

        async def reload_active(_preset, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("reload failed")
            raise recovery_failure

        class Request:
            async def json(self):
                return {"headroomGainDb": -6.0}

        main.runtime.canonical_volume_write_lock = None
        main.runtime.dsp_mutation_lock = None
        with mock.patch.object(main, "_require_dsp_manager", return_value=manager), mock.patch.object(
            main, "_load_dsp_preset", new=reload_active
        ), mock.patch.object(main.runtime, "dsp_runtime", runtime), mock.patch.object(
            main, "_volume_state_for_manager", new=mock.AsyncMock()
        ), mock.patch.object(
            preset_loading, "_restore_volume_state", new=mock.AsyncMock()
        ), mock.patch.object(main.manager, "broadcast", new=mock.AsyncMock()) as broadcast, mock.patch.object(
            main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
        ) as refresh:
            with self.assertRaises(type(recovery_failure)):
                await dsp_api.save_dsp_extras(Request())

        self.assertEqual(manager.load_global_extras(), previous)
        self.assertEqual(attempts, 2)
        runtime.stop.assert_awaited_once()
        broadcast.assert_not_awaited()
        refresh.assert_not_called()

    async def test_failed_reload_cannot_roll_back_a_newer_extras_update(self):
        with tempfile.TemporaryDirectory() as home:
            manager = DSPManager(home=Path(home))
            first_reload_started = asyncio.Event()
            release_first_reload = asyncio.Event()
            second_committed = asyncio.Event()
            loop = asyncio.get_running_loop()
            running_extras = manager.load_global_extras()
            reloads = 0
            real_apply = manager.apply_global_extras_to_all_presets

            def record_apply(extras):
                result = real_apply(extras)
                if extras["headroom"]["params"]["gainDb"] == -4.0:
                    loop.call_soon_threadsafe(second_committed.set)
                return result

            async def reload_active(_preset, **_kwargs):
                nonlocal reloads, running_extras
                reloads += 1
                if reloads == 1:
                    first_reload_started.set()
                    await release_first_reload.wait()
                    raise RuntimeError("first reload failed")
                running_extras = manager.load_global_extras()

            class Request:
                def __init__(self, headroom):
                    self.headroom = headroom

                async def json(self):
                    return {"headroomGainDb": self.headroom}

            main.runtime.canonical_volume_write_lock = None
            main.runtime.dsp_mutation_lock = None
            with mock.patch.object(manager, "apply_global_extras_to_all_presets", new=record_apply), mock.patch.object(
                main, "_require_dsp_manager", return_value=manager
            ), mock.patch.object(
                main, "_load_dsp_preset", new=reload_active
            ), mock.patch.object(main, "_volume_state_for_manager", new=mock.AsyncMock()), mock.patch.object(
                preset_loading, "_restore_volume_state", new=mock.AsyncMock()
            ), mock.patch.object(
                main.manager, "broadcast", new=mock.AsyncMock()
            ), mock.patch.object(
                main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
            ):
                first = asyncio.create_task(dsp_api.save_dsp_extras(Request(-6)))
                try:
                    await asyncio.wait_for(first_reload_started.wait(), 5)
                    second = asyncio.create_task(dsp_api.save_dsp_extras(Request(-4)))
                    try:
                        await asyncio.wait_for(second_committed.wait(), 0.2)
                    except asyncio.TimeoutError:
                        pass  # With the transaction lock, the second write waits.
                    await asyncio.sleep(0.02)
                finally:
                    release_first_reload.set()
                with self.assertRaisesRegex(RuntimeError, "first reload failed"):
                    await first
                self.assertEqual((await second)["status"], "ok")

            self.assertEqual(manager.load_global_extras()["headroom"]["params"]["gainDb"], -4.0)
            self.assertEqual(running_extras, manager.load_global_extras())

    async def test_failed_reload_restores_persistence_and_runtime_without_peak_refresh(self):
        cases = (
            ({"headroomGainDb": -6.0}, RuntimeError("runtime reload failed")),
            ({"loudnessEnabled": True}, RuntimeError("runtime reload failed")),
            ({"headroomGainDb": -6.0}, asyncio.CancelledError()),
        )
        for payload, failure in cases:
            with self.subTest(payload=payload, failure=type(failure).__name__), tempfile.TemporaryDirectory() as home:
                manager = DSPManager(home=Path(home))
                previous = manager.load_global_extras()
                running_extras = previous
                reload_seen = []

                async def reload_active(preset, **kwargs):
                    nonlocal running_extras
                    self.assertEqual(preset, "Neutral")
                    reload_seen.append(manager.load_global_extras())
                    if len(reload_seen) == 1:
                        raise failure
                    running_extras = manager.load_global_extras()

                class Request:
                    async def json(self):
                        return payload

                main.runtime.canonical_volume_write_lock = None
                main.runtime.dsp_mutation_lock = None
                with mock.patch.object(main, "_require_dsp_manager", return_value=manager), mock.patch.object(
                    main, "_load_dsp_preset", new=reload_active
                ), mock.patch.object(
                    main, "_volume_state_for_manager", new=mock.AsyncMock()
                ), mock.patch.object(
                    preset_loading, "_restore_volume_state", new=mock.AsyncMock()
                ), mock.patch.object(
                    main.manager, "broadcast", new=mock.AsyncMock()
                ) as broadcast, mock.patch.object(
                    main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
                ) as refresh:
                    with self.assertRaises(type(failure)):
                        await dsp_api.save_dsp_extras(Request())

                self.assertEqual(len(reload_seen), 2)
                self.assertNotEqual(reload_seen[0], previous)
                self.assertEqual(manager.load_global_extras(), previous)
                self.assertEqual(running_extras, previous)
                self.assertFalse(main._canonical_volume_write_lock().locked())
                broadcast.assert_not_awaited()
                refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
