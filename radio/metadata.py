"""Optional, cached radio metadata providers.

This module is deliberately independent from the audio player.  Provider
failure can only make metadata disappear; it can never affect stream loading.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import requests


RP_CHANNELS = {"rp-main": 0, "rp-mellow": 1, "rp-rock": 2, "rp-global": 3}
FIP_STATIONS = {
    "fip-main": 7, "fip-rock": 64, "fip-jazz": 65, "fip-groove": 66,
    "fip-world": 69, "fip-nouveautes": 70, "fip-reggae": 71,
    "fip-electro": 74, "fip-metal": 77, "fip-pop": 78, "fip-hiphop": 95,
}


def _text(value: Any) -> str | None:
    value = str(value or "").strip()
    return value or None


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def _base(station_id: str, provider: str, now: float) -> dict[str, Any]:
    return {
        "station_id": station_id, "provider": provider, "track_id": None,
        "artist": None, "title": None, "album": None, "cover_url": None,
        "started_at": None, "ends_at": None, "duration_seconds": None,
        "progress_seconds": None, "history": [], "source": "provider",
        "fetched_at": now, "stale": False,
    }


def parse_radio_paradise(station_id: str, channel: int, payload: Any, now: float) -> tuple[dict, float]:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise ValueError("invalid Radio Paradise playlist")
    item = payload[0]
    started = _number(item.get("sched_time"))
    duration_ms = _number(item.get("duration"))
    duration = duration_ms / 1000 if duration_ms else None
    title = _text(item.get("title"))
    if not title:
        raise ValueError("Radio Paradise title missing")
    result = _base(station_id, "radio_paradise", now)
    identity = _text(item.get("event")) or _text(item.get("song_id")) or title
    result.update({
        "track_id": f"rp:{channel}:{identity}", "artist": _text(item.get("artist")),
        "title": title, "album": _text(item.get("album")),
        "cover_url": _text(item.get("cover")), "started_at": started,
        "duration_seconds": duration,
    })
    if started is not None and duration is not None:
        result["ends_at"] = started + duration
        result["progress_seconds"] = min(duration, max(0.0, now - started))
    ttl = max(5.0, min(30.0, (result.get("ends_at") or (now + 10)) - now + 1))
    return result, ttl


def parse_fip(station_id: str, radio_id: int, payload: Any, now: float) -> tuple[dict, float]:
    if not isinstance(payload, dict) or not isinstance(payload.get("now"), dict):
        raise ValueError("invalid FIP metadata")
    item = payload["now"]
    line = _text(item.get("secondLine"))
    if not line:
        raise ValueError("FIP title missing")
    artist, title = (None, line)
    if " • " in line:
        artist, title = (part.strip() or None for part in line.split(" • ", 1))
    started, ends = _number(item.get("startTime")), _number(item.get("endTime"))
    cover = _text(item.get("cover"))
    result = _base(station_id, "fip", now)
    identity = _text(item.get("secondLineSongUuid")) or f"{started}:{line}"
    result.update({
        "track_id": f"fip:{radio_id}:{identity}", "artist": artist, "title": title,
        "cover_url": f"https://www.radiofrance.fr/pikapi/images/{cover}/512x512" if cover else None,
        "started_at": started, "ends_at": ends,
    })
    if started is not None and ends is not None and ends > started:
        result["duration_seconds"] = ends - started
        result["progress_seconds"] = min(ends - started, max(0.0, now - started))
    delay = _number(payload.get("delayToRefresh"))
    ttl = (delay / 1000) if delay is not None else ((ends - now + 1) if ends else 20)
    return result, max(5.0, min(180.0, ttl))


def parse_somafm(station_id: str, payload: Any, now: float) -> tuple[dict, float]:
    if not isinstance(payload, dict):
        raise ValueError("invalid SomaFM metadata")
    songs = payload.get("songs") if isinstance(payload.get("songs"), list) else payload.get("song")
    if not isinstance(songs, list) or not songs or not isinstance(songs[0], dict):
        raise ValueError("invalid SomaFM songs")
    item = songs[0]
    title = _text(item.get("title"))
    if not title:
        raise ValueError("SomaFM title missing")
    started = _number(item.get("date"))
    result = _base(station_id, "somafm", now)
    result.update({
        "track_id": f"soma:{station_id}:{_text(item.get('date')) or title}",
        "artist": _text(item.get("artist")), "title": title,
        "album": _text(item.get("album")), "cover_url": _text(item.get("albumArt")),
        "started_at": started,
    })
    result["history"] = [
        {"artist": _text(song.get("artist")), "title": _text(song.get("title")),
         "album": _text(song.get("album")), "started_at": _number(song.get("date"))}
        for song in songs[1:4] if isinstance(song, dict) and _text(song.get("title"))
    ]
    return result, 20.0


def parse_kexp(station_id: str, payload: Any, now: float) -> tuple[dict, float]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("invalid KEXP metadata")
    item = next((row for row in payload["results"] if isinstance(row, dict) and row.get("play_type") == "trackplay"), None)
    if not item or not _text(item.get("song")):
        raise ValueError("KEXP track missing")
    started = None
    try:
        started = datetime.fromisoformat(str(item.get("airdate", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        pass
    result = _base(station_id, "kexp", now)
    result.update({
        "track_id": f"kexp:{item.get('id')}", "artist": _text(item.get("artist")),
        "title": _text(item.get("song")), "album": _text(item.get("album")),
        "cover_url": _text(item.get("image_uri")) or _text(item.get("thumbnail_uri")),
        "started_at": started,
    })
    return result, 20.0


@dataclass
class _Cache:
    value: dict | None = None
    fresh_until: float = 0.0
    failures: int = 0
    retry_at: float = 0.0


class RadioMetadataService:
    def __init__(self, *, http_get: Callable[..., Any] = requests.get, clock: Callable[[], float] = time.time):
        self._http_get = http_get
        self._clock = clock
        self._cache: dict[str, _Cache] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def provider_for(station_id: str, stream_url: str = "") -> tuple[str, Any] | None:
        sid, url = station_id.removeprefix("radio_"), (stream_url or "").lower()
        if sid in RP_CHANNELS:
            return "rp", RP_CHANNELS[sid]
        if sid in FIP_STATIONS:
            return "fip", FIP_STATIONS[sid]
        if sid == "kexp-main" or "kexp.streamguys" in url:
            return "kexp", None
        if sid in {"live", "defcon"}:
            return None
        if "somafm.com" in url or sid in {"groovesalad", "dronezone", "secretagent"}:
            match = re.search(r"/(?:songs/)?([a-z0-9]+?)(?:-128-aac|130\.pls|\.json)(?:[/?]|$)", url)
            return "soma", match.group(1) if match else sid
        return None

    async def get(self, station_id: str, stream_url: str = "") -> dict | None:
        station_id = station_id.removeprefix("radio_")
        mapping = self.provider_for(station_id, stream_url)
        if not mapping:
            return None
        now = self._clock()
        cache = self._cache.setdefault(station_id, _Cache())
        if cache.value and now < cache.fresh_until:
            return self._with_progress(cache.value, now)
        if now < cache.retry_at:
            return self._stale(cache.value, now)
        lock = self._locks.setdefault(station_id, asyncio.Lock())
        async with lock:
            now = self._clock()
            if cache.value and now < cache.fresh_until:
                return self._with_progress(cache.value, now)
            try:
                value, ttl = await asyncio.to_thread(self._fetch, station_id, mapping, now)
                cache.value, cache.fresh_until = value, now + ttl
                cache.failures, cache.retry_at = 0, 0
                return self._with_progress(value, now)
            except Exception:
                cache.failures += 1
                cache.retry_at = now + min(120, 10 * (2 ** (cache.failures - 1)))
                return self._stale(cache.value, now)

    def _fetch(self, station_id: str, mapping: tuple[str, Any], now: float) -> tuple[dict, float]:
        provider, arg = mapping
        if provider == "rp":
            url, parser = f"https://api.radioparadise.com/api/playlist?chan={arg}", lambda p: parse_radio_paradise(station_id, arg, p, now)
        elif provider == "fip":
            url, parser = f"https://api.radiofrance.fr/livemeta/live/{arg}/transistor_musical_player", lambda p: parse_fip(station_id, arg, p, now)
        elif provider == "soma":
            url, parser = f"https://somafm.com/songs/{arg}.json", lambda p: parse_somafm(station_id, p, now)
        else:
            url, parser = "https://api.kexp.org/v2/plays/?format=json&limit=3", lambda p: parse_kexp(station_id, p, now)
        response = self._http_get(url, timeout=(2, 5), headers={"User-Agent": "FXRoute/RadioMetadata"})
        response.raise_for_status()
        return parser(response.json())

    @staticmethod
    def _with_progress(value: dict, now: float) -> dict:
        result = dict(value)
        started, duration = result.get("started_at"), result.get("duration_seconds")
        if started is not None and duration is not None:
            result["progress_seconds"] = min(duration, max(0.0, now - started))
        result["stale"] = False
        return result

    @staticmethod
    def _stale(value: dict | None, now: float) -> dict | None:
        if not value or now - float(value.get("fetched_at") or 0) > 90:
            return None
        result = RadioMetadataService._with_progress(value, now)
        result["stale"] = True
        return result
