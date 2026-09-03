#!/usr/bin/env python3
"""Concurrent threaded DSP mutations must stay serialized."""

import asyncio
import pathlib
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main
import dsp.api as dsp_api

dsp_api.configure_dsp_api(main._make_dsp_api_deps())


class FakeUploadFile:
    filename = "test.wav"

    def __init__(self):
        self._read = False

    async def read(self, size=-1):
        if self._read:
            return b""
        self._read = True
        return b"RIFFxxxx"


class DSPMutationSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        main.runtime.dsp_mutation_lock = None
        main.dsp_manager = None

    async def test_two_concurrent_ir_uploads_are_serialized(self):
        entered = threading.Event()
        release = threading.Event()
        critical = []

        class FakeManager:
            def upload_ir(self, source_path, filename, stored_name=None):
                critical.append(1)
                entered.set()
                release.wait(timeout=5)
                return {
                    "name": "x.irs",
                    "basename": "x",
                    "path": "/tmp/x.irs",
                    "size": 4,
                    "format": "irs",
                }

            def get_status(self):
                return {"status": "ok"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            first = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            second = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            await asyncio.sleep(0.05)
            # The second request must wait at the mutation lock: only one
            # manager mutation may be in flight at a time.
            self.assertEqual(len(critical), 1)

            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(len(critical), 2)

    async def test_convolver_create_uses_the_same_mutation_lock(self):
        main.runtime.dsp_mutation_lock = asyncio.Lock()
        lock = main._dsp_mutation_lock()
        self.assertIs(lock, main.runtime.dsp_mutation_lock)

        observed = []
        holder = asyncio.create_task(self._hold_lock(lock, observed))
        await asyncio.sleep(0.02)
        self.assertEqual(observed, ["held"])

        contender = asyncio.create_task(self._wait_for_lock(lock, observed))
        await asyncio.sleep(0.02)
        self.assertEqual(observed, ["held"])
        holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)
        await contender
        self.assertEqual(observed, ["held", "acquired"])

    async def _hold_lock(self, lock, observed):
        async with lock:
            observed.append("held")
            await asyncio.Event().wait()

    async def _wait_for_lock(self, lock, observed):
        async with lock:
            observed.append("acquired")


    async def test_threaded_upload_serializes_against_loop_delete(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FakeManager:
            def upload_ir(self, source_path, filename, stored_name=None):
                order.append("upload-entered")
                entered.set()
                release.wait(timeout=5)
                return {
                    "name": "x.irs",
                    "basename": "x",
                    "path": "/tmp/x.irs",
                    "size": 4,
                    "format": "irs",
                }

            def get_status(self):
                return {"status": "ok"}

            def delete_preset(self, preset_name):
                order.append("delete-entered")
                return None

            def get_active_preset(self):
                return None

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Some Preset"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            delete_task = asyncio.create_task(dsp_api.delete_dsp_preset(FakeDeleteRequest()))
            await asyncio.sleep(0.05)
            # The loop-side preset mutation must wait for the threaded upload.
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, delete_task)
            self.assertEqual(order, ["upload-entered", "delete-entered"])

    async def test_lock_binds_to_current_loop(self):
        lock = main._dsp_mutation_lock()
        async with lock:
            pass

    async def test_lock_is_recreated_after_lifecycle_reset(self):
        # New test loop: the shutdown path resets the lock to None, so the
        # runtime restart must create a fresh loop-bound lock (reusing the
        # old loop-bound lock would raise here).
        main.runtime.dsp_mutation_lock = None
        lock = main._dsp_mutation_lock()
        async with lock:
            pass


    async def test_threaded_upload_serializes_against_create_convolver_preset(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FakeManager:
            def upload_ir(self, source_path, filename, stored_name=None):
                order.append("upload-entered")
                entered.set()
                release.wait(timeout=5)
                return {
                    "name": "x.irs",
                    "basename": "x",
                    "path": "/tmp/x.irs",
                    "size": 4,
                    "format": "irs",
                }

            def create_convolver_preset(self, preset_name, ir_filename, extras=None):
                order.append("convolver-entered")
                return {"name": preset_name}

            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def load_preset(self, preset_name, convolver_sample_rate_hz=None):
                pass

            def get_status(self):
                return {"status": "ok"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            convolver_task = asyncio.create_task(dsp_api.create_convolver_preset(
                preset_name="New Convolver", ir_filename="x.irs"
            ))
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, convolver_task)
            self.assertEqual(order, ["upload-entered", "convolver-entered"])

    async def test_threaded_upload_serializes_against_global_extras_mutation(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}, "headroomGainDb": 0.0}

            def upload_ir(self, source_path, filename, stored_name=None):
                order.append("upload-entered")
                entered.set()
                release.wait(timeout=5)
                return {
                    "name": "x.irs",
                    "basename": "x",
                    "path": "/tmp/x.irs",
                    "size": 4,
                    "format": "irs",
                }

            def apply_global_extras_to_all_presets(self, extras):
                order.append("extras-entered")
                return {"extras": extras, "updated": 1, "skipped": []}

            def get_active_preset(self):
                return ""

            def get_status(self):
                return {"status": "ok"}

        class FakeExtrasRequest:
            async def json(self):
                return {"headroomGainDb": -2.5}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            extras_task = asyncio.create_task(dsp_api.save_dsp_extras(FakeExtrasRequest()))
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, extras_task)
            self.assertEqual(order, ["upload-entered", "extras-entered"])

    async def test_volume_write_never_enters_the_dsp_mutation_path(self):
        # The footer slider writes only the global master; it must not route
        # through ``set_loudness_volume_db`` (the old Loudness-owned path).
        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {}}}

            def get_active_preset(self):
                return "Neutral"

            def set_loudness_volume_db(self, volume_db):
                raise AssertionError("footer slider must not mutate Loudness")

        fake = FakeManager()
        with mock.patch.object(main, "dsp_manager", fake), mock.patch.object(
            main, "set_output_volume", return_value=32
        ) as set_master:
            result = await main._set_canonical_output_volume(32)

        self.assertEqual(result, {"volume": 32})
        set_master.assert_called_once_with(32)


    async def test_cancelled_upload_holds_lock_until_worker_finishes(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FakeManager:
            def upload_ir(self, source_path, filename, stored_name=None):
                order.append("upload-entered")
                entered.set()
                release.wait(timeout=5)
                return {
                    "name": "x.irs",
                    "basename": "x",
                    "path": "/tmp/x.irs",
                    "size": 4,
                    "format": "irs",
                }

            def delete_preset(self, preset_name):
                order.append("delete-entered")

            def get_active_preset(self):
                return None

            def get_status(self):
                return {"status": "ok"}

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Some Preset"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            # Cancel the caller while the worker thread is still running.
            upload_task.cancel()
            await asyncio.sleep(0.05)

            delete_task = asyncio.create_task(dsp_api.delete_dsp_preset(FakeDeleteRequest()))
            await asyncio.sleep(0.05)
            # The cancelled caller must still own the mutation lock until the
            # worker actually finished: the delete must not enter yet.
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, delete_task, return_exceptions=True)

            self.assertTrue(upload_task.cancelled())
            self.assertEqual(order, ["upload-entered", "delete-entered"])

    async def test_delete_of_active_preset_resyncs_the_neutral_fallback(self):
        class FakeManager:
            def __init__(self):
                self.active = "Room"
                self.deleted = []

            def get_active_preset(self):
                return self.active

            def delete_preset(self, preset_name):
                self.deleted.append(preset_name)
                if self.active == "Room":
                    # Mirror the real manager: deleting the active preset
                    # moves the persisted active state to Neutral.
                    self.active = "Neutral"

            def get_status(self):
                return {"status": "ok", "active_preset": self.active}

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Room"}

        fake = FakeManager()
        load_preset = mock.AsyncMock()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "_load_dsp_preset", load_preset
        ), mock.patch.object(main.manager, "broadcast", mock.AsyncMock()), mock.patch.object(
            main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
        ):
            result = await dsp_api.delete_dsp_preset(FakeDeleteRequest())

        self.assertEqual(result, {"status": "ok", "deleted": "Room"})
        self.assertEqual(fake.deleted, ["Room"])
        # The running engine must be resynced to the Neutral fallback instead
        # of keeping the deleted preset's graph alive.
        load_preset.assert_awaited_once_with("Neutral")

    async def test_delete_of_inactive_preset_does_not_touch_the_runtime(self):
        class FakeManager:
            def __init__(self):
                self.deleted = []

            def get_active_preset(self):
                return "Other"

            def delete_preset(self, preset_name):
                self.deleted.append(preset_name)

            def get_status(self):
                return {"status": "ok", "active_preset": "Other"}

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Room"}

        fake = FakeManager()
        load_preset = mock.AsyncMock()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "_load_dsp_preset", load_preset
        ), mock.patch.object(main.manager, "broadcast", mock.AsyncMock()), mock.patch.object(
            main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"
        ):
            await dsp_api.delete_dsp_preset(FakeDeleteRequest())

        self.assertEqual(fake.deleted, ["Room"])
        load_preset.assert_not_awaited()

    async def _extras_fake_manager(self, order):
        class FakeManager:
            EXCLUDED_GLOBAL_EXTRAS_PRESETS = {"Direct"}

            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def get_active_preset(self):
                return "Neutral"

            def apply_global_extras_to_all_presets(self, extras):
                order.append("extras-entered")
                return {"extras": extras, "updated": 1, "skipped": []}

            def get_status(self):
                return {"status": "ok"}

        return FakeManager()

    async def test_extras_fallback_reload_holds_the_mutation_lock_when_canonical_is_held(self):
        main.runtime.dsp_mutation_lock = None
        main.runtime.canonical_volume_write_lock = None
        lock_probe = {}

        async def fake_load_preset(preset_name, **_kwargs):
            # Runs while the route holds the canonical volume write lock and
            # must have re-acquired the DSP mutation lock before calling the
            # loader under its documented "both locks held" contract.
            lock_probe["canonical_locked"] = main._canonical_volume_write_lock().locked()
            lock_probe["mutation_locked"] = main._dsp_mutation_lock().locked()
            lock_probe["kwargs"] = dict(_kwargs)
            lock_probe["args"] = (preset_name,)

        class FakeExtrasRequest:
            async def json(self):
                return {"loudnessEnabled": True, "headroomGainDb": -6.0}

        order = []
        fake = await self._extras_fake_manager(order)
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "_load_dsp_preset", fake_load_preset
        ), mock.patch.object(main, "_volume_state_for_manager", mock.AsyncMock()), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            result = await dsp_api.save_dsp_extras(FakeExtrasRequest())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(lock_probe["args"], ("Neutral",))
        self.assertEqual(lock_probe["kwargs"], {"_locks_held": True})
        self.assertTrue(lock_probe["canonical_locked"])
        self.assertTrue(lock_probe["mutation_locked"])
        self.assertFalse(main._canonical_volume_write_lock().locked())
        main.runtime.canonical_volume_write_lock = None

    async def test_extras_fallback_reload_uses_full_loader_without_canonical(self):
        main.runtime.dsp_mutation_lock = None
        lock_probe = {}

        async def fake_load_preset(preset_name, **_kwargs):
            # Non-loudness extras update: no canonical lock is held, so the
            # route must hand the reload to the loader without the "both locks
            # held" shortcut (the loader acquires them itself).
            lock_probe["canonical_locked"] = main._canonical_volume_write_lock().locked()
            lock_probe["kwargs"] = dict(_kwargs)
            lock_probe["args"] = (preset_name,)

        class FakeExtrasRequest:
            async def json(self):
                return {"headroomGainDb": -6.0}

        order = []
        fake = await self._extras_fake_manager(order)
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main, "_load_dsp_preset", fake_load_preset
        ), mock.patch.object(main, "_volume_state_for_manager", mock.AsyncMock()), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            result = await dsp_api.save_dsp_extras(FakeExtrasRequest())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(lock_probe["args"], ("Neutral",))
        self.assertEqual(lock_probe["kwargs"], {})
        self.assertFalse(lock_probe["canonical_locked"])
        main.runtime.canonical_volume_write_lock = None

    async def test_preset_load_waits_for_threaded_mutation(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": False, "params": {}}}

            def upload_ir(self, source_path, filename, stored_name=None):
                order.append("upload-entered")
                entered.set()
                release.wait(timeout=5)
                return {
                    "name": "x.irs",
                    "basename": "x",
                    "path": "/tmp/x.irs",
                    "size": 4,
                    "format": "irs",
                }

            def load_preset(self, preset_name, convolver_sample_rate_hz=None):
                order.append("load-entered")

            def load_compare_state(self):
                return {}

            def save_compare_state(self, compare):
                pass

            def get_status(self):
                return {"status": "ok"}

        class FakeLoadRequest:
            async def json(self):
                return {"preset_name": "Neutral"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_dsp_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main.dsp_orchestrator, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(dsp_api.upload_dsp_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            load_task = asyncio.create_task(dsp_api.load_dsp_preset(FakeLoadRequest()))
            await asyncio.sleep(0.05)
            # load_preset must not enter the manager while the threaded
            # mutation is still running.
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, load_task)
            self.assertEqual(order, ["upload-entered", "load-entered"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
