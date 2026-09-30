#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Qobuz seek must change actual daemon state or report a transport failure."""

from __future__ import annotations

import json
import asyncio
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
import main
import streaming.qobuz.provider as qobuz_provider
from streaming.qobuz.provider import QobuzProvider


class _DaemonHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body, content_type='application/json'):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/api/playback' and self.server.natural_advance_start is not None:
            self.server.position = 30 + int(0.95 + time.monotonic() - self.server.natural_advance_start)
        if self.path == '/api/playback' and self.server.pending_seek is not None:
            if self.server.seek_reads_remaining:
                self.server.seek_reads_remaining -= 1
            else:
                self.server.position = self.server.pending_seek
                self.server.pending_seek = None
        routes = {
            '/api/status': {'logged_in': True, 'qconnect': True, 'state': 'ready', 'track_id': self.server.track_id},
            '/api/playback': {'state': self.server.state, 'track_id': self.server.track_id,
                'position_secs': self.server.position, 'duration_secs': self.server.duration,
                'volume': 1.0, 'sample_rate': 44100, 'bit_depth': 24},
            '/api/queue': {'current_index': 0, 'current_track': {
                'id': self.server.track_id, 'title': 'Track', 'artist': 'Artist', 'album': 'Album',
                'duration_secs': self.server.duration, 'artwork_url': '', 'hires': True,
                'bit_depth': 24, 'sample_rate': 44.1, 'source': 'qobuz'},
                'history': [], 'repeat': 'Off', 'shuffle': False,
                'stop_after_track_id': None, 'total_tracks': 1, 'upcoming': []},
        }
        self._reply(200 if self.path in routes else 404, routes.get(self.path, {}))

    def do_POST(self):
        if self.path != '/api/playback/seek':
            self._reply(404, {})
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.received.append(body)
        if self.server.reject_status:
            self._reply(self.server.reject_status, 'rejected', 'text/plain')
            return
        position = body.get('position_secs')
        if type(position) is not int or position < 0 or set(body) != {'position_secs'}:
            self._reply(422, 'missing unsigned integer position_secs', 'text/plain')
            return
        if self.server.change_track_on_seek:
            self.server.track_id = 43
        if self.server.advance_ignored_seek:
            self.server.natural_advance_start = time.monotonic()
        if self.server.play_on_seek:
            self.server.state = 'Playing'
        if not self.server.ignore_seek:
            if self.server.seek_reads_remaining:
                self.server.pending_seek = min(200, position)
            else:
                self.server.position = min(200, position)
        self._reply(200, 'ok', 'text/plain')


