#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""auto_gain decision/reason must describe the gain path actually taken.

The calculator used to own ``auto_gain.reason`` (``Diagnostic
recommendation calculated; no audio state changed``) and the runners never
overwrote it, so a committed gain still claimed that nothing changed while
``applied`` said ``True``. These cases pin the shared summary used by all
three runners (2.1, 2.2 Mono, 2.2 Stereo):

* step-1 acceptance, step-2 (correction) acceptance and a rejected
  correction that keeps step 1,
* a rejected trial restoring the original level,
* clamped recommendations (the +/-6 dB bound),
* side-wise acceptance for 2.2 Stereo,
* confirmation-gate restores for every gate action,
* applied metadata never keeps the calculator's "no audio state changed".
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.autosub.gain_trial import (  # noqa: E402
    _format_gain_deltas,
    _gain_outcome_summary,
    _gain_recommendation_clamped,
    _gain_restore_reason,
    _resolve_gain_trial_outcome,
)

ACCEPT = {"accepted": True, "reason": "score improved", "channels": {}}
REJECT = {"accepted": False, "reason": "no score gain", "channels": {}}


class FormatDeltaTests(unittest.TestCase):
    def test_equal_sides_read_as_one_value(self):
        self.assertEqual(_format_gain_deltas({"left": 2.0, "right": 2.0}), "+2.00 dB")

    def test_side_specific_deltas_name_both_sides(self):
        self.assertEqual(
            _format_gain_deltas({"left": -6.0, "right": 1.25}),
            "left -6.00 dB / right +1.25 dB",
        )

    def test_missing_side_defaults_to_zero(self):
        self.assertEqual(_format_gain_deltas({"left": -1.5}), "left -1.50 dB / right +0.00 dB")


class ClampDetectionTests(unittest.TestCase):
    def test_channel_clamp_marks_the_recommendation(self):
        auto_gain = {"channels": {"left": {"clamped": False}, "right": {"clamped": True}}}
        self.assertTrue(_gain_recommendation_clamped(auto_gain))

    def test_common_recommendation_clamp_marks_it(self):
        self.assertTrue(_gain_recommendation_clamped(
            {"recommendation": {"type": "common", "clamped": True}, "channels": {}}))

    def test_unclamped_recommendation_is_not_marked(self):
        auto_gain = {"recommendation": {"type": "common", "clamped": False},
                     "channels": {"left": {"clamped": False}}}
        self.assertFalse(_gain_recommendation_clamped(auto_gain))
        self.assertFalse(_gain_recommendation_clamped({}))
        self.assertFalse(_gain_recommendation_clamped(None))


