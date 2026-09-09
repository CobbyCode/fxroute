#!/usr/bin/env python3
"""Preset-load rollback: a failed load must restore the previous valid state.

Observable contracts of the real dsp.preset_loading._load_preset_locked (no production
code changed here):

- when loading the new preset fails, the previously committed preset is
  reloaded, the runtime is re-synced with the rollback reason, and the
  original error propagates (no half-applied preset stays committed);
- when the new preset half-applies (visible new active preset) before the
  failure, the half-applied state is reverted to the captured start state;
- when even the reload fails, the committed preset marker still falls back
  to the start preset and the original error still propagates.

Only visible state is pinned (committed active preset, runtime sync
reasons, propagated error), never internal helper calls. The existing
suites cover the endpoint error mapping and route-level serialization
with a faked loader; the loader's own recovery path had no coverage.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from dsp import preset_loading


class _PresetManagerDouble:
    """DSP manager double with an observable committed preset."""

    def __init__(
        self,
        *,
        active: str = "Start",
        fail_on: tuple[str, ...] = (),
        half_apply_on: tuple[str, ...] = (),
    ):
        self.active_preset = active
        self._fail_on = set(fail_on)
        self._half_apply_on = set(half_apply_on)
        self.load_calls: list[str] = []

    def load_global_extras(self):
        return {"loudness": {"enabled": False, "params": {}}}

    def get_active_preset(self):
        return self.active_preset

    def load_preset(self, preset_name, convolver_sample_rate_hz=None):
        self.load_calls.append(preset_name)
        if preset_name in self._half_apply_on:
            # The engine commits the new preset before the failure surfaces.
            self.active_preset = preset_name
        if preset_name in self._fail_on:
            raise RuntimeError(f"load failed: {preset_name}")
        self.active_preset = preset_name


class PresetLoadRollbackTests(unittest.IsolatedAsyncioTestCase):
    def _patched_world(self, manager):
        return (
            patch.object(main, "dsp_manager", manager),
            patch.object(main, "get_output_volume", return_value=50),
            patch.object(main.runtime, "dsp_runtime", None),
            patch.object(
                main.dsp_orchestrator, "sync_runtime", new=AsyncMock()
            ),
        )

    async def test_failed_load_keeps_previous_preset_and_reraises(self):
        manager = _PresetManagerDouble(active="Start", fail_on=("New",))
        dsp_patch, volume_patch, runtime_patch, sync_patch = self._patched_world(
            manager
        )
        with dsp_patch, volume_patch, runtime_patch, sync_patch as sync:
            with self.assertRaisesRegex(RuntimeError, "load failed: New"):
                await preset_loading._load_preset_locked("New")
        self.assertEqual(
            manager.load_calls,
            ["New"],
            "nothing was committed, so no previous-preset reload is needed",
        )
        self.assertEqual(manager.get_active_preset(), "Start")
        self.assertEqual(
            [call.kwargs.get("reason") for call in sync.await_args_list],
            ["native-dsp-preset-load-rollback"],
            "no successful-load sync may run on the failed state",
        )

    async def test_half_applied_preset_is_reverted(self):
        manager = _PresetManagerDouble(
            active="Start", fail_on=("New",), half_apply_on=("New",)
        )
        dsp_patch, volume_patch, runtime_patch, sync_patch = self._patched_world(
            manager
        )
        with dsp_patch, volume_patch, runtime_patch, sync_patch as sync:
            with self.assertRaisesRegex(RuntimeError, "load failed: New"):
                await preset_loading._load_preset_locked("New")
        self.assertEqual(
            manager.load_calls,
            ["New", "Start"],
            "the visibly half-applied preset must be reloaded with the start preset",
        )
        self.assertEqual(manager.get_active_preset(), "Start")
        self.assertIn(
            "native-dsp-preset-load-rollback",
            [call.kwargs.get("reason") for call in sync.await_args_list],
        )

    async def test_failed_reload_still_falls_back_and_reraises(self):
        manager = _PresetManagerDouble(
            active="Start", fail_on=("New", "Start"), half_apply_on=("New",)
        )
        dsp_patch, volume_patch, runtime_patch, sync_patch = self._patched_world(
            manager
        )
        with dsp_patch, volume_patch, runtime_patch, sync_patch as sync:
            with self.assertRaisesRegex(RuntimeError, "load failed"):
                await preset_loading._load_preset_locked("New")
        # Both the attempt and the reload failed: the committed marker must
        # still fall back to the captured start preset, and the runtime is
        # re-synced before the original error propagates.
        self.assertEqual(manager.active_preset, "Start")
        self.assertIn(
            "native-dsp-preset-load-rollback",
            [call.kwargs.get("reason") for call in sync.await_args_list],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
