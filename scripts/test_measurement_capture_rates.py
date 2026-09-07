#!/usr/bin/env python3
"""Regression: capture-input rate inspection must use the real pw-cli command.

On the 104 host (PipeWire 1.6.8) the UMC204HD measurement input reported
``supported_rates: []``, so the Convolver setup offered only the 48 kHz
fallback even though the device supports 44.1-192 kHz. Root cause: the
inspector invoked ``pw-cli enum-param <serial> Format`` — a subcommand that
does not exist (correct: ``pw-cli enum-params <serial> EnumFormat``, as used
by audio/samplerate/overview.py). The call failed, the rates key was never
set, and every fresh discovery (including manual reselect after a missing
device) fell back to 48 kHz.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from measurement.store import MeasurementStore

# Verbatim `pw-cli enum-params 60 EnumFormat` output from the 104 host for
# alsa_input.usb-BEHRINGER_UMC204HD_192k-00.analog-stereo.
ENUM_FORMAT_UMC204HD = """\
  Object: size 200, type Spa:Pod:Object:Param:Format (262147), id Spa:Enum:ParamId:EnumFormat (3)
    Prop: key Spa:Pod:Object:Param:Format:mediaType (1), flags 00000000
      Id 1        (Spa:Enum:MediaType:audio)
    Prop: key Spa:Pod:Object:Param:Format:mediaSubtype (2), flags 00000000
      Id 1        (Spa:Enum:MediaSubtype:raw)
    Prop: key Spa:Pod:Object:Param:Format:Audio:format (65537), flags 00000000
      Choice: type Spa:Enum:Choice:None, flags 00000000 24 4
        Id 267      (Spa:Enum:AudioFormat:S32LE)
        Id 267      (Spa:Enum:AudioFormat:S32LE)
    Prop: key Spa:Pod:Object:Param:Format:Audio:rate (65539), flags 00000000
      Choice: type Spa:Enum:Choice:Range, flags 00000000 28 4
        Int 44100
        Int 44100
        Int 192000
    Prop: key Spa:Pod:Object:Param:Format:Audio:channels (65540), flags 00000000
      Int 2
"""

WPCTL_INSPECT_NODE = """\
id 60, type PipeWire:Interface:Node
  * node.name = "alsa_input.usb-BEHRINGER_UMC204HD_192k-00.analog-stereo"
    audio.channels = "2"
    audio.rate = "48000"
  * device.id = "49"
"""


class InspectSourceDetailsTests(unittest.TestCase):
    def _run(self, argv):
        if argv[:2] == ["pw-cli", "enum-params"]:
            return SimpleNamespace(returncode=0, stdout=ENUM_FORMAT_UMC204HD)
        if argv[:2] == ["wpctl", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=WPCTL_INSPECT_NODE)
        return SimpleNamespace(returncode=1, stdout="")

    def test_umc204hd_reports_hardware_rate_range(self):
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(list(argv))
            return self._run(list(argv))

        with mock.patch("measurement.store.subprocess.run", side_effect=fake_run):
            details = MeasurementStore._inspect_source_details(object(), "60")
        self.assertEqual(
            details.get("supported_rates"),
            [44100, 48000, 88200, 96000, 176400, 192000],
        )
        format_calls = [argv for argv in seen if argv[:1] == ["pw-cli"]]
        self.assertEqual(len(format_calls), 1)
        self.assertEqual(format_calls[0][:2], ["pw-cli", "enum-params"])
        self.assertEqual(format_calls[0][3], "EnumFormat")

    def test_failed_enum_keeps_previous_list_semantics(self):
        def fake_run(argv, **kwargs):
            if argv[:2] == ["wpctl", "inspect"]:
                return SimpleNamespace(returncode=0, stdout=WPCTL_INSPECT_NODE)
            return SimpleNamespace(returncode=1, stdout="")

        with mock.patch("measurement.store.subprocess.run", side_effect=fake_run):
            details = MeasurementStore._inspect_source_details(object(), "60")
        self.assertEqual(details.get("supported_rates", []), [])
        self.assertEqual(details.get("sample_rate"), 48000)


if __name__ == "__main__":
    unittest.main()
