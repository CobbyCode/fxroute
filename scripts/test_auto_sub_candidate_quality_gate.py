#!/usr/bin/env python3
"""Regression tests for the AutoSub candidate plausibility gate and the
uncertain near-tie re-measurement.

The real-run fixture reproduces the 2026-08-31 2.2-stereo failure: the single
+1.76 ms candidate sweep was a capture artifact (normal 200-600 Hz chain
anchor, bass-band energy collapsed by ~20 dB) and, before the gate existed,
its extreme values defined both ends of the scorer's min-max normalization —
flipping the accepted right winner from +1.56 ms to +2.34 ms.
"""

import copy
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.store import score_sub_alignment_candidates
from measurement.autosub.scoring import (
    _AUTO_SUB_PLAUSIBILITY_EXCLUSION_REASON,
    _auto_sub_gate_candidate_rows,
    _auto_sub_needs_tiebreak,
    _auto_sub_remeasure_tiebreak,
)
import main

FIXTURE = ROOT / "scripts" / "fixtures" / "autosub-collapsed-candidate-replay.json"
FC = 80
FREQS = []
_value = 20.0
while _value < 1000.0:
    FREQS.append(round(_value, 3))
    _value *= 10 ** 0.0125


def load_fixture_rows():
    with open(FIXTURE) as fh:
        payload = json.load(fh)
    return copy.deepcopy(payload["candidates"])


def score(rows):
    scoring = score_sub_alignment_candidates([dict(row) for row in rows], crossover_hz=FC)
    return {
        "winner": scoring["winner"],
        "scores": {round(float(r["delay_ms"]), 2): r["final_score"] for r in scoring["results"]},
    }


def flat_points(level_db: float) -> list[list[float]]:
    return [[f, level_db] for f in FREQS]


def notched_points(level_db: float, notch_hz: float, depth_db: float, width: float = 0.05) -> list[list[float]]:
    points = []
    for f in FREQS:
        distance = abs(math.log2(f / notch_hz)) if f > 0 else 10.0
        points.append([f, round(level_db - depth_db * math.exp(-(distance ** 2) / width), 3)])
    return points


def row(delay_ms: float, points: list[list[float]], normalized_by: float = -100.0, **extra):
    return {
        "delay_ms": delay_ms,
        "name": str(delay_ms),
        "status": "completed",
        "points": points,
        "normalized_by_db": normalized_by,
        **extra,
    }


