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
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

T = TypeVar("T")

logger = logging.getLogger(__name__)


async def run_to_completion(awaitable: Awaitable[T]) -> T:
    """Await ``awaitable`` to its end; a cancel received meanwhile is re-raised after.

    Same contract as ``MeasurementStore.drain_job``: every cancel that reaches
    the caller while it waits is remembered, also when the work finishes in
    the same event-loop tick, and re-raised once the work is done. Repeated
    cancels cannot interrupt the work. The work's own exception or its own
    cancellation wins over a pending cancel, so a failed restore still
    surfaces loudly.

    The caller's cancel request is never swallowed: it leaves as the same
    ``CancelledError``, so the task's cancel count (Python 3.11+) stays
    matched and ``asyncio.timeout`` or a ``TaskGroup`` around the call keep
    their normal meaning.
    """
    task: asyncio.Future[Any] = asyncio.ensure_future(awaitable)
    caller_cancel: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            # A work that finished in this tick does not make the cancel moot.
            caller_cancel = exc
    result = task.result()
    if caller_cancel is not None:
        raise caller_cancel
    return result


async def restore_after(error: BaseException, restore: Awaitable[Any], *, what: str) -> None:
    """Run the restore that follows ``error`` to its end without replacing ``error``.

    The caller re-raises ``error`` afterwards. A failing restore is logged and
    added as a note to ``error``, so the original failure keeps its type and
    message. A cancel received meanwhile still leaves as ``CancelledError``
    after the restore (see ``run_to_completion``), with ``error`` as context.
    """
    try:
        await run_to_completion(restore)
    except Exception as restore_error:
        logger.error("%s failed after %s: %s", what, type(error).__name__, error,
                     exc_info=restore_error)
        error.add_note(f"{what} also failed: {restore_error}")
