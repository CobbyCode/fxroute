#!/usr/bin/env python3
"""Regression: consecutive TIDAL status/playback refreshes must keep the
footer quality facts complete.

``/api/status`` re-reads MPV telemetry on every poll; a transient read gap
(empty ``current_file`` during a transition, a failed/bounded property read)
used to emit ``stream_info: None``, which collapsed the footer meta-tag from
``FLAC · 24bit · 44.1kHz`` to the bare rate.  The state source must keep
the last known facts while the track identity is stable.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from playback.stream_info import StreamInfoLedger

FULL_RAW = {
    "codec": "FLAC (Free Lossless Audio Codec)",
    "bitrate_bps": 717314,
    "samplerate_hz": 44100,
    "bit_depth": 24,
    "format": "s32",
}
FULL_FACTS = {
    "codec": "FLAC",
    "profile": "Lossless",
    "bitrate_kbps": 717,
    "samplerate_hz": 44100,
    "bit_depth": 24,
}


class StreamInfoLedgerTests(unittest.TestCase):
    def test_consecutive_tidal_gaps_keep_complete_facts(self):
        ledger = StreamInfoLedger()
        track = {"source": "tidal", "id": "t1", "title": "Aerodynamic"}
        self.assertEqual(ledger.resolve(track, FULL_RAW), FULL_FACTS)
        # Two consecutive refreshes with empty/absent telemetry keep the facts.
        self.assertEqual(ledger.resolve(track, {}), FULL_FACTS)
        self.assertEqual(ledger.resolve(track, None), FULL_FACTS)

    def test_radio_station_gap_keeps_facts_for_the_same_station(self):
        ledger = StreamInfoLedger()
        station = {"source": "radio", "id": "radio_a"}
        self.assertEqual(ledger.resolve(station, FULL_RAW), FULL_FACTS)
        self.assertEqual(ledger.resolve(station, {}), FULL_FACTS)

    def test_local_track_gap_keeps_facts(self):
        ledger = StreamInfoLedger()
        track = {"source": "local", "id": "f1"}
        self.assertEqual(ledger.resolve(track, FULL_RAW), FULL_FACTS)
        self.assertEqual(ledger.resolve(track, {}), FULL_FACTS)

    def test_track_change_gap_never_inherits_previous_facts(self):
        ledger = StreamInfoLedger()
        ledger.resolve({"source": "tidal", "id": "t1"}, FULL_RAW)
        self.assertIsNone(ledger.resolve({"source": "tidal", "id": "t2"}, {}))
        # The new track can establish its own facts once telemetry returns.
        raw2 = {"codec": "AAC", "bitrate_bps": 320000, "samplerate_hz": 44100}
        self.assertEqual(ledger.resolve({"source": "tidal", "id": "t2"}, raw2),
                         {"codec": "AAC", "bitrate_kbps": 320, "samplerate_hz": 44100})

    def test_source_change_resets_the_ledger(self):
        ledger = StreamInfoLedger()
        ledger.resolve({"source": "tidal", "id": "t1"}, FULL_RAW)
        # A non-mpv source clears the ledger.
        self.assertIsNone(ledger.resolve({"source": "spotify"}, None))
        # A later TIDAL track must not inherit the old track's facts.
        self.assertIsNone(ledger.resolve({"source": "tidal", "id": "t9"}, {}))
        self.assertEqual(ledger.resolve({"source": "tidal", "id": "t9"}, FULL_RAW), FULL_FACTS)

    def test_unknown_track_never_keeps_facts(self):
        ledger = StreamInfoLedger()
        self.assertIsNone(ledger.resolve(None, FULL_RAW))
        self.assertIsNone(ledger.resolve({}, FULL_RAW))
        self.assertIsNone(ledger.resolve({"source": "qobuz", "id": "x"}, None))


class TidalStatusRefreshStabilityTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: consecutive /api/status polls must keep stream_info full."""

    def setUp(self):
        self._ledger = main.stream_info_ledger
        self._ledger.reset()

    def _player(self, stream_raw):
        return SimpleNamespace(
            state={
                "current_file": "/tmp/tidal.flac",
                "playing": True,
                "paused": False,
                "ended": False,
                "volume": 100,
                "position": 10.0,
                "duration": 200.0,
            },
            get_metadata=lambda: {},
            get_stream_audio_info=lambda: stream_raw,
        )

    async def _poll(self, stream_raw, track=None):
        track = track or {"source": "tidal", "id": "t1", "title": "Aerodynamic"}
        payload = {
            "current_track": track,
            "current_file": "/tmp/tidal.flac",
            "playing": True,
            "paused": False,
            "ended": False,
            "volume": 100,
        }
        player = self._player(stream_raw)
        with mock.patch.object(main, "build_playback_payload", return_value=payload), \
             mock.patch.object(main, "runtime", SimpleNamespace(player_instance=player)), \
             mock.patch.object(main, "_read_version_file", return_value="test"):
            return await main.get_status()

    async def test_consecutive_status_polls_keep_stream_info_complete(self):
        first = await self._poll(FULL_RAW)
        self.assertEqual(first["stream_info"], FULL_FACTS)
        # The next polls hit a telemetry gap (empty read, then absent file).
        second = await self._poll({})
        third = await self._poll(None)
        self.assertEqual(second["stream_info"], FULL_FACTS)
        self.assertEqual(third["stream_info"], FULL_FACTS)

    async def test_track_change_degrades_until_new_telemetry_arrives(self):
        await self._poll(FULL_RAW)
        next_track = {"source": "tidal", "id": "t2", "title": "Miss Annie"}
        gap = await self._poll({}, track=next_track)
        self.assertIsNone(gap["stream_info"])
        fresh = await self._poll(FULL_RAW, track=next_track)
        self.assertEqual(fresh["stream_info"], FULL_FACTS)

    async def test_non_mpv_source_clears_stream_info(self):
        await self._poll(FULL_RAW)
        spotify_track = {"source": "spotify", "id": "s1", "title": "Clockwatching"}
        cleared = await self._poll({}, track=spotify_track)
        self.assertIsNone(cleared["stream_info"])


if __name__ == "__main__":
    unittest.main()
