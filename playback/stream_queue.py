# SPDX-License-Identifier: AGPL-3.0-only
"""Prepare one same-rate streaming successor for an uninterrupted MPV boundary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import logging
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playback.queue import PlaybackQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PreparedSuccessor:
    index: int
    url: str
    entry_id: int | None
    current_entry_id: int
    key: tuple
    intent: object
    track: dict


class StreamQueuePrefetch:
    """Own the rolling native successor, not the committed app queue or graph."""

    def __init__(self, queue: PlaybackQueue) -> None:
        self._queue = queue
        self._generation = 0
        self._key: tuple | None = None
        self._task: asyncio.Task | None = None
        self._prepared: _PreparedSuccessor | None = None
        self._native_entries = False
        self._native_identity: tuple[int, int] | None = None
        self._readiness_attempts = 0
        self._retry_after: float | None = None

    @property
    def has_prepared(self) -> bool:
        return self._prepared is not None and self._prepared.entry_id is not None

    @property
    def has_native_entries(self) -> bool:
        return self._native_entries

    def invalidate(self, *, trim: bool = False, guarded: bool = False) -> None:
        if trim and self._native_entries:
            self.trim_to_current(guarded=guarded)
        self._generation += 1
        self._key = None
        self._prepared = None
        self._native_entries = False
        self._native_identity = None
        self._readiness_attempts = 0
        self._retry_after = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def _read_current_only_entry(self) -> dict | None:
        playlist = self._queue._player.get_property("playlist")
        if (not isinstance(playlist, list) or len(playlist) > 1
                or any(not isinstance(entry, dict)
                       or not isinstance(entry.get("id"), int)
                       or not isinstance(entry.get("filename"), str) for entry in playlist)):
            raise RuntimeError("MPV current-only playlist was not confirmed")
        return playlist[0] if playlist else None

    def trim_to_current(self, *, guarded: bool = False) -> dict | None:
        """Verify what MPV retained before retiring successor ownership."""
        if (not self._queue._deps.player_is_running()
                or self._native_identity != self._player_identity()):
            # A dead/replaced process cannot retain our queued entry. Never
            # inspect or clear a replacement process's unrelated playlist.
            self._native_entries = False
            self._native_identity = None
            self._prepared = None
            return None
        self.reconcile_current(guarded=guarded)
        clear_error = None
        try:
            self._queue.reduce_native_playlist_to_current()
        except Exception as exc:
            clear_error = exc
        try:
            entry = self._read_current_only_entry()
        except Exception:
            if clear_error is not None:
                raise clear_error
            raise
        if entry is not None:
            self._publish_current(self._consume_successor(entry["id"], entry["filename"]))
        self._native_entries = False
        self._native_identity = None
        return entry

    def _epoch(self):
        reader = getattr(self._queue._deps, "stream_prefetch_epoch", None)
        return reader() if callable(reader) else 0

    def _player_identity(self) -> tuple[int, int]:
        player = self._queue._player
        return id(player), getattr(player, "process_generation", 0)

    def _owns_stream(self, path: str, *, guarded: bool = False) -> bool:
        queue = self._queue
        deps = queue._deps
        owner = getattr(deps, "get_playback_owner", None)
        if callable(owner) and owner() != "tidal":
            return False
        current = deps.get_current_track_info() or {}
        state = queue._player.state
        return bool(
            deps.player_is_running()
            and (guarded or not deps.transition_is_active())
            and queue.mode == "app_replace"
            and current.get("source") == "tidal"
            and current.get("url") == path
            and state.get("current_file") == path
            and not state.get("ended")
        )

    def schedule(self) -> asyncio.Task | None:
        queue = self._queue
        deps = queue._deps
        if deps.resolve_stream_url is None or len(queue.tracks) <= 1:
            return None
        if not 0 <= queue.index < len(queue.tracks):
            return None
        path = str((deps.get_current_track_info() or {}).get("url") or "")
        if not path or not self._owns_stream(path):
            return None
        next_index = queue.index + 1
        if next_index >= len(queue.tracks):
            if not queue.loop:
                return None
            next_index = 0
        next_track = dict(queue.tracks[next_index])
        if next_track.get("source") != "tidal" or not next_track.get("id"):
            return None
        key = (self._generation, id(queue.tracks), queue.index, self._epoch(), path,
               self._player_identity())
        if self._prepared is not None:
            # A non-reloading graph transition changes the epoch but not the
            # confirmed MPV entries. Keep their identity until the boundary.
            live_entry_id = queue._player.state.get("playlist_entry_id")
            if self._prepared_context_is_current() and live_entry_id in {
                self._prepared.current_entry_id, self._prepared.entry_id
            }:
                return self._task
            self._prepared = None
        if key == self._key:
            if self._retry_after is None or monotonic() < self._retry_after:
                return self._task
        else:
            self._readiness_attempts = 0
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return None
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._key = key
        self._prepared = None
        self._retry_after = None
        intent = queue._capture_source_intent()
        create = getattr(deps, "create_background_task", None) or asyncio.create_task
        self._task = create(
            self._prepare(key, intent, next_index, next_track), name="tidal-stream-successor"
        )
        return self._task

    def _is_current(self, key: tuple, intent, *, guarded: bool = False) -> bool:
        queue = self._queue
        if key != self._key or key[5] != self._player_identity() or key[:4] != (
            self._generation, id(queue.tracks), queue.index, self._epoch()
        ):
            return False
        if not self._owns_stream(key[4], guarded=guarded):
            return False
        try:
            queue._ensure_source_intent_current(intent)
        except Exception:
            return False
        return True

    @asynccontextmanager
    async def _transport_guard(self):
        guard = getattr(self._queue._deps, "stream_transport_guard", None)
        if callable(guard):
            async with guard():
                yield
        else:
            yield

    async def _prepare(self, key: tuple, intent, next_index: int, track: dict) -> None:
        queue = self._queue
        try:
            url = await queue._deps.resolve_stream_url(track)
            if not url or not self._is_current(key, intent):
                return
            rate = track.get("sample_rate_hz")
            if not isinstance(rate, int) or rate <= 0:
                return
            async with self._transport_guard():
                # The guard itself holds the Coordinator lock, which its
                # transition_active reader also reports as active. Epoch and
                # intent checks still reject transitions queued meanwhile.
                if not self._is_current(key, intent, guarded=True):
                    return
                player = queue._player
                # Compare the decoded source rate, not a fixed hardware/SRC
                # policy rate stored in the committed playback payload.
                if player.get_property("path") != key[4]:
                    return
                params = player.get_property("audio-params") or {}
                if not params.get("samplerate"):
                    self._readiness_attempts += 1
                    if self._readiness_attempts < 3:
                        self._retry_after = monotonic() + 0.5
                    return
                if params.get("samplerate") != rate:
                    logger.info("TIDAL successor requires coordinated rate change: track=%s rate=%s",
                                track.get("id"), rate)
                    return
                # No await between validation and these transport-only IPC
                # calls: a queue/source commit cannot interleave an append.
                player.clear_playlist()
                current_entry = self._read_current_only_entry()
                if current_entry is None or current_entry["filename"] != key[4]:
                    return
                track["url"] = url
                # Retain the append intent until readback confirms its ID or
                # cleanup proves whether MPV kept the original or successor.
                self._prepared = _PreparedSuccessor(
                    next_index, url, None, current_entry["id"], key, intent, dict(track)
                )
                # An append may succeed even if its IPC response is lost.
                self._native_entries = True
                self._native_identity = self._player_identity()
                try:
                    player.loadfile(url, mode="append")
                    playlist = player.get_property("playlist")
                    if (not isinstance(playlist, list) or len(playlist) != 2
                            or not all(isinstance(entry, dict) for entry in playlist)
                            or playlist[0].get("filename") != key[4]
                            or playlist[1].get("filename") != url
                            or not isinstance(playlist[0].get("id"), int)
                            or not isinstance(playlist[1].get("id"), int)):
                        raise RuntimeError("MPV successor identity was not confirmed")
                except Exception:
                    # Cleanup stays under the transport lock. Retain ownership
                    # if cleanup fails so reset/play can still retire the entry.
                    try:
                        self.trim_to_current(guarded=True)
                    except Exception:
                        logger.warning("Failed to remove unconfirmed TIDAL successor", exc_info=True)
                    else:
                        self._prepared = None
                    raise
                queue.tracks[next_index] = dict(track)
                self._prepared = replace(self._prepared, entry_id=playlist[1]["id"])
                logger.info("TIDAL native successor prepared: index=%s track=%s rate=%s",
                            next_index, track.get("id"), rate)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("TIDAL successor preparation failed: track=%s", track.get("id"),
                           exc_info=True)

    def _prepared_context_is_current(self) -> bool:
        queue = self._queue
        prepared = self._prepared
        if (prepared is None or prepared.key[0] != self._generation
                or prepared.key[1] != id(queue.tracks) or prepared.key[2] != queue.index
                or prepared.key[5] != self._player_identity()):
            return False
        owner = getattr(queue._deps, "get_playback_owner", None)
        if callable(owner) and owner() != "tidal":
            return False
        if (queue._deps.get_current_track_info() or {}).get("source") != "tidal":
            return False
        try:
            queue._ensure_source_intent_current(prepared.intent)
        except Exception:
            return False
        return queue._deps.player_is_running()

    def sync_index(self, state: dict) -> tuple[int, dict] | None:
        player = self._queue._player
        live = player.state if player is not None else None
        if not isinstance(live, dict):
            return None
        if (state.get("playlist_entry_id") != live.get("playlist_entry_id")
                or state.get("current_file") != live.get("current_file") or live.get("ended")):
            return None
        return self._consume_successor(state.get("playlist_entry_id"), state.get("current_file"))

    def _consume_successor(self, entry_id: int | None, url: str | None) -> tuple[int, dict] | None:
        if not self._prepared_context_is_current():
            return None
        queue = self._queue
        prepared = self._prepared
        index = prepared.index
        if (not isinstance(entry_id, int) or url != prepared.url
                or not 0 <= index < len(queue.tracks)
                or queue.tracks[index].get("id") != prepared.track.get("id")):
            return None
        if prepared.entry_id is None:
            if entry_id == prepared.current_entry_id:
                return None
            queue.tracks[index] = dict(prepared.track)
        elif entry_id != prepared.entry_id or queue.tracks[index].get("url") != url:
            return None
        self._prepared = None
        return index, dict(queue.tracks[index])

    def _publish_current(self, synced: tuple[int, dict] | None) -> None:
        if synced is None:
            return
        queue = self._queue
        queue.index = synced[0]
        queue._deps.set_track_context(synced[1], synced[1])
        mark = getattr(queue._deps, "mark_playback_intent_changed", None)
        if callable(mark):
            mark()

    def reconcile_current(self, *, guarded: bool = False) -> None:
        """Publish an already-crossed native boundary before queue mutations."""
        if self._prepared is None:
            return
        queue = self._queue
        player = queue._player
        if player is None:
            return
        synced = queue.sync_index_from_mpv(player.state, guarded=guarded)
        self._publish_current(synced)
