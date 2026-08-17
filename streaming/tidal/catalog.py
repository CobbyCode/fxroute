# SPDX-License-Identifier: AGPL-3.0-only

"""TIDAL catalog access, normalized at the provider boundary.

This module is the only place that reads ``tidalapi`` search/catalog objects
and turns them into plain dicts using the shared streaming field names
(``id``, ``title``, ``artist``, ``album``, ``art_url``, ``duration``).  No
``tidalapi`` object crosses this boundary; the provider and API only ever see
the normalized dicts.

Sample rate / bit depth / format are *not* part of the catalog model: TIDAL
only exposes those once a stream is resolved, which is owned by
:mod:`streaming.tidal.playback`.
"""

from __future__ import annotations

from typing import Any

from streaming.tidal import auth

# tidalapi is imported lazily (see streaming.tidal.auth) so this module stays
# importable without the dependency; helpers raise TidalAuthError then.


def _session() -> Any:
    s = auth.manager.session()
    if s is None:
        raise auth.TidalAuthError("TIDAL is not authenticated")
    return s


def _require_tidalapi() -> None:
    if not auth.tidalapi_available():
        raise auth.TidalAuthError("tidalapi is not installed")


def _name(value: Any) -> str:
    return "" if value is None else str(value)


def _id_str(value: Any) -> str:
    return "" if value is None else str(value)


def _cover_url(album: Any, size: int = 640) -> str:
    try:
        image = getattr(album, "image", None)
        if callable(image):
            return str(image(size))
    except Exception:
        pass
    return ""


def normalize_track(track: Any) -> dict:
    """Normalize a tidalapi Track into a plain dict."""
    artist_obj = getattr(track, "artist", None)
    artist = getattr(artist_obj, "name", "") if artist_obj is not None else ""
    artists = [getattr(a, "name", "") for a in (getattr(track, "artists", None) or [])]
    album_obj = getattr(track, "album", None)
    return {
        "id": _id_str(getattr(track, "id", None)),
        "title": _name(getattr(track, "name", None) or getattr(track, "title", None)),
        "artist": artist,
        "artists": [a for a in artists if a],
        "album": getattr(album_obj, "name", "") if album_obj is not None else "",
        "art_url": _cover_url(album_obj),
        "duration": float(getattr(track, "duration", 0) or 0),
        "audio_quality": _name(getattr(track, "audio_quality", None)),
        "is_hi_res_lossless": bool(getattr(track, "is_hi_res_lossless", False)),
        "is_lossless": bool(getattr(track, "is_lossless", False)),
        "available": bool(getattr(track, "available", True)),
        "explicit": bool(getattr(track, "explicit", False)),
    }


def normalize_album(album: Any) -> dict:
    """Normalize a tidalapi Album into a plain dict."""
    artist_obj = getattr(album, "artist", None)
    return {
        "id": _id_str(getattr(album, "id", None)),
        "title": _name(getattr(album, "name", None) or getattr(album, "title", None)),
        "artist": getattr(artist_obj, "name", "") if artist_obj is not None else "",
        "art_url": _cover_url(album),
        "num_tracks": int(getattr(album, "num_tracks", 0) or 0),
        "audio_quality": _name(getattr(album, "audio_quality", None)),
        "available": bool(getattr(album, "available", True)),
        "year": int(getattr(album, "year", 0) or 0) or None,
    }


def normalize_artist(artist: Any) -> dict:
    """Normalize a tidalapi Artist into a plain dict."""
    return {
        "id": _id_str(getattr(artist, "id", None)),
        "name": _name(getattr(artist, "name", None)),
        "art_url": _artist_image_url(artist),
    }


def _artist_image_url(artist: Any) -> str:
    try:
        picture = getattr(artist, "picture", None)
        if callable(picture):
            return str(picture(640))
        image = getattr(artist, "image", None)
        if callable(image):
            return str(image(640))
    except Exception:
        pass
    return ""


