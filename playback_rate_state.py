# SPDX-License-Identifier: AGPL-3.0-only

"""Single authoritative owner of the mutable playback samplerate drift state.

``PlaybackRateState`` owns the samplerate drift observation state that used to
be scattered over ``main.py``.  Only stdlib, so the container stays testable
without FXRoute imports.

The PipeWire forced rate stays authoritative via ``get_samplerate_status`` /
``_get_current_pipewire_force_rate``, and the committed source mode via
``get_audio_source_overview``; neither is cached here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PlaybackRateState:
    """Authoritative owner of the playback samplerate drift observation state.

    * ``samplerate_drift_signature`` / ``samplerate_drift_readbacks`` hold the
      drift observation state: the signature of the currently observed
      mismatch and how many consecutive readbacks confirmed it.
    """

    samplerate_drift_signature: tuple[Any, ...] | None = None
    samplerate_drift_readbacks: int = 0

    def reset_drift_observation(self) -> None:
        """Clear the drift signature and its readback counter."""
        self.samplerate_drift_signature = None
        self.samplerate_drift_readbacks = 0

    def record_drift_observation(self, signature: tuple[Any, ...]) -> int:
        """Record one drift readback; return the total readback count.

        A new signature restarts the counter at one; an unchanged signature
        increments it.  Callers require more than one readback before acting,
        so a single transient MPV property update never starts a recovery.
        """
        if signature == self.samplerate_drift_signature:
            self.samplerate_drift_readbacks += 1
        else:
            self.samplerate_drift_signature = signature
            self.samplerate_drift_readbacks = 1
        return self.samplerate_drift_readbacks
