#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""run_to_completion finishes its work and never loses the caller's cancel.

Same contract as ``MeasurementStore.drain_job``: a cancel that reaches the
caller while it waits is re-raised after the work, also when the work finished
in the same event-loop tick. The Python 3.11+ cancel count must stay
consistent, so ``asyncio.timeout`` and ``TaskGroup`` around the call keep their
normal meaning.
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.run_to_completion import run_to_completion


async def settle(turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


class RunToCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def start(self, work):
        """Run ``work`` through run_to_completion in its own caller task."""
        caller = asyncio.create_task(run_to_completion(work))
        await settle()
        self.assertFalse(caller.done())
        return caller

    async def test_cancel_in_the_same_tick_as_completion_is_not_lost(self):
        gate = asyncio.get_running_loop().create_future()
        finished = []

        async def work():
            value = await gate
            finished.append(value)
            return value

        caller = await self.start(work())
        # The work completes and the caller is cancelled before either runs.
        gate.set_result("restored")
        caller.cancel("stop")
        with self.assertRaises(asyncio.CancelledError) as raised:
            await caller
        self.assertEqual(finished, ["restored"], "the work still ran to its end")
        self.assertEqual(raised.exception.args, ("stop",), "the caller's cancel is re-raised as is")

    async def test_cancel_while_running_waits_for_the_work(self):
        gate = asyncio.get_running_loop().create_future()
        finished = []

        async def work():
            finished.append(await gate)

        caller = await self.start(work())
        caller.cancel()
        caller.cancel()
        await settle()
        self.assertFalse(caller.done(), "repeated cancels cannot interrupt the work")
        gate.set_result("done")
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertEqual(finished, ["done"])

    async def test_uncancelled_caller_gets_the_result(self):
        async def work():
            await asyncio.sleep(0)
            return 7

        self.assertEqual(await run_to_completion(work()), 7)

    async def test_work_error_wins_over_a_pending_cancel(self):
        gate = asyncio.get_running_loop().create_future()

        async def work():
            await gate
            raise RuntimeError("restore failed")

        caller = await self.start(work())
        caller.cancel()
        await settle()
        gate.set_result(None)
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            await caller

    async def test_work_cancelled_by_itself_propagates(self):
        async def work():
            raise asyncio.CancelledError("inner")

        with self.assertRaises(asyncio.CancelledError):
            await run_to_completion(work())

    async def test_timeout_in_the_completion_tick_raises_timeout_and_restores_the_count(self):
        # asyncio.timeout cancels the task and turns that cancel back into a
        # TimeoutError only if the CancelledError reaches it; a swallowed
        # cancel would leave the task's cancel count raised.
        loop = asyncio.get_running_loop()
        gate = loop.create_future()
        finished = []
        counts = {}

        async def work():
            finished.append(await gate)

        async def caller_body():
            try:
                async with asyncio.timeout(3600) as scope:
                    counts["scope"] = scope
                    await run_to_completion(work())
            finally:
                counts["after"] = asyncio.current_task().cancelling()

        caller = asyncio.create_task(caller_body())
        await settle()
        # Expire the timeout in the same tick in which the work completes.
        gate.set_result("done")
        counts["scope"].reschedule(loop.time() - 1)
        with self.assertRaises(TimeoutError):
            await caller
        self.assertEqual(finished, ["done"])
        self.assertEqual(counts["after"], 0, "no cancel request is left behind")

    async def test_task_group_sibling_failure_in_the_completion_tick(self):
        # A failing sibling cancels the child; the child must stop after its
        # work instead of carrying on with a raised cancel count, and the
        # group reports the sibling's error, not a stray CancelledError.
        gate = asyncio.get_running_loop().create_future()
        trigger = asyncio.get_running_loop().create_future()
        after_work = []

        async def work():
            return await gate

        async def child():
            await run_to_completion(work())
            after_work.append("continued")
            await asyncio.sleep(0)

        async def sibling():
            await trigger
            gate.set_result("done")
            raise ValueError("sibling failed")

        with self.assertRaises(ExceptionGroup) as raised:
            async with asyncio.TaskGroup() as group:
                group.create_task(child())
                group.create_task(sibling())
                await settle()
                trigger.set_result(None)
        self.assertEqual([type(error) for error in raised.exception.exceptions], [ValueError])
        self.assertEqual(after_work, [], "the cancelled child did not carry on")
        self.assertEqual(asyncio.current_task().cancelling(), 0)


if __name__ == "__main__":
    unittest.main()
