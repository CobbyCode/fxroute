# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit caller ownership for the existing DSP sync locks.

Acquire rate before graph. Configuration transitions take volume, mutation,
rate, then graph; fast output edits take rate then graph. Mode recovery keeps
its existing rate, mutation, graph order (external DSP mutations refuse a busy
rate session before taking mutation ownership).

Ownership is an explicit caller contract, not inferred from Lock.locked() or
task identity: a caller may retain the locks while draining delegated work.
This does not grant measurement ownership or acquire any lock by itself.
"""

from enum import Enum, auto


class DspSyncOwnership(Enum):
    UNOWNED = auto()
    RATE = auto()
    RATE_AND_GRAPH = auto()

    @property
    def rate_owned(self) -> bool:
        return self is not DspSyncOwnership.UNOWNED

    @property
    def graph_owned(self) -> bool:
        return self is DspSyncOwnership.RATE_AND_GRAPH
