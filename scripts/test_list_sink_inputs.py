#!/usr/bin/env python3
"""Verhaltenstests für die REFACTOR-011-Extraktion:

- sink_inputs.list_sink_inputs (pactl sink-inputs Parser)

sowie Wrapper-Parität gegen main._list_sink_inputs einschließlich
Fehlerpfaden. Alle Tests mocken subprocess.run mit realistischen pactl-
Ausgaben.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sink_inputs


def _completed(returncode: int = 0, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


FULL_SINGLE = """Sink Input #42
\tDriver: protocol-native.c
\tOwner Module: module-stream-restore
\tClient: 77
\tSink: 12
\tSample Specification: float32le 2ch 48000Hz
\tChannel Map: front-left,front-right
\tCorked: no
\tMute: yes
\tVolume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
\t        balance 0.00
\tBuffer Latency: 123456 usec
\tSink Latency: 234567 usec
\tResample method: copy
\tProperties:
\t\tapplication.name = "mpv"
\t\tapplication.process.id = "1234"
\t\tnode.name = "mpv"
\t\tmedia.name = "Track One"
\t\tmedia.role = "music"
"""

FULL_MULTI = """Sink Input #42
\tSample Specification: float32le 2ch 48000Hz
\tSink: 12
\tCorked: no
\tMute: yes
\tVolume: front-left: 65536 / 100% / 0.00 dB
\tProperties:
\t\tapplication.name = "mpv"
\t\tnode.name = "mpv"

Sink Input #7
\tSample Specification: s16le 2ch 44100Hz
\tSink: 3
\tCorked: yes
\tMute: no
\tVolume: front-left: 32768 /  50% / -6.02 dB
\tProperties:
\t\tapplication.name = spotify
\t\tapplication.id = "spotify"
"""


class ListSinkInputsTests(unittest.TestCase):
    def test_full_single_sink_input(self):
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, FULL_SINGLE)) as run:
            entries = sink_inputs.list_sink_inputs()
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args, ["pactl", "list", "sink-inputs"])
        self.assertEqual(run.call_args.kwargs["timeout"], 1.5)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], "42")
        self.assertEqual(entry["sink"], "12")
        self.assertEqual(entry["sample_rate"], 48000)
        self.assertIs(entry["corked"], False)
        self.assertIs(entry["muted"], True)
        self.assertEqual(entry["volume_percent"], 100)
        self.assertEqual(entry["properties"]["application.name"], "mpv")
        self.assertEqual(entry["properties"]["node.name"], "mpv")
        self.assertEqual(entry["properties"]["media.name"], "Track One")

    def test_multiple_sink_inputs(self):
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, FULL_MULTI)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(len(entries), 2)
        first, second = entries
        self.assertEqual(first["id"], "42")
        self.assertEqual(first["sample_rate"], 48000)
        self.assertEqual(first["volume_percent"], 100)
        self.assertEqual(second["id"], "7")
        self.assertEqual(second["sample_rate"], 44100)
        self.assertIs(second["corked"], True)
        self.assertIs(second["muted"], False)
        self.assertEqual(second["volume_percent"], 50)
        self.assertEqual(second["properties"]["application.name"], "spotify")
        self.assertEqual(second["properties"]["application.id"], "spotify")

    def test_missing_optional_fields(self):
        minimal = """Sink Input #1
\tSink: 9
\tCorked: no
\tMute: no
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, minimal)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], "1")
        self.assertEqual(entry["sink"], "9")
        self.assertNotIn("sample_rate", entry)
        self.assertNotIn("volume_percent", entry)
        self.assertEqual(entry["properties"], {})

    def test_properties_without_quotes(self):
        no_quotes = """Sink Input #3
\tProperties:
\t\tapplication.name = mpv
\t\tnode.name = mpv
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, no_quotes)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(entries[0]["properties"]["application.name"], "mpv")
        self.assertEqual(entries[0]["properties"]["node.name"], "mpv")

    def test_properties_with_quotes_stripped(self):
        quoted = """Sink Input #3
\tProperties:
\t\tapplication.name = "mpv player"
\t\tnode.name = "mpv"
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, quoted)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(entries[0]["properties"]["application.name"], "mpv player")
        self.assertEqual(entries[0]["properties"]["node.name"], "mpv")

    def test_properties_line_without_separator_ignored(self):
        odd = """Sink Input #3
\tProperties:
\t\tapplication.name = "mpv"
\t\tmalformed line without separator
\t\tnode.name = "mpv"
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, odd)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(entries[0]["properties"]["application.name"], "mpv")
        self.assertEqual(entries[0]["properties"]["node.name"], "mpv")
        self.assertNotIn("malformed line without separator", entries[0]["properties"])

    def test_volume_variants(self):
        variants = """Sink Input #1
