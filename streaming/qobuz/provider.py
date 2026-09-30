# SPDX-License-Identifier: AGPL-3.0-only

"""Qobuz streaming provider backed by qbzd.

qbzd is the compatible forks headless Qobuz daemon: it plays audio, exposes a Qobuz Connect
endpoint, publishes MPRIS and serves a small HTTP control plane. FXRoute talks
to the control plane only; qbzd-specific JSON never leaves this module — it is
normalized into the same flat wire shape Spotify already publishes.
"""

from __future__ import annotations

import asyncio
import math
import time
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

# Track downloads run server-side before the play-track reply; a 20MB+
# FLAC fetch needs far more than the 2s control-plane default.
PLAY_TRACK_TIMEOUT = 120.0
PLAY_TRACK_CONFIRM_TIMEOUT_S = 3.0
SEEK_CONFIRM_TIMEOUT_S = 3.0
SEEK_CONFIRM_INTERVAL_S = 0.05


class QobuzSeekError(RuntimeError):
    """The daemon did not accept a seek command."""


class QobuzSeekPositionError(ValueError):
    """The requested seek position is invalid."""


class QobuzSeekNotConfirmed(QobuzSeekError):
    """The daemon acknowledged a seek without confirming its execution."""


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _sample_rate_hz(value: Any) -> int | None:
    """Normalize a qbzd sample-rate value to Hz.

    ``/api/queue`` reports catalog maximum rates in kHz (44.1, 88.2, 192),
    while ``/api/playback`` reports the decoded track rate in Hz (44100).
    Neither reports the PipeWire renderer's possibly resampled output rate.
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


def normalize_state(state: Any, is_playing: Any) -> str:
    s = str(state or "").strip().lower()
    if s in {"playing", "loading"} or is_playing is True:
        return "Playing"
    if s == "paused":
        return "Paused"
    return "Stopped"


def navigation_has_no_target(state: dict, action: str) -> bool:
    """Recognize a known queue boundary without changing daemon transport."""
    length = state.get("queue_len")
    if state.get("available") is False or not isinstance(length, int):
        return False
    if length == 0:
        return True
    # get_state exposes upcoming in the actual shuffle order, even for One.
    return bool(action == "next" and state.get("loop") != "playlist"
                and "next_track" in state and state["next_track"] is None)


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
        raw_track_id = playback.get("track_id")
        player_track_id = "" if raw_track_id in (None, 0) else _id_str(raw_track_id)
        if player_track_id and (track is None or _id_str(track.get("id")) != player_track_id):
            # Navigation selects the queue before audio loads. Never combine
            # the new queue track with the old player's transport or rate.
            candidates = (queue_state.get("history") or []) + (queue_state.get("upcoming") or [])
            track = next((item for item in candidates if isinstance(item, dict)
                          and _id_str(item.get("id")) == player_track_id), None)

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
            result["trackId"] = player_track_id
            result["duration"] = float(playback.get("duration_secs") or 0)

        result["status"] = normalize_state(playback.get("state"), None)
        result["position"] = float(playback.get("position_secs") or 0)
        result["shuffle"] = bool(queue_state.get("shuffle")) if isinstance(queue_state, dict) else False
        result["loop"] = _REPEAT_TO_LOOP.get(
            str((queue_state.get("repeat") if isinstance(queue_state, dict) else "") or "off").lower(),
            "none",
        )

        volume = playback.get("volume")
        if isinstance(volume, (int, float)):
            result["volume"] = max(0, min(100, round(float(volume) * 100)))

        # Decoded facts take precedence over catalog maxima, but only for the
        # loaded identity. The renderer format is observed separately in PW.
        if player_track_id and player_track_id == result["trackId"]:
            decoded_rate = _sample_rate_hz(playback.get("sample_rate"))
            decoded_depth = _int_or_none(playback.get("bit_depth"))
            if decoded_rate is not None:
                result["sample_rate"] = decoded_rate
            if decoded_depth is not None and decoded_depth > 0:
                result["bit_depth"] = decoded_depth
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
            # Automatic EOF clears the queue marker; get_state then exposes
            # the whole queue as upcoming, not a new manual Next target.
            duration = float(playback.get("duration_secs") or 0)
            exhausted = bool(
                queue_current is None and player_track_id
                and result["status"] in {"Paused", "Stopped"}
                and duration > 0 and result["position"] >= duration
            )
            if isinstance(upcoming, list) and upcoming and not exhausted:
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
        return await self._navigate("next")

    async def previous(self) -> dict:
        return await self._navigate("previous")

    async def _navigate(self, action: str) -> dict:
        """Select the fork queue target, load audio and confirm its identity."""
        queue = await backend.get_json(self._base_url, "/api/queue")
        if queue is None:
            raise RuntimeError(f"qbzd {action} queue readback failed")
        repeat_one = str(queue.get("repeat") or "").lower() in {"one", "track"}
        if action == "next" and repeat_one:
            # The fork reuses its automatic advance for manual Next, so One
            # otherwise selects the same track. Suspend One only for queue
            # selection (including shuffle); retain the user's repeat choice.
            changed = await backend.post_json(self._base_url, "/api/queue/repeat", {"mode": "off"})
            if changed is None:
                raise RuntimeError("qbzd manual next could not suspend repeat-one")
            try:
                result = await backend.post_json(self._base_url, f"/api/playback/{action}")
            finally:
                restored = await backend.post_json(self._base_url, "/api/queue/repeat", {"mode": "one"})
                if restored is None:
                    raise RuntimeError("qbzd manual next could not restore repeat-one")
        else:
            result = await backend.post_json(self._base_url, f"/api/playback/{action}")
        if not isinstance(result, dict) or "track" not in result:
            raise RuntimeError(f"qbzd {action} request failed")
        track = result["track"]
        if track is None:
            return {**await self.status(), "navigation_changed": False}
        if not isinstance(track, dict) or not _int_or_none(track.get("id")):
            raise RuntimeError(f"qbzd {action} returned no valid track")
        track_id = _id_str(track["id"])
        state = await self.play_track(track_id)
        deadline = time.monotonic() + PLAY_TRACK_CONFIRM_TIMEOUT_S
        while True:
            loaded_id = await self.loaded_track_id()
            if (str(loaded_id) == track_id and state.get("trackId") == track_id
                    and state.get("status") == "Playing"):
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.05, remaining))
            try:
                state = await asyncio.wait_for(self.status(), max(0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
        raise RuntimeError(f"qbzd {action} track was not confirmed: expected={track_id} actual={state.get('trackId')}")

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
        if not math.isfinite(position_sec):
            raise QobuzSeekPositionError("Seek position must be finite")
        original = await self.status()
        if not original.get("available"):
            raise QobuzSeekError("qbzd is unavailable")
        observed_at = time.monotonic()
        original_playback = await backend.get_json(self._base_url, "/api/playback")
        if original_playback is None:
            raise QobuzSeekError("qbzd playback readback failed")
        track_id = _id_str(original_playback.get("track_id"))
        if track_id in {"", "0"}:
            raise QobuzSeekNotConfirmed("No Qobuz track is loaded")
        if not original.get("trackId"):
            raise QobuzSeekError("qbzd status readback is incomplete")
        if original.get("trackId") != track_id:
            raise QobuzSeekNotConfirmed("Qobuz track changed before seek")
        original_position = float(original_playback.get("position_secs") or 0)
        was_playing = normalize_state(
            original_playback.get("state"), original_playback.get("is_playing")
        ) == "Playing"
        target = max(0, int(position_sec))
        duration = original_playback.get("duration_secs") or 0
        if math.isfinite(duration) and duration > 0:
            target = min(target, int(duration))
        # The daemon accepts unsigned whole seconds, not Connect milliseconds.
        result = await backend.post_json(
            self._base_url, "/api/playback/seek", {"position_secs": target}
        )
        if result is None:
            raise QobuzSeekError("qbzd seek request failed")
        started = time.monotonic()
        deadline = started + SEEK_CONFIRM_TIMEOUT_S
        moved = target == original_position
        while time.monotonic() < deadline:
            try:
                state = await asyncio.wait_for(self.status(), deadline - time.monotonic())
                playback = await asyncio.wait_for(
                    backend.get_json(self._base_url, "/api/playback"),
                    max(0, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                # A healthy late poll can exhaust the confirmation budget.
                # Backend GET failures return None and remain transport errors.
                break
            if not state.get("available") or playback is None:
                raise QobuzSeekError("qbzd seek readback failed")
            if not state.get("trackId") and _id_str(playback.get("track_id")) == track_id:
                raise QobuzSeekError("qbzd seek status readback is incomplete")
            if (state.get("trackId") != track_id
                    or _id_str(playback.get("track_id")) != track_id):
                raise QobuzSeekNotConfirmed("Qobuz track changed during seek")
            is_playing = normalize_state(playback.get("state"), playback.get("is_playing")) == "Playing"
            if not was_playing and (is_playing or state.get("status") == "Playing"):
                raise QobuzSeekNotConfirmed("Qobuz resumed externally during seek")
            # The raw transport identity also guards stale queue metadata at EOF.
            position = float(playback.get("position_secs") or 0)
            # Whole-second telemetry cannot distinguish a close forward seek
            # from natural advancement. Require an observable discontinuity.
            natural_allowance = (
                time.monotonic() - observed_at + 1
                if was_playing else 0
            )
            moved = moved or (
                max(position, state["position"]) < original_position
                or min(position, state["position"]) > original_position + natural_allowance
            )
            allowance = time.monotonic() - started + 1 if state.get("status") == "Playing" else 0
            if (moved and target <= position <= target + allowance
                    and target <= state["position"] <= target + allowance):
                return state
            await asyncio.sleep(min(SEEK_CONFIRM_INTERVAL_S, max(0, deadline - time.monotonic())))
        raise QobuzSeekNotConfirmed(
            "Qobuz seek was not confirmed; suspended playback needs an explicit resume, "
            "and close seeks may be indistinguishable from natural advancement"
        )

    async def play_track(self, track_id: int | str) -> dict:
        """Load a track by id and start playback (cold start).

        A bare resume cannot put audio into an empty fork player (Connect
        handoff with no current track, fresh daemon): the track must be
        downloaded first. The download runs server-side before the reply,
        so this call carries the download-sized timeout instead of the
        2s control-plane default.
        """
        try:
            tid = int(str(track_id).strip())
        except (TypeError, ValueError):
            raise ValueError(f"qbzd play-track needs a numeric track id, got {track_id!r}") from None
        if tid <= 0:
            raise ValueError(f"qbzd play-track needs a numeric track id, got {track_id!r}")
        result = await backend.post_json(
            self._base_url, "/api/playback/play-track", {"track_id": tid},
            timeout=PLAY_TRACK_TIMEOUT,
        )
        if not isinstance(result, dict) or not result.get("playing"):
            raise RuntimeError(f"qbzd refused to play track {tid}")
        return await self.status()

    async def loaded_track_id(self) -> int:
        """Return the raw player track id, or 0 when nothing is loaded."""
        playback = await backend.get_json(self._base_url, "/api/playback") or {}
        try:
            return max(0, int(playback.get("track_id") or 0))
        except (TypeError, ValueError):
            return 0

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
