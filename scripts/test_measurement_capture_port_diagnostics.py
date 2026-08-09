#!/usr/bin/env python3
"""Focused diagnostics for host-reference capture port discovery."""

import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from measurement import MeasurementStore


class MeasurementCapturePortDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = MeasurementStore(home=pathlib.Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _link(self, process):
        return self.store._link_host_reference_capture(
            reference_source_node_name="easyeffects_sink.monitor",
            mic_source_node_name="mic",
            record_node_name="record",
            requested_channel="left",
            record_process=process,
        )

    def test_reports_missing_record_inputs_while_process_is_running(self):
        process = Mock()
        process.poll.return_value = None

        with patch.object(
            self.store,
            "_list_source_output_ports",
            side_effect=lambda name: [f"{name}:monitor_FL"] if name.endswith(".monitor") else [f"{name}:capture_FL"],
        ), patch.object(self.store, "_list_pw_ports", return_value=[]), patch(
            "measurement.time.monotonic", side_effect=[0.0, 0.1, 5.0]
        ), patch("measurement.time.sleep"):
            with self.assertRaises(RuntimeError) as caught:
                self._link(process)

        message = str(caught.exception)
        self.assertIn("missing port groups: record_inputs", message)
        self.assertIn("pw-record=running", message)
        self.assertIn("easyeffects_sink.monitor:monitor_FL", message)
        self.assertIn("mic:capture_FL", message)

    def test_reports_early_process_exit_with_stderr(self):
        process = Mock()
        process.poll.return_value = 2
        process.communicate.return_value = ("", "unknown option --bad")

        with patch.object(self.store, "_list_source_output_ports", return_value=[]), patch.object(
            self.store, "_list_pw_ports", return_value=[]
        ), patch("measurement.time.monotonic", side_effect=[0.0, 0.1]):
            with self.assertRaises(RuntimeError) as caught:
                self._link(process)

        message = str(caught.exception)
        self.assertIn("returncode 2", message)
        self.assertIn("unknown option --bad", message)
        self.assertIn("reference_ports, mic_ports, record_inputs", message)


if __name__ == "__main__":
    unittest.main()
