# SPDX-License-Identifier: AGPL-3.0-only
"""Device approval must not outlive its caller or a newer auth intent."""

import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming.tidal import auth  # noqa: E402


class DeviceLoginLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file_patch = mock.patch.object(auth, "SESSION_FILE", Path(self.tmp.name) / "session.json")
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)
        self.manager = auth.TidalSession()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.session = SimpleNamespace(
            config=SimpleNamespace(quality=None), token_type="Bearer",
            access_token="approved", refresh_token="refresh", expiry_time=None,
            is_pkce=False, country_code="US", user=SimpleNamespace(id=3, email="u@example.com"),
            check_login=lambda: True,
        )
        self.session.get_link_login = lambda: SimpleNamespace(
            verification_uri="https://link.tidal.com", verification_uri_complete="https://link.tidal.com/1",
            user_code="1234", expires_in=600,
        )

        def approve(link, until_expiry=False):
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("approval test timed out")

        self.session.process_link_login = approve
        session_patch = mock.patch.object(auth, "_new_session", return_value=self.session)
        tidal_patch = mock.patch.object(auth, "tidalapi", object())
        session_patch.start()
        tidal_patch.start()
        self.addCleanup(session_patch.stop)
        self.addCleanup(tidal_patch.stop)

    async def asyncTearDown(self):
        self.release.set()

    async def _start_waiting(self):
        await self.manager.start_device_login_async()
        task = asyncio.create_task(self.manager.finish_device_login_async())
        await asyncio.wait_for(asyncio.to_thread(self.entered.wait, 2), 3)
        return task

    async def test_cancelled_approval_cannot_commit_after_worker_returns(self):
        task = await self._start_waiting()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.release.set()
        await asyncio.sleep(0.05)
        self.assertIsNone(self.manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    async def test_logout_does_not_wait_for_approval_and_invalidates_result(self):
        task = await self._start_waiting()
        await asyncio.wait_for(self.manager.clear(), 1)
        self.release.set()
        with self.assertRaises(auth.TidalAuthError):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(self.manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    async def test_newer_login_invalidates_old_approval(self):
        task = await self._start_waiting()
        await asyncio.wait_for(self.manager.start_device_login_async(), 1)
        self.release.set()
        with self.assertRaises(auth.TidalAuthError):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(self.manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    async def test_cancelled_start_cannot_restore_pending_after_logout(self):
        creating = threading.Event()
        release_create = threading.Event()
        real_create = self.manager._create_device_login

        def delayed_create(quality):
            creating.set()
            if not release_create.wait(5):
                raise RuntimeError("start test timed out")
            return real_create(quality)

        with mock.patch.object(self.manager, "_create_device_login", side_effect=delayed_create):
            task = asyncio.create_task(self.manager.start_device_login_async())
            await asyncio.wait_for(asyncio.to_thread(creating.wait, 2), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            await asyncio.wait_for(self.manager.clear(), 1)
            release_create.set()
            await asyncio.sleep(0.05)
        self.assertIsNone(self.manager._pending)
        self.assertIsNone(self.manager._pending_session)


if __name__ == "__main__":
    unittest.main()
