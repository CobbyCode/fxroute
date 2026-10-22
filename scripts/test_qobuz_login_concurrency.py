# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic login interleavings at the daemon HTTP boundary (no live qbzd)."""
from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streaming.qobuz import login
import httpx
from fastapi import FastAPI
import streaming
from streaming import api
from streaming.qobuz.provider import QobuzProvider


class LoginConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_session = login._session
        self.session = login._LoginSession("https://signin.example/original", None)
        login._session = self.session
        self.loop = asyncio.get_running_loop()
        self.poll_entered = asyncio.Event()
        self.callback_entered = asyncio.Event()
        self.release_poll = threading.Event()
        self.release_callback = threading.Event()
        self.hold_callback = False
        self.verdict = "pending"
        self.observer_verdict = "pending"
        self.status_reads = 0
        self.callbacks = []
        self.starts = []
        self.tasks = []
        self.patches = [
            mock.patch.object(login, "_login_lock", asyncio.Lock()),
            mock.patch.object(login, "qbzd_binary", return_value="/fake/qbzd"),
            mock.patch.object(login, "_http_get", side_effect=self._get),
            mock.patch.object(login, "_http_post", side_effect=self._post),
            mock.patch.object(login, "OAUTH_STATUS_INTERVAL", 0.005),
            mock.patch.object(login, "FINISH_TIMEOUT", 3.0),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        self.release_poll.set()
        self.release_callback.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for patch in reversed(self.patches):
            patch.stop()
        login._session = self.original_session

    def _get(self, path, timeout):
        if path.startswith("/api/auth/oauth/callback?code="):
            self.callbacks.append(path)
            self.loop.call_soon_threadsafe(self.callback_entered.set)
            if self.hold_callback and not self.release_callback.wait(2):
                raise AssertionError("callback test barrier was not released")
            return "<html>callback</html>"
        if path == "/api/auth/oauth/status":
            self.status_reads += 1
            if self.status_reads == 1:
                self.loop.call_soon_threadsafe(self.poll_entered.set)
                if not self.release_poll.wait(2):
                    raise AssertionError("status test barrier was not released")
                return {"status": self.verdict} if self.verdict is not None else None
            return {"status": self.observer_verdict}
        if path == "/api/status":
            return {"logged_in": False}
        raise AssertionError(f"unexpected daemon read: {path}")

    def _post(self, path, body, timeout):
        self.assertEqual(path, "/api/auth/oauth/start")
        self.starts.append(path)
        return {"oauth_url": "https://signin.example/replacement"}

    def _task(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    async def _prompt(self, awaitable):
        try:
            return await asyncio.wait_for(awaitable, 0.3)
        except TimeoutError:
            self.fail("account operation waited behind the long finish poll")

    async def _polling_finish(self):
        task = self._task(login.finish_login("original-code"))
        await asyncio.wait_for(self.poll_entered.wait(), 1)
        return task

    async def test_state_is_available_while_finish_status_read_is_blocked(self):
        await self._polling_finish()
        self.assertEqual(await self._prompt(login.state()), {
            "in_progress": True, "login_url": self.session.url, "expired": False})
        self.assertIs(login._session, self.session)

    async def test_cancel_is_available_and_old_finish_cannot_report_success(self):
        self.verdict = "success"
        finish = await self._polling_finish()
        self.assertEqual(await self._prompt(login.cancel()), {"cancelled": True, "in_progress": False})
        self.release_poll.set()
        self.assertEqual(await self._prompt(finish), {"ok": False, "status": "cancelled"})
        self.assertIsNone(login._session)
        self.assertEqual(self.starts, [])  # Cancel remains local: no daemon start/reset.

    async def test_begin_preserves_the_inflight_finish_owner(self):
        self.observer_verdict = "idle"
        await self._polling_finish()
        self.assertEqual(await self._prompt(login.begin_login()), {
            "started": False, "reason": "already-in-progress", "login_url": self.session.url})
        self.assertEqual(self.starts, [])
        self.assertIs(login._session, self.session)

    async def test_duplicate_finish_is_rejected_without_a_second_callback(self):
        await self._polling_finish()
        with self.assertRaisesRegex(RuntimeError, "already.*progress"):
            await self._prompt(login.finish_login("duplicate-code"))
        self.assertEqual(len(self.callbacks), 1)
        self.assertIs(login._session, self.session)

    async def test_state_does_not_reap_a_finishing_session_before_its_success(self):
        self.verdict = "success"
        self.observer_verdict = "success"
        finish = await self._polling_finish()
        self.assertTrue((await self._prompt(login.state()))["in_progress"])
        self.release_poll.set()
        self.assertEqual(await self._prompt(finish), {"ok": True, "status": "success"})
        self.assertIsNone(login._session)

    async def _replace_during_poll(self, verdict):
        self.verdict = verdict
        finish = await self._polling_finish()
        self.assertTrue((await self._prompt(login.cancel()))["cancelled"])
        begun = await self._prompt(login.begin_login())
        self.assertTrue(begun["started"])
        replacement = login._session
        self.assertIsNot(replacement, self.session)
        self.release_poll.set()
        self.assertEqual(await self._prompt(finish), {"ok": False, "status": "cancelled"})
        self.assertIs(login._session, replacement)
        self.assertEqual((await login.state())["login_url"], "https://signin.example/replacement")

    async def test_stale_success_does_not_erase_or_authenticate_replacement(self):
        await self._replace_during_poll("success")

    async def test_stale_error_does_not_erase_replacement(self):
        await self._replace_during_poll("error")

    async def test_stale_idle_does_not_erase_replacement(self):
        await self._replace_during_poll("idle")

    async def test_stale_unknown_does_not_keep_polling_or_erase_replacement(self):
        await self._replace_during_poll(None)

    async def test_old_timeout_cleanup_preserves_replacement(self):
        with mock.patch.object(login, "FINISH_TIMEOUT", 0.8):
            finish = await self._polling_finish()
            self.assertTrue((await self._prompt(login.cancel()))["cancelled"])
            self.assertTrue((await self._prompt(login.begin_login()))["started"])
            replacement = login._session
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await asyncio.wait_for(finish, 1.2)
            self.assertIs(login._session, replacement)

    async def test_cancelling_old_finish_task_preserves_replacement(self):
        finish = await self._polling_finish()
        self.assertTrue((await self._prompt(login.cancel()))["cancelled"])
        await self._prompt(login.begin_login())
        replacement = login._session
        finish.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await finish
        self.assertIs(login._session, replacement)

    async def test_cancelling_owned_finish_task_releases_ownership(self):
        finish = await self._polling_finish()
        finish.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await finish
        self.assertIsNone(login._session)
        self.assertTrue((await login.begin_login())["started"])

    async def test_cancel_during_poll_sleep_stops_before_reading_replacement_status(self):
        sleep_entered = asyncio.Event()
        release_sleep = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay):
            if delay == login.OAUTH_STATUS_INTERVAL:
                sleep_entered.set()
                await release_sleep.wait()
            else:
                await real_sleep(delay)

        self.release_poll.set()
        with mock.patch.object(login.asyncio, "sleep", side_effect=controlled_sleep):
            finish = await self._polling_finish()
            await asyncio.wait_for(sleep_entered.wait(), 1)
            await self._prompt(login.cancel())
            await self._prompt(login.begin_login())
            replacement = login._session
            release_sleep.set()
            self.assertEqual(await self._prompt(finish), {"ok": False, "status": "cancelled"})
            self.assertEqual(self.status_reads, 1)
            self.assertIs(login._session, replacement)

    async def test_callback_failure_releases_claim_so_login_can_be_retried(self):
        with mock.patch.object(login, "_http_get", side_effect=RuntimeError("callback failed")):
            with self.assertRaisesRegex(RuntimeError, "callback failed"):
                await login.finish_login("original-code")
        self.assertIsNone(login._session)
        self.assertTrue((await login.begin_login())["started"])

    async def test_logout_during_poll_drops_owner_and_preserves_later_begin(self):
        self.verdict = "success"
        finish = await self._polling_finish()
        with mock.patch.object(login, "_credential_paths", return_value=[]), \
                mock.patch.object(login, "_restart_daemon", new=mock.AsyncMock()) as restart:
            self.assertTrue((await self._prompt(login.logout()))["ok"])
        restart.assert_awaited_once()
        await self._prompt(login.begin_login())
        replacement = login._session
        self.release_poll.set()
        self.assertEqual(await self._prompt(finish), {"ok": False, "status": "cancelled"})
        self.assertIs(login._session, replacement)

    async def test_callback_remains_serialized_before_start_of_replacement(self):
        # Releasing the lock during the callback would let an old code consume a new daemon flow.
        self.hold_callback = True
        self.verdict = "success"
        finish = self._task(login.finish_login("original-code"))
        await asyncio.wait_for(self.callback_entered.wait(), 1)
        cancel = self._task(login.cancel())
        begin = self._task(login.begin_login())
        await asyncio.sleep(0)
        self.assertFalse(cancel.done())
        self.assertFalse(begin.done())
        self.assertEqual(self.starts, [])
        self.release_poll.set()
        self.release_callback.set()
        await asyncio.wait_for(cancel, 1)
        self.assertTrue((await asyncio.wait_for(begin, 1))["started"])
        await asyncio.wait_for(finish, 1)

    async def test_owned_timeout_clears_finished_session(self):
        with mock.patch.object(login, "FINISH_TIMEOUT", 0.02):
            finish = await self._polling_finish()
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await asyncio.wait_for(finish, 1)
        self.assertIsNone(login._session)

    async def test_http_state_and_cancel_use_real_provider_while_finish_is_pending(self):
        app = FastAPI()
        app.include_router(api.router)
        provider = QobuzProvider()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with mock.patch.object(streaming, "get_provider", return_value=provider), \
                    mock.patch.object(provider, "is_authenticated", new=mock.AsyncMock(return_value=False)), \
                    mock.patch.object(provider, "is_installed", return_value=True), \
                    mock.patch.object(api, "_deps", return_value=SimpleNamespace(request_origin_is_trusted=lambda _: True)):
                finish = self._task(client.post("/api/streaming/qobuz/auth/login/finish", json={"code": "original-code"}))
                await asyncio.wait_for(self.poll_entered.wait(), 1)
                state = await self._prompt(client.get("/api/streaming/qobuz/auth/state"))
                self.assertEqual(state.status_code, 200)
                self.assertTrue(state.json()["login"]["in_progress"])
                cancelled = await self._prompt(client.post("/api/streaming/qobuz/auth/login/cancel"))
                self.assertTrue(cancelled.json()["cancelled"])
                self.release_poll.set()
                result = await self._prompt(finish)
                self.assertEqual(result.status_code, 200)
                self.assertFalse(result.json()["ok"])
                self.assertEqual(result.json()["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
