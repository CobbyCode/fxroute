# SPDX-License-Identifier: AGPL-3.0-only
"""A PKCE login exchange must not outlive its caller or a newer auth intent."""

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


class PkceLoginLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
            is_pkce=True, country_code="US", user=SimpleNamespace(id=3, email="u@example.com"),
            check_login=lambda: True,
        )
        self.session.pkce_login_url = lambda: "https://login.tidal.com/pkce"

        def exchange(session, redirect_url):
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("exchange test timed out")
            return auth._session_payload(session)

        exchange_patch = mock.patch.object(auth.TidalSession, "_exchange_pkce_login", staticmethod(exchange))
        session_patch = mock.patch.object(auth, "_new_session", return_value=self.session)
        tidal_patch = mock.patch.object(auth, "tidalapi", object())
        for patcher in (exchange_patch, session_patch, tidal_patch):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        self.release.set()

    async def _start_waiting(self):
        await self.manager.pkce_login_url_async()
        task = asyncio.create_task(self.manager.finish_pkce_login_async("https://oops?code=1"))
        await asyncio.wait_for(asyncio.to_thread(self.entered.wait, 2), 3)
        return task

    async def test_cancelled_exchange_cannot_commit_after_worker_returns(self):
        task = await self._start_waiting()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.release.set()
        await asyncio.sleep(0.05)
        self.assertIsNone(self.manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    async def test_logout_does_not_wait_for_the_exchange_and_invalidates_its_result(self):
        task = await self._start_waiting()
        await asyncio.wait_for(self.manager.clear(), 1)
        self.release.set()
        with self.assertRaises(auth.TidalAuthError):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(self.manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    async def test_newer_login_invalidates_the_old_exchange(self):
        task = await self._start_waiting()
        await asyncio.wait_for(self.manager.pkce_login_url_async(), 1)
        self.release.set()
        with self.assertRaises(auth.TidalAuthError):
            await asyncio.wait_for(task, 2)
        self.assertIsNone(self.manager._session)
        self.assertFalse(auth.SESSION_FILE.exists())

    async def test_cancelled_start_cannot_restore_pending_after_logout(self):
        creating = threading.Event()
        release_create = threading.Event()
        real_create = self.manager._create_pkce_login

        def delayed_create():
            creating.set()
            if not release_create.wait(5):
                raise RuntimeError("start test timed out")
            return real_create()

        with mock.patch.object(self.manager, "_create_pkce_login", side_effect=delayed_create):
            task = asyncio.create_task(self.manager.pkce_login_url_async())
            await asyncio.wait_for(asyncio.to_thread(creating.wait, 2), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            await asyncio.wait_for(self.manager.clear(), 1)
            release_create.set()
            await asyncio.sleep(0.05)
        self.assertIsNone(self.manager._pending_session)


if __name__ == "__main__":
    unittest.main()
