#!/usr/bin/env python3
"""Preset-load lock order: canonical volume lock before DSP mutation lock.

Observable contracts of the real main._load_dsp_preset (no production code
changed here):

- the loader acquires the canonical volume write lock first and only then
  the DSP mutation lock. Any path taking them in the opposite order (e.g.
  the extras fallback, which holds canonical while re-acquiring mutation)
  would ABBA-deadlock against a swapped loader;
- a canonical volume write racing a preset load waits for the whole load
  (including the runtime sync) and is then applied exactly once: no
  interleaved intermediate state, no lost change, no leftover locked state.

Lock states and completion order are pinned, never call counts for their
own sake. The existing suites pin route-level serialization with a faked
loader; the loader's own acquisition order had no coverage.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class _PresetManagerDouble:
    def __init__(self):
        self.active_preset = "Start"
        self.load_calls: list[str] = []
        self.gate: threading.Event | None = None

    def load_global_extras(self):
        return {"loudness": {"enabled": False, "params": {}}}

    def get_active_preset(self):
        return self.active_preset

    def load_preset(self, preset_name, convolver_sample_rate_hz=None):
        self.load_calls.append(preset_name)
        if self.gate is not None:
            self.gate.wait(timeout=5)
        self.active_preset = preset_name


async def _wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.005)
    return False


class PresetLoadLockOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        main.runtime.dsp_mutation_lock = None
        main.runtime.canonical_volume_write_lock = None

    async def asyncTearDown(self):
        main.runtime.dsp_mutation_lock = None
        main.runtime.canonical_volume_write_lock = None
        main.dsp_manager = None

    def _patched_world(self, manager, set_master):
        return (
            patch.object(main, "dsp_manager", manager),
            patch.object(main, "get_output_volume", return_value=50),
            patch.object(main, "set_output_volume", new=set_master),
            patch.object(main.runtime, "dsp_runtime", None),
            patch.object(
                main.dsp_orchestrator, "sync_runtime", new=AsyncMock()
            ),
        )

    async def test_loader_holds_canonical_while_waiting_for_mutation(self):
        manager = _PresetManagerDouble()
        set_master = Mock(return_value=50)
        patches = self._patched_world(manager, set_master)
        mutation_lock = main._dsp_mutation_lock()
        await mutation_lock.acquire()
        try:
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                loader = asyncio.create_task(main._load_dsp_preset("New"))
                try:
                    held_first = await _wait_until(
                        lambda: main._canonical_volume_write_lock().locked()
                    )
                    self.assertTrue(
                        held_first,
                        "the loader must take the canonical lock before the mutation lock",
                    )
                    self.assertFalse(
                        loader.done(),
                        "the loader must wait for the mutation lock instead of proceeding",
                    )
                finally:
                    mutation_lock.release()
                await asyncio.wait_for(loader, timeout=5.0)
        finally:
            if mutation_lock.locked():
                mutation_lock.release()
        self.assertFalse(main._canonical_volume_write_lock().locked())
        self.assertFalse(main._dsp_mutation_lock().locked())
        self.assertEqual(manager.get_active_preset(), "New")

    async def test_concurrent_volume_write_waits_and_is_not_lost(self):
        manager = _PresetManagerDouble()
        gate = threading.Event()
        manager.gate = gate
        order: list[str] = []

        def record_volume(value):
            order.append("volume-applied")
            return 32

        set_master = Mock(side_effect=record_volume)
        orig_load = manager.load_preset

        def gated_load(preset_name, convolver_sample_rate_hz=None):
            order.append("load-entered")
            return orig_load(preset_name, convolver_sample_rate_hz)

        manager.load_preset = gated_load
        patches = self._patched_world(manager, set_master)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            loader = asyncio.create_task(main._load_dsp_preset("New"))
            try:
                entered = await _wait_until(lambda: "load-entered" in order)
                self.assertTrue(entered, "the load must reach the worker first")
                volume = asyncio.create_task(
                    main._set_canonical_output_volume(32)
                )
                await asyncio.sleep(0.1)
                self.assertFalse(
                    volume.done(),
                    "the volume write must wait while the load holds the canonical lock",
                )
                self.assertEqual(set_master.call_count, 0)
                gate.set()
                volume_result = await asyncio.wait_for(volume, timeout=5.0)
                await asyncio.wait_for(loader, timeout=5.0)
            finally:
                gate.set()
        self.assertEqual(volume_result, {"volume": 32})
        self.assertEqual(set_master.call_count, 1, "no lost or duplicated change")
        self.assertEqual(order[0], "load-entered")
        self.assertEqual(order[-1], "volume-applied")
        self.assertFalse(main._canonical_volume_write_lock().locked())
        self.assertFalse(main._dsp_mutation_lock().locked())


if __name__ == "__main__":
    unittest.main(verbosity=2)
