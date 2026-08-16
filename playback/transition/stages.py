# SPDX-License-Identifier: AGPL-3.0-only

"""Per-transition stage tracking for timing and failure labels."""

from __future__ import annotations

import logging
import time


logger = logging.getLogger(__name__)


class _TransitionStages:
    """Transition stage tracker owned by one ``execute`` run.

    Records per-stage timing and the failure-stage label, and carries the
    cleanup input shared by the stage sub-paths: ``gate_required`` (whether
    this transition ever closed the output gate).
    """

    def __init__(self, transition_id: str) -> None:
        self.transition_id = transition_id
        self.transition_started = time.monotonic()
        self.stage = "snapshot"
        self._stage_started = self.transition_started
        self._timings: dict[str, float] = {}
        self.gate_required = False

    def enter(self, name: str) -> None:
        """Enter the next stage, closing the timing of the current one."""
        now = time.monotonic()
        self._timings[self.stage] = self._timings.get(self.stage, 0.0) + (
            now - self._stage_started
        )
        self.stage = name
        self._stage_started = now

    def log(self, outcome: str) -> None:
        """Record the final stage timing and log the transition outcome."""
        now = time.monotonic()
        self._timings[self.stage] = self._timings.get(self.stage, 0.0) + (
            now - self._stage_started
        )
        details = ",".join(
            f"{name}={duration * 1000:.1f}ms"
            for name, duration in self._timings.items()
        )
        total_ms = (now - self.transition_started) * 1000
        if outcome == "committed":
            logger.info(
                "Playback transition timing: transition_id=%s "
                "outcome=committed total_ms=%.1f stages=%s",
                self.transition_id,
                total_ms,
                details,
            )
        else:
            logger.warning(
                "Playback transition timing: transition_id=%s "
                "outcome=failed stage=%s stage_ms=%.1f total_ms=%.1f "
                "stages=%s",
                self.transition_id,
                self.stage,
                self._timings.get(self.stage, 0.0) * 1000,
                total_ms,
                details,
            )

