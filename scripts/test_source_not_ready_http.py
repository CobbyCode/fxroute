#!/usr/bin/env python3
"""Transient source-start failures map to HTTP 503; other transition failures stay 500."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from playback.transition import PlaybackTransitionFailure  # noqa: E402
from playback.transition.models import SourceNotReady  # noqa: E402


class SourceNotReadyHttpTests(unittest.TestCase):
    def test_source_not_ready_is_runtime_error(self):
        self.assertTrue(issubclass(SourceNotReady, RuntimeError))

    def test_transient_failure_maps_to_503(self):
        exc = PlaybackTransitionFailure("x", transition_id="t", stage="target-source-start", transient=True)
        self.assertEqual(main._transition_error_http(exc).status_code, 503)

    def test_internal_failure_stays_500(self):
        exc = PlaybackTransitionFailure("x", transition_id="t", stage="target-source-start")
        self.assertFalse(exc.transient)
        self.assertEqual(main._transition_error_http(exc).status_code, 500)


if __name__ == "__main__":
    unittest.main()
