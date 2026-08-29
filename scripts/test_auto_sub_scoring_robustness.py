#!/usr/bin/env python3
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.store import (
    _auto_sub_deep_notch_penalty_db,
    auto_sub_chain_anchor_db,
    score_sub_alignment_candidates,
)
from measurement.autosub.candidates import (
    _auto_sub_coarse_winner_at_scan_edge,
    _auto_sub_fine_delay_candidates,
)
from measurement.autosub.measurement import (
    _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB,
    _auto_sub_gain_verdict,
    _auto_sub_local_dip_db,
    _auto_sub_local_dip_gate_sides,
)
from measurement.autosub.scoring import (
    _auto_sub_anchor_shifted_points,
    _auto_sub_display_anchor_reference_db,
    _auto_sub_select_polarity_shared_winner,
)

FC = 80
FREQS = [20.0, 25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 640.0]


def flat_points(level_db: float) -> list[list[float]]:
    return [[hz, level_db] for hz in FREQS]


def notch_points(level_db: float, notch_hz: float, depth_db: float) -> list[list[float]]:
    points = []
    for hz in FREQS:
        distance = abs(math.log2(hz / notch_hz)) if hz > 0 else 10.0
        dip = depth_db * math.exp(-(distance ** 2) / 0.05)
        points.append([hz, level_db - dip])
    return points


class DeepNotchRampTests(unittest.TestCase):
    def test_ramp_endpoints(self):
        self.assertEqual(_auto_sub_deep_notch_penalty_db(6.9), 0.0)
        self.assertEqual(_auto_sub_deep_notch_penalty_db(7.0), 0.0)
        self.assertEqual(_auto_sub_deep_notch_penalty_db(15.0), 0.5)
        self.assertEqual(_auto_sub_deep_notch_penalty_db(16.0), 0.5)

    def test_ramp_is_monotonic_without_threshold_jump(self):
        previous = 0.0
        dip = 7.0
        while dip <= 15.0:
            penalty = _auto_sub_deep_notch_penalty_db(dip)
            self.assertGreaterEqual(penalty, previous - 1e-12)
            previous = penalty
            dip += 0.25
        # The former 0.15 -> 0.30 step at 10 dB must be gone.
        self.assertLess(
            _auto_sub_deep_notch_penalty_db(10.01) / _auto_sub_deep_notch_penalty_db(9.99),
            1.5,
        )


class ChainAnchorTests(unittest.TestCase):
    def test_anchor_needs_main_only_points(self):
        self.assertIsNone(auto_sub_chain_anchor_db([[hz, 50.0] for hz in (20.0, 40.0, 80.0)]))
        self.assertEqual(auto_sub_chain_anchor_db(flat_points(50.0)), 50.0)

    def test_identical_shapes_score_equal_regardless_of_capture_level(self):
        hot = {"delay_ms": 0.0, "points": flat_points(51.0)}
        cold = {"delay_ms": -0.78, "points": flat_points(50.0)}
        scoring = score_sub_alignment_candidates([hot, cold], crossover_hz=FC)
        by_delay = {round(float(r["delay_ms"]), 2): r for r in scoring["results"]}
        self.assertAlmostEqual(by_delay[0.0]["score"], by_delay[-0.78]["score"], places=9)
        self.assertAlmostEqual(by_delay[0.0]["chain_anchor_deviation_db"], 0.5, places=6)
        self.assertAlmostEqual(by_delay[-0.78]["chain_anchor_deviation_db"], -0.5, places=6)

    def test_hot_reference_does_not_inflate_low_guard_loss(self):
        baseline = {"delay_ms": 0.0, "points": flat_points(51.0)}
        candidate = {"delay_ms": 0.78, "points": flat_points(50.0)}
        scoring = score_sub_alignment_candidates(
            [candidate, {"delay_ms": 1.56, "points": flat_points(50.0)}],
            crossover_hz=FC, low_guard_reference_points=flat_points(51.0),
        )
        # Baseline is 1 dB hot; after anchor normalization the candidate's
        # low-guard loss must be ~0 dB instead of 1 dB.
        by_delay = {round(float(r["delay_ms"]), 2): r for r in scoring["results"]}
        self.assertAlmostEqual(by_delay[0.78]["low_guard_loss_db"], 0.0, places=6)

    def test_hot_candidate_wins_on_shape_only(self):
        # A 1 dB hot capture must not beat an identical cold shape.
        hot = {"delay_ms": 0.0, "points": notch_points(51.0, 74.0, 12.0)}
        cold = {"delay_ms": -0.78, "points": notch_points(50.0, 74.0, 12.0)}
        scoring = score_sub_alignment_candidates([hot, cold], crossover_hz=FC)
        winner = scoring["winner"]
        self.assertIn(round(float(winner["delay_ms"]), 2), (0.0, -0.78))
        self.assertAlmostEqual(winner["score"], scoring["results"][1]["score"], places=9)


