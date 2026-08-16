# SPDX-License-Identifier: AGPL-3.0-only

"""Stable graph readback helper shared by the coordinator and adapters."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping


async def stable_graph_readbacks(
    read: Callable[[], Awaitable[Mapping[str, Any]]],
    *,
    count: int = 2,
) -> tuple[list[Mapping[str, Any]], list[str], bool]:
    """Collect ``count`` graph readbacks and evaluate canonical stability.

    Stability means every readback reports ``links_complete`` and all
    readbacks share one signature.  Returns ``(readbacks, signatures,
    stable)``; callers own the failure handling so each transition keeps its
    specific message and logging.
    """
    readbacks: list[Mapping[str, Any]] = []
    for _ in range(count):
        readback = await read()
        if not isinstance(readback, Mapping):
            raise RuntimeError("stable graph readback was not a mapping")
        readbacks.append(readback)
    signatures = [str(item.get("signature")) for item in readbacks]
    stable = bool(
        len(readbacks) == count
        and all(item.get("links_complete") for item in readbacks)
        and len(set(signatures)) == 1
    )
    return readbacks, signatures, stable

