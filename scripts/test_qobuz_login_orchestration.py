# SPDX-License-Identifier: AGPL-3.0-only

"""Regression tests for the qbzd login orchestration banner read and session state.

Two confirmed defects (2026-09-03):

* ``_read_until_url`` kept reading until EOF or the full timeout, even after
  the authorization URL was already seen.  A ``qbzd login --paste`` prints
  the URL and then blocks waiting for the pasted redirect with stdout still
  open, so ``begin_login`` stalled the whole ``BANNER_TIMEOUT`` (15 s) before
  the URL was returned.  It must return as soon as the URL line arrives; the
  timeout only bounds the never-URL case.
* ``begin_login`` answered ``already-in-progress`` forever when the previous
  login process had already exited (an expired session was never cleared).
  A dead session must be reaped automatically so a fresh login can start.

These tests spawn real helper subprocesses; no qbzd binary is required.
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streaming.qobuz import login  # noqa: E402

# Prints the auth URL, then blocks on stdin with stdout still open: exactly
# what a real ``qbzd login --paste`` does while it waits for the redirect.
_URL_ONLY_AND_WAIT = (
    "import sys\n"
    "print('https://play.qobuz.com/login?code=fresh-code-123')\n"
    "sys.stdout.flush()\n"
    "sys.stdin.read()\n"
)

# Blocks without ever printing a URL.
_SILENT_WAIT = "import sys; sys.stdin.read()\n"


class _ExitedProc:
    """Minimal dead-session stand-in: the process already exited."""

    returncode = 1


class _LiveProc:
    """Minimal live-session stand-in: the process is still running."""

    returncode = None


def _spawn_script(code: str, real_spawn):
    """Spawn a helper subprocess that impersonates a qbzd login banner."""

    async def spawn(*args, **kwargs):
        return await real_spawn(sys.executable, "-c", code, **kwargs)

    return spawn


class ReadUntilUrlTests(unittest.IsolatedAsyncioTestCase):
    """The banner read returns on the URL and keeps the timeout as a cap."""

    async def _spawn(self, code: str):
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.PIPE,
        )

    async def test_returns_immediately_when_url_is_seen(self):
        # The URL line arrives instantly, but the process then blocks on
        # stdin with stdout open (qbzd waiting for the pasted URL).  The old
        # implementation only returned when the whole timeout elapsed.
        proc = await self._spawn(_URL_ONLY_AND_WAIT)
        try:
            started = time.monotonic()
            url, collected = await login._read_until_url(proc, timeout=10.0)
            elapsed = time.monotonic() - started
        finally:
            proc.kill()
            await proc.wait()
        expected_url = "https://play.qobuz.com/login?code=fresh-code-123"
        self.assertEqual(url, expected_url)
        self.assertTrue(
            any(expected_url in line for line in collected),
            f"URL not found in collected lines: {collected!r}",
        )
        # Far below the 10 s cap: the read stopped at the URL, not the timeout.
        self.assertLess(elapsed, 3.0)

    async def test_timeout_still_caps_the_no_url_case(self):
        # A process that never prints the URL must not hang forever: the read
        # returns None once the timeout elapsed.
        proc = await self._spawn(_SILENT_WAIT)
        try:
            started = time.monotonic()
            url, collected = await login._read_until_url(proc, timeout=0.5)
            elapsed = time.monotonic() - started
        finally:
            proc.kill()
            await proc.wait()
        self.assertIsNone(url)
        self.assertEqual(collected, [])
        self.assertGreaterEqual(elapsed, 0.4)

    async def test_eof_without_url_returns_collected_lines(self):
        proc = await self._spawn("print('starting qbzd login')\n")
        try:
            url, collected = await login._read_until_url(proc, timeout=2.0)
        finally:
            await proc.wait()
        self.assertIsNone(url)
        self.assertEqual(collected, ["starting qbzd login\n"])


class BeginLoginSessionTests(unittest.IsolatedAsyncioTestCase):
    """Dead sessions are reaped; live sessions still report in progress."""

    async def asyncTearDown(self) -> None:
        session = login._session
        login._session = None
        if session is not None and hasattr(session.proc, "terminate"):
            # Fake stand-ins (_ExitedProc/_LiveProc) have no process to stop.
            await login._terminate(session.proc)

    async def test_begin_login_reaps_expired_session_and_starts_fresh(self):
        # A previous login whose process already exited is still registered;
        # begin_login must clear it and start a new login instead of
        # answering already-in-progress with the stale session forever.
        stale = login._LoginSession(_ExitedProc())
        stale.url = "https://stale.example/old-code"
        login._session = stale
        real_spawn = asyncio.create_subprocess_exec
        with (
            mock.patch.object(login, "qbzd_binary", return_value=sys.executable),
            mock.patch.object(
                login.asyncio,
                "create_subprocess_exec",
                new=_spawn_script(_URL_ONLY_AND_WAIT, real_spawn),
            ),
        ):
            result = await login.begin_login()
        self.assertTrue(result["started"])
        self.assertNotIn("reason", result)
        self.assertIn("fresh-code-123", result["login_url"])
        # The registered session now points at the fresh login, not the stale one.
        self.assertIsNotNone(login._session)
        self.assertIn("fresh-code-123", login._session.url)

    async def test_begin_login_reports_in_progress_for_live_session(self):
        # A genuinely running login keeps answering already-in-progress and
        # never spawns a second qbzd process.
        live = login._LoginSession(_LiveProc())
        live.url = "https://play.qobuz.com/login?code=in-flight"
        login._session = live

        def fail_if_spawned(*args, **kwargs):
            raise AssertionError("begin_login must not spawn over a live session")

        with (
            mock.patch.object(login, "qbzd_binary", return_value=sys.executable),
            mock.patch.object(
                login.asyncio, "create_subprocess_exec", new=fail_if_spawned
            ),
        ):
            result = await login.begin_login()
        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "already-in-progress")
        self.assertEqual(
            result["login_url"], "https://play.qobuz.com/login?code=in-flight"
        )

    async def test_begin_login_fails_fast_when_banner_never_comes(self):
        # No URL within the banner timeout: begin_login raises instead of
        # leaving a half-started session behind.  Uses a short patched
        # timeout so the test does not wait the real 15 s.
        real_spawn = asyncio.create_subprocess_exec
        with (
            mock.patch.object(login, "qbzd_binary", return_value=sys.executable),
            mock.patch.object(login, "BANNER_TIMEOUT", 0.3),
            mock.patch.object(
                login.asyncio,
                "create_subprocess_exec",
                new=_spawn_script(_SILENT_WAIT, real_spawn),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "did not report an authorization URL"
            ):
                await login.begin_login()
        self.assertIsNone(login._session)


if __name__ == "__main__":
    unittest.main()
