"""Characterization tests for the shared AutoSub candidate apply lifecycle."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measurement.autosub as autosub


class AutoSubCandidateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_complete_22_subwoofer_verification_checks_alignment_level_and_polarity(self):
        expected = {
            "sub1": {"level_db": 3.111, "alignment_ms": -2.14, "polarity": "invert"},
            "sub2": {"level_db": 0.412, "alignment_ms": -2.54, "polarity": "normal"},
        }
        persisted = {
            "mode": "subwoofer-2.2-stereo",
            "subwoofers": {
                "sub1": {"level_db": 3.1, "alignment_ms": -2.14, "polarity": "invert"},
                "sub2": {"level_db": 0.4, "alignment_ms": -2.54, "polarity": "normal"},
            },
        }
        self.assertTrue(autosub._auto_sub_22_verify_subwoofers(
            persisted, expected, "subwoofer-2.2-stereo",
        ))

        mismatches = (
            ("sub1", "alignment_ms", 0.0),
            ("sub2", "level_db", 0.8),
            ("sub1", "polarity", "normal"),
        )
        for sub_key, field, value in mismatches:
            with self.subTest(sub=sub_key, field=field):
                changed = copy.deepcopy(persisted)
                changed["subwoofers"][sub_key][field] = value
                self.assertFalse(autosub._auto_sub_22_verify_subwoofers(
                    changed, expected, "subwoofer-2.2-stereo",
                ))

        self.assertFalse(autosub._auto_sub_22_verify_subwoofers(
            {"mode": "subwoofer-2.2-stereo"}, expected, "subwoofer-2.2-stereo",
        ))
        for wrong_mode in ("stereo", "subwoofer-2.2"):
            self.assertFalse(autosub._auto_sub_22_verify_subwoofers(
                {**persisted, "mode": wrong_mode}, expected, "subwoofer-2.2-stereo",
            ))

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

        with patch.object(autosub.candidates, "set_audio_output_mode", side_effect=apply), \
             patch.object(autosub.candidates, "_dsp_runtime", return_value=object()), \
             patch.object(autosub.candidates, "_auto_sub_sync_dsp_runtime", new=AsyncMock()) as sync, \
             patch.object(autosub.candidates, "get_audio_output_overview", return_value=live), \
             patch.object(autosub.candidates.asyncio, "sleep", side_effect=wait):
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
        with patch.object(autosub.candidates, "set_audio_output_mode", side_effect=RuntimeError("write failed")), \
             patch.object(autosub.candidates, "_dsp_runtime", return_value=AsyncMock()), \
             patch.object(autosub.candidates.logger, "exception"), \
             patch.object(autosub.candidates, "asyncio") as asyncio_mock:
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
