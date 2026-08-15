"""Characterization tests for the shared AutoSub candidate apply lifecycle."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measurement.autosub as autosub


class AutoSubCandidateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_sync_wait_verify_sequence_uses_persisted_overview(self):
        events = []
        persisted = {"mode": "subwoofer-2.2"}
        live = {"mode": "subwoofer-2.2", "subwoofers": {"sub1": {"alignment_ms": 8.0}}}
        def apply(*args):
            events.append(("apply", args))
            return persisted

        def verify(overview):
            events.append(("verify", overview))
            return overview is live

        async def wait(delay):
            events.append(("sleep", delay))

        with patch.object(autosub, "set_audio_output_mode", side_effect=apply), \
             patch.object(autosub, "_dsp_runtime", return_value=object()), \
             patch.object(autosub, "_auto_sub_sync_dsp_runtime", new=AsyncMock()) as sync, \
             patch.object(autosub, "get_audio_output_overview", return_value=live), \
             patch.object(autosub.asyncio, "sleep", side_effect=wait):
            result = await autosub._auto_sub_apply_candidate(
                output_mode="subwoofer-2.2",
                global_config={"crossover_frequency_hz": 80},
                subwoofers_config={"sub1": {"alignment_ms": 8.0}},
                verify=lambda overview: verify(overview),
            )

        self.assertTrue(result)
        self.assertEqual([event[0] for event in events], ["apply", "sleep", "verify"])
        self.assertIs(events[2][1], live)
        sync.assert_awaited_once_with(
            output_mode="subwoofer-2.2", persisted_overview=persisted,
        )

    async def test_apply_failure_returns_false_without_claiming_verification(self):
        verify = AsyncMock()
        with patch.object(autosub, "set_audio_output_mode", side_effect=RuntimeError("write failed")), \
             patch.object(autosub, "_dsp_runtime", return_value=AsyncMock()), \
             patch.object(autosub.logger, "exception"), \
             patch.object(autosub, "asyncio") as asyncio_mock:
            result = await autosub._auto_sub_apply_candidate(
                output_mode="subwoofer-2.1",
                global_config={},
                subwoofers_config=None,
                verify=verify,
            )

        self.assertFalse(result)
        verify.assert_not_awaited()
        asyncio_mock.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
