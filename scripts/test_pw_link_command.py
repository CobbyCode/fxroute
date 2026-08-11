#!/usr/bin/env python3
"""Bounded pw-link diagnostic command tests.

``main._run_pw_link_command`` must never block a request indefinitely:
``communicate()`` is bounded by a timeout, and a timed-out child is fully
terminated and reaped (terminate -> bounded grace -> kill fallback).  No
real PipeWire daemon is needed; the normal path uses a fake child, the
timeout paths use real short-lived child processes.
"""

import asyncio
import os
import signal
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main


class FakeProc:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self.returncode = None
        self._hang = hang
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    async def communicate(self):
        # A hanging child models one that ignores SIGTERM: communicate only
        # returns after the child is killed (like real killed pipes reaching
        # EOF); until then it never completes.
        if self._hang and not self.killed:
            await asyncio.Event().wait()
        self.returncode = self.returncode if self.killed else self._returncode
        return self._stdout, self._stderr

    async def wait(self):
        self.wait_calls += 1
        self.returncode = self.returncode if self.killed else self._returncode
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9


class RunPwLinkCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_fast_path_unchanged(self):
        proc = FakeProc(stdout=b"node:1\n  |-> target\n", returncode=0)
        with patch.object(main.asyncio, "create_subprocess_exec", return_value=proc):
            result = await main._run_pw_link_command("-l")
        self.assertEqual(result, "node:1\n  |-> target")
        self.assertFalse(proc.terminated)
        self.assertFalse(proc.killed)
        self.assertEqual(proc.wait_calls, 0)

    async def test_nonzero_exit_path_unchanged(self):
        proc = FakeProc(stdout=b"", stderr=b"connection refused", returncode=1)
        with patch.object(main.asyncio, "create_subprocess_exec", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                await main._run_pw_link_command("-l")
        self.assertIn("connection refused", str(ctx.exception))

    async def test_nonzero_exit_with_empty_stderr_falls_back_to_command_name(self):
        proc = FakeProc(returncode=2)
        with patch.object(main.asyncio, "create_subprocess_exec", return_value=proc):
            with self.assertRaises(RuntimeError) as ctx:
                await main._run_pw_link_command("-io")
        self.assertIn("pw-link -io failed", str(ctx.exception))

    async def test_hanging_communicate_times_out_within_bound(self):
        proc = FakeProc(hang=True)
        started = asyncio.get_event_loop().time()
        with patch.object(main.asyncio, "create_subprocess_exec", return_value=proc), \
                patch.object(main, "_PW_LINK_COMMAND_TIMEOUT_SECONDS", 0.2), \
                patch.object(main, "_PW_LINK_TERMINATE_GRACE_SECONDS", 0.2):
            with self.assertRaises(RuntimeError) as ctx:
                await main._run_pw_link_command("-l")
        elapsed = asyncio.get_event_loop().time() - started
        self.assertIn("timed out", str(ctx.exception))
        self.assertLess(elapsed, 2.0, "timeout must fire within the defined bound")
        self.assertTrue(proc.terminated, "timed-out child must be terminated")
        self.assertTrue(
            proc.killed,
            "a child that ignores terminate must be escalated to kill",
        )
        self.assertIsNotNone(
            proc.returncode, "timed-out child must be reaped after kill"
        )

    async def test_timeout_with_real_process_terminates_and_reaps(self):
        real_exec = asyncio.create_subprocess_exec
        captured = {}

        async def fake_exec(*args, **kwargs):
            proc = await real_exec("sleep", "30", stdout=asyncio.subprocess.PIPE,
                                   stderr=asyncio.subprocess.PIPE)
            captured["proc"] = proc
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", fake_exec), \
                patch.object(main, "_PW_LINK_COMMAND_TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(RuntimeError):
                await main._run_pw_link_command("-l")
        proc = captured["proc"]
        self.assertIsNotNone(proc.returncode, "child must be reaped, not left running")
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)


class StopPwLinkProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_noop_for_already_exited_process(self):
        proc = FakeProc(returncode=0)
        proc.returncode = 0  # simulate a process that already exited
        await main._stop_pw_link_process(proc)
        self.assertFalse(proc.terminated)
        self.assertEqual(proc.wait_calls, 0)

    async def test_terminate_then_wait_for_cooperative_process(self):
        proc = await asyncio.create_subprocess_exec(
            "sleep", "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await main._stop_pw_link_process(proc)
        self.assertIsNotNone(proc.returncode, "process must be fully reaped")
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)

    async def test_kill_fallback_for_term_surviving_process(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.stdout.readline()  # wait until the SIGTERM ignore is installed
        with patch.object(main, "_PW_LINK_TERMINATE_GRACE_SECONDS", 0.2):
            await main._stop_pw_link_process(proc)
        self.assertEqual(proc.returncode, -signal.SIGKILL, "SIGTERM survivor must be killed")
        with self.assertRaises(ChildProcessError):
            os.waitpid(proc.pid, os.WNOHANG)

    async def test_no_lingering_asyncio_tasks_after_timeout_reap(self):
        before = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
        proc = await asyncio.create_subprocess_exec(
            "sleep", "30",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await main._stop_pw_link_process(proc)
        await asyncio.sleep(0.05)
        after = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
        self.assertLessEqual(after, before, "no background tasks may leak")


if __name__ == "__main__":
    unittest.main()
