# SPDX-License-Identifier: AGPL-3.0-only

"""Single authoritative owner of the mutable playback source/rate state.

``PlaybackRateState`` consolidates the globals that used to be scattered over
``main.py`` for source/rate coordination: the forced playback samplerate
mirror, the current source mode, and the samplerate drift observation state.
Only stdlib, so the container stays testable without FXRoute imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PlaybackRateState:
    """Authoritative owner of the playback source/rate coordination state.

    * ``playback_samplerate_force_rate`` mirrors the last PipeWire forced
      playback rate applied by ``_ensure_playback_samplerate_force``.
    * ``current_source_mode`` is the committed app source mode
      (app-playback / external-input / bluetooth-input).
    * ``samplerate_drift_signature`` / ``samplerate_drift_readbacks`` hold the
      drift observation state: the signature of the currently observed
      mismatch and how many consecutive readbacks confirmed it.
    """

    playback_samplerate_force_rate: int | None = None
    # Mirrors samplerate.SOURCE_MODE_APP_PLAYBACK.
    current_source_mode: str = "app-playback"
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