class CandidatePlausibilityGateTests(unittest.TestCase):
    def test_real_artifact_is_excluded_and_winner_restored(self):
        """The real run regression: one collapsed sweep must not decide the winner."""
        rows = load_fixture_rows()
        dirty = score(rows)
        self.assertEqual(round(float(dirty["winner"]["delay_ms"]), 2), 2.34)

        kept, exclusions = _auto_sub_gate_candidate_rows(copy.deepcopy(rows), FC, context="test")
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(round(float(exclusions[0]["delay_ms"]), 2), 1.76)
        self.assertEqual(exclusions[0]["side"], "main")
        self.assertLess(exclusions[0]["energy_deviation_db"], -10.0)
        self.assertGreater(exclusions[0]["energy_deviation_db"], -40.0)
        self.assertEqual([round(float(r["delay_ms"]), 2) for r in kept], [
            -3.12, -2.34, -1.56, -0.78, 0.0, 0.78, 1.17, 1.36, 1.56, 1.95, 2.34, 3.12,
        ])

        gated = score(kept)
        self.assertEqual(round(float(gated["winner"]["delay_ms"]), 2), 1.56)

    def test_gated_scoring_equals_scoring_without_the_artifact(self):
        """Gate removal must not change the scoring of the healthy candidates."""
        rows = load_fixture_rows()
        kept, _ = _auto_sub_gate_candidate_rows(copy.deepcopy(rows), FC, context="test")
        manual = [row for row in rows if round(float(row["delay_ms"]), 2) != 1.76]
        gated_scoring = score(copy.deepcopy(kept))
        manual_scoring = score(copy.deepcopy(manual))
        self.assertEqual(gated_scoring["scores"], manual_scoring["scores"])
        self.assertEqual(gated_scoring["winner"]["delay_ms"], manual_scoring["winner"]["delay_ms"])

    def test_excluded_row_is_marked_and_healthy_rows_stay_clean(self):
        rows = load_fixture_rows()
        kept, _ = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        marked = [row for row in rows if row.get("exclusion_reason")]
        self.assertEqual(len(marked), 1)
        self.assertEqual(round(float(marked[0]["delay_ms"]), 2), 1.76)
        self.assertEqual(marked[0]["exclusion_reason"], _AUTO_SUB_PLAUSIBILITY_EXCLUSION_REASON)
        self.assertEqual(marked[0]["plausibility"]["side"], "main")
        self.assertIn("energy_deviation_db", marked[0]["plausibility"])
        for row in kept:
            self.assertNotIn("exclusion_reason", row)

    def test_gate_stays_off_without_a_consistent_majority(self):
        # Two clusters of equal size: the set median falls between them, no
        # cluster is consistent-majority, so nothing can be excluded.
        rows = [
            row(0.0, flat_points(50.0)),
            row(0.78, flat_points(25.0)),
            row(1.56, flat_points(50.1)),
            row(2.34, flat_points(24.5)),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(kept), 4)
        self.assertEqual(exclusions, [])

    def test_gate_excludes_singleton_deviant_against_consistent_majority(self):
        # Band-integrated main+sub power is delay-invariant; with the anchor
        # equal across sweeps a +25 dB singleton is the implausible sweep,
        # whatever its direction.
        rows = [
            row(0.0, flat_points(50.0)),
            row(0.78, flat_points(25.0)),
            row(1.56, flat_points(24.5)),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(exclusions), 1)
        self.assertGreater(exclusions[0]["energy_deviation_db"], 6.0)
        self.assertEqual([round(float(r["delay_ms"]), 2) for r in kept], [0.78, 1.56])

    def test_gate_skips_rows_without_calibration_metadata(self):
        rows = [
            row(0.0, flat_points(50.0)),
            row(0.78, flat_points(50.1)),
            row(1.56, flat_points(25.0), normalized_by=None),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(kept), 3)
        self.assertEqual(exclusions, [])

    def test_gate_requires_a_normal_delay_neighbour(self):
        # The collapsed sweep sits far outside one scan step (0.78 ms at
        # fc=80) from every normal candidate: without a normal neighbour the
        # deviation could be a shared chain state, so nothing is excluded.
        rows = [
            row(0.0, flat_points(50.0)),
            row(5.0, flat_points(50.1)),
            row(20.0, flat_points(25.0)),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(kept), 3)
        self.assertEqual(exclusions, [])

    def test_gate_excludes_inflated_candidates_symmetrically(self):
        rows = [
            row(-0.78, flat_points(50.0)),
            row(0.0, flat_points(50.1)),
            row(0.78, flat_points(49.9)),
            row(1.56, flat_points(50.05)),
            row(2.34, flat_points(59.0)),  # +9 dB band power and anchor: chain-gain-inconsistent
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(exclusions), 1)
        self.assertGreater(exclusions[0]["energy_deviation_db"], 6.0)
        self.assertEqual([round(float(r["delay_ms"]), 2) for r in kept], [-0.78, 0.0, 0.78, 1.56])

    def test_chain_gain_excursion_within_anchor_correction_is_kept(self):
        # +2 dB band power from a chain-gain excursion with a matching +2 dB
        # anchor shift: the capped anchor correction absorbs it, so the sweep
        # stays in the scoring set exactly as the unchanged scorer expects.
        rows = [
            row(-0.78, flat_points(50.0)),
            row(0.0, flat_points(50.1)),
            row(0.78, flat_points(49.9)),
            row(1.56, flat_points(50.05)),
            row(2.34, flat_points(52.0), normalized_by=-102.0),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(exclusions, [])
        self.assertEqual(len(kept), 5)

    def test_gate_handles_dual_channel_rows(self):
        def dual(delay, left_level, right_level, nb=-100.0):
            return {
                "delay_ms": delay,
                "name": str(delay),
                "status": "completed",
                "points_left": flat_points(left_level),
                "points_right": flat_points(right_level),
                "normalized_by_db_left": nb,
                "normalized_by_db_right": nb,
            }

        rows = [
            dual(0.0, 50.0, 50.0),
            dual(0.78, 50.1, 49.9),
            dual(1.56, 25.0, 50.0),  # collapsed left side only
            dual(2.34, 50.05, 50.2),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]["side"], "left")
        self.assertEqual([round(float(r["delay_ms"]), 2) for r in kept], [0.0, 0.78, 2.34])

    def test_fc_narrow_deficit_diagnostic_is_reported(self):
        rows = [
            row(0.0, flat_points(50.0)),
            row(0.78, notched_points(50.0, 80.0, 8.0)),
        ]
        scoring = score_sub_alignment_candidates(copy.deepcopy(rows), crossover_hz=FC)
        by_delay = {round(float(r["delay_ms"]), 2): r for r in scoring["results"]}
        self.assertIn("fc_narrow_deficit_db", by_delay[0.0])
        self.assertIsNotNone(by_delay[0.78]["fc_narrow_deficit_db"])
        self.assertLessEqual(by_delay[0.78]["fc_narrow_deficit_db"], -3.0)
        self.assertLess(abs(by_delay[0.0]["fc_narrow_deficit_db"]), 0.5)

    def test_real_winner_rows_carry_negative_fc_narrow_deficit(self):
        rows = load_fixture_rows()
        kept, _ = _auto_sub_gate_candidate_rows(copy.deepcopy(rows), FC, context="test")
        scoring = score_sub_alignment_candidates(kept, crossover_hz=FC)
        by_delay = {round(float(r["delay_ms"]), 2): r for r in scoring["results"]}
        # The applied +2.34 ms candidate lost the most energy right around fc;
        # +1.56 ms keeps clearly more. Diagnostic only — no score effect.
        self.assertLess(by_delay[2.34]["fc_narrow_deficit_db"], -2.0)
        self.assertGreater(
            by_delay[1.56]["fc_narrow_deficit_db"],
            by_delay[2.34]["fc_narrow_deficit_db"],
        )


