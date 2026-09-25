#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Measurement-heartbeat context binding across browser tabs.

A heartbeat carries the measurement-session epoch the sending tab last
observed. A late heartbeat from an older tab context must not cancel a
newer pending close and keep a foreign measurement owner alive.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from fastapi.testclient import TestClient
from measurement.session import MeasurementSampleRateSession


class HeartbeatEpochSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_heartbeat_does_not_extend_newer_context(self):
        session = MeasurementSampleRateSession()
        session.active = True
        session.active_manual_job_ids.add("job-B")
        stale_epoch = session.capture_entry_epoch()
        # A newer context requests close; release is deferred while B runs.
        await session.request_close()
        self.assertTrue(session.close_requested)
        self.assertTrue(session.deferred_release_pending)
        # Late heartbeat from old tab A: the pending close must survive.
        await session.request_open(stale_epoch)
        self.assertTrue(session.close_requested)
        self.assertTrue(session.deferred_release_pending)

    async def test_fresh_heartbeat_still_reopens(self):
        session = MeasurementSampleRateSession()
        session.active = True
        session.active_manual_job_ids.add("job-B")
        await session.request_close()
        await session.request_open(session.capture_entry_epoch())
        self.assertFalse(session.close_requested)
        self.assertFalse(session.deferred_release_pending)

    async def test_legacy_heartbeat_without_epoch_still_reopens(self):
        session = MeasurementSampleRateSession()
        session.active = True
        session.close_requested = True
        session.deferred_release_pending = True
        await session.request_open()
        self.assertFalse(session.close_requested)
        self.assertFalse(session.deferred_release_pending)


class HeartbeatEpochHelperTests(unittest.TestCase):
    def test_parses_valid_epochs_and_keeps_legacy_missing(self):
        self.assertIsNone(main._heartbeat_session_epoch(None))
        self.assertIsNone(main._heartbeat_session_epoch("stale"))
        self.assertIsNone(main._heartbeat_session_epoch(-1))
        self.assertIsNone(main._heartbeat_session_epoch(True))
        self.assertEqual(main._heartbeat_session_epoch(3), 3)
        self.assertEqual(main._heartbeat_session_epoch("3"), 3)


class HeartbeatEndpointEpochTests(unittest.TestCase):
    def test_stale_tab_heartbeat_keeps_pending_close(self):
        session = MeasurementSampleRateSession()
        session.active = True
        session.active_manual_job_ids.add("job-B")
        stale_epoch = session.capture_entry_epoch()
        asyncio.run(session.request_close())
        current_epoch = session.capture_entry_epoch()
        self.assertNotEqual(stale_epoch, current_epoch)
        client = TestClient(main.app)
        with mock.patch.object(main, "measurement_sr_session", session), \
                mock.patch.object(main, "last_measurement_window_seen_at", 0.0):
            response = client.post("/api/power/measurement-heartbeat",
                                   json={"open": True, "session_epoch": stale_epoch})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["measurement_session_epoch"], current_epoch)
            self.assertTrue(session.close_requested)
            self.assertTrue(session.deferred_release_pending)
            response = client.post("/api/power/measurement-heartbeat",
                                   json={"open": True, "session_epoch": current_epoch})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(session.close_requested)

    def test_legacy_heartbeat_reports_epoch(self):
        session = MeasurementSampleRateSession()
        client = TestClient(main.app)
        with mock.patch.object(main, "measurement_sr_session", session), \
                mock.patch.object(main, "last_measurement_window_seen_at", 0.0):
            response = client.post("/api/power/measurement-heartbeat", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["measurement_session_epoch"],
                             session.capture_entry_epoch())


if __name__ == "__main__":
    unittest.main()