class QobuzSeekTests(unittest.TestCase):
    seek_path = '/api/streaming/qobuz/seek'

    def setUp(self):
        self.daemon = ThreadingHTTPServer(('127.0.0.1', 0), _DaemonHandler)
        self.daemon.position = 30
        self.daemon.duration = 200
        self.daemon.state = 'Playing'
        self.daemon.track_id = 42
        self.daemon.pending_seek = None
        self.daemon.seek_reads_remaining = 0
        self.daemon.ignore_seek = False
        self.daemon.change_track_on_seek = False
        self.daemon.advance_ignored_seek = False
        self.daemon.natural_advance_start = None
        self.daemon.play_on_seek = False
        self.daemon.reject_status = None
        self.daemon.received = []
        self.thread = threading.Thread(target=self.daemon.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_daemon)
        self.provider = QobuzProvider(f'http://127.0.0.1:{self.daemon.server_port}')
        self.broadcast = AsyncMock()
        for patcher in (
            patch('streaming.get_provider', return_value=self.provider),
            patch('streaming.qobuz.backend.qbzd_installed', return_value=True),
            patch.object(main.playback_state, 'current_playback_owner', 'qobuz'),
            patch.object(main.playback_state, 'latest_qobuz_state', None),
            patch.object(main.playback_state, 'qobuz_state_read_sequence', 0),
            patch.object(main.playback_state, 'qobuz_state_commit_sequence', 0),
            patch.object(main, 'get_output_volume_safe', return_value=20),
            patch.object(main.peak_monitor_coordinator, 'sync_qobuz_state', new=AsyncMock()),
            patch.object(main.manager, 'broadcast', new=self.broadcast),
            patch.object(qobuz_provider, 'SEEK_CONFIRM_TIMEOUT_S', 0.15, create=True),
            patch.object(qobuz_provider, 'SEEK_CONFIRM_INTERVAL_S', 0.01, create=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _stop_daemon(self):
        self.daemon.shutdown()
        self.daemon.server_close()
        self.thread.join(timeout=2)

    def seek(self, position):
        return self.client.post(self.seek_path, json={'position': position})

    def status(self):
        response = self.client.get('/api/streaming/qobuz/status')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_seek_changes_real_position_in_integer_seconds_and_readback(self):
        response = self.seek(90.4)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['position'], 90.0)
        self.assertEqual(self.status()['position'], 90.0)
        self.assertEqual(self.status()['trackId'], '42')
        self.assertEqual(self.status()['status'], 'Playing')
        self.assertEqual(self.daemon.received, [{'position_secs': 90}])

    def test_forward_backward_zero_and_negative_clamp(self):
        for requested, actual in ((120, 120), (12, 12), (0, 0), (-5, 0)):
            with self.subTest(requested=requested):
                response = self.seek(requested)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.status()['position'], actual)

    def test_rejected_seek_is_not_success_or_position_update(self):
        for status in (422, 500):
            with self.subTest(status=status):
                self.daemon.reject_status = status
                response = self.seek(90)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(self.status()['position'], 30.0)
                self.assertNotIn('position', response.json())
        self.broadcast.assert_not_awaited()

    def test_connection_failure_is_not_success(self):
        with patch('streaming.qobuz.backend.requests.post', side_effect=requests.ConnectionError()):
            response = self.seek(90)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.status()['position'], 30.0)
        self.broadcast.assert_not_awaited()

    def test_invalid_position_is_client_error_without_daemon_mutation(self):
        for raw in ('null', '"invalid"', '{}', '[]', '"NaN"', '"Infinity"'):
            with self.subTest(raw=raw):
                response = self.client.post(self.seek_path,
                    content='{"position":' + raw + '}', headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.daemon.received, [])
        self.broadcast.assert_not_awaited()

    def test_invalid_upstream_readback_is_not_classified_as_bad_client_position(self):
        self.daemon.duration = 'invalid'
        response = self.seek(90)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.daemon.position, 30)
        self.broadcast.assert_not_awaited()

    def test_delayed_paused_seek_returns_confirmed_position_not_old_snapshot(self):
        self.daemon.state = 'Paused'
        self.daemon.seek_reads_remaining = 3
        response = self.seek(90)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['position'], 90)
        self.assertEqual(response.json()['status'], 'Paused')
        self.assertEqual(self.status()['position'], 90)

    def test_suspended_paused_seek_is_conflict_not_phantom_success_or_resume(self):
        self.daemon.state = 'Paused'
        self.daemon.ignore_seek = True
        response = self.seek(90)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.status()['position'], 30)
        self.assertEqual(self.status()['status'], 'Paused')
        self.broadcast.assert_not_awaited()

    def test_seek_back_from_paused_track_end_returns_confirmed_position(self):
        self.daemon.state = 'Paused'
        self.daemon.position = 200
        self.daemon.seek_reads_remaining = 2
        response = self.seek(12)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['position'], 12)
        self.assertEqual(self.status()['position'], 12)

    def test_seek_clamps_to_known_duration_and_returns_actual_end_position(self):
        response = self.seek(220)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['position'], 200)
        self.assertEqual(self.status()['position'], 200)

    def test_track_change_during_seek_is_not_success_on_unrelated_track(self):
        self.daemon.change_track_on_seek = True
        response = self.seek(90)
        self.assertEqual(response.status_code, 409, response.text)
        self.broadcast.assert_not_awaited()

    def test_unavailable_daemon_is_transport_error_not_empty_track_conflict(self):
        with patch('streaming.qobuz.backend.requests.get', side_effect=requests.ConnectionError()):
            response = self.seek(90)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.daemon.received, [])
        self.broadcast.assert_not_awaited()

    def test_ignored_close_backward_seek_is_not_accepted_by_playing_allowance(self):
        self.daemon.ignore_seek = True
        response = self.seek(29)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.daemon.position, 30)
        self.broadcast.assert_not_awaited()

    def test_ignored_close_forward_seek_is_not_confirmed_by_natural_advancement(self):
        self.daemon.ignore_seek = True
        self.daemon.advance_ignored_seek = True
        response = self.seek(31)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertGreaterEqual(self.daemon.position, 31)
        self.broadcast.assert_not_awaited()

    def test_paused_metadata_with_playing_raw_baseline_cannot_confirm_ignored_seek(self):
        self.daemon.ignore_seek = True
        self.daemon.advance_ignored_seek = True
        real_status = self.provider.status
        reads = 0

        async def stale_initial_transport():
            nonlocal reads
            state = await real_status()
            reads += 1
            if reads == 1:
                state['status'] = 'Paused'
            return state

        with patch.object(self.provider, 'status', side_effect=stale_initial_transport):
            response = self.seek(31)
        self.assertEqual(response.status_code, 409, response.text)
        self.broadcast.assert_not_awaited()

    def test_partial_status_failure_is_transport_error_before_and_after_seek(self):
        real_get = requests.get
        for failed_read in (2, 4):
            with self.subTest(failed_read=failed_read):
                self.daemon.position = 30
                self.daemon.received.clear()
                self.broadcast.reset_mock()
                reads = 0

                def fail_one_status_read(url, **kwargs):
                    nonlocal reads
                    if url.endswith('/api/status'):
                        reads += 1
                        if reads == failed_read:
                            raise requests.ConnectionError('status read failed')
                    return real_get(url, **kwargs)

                with patch('streaming.qobuz.backend.requests.get', side_effect=fail_one_status_read):
                    response = self.seek(90)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertEqual(len(self.daemon.received), 0 if failed_read == 2 else 1)
                self.broadcast.assert_not_awaited()

    def test_external_resume_during_paused_seek_is_not_confirmation(self):
        self.daemon.state = 'Paused'
        self.daemon.ignore_seek = True
        self.daemon.advance_ignored_seek = True
        self.daemon.play_on_seek = True
        response = self.seek(31)
        self.assertEqual(response.status_code, 409, response.text)
        self.broadcast.assert_not_awaited()

    def test_confirmation_budget_exhaustion_is_not_a_backend_transport_failure(self):
        self.daemon.state = 'Paused'
        self.daemon.ignore_seek = True
        real_status = self.provider.status
        reads = 0

        async def healthy_delayed_poll():
            nonlocal reads
            reads += 1
            if reads > 1:
                await asyncio.sleep(0.2)
            return await real_status()

        with patch.object(self.provider, 'status', side_effect=healthy_delayed_poll):
            response = self.seek(90)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.status()['position'], 30)
        self.broadcast.assert_not_awaited()


class QobuzGlobalSeekTests(QobuzSeekTests):
    seek_path = '/api/playback/seek'


if __name__ == '__main__':
    unittest.main(verbosity=2)