class ArrivalShiftGateTests(unittest.TestCase):
    def test_arrival_shift_excludes_degraded_sweep(self):
        # Healthy sweeps jitter by the capture quantum (1024 samples); the
        # degraded chain shifted every subsequent arrival by 25600 samples.
        rows = [
            row(0.0, flat_points(50.0), alignment_samples=78268),
            row(0.78, flat_points(50.1), alignment_samples=79292),
            row(1.56, flat_points(49.9), alignment_samples=78268),
            row(2.34, flat_points(50.05), alignment_samples=52668),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]["reason"], "implausible_arrival_shift")
        self.assertEqual(round(float(exclusions[0]["delay_ms"]), 2), 2.34)
        self.assertLess(exclusions[0]["arrival_shift_samples"], -10000)
        self.assertEqual([round(float(r["delay_ms"]), 2) for r in kept], [0.0, 0.78, 1.56])

    def test_arrival_quantum_jitter_is_kept(self):
        rows = [
            row(0.0, flat_points(50.0), alignment_samples=77244),
            row(0.78, flat_points(50.1), alignment_samples=78268),
            row(1.56, flat_points(49.9), alignment_samples=79292),
            row(2.34, flat_points(50.05), alignment_samples=77244),
        ]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(exclusions, [])
        self.assertEqual(len(kept), 4)

    def test_rows_without_alignment_data_skip_the_arrival_check(self):
        rows = [row(0.0, flat_points(50.0)), row(0.78, flat_points(50.1)), row(1.56, flat_points(49.9))]
        kept, exclusions = _auto_sub_gate_candidate_rows(rows, FC, context="test")
        self.assertEqual(exclusions, [])
        self.assertEqual(len(kept), 3)


class WinnerDelayTests(unittest.TestCase):
    def test_zero_and_negative_zero_winner_delays_are_kept(self):
        from measurement.autosub.candidates import _auto_sub_winner_delay_ms

        # A legitimate 0.00 ms winner must not fall back to the incumbent
        # delay: float(0.0) or fallback silently substituted the fallback and
        # desynced the scored winner from the applied configuration.
        self.assertEqual(_auto_sub_winner_delay_ms({"delay_ms": -0.0}, 2.34), 0.0)
        self.assertEqual(_auto_sub_winner_delay_ms({"delay_ms": 0.0}, 2.34), 0.0)
        self.assertEqual(_auto_sub_winner_delay_ms({"delay_ms": -3.32}, 2.34), -3.32)
        self.assertEqual(_auto_sub_winner_delay_ms({"delay_ms": 5.46}, 2.34), 5.46)

    def test_missing_winner_delay_falls_back(self):
        from measurement.autosub.candidates import _auto_sub_winner_delay_ms

        self.assertEqual(_auto_sub_winner_delay_ms({}, 2.34), 2.34)
        self.assertEqual(_auto_sub_winner_delay_ms(None, 2.34), 2.34)
        self.assertEqual(_auto_sub_winner_delay_ms({"delay_ms": None}, 2.34), 2.34)


