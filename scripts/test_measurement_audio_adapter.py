"""Characterization tests for the low-level Measurement audio-system owner."""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from measurement_audio import MeasurementAudioAdapter


class MeasurementAudioAdapterTests(unittest.TestCase):
    def test_pipewire_port_and_link_commands_keep_order_and_timeouts(self):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command == ["pw-link", "-io"]:
                return subprocess.CompletedProcess(command, 0, "node:out_FL\nnode:out_FR\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        adapter = MeasurementAudioAdapter(command_runner=run)

        self.assertEqual(adapter.list_pw_ports("node"), ["node:out_FL", "node:out_FR"])
        adapter.create_pipewire_link("source:out", "record:in")
        self.assertTrue(adapter.disconnect_link("source:out", "record:in"))

        self.assertEqual(
            [call[0] for call in calls],
            [
                ["pw-link", "-io"],
                ["pw-link", "source:out", "record:in"],
                ["pw-link", "-d", "source:out", "record:in"],
            ],
        )
        self.assertTrue(all(call[1]["timeout"] == 3 for call in calls))

if __name__ == "__main__":
    unittest.main()
