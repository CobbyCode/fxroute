#!/usr/bin/env python3
"""Regression tests: bounded, cancellation-safe update process group.

Covers the runtime update path (GET/POST /api/system/update,
POST /api/system/restore -> _run_update_operation -> _run_update_script ->
scripts/update_fxroute.sh) against controlled shell -> python child trees:

- success keeps the result shape and leaves no processes behind
- overall timeout returns the established failure result and terminates
  the whole process group (TERM -> grace -> group SIGKILL -> drain -> reap)
- a SIGTERM-ignoring grandchild is caught by the group SIGKILL even when
  the shell already exited
- caller cancellation and double cancellation run the same terminal
  cleanup and re-raise CancelledError
- parallel operations (including check-vs-update) are rejected with 409
  and the guard is released after success, timeout and cancellation

Timeouts and grace periods are patched small; production values stay
90 s (check), 900 s (update/restore) and 5 s (grace).
"""

from __future__ import annotations

import asyncio
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

from fastapi import HTTPException


_REAL_EXEC = asyncio.create_subprocess_exec


def _write_tree(root: Path, *, child_sleep: float, grand_sleep: float = 60.0,
                term_ignore: bool = False, parent_exits: bool = False) -> Path:
    """Write a shell -> python child -> python grandchild fixture tree."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "grandchild.py").write_text(
        "import os, signal, sys, time\n"
        "if '--ignore-term' in sys.argv:\n"
        "    def _on_term(signum, frame):\n"
        f"        open({str(root / 'term-delivered')!r}, 'w').write('term\\n')\n"
        "    signal.signal(signal.SIGTERM, _on_term)\n"
        f"open({str(root / 'grand.pid')!r}, 'w').write(str(os.getpid()) + '\\n')\n"
        f"open({str(root / 'ready')!r}, 'w').write('ready\\n')\n"
        f"time.sleep({grand_sleep})\n"
    )
    (root / "child.py").write_text(
        "import os, subprocess, sys, time\n"
        f"open({str(root / 'child.pid')!r}, 'w').write(str(os.getpid()) + '\\n')\n"
        f"subprocess.Popen([sys.executable, {str(root / 'grandchild.py')!r}]\n"
        f"    + (['--ignore-term'] if {term_ignore} else []))\n"
        f"time.sleep({child_sleep})\n"
    )
    if parent_exits:
        tail = "exit 0\n"
    else:
        tail = "wait $CHILD\necho 'shell done'\n"
    (root / "parent.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'shell stdout line'\n"
        "echo 'shell stderr line' >&2\n"
        "echo $$ > " + str(root / "shell.pid") + "\n"
        f"'{sys.executable}' " + str(root / "child.py") + " &\n"
        "CHILD=$!\n"
        "echo $CHILD > " + str(root / "child.pid") + "\n"
        + tail
    )
    os.chmod(root / "parent.sh", 0o755)
    return root


def _read_pids(root: Path) -> dict:
    pids = {}
    for name in ("shell", "child", "grand"):
        f = root / f"{name}.pid"
        if f.exists():
            try:
                pids[name] = int(f.read_text().strip())
            except ValueError:
                pass
    return pids


def _proc_state(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0]
    except FileNotFoundError:
        return "gone"
    except Exception:
        return "unknown"


class UpdateProcessLifecycleTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="fxroute-update-test-"))
        self._spawned_shells: list[int] = []

    def tearDown(self):
        for pid in self._spawned_shells:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def _wait_ready(self, root: Path, timeout: float = 6.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = _read_pids(root)
            if (root / "ready").exists() and set(pids) >= {"shell", "child", "grand"}:
                self._spawned_shells.append(pids["shell"])
                return pids
            await asyncio.sleep(0.02)
        self.fail(f"update fixture tree never became ready (pids={_read_pids(root)})")

    async def _assert_group_gone(self, root: Path, timeout: float = 6.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pids = _read_pids(root)
            if pids and all(_proc_state(p) == "gone" for p in pids.values()):
                return
            await asyncio.sleep(0.05)
        states = {k: _proc_state(v) for k, v in _read_pids(root).items()}
        self.fail(f"update processes still alive: {states}")

    async def _assert_shell_reaped(self, shell_pid: int, timeout: float = 4.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                got = os.waitpid(shell_pid, os.WNOHANG)
            except ChildProcessError:
                return
            if got and got[0] == shell_pid:
                return
            await asyncio.sleep(0.05)
        self.fail(f"shell {shell_pid} was never reaped")

    # ------------------------------------------------------------------
    # 13) normal success
    # ------------------------------------------------------------------

    async def test_success_result_shape_and_clean_tree(self):
        tree = _write_tree(self._tmp / "ok", child_sleep=0.3, grand_sleep=0.3)
        with patch.object(main, "UPDATE_SCRIPT", tree / "parent.sh"), \
             patch.object(main, "_UPDATE_CHECK_TIMEOUT_SECONDS", 5.0), \
             patch.object(main, "_UPDATE_APPLY_TIMEOUT_SECONDS", 5.0):
            data = await main.system_update_status()
            self.assertEqual(
                set(data.keys()),
                {"ok", "installed_version", "returncode", "stdout", "stderr"},
                "result shape must be unchanged",
            )
            self.assertTrue(data["ok"])
            self.assertEqual(data["returncode"], 0)
            self.assertIn("shell stdout line", data["stdout"])
            self.assertIn("shell done", data["stdout"])
            self.assertIn("shell stderr line", data["stderr"])

            data2 = await main.system_update()
            self.assertTrue(data2["ok"])
            self.assertEqual(data2["returncode"], 0)
            self.assertFalse(data2["restart_scheduled"])
            self.assertEqual(
                set(data2.keys()),
                {"ok", "installed_version", "restart_scheduled", "service_name",
                 "returncode", "stdout", "stderr"},
            )
        pids = _read_pids(tree)
        self.assertIn("shell", pids)
        await self._assert_shell_reaped(pids["shell"])
        await self._assert_group_gone(tree)

    # ------------------------------------------------------------------
    # 14) overall timeout
    # ------------------------------------------------------------------

    async def test_overall_timeout_returns_failure_and_terminates_group(self):
        tree = _write_tree(self._tmp / "slow", child_sleep=60.0)
        with patch.object(main, "UPDATE_SCRIPT", tree / "parent.sh"), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.2):
            started = time.monotonic()
            result = await asyncio.wait_for(
                main._run_update_operation(0.4, "--defer-restart"), timeout=6.0
            )
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.0, "timeout + escalation must stay bounded")
        self.assertEqual(result["returncode"], -1, "timeout must be a failure result")
        self.assertIn("timed out after", result["stderr"])
        self.assertIn("shell stdout line", result["stdout"], "partial stdout must be preserved")
        self.assertIn("shell stderr line", result["stderr"])
        pids = _read_pids(tree)
        self.assertIn("shell", pids)
        await self._assert_shell_reaped(pids["shell"])
        await self._assert_group_gone(tree)

    # ------------------------------------------------------------------
    # 15) SIGTERM-ignoring descendant, parent already gone
    # ------------------------------------------------------------------

    async def test_sigterm_ignoring_grandchild_killed_by_group_sigkill_after_parent_exit(self):
        tree = _write_tree(
            self._tmp / "termignore", child_sleep=60.0, term_ignore=True, parent_exits=True
        )
        with patch.object(main, "UPDATE_SCRIPT", tree / "parent.sh"), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.3):
            result = await asyncio.wait_for(
                main._run_update_operation(0.5, "--defer-restart"), timeout=6.0
            )
        self.assertEqual(result["returncode"], -1)
        self.assertIn("timed out", result["stderr"])
        pids = _read_pids(tree)
        self.assertIn("shell", pids)
        self.assertEqual(_proc_state(pids["shell"]), "gone", "shell exited on its own")
        self.assertTrue(
            (tree / "term-delivered").exists(),
            "SIGTERM must reach the grandchild before the group SIGKILL",
        )
        await self._assert_shell_reaped(pids["shell"])
        await self._assert_group_gone(tree)

    # ------------------------------------------------------------------
    # 16) caller cancellation
    # ------------------------------------------------------------------

    async def test_caller_cancellation_terminates_group_and_propagates(self):
        tree = _write_tree(self._tmp / "cancel", child_sleep=60.0)
        with patch.object(main, "UPDATE_SCRIPT", tree / "parent.sh"), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.2):
            task = asyncio.create_task(main._run_update_operation(30.0, "--defer-restart"))
            pids = await self._wait_ready(tree)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=6.0)
        self.assertFalse(main._get_update_operation_lock().locked())
        await self._assert_shell_reaped(pids["shell"])
        await self._assert_group_gone(tree)

    # ------------------------------------------------------------------
    # 17) double cancellation during the grace window
    # ------------------------------------------------------------------

    async def test_double_cancel_during_grace_does_not_abort_cleanup(self):
        tree = _write_tree(self._tmp / "dbl", child_sleep=60.0, term_ignore=True)
        with patch.object(main, "UPDATE_SCRIPT", tree / "parent.sh"), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 1.0):
            task = asyncio.create_task(main._run_update_operation(30.0, "--defer-restart"))
            pids = await self._wait_ready(tree)
            task.cancel()
            await asyncio.sleep(0.25)  # cleanup is inside its grace window
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=6.0)
        self.assertTrue((tree / "term-delivered").exists())
        self.assertFalse(main._get_update_operation_lock().locked())
        await self._assert_shell_reaped(pids["shell"])
        await self._assert_group_gone(tree)

    # ------------------------------------------------------------------
    # 18) parallelism: 409, no second subprocess, guard freed afterwards
    # ------------------------------------------------------------------

    async def test_parallel_operation_rejected_409_and_guard_free_after_success(self):
        slow = _write_tree(self._tmp / "par-slow", child_sleep=60.0)
        fast = _write_tree(self._tmp / "par-fast", child_sleep=0.3, grand_sleep=0.3)
        spawns: list = []

        async def counted_exec(*args, **kwargs):
            proc = await _REAL_EXEC(*args, **kwargs)
            spawns.append(proc)
            return proc

        with patch.object(main.asyncio, "create_subprocess_exec", new=counted_exec):
            with patch.object(main, "UPDATE_SCRIPT", slow / "parent.sh"), \
                 patch.object(main, "_UPDATE_APPLY_TIMEOUT_SECONDS", 30.0), \
                 patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.2):
                op = asyncio.create_task(main._run_update_operation(30.0, "--defer-restart"))
                await self._wait_ready(slow)
                for caller in (main.system_update_status, main.system_update,
                               main.system_restore):
                    with self.assertRaises(HTTPException) as ctx:
                        await caller()
                    self.assertEqual(ctx.exception.status_code, 409)
                    self.assertIn("already in progress", ctx.exception.detail)
                self.assertEqual(len(spawns), 1, "no second update subprocess may start")
                op.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(op, timeout=6.0)
            # guard must be free again after cancellation
            with patch.object(main, "UPDATE_SCRIPT", fast / "parent.sh"), \
                 patch.object(main, "_UPDATE_APPLY_TIMEOUT_SECONDS", 5.0):
                result = await asyncio.wait_for(
                    main._run_update_operation(5.0, "--defer-restart"), timeout=5.0
                )
        self.assertEqual(result["returncode"], 0, "guard must be free again")
        self.assertEqual(len(spawns), 2)
        await self._assert_group_gone(fast)

    async def test_timeout_frees_guard(self):
        slow = _write_tree(self._tmp / "to-slow", child_sleep=60.0)
        fast = _write_tree(self._tmp / "to-fast", child_sleep=0.3, grand_sleep=0.3)
        with patch.object(main, "UPDATE_SCRIPT", slow / "parent.sh"), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.2):
            op = asyncio.create_task(main._run_update_operation(0.4, "--defer-restart"))
            await self._wait_ready(slow)
            with self.assertRaises(HTTPException) as ctx:
                await main._run_update_operation(0.4, "--defer-restart")
            self.assertEqual(ctx.exception.status_code, 409)
            result = await asyncio.wait_for(op, timeout=6.0)
            self.assertEqual(result["returncode"], -1)
            await self._assert_group_gone(slow)
            with patch.object(main, "UPDATE_SCRIPT", fast / "parent.sh"):
                result2 = await asyncio.wait_for(
                    main._run_update_operation(5.0, "--defer-restart"), timeout=5.0
                )
        self.assertEqual(result2["returncode"], 0, "guard must be free after a timeout")

    async def test_cancellation_frees_guard(self):
        slow = _write_tree(self._tmp / "c-slow", child_sleep=60.0)
        fast = _write_tree(self._tmp / "c-fast", child_sleep=0.3, grand_sleep=0.3)
        with patch.object(main, "UPDATE_SCRIPT", slow / "parent.sh"), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.2):
            op = asyncio.create_task(main._run_update_operation(30.0, "--defer-restart"))
            await self._wait_ready(slow)
            with self.assertRaises(HTTPException) as ctx:
                await main._run_update_operation(30.0, "--defer-restart")
            self.assertEqual(ctx.exception.status_code, 409)
            op.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(op, timeout=6.0)
            await self._assert_group_gone(slow)
            with patch.object(main, "UPDATE_SCRIPT", fast / "parent.sh"):
                result = await asyncio.wait_for(
                    main._run_update_operation(5.0, "--defer-restart"), timeout=5.0
                )
        self.assertEqual(result["returncode"], 0, "guard must be free after cancellation")

    # ------------------------------------------------------------------
    # 19) check-vs-update cross type
    # ------------------------------------------------------------------

    async def test_check_blocks_update_cross_type(self):
        slow = _write_tree(self._tmp / "chk-slow", child_sleep=60.0)
        fast = _write_tree(self._tmp / "chk-fast", child_sleep=0.3, grand_sleep=0.3)
        with patch.object(main, "UPDATE_SCRIPT", slow / "parent.sh"), \
             patch.object(main, "_UPDATE_CHECK_TIMEOUT_SECONDS", 30.0), \
             patch.object(main, "_UPDATE_APPLY_TIMEOUT_SECONDS", 30.0), \
             patch.object(main, "_UPDATE_TERMINATE_GRACE_SECONDS", 0.2):
            check = asyncio.create_task(main.system_update_status())
            await self._wait_ready(slow)
            with self.assertRaises(HTTPException) as ctx:
                await main.system_update()
            self.assertEqual(ctx.exception.status_code, 409)
            with self.assertRaises(HTTPException) as ctx:
                await main.system_restore()
            self.assertEqual(ctx.exception.status_code, 409)
            check.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(check, timeout=6.0)
        with patch.object(main, "UPDATE_SCRIPT", fast / "parent.sh"), \
             patch.object(main, "_UPDATE_CHECK_TIMEOUT_SECONDS", 5.0):
            data = await main.system_update_status()
        self.assertTrue(data["ok"])
        await self._assert_group_gone(fast)


if __name__ == "__main__":
    unittest.main(verbosity=2)
