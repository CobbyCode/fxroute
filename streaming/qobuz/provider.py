# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz streaming provider backed by qbzd.

qbzd is the compatible forks headless Qobuz daemon: it plays audio, exposes a Qobuz Connect
endpoint, publishes MPRIS and serves a small HTTP control plane. FXRoute talks
to the control plane only; qbzd-specific JSON never leaves this module — it is
normalized into the same flat wire shape Spotify already publishes.
"""

from __future__ import annotations

from typing import Any, ClassVar

from streaming.base.capabilities import Capabilities
from streaming.base.provider import StreamingProvider
from streaming.qobuz import backend, connect_state, login

QOBUZ_BACKEND = "qbzd"

# Fork repeat modes -> FXRoute loop vocabulary (Spotify parity: none/track/playlist).
_REPEAT_TO_LOOP = {"off": "none", "one": "track", "track": "track", "all": "playlist", "queue": "playlist"}
# Cycle order for the repeat toggle: off -> track -> queue -> off (fork values).
_LOOP_TO_REPEAT = {"none": "off", "track": "track", "playlist": "queue"}
_LOOP_CYCLE = {"none": "track", "track": "playlist", "playlist": "none"}


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _sample_rate_hz(value: Any) -> int | None:
    """Normalize a qbzd sample-rate value to Hz.

    ``/api/queue`` track objects report sample rates in kHz (44.1, 88.2, 192)
    while ``/api/playback`` reports the negotiated stream rate in Hz (44100).
    Values below 1000 are kHz and are scaled up; audio sample rates never
    legitimately fall below 1000 Hz, so the distinction is unambiguous.
    """
    try:
        if value is None:
            return None
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    if rate < 1000:
        rate *= 1000
    return int(rate)


def _id_str(value: Any) -> str:
    return "" if value is None else str(value)


def _normalize_state(state: Any, is_playing: Any) -> str:
    s = str(state or "").strip().lower()
    if s in {"playing", "loading"} or is_playing is True:
        return "Playing"
    if s == "paused":
        return "Paused"
    return "Stopped"


class QobuzProvider(StreamingProvider):
    """Qobuz playback via the qbzd daemon control plane."""

    provider_id: ClassVar[str] = "qobuz"
    display_name: ClassVar[str] = "Qobuz"

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url or backend.default_base_url()
        # Last known stream facts (trackId, sample_rate, bit_depth,
        # audio_format) so a transient now-playing gap during a pause or
        # transition never degrades a complete track's quality data.
        self._stream_facts: tuple = ("", None, None, None)

    def capabilities(self) -> Capabilities:
        # Implemented now: transport + now-playing (metadata, position, cover)
        # and the stream info qbzd exposes. Catalog (search/favorites/
        # playlists/...) is deliberately not declared yet: no provider methods
        # exist for it in this step.
        return Capabilities(
            transport=True,
            seek=True,
            shuffle=True,
            loop=True,
            progress=True,
            volume=True,
            cover=True,
            audio_format=True,
            sample_rate=True,
            bit_depth=True,
        )

    def is_installed(self) -> bool:
        return backend.qbzd_installed()

    async def is_available(self) -> bool:
        return backend.qbzd_installed() and await backend.is_reachable(self._base_url)

    async def backend(self) -> str | None:
        return QOBUZ_BACKEND if backend.qbzd_installed() else None

    async def is_authenticated(self) -> bool | None:
        """Return whether qbzd has a logged-in Qobuz account."""
        if not backend.qbzd_installed():
            return False
        status_doc = await backend.get_json(self._base_url, "/api/status")
        if status_doc is None:
            return False
        return status_doc.get("logged_in") is True

    async def status(self) -> dict:
        result: dict[str, Any] = {
            "available": await self.is_available(),
            "installed": self.is_installed(),
            "source": self.provider_id,
            "backend": await self.backend(),
            "authenticated": False,
            "connected": False,
            "capabilities": self.capabilities().to_dict(),
            "status": "Stopped",
            "artist": "",
            "title": "",
            "album": "",
            "trackId": "",
            "artUrl": "",
            "shuffle": False,
            "loop": "none",
            "position": 0.0,
            "duration": 0.0,
            "volume": 100,
            "sample_rate": None,
            "bit_depth": None,
            "audio_format": None,
            "queue_len": 0,
            "queue_index": 0,
            "next_track": None,
            "qconnect": None,
        }

        # A missing daemon/binary must short-circuit before any HTTP call.
        if not result["available"]:
            return result

        status_doc = await backend.get_json(self._base_url, "/api/status")
        if status_doc is None:
            return result

        result["authenticated"] = status_doc.get("logged_in") is True
        session_active = bool(status_doc.get("qconnect"))
        result["connected"] = session_active
        result["qconnect"] = {
            "enabled": True,
            "device_name": None,
            "session_active": session_active,
            "state": status_doc.get("state"),
        }

        # Transport facts come from /api/playback; the rich track object
        # (QueueTrack shape) from /api/queue current_track. An
        # unauthenticated daemon reports neither; the card then shows an
        # honest stopped state with no metadata.
        playback = await backend.get_json(self._base_url, "/api/playback") or {}
        queue_state = await backend.get_json(self._base_url, "/api/queue") or {}
        queue_current = queue_state.get("current_track") if isinstance(queue_state, dict) else None
        track = queue_current if isinstance(queue_current, dict) else None

        if track is not None:
            result["title"] = track.get("title") or ""
            result["artist"] = track.get("artist") or ""
            result["album"] = track.get("album") or ""
            result["trackId"] = _id_str(track.get("id"))
            result["artUrl"] = track.get("artwork_url") or ""
            result["duration"] = float(track.get("duration_secs") or 0)
            result["sample_rate"] = _sample_rate_hz(track.get("sample_rate"))
            result["bit_depth"] = _int_or_none(track.get("bit_depth"))
            # Qobuz streams FLAC at every quality tier; the ``hires`` flag
            # only distinguishes 16/24-bit (reported as ``bit_depth``).
            result["audio_format"] = "flac"
        else:
            raw_track_id = playback.get("track_id")
            result["trackId"] = "" if raw_track_id in (None, 0) else _id_str(raw_track_id)
            result["duration"] = float(playback.get("duration_secs") or 0)

        result["status"] = _normalize_state(playback.get("state"), None)
        result["position"] = float(playback.get("position_secs") or 0)
        result["shuffle"] = bool(queue_state.get("shuffle")) if isinstance(queue_state, dict) else False
        result["loop"] = _REPEAT_TO_LOOP.get(
            str((queue_state.get("repeat") if isinstance(queue_state, dict) else "") or "off").lower(),
            "none",
        )

        volume = playback.get("volume")
        if isinstance(volume, (int, float)):
            result["volume"] = max(0, min(100, round(float(volume) * 100)))

        # The negotiated stream rate/depth from /api/playback fill any
        # track-level gap (both are present while a stream is open).
        if result["sample_rate"] is None:
            result["sample_rate"] = _sample_rate_hz(playback.get("sample_rate"))
        if result["bit_depth"] is None:
            result["bit_depth"] = _int_or_none(playback.get("bit_depth"))
        if result["bit_depth"] is not None and result["bit_depth"] <= 0:
            result["bit_depth"] = None

        # Keep the last known stream facts per track, field by field: a
        # transient now-playing gap (pause/transition) must not degrade a
        # complete track's quality data, otherwise the footer tag collapses to
        # the bare rate. A same-track reading fills any gap from the remembered
        # fact, but a partial reading never erases a known field (audio_format
        # in particular is only ever set by the now-playing track object). Facts
        # are never borrowed across tracks, and an unattributed reading (no
        # track id at all) neither restores nor clobbers the remembered facts:
        # the next attributable reading of the same track still fills its gaps.
        track_id = result.get("trackId") or ""
        if track_id and track_id != self._stream_facts[0]:
            # Track changed: remember exactly this reading.
            self._stream_facts = (track_id, result["sample_rate"], result["bit_depth"], result["audio_format"])
        elif track_id and track_id == self._stream_facts[0]:
            prev_rate, prev_depth, prev_fmt = self._stream_facts[1], self._stream_facts[2], self._stream_facts[3]
            if result["sample_rate"] is None:
                result["sample_rate"] = prev_rate
            if result["bit_depth"] is None:
                result["bit_depth"] = prev_depth
            if result["audio_format"] is None:
                result["audio_format"] = prev_fmt
            self._stream_facts = (track_id, result["sample_rate"], result["bit_depth"], result["audio_format"])

        # Queue context: count, 1-based current position and the first upcoming
        # track. Kept compact; FXRoute never needs the full Connect queue.
        if isinstance(queue_state, dict):
            total = queue_state.get("total_tracks")
            current_index = queue_state.get("current_index")
            if isinstance(total, int):
                result["queue_len"] = max(0, total)
            if isinstance(current_index, int):
                result["queue_index"] = max(0, current_index)
            upcoming = queue_state.get("upcoming")
            if isinstance(upcoming, list) and upcoming:
                nxt = upcoming[0]
                if isinstance(nxt, dict):
                    result["next_track"] = {
                        "id": _id_str(nxt.get("id")),
                        "title": nxt.get("title") or "",
                        "artist": nxt.get("artist") or "",
                        "album": nxt.get("album") or "",
                        "artUrl": nxt.get("artwork_url") or "",
                    }

        # Standby parity with spotifyd: the daemon is up but FXRoute is not
        # the actively selected Qobuz output. Selection is journal-tracked
        # (streaming.qobuz.connect_state): qbzd keeps the last track paused
        # with its metadata after deselection and session_active stays true,
        # so play/pause alone must not decide. Unknown selection state (fresh
        # start, no journal evidence) conservatively shows the card.
        result["qbzd_standby"] = (
            result["status"] in ("Stopped", "Paused")
            and connect_state.is_device_active() is False
        )

        return result

    async def play(self) -> dict:
        await backend.post_json(self._base_url, "/api/playback/play")
        return await self.status()

    async def pause(self) -> dict:
        await backend.post_json(self._base_url, "/api/playback/pause")
        return await self.status()

    async def next(self) -> dict:
        await backend.post_json(self._base_url, "/api/playback/next")
        return await self.status()

    async def previous(self) -> dict:
        await backend.post_json(self._base_url, "/api/playback/previous")
        return await self.status()

    async def toggle(self) -> dict:
        # The fork exposes no toggle endpoint: derive it from the live state.
        current = await self.status()
        if current.get("status") == "Playing":
            await backend.post_json(self._base_url, "/api/playback/pause")
        else:
            await backend.post_json(self._base_url, "/api/playback/play")
        return await self.status()

    async def shuffle(self) -> dict:
        # The fork switches shuffle on the queue, not on playback.
        current = await self.status()
        await backend.post_json(
            self._base_url, "/api/queue/shuffle", {"enabled": not bool(current.get("shuffle"))}
        )
        return await self.status()

    async def repeat(self) -> dict:
        # The fork switches repeat on the queue with its own mode names.
        current = await self.status()
        next_loop = _LOOP_CYCLE.get(str(current.get("loop") or "none"), "none")
        await backend.post_json(self._base_url, "/api/queue/repeat", {"mode": _LOOP_TO_REPEAT[next_loop]})
        return await self.status()

    async def seek(self, position_sec: float) -> dict:
        # The fork seeks in milliseconds.
        await backend.post_json(
            self._base_url, "/api/playback/seek", {"position_ms": max(0, int(position_sec * 1000))}
        )
        return await self.status()

    async def set_volume(self, percent: float) -> dict:
        normalized = max(0.0, min(1.0, percent / 100.0))
        await backend.post_json(self._base_url, "/api/playback/volume", {"volume": normalized})
        return await self.status()

    # -- account login / re-auth / logout (daemon HTTP OAuth) -----------

    async def begin_login(self) -> dict:
        """Start the qbzd browser OAuth flow and return its sign-in URL."""
        return await login.begin_login()

    async def finish_login(self, pasted: str) -> dict:
        """Complete the qbzd browser OAuth flow with the pasted redirect."""
        result = await login.finish_login(pasted)
        result["authenticated"] = await self.is_authenticated()
        return result

    async def login_state(self) -> dict:
        """Return whether a qbzd browser login is currently in flight."""
        return await login.state()

    async def cancel_login(self) -> dict:
        """Terminate any in-flight qbzd browser login."""
        result = await login.cancel()
        result["authenticated"] = await self.is_authenticated()
        return result

    async def logout(self) -> dict:
        """Clear the qbzd credential (daemon credential reset)."""
        result = await login.logout()
        result["authenticated"] = await self.is_authenticated()
        return result
