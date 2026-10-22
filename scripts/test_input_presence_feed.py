#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""External presence feed: TTL bounds and the fail-closed HTTP return channel.

The feed only ever reports what a fresh external heartbeat said; expired,
unknown and malformed input reads as absent. The routes accept input keys the
audio source overview already defines (or the fixed STDIN key) and reject
everything else with 400.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from audio.input_presence import InputPresenceFeed  # noqa: E402
import main  # noqa: E402

X = "alsa_input.usb-Focusrite-00.pro-input-0::pair:1-2"


class PresenceFeedTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.feed = InputPresenceFeed(ttl_seconds=30.0, monotonic=lambda: self.now)

    def test_fresh_report_reads_present_until_the_ttl_passed(self):
        self.feed.report(X, True)
        self.assertTrue(self.feed.present(X))
        self.now += 30.0
        self.assertTrue(self.feed.present(X), "the TTL boundary is still fresh")
        self.now += 0.1
        self.assertFalse(self.feed.present(X), "an expired report reads as absent")

    def test_explicit_false_clears_immediately(self):
        self.feed.report(X, True)
        self.feed.report(X, False)
        self.assertFalse(self.feed.present(X))

    def test_unknown_key_is_absent_and_empty_key_is_rejected(self):
        self.assertFalse(self.feed.present("alsa_input.gone"))
        with self.assertRaises(ValueError):
            self.feed.report("  ", True)

    def test_snapshot_lists_only_fresh_reports(self):
        self.feed.report(X, True)
        self.assertEqual(self.feed.snapshot(), {"ttl_seconds": 30.0, "inputs": {X: True}})
        self.now += 31.0
        self.assertEqual(self.feed.snapshot()["inputs"], {})


class PresenceRouteTests(unittest.TestCase):
    def setUp(self):
        self.now = 500.0
        self.feed = InputPresenceFeed(ttl_seconds=30.0, monotonic=lambda: self.now)
        self.overview = SimpleNamespace(fresh_latest=lambda max_age: {"inputs": [{"key": X}]})
        for patch in (
            mock.patch.object(main, "input_presence", self.feed),
            mock.patch.object(main, "source_overview_feed", self.overview),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = TestClient(main.app)

    def post(self, payload):
        return self.client.post("/api/audio/input-presence", json=payload)

    def test_report_roundtrip_and_ttl_in_the_status_view(self):
        response = self.post({"inputKey": X, "present": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["ttl_seconds"], 30.0)
        self.assertEqual(self.client.get("/api/audio/input-presence").json()["inputs"], {X: True})
        self.now += 31.0
        self.assertEqual(
            self.client.get("/api/audio/input-presence").json()["inputs"], {},
            "a stale heartbeat is fail-closed",
        )

    def test_explicit_off_clears_the_key(self):
        self.post({"inputKey": X, "present": True})
        response = self.post({"inputKey": X, "present": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/audio/input-presence").json()["inputs"], {})

    def test_presence_value_changes_are_logged_without_heartbeat_spam(self):
        with self.assertLogs("main", level="INFO") as captured:
            self.post({"inputKey": X, "present": True})
            self.post({"inputKey": X, "present": True})
            self.post({"inputKey": X, "present": False})
        messages = [line for line in captured.output if "Input presence:" in line]
        self.assertEqual(len(messages), 2)
        self.assertIn(X, messages[0])
        self.assertIn("present=True", messages[0])
        self.assertIn("present=False", messages[1])

    def test_unknown_key_is_rejected_and_never_stored(self):
        response = self.post({"inputKey": "alsa_input.gone", "present": True})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.feed.snapshot()["inputs"], {})
        self.assertEqual(
            self.post({"inputKey": "alsa_input.gone", "present": False}).status_code, 400,
            "an explicit off for an unknown key is rejected too",
        )

    def test_stdin_key_is_addressable_without_an_overview_entry(self):
        response = self.post({"inputKey": "stdin-input", "present": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.feed.present("stdin-input"))

    def test_malformed_bodies_are_rejected(self):
        for payload in ([], 123, None, {}, {"inputKey": X}, {"present": True},
                        {"inputKey": X, "present": "yes"}, {"inputKey": "", "present": True}):
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.feed.snapshot()["inputs"], {})


if __name__ == "__main__":
    unittest.main()
