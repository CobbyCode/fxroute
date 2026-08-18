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


def favorites_albums(limit: int = 50) -> list[dict]:
    """Return the user's favorited albums (normalized)."""
    _require_tidalapi()
    session = _session()
    try:
        albums = session.user.favorites.albums(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL favorite albums failed: {exc}") from exc
    return [normalize_album(a) for a in albums]


def favorites_artists(limit: int = 50) -> list[dict]:
    """Return the user's favorited artists (normalized)."""
    _require_tidalapi()
    session = _session()
    try:
        artists = session.user.favorites.artists(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL favorite artists failed: {exc}") from exc
    return [normalize_artist(a) for a in artists]


def _favorite_items(favorites: Any, kind: str, page_size: int = 50) -> list:
    """Return all favorited items of ``kind`` (``tracks``/``albums``).

    Prefer the paginated helper when tidalapi offers it (it already walks the
    full collection via the count endpoint); otherwise page explicitly with
    ``limit``/``offset`` so collections beyond one API page are never
    truncated.  Both return plain ``tidalapi`` objects that the caller
    normalizes.
    """
    paginated = getattr(favorites, f"{kind}_paginated", None)
    if callable(paginated):
        try:
            return list(paginated() or [])
        except TypeError:
            pass
    plain = getattr(favorites, kind, None)
    if not callable(plain):
        return []
    items: list = []
    offset = 0
    while True:
        page = list(plain(limit=page_size, offset=offset) or [])
        items.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return items


def favorite_state() -> dict:
    """Return the user's favorited track/album/playlist ids (heart state).

    TIDAL only stamps ``user_date_added`` on items returned from the favorites
    listing, never on search/lookup results, so the UI cannot derive heart
    state from the catalog payload.  This is the single source of truth for
    which tracks/albums/playlists are favorited; the UI compares ids against
    it.  Playlist ids are the same UUIDs the playlists endpoints return.
    """
    _require_tidalapi()
    session = _session()
    try:
        favorites = session.user.favorites
        tracks = _favorite_items(favorites, "tracks")
        albums = _favorite_items(favorites, "albums")
        playlists = _favorite_items(favorites, "playlists")
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL favorite state failed: {exc}") from exc
    return {
        "tracks": [_id_str(getattr(t, "id", None)) for t in tracks],
        "albums": [_id_str(getattr(a, "id", None)) for a in albums],
        "playlists": [_id_str(getattr(p, "id", None)) for p in playlists],
    }


def set_track_favorite(track_id: str, favorite: bool) -> dict:
    """Add/remove a track from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    try:
        favorites = session.user.favorites
        ok = favorites.add_track(str(track_id)) if favorite else favorites.remove_track(str(track_id))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL track favorite update failed: {exc}") from exc
    if not ok:
        raise auth.TidalAuthError("TIDAL track favorite update failed")
    return {"type": "track", "id": str(track_id), "favorite": bool(favorite)}


def set_album_favorite(album_id: str, favorite: bool) -> dict:
    """Add/remove an album from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    try:
        favorites = session.user.favorites
        ok = favorites.add_album(str(album_id)) if favorite else favorites.remove_album(str(album_id))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL album favorite update failed: {exc}") from exc
    if not ok:
        raise auth.TidalAuthError("TIDAL album favorite update failed")
    return {"type": "album", "id": str(album_id), "favorite": bool(favorite)}


def set_playlist_favorite(playlist_id: str, favorite: bool) -> dict:
    """Add/remove a playlist from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    try:
        favorites = session.user.favorites
        if favorite:
            ok = favorites.add_playlist(str(playlist_id))
        else:
            ok = favorites.remove_playlist(str(playlist_id))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlist favorite update failed: {exc}") from exc
    if not ok:
        raise auth.TidalAuthError("TIDAL playlist favorite update failed")
    return {"type": "playlist", "id": str(playlist_id), "favorite": bool(favorite)}


def get_album(album_id: str) -> dict:
    """Fetch one TIDAL album by id and normalize it (title/artist/year/quality)."""
    _require_tidalapi()
    session = _session()
    try:
        album = session.album(album_id)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL album {album_id} lookup failed: {exc}") from exc
    return normalize_album(album)


def user_playlists(limit: int = 200) -> list[dict]:
    """Return the user's own + favorited TIDAL playlists (normalized, deduped).

    ``session.user.playlists()`` returns only playlists the user created, so
    playlists saved from the TIDAL app (editorial/user playlists in the
    favorites collection) would be invisible.  ``playlist_and_favorite_playlists``
    returns both in one list; dedupe on id in case a playlist is both owned
    and favorited.
    """
    _require_tidalapi()
    session = _session()
    try:
        page_size = 50
        seen: dict[str, dict] = {}
        offset = 0
        while True:
            items = session.user.playlist_and_favorite_playlists(offset=offset, limit=page_size)
            for item in items:
                normalized = normalize_playlist(item)
                key = normalized["id"] or normalized["name"]
                seen.setdefault(key, normalized)
            if len(items) < page_size:
                break
            offset += page_size
            if offset >= limit:
                break
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlists failed: {exc}") from exc
    return list(seen.values())


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
