#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Cancellation refactors must preserve each owner's completion boundary."""

import asyncio
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import radio.api as radio_api
from audio.digital_input import DigitalInput
from common.run_to_completion import finish_then_cancel
from measurement import spl_calibration as spl
from playback.runtime.source import _drain_qobuz_operation


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class WorkerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def check_worker(self, owner, *, cancel, fail):
        entered = threading.Event()
        release = threading.Event()
        finished = []
        lock = asyncio.Lock()

        def worker(value, *, suffix):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Worker was not released")
            finished.append(value + suffix)
            if fail:
                raise RuntimeError("worker failed")
            return value + suffix

        with patch.object(radio_api, "_station_mutation_lock", lock):
            operation = (main._run_locked_worker(lock, worker, "done", suffix="!")
                         if owner == "main" else
                         radio_api._run_locked_station_worker(worker, "done", suffix="!"))
            caller = asyncio.create_task(operation)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                if cancel:
                    for _ in range(3):
                        caller.cancel("request stopped")
                        await settle()
                        self.assertFalse(caller.done())
                        self.assertTrue(lock.locked())
                        self.assertEqual(finished, [])
                release.set()
                if fail:
                    # An exception delivered by shield still wins after cancel.
                    with self.assertRaisesRegex(RuntimeError, "worker failed"):
                        await caller
                elif cancel:
                    with self.assertRaises(asyncio.CancelledError) as raised:
                        await caller
                    self.assertEqual(raised.exception.args, ())
                else:
                    self.assertEqual(await caller, "done!")
                self.assertEqual(finished, ["done!"])
                self.assertFalse(lock.locked())
            finally:
                release.set()
                await asyncio.gather(caller, return_exceptions=True)

    async def test_main_and_radio_keep_worker_and_lock_until_completion(self):
        for owner in ("main", "radio"):
            for cancel, fail in ((False, False), (False, True), (True, False), (True, True)):
                with self.subTest(owner=owner, cancel=cancel, fail=fail):
                    await self.check_worker(owner, cancel=cancel, fail=fail)

    async def test_completion_tick_worker_failure_keeps_existing_cancel_priority(self):
        gate = asyncio.Future()

        async def work():
            await gate
            raise RuntimeError("worker failed")

        caller = asyncio.create_task(finish_then_cancel(work(), what="Worker"))
        await settle()
        gate.set_result(None)
        caller.cancel("stop")
        with self.assertLogs("common.run_to_completion", level="ERROR"):
            with self.assertRaises(asyncio.CancelledError) as raised:
                await caller
        self.assertEqual(raised.exception.args, ())


class OperationCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_failure_wins_but_qobuz_keeps_original_cancel(self):
        for owner in ("commit", "qobuz"):
            for fail in (False, True):
                with self.subTest(owner=owner, fail=fail):
                    gate = asyncio.Future()
                    names = []

                    async def work():
                        names.append(asyncio.current_task().get_name())
                        await gate
                        if fail:
                            raise RuntimeError("operation failed")

                    operation = (main._finish_despite_cancellation(work(), name="test-commit")
                                 if owner == "commit" else _drain_qobuz_operation(work()))
                    caller = asyncio.create_task(operation)
                    try:
                        await settle()
                        for message in ("first", "last"):
                            caller.cancel(message)
                            await settle()
                            self.assertFalse(caller.done())
                        gate.set_result(None)
                        if owner == "commit" and fail:
                            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                                await caller
                        else:
                            if owner == "qobuz" and fail:
                                with self.assertLogs("playback.runtime.source", level="WARNING"):
                                    with self.assertRaises(asyncio.CancelledError) as raised:
                                        await caller
                            else:
                                with self.assertRaises(asyncio.CancelledError) as raised:
                                    await caller
                            self.assertEqual(raised.exception.args, ("last",) if owner == "qobuz" else ())
                        self.assertEqual(names, ["test-commit" if owner == "commit" else
                                                 "qobuz-transition-operation"])
                    finally:
                        if not gate.done():
                            gate.set_result(None)
                        await asyncio.gather(caller, return_exceptions=True)

    async def test_digital_spawn_registers_child_and_reader_before_propagating_cancel(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                adapter = DigitalInput()
                gate = asyncio.Future()
                stderr = asyncio.StreamReader()
                stderr.feed_eof()
                process = SimpleNamespace(stderr=stderr)

                async def spawn(*args, **kwargs):
                    await gate
                    if fail:
                        raise OSError("spawn failed")
                    return process

                with patch("audio.digital_input.asyncio.create_subprocess_exec", spawn):
                    caller = asyncio.create_task(adapter._spawn())
                    try:
                        await settle()
                        for _ in range(3):
                            caller.cancel("stop")
                            await settle()
                            self.assertFalse(caller.done())
                            self.assertIsNone(adapter.process)
                        gate.set_result(None)
                        if fail:
                            with self.assertRaisesRegex(OSError, "spawn failed"):
                                await caller
                            self.assertIsNone(adapter.process)
                            self.assertIsNone(adapter._stderr_task)
                        else:
                            with self.assertRaises(asyncio.CancelledError) as raised:
                                await caller
                            self.assertEqual(raised.exception.args, ())
                            self.assertIs(adapter.process, process)
                            self.assertIsNotNone(adapter._stderr_task)
                            await adapter._stderr_task
                    finally:
                        if not gate.done():
                            gate.set_result(None)
                        await asyncio.gather(caller, return_exceptions=True)

    async def test_spl_thread_requests_stop_on_each_cancel_and_drains_before_return(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                entered, release = threading.Event(), threading.Event()
                stop_requests = []
                process = SimpleNamespace(poll=lambda: None,
                                          terminate=lambda: stop_requests.append("stop"))
                operation = spl._SplCalibrationOperation("test", "automatic", "spl-test", recorder=process)

                def worker():
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError("SPL worker was not released")
                    if fail:
                        raise RuntimeError("SPL worker failed")
                    return operation.cancel_requested

                caller = asyncio.create_task(spl._run_operation_thread(operation, worker))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    for _ in range(3):
                        caller.cancel("stop")
                        await settle()
                        self.assertFalse(caller.done())
                    self.assertTrue(operation.cancel_requested)
                    self.assertEqual(stop_requests, ["stop", "stop", "stop"])
                    release.set()
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "SPL worker failed"):
                            await caller
                    else:
                        with self.assertRaises(asyncio.CancelledError):
                            await caller
                finally:
                    release.set()
                    await asyncio.gather(caller, return_exceptions=True)

    async def test_spl_cleanup_finishes_and_its_error_wins_over_cancel(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                gate = asyncio.Future()
                released = []

                async def cleanup(operation, *, release_unrestored):
                    await gate
                    released.append(release_unrestored)
                    if fail:
                        raise RuntimeError("cleanup failed")

                with patch.object(spl, "_cleanup_operation", cleanup):
                    caller = asyncio.create_task(spl._cleanup_operation_shielded(
                        object(), release_unrestored=True))
                    try:
                        await settle()
                        for _ in range(3):
                            caller.cancel("stop")
                            await settle()
                            self.assertFalse(caller.done())
                            self.assertEqual(released, [])
                        gate.set_result(None)
                        if fail:
                            with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
                                await caller
                        else:
                            with self.assertRaises(asyncio.CancelledError):
                                await caller
                        self.assertEqual(released, [True])
                    finally:
                        if not gate.done():
                            gate.set_result(None)
                        await asyncio.gather(caller, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