class UncertainTiebreakTests(unittest.IsolatedAsyncioTestCase):
    def scoring_with(self, results, confidence):
        return {
            "confidence": confidence,
            "results": results,
            "runner_up": results[1] if len(results) >= 2 else None,
        }

    def result_row(self, delay, score):
        return {"delay_ms": delay, "score": score}

    async def test_uncertain_confidence_remeasures_top_two_and_decides_on_confirmed_data(self):
        rows = [
            row(0.0, notched_points(50.0, 80.0, 8.0)),
            row(1.0, notched_points(50.0, 80.0, 9.0)),
            row(2.0, notched_points(50.0, 80.0, 12.0)),
        ]
        scoring = self.scoring_with(
            [self.result_row(0.0, 0.501), self.result_row(1.0, 0.5005), self.result_row(2.0, 0.1)],
            "uncertain",
        )
        measured_delays = []

        async def measure(delay_ms, index):
            measured_delays.append((round(float(delay_ms), 2), index))
            if round(float(delay_ms), 2) == 0.0:
                return row(0.0, notched_points(50.0, 80.0, 14.0))  # confirmed worse
            return row(1.0, flat_points(50.0))  # confirmed better

        outcome = await _auto_sub_remeasure_tiebreak(
            scoring=scoring, rows=rows, measure=measure, crossover_hz=FC,
            low_guard_reference_delay_ms=0.0,
        )
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome["applied"])
        self.assertEqual(measured_delays, [(0.0, 0), (1.0, 1)])
        self.assertTrue(rows[0]["tiebreak_remeasured"])
        self.assertTrue(rows[1]["tiebreak_remeasured"])
        self.assertFalse(rows[2].get("tiebreak_remeasured", False))
        self.assertTrue(outcome["diagnostics"]["changed"])
        self.assertEqual(outcome["diagnostics"]["winner_before"], 0.0)
        self.assertEqual(outcome["diagnostics"]["winner_after"], 1.0)
        self.assertEqual(round(float(outcome["scoring"]["winner"]["delay_ms"]), 2), 1.0)
        self.assertEqual(len(outcome["measured_results"]), 2)

    async def test_close_confidence_does_not_remeasure(self):
        rows = [row(0.0, flat_points(50.0)), row(1.0, flat_points(49.9))]

        async def measure(delay_ms, index):  # pragma: no cover - must not run
            raise AssertionError("measure must not be called for close confidence")

        scoring = self.scoring_with(
            [self.result_row(0.0, 0.6), self.result_row(1.0, 0.55)], "close",
        )
        self.assertIsNone(await _auto_sub_remeasure_tiebreak(
            scoring=scoring, rows=rows, measure=measure, crossover_hz=FC,
        ))

    async def test_degraded_remeasures_are_rejected_and_original_decision_stands(self):
        rows = [
            row(0.0, notched_points(50.0, 80.0, 8.0), alignment_samples=78268),
            row(1.0, notched_points(50.0, 80.0, 9.0), alignment_samples=78268),
            row(2.0, notched_points(50.0, 80.0, 12.0), alignment_samples=79292),
        ]
        scoring = self.scoring_with(
            [self.result_row(0.0, 0.501), self.result_row(1.0, 0.5005), self.result_row(2.0, 0.1)],
            "uncertain",
        )

        async def measure(delay_ms, index):
            # Degraded chain state: collapsed bass and a constant arrival
            # shift, exactly like the real 799f3bd5d1ab tiebreak remeasures.
            return row(float(delay_ms), notched_points(38.0, 80.0, 9.0), alignment_samples=52668)

        outcome = await _auto_sub_remeasure_tiebreak(
            scoring=scoring, rows=rows, measure=measure, crossover_hz=FC,
            low_guard_reference_delay_ms=0.0,
        )
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome["applied"])
        self.assertIsNone(outcome["scoring"])
        self.assertEqual(len(outcome["diagnostics"]["measure_failures"]), 2)
        self.assertEqual(
            outcome["diagnostics"]["measure_failures"][0]["reason"],
            "remeasure_implausible",
        )
        for original in rows:
            self.assertNotIn("tiebreak_remeasured", original)
            self.assertNotIn("plausibility", original)

    async def test_partial_remeasure_failure_keeps_the_healthy_confirmation(self):
        rows = [
            row(0.0, notched_points(50.0, 80.0, 8.0), alignment_samples=78268),
            row(1.0, notched_points(50.0, 80.0, 9.0), alignment_samples=78268),
            row(2.0, notched_points(50.0, 80.0, 12.0), alignment_samples=79292),
        ]
        scoring = self.scoring_with(
            [self.result_row(0.0, 0.501), self.result_row(1.0, 0.5005)], "uncertain",
        )

        async def measure(delay_ms, index):
            if index == 0:
                return row(0.0, flat_points(50.0), alignment_samples=78268)  # confirmed healthy
            return row(1.0, flat_points(38.0), alignment_samples=52668)  # degraded

        outcome = await _auto_sub_remeasure_tiebreak(
            scoring=scoring, rows=rows, measure=measure, crossover_hz=FC,
            low_guard_reference_delay_ms=0.0,
        )
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome["applied"])
        self.assertEqual(len(outcome["measured_results"]), 1)
        self.assertEqual(len(outcome["diagnostics"]["measure_failures"]), 1)
        self.assertTrue(rows[0]["tiebreak_remeasured"])
        self.assertNotIn("tiebreak_remeasured", rows[1])

    async def test_failed_remeasures_keep_the_original_decision(self):
        rows = [row(0.0, flat_points(50.0)), row(1.0, flat_points(49.9))]
        scoring = self.scoring_with(
            [self.result_row(0.0, 0.501), self.result_row(1.0, 0.5005)], "uncertain",
        )

        async def measure(delay_ms, index):
            return {"delay_ms": delay_ms, "status": "failed", "error": "sweep failed", "points": []}

        outcome = await _auto_sub_remeasure_tiebreak(
            scoring=scoring, rows=rows, measure=measure, crossover_hz=FC,
        )
        self.assertIsNotNone(outcome)
        self.assertFalse(outcome["applied"])
        self.assertIsNone(outcome["scoring"])
        self.assertEqual(len(outcome["diagnostics"]["measure_failures"]), 2)
        for original in rows:
            self.assertNotIn("tiebreak_remeasured", original)

    def test_needs_tiebreak_only_for_uncertain_with_runner_up(self):
        self.assertTrue(_auto_sub_needs_tiebreak({
            "confidence": "uncertain",
            "results": [self.result_row(0.0, 0.5), self.result_row(1.0, 0.4999)],
            "runner_up": self.result_row(1.0, 0.4999),
        }))
        self.assertFalse(_auto_sub_needs_tiebreak({
            "confidence": "close",
            "results": [self.result_row(0.0, 0.5), self.result_row(1.0, 0.44)],
            "runner_up": self.result_row(1.0, 0.44),
        }))
        self.assertFalse(_auto_sub_needs_tiebreak({"confidence": "uncertain", "results": []}))


