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

import logging
from typing import Any

from streaming.tidal import auth
from streaming.tidal.cache import library_cache

logger = logging.getLogger(__name__)

# tidalapi is imported lazily (see streaming.tidal.auth) so this module stays
# importable without the dependency; helpers raise TidalAuthError then.


def _session() -> Any:
    s = auth.manager.session()
    if s is None:
        raise auth.TidalAuthError("TIDAL is not authenticated")
    return s


def _cache_user_id(session: Any) -> str:
    """Stable TIDAL user id used to key the browse cache ('' when unknown).

    ``session.user`` is already materialized by the caller's own catalog
    access, so this is a cheap read; when it is not available (e.g. a
    partially restored session), fall back to the last-known user id from the
    auth manager.  An unknown user id disables cache I/O, never raising.
    """
    try:
        user = getattr(session, "user", None)
        if user is not None:
            value = getattr(user, "id", None)
            if value is not None and str(value).strip():
                return str(value)
    except Exception:  # noqa: BLE001 - a lazy user fetch can hit the network
        pass
    return auth.manager.last_user_id() or ""


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
        "artist_id": _id_str(getattr(artist_obj, "id", None)) if artist_obj is not None else "",
        "art_url": _cover_url(album),
        "num_tracks": int(getattr(album, "num_tracks", 0) or 0),
        "audio_quality": _name(getattr(album, "audio_quality", None)),
        "version": _name(getattr(album, "version", None)),
        "available": bool(getattr(album, "available", True)),
        "year": int(getattr(album, "year", 0) or 0) or None,
    }


def _album_quality_rank(value: Any) -> int:
    quality = _name(value).upper().replace("-", "_").replace(" ", "_")
    if "HI_RES" in quality or "MASTER" in quality or quality == "MQA":
        return 3
    if "LOSSLESS" in quality:
        return 2
    if quality in {"HIGH", "AAC"}:
        return 1
    return 0


def _album_release_key(album: dict) -> tuple | None:
    """Identify quality variants without collapsing distinct editions."""
    title = " ".join(str(album.get("title") or "").casefold().split())
    artist = " ".join(str(album.get("artist_id") or album.get("artist") or "").casefold().split())
    version = " ".join(str(album.get("version") or "").casefold().split())
    if not title or not artist:
        return None
    return artist, title, version, int(album.get("year") or 0), int(album.get("num_tracks") or 0)


def _album_preference(album: dict) -> tuple[int, int]:
    return _album_quality_rank(album.get("audio_quality")), int(bool(album.get("available", True)))


def _dedupe_albums(albums: list[dict]) -> list[dict]:
    """Drop duplicate album identities while retaining the best quality."""
    result: list[dict] = []
    indexes: dict[tuple, int] = {}
    for album in albums:
        keys: list[tuple] = []
        album_id = str(album.get("id") or "").strip()
        if album_id:
            keys.append(("id", album_id))
        release_key = _album_release_key(album)
        if release_key is not None:
            keys.append(("release", release_key))

        existing_index = None
        for key in keys:
            if key in indexes:
                existing_index = indexes[key]
                break
        if existing_index is None:
            existing_index = len(result)
            result.append(album)
        elif _album_preference(album) > _album_preference(result[existing_index]):
            result[existing_index] = album
        for key in keys:
            indexes[key] = existing_index
    return result


# TIDAL picture UUID -> CDN URL template (same one tidalapi uses for
# album/playlist images). 480px is a valid artist resolution in tidalapi
# (160/320/480/750); 640 is album-only and 403s for artists.
_ARTIST_IMAGE_SIZE = 480
_TIDAL_IMAGE_URL = "https://resources.tidal.com/images/%s/%ix%i.jpg"
# tidalapi's DEFAULT_ARTIST_IMG is substituted when TIDAL has no picture;
# the current CDN does not serve it (403), so it must not be emitted as a
# broken URL — the frontend renders its neutral placeholder instead.
_TIDAL_DEFAULT_ARTIST_IMG = "1e01cdb6-f15d-4d8b-8440-a047976c1cac"


def normalize_artist(artist: Any) -> dict:
    """Normalize a tidalapi Artist into a plain dict."""
    return {
        "id": _id_str(getattr(artist, "id", None)),
        "name": _name(getattr(artist, "name", None)),
        "art_url": _artist_image_url(artist),
    }


