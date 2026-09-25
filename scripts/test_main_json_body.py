#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""JSON object validation at main.py's mutating HTTP boundaries."""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from starlette.requests import Request

import main


INVALID_BODIES = ("[]", "123", "null", '{"broken":')


class JsonBodyTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def post_raw(self, path, body):
        return self.client.post(path, content=body, headers={"Content-Type": "application/json"})

    def test_heartbeat_rejects_non_objects_and_malformed_json_without_opening(self):
        with mock.patch.object(main, "measurement_sr_session", None), mock.patch.object(
            main, "last_measurement_window_seen_at", 0.0
        ):
            for body in INVALID_BODIES:
                with self.subTest(body=body):
                    response = self.post_raw("/api/power/measurement-heartbeat", body)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(main.last_measurement_window_seen_at, 0.0)
            response = self.post_raw("/api/power/measurement-heartbeat", "{}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["measurement_window_open"])

    def test_device_name_rejects_non_objects_and_malformed_json(self):
        with mock.patch.object(main.shutil, "which", return_value="/usr/bin/hostnamectl"), mock.patch.object(
            main.streaming_api, "_mdns_device_name", return_value="fxroute"
        ), mock.patch.object(main.asyncio, "create_subprocess_exec") as spawn:
            for body in INVALID_BODIES + ("{}",):
                with self.subTest(body=body):
                    response = self.post_raw("/api/system/device-name", body)
                    self.assertEqual(response.status_code, 400, response.text)
            response = self.client.post("/api/system/device-name", json={"hostname": "fxroute"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["changed"])
            spawn.assert_not_called()

    def test_spotify_seek_rejects_non_objects_and_malformed_json(self):
        seek = mock.AsyncMock(return_value={"status": "Playing"})
        with mock.patch.object(main, "spotify_seek_to", seek), mock.patch.object(
            main, "broadcast_spotify_state", new=mock.AsyncMock(side_effect=lambda state: state)
        ):
            for body in INVALID_BODIES:
                with self.subTest(body=body):
                    response = self.post_raw("/api/spotify/seek", body)
                    self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(seek.await_count, 0)
            for body, position in (("{}", 0.0), ('{"position": 12.5}', 12.5)):
                response = self.post_raw("/api/spotify/seek", body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(seek.await_args.args, (position,))

    def test_global_seek_validates_body_for_both_streaming_owners(self):
        for owner in ("spotify", "qobuz"):
            with self.subTest(owner=owner):
                seek = mock.AsyncMock(return_value={"status": "Playing"})
                patches = [mock.patch.object(main, "_resolve_playback_owner", return_value=owner)]
                if owner == "spotify":
                    patches += [mock.patch.object(main, "spotify_seek_to", seek), mock.patch.object(
                        main, "broadcast_spotify_state", new=mock.AsyncMock(side_effect=lambda state: state)
                    )]
                else:
                    provider = mock.Mock(seek=seek)
                    patches += [mock.patch.object(main.streaming, "get_provider", return_value=provider),
                                mock.patch.object(main, "broadcast_qobuz_state", new=mock.AsyncMock(side_effect=lambda state: state))]
                with patches[0], patches[1], patches[2]:
                    for body in INVALID_BODIES:
                        with self.subTest(body=body):
                            response = self.post_raw("/api/playback/seek", body)
                            self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(seek.await_count, 0)
                    for body, position in (("{}", 0.0), ('{"position": 12.5}', 12.5)):
                        response = self.post_raw("/api/playback/seek", body)
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(seek.await_args.args, (position,))

    def test_native_seek_validates_body_even_when_debounced(self):
        player = mock.Mock(_running=True, state={"position": 4, "current_file": "track.flac"})
        with mock.patch.object(main, "_resolve_playback_owner", return_value=None), mock.patch.object(
            main.runtime, "player_instance", player
        ), mock.patch.object(main, "_can_send_play_command", return_value=False), mock.patch.object(
            main, "build_playback_payload", return_value={"position": 4}
        ):
            for body in INVALID_BODIES:
                with self.subTest(body=body):
                    response = self.post_raw("/api/playback/seek", body)
                    self.assertEqual(response.status_code, 400, response.text)
            response = self.post_raw("/api/playback/seek", "{}")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["position"], 4)

    def test_manual_library_rejects_non_objects_and_malformed_json(self):
        library = mock.Mock()

        def add_manual_url(url):
            if not url:
                raise ValueError("URL required")
            return {"url": url}

        library.add_manual_url.side_effect = add_manual_url
        library.status_cached.return_value = {"libraries": []}
        library.claim_background_refresh.return_value = False
        library.discovery_running.return_value = False
        with mock.patch.object(main.runtime.music_library, "manager", library):
            for body in INVALID_BODIES + ("{}",):
                with self.subTest(body=body):
                    response = self.post_raw("/api/music-libraries/manual", body)
                    self.assertEqual(response.status_code, 400, response.text)
            response = self.client.post("/api/music-libraries/manual", json={"url": "smb://host/share"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["entry"], {"url": "smb://host/share"})

    def test_shuffle_rejects_non_objects_and_malformed_json(self):
        with mock.patch.object(main, "_resolve_playback_owner", return_value=None):
            for body in INVALID_BODIES + ("{}",):
                with self.subTest(body=body):
                    response = self.post_raw("/api/playback/shuffle", body)
                    self.assertEqual(response.status_code, 400, response.text)


class BackgroundTaskOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_deferred_restart_is_owned_until_completion(self):
        started = asyncio.Event()
        release = asyncio.Event()
        tasks = []

        async def restart(_service):
            try:
                started.set()
                await release.wait()
            finally:
                main.update_lifecycle.finish_deferred_restart()

        async def update(_timeout, *_args, on_result=None):
            result = {"returncode": 0, "stdout": "Pulling updates with fast-forward only.\n", "stderr": ""}
            if on_result:
                on_result(result)
            return result

        try:
            with mock.patch.object(main, "_run_update_operation", new=update), \
                 mock.patch.object(main, "_restart_fxroute_service_after_response", restart):
                request = Request({"type": "http", "method": "POST", "path": "/api/system/update",
                                   "scheme": "http", "server": ("testserver", 80), "headers": [], "query_string": b""})
                response = await main.system_update(request)
                self.assertTrue(response["restart_scheduled"])
                await asyncio.wait_for(started.wait(), 2)
                tasks = list(main.runtime.lifecycle_background_tasks)
                self.assertEqual(len(tasks), 1)
                self.assertFalse(tasks[0].done())
        finally:
            release.set()
            await asyncio.gather(*tasks)
            await asyncio.sleep(0)
        self.assertFalse(main.runtime.lifecycle_background_tasks)

    async def test_claimed_refresh_is_owned_until_completion(self):
        started = asyncio.Event()
        manager = mock.Mock()
        manager.claim_background_refresh.return_value = True
        loop = asyncio.get_running_loop()
        release_thread = threading.Event()

        def refresh():
            loop.call_soon_threadsafe(started.set)
            release_thread.wait(2)

        manager.run_claimed_refresh.side_effect = refresh
        try:
            refreshing = await main._request_library_discovery_refresh(manager)
            self.assertTrue(refreshing)
            await asyncio.wait_for(started.wait(), 2)
            self.assertTrue(main.runtime.lifecycle_background_tasks)
        finally:
            release_thread.set()
            await asyncio.gather(*main.runtime.lifecycle_background_tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.assertFalse(main.runtime.lifecycle_background_tasks)


if __name__ == "__main__":
    unittest.main()
