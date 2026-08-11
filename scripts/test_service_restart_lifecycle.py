#!/usr/bin/env python3
"""Regression tests: bounded non-blocking deferred FXRoute service restart.

P2 fix: _restart_fxroute_service_after_response() hands the restart job to
systemd --user via ``systemctl --user --no-block restart`` and bounds the
systemctl client instead of awaiting it unboundedly.

Covered against controlled stand-in children (never the real systemctl):

- exact command contract (argv + DEVNULL streams)
- fast rc=0 success stays silent and completes normally
- rc!=0 is logged, never raised, and the 0.8 s delay is kept
- a hanging systemctl client is bounded: terminate -> grace -> kill -> reap,
  logged as "restart outcome unknown", task ends normally
- a SIGTERM-ignoring client is escalated to SIGKILL and reaped
- cancellation runs the shielded terminal cleanup and re-raises
  CancelledError; a second cancellation during the grace period still
  reaps the child
- the already sent update/restore response payload is unchanged while the
  background restart times out

Timeouts and grace periods are patched small; production values remain
15 s (client timeout) and 3 s (grace).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main

_REAL_EXEC = asyncio.create_subprocess_exec

_HANG = "import time; time.sleep(3600)"
_FAST = "import sys; sys.exit(0)"
_FAIL = "import sys; sys.exit(3)"


class _FakeProc:
    def __init__(self, returncode: int):
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def _assert_reaped(testcase, proc) -> None:
    testcase.assertIsNotNone(proc.returncode, "child must be killed and reaped")
    with testcase.assertRaises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class ServiceRestartLifecycleTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="fxroute-restart-test-"))
        self._spawned: list[asyncio.subprocess.Process] = []

    def tearDown(self):
        for proc in self._spawned:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _spawn_standin(self, code: str, captured: dict):
        async def spawn(*args, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = kwargs
            proc = await _REAL_EXEC(sys.executable, "-c", code, **kwargs)
            captured["proc"] = proc
            self._spawned.append(proc)
            return proc

        return spawn

    def _spawn_term_ignorer(self, captured: dict):
        marker = self._tmp / "termignorer.ready"
        script = self._tmp / "termignorer.py"
        script.write_text(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(marker)!r}, 'w').write('ready\\n')\n"
            "time.sleep(3600)\n"
        )
        self._termignorer_marker = marker

        async def spawn(*args, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = kwargs
            proc = await _REAL_EXEC(sys.executable, str(script), **kwargs)
            captured["proc"] = proc
            self._spawned.append(proc)
            return proc

        return spawn

    async def _await_spawned(self, captured: dict) -> None:
        deadline = time.monotonic() + 8
        while "proc" not in captured and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertIn("proc", captured, "stand-in child was never spawned")

    async def _wait_ready(self, path: Path, timeout: float = 6.0) -> None:
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        self.assertTrue(path.exists(), "stand-in never became ready")

    async def test_A_command_contract_and_silent_success(self):
        captured: dict = {}

        async def fake_exec(*args, **kwargs):
            captured["args"] = list(args)
            captured["kwargs"] = kwargs
            return _FakeProc(returncode=0)

        with patch.object(main.asyncio, "create_subprocess_exec", new=fake_exec), \
             patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 5.0):
            task = asyncio.create_task(
                main._restart_fxroute_service_after_response("fxroute")
            )
            result = await asyncio.wait_for(task, timeout=6.0)
        self.assertIsNone(result)
        self.assertEqual(captured["args"], [
            "systemctl", "--user", "--no-block", "restart", "fxroute.service",
        ])
        self.assertIs(captured["kwargs"]["stdout"], asyncio.subprocess.DEVNULL)
        self.assertIs(captured["kwargs"]["stderr"], asyncio.subprocess.DEVNULL)

    async def test_B_fast_success_with_real_child_no_warning(self):
        captured: dict = {}
        handler = _Capture()
        main.logger.addHandler(handler)
        try:
            with patch.object(
                main.asyncio, "create_subprocess_exec",
                new=self._spawn_standin(_FAST, captured),
            ), patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 5.0):
                t0 = time.monotonic()
                task = asyncio.create_task(
                    main._restart_fxroute_service_after_response("fxroute")
                )
                result = await asyncio.wait_for(task, timeout=6.0)
                elapsed = time.monotonic() - t0
        finally:
            main.logger.removeHandler(handler)
        self.assertIsNone(result)
        self.assertGreaterEqual(elapsed, 0.8, "0.8 s delay must be kept")
        self.assertEqual(captured["args"], [
            "systemctl", "--user", "--no-block", "restart", "fxroute.service",
        ])
        self.assertEqual(captured["proc"].returncode, 0)
        self.assertEqual(handler.records, [], "fast success must stay silent")
        _assert_reaped(self, captured["proc"])

    async def test_C_nonzero_exit_logged_but_not_raised(self):
        captured: dict = {}
        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_standin(_FAIL, captured),
        ), patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 5.0), \
                self.assertLogs("main", level="WARNING") as logs:
            task = asyncio.create_task(
                main._restart_fxroute_service_after_response("fxroute")
            )
            result = await asyncio.wait_for(task, timeout=6.0)
        self.assertIsNone(result, "nonzero exit must not raise")
        self.assertEqual(captured["proc"].returncode, 3)
        self.assertTrue(
            any("exited with code 3" in r.getMessage() for r in logs.records),
            "nonzero exit must be logged",
        )

    async def test_D_timeout_terminates_and_reaps_child(self):
        captured: dict = {}
        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_standin(_HANG, captured),
        ), patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 0.4), \
                patch.object(main, "_SERVICE_RESTART_TERMINATE_GRACE_SECONDS", 0.3), \
                self.assertLogs("main", level="WARNING") as logs:
            task = asyncio.create_task(
                main._restart_fxroute_service_after_response("fxroute")
            )
            await self._await_spawned(captured)
            result = await asyncio.wait_for(task, timeout=6.0)
        self.assertIsNone(result, "timeout must end the task normally")
        proc = captured["proc"]
        self.assertEqual(proc.returncode, -15, "hanging child must be SIGTERMed")
        _assert_reaped(self, proc)
        self.assertTrue(
            any("timed out" in r.getMessage() for r in logs.records),
            "timeout must be logged as outcome unknown",
        )

    async def test_E_sigterm_ignoring_child_escalated_to_kill(self):
        captured: dict = {}
        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_term_ignorer(captured),
        ), patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 0.2), \
                patch.object(main, "_SERVICE_RESTART_TERMINATE_GRACE_SECONDS", 0.2), \
                self.assertLogs("main", level="WARNING") as logs:
            task = asyncio.create_task(
                main._restart_fxroute_service_after_response("fxroute")
            )
            await self._await_spawned(captured)
            await self._wait_ready(self._termignorer_marker)
            result = await asyncio.wait_for(task, timeout=6.0)
        self.assertIsNone(result)
        proc = captured["proc"]
        self.assertEqual(proc.returncode, -9, "SIGTERM-survivor must be SIGKILLed")
        _assert_reaped(self, proc)
        self.assertTrue(any("timed out" in r.getMessage() for r in logs.records))

    async def test_F_cancellation_cleans_up_child_and_reraises(self):
        captured: dict = {}
        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_standin(_HANG, captured),
        ), patch.object(main, "_SERVICE_RESTART_TERMINATE_GRACE_SECONDS", 0.3):
            task = asyncio.create_task(
                main._restart_fxroute_service_after_response("fxroute")
            )
            await self._await_spawned(captured)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=6.0)
        proc = captured["proc"]
        self.assertEqual(proc.returncode, -15)
        _assert_reaped(self, proc)

    async def test_F2_double_cancellation_during_grace_still_reaps(self):
        captured: dict = {}
        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_term_ignorer(captured),
        ), patch.object(main, "_SERVICE_RESTART_TERMINATE_GRACE_SECONDS", 0.5):
            task = asyncio.create_task(
                main._restart_fxroute_service_after_response("fxroute")
            )
            await self._await_spawned(captured)
            await self._wait_ready(self._termignorer_marker)
            task.cancel()
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=6.0)
        proc = captured["proc"]
        self.assertEqual(proc.returncode, -9, "cleanup must survive double cancellation")
        _assert_reaped(self, proc)

    async def test_G_update_response_unchanged_while_restart_times_out(self):
        payload = {
            "returncode": 0,
            "stdout": "Pulling updates with fast-forward only.\n",
            "stderr": "",
        }
        captured: dict = {}

        async def fake_update(timeout: float, *args: str) -> dict:
            return dict(payload)

        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_standin(_HANG, captured),
        ), patch.object(main, "_run_update_operation", new=fake_update), \
                patch.object(main, "_read_version_file", return_value="9.9.9"), \
                patch.object(main, "_configured_service_name", return_value="fxroute"), \
                patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 0.3), \
                patch.object(main, "_SERVICE_RESTART_TERMINATE_GRACE_SECONDS", 0.3), \
                self.assertLogs("main", level="WARNING") as logs:
            t0 = time.monotonic()
            resp = await main.system_update()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(1.6)
            background = [
                t for t in asyncio.all_tasks()
                if t.get_coro() is not None
                and t.get_coro().__qualname__ == "_restart_fxroute_service_after_response"
            ]
        self.assertLess(elapsed, 0.7, "endpoint must not await the deferred restart")
        self.assertEqual(resp, {
            "ok": True,
            "installed_version": "9.9.9",
            "restart_scheduled": True,
            "service_name": "fxroute",
            **payload,
        })
        self.assertTrue(any("timed out" in r.getMessage() for r in logs.records))
        for task in background:
            self.assertTrue(task.done(), "background restart must finish")
            self.assertIsNone(task.exception(), "no unhandled background exception")
        _assert_reaped(self, captured["proc"])

    async def test_G2_restore_response_unchanged_while_restart_times_out(self):
        payload = {"returncode": 0, "stdout": "", "stderr": ""}
        captured: dict = {}

        async def fake_update(timeout: float, *args: str) -> dict:
            return dict(payload)

        with patch.object(
            main.asyncio, "create_subprocess_exec",
            new=self._spawn_standin(_HANG, captured),
        ), patch.object(main, "_run_update_operation", new=fake_update), \
                patch.object(main, "_read_version_file", return_value="9.9.9"), \
                patch.object(main, "_configured_service_name", return_value="fxroute"), \
                patch.object(main, "_SERVICE_RESTART_TIMEOUT_SECONDS", 0.3), \
                patch.object(main, "_SERVICE_RESTART_TERMINATE_GRACE_SECONDS", 0.3), \
                self.assertLogs("main", level="WARNING") as logs:
            t0 = time.monotonic()
            resp = await main.system_restore()
            elapsed = time.monotonic() - t0
            await asyncio.sleep(1.6)
            background = [
                t for t in asyncio.all_tasks()
                if t.get_coro() is not None
                and t.get_coro().__qualname__ == "_restart_fxroute_service_after_response"
            ]
        self.assertLess(elapsed, 0.7, "endpoint must not await the deferred restart")
        self.assertEqual(resp, {
            "ok": True,
            "installed_version": "9.9.9",
            "restart_scheduled": True,
            "service_name": "fxroute",
            **payload,
        })
        self.assertTrue(any("timed out" in r.getMessage() for r in logs.records))
        for task in background:
            self.assertTrue(task.done(), "background restart must finish")
            self.assertIsNone(task.exception(), "no unhandled background exception")
        _assert_reaped(self, captured["proc"])

    async def test_G3_update_failure_keeps_restart_unscheduled(self):
        payload = {
            "returncode": 1,
            "stdout": "",
            "stderr": "git fetch failed",
        }

        async def fake_update(timeout: float, *args: str) -> dict:
            return dict(payload)

        with patch.object(main, "_run_update_operation", new=fake_update), \
                patch.object(main, "_read_version_file", return_value="9.9.9"), \
                patch.object(main, "_configured_service_name", return_value="fxroute"):
            resp = await main.system_update()
        self.assertEqual(resp, {
            "ok": False,
            "installed_version": "9.9.9",
            "restart_scheduled": False,
            "service_name": "fxroute",
            **payload,
        })
        await asyncio.sleep(0.1)
        self.assertEqual(
            [t for t in asyncio.all_tasks()
             if t.get_coro() is not None
             and t.get_coro().__qualname__ == "_restart_fxroute_service_after_response"],
            [],
            "failed update must not schedule a restart",
        )


if __name__ == "__main__":
    unittest.main()