def _artist_image_url(artist: Any) -> str:
    """Resolve a TIDAL artist picture URL without extra requests.

    tidalapi parses ``Artist.picture`` as a UUID string (search and favorites
    results always carry it), so the URL is formatted directly from the UUID.
    A callable ``picture``/``image`` helper (other tidalapi versions) is only
    invoked while a picture is already known: the detail-fetch fallback inside
    ``image()`` would otherwise fire one request per artist (N+1), which is
    never wanted for list rows. Artists without a real picture (tidalapi's
    default placeholder UUID) yield an empty URL so the UI shows its neutral
    placeholder instead of a broken image.
    """
    try:
        picture = getattr(artist, "picture", None)
        if isinstance(picture, str) and picture:
            if picture == _TIDAL_DEFAULT_ARTIST_IMG:
                return ""
            return _TIDAL_IMAGE_URL % (picture.replace("-", "/"), _ARTIST_IMAGE_SIZE, _ARTIST_IMAGE_SIZE)
        if picture:
            if callable(picture):
                return str(picture(_ARTIST_IMAGE_SIZE))
            image = getattr(artist, "image", None)
            if callable(image):
                return str(image(_ARTIST_IMAGE_SIZE))
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
        normalized = [normalizer(item) for item in items]
        out[key] = _dedupe_albums(normalized) if key == "albums" else normalized
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


def _cached_or_raise(user_id: str, kind: str, message: str, cause: Exception) -> list | dict:
    """Return the cached payload for ``kind`` or raise ``TidalAuthError(message)``.

    A failed live fetch must not empty the visible library: when the last
    successful payload is cached for this account it is served instead, and
    the cache row itself is never touched by the failure.
    """
    cached = library_cache.get(user_id, kind) if user_id else None
    if cached is not None:
        logger.warning("TIDAL %s fetch failed (%s); serving cached data", kind, cause)
        return cached
    raise auth.TidalAuthError(message) from cause


def favorites_tracks(limit: int = 50) -> list[dict]:
    """Return the user's favorited tracks (normalized); last-known state on failure."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        user = session.user
        favorites = user.favorites
        tracks = favorites.tracks(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _cached_or_raise(user_id, "tracks", f"TIDAL favorites failed: {exc}", exc)
    payload = [normalize_track(t) for t in tracks]
    library_cache.put(user_id, "tracks", payload)
    return payload


def favorites_albums(limit: int = 50) -> list[dict]:
    """Return the user's favorited albums (normalized); last-known state on failure."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        albums = session.user.favorites.albums(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _cached_or_raise(user_id, "albums", f"TIDAL favorite albums failed: {exc}", exc)
    payload = _dedupe_albums([normalize_album(a) for a in albums])
    library_cache.put(user_id, "albums", payload)
    return payload


