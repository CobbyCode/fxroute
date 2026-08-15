#!/usr/bin/env python3
"""Focused capture retry and fallback policy checks."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement.capture_policy import MeasurementCapturePolicyRunner


class MeasurementCapturePolicyTests(unittest.TestCase):
    def _runner(self, capture_attempt, *, warning_codes=None, raise_mic=None, cancelled=None):
        warning_codes = warning_codes or set()
        cancelled = cancelled or set()
        return MeasurementCapturePolicyRunner(
            capture_attempt=capture_attempt,
            is_cancelled=lambda job_id, _exc=None: job_id in cancelled,
            evaluate_electrical_reference=lambda _analysis: {"usable": False, "warning": "ER rejected"},
            should_keep_electrical_reference=lambda _analysis, _scope: False,
            mark_electrical_reference_usable=lambda _analysis, warning: None,
            append_reference_fallback_warning=lambda _analysis, _warning: None,
            analysis_has_warning=lambda analysis, code: code in warning_codes,
            try_raise_mic=raise_mic or (lambda _analysis, **_kwargs: False),
            cancel_aware_sleep=lambda _job_id, _delay: None,
            retry_sleep=lambda _delay: None,
            should_retry_host_capture=lambda _exc: True,
            max_attempts=3,
            retry_delay=0.0,
        )

    def test_er_quality_rejection_uses_host_fallback_with_one_attempt(self):
        references = []

        def capture_attempt(*, reference_capture, capture_channels, electrical_reference_channel_index):
            references.append((reference_capture, capture_channels, electrical_reference_channel_index))
            return ({"quality": "ok"}, {"capture": True}, {"playback": True})

        result = self._runner(capture_attempt).run(
            job_id="job-1",
            owner_job_id="job-1",
            use_electrical_reference=True,
            electrical_reference={"kind": "electrical"},
            host_reference={"kind": "host"},
            capture_channels=3,
            electrical_reference_channel_index=2,
            mic_target="mic-1",
            measurement_scope="active_chain",
        )

        self.assertEqual(references, [
            ({"kind": "electrical"}, 3, 2),
            ({"kind": "host"}, 2, None),
        ])
        self.assertEqual(result.attempts_used, 1)

    def test_er_exception_fallback_preserves_attempt_count(self):
        references = []

        def capture_attempt(*, reference_capture, **_kwargs):
            references.append(reference_capture)
            if reference_capture["kind"] == "electrical":
                raise RuntimeError("ER unavailable")
            return ({"quality": "ok"}, {"capture": True}, {"playback": True})

        result = self._runner(capture_attempt).run(
            job_id="job-1",
            owner_job_id="job-1",
            use_electrical_reference=True,
            electrical_reference={"kind": "electrical"},
            host_reference={"kind": "host"},
            capture_channels=3,
            electrical_reference_channel_index=2,
            mic_target="mic-1",
            measurement_scope="active_chain",
        )

        self.assertEqual(references, [{"kind": "electrical"}, {"kind": "host"}])
        self.assertEqual(result.attempts_used, 1)

    def test_low_capture_retries_once_after_mic_gain_change(self):
        attempts = []

        def capture_attempt(**_kwargs):
            attempts.append(True)
            return ({"quality": "ok"}, {"capture": True}, {"playback": True})

        def raise_mic(_analysis, **kwargs):
            return kwargs["attempt_index"] == 0

        result = self._runner(
            capture_attempt,
            warning_codes={"capture-level-low"},
            raise_mic=raise_mic,
        ).run(
            job_id="job-1",
            owner_job_id="job-1",
            use_electrical_reference=False,
            electrical_reference=None,
            host_reference={"kind": "host"},
            capture_channels=2,
            electrical_reference_channel_index=None,
            mic_target="mic-1",
            measurement_scope="active_chain",
        )

        self.assertEqual(len(attempts), 2)
        self.assertEqual(result.attempts_used, 2)
        self.assertTrue(result.mic_auto_boosted)

    def test_cancellation_before_er_fallback_is_terminal(self):
        cancelled = {"not-job"}

        def capture_attempt(**_kwargs):
            cancelled.add("job-1")
            return ({"quality": "ok"}, {"capture": True}, {"playback": True})

        with self.assertRaisesRegex(RuntimeError, "Measurement cancelled"):
            self._runner(capture_attempt, cancelled=cancelled).run(
                job_id="job-1",
                owner_job_id="job-1",
                use_electrical_reference=True,
                electrical_reference={"kind": "electrical"},
                host_reference={"kind": "host"},
                capture_channels=3,
                electrical_reference_channel_index=2,
                mic_target="mic-1",
                measurement_scope="active_chain",
            )


if __name__ == "__main__":
    unittest.main()