class OutcomeSummaryTests(unittest.TestCase):
    def summary(self, decision, reason, **kwargs):
        kwargs.setdefault("deltas_db", {"left": 1.5, "right": 1.5})
        return _gain_outcome_summary(decision, reason, **kwargs)

    def test_step1_accept_reports_the_applied_delta(self):
        decision, reason = self.summary("accepted_step1", "score improved", applied=True)
        self.assertEqual(decision, "accepted_step1")
        self.assertEqual(reason, "Gain applied (step-1 accepted): +1.50 dB (score improved)")

    def test_step2_accept_reports_the_correction(self):
        decision, reason = self.summary(
            "accepted_step2", "correction improved the response", applied=True,
            deltas_db={"left": 2.5, "right": 2.5})
        self.assertEqual(decision, "accepted_step2")
        self.assertEqual(
            reason,
            "Gain applied (step-2 accepted): +2.50 dB (correction improved the response)")

    def test_correction_reject_keeps_step1_and_names_the_step(self):
        decision, reason = self.summary(
            "accepted_step1",
            "Step-1 retained; step-2 correction rejected (no score gain)",
            applied=True, deltas_db={"left": 2.0, "right": 2.0})
        self.assertEqual(decision, "accepted_step1")
        self.assertIn("Step-1 retained; step-2 correction rejected", reason)

    def test_rejected_trial_reports_the_restore(self):
        decision, reason = self.summary("restored", "no score gain", applied=False)
        self.assertEqual(decision, "restored")
        self.assertEqual(
            reason, "Gain trial rejected; original level restored (no score gain)")

    def test_zero_delta_trial_keeps_the_original_level(self):
        decision, reason = self.summary(
            "accepted_step1", "no score gain", applied=False, deltas_db={})
        self.assertEqual(decision, "accepted_step1")
        self.assertEqual(reason, "Gain trial kept the original level (no score gain)")

    def test_clamped_recommendation_is_named(self):
        _, reason = self.summary("accepted_step1", None, applied=True, clamped=True)
        self.assertIn("clamped to the +/-6 dB bound", reason)

    def test_partial_side_acceptance_names_the_kept_side(self):
        decision, reason = self.summary(
            "accepted_step1", None, applied=True,
            deltas_db={"left": -6.0, "right": -6.0},
            accepted_sides={"left": True, "right": False})
        self.assertEqual(decision, "accepted_step1")
        # The kept side must not be credited with the trial delta.
        self.assertEqual(
            reason,
            "Gain applied (step-1 accepted): left -6.00 dB / right +0.00 dB; "
            "right kept the original level")

    def test_full_side_acceptance_stays_compact(self):
        _, reason = self.summary(
            "accepted_step2", None, applied=True, deltas_db={"left": -6.0, "right": -6.0},
            accepted_sides={"left": True, "right": True})
        self.assertNotIn("kept the original level", reason)

    def test_applied_gain_never_claims_no_audio_state_changed(self):
        for decision, applied in (("accepted_step1", True),
                                   ("accepted_step2", True),
                                   ("restored", False)):
            with self.subTest(decision=decision, applied=applied):
                _, reason = self.summary(decision, "score improved", applied=applied)
                self.assertNotIn("no audio state changed", reason)

    def test_missing_reason_still_produces_a_readable_sentence(self):
        _, reason = self.summary("accepted_step1", None, applied=True)
        self.assertEqual(reason, "Gain applied (step-1 accepted): +1.50 dB")


class RestoreReasonTests(unittest.TestCase):
    def test_every_gate_action_has_a_truthful_reason(self):
        for action in ("alignment_reverted_balance_kept", "reverted_to_original",
                       "winner_alignment_original_kept"):
            with self.subTest(action=action):
                reason = _gain_restore_reason(action)
                self.assertIn("Confirmation gate", reason)
                self.assertIn("level", reason)
                self.assertNotIn("no audio state changed", reason)

    def test_unknown_gate_action_still_reports_a_restore(self):
        reason = _gain_restore_reason("something_new")
        self.assertIn("original level restored", reason)


class ResolverConsistencyTests(unittest.TestCase):
    """The runners must not keep their own copies of the decision logic."""

    def test_resolver_and_summary_agree_for_every_verdict_pair(self):
        cases = [
            (True, ACCEPT, None, "accepted_step1"),
            (True, ACCEPT, {"accepted": True, "reason": "ok", "channels": {}},
             "accepted_step2"),
            (True, ACCEPT, {"accepted": False, "reason": "worse", "channels": {}},
             "accepted_step1"),
            (True, ACCEPT,
             {"accepted": False, "reason": "worse", "channels": {}, "step1_retained": True},
             "accepted_step1"),
            (False, REJECT, None, "restored"),
            (False, REJECT, {"accepted": True, "reason": "ok", "channels": {}},
             "accepted_step2"),
        ]
        for step1_accepted, verdict, correction, expected in cases:
            with self.subTest(step1=step1_accepted, correction=correction):
                decision, reason = _resolve_gain_trial_outcome(
                    step1_accepted, verdict, correction)
                self.assertEqual(decision, expected)
                summary_decision, summary_reason = _gain_outcome_summary(
                    decision, reason, applied=decision.startswith("accepted"),
                    deltas_db={"left": 1.0, "right": 1.0})
                self.assertEqual(summary_decision, decision)
                self.assertTrue(summary_reason)
                self.assertNotIn("no audio state changed", summary_reason)

    def test_rejected_step2_correction_is_named_in_the_summary(self):
        decision, reason = _resolve_gain_trial_outcome(
            True, ACCEPT, {"accepted": False, "reason": "worse", "channels": {}})
        _, summary = _gain_outcome_summary(
            decision, reason, applied=True, deltas_db={"left": 1.0, "right": 1.0})
        self.assertIn("Step-1 retained; step-2 correction rejected", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
