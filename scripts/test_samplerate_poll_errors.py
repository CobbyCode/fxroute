#!/usr/bin/env python3
"""Samplerate poll loops: resilient on transient failures, loud on bugs.

Contracts (no production code changed here):

- a transient listing failure (OSError, e.g. wedged/missing pactl) keeps
  the existing bounded-timeout resilience: the poll keeps waiting and
  finally raises the established "did not become readable" RuntimeError;
- a programming error (TypeError, e.g. signature drift in the observation
  helpers) propagates immediately instead of stalling to a misleading
  "PipeWire unstable" timeout;
- list_sink_inputs maps a hung/unrunnable pactl to [] (existing contract)
  but propagates programming errors instead of masking them as "no inputs".
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.sink_inputs as sink_inputs
import playback.media_readiness as media_readiness


class SampleratePollErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_spotify_type_error_propagates_instead_of_timeout(self):
        with patch.object(
            media_readiness,
            "list_spotify_sink_inputs",
            side_effect=TypeError("bad entries"),
        ):
            with self.assertRaises(TypeError):
                await media_readiness.wait_for_spotify_sink_input_samplerate(
                    expected_rate=44100, timeout_ms=60
                )

    async def test_spotify_os_error_keeps_timeout_resilience(self):
        with patch.object(
            media_readiness,
            "list_spotify_sink_inputs",
            side_effect=OSError("pactl wedged"),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not become readable"):
                await media_readiness.wait_for_spotify_sink_input_samplerate(
                    expected_rate=44100, timeout_ms=60
                )

    async def test_qobuz_type_error_propagates_instead_of_timeout(self):
        with patch.object(
            media_readiness,
            "list_qobuz_sink_inputs",
            side_effect=TypeError("bad entries"),
        ):
            with self.assertRaises(TypeError):
                await media_readiness.wait_for_qobuz_sink_input_samplerate(
                    expected_rate=44100, timeout_ms=60
                )

    def test_list_sink_inputs_timeout_returns_empty(self):
        with patch(
            "audio.sink_inputs.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pactl", timeout=1.5),
        ):
            self.assertEqual(sink_inputs.list_sink_inputs(), [])

    def test_list_sink_inputs_programming_error_propagates(self):
        with patch(
            "audio.sink_inputs.subprocess.run",
            side_effect=ValueError("bad timeout"),
        ):
            with self.assertRaises(ValueError):
                sink_inputs.list_sink_inputs()


if __name__ == "__main__":
    unittest.main(verbosity=2)
