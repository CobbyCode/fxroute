#!/usr/bin/env python3

"""Samplerate status readback logs INFO only when the state actually changes."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class SamplerateStatusChangeLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The gate is process-global; each test starts with an unset sentinel.
        main._last_logged_samplerate_signature = None

    @staticmethod
    def _status(rate, state):
        return {"active_rate": rate, "relevant_sink": {"state": state}}

    @staticmethod
    def _entry_count(logs):
        return sum(1 for line in logs.output if "audio_samplerate_status entry" in line)

    async def test_identical_readback_polls_log_only_once(self):
        status = self._status(48000, "RUNNING")
        with mock.patch.object(main, "get_samplerate_status", return_value=status), mock.patch.object(
            main.playback_state, "current_playback_owner", "spotify"
        ):
            with self.assertLogs("main", level="INFO") as logs:
                for _ in range(10):
                    await main.audio_samplerate_status()
            self.assertEqual(self._entry_count(logs), 1)

    async def test_owner_change_logs_then_new_steady_state_stays_silent(self):
        status = self._status(48000, "RUNNING")
        with mock.patch.object(main, "get_samplerate_status", return_value=status):
            with self.assertLogs("main", level="INFO") as logs:
                with mock.patch.object(main.playback_state, "current_playback_owner", "spotify"):
                    await main.audio_samplerate_status()
                with mock.patch.object(main.playback_state, "current_playback_owner", None):
                    await main.audio_samplerate_status()
                    await main.audio_samplerate_status()
            self.assertEqual(self._entry_count(logs), 2)

    async def test_rate_change_logs_and_return_value_is_unchanged_readback(self):
        statuses = iter(
            (self._status(48000, "RUNNING"), self._status(44100, "RUNNING"), self._status(44100, "RUNNING"))
        )
        with mock.patch.object(
            main, "get_samplerate_status", side_effect=lambda: next(statuses)
        ), mock.patch.object(main.playback_state, "current_playback_owner", "spotify"):
            with self.assertLogs("main", level="INFO") as logs:
                first = await main.audio_samplerate_status()
                await main.audio_samplerate_status()
                await main.audio_samplerate_status()
            self.assertEqual(first, {"active_rate": 48000, "relevant_sink": {"state": "RUNNING"}})
            self.assertEqual(self._entry_count(logs), 2)
            self.assertTrue(any("active_rate=48000" in line for line in logs.output))
            self.assertTrue(any("active_rate=44100" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
