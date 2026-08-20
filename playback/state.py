"""Playback state helpers (REFACTOR-006-Extrakt).

State-free check functions for player/Spotify state and track matching,
extracted 1:1 from ``main.py``. No imports from ``main`` or other project
modules, stdlib only.

``PlaybackState`` is the single authoritative owner of the mutable
playback/transition state that previously lived as global variables spread
across ``main.py``.  Stdlib only, so the container stays testable without
FXRoute imports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def is_local_playback_active(state: dict | None) -> bool:
    state = state or {}
    return bool(state.get("current_file") and not state.get("paused") and not state.get("ended"))


def is_external_playback_active(state: dict | None) -> bool:
    """Return whether an external renderer (spotify/qobuz) reports Playing.

    Both external renderers publish the same flat normalized state shape, so
    the activity check is shared: an external source owns playback only while
    it is available and reports ``status == "Playing"``. Paused/Stopped never
    count as active.
    """
    state = state or {}
    return bool(state.get("available") and state.get("status") == "Playing")


def is_spotify_playback_active(state: dict | None) -> bool:
    """Return whether Spotify reports Playing (alias of the shared external
    renderer activity check; Spotify and Qobuz publish the same shape)."""
    return is_external_playback_active(state)


def playback_state_matches_track(state: dict | None, track: dict | None) -> bool:
    state = state or {}
    track = track or {}
    source = track.get("source")
    current_file = state.get("current_file")
    track_url = track.get("url")
    if source in {"local", "radio", "tidal"} and current_file and track_url and current_file != track_url:
        return False
    return True


@dataclass
class PlaybackState:
    """Single authoritative owner of the mutable playback/transition state.

    One instance owns the playback context (current/last track snapshots,
    Spotify UI state, authoritative playback owner) together with the
    transition state
    machine (intent generation, attempt epoch, pending-attempt counter and
    the published commit tokens).  ``main.py`` keeps thin wrappers around the
    state-machine methods so the existing dependency wiring and test patching
    contract stays unchanged.

    Field relationships, made explicit here:

    * ``playback_transition_epoch`` advances on every attempt start;
      ``playback_transition_pending_attempts`` counts in-flight attempts.
      A captured epoch is only "current" while no attempt is pending.
    * ``playback_intent_generation`` advances on user playback actions and is
      compared against captured measurement-restore intent tokens.
    * ``playback_context_commit_id`` is published only at the application
      commit boundary (source/track change); the Coordinator owns its own
      ``last_successful_commit_id`` for every committed operation.
    """

    current_track_info: dict[str, Any] | None = None
    last_track_info: dict[str, Any] | None = None
    last_radio_track_info: dict[str, Any] | None = None
    latest_spotify_state: dict[str, Any] | None = None
    latest_qobuz_state: dict[str, Any] | None = None
    # Authoritative playback owner: which source currently owns playback.
    # ``None`` means idle/no committed owner. Pausing does not clear it; only
    # a real new playback intent / external source claim changes it.
    current_playback_owner: str | None = None

    playback_intent_generation: int = 0
    playback_transition_epoch: int = 0
    playback_transition_pending_attempts: int = 0
    playback_context_commit_id: str | None = None
    latest_player_state_seq_seen: int = 0

    def mark_playback_intent_changed(self) -> None:
        """Advance the measurement-restore intent token after a user action."""
        self.playback_intent_generation += 1

    def begin_transition_attempt(self) -> int:
        """Start one transition attempt; returns its monotonic epoch."""
        self.playback_transition_epoch += 1
        self.playback_transition_pending_attempts += 1
        return self.playback_transition_epoch

    def end_transition_attempt(self) -> bool:
        """Finish one attempt; returns True once no attempt is pending."""
        self.playback_transition_pending_attempts -= 1
        if self.playback_transition_pending_attempts < 0:
            self.playback_transition_pending_attempts = 0
            logger.critical("playback transition attempt accounting underflow")
        return self.playback_transition_pending_attempts == 0

    def capture_transition_epoch(self) -> int | None:
        """Capture a playback-context token; None while an attempt is in flight.

        A token captured while any attempt is active must stay invalid forever,
        matching the legacy odd/even generation contract: only idle captures may
        ever become a committed context.
        """
        if self.playback_transition_pending_attempts > 0:
            return None
        return self.playback_transition_epoch

    def transition_context_is_current(self, generation: int | None) -> bool:
        """Return true only for a context token captured at an idle boundary."""
        return (
            isinstance(generation, int)
            and generation == self.playback_transition_epoch
            and self.playback_transition_pending_attempts == 0
        )

    def current_playback_commit_id(self) -> str | None:
        """Return the published playback-context token."""
        return self.playback_context_commit_id

    def publish_playback_context_commit(self, commit_token: str | None) -> None:
        """Publish the playback-context token exactly once at the app boundary."""
        if commit_token:
            self.playback_context_commit_id = str(commit_token)

    def set_track_context(self, current: dict[str, Any] | None, last: dict[str, Any] | None) -> None:
        """Commit the current and last track snapshots together."""
        self.current_track_info = current
        self.last_track_info = last

    def commit_playback_context(
        self,
        *,
        current: dict[str, Any] | None,
        last: dict[str, Any] | None,
        owner: str | None,
        commit_token: str | None,
        last_radio: dict[str, Any] | None = None,
    ) -> bool:
        changed_owner = self.current_playback_owner != owner
        self.current_track_info = current
        self.last_track_info = last
        if last_radio is not None:
            self.last_radio_track_info = dict(last_radio)
        self.current_playback_owner = owner
        if commit_token:
            self.playback_context_commit_id = str(commit_token)
        return changed_owner

    def clear_playback_context(self, last_radio: dict | None = None) -> bool:
        had_owner = self.current_playback_owner is not None
        if last_radio is not None:
            self.last_radio_track_info = dict(last_radio)
        self.current_track_info = None
        self.current_playback_owner = None
        return had_owner