\tVolume: front-left: 65536 / 100% / 0.00 dB
Sink Input #2
\tVolume: front-left: 32768 / 50% / -6.02 dB, front-right: 32768 / 50% / -6.02 dB
Sink Input #3
\tVolume: mono: 65536 / 100% / 0.00 dB
Sink Input #4
\tVolume: front-left: 32768 / 50 % / -6.02 dB
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, variants)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(entries[0]["volume_percent"], 100)
        self.assertEqual(entries[1]["volume_percent"], 50)
        self.assertEqual(entries[2]["volume_percent"], 100)
        # Original-RegEx (kein \\s* zwischen Ziffern und %) matcht `/ 50 % /`
        # nicht -> kein volume_percent gesetzt (1:1-Verhalten vor Extraktion)
        self.assertNotIn("volume_percent", entries[3])

    def test_volume_without_percent_marker_skipped(self):
        odd = """Sink Input #1
\tVolume: front-left: 65536
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, odd)):
            entries = sink_inputs.list_sink_inputs()
        self.assertNotIn("volume_percent", entries[0])

    def test_sample_specification_variants(self):
        specs = """Sink Input #1
\tSample Specification: float32le 2ch 48000Hz
Sink Input #2
\tSample Specification: s16le 2ch 44100Hz
Sink Input #3
\tSample Specification: float32le 2ch 44100Hz
Sink Input #4
\tSample Specification: unknown
"""
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, specs)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(entries[0]["sample_rate"], 48000)
        self.assertEqual(entries[1]["sample_rate"], 44100)
        self.assertEqual(entries[2]["sample_rate"], 44100)
        self.assertNotIn("sample_rate", entries[3])

    def test_empty_output_returns_empty_list(self):
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, "")):
            self.assertEqual(sink_inputs.list_sink_inputs(), [])

    def test_returncode_nonzero_returns_empty_list(self):
        with patch("sink_inputs.subprocess.run", return_value=_completed(1, "boom")):
            self.assertEqual(sink_inputs.list_sink_inputs(), [])

    def test_subprocess_exception_returns_empty_list(self):
        with patch("sink_inputs.subprocess.run", side_effect=OSError("pactl missing")):
            self.assertEqual(sink_inputs.list_sink_inputs(), [])
        with patch("sink_inputs.subprocess.run", side_effect=TimeoutError("timeout")):
            self.assertEqual(sink_inputs.list_sink_inputs(), [])

    def test_trailing_input_appended(self):
        # Kein Abschluss-Header nach dem letzten Block — wird trotzdem angehängt
        with patch("sink_inputs.subprocess.run", return_value=_completed(0, FULL_SINGLE)):
            entries = sink_inputs.list_sink_inputs()
        self.assertEqual(len(entries), 1)


class WrapperParityTests(unittest.TestCase):
    def setUp(self):
        import main
        self.main = main

    def _parity(self, stdout: str, returncode: int = 0):
        with patch("sink_inputs.subprocess.run", return_value=_completed(returncode, stdout)):
            return (
                self.main._list_sink_inputs(),
                sink_inputs.list_sink_inputs(),
            )

    def test_parity_full_single(self):
        a, b = self._parity(FULL_SINGLE)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 1)

    def test_parity_full_multi(self):
        a, b = self._parity(FULL_MULTI)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 2)

    def test_parity_empty_output(self):
        a, b = self._parity("")
        self.assertEqual(a, b)
        self.assertEqual(a, [])

    def test_parity_returncode_nonzero(self):
        a, b = self._parity("partial output", returncode=1)
        self.assertEqual(a, b)
        self.assertEqual(a, [])

    def test_parity_subprocess_exception(self):
        with patch("sink_inputs.subprocess.run", side_effect=OSError("boom")):
            self.assertEqual(self.main._list_sink_inputs(), sink_inputs.list_sink_inputs())
            self.assertEqual(self.main._list_sink_inputs(), [])
        with patch("sink_inputs.subprocess.run", side_effect=TimeoutError("timeout")):
            self.assertEqual(self.main._list_sink_inputs(), sink_inputs.list_sink_inputs())
            self.assertEqual(self.main._list_sink_inputs(), [])

    def test_parity_odd_lines(self):
        odd = """Sink Input #1
\tSample Specification: float32le 2ch 48000Hz
\tSink: 12
\tCorked: no
\tMute: no
\tVolume: front-left: 65536 / 100% / 0.00 dB
\tProperties:
\t\tapplication.name = "mpv"
\t\tmalformed
\t\tnode.name = mpv
"""
        a, b = self._parity(odd)
        self.assertEqual(a, b)
        self.assertEqual(a[0]["properties"]["application.name"], "mpv")
        self.assertEqual(a[0]["properties"]["node.name"], "mpv")


if __name__ == "__main__":
    unittest.main()