def favorites_artists(limit: int = 50) -> list[dict]:
    """Return the user's favorited artists (normalized); last-known state on failure."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        artists = session.user.favorites.artists(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _cached_or_raise(user_id, "artists", f"TIDAL favorite artists failed: {exc}", exc)
    payload = [normalize_artist(a) for a in artists]
    library_cache.put(user_id, "artists", payload)
    return payload


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
    """Return the user's favorited track/album/artist/playlist ids (heart state).

    TIDAL only stamps ``user_date_added`` on items returned from the favorites
    listing, never on search/lookup results, so the UI cannot derive heart
    state from the catalog payload.  This is the single source of truth for
    which tracks/albums/artists/playlists are favorited; the UI compares ids
    against it.  Playlist ids are the same UUIDs the playlists endpoints
    return.

    The last successful result is cached per account; on a failed live fetch
    the cached ids are served instead so hearts never regress to unknown.
    """
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        favorites = session.user.favorites
        tracks = _favorite_items(favorites, "tracks")
        albums = _favorite_items(favorites, "albums")
        artists = _favorite_items(favorites, "artists")
        playlists = _favorite_items(favorites, "playlists")
    except Exception as exc:  # noqa: BLE001
        return _cached_or_raise(user_id, "ids", f"TIDAL favorite state failed: {exc}", exc)
    payload = {
        "tracks": [_id_str(getattr(t, "id", None)) for t in tracks],
        "albums": [_id_str(getattr(a, "id", None)) for a in albums],
        "artists": [_id_str(getattr(a, "id", None)) for a in artists],
        "playlists": [_id_str(getattr(p, "id", None)) for p in playlists],
    }
    library_cache.put(user_id, "ids", payload)
    return payload


def _update_cached_favorite_id(user_id: str, kind: str, item_id: str, favorite: bool) -> None:
    """Apply a successful favorite toggle to the cached id state.

    The cached ids are the heart source shown before a background refresh;
    keeping them in sync means a toggle is still reflected the next time the
    tab opens, even when the live refresh fails afterwards.
    """
    if not user_id:
        return
    cached = library_cache.get(user_id, "ids")
    if cached is None or not isinstance(cached, dict):
        return
    values = [str(v) for v in cached.get(kind, [])]
    item = str(item_id)
    if favorite:
        if item not in values:
            values.append(item)
    else:
        values = [v for v in values if v != item]
    cached[kind] = values
    library_cache.put(user_id, "ids", cached)


def set_track_favorite(track_id: str, favorite: bool) -> dict:
    """Add/remove a track from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        favorites = session.user.favorites
        ok = favorites.add_track(str(track_id)) if favorite else favorites.remove_track(str(track_id))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL track favorite update failed: {exc}") from exc
    if not ok:
        raise auth.TidalAuthError("TIDAL track favorite update failed")
    _update_cached_favorite_id(user_id, "tracks", str(track_id), bool(favorite))
    return {"type": "track", "id": str(track_id), "favorite": bool(favorite)}


def set_album_favorite(album_id: str, favorite: bool) -> dict:
    """Add/remove an album from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        favorites = session.user.favorites
        ok = favorites.add_album(str(album_id)) if favorite else favorites.remove_album(str(album_id))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL album favorite update failed: {exc}") from exc
    if not ok:
        raise auth.TidalAuthError("TIDAL album favorite update failed")
    _update_cached_favorite_id(user_id, "albums", str(album_id), bool(favorite))
    return {"type": "album", "id": str(album_id), "favorite": bool(favorite)}


def set_artist_favorite(artist_id: str, favorite: bool) -> dict:
    """Add/remove an artist from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        favorites = session.user.favorites
        ok = favorites.add_artist(str(artist_id)) if favorite else favorites.remove_artist(str(artist_id))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL artist favorite update failed: {exc}") from exc
    if not ok:
        raise auth.TidalAuthError("TIDAL artist favorite update failed")
    _update_cached_favorite_id(user_id, "artists", str(artist_id), bool(favorite))
    return {"type": "artist", "id": str(artist_id), "favorite": bool(favorite)}


def set_playlist_favorite(playlist_id: str, favorite: bool) -> dict:
    """Add/remove a playlist from the user's TIDAL favorites."""
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
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
    _update_cached_favorite_id(user_id, "playlists", str(playlist_id), bool(favorite))
    return {"type": "playlist", "id": str(playlist_id), "favorite": bool(favorite)}


def get_artist(artist_id: str) -> dict:
    """Fetch one TIDAL artist with its albums and top tracks (normalized)."""
    _require_tidalapi()
    session = _session()
    try:
        artist = session.artist(artist_id)
        albums = artist.get_albums(limit=50)
        top_tracks = artist.get_top_tracks(limit=10)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL artist {artist_id} lookup failed: {exc}") from exc
    return {
        **normalize_artist(artist),
        "albums": _dedupe_albums([normalize_album(a) for a in albums]),
        "top_tracks": [normalize_track(t) for t in top_tracks],
    }


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
    """Return the user's favorited TIDAL playlists (My Collection, normalized).

    The TIDAL → Playlists view shows exactly what is in My Collection
    (favorites).  Newly created playlists are auto-favorited so they appear
    immediately; unfavoriting a playlist removes it from this view.  The last
    successful result is cached per account and served when a live fetch fails.
    """
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        favorites = session.user.favorites
        items = favorites.playlists()
    except Exception as exc:  # noqa: BLE001
        return _cached_or_raise(user_id, "playlists", f"TIDAL playlists failed: {exc}", exc)
    payload = [normalize_playlist(item) for item in items]
    library_cache.put(user_id, "playlists", payload)
    return payload


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


