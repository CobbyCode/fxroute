# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL streaming provider backed by ``tidalapi`` (native FXRoute provider).

Unlike Spotify (external MPRIS player) and Qobuz (external qbzd daemon), TIDAL
is a native provider: catalog/auth live in this package, but playback is the
existing FXRoute playback owner (MPV -> fxroute_dsp_sink -> DSP).  The provider
therefore never owns a transport of its own — transport methods are inherited
as ``ProviderNotImplemented`` and the UI drives the shared ``/api/play`` and
``/api/playback/*`` endpoints — while ``status()`` reflects the committed
FXRoute playback context for the ``tidal`` source via an injected callback.

No ``tidalapi`` object crosses this boundary: every return value is a plain
dict using the shared streaming field names.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar

from artist_enrichment import ArtistEnrichmentService, get_shared_artist_enrichment
from streaming.base.capabilities import Capabilities
from streaming.base.provider import StreamingProvider
from streaming.tidal import auth, catalog, playback

logger = logging.getLogger(__name__)

TIDAL_BACKEND = "tidalapi"


class TidalProvider(StreamingProvider):
    """TIDAL catalog/state provider; playback rides the FXRoute owner."""

    provider_id: ClassVar[str] = "tidal"
    display_name: ClassVar[str] = "TIDAL"

    def __init__(self, artist_enrichment: ArtistEnrichmentService | None = None) -> None:
        self._get_active_playback: Callable[[], dict] | None = None
        self._artist_enrichment = artist_enrichment

    def _enrichment(self) -> ArtistEnrichmentService:
        if self._artist_enrichment is None:
            self._artist_enrichment = get_shared_artist_enrichment()
        return self._artist_enrichment

    def configure(self, get_active_playback: Callable[[], dict]) -> None:
        """Bind the FXRoute playback owner so ``status()`` can report now-playing."""
        self._get_active_playback = get_active_playback

    def capabilities(self) -> Capabilities:
        # Transport/seek/shuffle/loop/volume are provided by the shared FXRoute
        # playback owner; catalog/search/favorites/playlists by tidalapi; and
        # stream info comes from the resolved DASH/FLAC stream.
        return Capabilities(
            transport=True,
            seek=True,
            shuffle=True,
            loop=True,
            progress=True,
            volume=True,
            search=True,
            library=True,
            favorites=True,
            playlists=True,
            cover=True,
            audio_format=True,
            sample_rate=True,
            bit_depth=True,
        )

    def is_installed(self) -> bool:
        return auth.tidalapi_available()

    async def is_available(self) -> bool:
        return auth.tidalapi_available()

    async def backend(self) -> str | None:
        return TIDAL_BACKEND if auth.tidalapi_available() else None

    async def is_authenticated(self) -> bool:
        return await auth.manager.is_authenticated()

    async def status(self) -> dict:
        installed = self.is_installed()
        result: dict[str, Any] = {
            "available": installed,
            "installed": installed,
            "source": self.provider_id,
            "backend": TIDAL_BACKEND if installed else None,
            "authenticated": False,
            "user": None,
            "is_pkce": False,
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
        }
        if not installed:
            return result

        if await auth.manager.is_authenticated():
            result["authenticated"] = True
            session = await auth.manager.get_session()
            user = getattr(session, "user", None)
            result["user"] = {
                "id": getattr(user, "id", None),
                "email": getattr(user, "email", "") if user is not None else "",
                "country_code": getattr(session, "country_code", None),
            }
            result["is_pkce"] = bool(getattr(session, "is_pkce", False))

        # Reflect the committed FXRoute playback context for the tidal source.
        if self._get_active_playback is not None:
            payload = self._get_active_playback() or {}
            track = payload.get("current_track") or {}
            if track.get("source") == "tidal":
                result["status"] = "Playing" if payload.get("playing") and not payload.get("paused") else ("Paused" if payload.get("paused") else "Stopped")
                result["title"] = track.get("title") or ""
                result["artist"] = track.get("artist") or ""
                result["album"] = track.get("album") or ""
                result["trackId"] = str(track.get("id") or "")
                result["artUrl"] = track.get("art_url") or track.get("artUrl") or ""
                result["position"] = float(payload.get("position") or 0)
                result["duration"] = float(track.get("duration") or 0)
                result["volume"] = int(round(float(payload.get("volume") or 100)))
                result["sample_rate"] = track.get("sample_rate_hz")
                result["bit_depth"] = track.get("bit_depth")
                result["audio_format"] = track.get("audio_format")
                result["shuffle"] = bool((payload.get("queue") or {}).get("shuffle"))
                result["loop"] = "playlist" if (payload.get("queue") or {}).get("loop") else "none"
        return result

    # -- catalog / auth (provider boundary) ---------------------------------

    async def search(self, query: str, types: list[str] | None = None, limit: int = 25) -> dict:
        return await _to_thread(catalog.search, query, types, limit)

    async def get_track(self, track_id: str) -> dict:
        return await _to_thread(catalog.get_track, track_id)

    async def get_album_tracks(self, album_id: str) -> list[dict]:
        return await _to_thread(catalog.get_album_tracks, album_id)

    async def favorites(self, limit: int = 50) -> list[dict]:
        return await _to_thread(catalog.favorites_tracks, limit)

    async def favorites_albums(self, limit: int = 50) -> list[dict]:
        return await _to_thread(catalog.favorites_albums, limit)

    async def favorites_artists(self, limit: int = 50) -> list[dict]:
        return await _to_thread(catalog.favorites_artists, limit)

    async def favorite_state(self) -> dict:
        return await _to_thread(catalog.favorite_state)

    async def set_track_favorite(self, track_id: str, favorite: bool) -> dict:
        return await _to_thread(catalog.set_track_favorite, track_id, favorite)

    async def set_album_favorite(self, album_id: str, favorite: bool) -> dict:
        return await _to_thread(catalog.set_album_favorite, album_id, favorite)

    async def set_artist_favorite(self, artist_id: str, favorite: bool) -> dict:
        return await _to_thread(catalog.set_artist_favorite, artist_id, favorite)

    async def set_playlist_favorite(self, playlist_id: str, favorite: bool) -> dict:
        return await _to_thread(catalog.set_playlist_favorite, playlist_id, favorite)

    async def get_artist(self, artist_id: str) -> dict:
        data = await _to_thread(catalog.get_artist, artist_id)
        return await _to_thread(self._attach_artist_enrichment, data)

    def _attach_artist_enrichment(self, data: dict) -> dict:
        """Compose shared artist enrichment for the TIDAL artist detail.

        MusicBrainz/Wikipedia/ListenBrainz availability never affects the
        operator's own TIDAL data: failures degrade to ``enrichment.available``
        false and the normalized artist payload is returned unchanged.
        """
        result = dict(data)
        try:
            enrichment = self._enrichment().enriched_artist(
                "tidal",
                str(result.get("id") or ""),
                str(result.get("name") or ""),
                art_url=str(result.get("art_url") or ""),
                album_titles=[
                    str(album.get("title") or "")
                    for album in (result.get("albums") or [])
                    if str(album.get("title") or "").strip()
                ],
                load_similar=True,
            )
            result["enrichment"] = enrichment
        except Exception as exc:  # noqa: BLE001 - enrichment must never break TIDAL
            logger.warning("TIDAL artist enrichment failed for %s: %s", result.get("id"), exc)
            result["enrichment"] = {"available": False, "error": str(exc)[:200]}
        return result

    async def get_album(self, album_id: str) -> dict:
        data = await _to_thread(catalog.get_album, album_id)
        return await _to_thread(self._attach_album_enrichment, data)

    def _attach_album_enrichment(self, data: dict) -> dict:
        """Compose the shared album enrichment (artist about + MB supplement)."""
        result = dict(data)
        try:
            enrichment = self._enrichment().enriched_album(
                "tidal",
                str(result.get("artist_id") or ""),
                str(result.get("title") or ""),
                str(result.get("artist") or ""),
            )
            result["enrichment"] = enrichment
        except Exception as exc:  # noqa: BLE001 - enrichment must never break TIDAL
            logger.warning("TIDAL album enrichment failed for %s: %s", result.get("id"), exc)
            result["enrichment"] = {"available": False, "error": str(exc)[:200]}
        return result

    async def playlists(self) -> list[dict]:
        return await _to_thread(catalog.user_playlists)

    async def playlist_tracks(self, playlist_id: str) -> list[dict]:
        return await _to_thread(catalog.playlist_tracks, playlist_id)

    async def resolve_stream(self, track_id: str) -> dict:
        return await _to_thread(playback.resolve_stream_for_id, track_id)

    async def start_device_login(self) -> dict:
        link = await auth.manager.start_device_login_async()
        return {
            "verification_uri": link.verification_uri,
            "verification_uri_complete": link.verification_uri_complete,
            "user_code": link.user_code,
            "expires_in": link.expires_in,
        }

    async def finish_device_login(self) -> dict:
        return await auth.manager.finish_device_login_async()

    async def pkce_login_url(self) -> str:
        return await auth.manager.pkce_login_url_async()

    async def finish_pkce_login(self, redirect_url: str) -> dict:
        return await auth.manager.finish_pkce_login_async(redirect_url)

    async def logout(self) -> None:
        await auth.manager.clear()


async def _to_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    import asyncio

    return await asyncio.to_thread(fn, *args, **kwargs)
