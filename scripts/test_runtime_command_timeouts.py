#!/usr/bin/env python3
"""Regression tests: bounded runtime commands (Subwoofer / Peak-Monitor / pactl).

Every hanging command is simulated with a real sleeping child spawned through
the same ``asyncio.create_subprocess_exec`` call site the production code
looks up (patched only at the module attribute).  Timeout and grace constants
are patched small so the suite stays fast; production values remain 5 s
(subwoofer), 3 s (peak monitor / pactl).

Invariant under test: success, timeout, and caller cancellation must never
leave a command child alive, and lock holders must release ownership.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import peak_monitor
import subwoofer_runtime

from peak_monitor import EasyEffectsPeakMonitor, MonitorTarget
from subwoofer_runtime import Subwoofer21Runtime

_REAL_EXEC = asyncio.create_subprocess_exec

SIGTERM_IGNORER = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(60)",
]


async def _spawn_sleeper(*args, **kwargs):
    return await _REAL_EXEC(
        sys.executable, "-c", "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _spawn_sigterm_ignorer(*args, **kwargs):
    """Spawn a child that ignores SIGTERM and confirm the handler is installed."""
    proc = await _REAL_EXEC(
        *SIGTERM_IGNORER,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.stdout.readline()
    return proc


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class _DummyProc:
    returncode = None


def _assert_reaped(testcase, proc) -> None:
    testcase.assertIsNotNone(proc.returncode, "child must be killed and reaped")
    with testcase.assertRaises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


class SubwooferBoundedCommandTests(unittest.IsolatedAsyncioTestCase):
    """P2 fix: subwoofer_runtime._run_command is bounded to 5 s."""

    async def test_A_timeout_returns_command_failure_and_frees_sync_lock(self):
        runtime = Subwoofer21Runtime()
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sleeper(*args, **kwargs)
            captured["proc"] = proc
            return proc

        async def locked_caller():
            async with runtime._sync_lock:
                return await Subwoofer21Runtime._run_command(["pw-link", "-l"])

        with patch.object(subwoofer_runtime.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(subwoofer_runtime, "RUNTIME_COMMAND_TIMEOUT_SECONDS", 0.2), \
             patch.object(subwoofer_runtime, "RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS", 0.05):
            started = time.monotonic()
            result = await asyncio.wait_for(locked_caller(), timeout=2.0)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "bounded call must return promptly")
        self.assertNotEqual(
            result.returncode, 0,
            "timeout must behave like a command failure, not success",
        )
        self.assertIn("timed out", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(runtime._sync_lock.locked(), "lock must be released after timeout")

        async with runtime._sync_lock:
            pass  # a second caller can acquire the lock again

        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        _assert_reaped(self, proc)

    async def test_B_cancellation_reaps_child_and_frees_sync_lock(self):
        runtime = Subwoofer21Runtime()
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sleeper(*args, **kwargs)
            captured["proc"] = proc
            return proc

        async def locked_caller():
            async with runtime._sync_lock:
                await Subwoofer21Runtime._run_command(["pw-link", "-l"])

        with patch.object(subwoofer_runtime.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(subwoofer_runtime, "RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS", 0.05):
            task = asyncio.create_task(locked_caller())
            await asyncio.sleep(0.15)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(runtime._sync_lock.locked(), "lock must be released after cancellation")
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        _assert_reaped(self, proc)

    async def test_B2_cancellation_with_sigterm_ignorer_escalates_to_kill(self):
        """SIGTERM-ignoring child: cancel -> grace expires -> kill -> reap."""
        runtime = Subwoofer21Runtime()
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sigterm_ignorer(*args, **kwargs)
            captured["proc"] = proc
            return proc

        async def locked_caller():
            async with runtime._sync_lock:
                await Subwoofer21Runtime._run_command(["pw-link", "-l"])

        with patch.object(subwoofer_runtime.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(subwoofer_runtime, "RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS", 0.3):
            task = asyncio.create_task(locked_caller())
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        self.assertEqual(proc.returncode, -9, "SIGTERM-survivor must be killed (SIGKILL)")
        _assert_reaped(self, proc)
        self.assertFalse(runtime._sync_lock.locked(), "lock must be released after cancellation")
        async with runtime._sync_lock:
            pass  # a second caller can acquire the lock again

    async def test_timeout_with_sigterm_ignorer_escalates_to_kill(self):
        """Timeout path: terminate ignored -> grace expires -> kill -> drain -> failure."""
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sigterm_ignorer(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(subwoofer_runtime.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(subwoofer_runtime, "RUNTIME_COMMAND_TIMEOUT_SECONDS", 0.2), \
             patch.object(subwoofer_runtime, "RUNTIME_COMMAND_TERMINATE_GRACE_SECONDS", 0.2):
            started = time.monotonic()
            result = await asyncio.wait_for(
                Subwoofer21Runtime._run_command(["pw-link", "-l"]),
                timeout=5.0,
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "timeout + escalation must stay bounded")
        self.assertNotEqual(result.returncode, 0, "timeout must be a command failure")
        self.assertIn("timed out", result.stderr)
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        self.assertEqual(proc.returncode, -9, "SIGTERM-survivor must be killed (SIGKILL)")
        _assert_reaped(self, proc)

    async def test_G_success_passthrough_unchanged(self):
        result = await Subwoofer21Runtime._run_command(
            [sys.executable, "-c", "print('out'); import sys; print('err', file=sys.stderr)"]
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "out\n")
        self.assertEqual(result.stderr, "err\n")


class PeakMonitorBoundedCommandTests(unittest.IsolatedAsyncioTestCase):
    """P2 fix: pw-cli/pw-link calls run under a 3 s local command bound."""

    def _monitor(self) -> EasyEffectsPeakMonitor:
        monitor = EasyEffectsPeakMonitor(on_change=None)
        monitor._running = True
        monitor._capture_node_name = "fxroute_pm_capture_x"
        return monitor

    async def test_C_single_hanging_pw_cli_beats_deadline_and_reaps(self):
        monitor = self._monitor()
        monitor._proc = _DummyProc()
        target = MonitorTarget("ee_soe_output_level", 42, "Output Level")
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sleeper(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(peak_monitor.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(peak_monitor, "PEAK_MONITOR_COMMAND_TIMEOUT_SECONDS", 0.2), \
             patch.object(peak_monitor, "PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS", 0.05):
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                await asyncio.wait_for(
                    monitor._link_capture_stream(target, monitor._capture_node_name),
                    timeout=2.0,
                )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "command timeout must fire before the discovery deadline")
        self.assertIn("timed out", str(ctx.exception))
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        _assert_reaped(self, proc)

    async def test_D_pw_link_cancellation_reaps_and_frees_transition_lock(self):
        monitor = self._monitor()
        captured: dict[str, object] = {}
        original_lock = main.peak_monitor_transition_lock
        main.peak_monitor_transition_lock = asyncio.Lock()

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sleeper(*args, **kwargs)
            captured["proc"] = proc
            return proc

        async def locked_caller():
            async with main.peak_monitor_transition_lock:
                await monitor._run_link("a:output_FL", "b:input_FL")

        try:
            with patch.object(peak_monitor.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
                 patch.object(peak_monitor, "PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS", 0.05):
                task = asyncio.create_task(locked_caller())
                await asyncio.sleep(0.15)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(
                main.peak_monitor_transition_lock.locked(),
                "transition lock must not stay held after cancellation",
            )
            async with main.peak_monitor_transition_lock:
                pass  # a second caller can acquire the lock again
            proc = captured.get("proc")
            self.assertIsNotNone(proc)
            _assert_reaped(self, proc)
        finally:
            main.peak_monitor_transition_lock = original_lock

    async def test_D2_pw_link_cancellation_with_sigterm_ignorer_escalates_to_kill(self):
        """SIGTERM-ignoring child: cancel -> grace expires -> kill -> reap,
        transition lock released."""
        monitor = self._monitor()
        captured: dict[str, object] = {}
        original_lock = main.peak_monitor_transition_lock
        main.peak_monitor_transition_lock = asyncio.Lock()

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sigterm_ignorer(*args, **kwargs)
            captured["proc"] = proc
            return proc

        async def locked_caller():
            async with main.peak_monitor_transition_lock:
                await monitor._run_link("a:output_FL", "b:input_FL")

        try:
            with patch.object(peak_monitor.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
                 patch.object(peak_monitor, "PEAK_MONITOR_COMMAND_TERMINATE_GRACE_SECONDS", 0.3):
                task = asyncio.create_task(locked_caller())
                await asyncio.sleep(0.2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5.0)
            proc = captured.get("proc")
            self.assertIsNotNone(proc)
            self.assertEqual(proc.returncode, -9, "SIGTERM-survivor must be killed (SIGKILL)")
            _assert_reaped(self, proc)
            self.assertFalse(
                main.peak_monitor_transition_lock.locked(),
                "transition lock must not stay held after cancellation",
            )
            async with main.peak_monitor_transition_lock:
                pass  # a second caller can acquire the lock again
        finally:
            main.peak_monitor_transition_lock = original_lock

    async def test_G_success_passthrough_unchanged(self):
        returncode, stdout, stderr = await peak_monitor._run_bounded_command(
            [sys.executable, "-c", "print('out')"]
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout, b"out\n")
        self.assertEqual(stderr, b"")


class PactlBoundedCommandTests(unittest.IsolatedAsyncioTestCase):
    """P2 fix: main._run_pactl_command is bounded to 3 s."""

    async def test_E_timeout_is_bounded_failure_and_reaps(self):
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sleeper(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(main, "_PACTL_COMMAND_TIMEOUT_SECONDS", 0.2), \
             patch.object(main, "_PACTL_TERMINATE_GRACE_SECONDS", 0.05):
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                await asyncio.wait_for(
                    main._run_pactl_command("unload-module", "123"),
                    timeout=2.0,
                )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertIn("timed out", str(ctx.exception))
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        _assert_reaped(self, proc)

    async def test_F_cancellation_reaps_and_propagates(self):
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sleeper(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(main, "_PACTL_TERMINATE_GRACE_SECONDS", 0.05):
            task = asyncio.create_task(main._run_pactl_command("unload-module", "123"))
            await asyncio.sleep(0.15)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        _assert_reaped(self, proc)

    async def test_F2_cancellation_with_sigterm_ignorer_escalates_to_kill(self):
        """SIGTERM-ignoring child: cancel -> grace expires -> kill -> reap."""
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sigterm_ignorer(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(main, "_PACTL_TERMINATE_GRACE_SECONDS", 0.3):
            task = asyncio.create_task(main._run_pactl_command("unload-module", "123"))
            await asyncio.sleep(0.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        self.assertEqual(proc.returncode, -9, "SIGTERM-survivor must be killed (SIGKILL)")
        _assert_reaped(self, proc)

    async def test_double_cancel_during_grace_does_not_abort_cleanup(self):
        """Contract: cancel #1 starts cleanup, cancel #2 during the grace
        period must not interrupt terminate/grace/kill/pipe-drain; the child
        is still reaped and the caller still ends with CancelledError."""
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sigterm_ignorer(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(main, "_PACTL_TERMINATE_GRACE_SECONDS", 1.0):
            task = asyncio.create_task(main._run_pactl_command("unload-module", "123"))
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.sleep(0.2)  # cleanup is inside its grace window
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        self.assertEqual(proc.returncode, -9, "double-cancelled cleanup must still kill the child")
        _assert_reaped(self, proc)
        self.assertTrue(
            proc.stdout.at_eof(),
            "stdout pipe must be drained terminally even under double cancellation",
        )

    async def test_timeout_with_sigterm_ignorer_escalates_to_kill(self):
        """Timeout path: terminate ignored -> grace expires -> kill -> drain
        -> the established best-effort timeout failure."""
        captured: dict[str, object] = {}

        async def spawn_and_capture(*args, **kwargs):
            proc = await _spawn_sigterm_ignorer(*args, **kwargs)
            captured["proc"] = proc
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", new=spawn_and_capture), \
             patch.object(main, "_PACTL_COMMAND_TIMEOUT_SECONDS", 0.2), \
             patch.object(main, "_PACTL_TERMINATE_GRACE_SECONDS", 0.2):
            started = time.monotonic()
            with self.assertRaises(RuntimeError) as ctx:
                await asyncio.wait_for(
                    main._run_pactl_command("unload-module", "123"),
                    timeout=5.0,
                )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, "timeout + escalation must stay bounded")
        self.assertIn("timed out", str(ctx.exception))
        proc = captured.get("proc")
        self.assertIsNotNone(proc)
        self.assertEqual(proc.returncode, -9, "SIGTERM-survivor must be killed (SIGKILL)")
        _assert_reaped(self, proc)

    async def test_G_success_passthrough_unchanged(self):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(0, b"stdout text\n", b"")

        with patch.object(main.asyncio, "create_subprocess_exec", new=fake_exec):
            output = await main._run_pactl_command("list", "short", "sinks")
        self.assertEqual(output, "stdout text")


if __name__ == "__main__":
    unittest.main()