class EdgeScanTests(unittest.TestCase):
    SCAN = [-3.12, -2.34, -1.56, -0.78, 0.0, 0.78, 1.56, 2.34, 3.12]

    def test_edge_detection(self):
        self.assertEqual(_auto_sub_coarse_winner_at_scan_edge(-3.12, self.SCAN), "below")
        self.assertEqual(_auto_sub_coarse_winner_at_scan_edge(3.12, self.SCAN), "above")
        self.assertIsNone(_auto_sub_coarse_winner_at_scan_edge(-2.34, self.SCAN))
        self.assertIsNone(_auto_sub_coarse_winner_at_scan_edge(-2.34, []))

    def test_edge_winner_extends_fine_window_beyond_the_scan(self):
        winner = {"delay_ms": -3.12}
        delays = _auto_sub_fine_delay_candidates(winner, None, 0.78125, set(self.SCAN), scan_delays=self.SCAN)
        self.assertIn(-3.71, delays)
        self.assertIn(-3.90, delays)
        self.assertLessEqual(len(delays), 6)

    def test_interior_winner_keeps_half_step_limit(self):
        winner = {"delay_ms": -2.34}
        delays = _auto_sub_fine_delay_candidates(winner, None, 0.78125, set(self.SCAN), scan_delays=self.SCAN)
        self.assertTrue(delays)
        self.assertLessEqual(max(abs(delay + 2.34) for delay in delays), 0.78125 / 2 + 0.01)


class SharedSetPolarityTests(unittest.TestCase):
    def scored(self, rows: list[tuple[float, float]]) -> list[dict]:
        return [{"delay_ms": delay, "final_score": score} for delay, score in rows]

    def test_clear_margin_accepts_best_invert(self):
        decision = _auto_sub_select_polarity_shared_winner(
            self.scored([(0.0, 0.44), (1.0, 0.45), (2.0, 0.60), (3.0, 0.30)]),
        )
        self.assertTrue(decision["accepted"])
        self.assertEqual(decision["alternative_delay_ms"], 2.0)
        self.assertAlmostEqual(decision["score_gain"], 0.16, places=6)

    def test_small_margin_keeps_incumbent(self):
        decision = _auto_sub_select_polarity_shared_winner(
            self.scored([(0.0, 0.44), (1.0, 0.50), (2.0, 0.47)]),
        )
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["reason"], "incumbent_protected_unclear_advantage")

    def test_missing_incumbent_never_accepts(self):
        decision = _auto_sub_select_polarity_shared_winner(self.scored([(1.0, 0.9)]))
        self.assertFalse(decision["accepted"])


class GainVerdictAnchorTests(unittest.TestCase):
    def diagnostics(self, left_residual: float, left_anchor: float | None,
                    right_residual: float, right_anchor: float | None) -> dict:
        return {
            "gain_calculated": True,
            "channels": {
                "left": {"raw_recommendation_db": left_residual, "chain_anchor_db": left_anchor},
                "right": {"raw_recommendation_db": right_residual, "chain_anchor_db": right_anchor},
            },
        }

    def test_chain_excursion_no_longer_rejects_the_gain_step(self):
        before = self.diagnostics(-0.60, 51.20, 0.50, 51.20)
        after = self.diagnostics(-0.728, 52.06, 0.04, 51.30)
        verdict = _auto_sub_gain_verdict(before, after, "subwoofer-2.2-stereo")
        self.assertTrue(verdict["accepted"])
        self.assertAlmostEqual(
            verdict["channels"]["left"]["adjusted_after_absolute_residual_db"], 0.132, places=3,
        )
        self.assertAlmostEqual(verdict["channels"]["left"]["anchor_adjustment_db"], 0.86, places=3)

    def test_real_regression_is_still_rejected(self):
        before = self.diagnostics(-0.60, 51.20, 0.50, 51.20)
        after = self.diagnostics(-2.00, 51.20, 0.04, 51.20)
        verdict = _auto_sub_gain_verdict(before, after, "subwoofer-2.2-stereo")
        self.assertFalse(verdict["accepted"])
        self.assertTrue(verdict["channels"]["left"]["anchor_adjustment_db"] is not None)

    def test_without_anchors_the_raw_comparison_applies(self):
        before = self.diagnostics(-0.60, None, 0.50, None)
        after = self.diagnostics(-1.80, None, 0.04, None)
        verdict = _auto_sub_gain_verdict(before, after, "subwoofer-2.2-stereo")
        self.assertFalse(verdict["accepted"])
        self.assertIsNone(verdict["channels"]["left"]["anchor_adjustment_db"])


class DisplayAnchorTests(unittest.TestCase):
    def test_reference_is_median_of_valid_anchors(self):
        reference = _auto_sub_display_anchor_reference_db(
            [flat_points(51.2), flat_points(51.3), flat_points(52.2), flat_points(51.25)]
        )
        self.assertAlmostEqual(reference, 51.275, places=6)

    def test_hot_trace_shifted_down_and_cold_trace_untouched(self):
        reference = 51.2
        hot = _auto_sub_anchor_shifted_points(flat_points(52.2), reference)
        cold = _auto_sub_anchor_shifted_points(flat_points(51.2), reference)
        self.assertAlmostEqual(hot[0][1], 51.2, places=6)
        self.assertEqual(cold[0][1], 51.2)

    def test_shift_is_capped(self):
        shifted = _auto_sub_anchor_shifted_points(flat_points(54.2), 51.2)
        self.assertAlmostEqual(shifted[0][1], 52.7, places=6)

    def test_points_without_anchor_returned_unchanged(self):
        bass_only = [[hz, 50.0] for hz in (20.0, 40.0, 80.0)]
        self.assertIs(_auto_sub_anchor_shifted_points(bass_only, 51.2), bass_only)


