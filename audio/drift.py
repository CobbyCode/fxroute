# SPDX-License-Identifier: AGPL-3.0-only
"""Playback samplerate drift observation.

Owns the repeated-readback drift signature bookkeeping and the read-only
drift observation that requests a Coordinator recovery on a stable mismatch.
No imports from ``main``: live services are injected by the composition
root, rate-domain helpers come from ``audio.samplerate``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

import audio.samplerate as samplerate

logger = logging.getLogger(__name__)


@dataclass
class SamplerateDriftDependencies:
    """Live services the drift observer needs."""

    get_current_track_info: Callable[[], dict[str, Any] | None]
    get_player_instance: Callable[[], Any]
    coordinator_source_rate: Callable[..., int | None]
    get_player_audio_samplerate: Callable[[], int | None]
    get_samplerate_status: Callable[[], dict[str, Any]]
    playback_transition_is_active: Callable[[], bool]
    is_measurement_window_open: Callable[[], bool]
    measurement_session_active: Callable[[], bool]
    measurement_audio_graph_owned: Callable[[], bool]
    request_coordinated_recovery: Callable[..., Any]


class SamplerateDriftObserver:
    """Single owner of drift-observation state and its recovery request."""

    def __init__(self, deps: SamplerateDriftDependencies) -> None:
        self._deps = deps
        self.signature: tuple[Any, ...] | None = None
        self.readbacks: int = 0

    def reset(self) -> None:
        self.signature = None
        self.readbacks = 0

    def record(self, signature: tuple[Any, ...]) -> int:
        if signature == self.signature:
            self.readbacks += 1
        else:
            self.signature = signature
            self.readbacks = 1
        return self.readbacks

    async def observe(self) -> None:
        """Observe a stable source/MPV/hardware-rate mismatch without mutating playback."""

        deps = self._deps
        # The Coordinator and the measurement session own all rate mutations.
        # A readback captured during either operation is not evidence of a
        # settled playback drift and must not start a competing repair.
        if (
            deps.playback_transition_is_active()
            or deps.is_measurement_window_open()
            or deps.measurement_session_active()
            or deps.measurement_audio_graph_owned()
        ):
            self.reset()
            return

        track = dict(deps.get_current_track_info() or {})
        source = str(track.get("source") or "")
        if source not in {"local", "radio"}:
            self.reset()
            return

        player_instance = deps.get_player_instance()
        state = dict(player_instance.state if player_instance else {})
        current_file = state.get("current_file")
        expected_url = str(track.get("url") or "")
        if (
            not current_file
            or state.get("ended")
            or (expected_url and current_file != expected_url)
        ):
            self.reset()
            return

        # Read all rate domains as one observation.  The track rate is the
        # last successful Coordinator context, MPV audio-params is the live
        # source truth, and the PipeWire values are the current hardware
        # readback.
        track_rate = deps.coordinator_source_rate(source, track)
        actual_rate = deps.get_player_audio_samplerate()
        try:
            samplerate_status = deps.get_samplerate_status()
        except Exception:
            samplerate_status = {}
        active_rate = samplerate_status.get("active_rate") if isinstance(samplerate_status, dict) else None
        force_rate = samplerate_status.get("force_rate") if isinstance(samplerate_status, dict) else None
        if (
            not isinstance(track_rate, int)
            or track_rate <= 0
            or not isinstance(actual_rate, int)
            or actual_rate <= 0
            or not isinstance(active_rate, int)
            or active_rate <= 0
        ):
            self.reset()
            return

        target_rate = samplerate.effective_playback_rate(actual_rate)
        if not isinstance(target_rate, int) or target_rate <= 0:
            self.reset()
            return

        source_metadata_aligned = (
            samplerate.load_sample_rate_policy().get("mode") == "fixed"
            or track_rate == actual_rate
        )
        healthy = (
            source_metadata_aligned
            and active_rate == target_rate
            and (force_rate is None or force_rate == 0 or force_rate == target_rate)
        )
        if healthy:
            self.reset()
            return

        signature = (
            source,
            expected_url or str(track.get("id") or ""),
            str(current_file),
            track_rate,
            actual_rate,
            target_rate,
            active_rate,
            force_rate,
        )
        readbacks = self.record(signature)

        # One readback can be a transient MPV property update.  Require the
        # same source and the same mismatch on a later watcher pass before
        # requesting recovery.
        if readbacks <= 1:
            return

        diagnosis = {
            "signature": (
                f"samplerate:{source}:{expected_url or track.get('id')}:"
                f"track={track_rate}:mpv={actual_rate}:target={target_rate}:active={active_rate}:force={force_rate}"
            ),
            "expected_rate": target_rate,
            "track_rate": track_rate,
            "actual_rate": actual_rate,
            "mpv_rate": actual_rate,
            "hardware_rate": active_rate,
            "force_rate": force_rate,
        }
        self.reset()
        logger.warning(
            "Stable playback samplerate drift observed; requesting Coordinator recovery: "
            "source=%s url=%s track=%s mpv=%s target=%s active=%s force=%s",
            source,
            expected_url,
            track_rate,
            actual_rate,
            target_rate,
            active_rate,
            force_rate,
        )
        recovery_track = dict(track)
        # MPV is the authoritative source-rate readback for this repair.  Keep
        # the watcher read-only by passing a copy; the Coordinator updates the
        # committed track context only after a successful recovery commit.
        recovery_track["sample_rate_hz"] = actual_rate
        await deps.request_coordinated_recovery(
            recovery_track,
            "samplerate-drift-watcher",
            reload_source=True,
            diagnosis=diagnosis,
        )
