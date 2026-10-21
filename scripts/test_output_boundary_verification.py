#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Output commit verification rejects missing, additional and bypass links."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dsp.runtime import CommandResult, DSPRuntime, DSPRuntimeConfig


class OutputBoundaryVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_boundary_is_exact_even_when_program_ingress_is_absent(self):
        desired = [('fxroute_dsp:output_1', 'hw:playback_FL'),
                   ('fxroute_dsp:output_2', 'hw:playback_FR')]
        live = list(desired)
        async def run(args):
            self.assertEqual(args, ('pw-link', '-l'))
            return CommandResult(0, '\n'.join(f'{source} -> {target}' for source, target in live))
        runtime = DSPRuntime(None, command_runner=run)
        runtime._config = DSPRuntimeConfig('stereo', 'hw', 48000, ('playback_FL', 'playback_FR'), (),
                                            ((1, 'playback_FL'), (2, 'playback_FR')))
        self.assertTrue(await runtime.verify_output_links())
        for broken in (desired[:1], desired + [('fxroute_dsp:output_3', 'hw:playback_FL')],
                       desired + [('mpv:output_FL', 'hw:playback_FL')]):
            with self.subTest(links=broken):
                live[:] = broken
                self.assertFalse(await runtime.verify_output_links())


if __name__ == '__main__':
    unittest.main()