class LocalDipGateTests(unittest.TestCase):
    GRID = [40.0 * (2.0 ** (index / 12.0)) for index in range(73)]  # 40..160 Hz, 1/12 oct

    def curve(self, level_fn) -> list[list[float]]:
        return [[hz, level_fn(hz)] for hz in self.GRID]

    def test_flat_curve_has_no_local_dip(self):
        self.assertAlmostEqual(_auto_sub_local_dip_db(self.curve(lambda hz: 50.0), 40.0, 160.0), 0.0, places=6)

    def test_local_notch_is_measured_against_its_surround(self):
        # A narrow 8 dB notch at 74 Hz on a 50 dB base.
        def level(hz):
            return 50.0 - 8.0 * math.exp(-((math.log2(hz / 74.0)) ** 2) / 0.004)
        dip = _auto_sub_local_dip_db(self.curve(level), 40.0, 160.0)
        self.assertGreater(dip, 2.5)

    def test_broadband_level_shift_and_balance_tilt_do_not_trip_the_metric(self):
        # A -4.7 dB sub level trim shifts everything down; a balance change
        # tilts smoothly. Neither may look like a local dip.
        def tilt(hz):
            return 50.0 - 4.7 - 2.0 * (math.log2(hz / 80.0))
        shifted = _auto_sub_local_dip_db(self.curve(lambda hz: 45.3), 40.0, 160.0)
        tilted = _auto_sub_local_dip_db(self.curve(tilt), 40.0, 160.0)
        self.assertAlmostEqual(shifted, 0.0, places=6)
        self.assertLess(abs(tilted), 1.0)

    def test_gate_sides_and_tolerance(self):
        before = {"left": 6.9, "right": 6.9}
        self.assertEqual(_auto_sub_local_dip_gate_sides(before, {"left": 9.0, "right": 10.08}, 2.5), ["right"])
        self.assertEqual(_auto_sub_local_dip_gate_sides(before, {"left": 9.0, "right": 9.0}, 2.5), [])
        self.assertEqual(
            _auto_sub_local_dip_gate_sides(before, {"left": None, "right": 12.0}, _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB),
            ["right"],
        )
        self.assertEqual(_auto_sub_local_dip_gate_sides(before, {"left": None, "right": None}, 2.5), [])

    def test_tolerance_matches_real_run_signature(self):
        # Real rejected run: Before 6.91 -> After 10.08 at the new 74 Hz notch.
        self.assertLess(10.08 - 6.91, _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB + 1.5)
        self.assertGreater(10.08, 6.91 + _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB)
        # Real accepted 2.2-mono outcome: Before 17.01 -> After 9.77.
        self.assertLess(9.77, 17.01 + _AUTO_SUB_LOCAL_DIP_TOLERANCE_DB)


class JobSnapshotPersistenceTests(unittest.TestCase):
    def test_snapshot_written_and_pruned(self):
        import os
        import tempfile
        from measurement.autosub import deps as autosub_deps
        from measurement.autosub.jobs import (
            _AUTO_SUB_SNAPSHOT_KEEP,
            _persist_auto_sub_job_snapshot,
        )

        with tempfile.TemporaryDirectory() as tmp:
            jobs_dir = Path(tmp) / "measurements"
            autosub_deps.configure_dependencies(autosub_deps.AutoSubDependencies(
                get_dsp_runtime=lambda: None,
                get_measurement_store=lambda: type("Store", (), {"jobs_dir": jobs_dir})(),
                get_measurement_session=lambda: None,
                get_dsp_manager=lambda: None,
            ))
            try:
                target_dir = jobs_dir / "autosub"
                for index in range(_AUTO_SUB_SNAPSHOT_KEEP + 3):
                    stale = target_dir / f"autosub-job-stale{index:03d}.json"
                    stale.parent.mkdir(parents=True, exist_ok=True)
                    stale.write_text("{}", encoding="utf-8")
                    os.utime(stale, (1000000 + index, 1000000 + index))
                job = {"status": "completed", "mode": "subwoofer-2.2-stereo", "result": {"winner": {}}}
                _persist_auto_sub_job_snapshot(job, "test-job-persist")
                persisted = target_dir / "autosub-job-test-job-persist.json"
                self.assertTrue(persisted.exists())
                payload = json.loads(persisted.read_text(encoding="utf-8"))
                self.assertEqual(payload["job_id"], "test-job-persist")
                self.assertEqual(payload["mode"], "subwoofer-2.2-stereo")
                remaining = list(target_dir.glob("autosub-job-*.json"))
                self.assertLessEqual(len(remaining), _AUTO_SUB_SNAPSHOT_KEEP)
            finally:
                autosub_deps._autosub_deps = None


if __name__ == "__main__":
    unittest.main()
