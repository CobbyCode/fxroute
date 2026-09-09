#!/usr/bin/env python3
"""Shared process-stop lifecycle: terminate -> grace -> kill -> reap.

Contracts for audio.pw_link.stop_command_child[_cancellation_safe]
(the single owner; dsp/peak_monitor.py delegates to it):

- a SIGTERM-ignoring child is killed after the grace period and reaped
  (returncode -SIGKILL, no zombie);
- None and already-exited processes are cheap no-ops;
- caller cancellation during the grace does not orphan the child: the
  cleanup still runs to completion and the helper reports True (the
  caller owns re-raising CancelledError);
- the peak monitor keeps its deliberate 1.0 s grace (not the 3 s pw-link
  default): a hanging command child behind _run_bounded_command is
  stopped with PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audio.pw_link as pw_link
import dsp.peak_monitor as peak_monitor
from common import process_stop

_IGNORE_TERM_SLEEP = [
    sys.executable,
    "-c",
    "import signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open(sys.argv[1], 'w').write('ready\\n'); time.sleep(30)",
]
# Plain hanging child (no handshake needed: either termination path still
# exercises the timeout cleanup).
_HANG_SLEEP = [
    sys.executable,
    "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
]


async def _spawn_child(*args) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _spawn_term_ignoring_child(tmpdir: str):
    """Spawn a child that ignores SIGTERM; return once its handler is live.

    Without the handshake the test's terminate() could land before the
    child installed its SIGTERM handler (default disposition would kill
    it and the test would observe SIGTERM instead of the SIGKILL path).
    """
    ready = str(Path(tmpdir) / f"ready-{time.monotonic_ns()}")
    proc = await _spawn_child(*_IGNORE_TERM_SLEEP, ready)
    deadline = time.monotonic() + 5.0
    while not Path(ready).exists():
        if time.monotonic() > deadline:
            raise AssertionError("term-ignoring child never became ready")
        await asyncio.sleep(0.01)
    return proc


async def _reap(proc) -> None:
    try:
        if proc.returncode is None:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        pass


class StopCommandChildTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_ignoring_child_is_killed_and_reaped(self):
        tmp = tempfile.mkdtemp(prefix="stop-lifecycle-")
        proc = await _spawn_term_ignoring_child(tmp)
        try:
            await pw_link.stop_command_child(proc, 0.2)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
        finally:
            await _reap(proc)
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_none_and_exited_processes_are_noops(self):
        await pw_link.stop_command_child(None, 0.2)
        proc = await _spawn_child(sys.executable, "-c", "pass")
        await asyncio.wait_for(proc.wait(), timeout=5.0)
        await pw_link.stop_command_child(proc, 0.2)
        self.assertEqual(proc.returncode, 0)

    async def test_cancellation_does_not_orphan_child(self):
        tmp = tempfile.mkdtemp(prefix="stop-lifecycle-")
        proc = await _spawn_term_ignoring_child(tmp)
        try:
            task = asyncio.create_task(
                pw_link.stop_command_child_cancellation_safe(proc, 5.0)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            cancelled = await asyncio.wait_for(task, timeout=8.0)
            self.assertTrue(cancelled)
            self.assertEqual(proc.returncode, -signal.SIGKILL)
        finally:
            await _reap(proc)
            shutil.rmtree(tmp, ignore_errors=True)


class PeakMonitorGraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_bounded_command_timeout_uses_peak_grace(self):
        seen: dict = {}
        real_safe = process_stop.stop_command_child_cancellation_safe

        async def recording_safe(proc, *args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            return await real_safe(proc, *args, **kwargs)

        with patch.object(
            process_stop, "stop_command_child_cancellation_safe", new=recording_safe
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await peak_monitor._run_bounded_command(
                    _HANG_SLEEP, timeout=0.05
                )
        self.assertEqual(seen.get("args"), (1.0,))
        self.assertEqual(
            seen.get("kwargs", {}).get("cleanup_log"),
            "Peak monitor command child cleanup failed",
        )
        self.assertTrue(seen.get("kwargs", {}).get("cleanup_log_exc_info"))

    async def test_bounded_command_timeout_reaps_child(self):
        real_exec = asyncio.create_subprocess_exec
        captured: dict = {}

        async def recording_exec(*args, **kwargs):
            proc = await real_exec(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(asyncio, "create_subprocess_exec", new=recording_exec):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                await peak_monitor._run_bounded_command(
                    _HANG_SLEEP, timeout=0.05
                )
        self.assertEqual(captured["proc"].returncode, -signal.SIGKILL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
