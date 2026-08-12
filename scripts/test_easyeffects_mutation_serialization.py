#!/usr/bin/env python3
"""Concurrent threaded EasyEffects mutations must stay serialized."""

import asyncio
import pathlib
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import main


class FakeUploadFile:
    filename = "test.wav"

    def __init__(self):
        self._read = False

    async def read(self, size=-1):
        if self._read:
            return b""
        self._read = True
        return b"RIFFxxxx"


class EasyEffectsMutationSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        main.easyeffects_mutation_lock = None
        main.easyeffects_manager = None

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
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            first = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            second = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            await asyncio.sleep(0.05)
            # The second request must wait at the mutation lock: only one
            # manager mutation may be in flight at a time.
            self.assertEqual(len(critical), 1)

            release.set()
            await asyncio.gather(first, second)
            self.assertEqual(len(critical), 2)

    async def test_convolver_create_uses_the_same_mutation_lock(self):
        main.easyeffects_mutation_lock = asyncio.Lock()
        lock = main._easyeffects_mutation_lock()
        self.assertIs(lock, main.easyeffects_mutation_lock)

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

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Some Preset"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            delete_task = asyncio.create_task(main.delete_easyeffects_preset(FakeDeleteRequest()))
            await asyncio.sleep(0.05)
            # The loop-side preset mutation must wait for the threaded upload.
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, delete_task)
            self.assertEqual(order, ["upload-entered", "delete-entered"])

    async def test_lock_binds_to_current_loop(self):
        lock = main._easyeffects_mutation_lock()
        async with lock:
            pass

    async def test_lock_is_recreated_after_lifecycle_reset(self):
        # New test loop: the shutdown path resets the lock to None, so the
        # runtime restart must create a fresh loop-bound lock (reusing the
        # old loop-bound lock would raise here).
        main.easyeffects_mutation_lock = None
        lock = main._easyeffects_mutation_lock()
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

            def load_preset(self, preset_name, convolver_sample_rate_hz=None):
                pass

            def get_status(self):
                return {"status": "ok"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            convolver_task = asyncio.create_task(main.create_convolver_preset(
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
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            extras_task = asyncio.create_task(main.save_easyeffects_extras(FakeExtrasRequest()))
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, extras_task)
            self.assertEqual(order, ["upload-entered", "extras-entered"])

    async def test_threaded_upload_serializes_against_loudness_volume_mutation(self):
        entered = threading.Event()
        release = threading.Event()
        order = []

        class FakeManager:
            def load_global_extras(self):
                return {"loudness": {"enabled": True, "params": {}}}

            def loudness_db_from_percent(self, percent):
                return -float(percent)

            def set_loudness_volume_db(self, volume_db):
                order.append("loudness-entered")
                entered.set()
                release.wait(timeout=5)
                return {"extras": {"loudness": {"params": {"volumeDb": volume_db}}}}

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

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main, "easyeffects_manager", fake
        ), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"), mock.patch.object(
            main, "set_output_volume", return_value=100
        ):
            upload_task = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            volume_task = asyncio.create_task(main._set_canonical_output_volume(32))
            await asyncio.sleep(0.05)
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, volume_task)
            self.assertEqual(order, ["upload-entered", "loudness-entered"])


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

            def get_status(self):
                return {"status": "ok"}

        class FakeDeleteRequest:
            async def json(self):
                return {"preset_name": "Some Preset"}

        fake = FakeManager()
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            # Cancel the caller while the worker thread is still running.
            upload_task.cancel()
            await asyncio.sleep(0.05)

            delete_task = asyncio.create_task(main.delete_easyeffects_preset(FakeDeleteRequest()))
            await asyncio.sleep(0.05)
            # The cancelled caller must still own the mutation lock until the
            # worker actually finished: the delete must not enter yet.
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, delete_task, return_exceptions=True)

            self.assertTrue(upload_task.cancelled())
            self.assertEqual(order, ["upload-entered", "delete-entered"])

    async def test_preset_load_waits_for_threaded_mutation(self):
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
        with mock.patch.object(main, "_require_easyeffects_manager", return_value=fake), mock.patch.object(
            main.manager, "broadcast", mock.AsyncMock()
        ), mock.patch.object(main, "schedule_peak_monitor_refresh_after_effects_change"):
            upload_task = asyncio.create_task(main.upload_easyeffects_ir(FakeUploadFile()))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))

            load_task = asyncio.create_task(main.load_easyeffects_preset(FakeLoadRequest()))
            await asyncio.sleep(0.05)
            # load_preset must not enter the manager while the threaded
            # mutation is still running.
            self.assertEqual(order, ["upload-entered"])

            release.set()
            await asyncio.gather(upload_task, load_task)
            self.assertEqual(order, ["upload-entered", "load-entered"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
