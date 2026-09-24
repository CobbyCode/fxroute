#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime-level role-derived output mute masks (area measurement masking)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsp.runtime import DSPRuntime


class OutputMaskRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = []

    async def _runtime(self, outputs=8):
        runtime = DSPRuntime(None)
        if outputs is not None:
            runtime._config = SimpleNamespace(layout=[{"name": f"way_{index}"} for index in range(outputs)])

        async def control(command, *, reply):
            self.commands.append(command)
            return "ok\n"

        runtime._control = control
        return runtime

    async def test_area_mask_sets_and_clears_exactly_its_own_bits(self):
        runtime = await self._runtime()
        previous = await runtime.apply_output_mask(0b00001101)
        self.assertEqual(previous, 0)
        self.assertEqual(runtime._output_mask, 0b00001101)
        await runtime.clear_output_mask(0b00001101)
        self.assertEqual(runtime._output_mask, 0)
        self.assertEqual(self.commands, ["mute 13 1", "mute 13 0"])

    async def test_unrelated_mute_bits_survive_an_area_measurement(self):
        runtime = await self._runtime()
        # A legacy exact-sub mute owns other bits; masking only writes its own.
        runtime._output_mask = 0b1
        await runtime.apply_output_mask(0b1100)
        self.assertEqual(runtime._output_mask, 0b1101)
        await runtime.clear_output_mask(0b1100)
        self.assertEqual(runtime._output_mask, 0b1)

    async def test_mask_validation_rejects_zero_and_unexposed_outputs(self):
        runtime = await self._runtime(outputs=4)
        for mask in (0, -1, True, 1 << 32):
            with self.subTest(mask=mask), self.assertRaisesRegex(ValueError, "32-bit"):
                await runtime.apply_output_mask(mask)
        with self.assertRaisesRegex(ValueError, "does not expose"):
            await runtime.apply_output_mask(0b10000)
        with self.assertRaisesRegex(ValueError, "does not expose"):
            await runtime.clear_output_mask(0b10000)
        self.assertEqual(self.commands, [])
        self.assertEqual(runtime._output_mask, 0)

    async def test_mask_without_a_loaded_config_still_validates_the_value(self):
        runtime = await self._runtime(outputs=None)
        await runtime.apply_output_mask(1)
        self.assertEqual(self.commands, ["mute 1 1"])
        with self.assertRaisesRegex(ValueError, "32-bit"):
            await runtime.apply_output_mask(0)

    async def test_stop_resets_the_tracked_mask(self):
        runtime = await self._runtime()
        await runtime.apply_output_mask(0b11)
        await runtime.stop()
        self.assertEqual(runtime._output_mask, 0)


if __name__ == "__main__":
    unittest.main()