def playlist_detail(playlist_id: str) -> dict:
    """Return one TIDAL playlist with its tracks and distinct artist identities.

    The playlist row mirrors :func:`normalize_playlist` (so description and
    art_url stay available for the detail header); ``artists`` lists the
    distinct track artists in first-appearance order with their TIDAL ids,
    which the provider uses to attach the shared artist enrichment when the
    playlist maps to exactly one artist.  Tracks are the same normalized rows
    as :func:`playlist_tracks`.
    """
    _require_tidalapi()
    session = _session()
    try:
        playlist = session.playlist(playlist_id)
        tracks = playlist.tracks()
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlist {playlist_id} lookup failed: {exc}") from exc
    artists: list[dict[str, str]] = []
    seen: set[str] = set()
    for track in tracks:
        artist_obj = getattr(track, "artist", None)
        name = getattr(artist_obj, "name", "") if artist_obj is not None else ""
        aid = getattr(artist_obj, "id", None) if artist_obj is not None else None
        key = str(aid) if aid is not None else name
        if key and key not in seen:
            seen.add(key)
            artists.append({"id": _id_str(aid), "name": name})
    return {
        **normalize_playlist(playlist),
        "tracks": [normalize_track(t) for t in tracks],
        "artists": artists,
    }


def create_playlist(title: str, description: str = "", track_ids: list[str] | None = None) -> dict:
    """Create a new TIDAL playlist, seed it with tracks, and add it to My Collection.

    ``track_ids`` are added right after creation via the same v1 items write
    used by :func:`add_playlist_tracks` (ETag included).  The new playlist is
    also favorited so it appears in My Collection (playlist view = collection
    state).  Both the playlist list and the favorite id caches are dropped so
    the next loads reflect the new entry with an active heart.
    """
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    try:
        playlist = session.user.create_playlist(str(title).strip(), str(description or ""))
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlist creation failed: {exc}") from exc
    ids = [str(i) for i in (track_ids or []) if str(i).strip()]
    if ids:
        _add_items(session, playlist, ids)
    # Add the new playlist to My Collection (favorites) so it appears under
    # TIDAL → Playlists with an active heart immediately.
    pid = str(getattr(playlist, "id", ""))
    if pid:
        try:
            session.user.favorites.add_playlist(pid)
            _update_cached_favorite_id(user_id, "playlists", pid, True)
        except Exception:
            pass  # playlist creation succeeded; favorite is best-effort
    library_cache.delete(user_id, "playlists")
    return normalize_playlist(playlist)


def _add_items(session: Any, playlist: Any, track_ids: list[str]) -> list[str]:
    """Append tracks to a playlist via the v1 items endpoint (live-verified).

    The v1 write requires the playlist ETag as ``If-None-Match`` (optimistic
    concurrency; without it the API answers 412).  Returns the ids TIDAL
    confirmed as added (``addedItemIds``).
    """
    etag = getattr(playlist, "_etag", None)
    headers = {"If-None-Match": etag} if etag else None
    response = session.request.request(
        "POST",
        "playlists/%s/items" % getattr(playlist, "id", ""),
        data={
            "onArtifactNotFound": "SKIP",
            "onDupes": "SKIP",
            "trackIds": ",".join(track_ids),
        },
        headers=headers,
    )
    payload = response.json() if response is not None else {}
    return [str(i) for i in (payload.get("addedItemIds") or [])]


def add_playlist_tracks(playlist_id: str, track_ids: list[str]) -> dict:
    """Add tracks to an existing TIDAL playlist (v1 items endpoint, live-verified).

    The cached playlist list is dropped afterwards so the next load reflects
    the updated track counts.
    """
    _require_tidalapi()
    session = _session()
    user_id = _cache_user_id(session)
    ids = [str(i) for i in track_ids if str(i).strip()]
    if not ids:
        raise auth.TidalAuthError("TIDAL playlist add requires at least one track id")
    try:
        playlist = session.playlist(str(playlist_id))
        added = _add_items(session, playlist, ids)
    except Exception as exc:  # noqa: BLE001
        raise auth.TidalAuthError(f"TIDAL playlist update failed: {exc}") from exc
    library_cache.delete(user_id, "playlists")
    return {
        "playlist_id": str(playlist_id),
        "added_track_ids": added,
    }
