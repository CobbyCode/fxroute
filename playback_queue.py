# SPDX-License-Identifier: AGPL-3.0-only

"""FXRoute playback queue: single owner of the queue state and queue semantics.

This module owns the committed queue state (track list, original order, index,
mode, loop, shuffle, single-track-loop) plus the queue decision logic: queue
preparation as an uncommitted candidate, atomic commit/reset, navigation
(next/previous/load), shuffle/loop mode changes, the MPV-native playlist
synchronization and the native-loss normalization.

The module is deliberately decoupled from ``main.py``: every application-shell
dependency (player, coordinator transition facade, track context, rate/policy
helpers, library) arrives through the explicit ``PlaybackQueueDependencies``
wiring, resolved late-bound so production wiring and test mocks observe the
same attributes.

Module boundary: must never import ``main`` (enforced by
``scripts/check_router_structure.py``).  The playback coordinator boundary is
the single injected ``run_transition`` callable: this module builds
``TransitionRequest`` objects but never enters transition stages, gate, DSP,
graph or recovery logic.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException
from playback_transition import PlaybackTransitionFailure, TransitionRequest

logger = logging.getLogger(__name__)


@dataclass
class QueueCandidate:
    """Prepared queue state that is not yet committed to the queue owner.

    ``/api/play`` builds one of these, hands the immutable snapshot to the
    Coordinator, and publishes it only after the playback transition
    committed.  The committed state stays untouched until then.
    """

    queue: list
    original: list
    index: int
    mode: str
    loop: bool
    shuffle: bool
    single_track_loop: bool
    track: dict


@dataclass(frozen=True)
class QueueSnapshot:
    """Read-only copy of the committed queue state for transport/readers."""

    tracks: list
    original: list
    index: int
    mode: str
    loop: bool
    shuffle: bool
    single_track_loop: bool


@dataclass(frozen=True)
class PlaybackQueueDependencies:
    """Late-bound accessors into the FXRoute application shell (main.py).

    Every field resolves the current shell state at call time, mirroring the
    existing ``make_playback_runtime_deps`` pattern: production wiring and
    test mocks observe the same module attributes.  The queue module never
    imports the shell directly.
    """

    player: Callable[[], Any]
    run_transition: Callable[..., Awaitable[Any]]
    commit_coordinated_track: Callable[..., None]
    get_current_track_info: Callable[[], dict | None]
    set_track_context: Callable[[dict, dict], None]
    transition_is_active: Callable[[], bool]
    player_is_running: Callable[..., bool]
    wait_for_player_current_file: Callable[..., Awaitable[bool]]
    coordinator_target_rate: Callable[..., int | None]
    coordinator_rate_change: Callable[..., bool]
    sample_rate_policy_is_auto: Callable[[], bool]
    transition_error_http: Callable[[PlaybackTransitionFailure], HTTPException]
    get_tracks: Callable[[], list]
    build_playback_payload: Callable[..., dict]


def can_use_native_local_queue(tracks: list[dict]) -> bool:
    """Return whether MPV can own one already-safe homogeneous playlist."""
    if len(tracks) <= 1:
        return False
    rates = []
    for track in tracks:
        if track.get("source", "local") != "local" or not str(track.get("url") or "").strip():
            return False
        rate = track.get("sample_rate_hz")
        if not isinstance(rate, int) or rate <= 0:
            return False
        rates.append(rate)
    return len(set(rates)) == 1


def cleared_queue_candidate(track: dict | None = None) -> QueueCandidate:
    """Candidate for a play request that intentionally replaces any queue."""
    return QueueCandidate(
        queue=[],
        original=[],
        index=-1,
        mode="app_replace",
        loop=False,
        shuffle=False,
        single_track_loop=False,
        track=dict(track) if track else {},
    )


class PlaybackQueue:
    """Authoritative playback queue state and queue semantics.

    One instance owns all seven correlated state values; ``commit``/``reset``
    mutate them atomically.  Navigation and mode changes leave the committed
    state untouched until the injected coordinator transition committed.
    """

    def __init__(self, deps: PlaybackQueueDependencies) -> None:
        self._deps = deps
        self.tracks: list[dict] = []
        self.original: list[dict] = []
        self.index: int = -1
        self.mode: str = "app_replace"
        self.loop: bool = False
        self.shuffle: bool = False
        self.single_track_loop: bool = False

    @property
    def _player(self) -> Any:
        return self._deps.player()

    def commit(self, candidate: QueueCandidate) -> None:
        """Publish a prepared queue candidate to the committed state."""
        self.tracks = [dict(item) for item in candidate.queue]
        self.original = [dict(item) for item in candidate.original]
        self.index = candidate.index
        self.mode = candidate.mode
        self.loop = candidate.loop
        self.shuffle = candidate.shuffle
        self.single_track_loop = candidate.single_track_loop

    def reset(self) -> None:
        """Clear the committed queue, trimming a native MPV playlist first."""
        was_native = self.mode == "native_mpv"
        if was_native:
            self.reduce_native_playlist_to_current()
            self.reset_mpv_loop_state()
        self.tracks = []
        self.original = []
        self.index = -1
        self.mode = "app_replace"
        self.loop = False
        self.shuffle = False
        self.single_track_loop = False

    def snapshot(self) -> QueueSnapshot:
        """Read-only copy of the committed queue state."""
        return QueueSnapshot(
            tracks=[dict(item) for item in self.tracks],
            original=[dict(item) for item in self.original],
            index=self.index,
            mode=self.mode,
            loop=self.loop,
            shuffle=self.shuffle,
            single_track_loop=self.single_track_loop,
        )

    def payload(self) -> dict:
        return {
            "active": len(self.tracks) > 1,
            "index": self.index,
            "count": len(self.tracks),
            "mode": self.mode,
            "tracks": [dict(item) for item in self.tracks],
            "loop": self.loop or self.single_track_loop,
            "shuffle": self.shuffle,
        }

    def native_request_fields(self, queue_state: QueueCandidate | None = None) -> dict[str, Any]:
        """Snapshot native-queue metadata for a Coordinator request.

        Reads the committed state by default; callers staging a not-yet-
        committed queue state pass that candidate instead.
        """
        if queue_state is None:
            mode = self.mode
            queue = self.tracks
            index = self.index
            loop = self.loop
        else:
            mode = queue_state.mode
            queue = queue_state.queue
            index = queue_state.index
            loop = queue_state.loop
        if mode != "native_mpv" or not can_use_native_local_queue(queue):
            return {}
        start_index = index if index >= 0 else 0
        return {
            "native_queue": tuple(dict(item) for item in queue),
            "native_queue_index": start_index,
            "native_queue_loop": bool(loop),
            # Queue order is already concrete in the prepared state.  Keep the
            # field explicit for request compatibility, but never ask MPV to
            # reshuffle it.
            "native_queue_shuffle": False,
        }

    def prepare_local_queue(self, track_id: str, queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False, *, reshuffle: bool = True, tracks: Optional[list] = None) -> QueueCandidate:
        """Build the requested queue as an uncommitted candidate.

        Metadata-only preparation: the committed queue state is untouched
        until ``commit`` publishes the candidate after a successful playback
        transition.

        ``tracks`` may be passed in from async callers that already offloaded
        the scan-capable ``library_scanner.get_tracks()`` read to a worker.
        """
        if tracks is None:
            tracks = self._deps.get_tracks()
        tracks_by_id = {track.id: track for track in tracks}

        selected_ids = []
        requested_ids = queue_track_ids if queue_track_ids else [track_id]
        for candidate in requested_ids:
            if candidate in tracks_by_id and candidate not in selected_ids:
                selected_ids.append(candidate)
        if track_id in tracks_by_id and track_id not in selected_ids:
            selected_ids.insert(0, track_id)

        ordered_tracks = [tracks_by_id[selected_id].to_dict() for selected_id in selected_ids]
        if not ordered_tracks:
            raise HTTPException(status_code=404, detail="Track not found")

        original_tracks = [dict(track) for track in ordered_tracks]

        if shuffle and reshuffle and len(ordered_tracks) > 1:
            current_index = next(
                (index for index, track in enumerate(ordered_tracks) if track.get("id") == track_id),
                0,
            )
            future = [dict(track) for track in ordered_tracks[current_index + 1:]]
            random.shuffle(future)
            ordered_tracks = [dict(track) for track in ordered_tracks[:current_index + 1]] + future

        queue = ordered_tracks if len(ordered_tracks) > 1 else []
        original = original_tracks if len(original_tracks) > 1 else []
        # A homogeneous local queue is safe to hand to MPV only after the
        # Coordinator has committed the common rate/DSP/graph/gate state.  The
        # request carries the immutable queue snapshot; the mode becomes
        # visible as native only after that transition commits.
        mode = "native_mpv" if can_use_native_local_queue(ordered_tracks) else "app_replace"
        if queue:
            track_index = next(
                (index for index, item in enumerate(queue) if item.get("id") == track_id),
                0,
            )
            track = dict(queue[track_index])
        else:
            track_index = -1
            track = dict(ordered_tracks[0])

        return QueueCandidate(
            queue=queue,
            original=original,
            index=track_index,
            mode=mode,
            loop=bool(loop and len(ordered_tracks) > 1),
            shuffle=bool(shuffle and len(ordered_tracks) > 1),
            single_track_loop=bool(loop and len(ordered_tracks) == 1),
            track=track,
        )

    async def load_track(self, index: int, *, transition_reason: str = "queue navigation", queue_candidate: QueueCandidate | None = None) -> bool:
        """Navigate to ``index`` of the committed queue.

        ``queue_candidate`` allows queue navigation that first replaces the
        committed queue. The prepared queue is published only after the
        transition committed, and failure keeps the old order/index/track
        context intact.
        """
        if queue_candidate is not None:
            if index < 0 or index >= len(queue_candidate.queue):
                return False
            next_track = dict(queue_candidate.queue[index])
            native_fields = {}
            native_jump = False
        else:
            if len(self.tracks) <= 1 or index < 0 or index >= len(self.tracks):
                return False
            next_track = dict(self.tracks[index])
            if self.mode == "native_mpv":
                player = self._player
                set_playlist_pos = getattr(player, "set_playlist_pos", None)
                if not callable(set_playlist_pos):
                    return False
                set_playlist_pos(index)
                self.index = index
                self._deps.set_track_context(next_track, next_track)
                return True
        target_url = str(next_track.get("url") or "")
        if not target_url:
            self.reset()
            return False
        source = str(next_track.get("source") or "local")
        target_rate = self._deps.coordinator_target_rate(source, next_track)
        request = TransitionRequest(
            operation="queue",
            source=source,
            target_rate=target_rate,
            target_url=target_url,
            target_track=next_track,
            should_play=True,
            rate_change=self._deps.coordinator_rate_change(target_rate),
            reload_source=True,
            detail=transition_reason,
        )
        try:
            result = await self._deps.run_transition(request)
        except PlaybackTransitionFailure as exc:
            raise self._deps.transition_error_http(exc) from exc
        if not getattr(result, "committed", False):
            raise HTTPException(status_code=500, detail="Playback transition was not committed")
        rate_updated = False
        if self._deps.sample_rate_policy_is_auto() and source in {"local", "radio"} and isinstance(result.target_rate, int) and result.target_rate > 0:
            next_track["sample_rate_hz"] = result.target_rate
            rate_updated = True
        if queue_candidate is not None:
            self.commit(queue_candidate)
        if rate_updated and 0 <= index < len(self.tracks):
            self.tracks[index]["sample_rate_hz"] = result.target_rate
        if queue_candidate is None:
            self.index = index
        self._deps.commit_coordinated_track(
            next_track, source=source, commit_token=getattr(result, "transition_id", None)
        )
        return True

    async def advance(self, *, transition_reason: str = "queue advance") -> str:
        """Advance the committed queue.

        Returns an explicit tri-state outcome instead of a plain bool so the
        API can distinguish a successful terminal queue end from a navigation
        that was not possible at all:

        ``"advanced"``
            Another queue track was committed successfully.
        ``"ended"``
            The terminal queue end state was committed successfully (the
            queue is cleared, matching the auto-EOF end semantics).  The
            queue mutation happened before this value is returned; the API
            must represent it as a successful response, never as an error.
        ``"unavailable"``
            No navigation was possible and no authoritative queue state was
            changed.
        """
        if len(self.tracks) <= 1:
            return "unavailable"
        next_index = self.index + 1
        if next_index >= len(self.tracks):
            if self.mode == "native_mpv":
                if self.loop:
                    next_index = 0
                else:
                    return "unavailable"
            else:
                if self.loop:
                    if self.shuffle:
                        next_index = 0
                    else:
                        next_index = 0
                else:
                    self.reset()
                    return "ended"
        return (
            "advanced"
            if await self.load_track(next_index, transition_reason=transition_reason)
            else "unavailable"
        )

    async def rewind(self, *, transition_reason: str = "queue rewind") -> bool:
        if len(self.tracks) <= 1:
            return False
        prev_index = self.index - 1
        if prev_index < 0:
            return False
        return await self.load_track(prev_index, transition_reason=transition_reason)

    async def _reorder_native_mpv_playlist(
        self,
        target_queue: list[dict],
        target_index: int,
        enabled: bool,
    ) -> bool:
        """Reorder MPV without committing app state until every IPC call succeeds."""
        player = self._player
        set_playlist_pos = getattr(player, "set_playlist_pos", None)
        move_playlist_entry = getattr(player, "move_playlist_entry", None)
        set_loop_playlist = getattr(player, "set_loop_playlist", None)
        if not callable(move_playlist_entry) or not callable(set_playlist_pos):
            return False

        try:
            current_order = [dict(track) for track in self.tracks]
            for desired_index, desired_track in enumerate(target_queue):
                match_index = next(
                    (
                        index
                        for index in range(desired_index, len(current_order))
                        if current_order[index].get("id") == desired_track.get("id")
                        or (
                            not desired_track.get("id")
                            and current_order[index].get("url") == desired_track.get("url")
                        )
                    ),
                    None,
                )
                if match_index is None:
                    raise RuntimeError("Native MPV queue entry is missing")
                if match_index != desired_index:
                    move_playlist_entry(match_index, desired_index)
                    entry = current_order.pop(match_index)
                    current_order.insert(desired_index, entry)
            if callable(set_loop_playlist):
                set_loop_playlist(bool(self.loop))
            if target_index < 0 or target_index >= len(current_order):
                raise RuntimeError("Native MPV queue index is invalid")
            if current_order[target_index].get("url") != (player.state if player else {}).get("current_file"):
                set_playlist_pos(target_index)
            return True
        except Exception:
            logger.warning(
                "Native queue reorder failed; reducing MPV to its current entry",
                exc_info=True,
            )
            try:
                self.reduce_native_playlist_to_current()
                self.reset_mpv_loop_state()
                # MPV is now intentionally current-entry-only, so discard the
                # old committed queue rather than reporting an app/MPV mismatch.
                self.tracks = []
                self.original = []
                self.index = -1
                self.mode = "app_replace"
                self.loop = False
                self.shuffle = False
                self.single_track_loop = False
            except Exception:
                logger.warning("Failed to normalize MPV after native queue reorder failure", exc_info=True)
            raise

    async def set_shuffle(self, enabled: bool) -> bool:
        if self._deps.transition_is_active():
            raise HTTPException(status_code=409, detail="A playback transition is in progress")
        if len(self.tracks) <= 1:
            self.shuffle = False
            return False
        current_index = self.index if 0 <= self.index < len(self.tracks) else 0
        current_track = dict(self.tracks[current_index])
        current_track_id = current_track.get("id")
        current_track_url = current_track.get("url")

        if enabled:
            target_queue = [dict(track) for track in self.tracks[:current_index + 1]]
            future = [dict(track) for track in self.tracks[current_index + 1:]]
            random.shuffle(future)
            target_queue.extend(future)
            target_index = current_index
        elif self.original:
            target_queue = [dict(track) for track in self.original]
            target_index = next(
                (
                    index
                    for index, track in enumerate(target_queue)
                    if (
                        current_track_id is not None
                        and track.get("id") == current_track_id
                    )
                    or (
                        current_track_id is None
                        and current_track_url
                        and track.get("url") == current_track_url
                    )
                ),
                min(current_index, len(target_queue) - 1),
            )
        else:
            target_queue = [dict(track) for track in self.tracks]
            target_index = current_index

        if self.mode == "native_mpv":
            # Shuffle ON keeps the current entry at position zero.  Rebuild the
            # native playlist directly so changing order does not enter the full
            # output-graph transition path.
            if await self._reorder_native_mpv_playlist(target_queue, target_index, enabled):
                self.tracks = target_queue
                self.index = target_index
                self.shuffle = bool(enabled)
                self._deps.set_track_context(dict(target_queue[self.index]), dict(target_queue[self.index]))
                return True

        if self.mode == "native_mpv":
            # Replacing a native playlist changes the source staging boundary and
            # therefore belongs to the Coordinator.  Keep the old queue visible
            # until this gated replacement commits successfully.
            target_track = dict(target_queue[target_index])
            target_url = str(target_track.get("url") or "")
            if not target_url:
                return False
            player_state = self._player.state if self._player else {}
            should_play = bool(
                player_state.get("playing")
                and not player_state.get("paused")
                and not player_state.get("ended")
            )
            target_rate = self._deps.coordinator_target_rate("local", target_track)
            try:
                result = await self._deps.run_transition(TransitionRequest(
                    operation="queue",
                    source="local",
                    target_rate=target_rate,
                    target_url=target_url,
                    target_track=target_track,
                    should_play=should_play,
                    rate_change=self._deps.coordinator_rate_change(target_rate),
                    reload_source=True,
                    detail="queue-shuffle-on" if enabled else "queue-shuffle-off",
                    native_queue=tuple(target_queue),
                    native_queue_index=target_index,
                    native_queue_loop=bool(self.loop),
                    native_queue_shuffle=False,
                ))
            except PlaybackTransitionFailure:
                raise
            if not getattr(result, "committed", False):
                raise HTTPException(status_code=500, detail="Playback transition was not committed")

            committed_rate = getattr(result, "target_rate", None)
            if isinstance(committed_rate, int) and committed_rate > 0:
                for track in target_queue:
                    track["sample_rate_hz"] = committed_rate
            self.tracks = target_queue
            self.index = target_index
            self.shuffle = bool(enabled)
            self._deps.set_track_context(dict(target_queue[target_index]), dict(target_queue[target_index]))
            return True

        self.tracks = target_queue
        self.index = target_index
        self.shuffle = bool(enabled)
        return True

    def set_loop(self, enabled: bool) -> bool:
        has_local_track = bool(self._deps.get_current_track_info() and self._deps.get_current_track_info().get("source") == "local")
        if not has_local_track:
            self.loop = False
            self.single_track_loop = False
            return False
        if len(self.tracks) > 1:
            self.loop = bool(enabled)
            self.single_track_loop = False
            if self.mode == "native_mpv" and self._deps.player_is_running():
                set_loop_playlist = getattr(self._player, "set_loop_playlist", None)
                if callable(set_loop_playlist):
                    set_loop_playlist(self.loop)
            return True
        self.single_track_loop = bool(enabled)
        self.loop = False
        return True

    def sync_active_local_queue_selection(self, queue_track_ids: Optional[list[str]] = None, shuffle: bool = False, loop: bool = False, *, tracks: Optional[list] = None) -> dict:
        current_track = dict(self._deps.get_current_track_info() or {})
        if current_track.get("source") != "local" or not current_track.get("id"):
            raise HTTPException(status_code=409, detail="Local playback is not active")

        player_state = self._player.state if self._player else {}
        if not player_state.get("current_file") or player_state.get("ended"):
            raise HTTPException(status_code=409, detail="Nothing is currently loaded to update")

        if self.mode == "native_mpv":
            self.reduce_native_playlist_to_current()
            self.reset_mpv_loop_state()

        candidate = self.prepare_local_queue(
            current_track["id"],
            queue_track_ids,
            shuffle=shuffle,
            loop=loop,
            reshuffle=False,
            tracks=tracks,
        )
        self.commit(candidate)
        track_info = candidate.track
        self._deps.set_track_context(track_info, track_info)

        if len(self.tracks) > 1:
            self.mode = "app_replace"

        player = self._player
        if player and player._running:
            self.reset_mpv_loop_state()

        return self._deps.build_playback_payload(player_state)

    def sync_index_from_mpv(self, state: dict) -> tuple[int, dict] | None:
        """Mirror MPV's native playlist position into the committed index.

        Once a homogeneous queue has been committed, MPV owns natural playlist
        boundaries.  A path/playlist-pos event only updates the app-side queue
        index and returns the matching track context; it never starts another
        rate, DSP, graph or gate transition (the caller remains responsible
        for the app-context bookkeeping).
        """
        if (
            self.mode != "native_mpv"
            or len(self.tracks) <= 1
            or self._deps.transition_is_active()
            or state.get("ended")
            or state.get("paused")
            or not state.get("current_file")
        ):
            return None
        # MPV's playlist-pos is the native (possibly shuffled) playlist
        # position, not FXRoute's stable queue index. The current URL is the
        # authoritative cross-context identity; only use playlist-pos when it
        # also names that same app-side track.
        queue_index = next(
            (
                index
                for index, track in enumerate(self.tracks)
                if track.get("url") == state.get("current_file")
            ),
            None,
        )
        if queue_index is None:
            native_index = state.get("playlist_pos")
            if isinstance(native_index, int) and 0 <= native_index < len(self.tracks):
                candidate = self.tracks[native_index]
                if candidate.get("url") == state.get("current_file"):
                    queue_index = native_index
        if queue_index is None:
            return None
        track = dict(self.tracks[queue_index])
        self.index = queue_index
        return queue_index, track

    def normalize_after_native_loss(self) -> None:
        """Normalize after a staged failure invalidated MPV's native playlist.

        Trims MPV to its current file, resets its loop/shuffle controls and
        publishes the mode as ``app_replace``.  The mode is normalized even
        when the transport cleanup itself failed, so the retained queue data
        stays usable through app-owned navigation.
        """
        if self.mode != "native_mpv":
            return
        try:
            self.reduce_native_playlist_to_current()
            self.reset_mpv_loop_state()
        except Exception:
            logger.warning(
                "Failed to trim native playlist during native-loss normalization",
                exc_info=True,
            )
        self.mode = "app_replace"

    def reduce_native_playlist_to_current(self) -> None:
        """Keep MPV's current file and atomically drop all queued entries.

        ``playlist-clear`` is explicitly idempotent with respect to the
        currently played entry.  The former index loop was vulnerable to a
        concurrent MPV playlist change: a stale ``playlist-remove`` then
        aborted ``/api/play`` before the Coordinator could receive the new
        request.
        """
        if not self._deps.player_is_running():
            return
        player = self._player
        clear_playlist = getattr(player, "clear_playlist", None)
        if not callable(clear_playlist):
            # Keep small adapters used by maintenance/test contexts compatible;
            # production MPVWrapper exposes clear_playlist explicitly.
            send_command = getattr(player, "_send_command", None)
            if callable(send_command):
                clear_playlist = lambda: send_command("playlist-clear")
        if not callable(clear_playlist):
            raise RuntimeError("MPV adapter cannot clear its native playlist")
        try:
            clear_playlist()
        except Exception as exc:
            # A shortened playlist can race the clear command.  Only suppress
            # this narrow stale-entry case after a read-only proof that MPV is
            # already reduced to its current file; genuine IPC failures remain
            # fatal.
            if _native_mpv_playlist_error_is_stale(exc) and self._native_mpv_playlist_is_effectively_current_only():
                logger.info("Native MPV playlist was already reduced while clearing stale entries: %s", exc)
                return
            raise

    def reset_mpv_loop_state(self) -> None:
        player = self._player
        if not player or not player._running:
            return
        set_loop_playlist = getattr(player, "set_loop_playlist", None)
        if callable(set_loop_playlist):
            set_loop_playlist(False)
        set_loop_file = getattr(player, "set_loop_file", None)
        if callable(set_loop_file):
            set_loop_file(False)
        set_shuffle = getattr(player, "set_shuffle", None)
        if callable(set_shuffle):
            set_shuffle(False)

    def _native_mpv_playlist_is_effectively_current_only(self) -> bool:
        """Confirm that MPV already has no queued entry beyond the current file."""
        if not self._deps.player_is_running():
            return False
        player = self._player
        state = dict(getattr(player, "state", {}) or {})
        if not state.get("current_file"):
            return False
        get_property = getattr(player, "get_property", None)
        if not callable(get_property):
            return False
        try:
            playlist_count = get_property("playlist-count")
        except Exception:
            return False
        return isinstance(playlist_count, int) and playlist_count <= 1


def _native_mpv_playlist_error_is_stale(exc: Exception) -> bool:
    """Recognize only errors caused by an already-gone playlist entry."""
    message = str(exc).lower()
    command_error = "playlist-remove" in message or "playlist-clear" in message
    stale_state = any(
        marker in message
        for marker in (
            "already gone",
            "already removed",
            "playlist entry",
            "playlist index",
            "no such entry",
            "out of range",
            "is empty",
        )
    )
    return command_error and stale_state


queue: PlaybackQueue | None = None


def configure_playback_queue(deps: PlaybackQueueDependencies) -> PlaybackQueue:
    """Create the authoritative queue instance with the app-shell wiring.

    Production calls this exactly once at import time; the instance becomes
    the single owner of the queue state shared by API handlers, the player
    callback and ``playback_runtime.py``.
    """
    global queue
    queue = PlaybackQueue(deps)
    return queue