class LedgerExclusionTests(unittest.TestCase):
    def ledger(self, candidates, scoring, **kwargs):
        with patch_logger():
            return main_autosub_ledger(candidates, scoring, **kwargs)

    def test_marked_exclusion_is_reported_without_mutation(self):
        import copy

        from measurement.autosub.scoring import _auto_sub_candidate_ledger

        marked = {
            "delay_ms": 1.76,
            "name": "1.76",
            "status": "completed",
            "points": [[20.0, -10.0], [30.0, -10.0], [40.0, -10.0]],
            "exclusion_reason": "implausible_bass_energy",
            "plausibility": {"side": "main", "deviation_db": -20.4},
        }
        healthy = {
            "delay_ms": 1.56,
            "name": "1.56",
            "status": "completed",
            "points": [[20.0, -10.0], [30.0, -10.0], [40.0, -10.0]],
        }
        scoring = {"results": [{"delay_ms": 1.56, "score": 0.6, "final_score": 0.6}]}
        original = copy.deepcopy([marked, healthy])
        with patch_logger():
            rows = _auto_sub_candidate_ledger(
                [marked, healthy], scoring, mode="2.2_stereo", phase="right_fine", channel="right",
            )
        self.assertFalse(rows[0]["included_in_scoring"])
        self.assertTrue(rows[0]["eligible_for_scoring"])
        self.assertEqual(rows[0]["exclusion_reason"], "implausible_bass_energy")
        self.assertEqual(rows[0]["plausibility"], {"side": "main", "deviation_db": -20.4})
        self.assertTrue(rows[1]["included_in_scoring"])
        self.assertIsNone(rows[1]["exclusion_reason"])
        self.assertEqual([marked, healthy], original)


def patch_logger():
    from unittest.mock import patch

    return patch.object(main.logger, "info")


def main_autosub_ledger(*args, **kwargs):
    import measurement.autosub as autosub

    return autosub._auto_sub_candidate_ledger(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
