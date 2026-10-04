# SPDX-License-Identifier: AGPL-3.0-only
"""External presence feed for input auto-select.

Owns the per-input presence reports an external automation (for example
Home Assistant) sends over the HTTP return channel. A report is a
heartbeat: only a fresh ``present`` report counts, anything older than its
TTL reads as absent, and an explicit ``present: false`` clears the key
immediately. The feed never invents addresses -- keys are the input keys
of the audio source overview (or the fixed STDIN key), passed in as-is.

No imports from ``main``: the composition root owns the instance and wires
the HTTP route and the auto-select reader.
"""

from __future__ import annotations

import time
from typing import Any, Callable

DEFAULT_PRESENCE_TTL_SECONDS = 30.0


class InputPresenceFeed:
    """Presence reports per input key, TTL-bounded and fail-closed."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_PRESENCE_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._monotonic = monotonic
        self._reported_at: dict[str, float] = {}

    def report(self, key: str, present: bool) -> None:
        """Record one report; an explicit ``False`` clears immediately."""
        normalized = (key or "").strip()
        if not normalized:
            raise ValueError("Input key is required")
        if present:
            self._reported_at[normalized] = self._monotonic()
        else:
            self._reported_at.pop(normalized, None)

    def present(self, key: str) -> bool:
        """Fresh presence for ``key``; unknown and expired keys are absent."""
        reported = self._reported_at.get((key or "").strip())
        if reported is None:
            return False
        return (self._monotonic() - reported) <= self.ttl_seconds

    def snapshot(self) -> dict[str, Any]:
        """Currently fresh reports, for the read-only status route."""
        return {
            "ttl_seconds": self.ttl_seconds,
            "inputs": {
                key: True for key in sorted(self._reported_at) if self.present(key)
            },
        }

    def reset(self) -> None:
        """Forget every report (used by tests and a clean shutdown)."""
        self._reported_at.clear()
