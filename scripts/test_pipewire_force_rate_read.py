#!/usr/bin/env python3
"""get_current_pipewire_force_rate must read only the force-rate pin.

Idle watchers and the measurement restore path need the live
``clock.force-rate`` pin and nothing else.  It used to be derived from the
full samplerate status, which costs four subprocesses (pw-metadata, wpctl,
pactl, pw-cli) per call; every idle link-watch tick paid that for a value
that is 0 whenever no pin is live.  The narrow read keeps the
status-derived value semantics (live pin else 0) while issuing a single
pw-metadata read.  A failed read reports None (unknown) rather than 0
(no pin), so callers distinguish failure from an unpinned graph.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.samplerate as samplerate  # noqa: E402
import audio.samplerate.alignment as alignment  # noqa: E402

METADATA_OUTPUT = """\
update: id:0 key:'clock.rate' value:'44100' type:''
update: id:0 key:'clock.allowed-rates' value:'[ 44100 48000 96000 ]' type:''
update: id:0 key:'clock.force-rate' value:'{force}' type:''
"""


class PipewireForceRateReadTests(unittest.TestCase):
    def _read(self, output: str, *, runs: list[list[str]] | None = None):
        def fake_run(args):
            if runs is not None:
                runs.append(list(args))
            return output

        with mock.patch.object(alignment, "_run_command", side_effect=fake_run):
            return samplerate.get_current_pipewire_force_rate()

    def test_reads_only_the_pin_metadata(self):
        runs: list[list[str]] = []
        self._read(METADATA_OUTPUT.format(force="0"), runs=runs)
        self.assertEqual(runs, [["pw-metadata", "-n", "settings", "0"]])

    def test_returns_the_live_pin(self):
        self.assertEqual(self._read(METADATA_OUTPUT.format(force="96000")), 96000)

    def test_reports_zero_when_no_pin_is_set(self):
        self.assertEqual(self._read(METADATA_OUTPUT.format(force="0")), 0)

    def test_reports_zero_when_the_pin_entry_is_absent(self):
        self.assertEqual(self._read("update: id:0 key:'clock.rate' value:'44100'\n"), 0)

    def test_reports_none_when_the_read_fails(self):
        with mock.patch.object(alignment, "_run_command", side_effect=RuntimeError("boom")):
            self.assertIsNone(samplerate.get_current_pipewire_force_rate())

    def test_matches_the_status_derived_normalization(self):
        # Value semantics match the full status' force_rate after the same
        # "no pin means zero" normalization, for a live pin and no pin.
        # Read failure is None (see above) and covered separately.
        for force in ("0", "44100", "96000"):
            status = {"force_rate": int(force)}
            expected = status.get("force_rate") if status["force_rate"] > 0 else 0
            self.assertEqual(self._read(METADATA_OUTPUT.format(force=force)), expected)


if __name__ == "__main__":
    unittest.main()
