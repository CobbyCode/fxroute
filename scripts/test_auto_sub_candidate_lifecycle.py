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

    def test_21_restore_verifier_accepts_snapshot_and_rejects_each_field_drift(self):
        snapshot = {
            "mode": "subwoofer-2.1",
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": -2.14,
                "sub_level_db": -3.0,
                "sub_polarity": "normal",
            },
        }
        self.assertTrue(autosub._auto_sub_21_verify_restored(copy.deepcopy(snapshot), snapshot))
        drifts = [
            ("mode", "subwoofer-2.2"),
            ("sub_alignment_ms", 0.0),
            ("sub_level_db", 9.0),
            ("sub_polarity", "invert"),
            ("crossover_frequency_hz", 120),
            ("main_highpass_enabled", False),
        ]
        for key, value in drifts:
            with self.subTest(key=key):
                drifted = copy.deepcopy(snapshot)
                if key == "mode":
                    drifted["mode"] = value
                else:
                    drifted["subwoofer"][key] = value
                self.assertFalse(autosub._auto_sub_21_verify_restored(drifted, snapshot))

    async def test_restore_21_reapplies_once_on_transient_mismatch(self):
        """A transient read-back mismatch re-applies the restore once."""
        snapshot = {
            "mode": "subwoofer-2.1",
            "subwoofer": {
                "crossover_frequency_hz": 80,
                "main_highpass_enabled": True,
                "sub_alignment_ms": -2.14,
                "sub_level_db": -3.0,
                "sub_polarity": "normal",
            },
        }
        calls = []

        async def fake_apply(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            calls.append((output_mode, copy.deepcopy(global_config), subwoofers_config, verify))
            return len(calls) == 2  # first read-back lags, second matches

        with patch.object(autosub.candidates, "_auto_sub_apply_candidate", side_effect=fake_apply):
            restored = await autosub._restore_auto_sub_original_config(snapshot)
        self.assertTrue(restored)
        self.assertEqual(len(calls), 2)
        mode, global_config, subwoofers_config, verify = calls[0]
        self.assertEqual(mode, "subwoofer-2.1")
        self.assertEqual(global_config, snapshot["subwoofer"])
        self.assertIsNone(subwoofers_config)
        # The verify lambda accepts the exact start state and rejects drift.
        self.assertTrue(verify(copy.deepcopy(snapshot)))
        drifted = copy.deepcopy(snapshot)
        drifted["subwoofer"]["sub_level_db"] = 6.0
        self.assertFalse(verify(drifted))

    async def test_restore_21_returns_false_after_two_failed_verifications(self):
        snapshot = {"mode": "subwoofer-2.1", "subwoofer": {"sub_alignment_ms": -2.14}}
        calls = []

        async def always_fail(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            calls.append(output_mode)
            return False

        with patch.object(autosub.candidates, "_auto_sub_apply_candidate", side_effect=always_fail):
            restored = await autosub._restore_auto_sub_original_config(snapshot)
        self.assertFalse(restored)
        self.assertEqual(len(calls), 2)

    async def test_restore_22_routes_both_subwoofers_and_verifies_full_state(self):
        snapshot = {
            "mode": "subwoofer-2.2",
            "crossover_frequency_hz": 80,
            "main_highpass_enabled": True,
            "subwoofers": {
                "sub1": {"level_db": -3.0, "alignment_ms": -2.14, "polarity": "normal"},
                "sub2": {"level_db": 0.4, "alignment_ms": 2.54, "polarity": "invert"},
            },
        }
        captured = {}

        async def fake_apply(*, output_mode, global_config, subwoofers_config, verify, load_overview=None):
            captured["mode"] = output_mode
            captured["subwoofers"] = copy.deepcopy(subwoofers_config)
            captured["verify"] = verify
            return True

        with patch.object(autosub.candidates, "_auto_sub_apply_candidate", side_effect=fake_apply):
            restored = await autosub._restore_auto_sub_original_config(snapshot)
        self.assertTrue(restored)
        self.assertEqual(captured["mode"], "subwoofer-2.2")
        # Both subs are restored at their original alignments and polarities.
        self.assertEqual(captured["subwoofers"]["sub1"]["alignment_ms"], -2.14)
        self.assertEqual(captured["subwoofers"]["sub2"]["alignment_ms"], 2.54)
        self.assertEqual(captured["subwoofers"]["sub1"]["polarity"], "normal")
        self.assertEqual(captured["subwoofers"]["sub2"]["polarity"], "invert")
        live = {"mode": "subwoofer-2.2", "subwoofers": copy.deepcopy(snapshot["subwoofers"])}
        self.assertTrue(captured["verify"](live))
        drifted = copy.deepcopy(live)
        drifted["subwoofers"]["sub1"]["level_db"] = 6.0
        self.assertFalse(captured["verify"](drifted))
        drifted["subwoofers"]["sub2"]["polarity"] = "normal"
        self.assertFalse(captured["verify"](drifted))


if __name__ == "__main__":
    unittest.main()