def normalize_playlist(playlist: Any) -> dict:
    """Normalize a tidalapi Playlist into a plain dict."""
    return {
        "id": _id_str(getattr(playlist, "id", None)),
        "name": _name(getattr(playlist, "name", None)),
        "track_count": int(getattr(playlist, "num_tracks", 0) or 0),
        "art_url": _playlist_image_url(playlist),
        "description": _name(getattr(playlist, "description", None)),
    }


def _playlist_image_url(playlist: Any) -> str:
    for attr in ("square_picture", "image", "picture"):
        fn = getattr(playlist, attr, None)
        if callable(fn):
            try:
                return str(fn(640))
            except Exception:
                continue
    return ""


# Search model keys -> normalizer. ``models`` names below mirror tidalapi's
# ``SearchResults`` dict keys (tracks/albums/artists/playlists).
_SEARCH_NORMALIZERS = {
    "tracks": normalize_track,
    "albums": normalize_album,
    "artists": normalize_artist,
    "playlists": normalize_playlist,
}


def search(query: str, types: list[str] | None = None, limit: int = 25) -> dict:
    """Search TIDAL for the requested content types.

    ``types`` selects from ``tracks``, ``albums``, ``artists``, ``playlists``
    (default: tracks only).  Returns ``{type: [normalized dict, ...]}``.
    """
    _require_tidalapi()
    session = _session()
    selected = [t for t in (types or ["tracks"]) if t in _SEARCH_NORMALIZERS]
    if not selected:
        selected = ["tracks"]

    import tidalapi

    model_map = {
        "tracks": tidalapi.Track,
        "albums": tidalapi.Album,
        "artists": tidalapi.Artist,
        "playlists": tidalapi.Playlist,
    }
    models = [model_map[t] for t in selected]
    try:
        results = session.search(query, models=models, limit=limit)
    except Exception as exc:  # noqa: BLE001 - tidalapi raises broad types
        raise auth.TidalAuthError(f"TIDAL search failed: {exc}") from exc

    out: dict[str, list] = {}
    for key in selected:
        items = results.get(key) if hasattr(results, "get") else getattr(results, key, None)
        if items is None:
            out[key] = []
            continue
        normalizer = _SEARCH_NORMALIZERS[key]
        out[key] = [normalizer(item) for item in items]
    return out


def get_track(track_id: str) -> dict:
    """Fetch one TIDAL track by id and normalize it."""
    _require_tidalapi()
    session = _session()
    import tidalapi

    try:
        track = session.track(track_id)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL track {track_id} lookup failed: {exc}") from exc
    return normalize_track(track)


def get_album_tracks(album_id: str) -> list[dict]:
    """Return the normalized tracks of one TIDAL album."""
    _require_tidalapi()
    session = _session()
    import tidalapi

    try:
        album = session.album(album_id)
        tracks = album.tracks()
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL album {album_id} lookup failed: {exc}") from exc
    return [normalize_track(t) for t in tracks]


def favorites_tracks(limit: int = 50) -> list[dict]:
    """Return the user's favorited tracks (normalized)."""
    _require_tidalapi()
    session = _session()
    try:
        user = session.user
        favorites = user.favorites
        tracks = favorites.tracks(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL favorites failed: {exc}") from exc
    return [normalize_track(t) for t in tracks]


def user_playlists() -> list[dict]:
    """Return the user's own TIDAL playlists (normalized)."""
    _require_tidalapi()
    session = _session()
    try:
        playlists = session.user.playlists()
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlists failed: {exc}") from exc
    return [normalize_playlist(p) for p in playlists]


def playlist_tracks(playlist_id: str) -> list[dict]:
    """Return the normalized tracks of one TIDAL playlist."""
    _require_tidalapi()
    session = _session()
    try:
        playlist = session.playlist(playlist_id)
        tracks = playlist.tracks()
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlist {playlist_id} lookup failed: {exc}") from exc
    return [normalize_track(t) for t in tracks]
