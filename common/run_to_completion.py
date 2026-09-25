# SPDX-License-Identifier: AGPL-3.0-only

"""Finish a cleanup step even while its caller is being cancelled.

``asyncio.shield`` alone hands a cancellation back to the caller at once while
the shielded work keeps running in the background. A caller that then releases
an owner, a lock or a job slot does so before the restore or mute cleanup it
shielded has landed. ``run_to_completion`` waits for the work instead and
re-raises the cancellation only afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")


async def run_to_completion(awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` to its end; a cancel received meanwhile is re-raised after.

    Repeated cancels cannot interrupt the work. Its own exception wins over a
    pending cancel, so a failed restore still surfaces loudly.
    """
    task: asyncio.Future[Any] = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError()
    return result
