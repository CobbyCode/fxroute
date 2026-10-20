"""Non-blocking, bounded cover-availability probes for network libraries."""

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import partial

from common.run_to_completion import finish_then_cancel

logger = logging.getLogger(__name__)


class ArtworkBusy(RuntimeError):
    """The isolated artwork worker budget is exhausted."""


class ArtworkWorkers:
    """Bound admission by real futures, not cancellable asyncio wrappers."""

    def __init__(self, executor: Executor, *, max_pending: int = 32):
        self._executor = executor
        self._slots = threading.BoundedSemaphore(max_pending)

    async def run(self, function: Callable, *args, **kwargs):
        if not self._slots.acquire(blocking=False):
            raise ArtworkBusy("Library file workers are busy; retry shortly")
        try:
            future = self._executor.submit(partial(function, *args, **kwargs))
        except BaseException:
            self._slots.release()
            raise
        future.add_done_callback(lambda _future: self._slots.release())
        return await finish_then_cancel(asyncio.wrap_future(future), what="Library artwork worker")


artwork_workers = ArtworkWorkers(ThreadPoolExecutor(max_workers=4, thread_name_prefix="library-artwork"))


class NetworkCoverCache:
    """Event-loop-owned cache; a stalled server cannot create unlimited workers."""

    def __init__(self):
        self._values: OrderedDict[Hashable, tuple[bool, float]] = OrderedDict()
        self._pending: set[Hashable] = set()

    def get(self, key: Hashable, probe: Callable[[], bool], schedule: Callable, *, initial: bool = False) -> bool:
        value, checked = self._values.get(key, (initial, 0.0))
        if key in self._values and time.monotonic() - checked < 30:
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return value
        if key not in self._pending and len(self._pending) < 2:
            self._pending.add(key)
            schedule(self._refresh(key, probe), name="network-cover-probe")
        return value

    async def _refresh(self, key: Hashable, probe: Callable[[], bool]) -> None:
        try:
            value = bool(await artwork_workers.run(probe))
        except Exception as exc:
            logger.debug("Network cover probe failed: %s", exc)
            value = self._values.get(key, (False, 0.0))[0]
        finally:
            self._pending.discard(key)
        self._values[key] = (value, time.monotonic())
        self._values.move_to_end(key)
        while len(self._values) > 128:
            self._values.popitem(last=False)
