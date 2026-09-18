"""Characterization tests for the shared AutoSub candidate apply lifecycle.

Backend-v2 migration: candidate apply and original-config restore are
service-only (staging through the job's owner). Legacy persist/sync/verify
helpers are deleted; the 22 slot verification math is unchanged.
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import measurement.autosub.candidates as autosub_candidates


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
        self.assertTrue(autosub_candidates._auto_sub_22_verify_subwoofers(
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
                self.assertFalse(autosub_candidates._auto_sub_22_verify_subwoofers(
                    changed, expected, "subwoofer-2.2-stereo",
                ))

        self.assertFalse(autosub_candidates._auto_sub_22_verify_subwoofers(
            {"mode": "subwoofer-2.2-stereo"}, expected, "subwoofer-2.2-stereo",
        ))
        for wrong_mode in ("stereo", "subwoofer-2.2"):
            self.assertFalse(autosub_candidates._auto_sub_22_verify_subwoofers(
                {**persisted, "mode": wrong_mode}, expected, "subwoofer-2.2-stereo",
            ))

    async def test_apply_without_service_job_raises(self):
        with self.assertRaises(RuntimeError):
            await autosub_candidates._auto_sub_apply_candidate(
                global_config={"crossover_frequency_hz": 80},
                subwoofers_config=None,
                job={"id": "legacy", "status": "running"},
            )
        with self.assertRaises(RuntimeError):
            await autosub_candidates._auto_sub_apply_candidate(
                global_config={},
                subwoofers_config=None,
            )

    async def test_restore_without_service_job_raises(self):
        with self.assertRaises(RuntimeError):
            await autosub_candidates._restore_original_config_or_fail_job(
                {"id": "legacy", "status": "running"},
                {"mode": "subwoofer-2.1"},
                "2.1 AutoSub failed",
            )


if __name__ == "__main__":
    unittest.main()
