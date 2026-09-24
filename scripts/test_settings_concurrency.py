#!/usr/bin/env python3
"""Regression tests for serialized settings persistence and source-mode rollback."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measurement.file_store as file_store_module
import measurement.session as measurement_session
from common.atomic_write import atomic_write_text
from measurement.file_store import MeasurementFileStore


class _JsonRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class SourceModeConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_change_rolls_back_before_a_concurrent_change(self):
        import main

        old_lock = main.runtime.source_transition_lock
        main.runtime.source_transition_lock = asyncio.Lock()
        state = {"mode": "app-playback", "selected_input_key": None}
        first_external_attempt = True
        first_sync_started = asyncio.Event()
        release_first_sync = asyncio.Event()
        sync_order = []

        def set_source(mode, input_key=None):
            state["mode"] = mode
            state["selected_input_key"] = input_key
            return dict(state)

        async def external_sync(overview):
            nonlocal first_external_attempt
            mode = overview.get("mode")
            sync_order.append(mode)
            if mode == "external-input" and first_external_attempt:
                first_external_attempt = False
                first_sync_started.set()
                await release_first_sync.wait()
                raise RuntimeError("external sync failed")
            return overview

        async def bluetooth_sync(overview):
            sync_order.append(f"bluetooth:{overview.get('mode')}")
            return overview

        async def no_pause():
            return None

        async def no_peak_sync(_overview):
            return None

        try:
            with (
                patch.object(main, "set_audio_source_selection", side_effect=set_source),
                patch.object(main.samplerate, "_load_audio_source_selection", side_effect=lambda: dict(state)),
                patch.object(main, "external_input", SimpleNamespace(sync=external_sync)),
                patch.object(main, "bluetooth_input", SimpleNamespace(sync=bluetooth_sync)),
                patch.object(main, "_pause_all_app_playback_for_external_input", no_pause),
                patch.object(main.peak_monitor_coordinator, "sync_source_mode_state", no_peak_sync),
            ):
                first = asyncio.create_task(
                    main.save_audio_source_selection_route(
                        _JsonRequest({"mode": "external-input", "inputKey": "first"})
                    )
                )
                await asyncio.wait_for(first_sync_started.wait(), timeout=1)

                second = asyncio.create_task(
                    main.save_audio_source_selection_route(
                        _JsonRequest({"mode": "external-input", "inputKey": "second"})
                    )
                )
                # A second request may complete before the first is released
                # without the lock; the final state catches that stale rollback.
                await asyncio.sleep(0)
                release_first_sync.set()

                with self.assertRaises(main.HTTPException):
                    await first
                await asyncio.wait_for(second, timeout=1)

                self.assertEqual(state, {"mode": "external-input", "selected_input_key": "second"})
                self.assertEqual(
                    sync_order,
                    [
                        "external-input",
                        "app-playback",
                        "bluetooth:app-playback",
                        "external-input",
                        "bluetooth:external-input",
                    ],
                )
        finally:
            main.runtime.source_transition_lock = old_lock


class MeasurementSettingsConcurrencyTests(unittest.TestCase):
    def test_measurement_and_calibration_updates_do_not_lose_each_other(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeasurementFileStore(Path(root), has_active_job=lambda: False)
            calibration_meta = store._store_calibration_file("calibration.csv", b"20,0\n")
            first_write_started = threading.Event()
            release_first_write = threading.Event()
            write_count = 0
            write_lock = threading.Lock()
            real_atomic_write = atomic_write_text

            def controlled_atomic_write(path, text):
                nonlocal write_count
                payload = json.loads(text)
                measure = payload.get("measure", {})
                with write_lock:
                    write_count += 1
                    current_write = write_count
                if current_write == 1 and "selectedInputId" in measure:
                    first_write_started.set()
                    if not release_first_write.wait(2):
                        raise AssertionError("second settings update did not wait for the first")
                real_atomic_write(path, text)

            errors = []

            def run_measurement_update():
                try:
                    with patch.object(measurement_session, "_measurement_services", return_value=SimpleNamespace(get_store=lambda: store)):
                        measurement_session._update_measurement_setup_settings({"selectedInputId": "mic-a"})
                except BaseException as exc:  # capture thread failures for assertion
                    errors.append(exc)

            def run_calibration_update():
                try:
                    store.set_active_calibration_file_id(calibration_meta["id"])
                except BaseException as exc:
                    errors.append(exc)

            try:
                with patch.object(file_store_module, "atomic_write_text", controlled_atomic_write, create=True):
                    first = threading.Thread(target=run_measurement_update)
                    first.start()
                    self.assertTrue(first_write_started.wait(2), "measurement update did not reach atomic persistence")

                    second = threading.Thread(target=run_calibration_update)
                    second.start()
                    release_first_write.set()
                    first.join(2)
                    second.join(2)
                    self.assertFalse(first.is_alive())
                    self.assertFalse(second.is_alive())
            finally:
                release_first_write.set()

            self.assertEqual(errors, [])
            settings = json.loads(store.settings_path.read_text(encoding="utf-8"))
            self.assertEqual(settings["measure"]["selectedInputId"], "mic-a")
            self.assertEqual(settings["measure"]["activeCalibrationFileId"], calibration_meta["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
